"""Сценарий из YAML: рендер текстов, расчёт времени шагов и их выполнение."""

import json
import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays
import yaml
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app import channels
from app.channels import Button, OutMessage
from app.config import settings
from app.models import Employee, Job, Message

VARIANT = re.compile(r"\{([^{}|]*)\|([^{}|]*)\}")


def overrides_path():
    return settings().data_dir / "scenario_overrides.json"


def step_key(step_id: str, sites: list[str] | None) -> str:
    return f"{step_id}|{','.join(sorted(sites or []))}"


@lru_cache
def scenario() -> dict:
    """Сценарий ТЗ из YAML + правки текстов, сделанные HR в панели (хранятся отдельно, исходник не меняется)."""
    data = yaml.safe_load(settings().scenario_path.read_text(encoding="utf-8"))
    path = overrides_path()
    edits = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for st in data["steps"]:
        for idx, text in edits.get(step_key(st["id"], st.get("sites")), {}).items():
            msgs = st.get("messages", [])
            if int(idx) < len(msgs) and "text" in msgs[int(idx)]:
                msgs[int(idx)]["text"] = text
    return data


def reload_scenario() -> None:
    scenario.cache_clear()


def save_step_texts(step_id: str, sites: list[str], texts: dict[int, str]) -> None:
    path = overrides_path()
    edits = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    edits.setdefault(step_key(step_id, sites), {}).update({str(i): t for i, t in texts.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(edits, ensure_ascii=False, indent=1), encoding="utf-8")
    reload_scenario()


# --- тексты ------------------------------------------------------------------------------------------


def render(text: str, emp: Employee) -> str:
    vy = emp.address_form == "vy"
    text = VARIANT.sub(lambda m: m.group(2) if vy else m.group(1), text)
    return (
        text.replace("{name}", emp.first_name)
        .replace("{site}", emp.site_code)
        .replace("{employee}", emp.full_name)
        .replace("{position}", emp.position or "должность не указана")
        .replace("{start_date}", emp.start_date.strftime("%d.%m.%Y"))
        .strip()
    )


def step(step_id: str, site: str) -> dict | None:
    """Шаг с учётом площадки: сначала вариант для площадки, иначе общий."""
    general = None
    for st in scenario()["steps"]:
        if st["id"] != step_id:
            continue
        if site in st.get("sites", []):
            return st
        if not st.get("sites"):
            general = st
    return general


def button_rows(raw: list) -> list[list[dict]]:
    return [row if isinstance(row, list) else [row] for row in raw or []]


def build_message(m: dict, emp: Employee, step_id: str, idx: int) -> OutMessage:
    rows = []
    for r, row in enumerate(button_rows(m.get("buttons"))):
        rows.append(
            [
                Button(render(b["text"], emp), url=b.get("url"))
                if b.get("url")
                else Button(render(b["text"], emp), payload=f"b:{step_id}:{idx}:{r}:{c}")
                for c, b in enumerate(row)
            ]
        )
    file = m.get("file", "").replace("{site}", emp.site_code.lower()) or None
    if file and not (settings().media_dir / file).exists():
        file = None
    return OutMessage(text=render(m.get("text", ""), emp), buttons=rows, image=m.get("image"), file=file)


def find_button(payload: str, emp: Employee) -> dict | None:
    _, step_id, idx, r, c = payload.split(":")
    if step_id == "menu":
        return button_rows(scenario()["menu"]["buttons"])[int(r)][int(c)]
    st = step(step_id, emp.site_code)
    if not st:
        return None
    return button_rows(st["messages"][int(idx)].get("buttons"))[int(r)][int(c)]


# --- календарь ---------------------------------------------------------------------------------------


@lru_cache
def ru_holidays(year: int) -> holidays.HolidayBase:
    return holidays.country_holidays("RU", years=year)


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in ru_holidays(d.year)


def next_workday(d: date) -> date:
    while not is_workday(d):
        d += timedelta(days=1)
    return d


def prev_workday(d: date) -> date:
    while not is_workday(d):
        d -= timedelta(days=1)
    return d


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


def step_date(at: dict, emp: Employee) -> date | None:
    start = emp.start_date
    if "day" in at:
        return start + timedelta(days=at["day"])
    if "workday" in at:
        d = next_workday(start)
        for _ in range(at["workday"] - 1):
            d = next_workday(d + timedelta(days=1))
        return d
    if "month_start" in at:
        return next_workday(add_months(start, at["month_start"] - 1))
    if "probation_end" in at:
        return prev_workday(emp.probation_end)
    if any(k in at for k in ("days", "weeks", "months")):
        d = add_months(start, at.get("months", 0)) + timedelta(days=at.get("days", 0), weeks=at.get("weeks", 0))
        return next_workday(d)
    return None


def site_tz(site_code: str) -> ZoneInfo:
    """Часовой пояс площадки — из сценария (у филиалов в разных регионах он свой)."""
    return ZoneInfo(scenario()["sites"].get(site_code, {}).get("tz", settings().default_tz))


def step_run_at(at: dict, emp: Employee) -> datetime | None:
    d = step_date(at, emp)
    if d is None:
        return None
    hh, mm = map(int, at.get("time", settings().default_send_time).split(":"))
    return datetime.combine(d, time(hh, mm), tzinfo=site_tz(emp.site_code))


# --- планирование ------------------------------------------------------------------------------------


PHASES = ["Знакомство", "До выхода", "Первый день", "Первые дни", "Первый месяц", "Второй месяц", "Третий месяц и итоги"]


def phase_of(at: dict | None) -> str:
    """Этап пути новичка по описанию шага (а не по дате: выход в воскресенье не должен сдвигать «первый день»)."""
    if not at or at.get("event"):
        return PHASES[0]
    if "day" in at:
        return PHASES[1]
    if at.get("workday") == 1:
        return PHASES[2]
    if "workday" in at or at.get("days", 99) <= 10:
        return PHASES[3]
    if "weeks" in at and not at.get("months"):
        return PHASES[4]
    if at.get("month_start") == 2 or at.get("months") == 1:
        return PHASES[5]
    return PHASES[6]


def timeline(emp: Employee) -> list[tuple[dict, datetime | None]]:
    """Все шаги таймлайна сотрудника с учётом площадки (для планировщика и для карточки в админке)."""
    seen: set[str] = set()
    out = []
    for st in scenario()["steps"]:
        at = st.get("at")
        if not at or st["id"] in seen:
            continue
        chosen = step(st["id"], emp.site_code)
        if chosen is None or chosen.get("at") is None:
            continue
        seen.add(st["id"])
        out.append((chosen, None if at.get("event") else step_run_at(chosen["at"], emp)))
    return out


async def schedule_employee(s: AsyncSession, emp: Employee) -> int:
    """(Пере)планирует шаги сотрудника. Ручные шаги («по отмашке рекрутера») в очередь не попадают."""
    await s.execute(
        update(Job)
        .where(Job.employee_id == emp.id, Job.status == "pending", Job.kind == "step")
        .values(status="cancelled")
    )
    now = datetime.now(ZoneInfo("UTC"))
    n = 0
    for st, run_at in timeline(emp):
        if run_at is None or st.get("manual") or run_at < now:
            continue
        s.add(Job(employee_id=emp.id, kind="step", ref=st["id"], run_at=run_at))
        n += 1
    await s.commit()
    return n


# --- выполнение --------------------------------------------------------------------------------------


async def run_step(s: AsyncSession, emp: Employee, step_id: str) -> None:
    from app.bot import surveys
    from app.bot.dialog import answer_question

    st = step(step_id, emp.site_code)
    if st is None:
        return
    audience = st.get("audience", "employee")
    if audience == "employee" and emp.status == "invited":
        return  # бот MAX не может написать первым: пока новичок не открыл бота по QR, шаги для него пропускаем
    pressed = emp.state.get("pressed", [])
    if any(k.startswith(f"{step_id}:") for k in pressed):
        # шаг отправлен заново (HR запустил вручную) — его кнопки снова активны
        emp.state = {**emp.state, "pressed": [k for k in pressed if not k.startswith(f"{step_id}:")]}
        await s.commit()
    for idx, m in enumerate(st.get("messages", [])):
        if audience != "employee":
            await notify_audience(s, emp, audience, render(m["text"], emp), step_id)
            continue
        if m.get("ai"):
            await answer_question(s, emp, m["ai"], log_question=False)
            continue
        out = build_message(m, emp, step_id, idx)
        if out.text or out.image or out.file:
            await channels.send(s, emp, out, step_id=step_id)
        if m.get("await"):
            emp.state = {**emp.state, "await": {"kind": m["await"]}}
            await s.commit()
        if m.get("survey"):
            await surveys.start(s, emp, m["survey"])
    for nxt in st.get("then", []):
        await run_step(s, emp, nxt)


async def notify_audience(s: AsyncSession, emp: Employee, audience: str, text: str, step_id: str | None) -> None:
    """Push руководителю/рекрутеру: в MAX, если он подключён, и всегда — в журнал карточки сотрудника."""
    staff = emp.manager if audience == "manager" else emp.recruiter
    delivered = await channels.notify_staff(staff, OutMessage(text=f"🐾 Корсик · {emp.full_name}\n\n{text}"))
    s.add(
        Message(
            employee_id=emp.id,
            direction="out",
            author=f"to_{audience}",
            text=text,
            step_id=step_id,
            payload={"delivered": delivered, "staff": staff.name if staff else None},
        )
    )
    await s.commit()
