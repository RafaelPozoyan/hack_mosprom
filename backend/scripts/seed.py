"""Демо-данные: база знаний + сотрудники на разных этапах адаптации.

Ответы сотрудников не пишутся в базу напрямую — они проходят через тот же обработчик, что и сообщения из MAX
(опросы, NLP-анализ, риски, эскалации, push руководителю). Затем время событий сдвигается в прошлое,
чтобы дашборд показал динамику. Персонажи и их ответы вымышлены.

Запуск: uv run python scripts/seed.py [папки с документами...]   (по умолчанию ../materials и kb/)
"""

import asyncio
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot import dialog, response_time
from app.bot.scenario import add_months, next_workday, schedule_employee, step, step_run_at
from app.channels import InEvent
from app.config import settings
from app.db import init_db, session
from app.main import seed_sites
from app.models import Document, Employee, Job, Staff, Ticket, TicketDeadline
from app.rag.ingest import ingest

ROOT = Path(__file__).resolve().parents[1]
KB_EXT = {".pdf", ".pptx", ".docx", ".md", ".txt"}
TABLES = ["messages", "survey_answers", "analyses", "tickets", "questions"]

STAFF = [
    ("Анна Смирнова", "hr", None),
    ("Мария Кузнецова", "recruiter", None),
    ("Игорь Волков", "manager", None),
    ("Елена Орлова", "head", None),
]

# (ФИО, должность, площадка, дней с выхода, форма обращения, сценарий ответов)
PEOPLE = [
    ("Петров Иван", "Инженер-технолог", "GTP", -2, None, []),
    (
        "Соколова Ольга",
        "Специалист по закупкам",
        "Luzhniki",
        4,
        "ty",
        [
            ("d3_survey", {"docs": "a", "team": "b", "tasks": "c"}),
            ("chat", "Как получить доступ к корпоративному порталу?"),
            ("chat", "Какие документы нужны для оформления?"),
        ],
    ),
    (
        "Новиков Дмитрий",
        "Аналитик данных",
        "GrPP",
        16,
        "vy",
        [
            ("d3_survey", {"docs": "a", "team": "a", "tasks": "a"}),
            (
                "w2_check",
                {
                    "tasks_clear": "yes",
                    "info_enough": ("no", "Не хватает доступа к отчётам в 1С, жду уже неделю, работа стоит"),
                    "manager_available": "9",
                    "manager_feedback": "8",
                    "peers_support": "9",
                    "it_support": (
                        "4",
                        "Ноутбук выдали только на четвёртый день, VPN до сих пор не настроен",
                    ),
                    "access_timely": ("3", "Доступы к 1С и CRM дали только через неделю после выхода"),
                    "hr_support": "9",
                    "hr_onboarding": "10",
                },
            ),
            ("chat", "Во сколько начинается рабочий день?"),
            ("chat", "Можно ли принимать подарки от поставщиков?"),
        ],
    ),
    (
        "Морозова Алина",
        "Оператор экструзионной линии",
        "GTP",
        55,
        "ty",
        [
            ("d3_survey", {"docs": "a", "team": "c", "tasks": "b"}),
            (
                "w2_check",
                {
                    "tasks_clear": "yes",
                    "info_enough": "yes",
                    "manager_available": (
                        "6",
                        "Наставник постоянно занят, отвечает раздражённо и при всех говорит, что у него нет времени объяснять очевидное",
                    ),
                    "manager_feedback": ("5", "Обратной связи почти нет, узнаю об ошибках от коллег"),
                    "peers_support": ("6", "Коллеги помогают, но наставник сказал их не отвлекать"),
                    "it_support": "8",
                    "access_timely": "9",
                    "hr_support": "8",
                    "hr_onboarding": "9",
                },
            ),
            ("m2_congrats", {"progress": "c"}),
            ("m1w3_survey", {"mood": "c", "helpers": ["peers"], "accelerate": "clear_tasks"}),
            ("chat", "Есть ли корпоративный транспорт до площадки?"),
        ],
    ),
    (
        "Лебедев Павел",
        "Менеджер по продажам",
        "Luzhniki",
        91,
        "ty",
        [
            ("d3_survey", {"docs": "a", "team": "a", "tasks": "a"}),
            (
                "w2_check",
                {
                    "tasks_clear": "yes",
                    "info_enough": "yes",
                    "manager_available": "9",
                    "manager_feedback": "9",
                    "peers_support": "10",
                    "it_support": "9",
                    "access_timely": "8",
                    "hr_support": "10",
                    "hr_onboarding": "9",
                },
            ),
            (
                "m1w3_survey",
                {"mood": "a", "helpers": ["manager", "peers", "recruiter"], "accelerate": "automation"},
            ),
            ("chat", "Как оформить ежегодный отпуск?"),
            ("chat", "Какая компенсация за использование личного автомобиля?"),
            (
                "final",
                {
                    "hardest": "b",
                    "stay": ("b", "Предложили более высокую зарплату в другой компании, пока думаю"),
                    "load": ("7", "Много срочных задач от смежных отделов"),
                    "expectations": "8",
                    "info_enough": "yes",
                    "manager_available": "9",
                    "manager_feedback": "8",
                    "peers_support": "9",
                    "departments": ("5", "Согласование договоров с юристами занимает по две недели"),
                    "hr_support": "9",
                    "goals_done": "8",
                    "improve": "Сделать единый чат с ответами по процессам согласования договоров",
                },
            ),
        ],
    ),
]


async def load_kb(dirs: list[Path]) -> None:
    for d in dirs:
        for f in sorted(d.iterdir()):
            if f.suffix.lower() not in KB_EXT or "ТЗ" in f.name or "КЕЙС" in f.name:
                continue
            async with session() as s:
                if await s.scalar(
                    select(Document.id).where(Document.filename == f.name, Document.status == "ready")
                ):
                    print(f"  уже в базе: {f.name}")
                    continue
                doc = await ingest(s, f.name, f.read_bytes(), f.stem, [], settings().chunk_max_chars)
                print(f"  {doc.status}: {f.name} → {doc.n_chunks} чанков {doc.error or ''}")


async def max_ids(s) -> dict[str, int]:
    return {t: (await s.scalar(text(f"SELECT coalesce(max(id), 0) FROM {t}"))) for t in TABLES}


async def backdate(s, before: dict[str, int], at: datetime) -> None:
    for t in TABLES:
        await s.execute(text(f"UPDATE {t} SET created_at = :at WHERE id > :id"), {"at": at, "id": before[t]})
    # Срок первого ответа HR отсчитывается от сдвинутого времени обращения, как будто оно пришло тогда.
    for t in (await s.scalars(select(Ticket).where(Ticket.id > before["tickets"]))).all():
        due = response_time.deadline(at, t.severity, ZoneInfo(t.employee.site.tz))
        await s.execute(update(TicketDeadline).where(TicketDeadline.ticket_id == t.id).values(due_at=due))
        await s.execute(
            update(Job)
            .where(Job.kind == response_time.KIND, Job.ref == str(t.id), Job.status == "pending")
            .values(run_at=func.least(Job.run_at, due))
        )
    await s.commit()


async def event(emp_id: int, ev: InEvent) -> None:
    async with session() as s:
        emp = await s.get(Employee, emp_id)
        await dialog.handle(s, emp, ev)


async def answer_survey(emp_id: int, answers: dict) -> None:
    """Отвечает на опрос так, как это сделал бы сотрудник кнопками и текстом."""
    for _ in range(40):
        async with session() as s:
            emp = await s.get(Employee, emp_id)
            st = emp.state.get("survey")
            aw = (emp.state.get("await") or {}).get("kind")
        if not st:
            return
        from app.bot.surveys import questions

        q = questions(emp, st["id"])[st["q"]]
        val = answers.get(q["id"])
        if aw == "survey_comment":
            comment = val[1] if isinstance(val, tuple) else None
            await event(
                emp_id,
                InEvent("text", text=comment) if comment else InEvent("callback", payload="a:skip_comment"),
            )
            continue
        if q["type"] == "text":
            await event(
                emp_id,
                InEvent("text", text=val)
                if val
                else InEvent("callback", payload=f"s:{st['id']}:{q['id']}:skip"),
            )
        elif q["type"] == "multi":
            for opt in val or []:
                await event(emp_id, InEvent("callback", payload=f"m:{st['id']}:{q['id']}:{opt}"))
            await event(emp_id, InEvent("callback", payload=f"s:{st['id']}:{q['id']}:done"))
        else:
            raw = val[0] if isinstance(val, tuple) else (val or q.get("options", [{}])[0].get("id", "yes"))
            await event(emp_id, InEvent("callback", payload=f"s:{st['id']}:{q['id']}:{raw}"))
            # если вариант просит комментарий — следующий виток цикла отправит его


async def main(dirs: list[Path]) -> None:
    await init_db()
    await seed_sites()
    print("База знаний:")
    await load_kb(dirs)

    async with session() as s:
        if await s.scalar(select(Employee.id).limit(1)):
            print("Сотрудники уже есть — пропускаю демо-сотрудников")
            return
        staff = {}
        for name, role, site in STAFF:
            x = Staff(name=name, role=role, site_code=site, invite_code=secrets.token_urlsafe(9))
            s.add(x)
            staff[role] = x
        await s.commit()
        ids = []
        for full, pos, site, days, form, _ in PEOPLE:
            start = next_workday(date.today() - timedelta(days=days))  # выходят на работу в рабочий день
            e = Employee(
                full_name=full,
                first_name=full.split()[1],
                position=pos,
                site_code=site,
                start_date=start,
                probation_end=add_months(start, settings().probation_months),
                manager_id=staff["manager"].id,
                recruiter_id=staff["recruiter"].id,
                address_form=form,
                channel="web",
                status="invited" if days < 0 else "active",
                invite_code=secrets.token_urlsafe(9),
                state={},
            )
            s.add(e)
            await s.commit()
            ids.append(e.id)
            e = await s.get(Employee, e.id)
            await schedule_employee(s, e)

    for emp_id, (full, *_rest, script) in zip(ids, PEOPLE, strict=True):
        print(f"\n{full}:")
        for kind, payload in script:
            async with session() as s:
                emp = await s.get(Employee, emp_id)
                before = await max_ids(s)
                st = step(kind, emp.site_code) if kind != "chat" else None
                at = step_run_at(st["at"], emp) if st else None
            if kind == "chat":
                await event(emp_id, InEvent("text", text=payload))
                await asyncio.sleep(0)
                print(f"  вопрос: {payload}")
            else:
                async with session() as s:
                    emp = await s.get(Employee, emp_id)
                    from app.bot.scenario import run_step

                    await run_step(s, emp, kind)
                await answer_survey(emp_id, payload)
                print(f"  шаг {kind}: отвечен")
            # даём фоновому NLP-анализу сообщений завершиться до сдвига дат
            await asyncio.sleep(1)
            while len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()]) > 0:
                await asyncio.sleep(1)
            if at is None:
                async with session() as s:
                    emp = await s.get(Employee, emp_id)
                    at = datetime.now(ZoneInfo("UTC")) - timedelta(
                        days=max(0, (date.today() - emp.start_date).days - 1)
                    )
            async with session() as s:
                await backdate(s, before, at)
    print("\nГотово.")


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]] or [ROOT.parent / "materials", ROOT / "kb"]
    asyncio.run(main(args))
