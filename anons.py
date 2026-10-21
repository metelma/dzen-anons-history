"""Робот анонсов: новая статья на Дзене -> анонс в Телеграм-канал.

Как работает:
1. Невидимым браузером открывает страницу канала на Дзене и собирает ссылки на статьи.
2. Сравнивает со списком уже известных статей (seen.json рядом со скриптом).
3. Для каждой новой статьи берёт заголовок, описание и обложку и отправляет анонс в канал.
4. Самый первый запуск только запоминает статьи, которые уже есть, и ничего не отправляет.

Настройки через переменные окружения:
  TG_BOT_TOKEN  - токен бота от @BotFather (обязателен для отправки)
  TG_CHAT_ID    - канал, по умолчанию @mm_history
  DZEN_CHANNEL  - адрес канала на Дзене, по умолчанию https://dzen.ru/history_mm
  DRY_RUN=1     - ничего не отправлять, только показать, что ушло бы
"""

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
CHANNEL_URL = os.environ.get("DZEN_CHANNEL", "https://dzen.ru/history_mm")
CHAT_ID = os.environ.get("TG_CHAT_ID", "@mm_history")
TOKEN = os.environ.get("TG_BOT_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def log(msg):
    print(msg, flush=True)


def article_id(url):
    return url.split("?")[0].rstrip("/").split("/")[-1]


def collect_articles(page):
    """Ссылки на статьи со страницы канала, сверху вниз (новые первыми)."""
    page.goto(CHANNEL_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector('a[href*="/a/"]', timeout=30000)
    links = page.eval_on_selector_all(
        'a[href*="/a/"]', "els => els.map(a => a.href.split('?')[0])"
    )
    result = []
    for link in links:
        if link not in result:
            result.append(link)
    return result


def article_info(page, url):
    """Заголовок, описание и обложка статьи из её страницы."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def meta(prop):
        el = page.query_selector(f'meta[property="{prop}"]') or page.query_selector(
            f'meta[name="{prop}"]'
        )
        return (el.get_attribute("content") or "").strip() if el else ""

    title = meta("og:title") or page.title().split(" | ")[0]
    description = meta("og:description") or meta("description")
    image = meta("og:image")
    return {"url": url, "title": title.strip(), "description": description, "image": image}


def short_text(text, limit=400):
    """Обрезает описание по концу предложения, чтобы влезло в подпись."""
    text = " ".join(text.split())
    # Дзен ставит в описание приставку «Статья автора «Канал» в Дзене ✍: »
    text = re.sub(r"^Статья автора .*? в Дзене\s*✍?\s*:\s*", "", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > 100 else cut.rsplit(" ", 1)[0] + "…"


def make_caption(info):
    parts = [f"<b>Новая статья на Дзене: «{html.escape(info['title'])}»</b>"]
    if info["description"]:
        parts.append(html.escape(short_text(info["description"])))
    parts.append(f'📖 Читайте: <a href="{html.escape(info["url"])}">{html.escape(info["url"])}</a>')
    return "\n\n".join(parts)


def tg(method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=60) as resp:
        answer = json.load(resp)
    if not answer.get("ok"):
        raise RuntimeError(f"Телеграм ответил ошибкой: {answer}")
    return answer


def send(info):
    caption = make_caption(info)
    if DRY_RUN:
        log("---- пробный анонс (не отправлен) ----")
        log(f"обложка: {info['image']}")
        log(caption)
        return
    if info["image"]:
        try:
            tg("sendPhoto", {"chat_id": CHAT_ID, "photo": info["image"],
                             "caption": caption, "parse_mode": "HTML"})
            return
        except Exception as e:  # обложка не прошла - шлём без неё
            log(f"обложка не отправилась ({e}), отправляю текстом")
    tg("sendMessage", {"chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML"})


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return None


def save_seen(ids):
    SEEN_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    if not TOKEN and not DRY_RUN:
        sys.exit("Нет TG_BOT_TOKEN. Для пробы без отправки запусти с DRY_RUN=1")

    seen = load_seen()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=UA, locale="ru-RU").new_page()
        links = collect_articles(page)
        log(f"на канале найдено статей: {len(links)}")
        if not links:
            sys.exit("Статьи не найдены - Дзен мог не пустить робота")

        if seen is None:
            save_seen({article_id(u) for u in links})
            log("первый запуск: запомнила статьи, которые уже есть, анонсы не отправляла")
            browser.close()
            return

        seen = set(seen)
        new = [u for u in links if article_id(u) not in seen]
        log(f"новых статей: {len(new)}")
        for url in reversed(new):  # старые из новых идут первыми
            info = article_info(page, url)
            send(info)
            seen.add(article_id(url))
            save_seen(seen)  # сохраняем после каждой, чтобы не задвоить при сбое
            log(f"анонс: {info['title']}")
        browser.close()


if __name__ == "__main__":
    main()
