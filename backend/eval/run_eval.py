"""Оценка RAG на размеченном наборе eval/questions.yaml.

1. Поиск (без LLM): Hit@1/3/5 и MRR для режимов vector / lexical / hybrid / hybrid+rerank — вклад каждого компонента.
2. Калибровка порога: по оценкам реранкера на отвечаемых и неотвечаемых вопросах выбирается порог
   с максимальной сбалансированной точностью — он и становится RERANK_MIN_SCORE (не «магическое число»).
3. Сквозной прогон (с LLM, флаг --e2e): доля ответов, фактическая точность, доля ложных ответов на вопросы вне базы.

Запуск: uv run python eval/run_eval.py [--e2e]   → eval/results.md
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import session
from app.rag.answer import answer
from app.rag.search import search

HERE = Path(__file__).resolve().parent
MODES = [("vector", False), ("lexical", False), ("hybrid", False), ("hybrid", True)]


def norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", s.lower().replace("ё", "е"))


def rank_of(hits, must: str | list[str]) -> int | None:
    keys = [norm(m) for m in ([must] if isinstance(must, str) else must)]
    for i, h in enumerate(hits, 1):
        text = norm(f"{h.heading} {h.text}")
        if any(k in text for k in keys):
            return i
    return None


async def retrieval(qs: list[dict]) -> tuple[dict, dict]:
    pos = [q for q in qs if q.get("answerable", True)]
    table, top_scores = {}, {"pos": [], "neg": []}
    async with session() as s:
        for mode, rr in MODES:
            ranks = []
            for q in pos:
                hits = await search(s, q["q"], top_n=20, mode=mode, use_rerank=rr)
                ranks.append(rank_of(hits, q["must"]))
                if mode == "hybrid" and rr:  # оценки этого режима и есть оценки продакшен-поиска — сразу в калибровку
                    top_scores["pos"].append(hits[0].score if hits else 0.0)
            name = f"{mode}{' + rerank' if rr else ''}"
            table[name] = {
                "hit@1": sum(r == 1 for r in ranks if r) / len(ranks),
                "hit@3": sum(1 for r in ranks if r and r <= 3) / len(ranks),
                "hit@5": sum(1 for r in ranks if r and r <= 5) / len(ranks),
                "hit@10": sum(1 for r in ranks if r and r <= 10) / len(ranks),
                "hit@20": sum(1 for r in ranks if r) / len(ranks),
                "mrr": sum(1 / r for r in ranks if r) / len(ranks),
                "misses": [q["q"] for q, r in zip(pos, ranks, strict=True) if not r or r > 5],
            }
        for q in qs:
            if not q.get("answerable", True):
                hits = await search(s, q["q"], top_n=1)
                top_scores["neg"].append(hits[0].score if hits else 0.0)
    return table, top_scores


def calibrate(top: dict) -> tuple[float, float]:
    """Порог с максимальной сбалансированной точностью: отвечать на отвечаемые, отказывать на прочие."""
    best_t, best_acc = 0.0, -1.0
    for t in sorted(set(top["pos"] + top["neg"])):
        tpr = sum(x >= t for x in top["pos"]) / len(top["pos"])
        tnr = sum(x < t for x in top["neg"]) / len(top["neg"])
        if (tpr + tnr) / 2 > best_acc:
            best_t, best_acc = t, (tpr + tnr) / 2
    # все пороги между соседними оценками дают ту же точность — берём середину зазора (максимальный запас)
    below = [x for x in top["pos"] + top["neg"] if x < best_t]
    return ((max(below) + best_t) / 2 if below else best_t), best_acc


def has_facts(answer: str, expect: list) -> bool:
    """Каждый ожидаемый факт есть в ответе; вложенный список — варианты записи одного факта («шест», «6 мес»)."""
    text = answer.lower().replace("ё", "е")
    return all(any(v.lower().replace("ё", "е") in text for v in (e if isinstance(e, list) else [e])) for e in expect)


async def end_to_end(qs: list[dict]) -> list[dict]:
    rows = []
    async with session() as s:
        for q in qs:
            t0 = time.monotonic()
            res = await answer(s, q["q"])
            ok = None
            if q.get("answerable", True):
                ok = res.answered and has_facts(res.text, q.get("expect", []))
            rows.append({"q": q["q"], "answerable": q.get("answerable", True), "answered": res.answered, "correct": ok,
                         "reason": res.reason, "score": res.best_score, "answer": res.text, "sec": round(time.monotonic() - t0, 1)})
            print(f"  [{rows[-1]['sec']}s] {'✓' if (ok or (not q.get('answerable', True) and not res.answered)) else '✗'} {q['q']} → {res.reason}")
    return rows


def report(table: dict, top: dict, thr: tuple[float, float], e2e: list[dict] | None) -> str:
    cfg = settings()
    lines = [
        "# Оценка RAG «Корсик»",
        "",
        (f"Набор: {len(top['pos'])} вопросов с ответом в базе знаний и {len(top['neg'])} без ответа "
        f"([questions.yaml](questions.yaml)). Эмбеддинги BAAI/bge-m3, реранкер BAAI/bge-reranker-v2-m3, "
        f"чанк до {cfg.chunk_max_chars} символов, LLM `{cfg.llm_model}`."),
        "",
        "## Поиск: вклад компонентов",
        "",
        "| Режим | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Hit@20 | MRR |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, m in table.items():
        lines.append(f"| {name} | {m['hit@1']:.0%} | {m['hit@3']:.0%} | {m['hit@5']:.0%} | {m['hit@10']:.0%} | {m['hit@20']:.0%} | {m['mrr']:.2f} |")
    misses = table["hybrid + rerank"]["misses"]
    if misses:
        lines += ["", "Не найдено в топ-5 (hybrid + rerank): " + "; ".join(misses)]
    lines += [
        "",
        "## Порог уверенности (калибровка)",
        "",
        (f"Порог реранкера с максимальной сбалансированной точностью: **{thr[0]:.3f}** "
        f"(balanced accuracy {thr[1]:.0%}); он же применяется в сквозном прогоне и в RERANK_MIN_SCORE."),
        "",
        (f"Оценка лучшего фрагмента — вопросы с ответом: min {min(top['pos']):.3f}, медиана {sorted(top['pos'])[len(top['pos']) // 2]:.3f}; "
        f"вопросы без ответа: max {max(top['neg']):.3f}, медиана {sorted(top['neg'])[len(top['neg']) // 2]:.3f}."),
    ]
    if e2e:
        pos = [r for r in e2e if r["answerable"]]
        neg = [r for r in e2e if not r["answerable"]]
        reasons: dict[str, int] = {}
        for r in e2e:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        lines += [
            "",
            "## Сквозной прогон (поиск + LLM + проверки)",
            "",
            (f"- Ответил на вопросы из базы: **{sum(r['answered'] for r in pos)}/{len(pos)}**, из них фактически верно: "
            f"**{sum(bool(r['correct']) for r in pos)}/{len(pos)}**"),
            f"- Ложных ответов на вопросы вне базы (галлюцинаций): **{sum(r['answered'] for r in neg)}/{len(neg)}**",
            f"- Причины отказов и ответов: {', '.join(f'{k}: {v}' for k, v in sorted(reasons.items()))}",
            f"- Среднее время ответа: {sum(r['sec'] for r in e2e) / len(e2e):.1f} с",
            "",
            "| Вопрос | Ожидание | Результат | Причина |",
            "|---|---|---|---|",
        ]
        for r in e2e:
            verdict = ("✅ верно" if r["correct"] else ("⚠️ ответ без ключевого факта" if r["answered"] else "↪️ передано HR")) if r["answerable"] \
                else ("❌ ложный ответ" if r["answered"] else "✅ отказ")
            lines.append(f"| {r['q']} | {'ответ' if r['answerable'] else 'отказ'} | {verdict} | {r['reason']} |")
    return "\n".join(lines) + "\n"


async def main() -> None:
    qs = yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))
    print("Поиск...")
    table, top = await retrieval(qs)
    thr = calibrate(top)
    for name, m in table.items():
        print(f"  {name:18} hit@1={m['hit@1']:.0%} hit@5={m['hit@5']:.0%} hit@10={m['hit@10']:.0%} hit@20={m['hit@20']:.0%} mrr={m['mrr']:.2f}")
    print(f"  порог={thr[0]:.3f} balanced_acc={thr[1]:.0%}")
    e2e = None
    if "--e2e" in sys.argv:
        settings().rerank_min_score = round(thr[0], 4)  # сквозной прогон — с откалиброванным порогом
        print("Сквозной прогон...")
        e2e = await end_to_end(qs)
        (HERE / "results.json").write_text(json.dumps(e2e, ensure_ascii=False, indent=1), encoding="utf-8")
    (HERE / "results.md").write_text(report(table, top, thr, e2e), encoding="utf-8")
    print(f"→ {HERE / 'results.md'}")


if __name__ == "__main__":
    asyncio.run(main())
