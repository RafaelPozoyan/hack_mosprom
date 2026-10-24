from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.db import Base


class Site(Base):
    """Площадка — тег сотрудника из ТЗ (GrPP, GTP, Luzhniki): от неё зависят тексты, файлы и часовой пояс."""

    __tablename__ = "sites"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="office")  # office | plant
    tz: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")


class Staff(Base):
    """Сотрудники HR и руководители — получатели эскалаций, дайджестов и push-уведомлений."""

    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # hr | recruiter | manager | head
    max_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    max_link: Mapped[str | None] = mapped_column(String(500))  # ссылка «перейти в диалог» для кнопки
    site_code: Mapped[str | None] = mapped_column(ForeignKey("sites.code"))
    invite_code: Mapped[str] = mapped_column(String(64), unique=True)  # диплинк staff_<code> → подписка на push


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    first_name: Mapped[str] = mapped_column(String(100))
    position: Mapped[str] = mapped_column(String(200), default="")
    department: Mapped[str] = mapped_column(String(200), default="")
    site_code: Mapped[str] = mapped_column(ForeignKey("sites.code"))
    start_date: Mapped[date] = mapped_column(Date)
    probation_end: Mapped[date] = mapped_column(Date)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    recruiter_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    address_form: Mapped[str | None] = mapped_column(String(2))  # ty | vy — выбирает сам сотрудник
    channel: Mapped[str] = mapped_column(String(10), default="web")  # web (демо) | max
    max_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)  # один аккаунт MAX — один сотрудник
    invite_code: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="invited")  # invited | active | finished
    state: Mapped[dict] = mapped_column(JSONB, default=dict)  # текущий опрос/ожидание ввода
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    site: Mapped[Site] = relationship(lazy="joined")
    manager: Mapped[Staff | None] = relationship(foreign_keys=[manager_id], lazy="joined")
    recruiter: Mapped[Staff | None] = relationship(foreign_keys=[recruiter_id], lazy="joined")


class Job(Base):
    """Очередь отложенных действий (шаги таймлайна, напоминания, дайджест). Владение задачей защищает блокировка PostgreSQL."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20), default="step")  # step | reminder | digest | handoff
    ref: Mapped[str] = mapped_column(String(100))  # id шага / опроса
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | running | done | cancelled | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_due", "status", "run_at"),)


class StepAttachment(Base):
    """Файл ручного запуска сохраняется до исполнения и доступен при повторе задачи."""

    __tablename__ = "step_attachments"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    file: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(Text)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    author: Mapped[str] = mapped_column(String(20))  # employee | bot | hr
    text: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)  # кнопки, вложения, источники
    step_id: Mapped[str | None] = mapped_column(String(100))
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id", ondelete="SET NULL"))
    max_mid: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SurveyAnswer(Base):
    __tablename__ = "survey_answers"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    survey_id: Mapped[str] = mapped_column(String(50))
    question_id: Mapped[str] = mapped_column(String(50))
    value: Mapped[dict] = mapped_column(JSONB)  # {"option": "b"} | {"score": 7} | {"options": [...]} | {"text": ...}
    comment: Mapped[str | None] = mapped_column(Text)
    flagged: Mapped[bool] = mapped_column(default=False)  # ответ из «тревожных» вариантов ТЗ
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Analysis(Base):
    """Результат NLP-анализа свободного текста сотрудника (комментарий в опросе или сообщение в чате)."""

    __tablename__ = "analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20))  # survey | chat
    text: Mapped[str] = mapped_column(Text)
    sentiment: Mapped[str] = mapped_column(String(10))
    emotions: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    topics: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    risk_flags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    severity: Mapped[str] = mapped_column(String(10))
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Ticket(Base):
    """Эскалация на живого HR: вопрос без ответа в базе, проблема из опроса или красный флаг."""

    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # question | problem | alert
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closed
    subject: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")  # handoff-карточка для HR
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    hr_chat_mid: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    employee: Mapped[Employee] = relationship(lazy="joined")


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    site_codes: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)  # пусто — для всех площадок
    status: Mapped[str] = mapped_column(String(12), default="processing")  # processing | ready | error
    error: Mapped[str | None] = mapped_column(Text)
    n_chunks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(settings().embed_dim))
    tsv = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', coalesce(heading, '') || ' ' || text)", persisted=True)
    )

    document: Mapped[Document] = relationship(lazy="joined")

    __table_args__ = (
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Question(Base):
    """Каждый свободный вопрос к ИИ: для аналитики тем и поиска пробелов в базе знаний."""

    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    answered: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(String(30), default="")  # ok | low_score | not_in_context | bad_citation
    best_score: Mapped[float | None] = mapped_column(Float)
    topic: Mapped[str | None] = mapped_column(String(100))
    answer: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    feedback: Mapped[int | None] = mapped_column(Integer)  # 1 | -1
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Digest(Base):
    __tablename__ = "digests"
    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    stats: Mapped[dict] = mapped_column(JSONB)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MaxUpdate(Base):
    """Принятые события вебхука: ключ остаётся после обработки, исходное тело очищается."""

    __tablename__ = "max_updates"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    actor: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (
        Index("ix_max_updates_due", "status", "available_at"),
        Index("ix_max_updates_actor", "actor", "status", "id"),
    )
