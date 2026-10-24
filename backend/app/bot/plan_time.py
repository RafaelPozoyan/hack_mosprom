"""Плановое время сообщений сценария для демо-чата: «3-й рабочий день, 15:00 · ср 11.11».

Дата и время берутся из `timeline` — того же расчёта, по которому работает очередь рассылок и строится таймлайн
в карточке сотрудника, поэтому подпись не расходится с планом. Здесь только слова о расписании шага (`at`).
Подпись добавляет демо-API отдельным полем сообщения для панели: в MAX она не уходит и в текст не попадает.
"""

from collections import defaultdict
from datetime import datetime

from app.bot import followup
from app.bot.scenario import button_rows, scenario, site_tz, step, timeline
from app.models import Employee, Job

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# Сдвиг от даты выхода: месяцы, недели, дни; формы слова — для 1, 2–4 и 5+.
OFFSETS = (
    ("months", ("месяц", "месяца", "месяцев")),
    ("weeks", ("неделя", "недели", "недель")),
    ("days", ("день", "дня", "дней")),
)
JOB_NAMES = {
    "reminder": "Напоминание об опросе",
    followup.REMINDER_KIND: "Напоминание об обязательном шаге",
    followup.ESCALATION_KIND: "Сигнал HR: обязательный шаг не пройден",
}
MENU = "@menu"  # источник шагов, которые открывают кнопки меню и команды; не совпадает ни с одним id шага


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 день», «2 дня», «5 дней», «21 день»: слово в форме, которую требует число."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def schedule_words(at: dict) -> str | None:
    """Расписание шага словами относительно выхода. Порядок проверок тот же, что в `step_date`."""
    if at.get("event"):
        return "сразу после запуска по QR"
    if "day" in at:
        n = at["day"]
        if n < 0:
            return f"за {plural(-n, 'день', 'дня', 'дней')} до выхода"
        if n == 0:
            return "в день выхода"
        return f"{plural(n, 'день', 'дня', 'дней')} после выхода"
    if "workday" in at:
        return f"{at['workday']}-й рабочий день"
    if "month_start" in at:
        return f"начало {at['month_start']}-го месяца работы"
    if "probation_end" in at:
        return "конец испытательного срока"
    parts = [plural(at[key], *forms) for key, forms in OFFSETS if at.get(key)]
    if not parts:
        return None
    head = ", ".join(parts[:-1])
    return f"{head + ' и ' if head else ''}{parts[-1]} после выхода"


def moment(emp: Employee, when: datetime) -> str:
    """«15:00 · ср 11.11» по часовому поясу площадки: время и дата новичка, а не сервера."""
    local = when.astimezone(site_tz(emp.site_code))
    return f"{local:%H:%M} · {WEEKDAYS[local.weekday()]} {local:%d.%m}"


def caption(emp: Employee, at: dict, run_at: datetime | None) -> str | None:
    """Подпись шага: слова о расписании и, если дата известна, время и дата площадки."""
    words = schedule_words(at)
    if words is None or run_at is None:
        return words  # у шага «после запуска по QR» плановой даты нет: она зависит от новичка
    return f"{words}, {moment(emp, run_at)}"


def followers(st: dict) -> list[str]:
    """Шаги, которые сценарий запускает сразу следом: `then` шага и `then` его опроса."""
    out = list(st.get("then", []))
    for message in st.get("messages", []):
        then = scenario()["surveys"].get(message.get("survey"), {}).get("then")
        if then:
            out.append(then)
    return out


def triggers() -> dict[str, set[str]]:
    """Кто может запустить шаг: id шага (then, then опроса, кнопка) или MENU (кнопки меню и команды)."""
    cfg = scenario()
    out: defaultdict[str, set[str]] = defaultdict(set)
    for st in cfg["steps"]:
        for target in followers(st):
            out[target].add(st["id"])
        for message in st.get("messages", []):
            for row in button_rows(message.get("buttons")):
                for button in row:
                    if button.get("goto"):
                        out[button["goto"]].add(st["id"])
    for row in button_rows(cfg.get("menu", {}).get("buttons")):
        for button in row:
            if button.get("goto"):
                out[button["goto"]].add(MENU)
    for target in cfg.get("commands", {}).values():
        out[target].add(MENU)
    return out


def anchor(step_id: str, own: dict[str, str], sources: dict[str, set[str]]) -> str | None:
    """Шаг со своим временем, от которого однозначно идёт цепочка к этому шагу; None — если запуск не один."""
    seen: set[str] = set()
    while step_id not in own:
        found = sources.get(step_id, set())
        if len(found) != 1 or step_id in seen:
            return None
        seen.add(step_id)
        (step_id,) = found
    return step_id


def captions(emp: Employee) -> dict[str, str]:
    """Подпись по `Message.step_id`: у шага со временем — своя, у шагов, которые идут только от него, — та же.

    Так подписаны шаги следом (`then`), ответы на кнопки шага и знакомство после «На ТЫ / На ВЫ». Шаги, которые
    открываются из нескольких мест («Где пообедать» — из шага и из меню), и сообщения без шага (ответы ИИ и HR)
    подписи не получают: когда их увидит новичок, от плана не зависит.
    """
    own: dict[str, str] = {}
    for st, run_at in timeline(emp):
        text = caption(emp, st["at"], run_at)
        if text is not None:
            own[st["id"]] = text
    sources = triggers()
    out: dict[str, str] = {}
    for st in scenario()["steps"]:
        root = anchor(st["id"], own, sources)
        if root is None:
            continue
        out[st["id"]] = own[root]
        for message in st.get("messages", []):
            if message.get("survey"):
                # вопросы опроса сохраняются с step_id «survey:<id>», а не с id шага
                out[f"survey:{message['survey']}"] = own[root]
    return out


def upcoming(emp: Employee, job: Job) -> dict[str, str | None]:
    """Название и плановое время задачи: что она сделает и когда это было бы по плану.

    У напоминаний и сигнала HR места в плане нет: срок отсчитывается от момента, когда шаг реально ушёл, — у них
    только название.
    """
    if job.kind != "step":
        return {"title": JOB_NAMES.get(job.kind, "Служебное сообщение Корсика"), "plan": None}
    found = step(job.ref, emp.site_code)
    return {
        "title": found.get("title", job.ref) if found else job.ref,
        "plan": captions(emp).get(job.ref) or moment(emp, job.run_at),
    }


def start_hint(emp: Employee) -> dict[str, str | None] | None:
    """Пока новичок не запустил бота, следующий шаг — знакомство «после запуска по QR»: перемотка ничего не пошлёт."""
    for st, _ in timeline(emp):
        if st["at"].get("event"):
            return {"title": st.get("title", st["id"]), "plan": caption(emp, st["at"], None)}
    return None
