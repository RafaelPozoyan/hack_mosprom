"""Жизненный цикл обращений HR: ошибки API и повторные действия не меняют закрытый диалог и не плодят новые."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from app.api import admin
from app.bot import dialog, escalation, surveys, updates
from app.db import get_session
from app.models import Question


class FakeSession:
    def __init__(self, ticket):
        self.info = {}
        self.ticket = ticket
        self.commits = 0

    async def get(self, model, ident):
        return self.ticket

    async def scalar(self, stmt):
        return self.ticket

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def refresh(self, instance):
        # В этой заглушке объекты общие; конкурентное перечитывание проверяется на PostgreSQL.
        assert instance is self.ticket or instance is self.ticket.employee


@pytest.fixture
def case(monkeypatch):
    emp = SimpleNamespace(
        id=1,
        first_name="Анна",
        full_name="Иванова Анна",
        position="Инженер",
        site_code="GTP",
        address_form="vy",
        start_date=datetime(2026, 10, 26),
        state={"survey": {"id": "day3"}},
    )
    ticket = SimpleNamespace(
        id=7,
        employee_id=1,
        employee=emp,
        kind="problem",
        subject="Вопрос сотрудника: где получить пропуск?",
        status="open",
        closed_at=None,
    )
    send, post, publish = AsyncMock(), AsyncMock(), Mock()
    monkeypatch.setattr(escalation.channels, "send", send)
    monkeypatch.setattr(escalation.channels, "post_hr_chat", post)
    monkeypatch.setattr(escalation.hub, "publish", publish)
    return SimpleNamespace(
        ticket=ticket, emp=emp, session=FakeSession(ticket), send=send, post=post, publish=publish
    )


def api_request(s, path, body=None):
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.auth] = lambda: None

    async def session_override():
        yield s

    app.dependency_overrides[get_session] = session_override

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            return await client.post(path, json=body or {})

    return asyncio.run(request())


@pytest.mark.parametrize("action", ["reply", "close"])
def test_missing_ticket_returns_404(action):
    response = api_request(FakeSession(None), f"/api/tickets/999/{action}", {"text": "Ответ"})
    assert response.status_code == 404


def test_closed_ticket_reply_returns_conflict_without_side_effects(case):
    case.ticket.status = "closed"
    case.ticket.closed_at = datetime(2026, 10, 24, tzinfo=UTC)
    state = dict(case.emp.state)
    response = api_request(case.session, "/api/tickets/7/reply", {"text": "Поздний ответ"})
    assert response.status_code == 409
    assert "закрыто" in response.json()["detail"]
    assert case.emp.state == state
    assert case.ticket.closed_at == datetime(2026, 10, 24, tzinfo=UTC)
    case.send.assert_not_awaited()
    assert case.session.commits == 0


def test_reply_and_repeated_close_preserve_dialog_state(case):
    assert (
        api_request(case.session, "/api/tickets/7/reply", {"text": "Поможем с доступами"}).status_code == 200
    )
    assert case.emp.state["ticket_id"] == 7
    assert case.send.await_args.kwargs["ticket_id"] == 7
    assert api_request(case.session, "/api/tickets/7/close").status_code == 200
    assert case.ticket.status == "closed"
    assert case.emp.state == {"survey": {"id": "day3"}}
    closed_at = case.ticket.closed_at
    commits = case.session.commits
    assert api_request(case.session, "/api/tickets/7/close").status_code == 200
    assert case.ticket.closed_at == closed_at
    assert case.session.commits == commits
    assert case.send.await_count == 2  # ответ HR и одно уведомление о закрытии
    case.publish.assert_called_once_with(escalation.ADMIN_FEED, {"type": "ticket_closed", "id": 7})


def test_message_in_open_dialog_is_confirmed_with_way_back(case):
    case.emp.state["ticket_id"] = 7
    asyncio.run(
        escalation.forward_from_employee(case.session, case.emp, "А пропуск на машину нужен?", confirm=True)
    )
    case.post.assert_awaited_once()
    out = case.send.await_args.args[2]
    assert out.text.startswith("Переслал HR")
    assert "нажмите" in out.text
    assert out.buttons[0][0].payload == "a:close_dialog"
    assert case.send.await_args.kwargs["ticket_id"] == 7


def test_closing_another_ticket_preserves_current_dialog(case):
    case.emp.state["ticket_id"] = 8
    asyncio.run(escalation.close(case.session, case.ticket))
    assert case.emp.state == {"survey": {"id": "day3"}, "ticket_id": 8}


def test_closing_alert_does_not_notify_employee(case):
    case.ticket.kind = "alert"
    assert api_request(case.session, "/api/tickets/7/close").status_code == 200
    assert case.ticket.status == "closed"
    case.send.assert_not_awaited()


@pytest.mark.parametrize("closed", [False, True])
def test_max_reply_respects_ticket_status(case, monkeypatch, closed):
    case.ticket.status = "closed" if closed else "open"

    @asynccontextmanager
    async def session_override():
        yield case.session

    monkeypatch.setattr(updates, "session", session_override)
    msg = {"link": {"type": "reply", "message": {"mid": "card-7"}}, "sender": {"first_name": "Мария"}}
    asyncio.run(updates._on_hr_chat(msg, "Поможем с доступами"))
    if closed:
        case.send.assert_not_awaited()
        assert "ticket_id" not in case.emp.state
        case.post.assert_awaited_once()
        assert "#7" in case.post.await_args.args[1].text
        assert "закрыто" in case.post.await_args.args[1].text
    else:
        case.send.assert_awaited_once()
        assert case.send.await_args.args[2].text == "💬 Мария:\nПоможем с доступами"
        assert case.emp.state["ticket_id"] == 7
        case.post.assert_not_awaited()


@pytest.mark.parametrize(
    ("subject", "forwarded"),
    [("Вопрос сотрудника: где получить пропуск?", False), ("Вопрос сотрудника: где столовая?", True)],
)
def test_open_dialog_does_not_duplicate_ticket(case, monkeypatch, subject, forwarded):
    case.emp.state["ticket_id"] = 7
    create = AsyncMock()
    monkeypatch.setattr(escalation, "create_ticket", create)
    t = asyncio.run(escalation.open_dialog(case.session, case.emp, subject, kind="question"))
    assert t is case.ticket
    create.assert_not_awaited()  # повторное нажатие кнопки — то же обращение, без второго уведомления рекрутеру
    assert case.post.await_count == int(forwarded)  # новый вопрос — в тот же диалог HR
    case.send.assert_awaited_once()
    assert case.send.await_args.kwargs["ticket_id"] == 7


class Store(FakeSession):
    """Обращения и вопросы по id: open_dialog и close работают по-настоящему."""

    def __init__(self, questions):
        super().__init__(None)
        self.tickets, self.questions = {}, {q.id: q for q in questions}

    async def get(self, model, ident):
        return (self.questions if model is Question else self.tickets).get(int(ident))


@pytest.fixture
def store(case, monkeypatch):
    s = Store(
        [
            SimpleNamespace(id=5, employee_id=1, text="Есть ли парковка у офиса?"),
            SimpleNamespace(id=6, employee_id=2, text="Вопрос другого новичка"),
        ]
    )

    async def create_ticket(_s, emp, kind, subject, severity="medium"):
        t = SimpleNamespace(
            id=len(s.tickets) + 1, employee=emp, kind=kind, subject=subject, status="open", closed_at=None
        )
        s.tickets[t.id] = t
        return t

    monkeypatch.setattr(escalation, "create_ticket", create_ticket)
    return s


def test_refusal_buttons_share_one_ticket_for_the_question(case, store):
    for act in ("recruiter:5", "relay_q:5", "recruiter:5"):  # обе кнопки под отказом, в любом порядке
        asyncio.run(dialog.action(store, case.emp, act))
    assert [t.subject for t in store.tickets.values()] == ["Вопрос сотрудника: Есть ли парковка у офиса?"]
    case.post.assert_not_awaited()  # повторы — «Уже передал», без пересылки HR


def test_survey_recruiter_after_closed_ticket_does_not_reuse_old_question(case, store):
    asyncio.run(dialog.action(store, case.emp, "recruiter:5"))
    asyncio.run(escalation.close(store, store.tickets[1]))
    asyncio.run(dialog.action(store, case.emp, "recruiter"))  # кнопка из опроса — без вопроса
    assert [t.subject for t in store.tickets.values()] == [
        "Вопрос сотрудника: Есть ли парковка у офиса?",
        "Сотрудник просит связаться с рекрутером",
    ]


def test_recruiter_button_ignores_foreign_question(case, store):
    asyncio.run(dialog.action(store, case.emp, "recruiter:6"))
    assert store.tickets[1].subject == "Сотрудник просит связаться с рекрутером"


def test_refusal_offer_binds_both_buttons_to_question():
    rows = surveys.offer_buttons(SimpleNamespace(recruiter=None), ["relay", "recruiter"], question_id=5)
    assert [b.payload for row in rows for b in row] == ["a:relay_q:5", "a:recruiter:5"]


def test_open_dialog_after_closed_ticket_creates_new(case, monkeypatch):
    case.emp.state["ticket_id"] = 7
    case.ticket.status = "closed"
    create = AsyncMock(return_value=SimpleNamespace(id=8))
    monkeypatch.setattr(escalation, "create_ticket", create)
    asyncio.run(
        escalation.open_dialog(case.session, case.emp, "Вопрос сотрудника: где столовая?", kind="question")
    )
    create.assert_awaited_once()
    assert case.emp.state["ticket_id"] == 8
