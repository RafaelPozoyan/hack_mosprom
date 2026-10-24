"""Запись видео-демо: настоящий прогон панели и чата в браузере (Playwright), без постановочных кадров.

Сценарий повторяет «Как посмотреть за 5 минут» из README. Ожидание ответов ИИ остаётся в записи —
при монтаже его можно сократить, пометив это в кадре.

Запуск (из контейнера Playwright, сеть host):
  python record_demo.py http://localhost:5173 <id сотрудника> <папка для видео>
"""

import sys

from playwright.sync_api import Page, sync_playwright

BASE, EMP_ID, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
WAIT_AI_MS = 180_000


BOT = '[data-author="bot"]'  # пузыри сообщений бота; индикатор «печатает…» сюда не входит


def bot_messages(page: Page) -> int:
    return page.locator(BOT).count()


def wait_bot(page: Page, before: int, n: int = 1) -> None:
    page.wait_for_function(
        "([s, b, n]) => document.querySelectorAll(s).length >= b + n",
        arg=[BOT, before, n],
        timeout=WAIT_AI_MS,
    )
    page.wait_for_timeout(1500)


def ask(page: Page, text: str) -> None:
    before = bot_messages(page)
    page.get_by_placeholder("Сообщение").fill(text)
    page.wait_for_timeout(600)
    page.keyboard.press("Enter")
    wait_bot(page, before)
    page.wait_for_timeout(2500)


def press(page: Page, label: str, expect: int = 1) -> None:
    before = bot_messages(page)
    page.get_by_role("button", name=label).last.click()
    wait_bot(page, before, expect)
    page.wait_for_timeout(2000)


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 1600, "height": 960},
        record_video_dir=OUT,
        record_video_size={"width": 1600, "height": 960},
    )
    page = ctx.new_page()
    page.add_init_script(
        "localStorage.setItem('korsik_role', JSON.stringify('hr')); localStorage.setItem('korsik_demo', 'true')"
    )

    page.goto(f"{BASE}/", wait_until="networkidle")
    page.wait_for_timeout(3500)
    page.goto(f"{BASE}/employees/{EMP_ID}", wait_until="networkidle")
    page.wait_for_timeout(2500)

    press(page, "Новичок сканирует QR", expect=2)
    press(page, "На ВЫ", expect=4)
    ask(page, "Сколько дней ежегодного отпуска положено?")
    ask(page, "Есть ли парковка у офиса?")

    # перемотка таймлайна до опроса третьего дня: шаги без ИИ, поэтому ждём коротко
    for _ in range(15):
        page.get_by_role("button", name="Перемотать к следующему шагу").click()
        page.wait_for_timeout(2500)
        if page.get_by_role("button", name="Все отлично!").count():
            break
    for label in ("Все отлично!", "Пока присматриваюсь", "Пока ничего не понял"):
        page.get_by_role("button", name=label).last.click()
        page.wait_for_timeout(3000)
    page.get_by_role("button", name="Сигналы").click()
    page.wait_for_timeout(3500)

    page.goto(f"{BASE}/tickets", wait_until="networkidle")
    page.wait_for_timeout(4000)
    page.goto(f"{BASE}/", wait_until="networkidle")
    page.wait_for_timeout(4000)
    ctx.close()
    browser.close()
