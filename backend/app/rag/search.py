"""Гибридный поиск: полнотекстовый (Postgres, русская морфология) + векторный (pgvector, BGE-M3) → RRF → реранкер.

Лексический поиск ловит точные термины и номера пунктов («п. 5.3», «СНИЛС»), векторный — перефразировки
(«во сколько приходить» ↔ «начало рабочего дня»). Реранкер-кросс-энкодер (bge-reranker-v2-m3) даёт
калиброванную оценку релевантности, по которой бот решает: отвечать или передать вопрос HR.
"""

from dataclasses import dataclass

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.rag.ingest import embed


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    title: str
    heading: str
    text: str
    score: float  # оценка реранкера (0..1) или RRF, если реранкер не настроен


SITE_FILTER = "d.status = 'ready' AND (cardinality(d.site_codes) = 0 OR :site = ANY(d.site_codes))"

VECTOR_SQL = text(f"""
    SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
    WHERE {SITE_FILTER}
    ORDER BY c.embedding <=> CAST(:vec AS vector) LIMIT :k
""")

# Запрос превращаем в OR по леммам: websearch/plainto_tsquery требуют все слова сразу,
# а в живом вопросе («подскажи, как получить доступ к порталу?») половина слов в документе не встречается.
LEXICAL_SQL = text(f"""
    WITH q AS (
        SELECT to_tsquery('simple', coalesce(array_to_string(array(
            SELECT quote_literal(lex) FROM unnest(tsvector_to_array(to_tsvector('russian', :q))) AS lex
        ), ' | '), '')) AS q
    )
    SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id, q
    WHERE {SITE_FILTER} AND numnode(q.q) > 0 AND c.tsv @@ q.q
    ORDER BY ts_rank_cd(c.tsv, q.q) DESC LIMIT :k
""")

FETCH_SQL = text("""
    SELECT c.id, c.document_id, d.title, c.heading, c.text
    FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ANY(:ids)
""")


def rrf(rankings: list[list[int]], k: int) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


async def rerank(query: str, texts: list[str]) -> list[float]:
    async with httpx.AsyncClient(timeout=120) as http:
        r = await http.post(
            f"{settings().rerank_url}/rerank", json={"query": query, "texts": texts, "truncate": True}
        )
        r.raise_for_status()
    scores = [0.0] * len(texts)
    for item in r.json():
        scores[item["index"]] = item["score"]
    return scores


async def search(
    s: AsyncSession,
    query: str,
    site: str = "",
    top_n: int | None = None,
    mode: str = "hybrid",
    use_rerank: bool = True,
) -> list[Hit]:
    """mode: hybrid | vector | lexical — отдельные режимы нужны для абляции в eval/run_eval.py."""
    cfg = settings()
    k = cfg.retrieve_candidates
    rankings = []
    if mode in ("hybrid", "vector"):
        [vec] = await embed([query])
        rankings.append(list(await s.scalars(VECTOR_SQL, {"vec": str(vec), "site": site, "k": k})))
    if mode in ("hybrid", "lexical"):
        rankings.append(list(await s.scalars(LEXICAL_SQL, {"q": query, "site": site, "k": k})))
    fused = rrf(rankings, cfg.rrf_k)[: cfg.rerank_candidates]
    if not fused:
        return []
    rows = {r.id: r for r in (await s.execute(FETCH_SQL, {"ids": [cid for cid, _ in fused]})).all()}
    hits = [
        Hit(cid, rows[cid].document_id, rows[cid].title, rows[cid].heading, rows[cid].text, sc)
        for cid, sc in fused
    ]
    if cfg.rerank_url and use_rerank:
        scores = await rerank(query, [f"{h.heading}\n{h.text}" for h in hits])
        for h, sc in zip(hits, scores, strict=True):
            h.score = sc
        hits.sort(key=lambda h: h.score, reverse=True)
    return hits[: top_n or cfg.rerank_top_n]
