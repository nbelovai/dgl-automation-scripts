"""
Парсер новостей GSMArena и PhoneArena → Google Sheets.
Дата/время (МСК), аннотация (автоперевод), ссылка.
Сортировка по дате публикации. Защита от дубликатов через таблицу.

GSMArena  — RSS-фид (20 статей с датами и описаниями за 1 запрос).
PhoneArena — месячный sitemap (все статьи за месяц, без Cloudflare).

Автор: Claude | Версия: 5.0
"""

import re
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from deep_translator import GoogleTranslator

# ============================================================
# НАСТРОЙКИ
# ============================================================

SERVICE_ACCOUNT_FILE = "credentials.json"

SPREADSHEET_NAME = "Перевод"
SPREADSHEET_ID = ""

WORKSHEET_NAME = "Новости-2"

# Московское время = UTC+3
MSK = timezone(timedelta(hours=3))

# Столбцы: A=Статус[не трогаем], B=Дата, C=Время, D=Аннотация, E=Ссылка
COL_LINK = 5   # E

# Источники
GSMARENA_RSS = "https://www.gsmarena.com/rss-news-reviews.php3"
PHONEARENA_MONTH_SITEMAP = "https://www.phonearena.com/sitemaps/news/{year}/{month:02d}/index.xml"
PHONEARENA_GOOGLENEWS = "https://www.phonearena.com/sitemaps/googlenews.xml"

# ============================================================
# User-Agent
# ============================================================
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Переводчик EN → RU
translator = GoogleTranslator(source="en", target="ru")

# ============================================================
# Логирование
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================

def translate_text(text: str) -> str:
    if not text:
        return ""
    try:
        return translator.translate(text)
    except Exception as e:
        log.warning(f"  Ошибка перевода: {e}")
        return text


def title_from_url(url: str) -> str:
    """Извлекает читаемый заголовок из URL-слага PhoneArena."""
    match = re.search(r"/news/(.+?)(?:_id\d+)?$", url)
    if not match:
        return ""
    slug = match.group(1)
    title = slug.replace("-", " ").strip()
    return title[0].upper() + title[1:] if title else ""


def fetch_og_description(url: str) -> str:
    """Загружает og:description со страницы статьи GSMArena."""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        og = soup.find("meta", property="og:description")
        if og and og.get("content"):
            return og["content"].strip()
    except Exception as e:
        log.warning(f"  Не удалось загрузить описание: {e}")
    return ""


# ============================================================
# ПАРСЕРЫ
# ============================================================

def parse_gsmarena_rss() -> list[dict]:
    """
    RSS-фид GSMArena: ~20 новостей с заголовками и датами.
    Один запрос для списка, описания не берём (в RSS — полный текст статьи).
    """
    try:
        resp = requests.get(GSMARENA_RSS, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Ошибка загрузки RSS GSMArena: {e}")
        return []

    soup = BeautifulSoup(resp.text, "xml")
    articles = []

    for item in soup.find_all("item"):
        link = item.find("link")
        title = item.find("title")
        pub_date = item.find("pubDate")

        if not link or not link.text:
            continue

        url = link.text.strip()

        # Только новости, пропускаем обзоры и прочее
        if "-news-" not in url:
            continue

        art = {
            "url": url,
            "source": "GSMArena",
            "title": title.text.strip() if title else "",
        }

        # pubDate: "Mon, 16 Mar 2026 16:03:02 +0100"
        if pub_date and pub_date.text:
            try:
                dt = parsedate_to_datetime(pub_date.text.strip())
                dt_msk = dt.astimezone(MSK)
                art["date"] = dt_msk.strftime("%d.%m.%Y")
                art["time"] = dt_msk.strftime("%H:%M")
                art["dt"] = dt_msk
            except (ValueError, TypeError):
                pass

        articles.append(art)

    return articles


def parse_phonearena_month(year: int, month: int) -> list[dict]:
    """Месячный sitemap PhoneArena: все статьи за месяц с датами."""
    url = PHONEARENA_MONTH_SITEMAP.format(year=year, month=month)

    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Ошибка загрузки {url}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "xml")
    articles = []

    for url_tag in soup.find_all("url"):
        loc = url_tag.find("loc")
        if not loc:
            continue
        href = loc.text.strip()
        if "/news/" not in href:
            continue

        lastmod = url_tag.find("lastmod")
        dt_msk = None
        if lastmod and lastmod.text:
            try:
                dt = datetime.fromisoformat(lastmod.text.strip())
                dt_msk = dt.astimezone(MSK)
            except ValueError:
                pass

        art = {
            "url": href,
            "source": "PhoneArena",
            "title": title_from_url(href),
        }
        if dt_msk:
            art["date"] = dt_msk.strftime("%d.%m.%Y")
            art["time"] = dt_msk.strftime("%H:%M")
            art["dt"] = dt_msk

        articles.append(art)

    return articles


def fetch_googlenews_titles() -> dict[str, str]:
    """Загружает {url: title} из Google News sitemap (~30 свежих статей)."""
    titles = {}
    try:
        resp = requests.get(PHONEARENA_GOOGLENEWS, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "xml")
        for url_tag in soup.find_all("url"):
            loc = url_tag.find("loc")
            title_tag = url_tag.find("title")
            if loc and title_tag:
                titles[loc.text.strip()] = title_tag.text.strip()
    except Exception as e:
        log.warning(f"Не удалось загрузить Google News sitemap: {e}")
    return titles


# ============================================================
# GOOGLE SHEETS
# ============================================================

def connect_google_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    gc = gspread.authorize(creds)

    if SPREADSHEET_ID:
        sh = gc.open_by_key(SPREADSHEET_ID)
    elif SPREADSHEET_NAME:
        sh = gc.open(SPREADSHEET_NAME)
    else:
        raise ValueError("Укажи SPREADSHEET_NAME или SPREADSHEET_ID!")

    return sh.worksheet(WORKSHEET_NAME)


def get_existing_data(ws) -> tuple[set, dict[str, datetime]]:
    """
    Считывает столбцы B (дата), C (время), E (ссылка) из таблицы.
    Возвращает:
      - set всех URL (для дедупликации)
      - dict {домен: datetime} — самая свежая статья каждого домена (cutoff)
    """
    # Один batch-запрос: B, C, E
    all_data = ws.get("B:E")

    existing_urls = set()
    cutoffs = {}  # "gsmarena.com" -> datetime, "phonearena.com" -> datetime

    for row in all_data[1:]:  # пропускаем заголовок
        if len(row) < 4 or not row[3]:
            continue

        url = row[3]
        existing_urls.add(url)

        date_str = row[0] if len(row) > 0 else ""
        time_str = row[1] if len(row) > 1 else ""

        if not date_str or not time_str:
            continue

        # Определяем домен
        domain = None
        if "gsmarena.com" in url:
            domain = "gsmarena"
        elif "phonearena.com" in url:
            domain = "phonearena"
        else:
            continue

        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
            dt = dt.replace(tzinfo=MSK)
            if domain not in cutoffs or dt > cutoffs[domain]:
                cutoffs[domain] = dt
        except ValueError:
            continue

    return existing_urls, cutoffs


def get_first_empty_row(ws) -> int:
    values = ws.col_values(COL_LINK)
    return len(values) + 1


def write_articles(ws, articles: list[dict], start_row: int) -> int:
    """Записывает статьи в столбцы B-E. Столбец A (Статус) не трогает."""
    if not articles:
        return 0

    rows = []
    for art in articles:
        rows.append([
            art.get("date", ""),
            art.get("time", ""),
            art.get("annotation", ""),
            art["url"],
        ])

    end_row = start_row + len(rows) - 1
    ws.update(values=rows, range_name=f"B{start_row}:E{end_row}", value_input_option="USER_ENTERED")

    return len(rows)


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

def main():
    log.info("=" * 50)
    log.info("Запуск парсера новостей v5.0")
    log.info("=" * 50)

    now = datetime.now(MSK)

    # Подключаемся к Google Sheets
    try:
        ws = connect_google_sheets()
        log.info("Подключение к таблице — OK")
    except Exception as e:
        log.error(f"Не удалось подключиться к Google Sheets: {e}")
        return

    # Считываем существующие данные из таблицы (1 запрос)
    existing_urls, cutoffs = get_existing_data(ws)
    log.info(f"В таблице уже {len(existing_urls)} ссылок")
    for domain, dt in cutoffs.items():
        log.info(f"  Последняя {domain}: {dt.strftime('%d.%m.%Y %H:%M')}")

    all_new_articles = []

    # ---- GSMArena (RSS) ----
    log.info("Парсим GSMArena (RSS)...")
    gsm_articles = parse_gsmarena_rss()
    log.info(f"  Статей в RSS: {len(gsm_articles)}")

    gsm_cutoff = cutoffs.get("gsmarena")
    new_gsm = [
        a for a in gsm_articles
        if a["url"] not in existing_urls
        and (not gsm_cutoff or a.get("dt") and a["dt"] > gsm_cutoff)
    ]
    log.info(f"  Новых (после cutoff): {len(new_gsm)}")

    for art in new_gsm:
        # og:description со страницы статьи (1 запрос на статью)
        desc = fetch_og_description(art["url"])
        art["annotation"] = translate_text(desc) if desc else translate_text(art["title"])
        log.info(f"    {art.get('date', '???')} {art.get('time', '???')} — {art['title'][:50]}")

    all_new_articles.extend(new_gsm)

    # ---- PhoneArena (месячный sitemap) ----
    log.info("Парсим PhoneArena (месячный sitemap)...")

    all_pa = parse_phonearena_month(now.year, now.month)

    # В первые 3 дня месяца также проверяем предыдущий
    if now.day <= 3:
        prev = now.replace(day=1) - timedelta(days=1)
        log.info(f"  Также проверяем {prev.year}-{prev.month:02d}...")
        all_pa.extend(parse_phonearena_month(prev.year, prev.month))

    log.info(f"  Статей в sitemap: {len(all_pa)}")

    pa_cutoff = cutoffs.get("phonearena")
    new_pa = [
        a for a in all_pa
        if a["url"] not in existing_urls
        and (not pa_cutoff or a.get("dt") and a["dt"] > pa_cutoff)
    ]
    log.info(f"  Новых (после cutoff): {len(new_pa)}")

    if new_pa:
        gn_titles = fetch_googlenews_titles()
        log.info(f"  Заголовков из Google News: {len(gn_titles)}")

        for art in new_pa:
            if art["url"] in gn_titles:
                art["title"] = gn_titles[art["url"]]
            art["annotation"] = translate_text(art["title"])
            log.info(f"    {art.get('date', '???')} {art.get('time', '???')} — {art['title'][:50]}")

    all_new_articles.extend(new_pa)

    # ---- Сортировка и запись ----
    far_future = datetime.max.replace(tzinfo=MSK)
    all_new_articles.sort(key=lambda a: a.get("dt", far_future))

    if all_new_articles:
        log.info(f"Итого {len(all_new_articles)} новых статей:")
        for art in all_new_articles[:10]:
            log.info(f"  {art.get('date', '???')} {art.get('time', '???')} [{art['source']}] {art['title'][:40]}")
        if len(all_new_articles) > 10:
            log.info(f"  ... и ещё {len(all_new_articles) - 10}")

    total_added = 0
    if all_new_articles:
        start_row = get_first_empty_row(ws)
        added = write_articles(ws, all_new_articles, start_row)
        total_added = added
        log.info(f"Записано {added} строк, начиная с {start_row}")

    log.info("=" * 50)
    log.info(f"Готово! Добавлено строк: {total_added}")
    log.info("=" * 50)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
