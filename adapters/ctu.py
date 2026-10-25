"""Adapter for CTU (Central Transmission Utility of India) sources.

Handles two URL shapes:
  1. Direct PDF links -> returned as-is.
  2. The ISTS Consultation Meetings listing page, which loads its table via a
     POST AJAX endpoint (discovered by inspecting network traffic with
     Playwright): https://ctuil.in/ists-consultation-meeting?p=ajax

The listing page has region tabs (All India, Northern, Eastern, Southern,
Western, North Eastern) but posting activeRegion="All Region" and paginating
through every page already returns every document across all regions, so we
don't need to click each tab individually - one paginated sweep is enough.
"""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import SYSTEM_DIR

LISTING_URL = "https://ctuil.in/ists-consultation-meeting"
AJAX_URL = "https://ctuil.in/ists-consultation-meeting?p=ajax"
ARCHIVE_LISTING_URL = "https://ctuil.in/ists-consultation-meeting/archive"
ARCHIVE_AJAX_URL = "https://ctuil.in/ists-consultation-meeting/archive?p=ajax"
BASE_URL = "https://ctuil.in"

# --- Incremental historical backfill ----------------------------------------
#
# We only want documents on/after a cutoff date, but rather than pull the
# entire history in one expensive run, the cutoff pushes itself back by
# BACKFILL_STEP_DAYS every time this adapter runs (i.e. every weekly job).
# Already-converted PDFs are skipped by bot.py regardless of discovery range,
# so widening the window costs nothing for dates already covered - only the
# newly-exposed older documents get downloaded and OCR'd each week. State
# (the current cutoff) persists in a small JSON file next to this project.
BACKFILL_STATE_FILE = SYSTEM_DIR / "backfill_state.json"
INITIAL_MIN_DATE = datetime(2025, 1, 1)
BACKFILL_STEP_DAYS = 100  # ~3.3 months back per run - >1 year back within 3-4 weekly runs
BACKFILL_FLOOR_DATE = datetime(2021, 1, 1)  # confirmed start of CTU's published CMETS history (two
# consecutive empty discovery runs below 2021-03-03 on 2026-10-09) - stop pushing back further


def escape_bare_ampersands(html):
    """CTU file names contain raw '&' (e.g. "Conn&LTA_Minutes..."). html.parser
    reads "&LT" as the legacy entity '<' and corrupts the link, so escape any
    '&' that doesn't start a complete entity before parsing."""
    return re.sub(r"&(?![A-Za-z][A-Za-z0-9]*;|#\d+;|#[xX][0-9A-Fa-f]+;)", "&amp;", html)


def _load_and_advance_min_date():
    """Return this run's cutoff date, pushing it further into the past and
    persisting the new value for next time."""
    if BACKFILL_STATE_FILE.exists():
        try:
            state = json.loads(BACKFILL_STATE_FILE.read_text(encoding="utf-8"))
            current_min = datetime.strptime(state["min_date"], "%Y-%m-%d")
        except Exception:
            current_min = INITIAL_MIN_DATE
    else:
        current_min = INITIAL_MIN_DATE

    if current_min <= BACKFILL_FLOOR_DATE:
        new_min = current_min  # backfill already complete - hold steady
    else:
        new_min = max(current_min - timedelta(days=BACKFILL_STEP_DAYS), BACKFILL_FLOOR_DATE)

    BACKFILL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKFILL_STATE_FILE.write_text(
        json.dumps({"min_date": new_min.strftime("%Y-%m-%d")}, indent=2),
        encoding="utf-8",
    )
    print(f"[ctu adapter] Backfill cutoff for this run: {new_min.date()} "
          f"(was {current_min.date()})")
    return new_min

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": LISTING_URL,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

MAX_PAGES_SAFETY_CAP = 200


def discover_pdfs(url):
    """Return a list of full PDF URLs discoverable from `url`."""
    if url.lower().split("?")[0].endswith(".pdf"):
        return [url]

    if "ists-consultation-meeting" in url:
        live = _scan_listing(AJAX_URL, LISTING_URL, "", _load_and_advance_min_date())
        # The archive holds the pre-CMETS "Connectivity & LTA" meetings (2006-2023).
        # It's a closed, historical set, so the backfill cutoff doesn't apply to it.
        archive = _scan_listing(ARCHIVE_AJAX_URL, ARCHIVE_LISTING_URL, "archive", None)
        return sorted(set(live) | set(archive))

    return []


def _parse_row_date(text):
    text = text.strip()
    for fmt in ("%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _scan_listing(ajax_url, referer, archive_value, min_date):
    """Return Minutes-only PDF links from one CMETS listing (live or archive).

    The listing table has columns: Sr.No., Meeting, Date, Region, Notice,
    Agenda, Minutes (in that order when activeRegion="All Region"). Rows are
    sorted by meeting_date DESC, so with a cutoff we stop paginating as soon
    as a whole page falls before it. With min_date=None every row is taken,
    including archive rows whose date cell is blank.
    """
    headers = dict(HEADERS, Referer=referer)
    pdf_links = set()
    page = 1
    total_pages = None

    session = requests.Session()

    while True:
        data = {
            "archive": archive_value,
            "activeRegion": "All Region",
            "sort_field": "IstsConsultationMeetings.meeting_date",
            "sort_type": "DESC",
            "page": str(page),
            "search_keyword": "",
            "cnslt_mtng_from_date": "",
            "cnslt_mtng_to_date": "",
        }

        resp = session.post(ajax_url, headers=headers, data=data, timeout=30)
        resp.raise_for_status()
        html = resp.text

        soup = BeautifulSoup(escape_bare_ampersands(html), "html.parser")
        rows = soup.select("table.tableStyle tr")

        data_rows = [r for r in rows if r.find("td") is not None]
        if not data_rows and page > 1:
            break

        any_row_in_range = False
        for row in data_rows:
            cells = row.find_all("td")
            if len(cells) < 7:
                continue  # not a full Sr.No/Meeting/Date/Region/Notice/Agenda/Minutes row

            if min_date is not None:
                row_date = _parse_row_date(cells[2].get_text())
                if row_date is None or row_date < min_date:
                    continue

            any_row_in_range = True
            minutes_cell = cells[6]
            for a in minutes_cell.find_all("a", href=True):
                if a["href"].lower().endswith(".pdf"):
                    pdf_links.add(urljoin(BASE_URL, a["href"]))

        if min_date is not None and data_rows and not any_row_in_range:
            # Sorted DESC by date: once a full page is entirely before the
            # cutoff, every following page will be older still.
            break

        if total_pages is None:
            page_numbers = [int(n) for n in re.findall(r"gotopage\((\d+)\)", html)]
            total_pages = max(page_numbers) if page_numbers else 1

        if page >= total_pages or page >= MAX_PAGES_SAFETY_CAP:
            break

        page += 1
        time.sleep(0.5)  # be polite to the server

    return sorted(pdf_links)
