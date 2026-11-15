"""Phase 2: send the pages the local converter found hard to Gemini, and keep Gemini's version only where it is safe.

Run from the repo root on GitHub (cloud-read workflow): python -m convert.cloud_pass

Which files: markdown made by the table-aware converter (converter: hybrid-v2) whose frontmatter still lists
`cloud_pages`, newest meeting first. For each file the original PDF is downloaded again and the listed pages are sent to
Gemini in windows of up to 10 consecutive pages (one request per window), with order-search's prompt.

Number guard (from order-search, hybrid_convert.choose_page): for every page, the numbers in the PDF's own text layer are
counted in Gemini's version and in the local version. Gemini's version replaces the local one only if it keeps every
number (or at least as many as the local version). Scanned pages have no text layer, so nothing can be checked: Gemini's
version replaces the Tesseract text (it reads scans far better), and the page is listed in `cloud_unchecked`.

Frontmatter after the pass: `cloud_pages` keeps only pages still to do, plus
    cloud_replaced     pages now holding Gemini's version (numbers checked)
    cloud_unchecked    scanned pages now holding Gemini's version (no text layer to check against)
    cloud_kept_local   pages where Gemini lost numbers, so the local version stays
    cloud_empty        pages Gemini returned nothing for
    cloud_model, cloud_date

Quota: Google's limit is per project and per model, shared with order-search. The job stops for the day at
GEMINI_DAILY_CAP requests (counted in _system/gemini_usage.json by Pacific date, when Google resets) or as soon as Google
reports the daily limit, whichever comes first. A per-minute limit only pauses it.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import KB_ROOT, SYSTEM_DIR  # noqa: E402
from convert import hybrid_convert as hc  # noqa: E402
from convert import table_aware  # noqa: E402
from convert.reconvert import join, split  # noqa: E402
from bot import download_pdf  # noqa: E402

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
DAILY_CAP = int(os.environ.get("GEMINI_DAILY_CAP", "250"))
BUDGET_MINUTES = float(os.environ.get("CLOUD_BUDGET_MIN", "300"))
STATE_FILE = SYSTEM_DIR / "cloud_state.json"
USAGE_FILE = SYSTEM_DIR / "gemini_usage.json"
MAX_ATTEMPTS = 3
WINDOW = 10
MIN_GAP_S = 5            # free tier allows ~15 requests a minute; stay well below
SKIP_NAMES = {"README.md", "NEEDS_ATTENTION.md"}
MARKER = re.compile(r"^<!-- page (\d+)(?: \| printed \d+)? -->$", re.M)
PROMPT = (
    "Convert these PDF pages to GitHub-flavored Markdown. They are from Indian power transmission documents (meeting "
    "minutes, connectivity applications, transmission schemes). Rules: (1) Put the line <!-- page-break --> between "
    "pages, in order, and nothing else between them. (2) For every table, write its table number and title on the line "
    "above it (as printed) and state the unit shown in the heading (MW / MWh / MVA / kV / km / Rs. Crore). (3) Keep every "
    "column and every column header exactly as printed; never merge or drop columns; repeat multi-line headers on one "
    "header row. (4) Keep each row's text on ONE line (unwrap text that wraps inside a cell). (5) Where a cell shows '-' "
    "or 'NA' write exactly that; leave genuinely empty cells empty so columns do not shift. (6) Copy numbers, dates and "
    "application numbers exactly as printed, with their decimal points; never compute, round or invent. (7) No commentary.")


class DailyLimit(Exception):
    pass


# ---------------------------------------------------------------- quota bookkeeping
def _pacific_day():
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def used_today():
    return _load(USAGE_FILE).get(f"{_pacific_day()}|{MODEL}", 0)


def count_request():
    u = _load(USAGE_FILE)
    k = f"{_pacific_day()}|{MODEL}"
    u[k] = u.get(k, 0) + 1
    _save(USAGE_FILE, u)


# ---------------------------------------------------------------- Gemini
class Gemini:
    def __init__(self):
        from google import genai
        from google.genai import types
        self.types = types
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.last = 0.0
        self.requests = 0

    def ask(self, pdf_bytes):
        """-> text or None (model returned nothing). Raises DailyLimit; other persistent errors raise RuntimeError."""
        if used_today() >= DAILY_CAP:
            raise DailyLimit(f"daily cap of {DAILY_CAP} requests reached")
        last_err = ""
        for attempt, wait in enumerate((0, 30, 60, 120, 300)):
            time.sleep(max(wait, MIN_GAP_S - (time.time() - self.last)))
            self.last = time.time()
            try:
                res = self.client.models.generate_content(
                    model=MODEL,
                    contents=[self.types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), PROMPT])
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                if re.search(r"429|RESOURCE_EXHAUSTED|quota", last_err, re.I):
                    if re.search(r"PerDay|per day", last_err, re.I) or not re.search(r"PerMinute|per minute|retry", last_err, re.I):
                        raise DailyLimit(last_err[:300]) from exc
                print(f"   Gemini error (attempt {attempt + 1}/5): {last_err[:150]}", flush=True)
                continue
            count_request()
            self.requests += 1
            return res.text or None
        raise RuntimeError(f"Gemini kept failing: {last_err[:200]}")


def split_pages(text, n):
    if not text:
        return None
    text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text.strip())
    parts = [p.strip() for p in re.split(r"<!--\s*page-break\s*-->", text)]
    parts = [p for p in parts if p] if len(parts) != n else parts
    return parts if len(parts) == n else None


def pages_pdf(doc, idxs):
    import pymupdf
    out = pymupdf.open()
    for i in idxs:
        out.insert_pdf(doc, from_page=i, to_page=i)
    data = out.tobytes()
    out.close()
    return data


def windows(pages):
    """1-based pages -> runs of 0-based indices spanning at most WINDOW pages (pages between are sent but not used)."""
    runs, cur = [], []
    for p in sorted(pages):
        i = p - 1
        if cur and i - cur[0] >= WINDOW:
            runs.append(cur)
            cur = []
        cur.append(i)
    if cur:
        runs.append(cur)
    return runs


def read_cloud(gem, doc, pages):
    """-> {1-based page: cloud markdown or ''} for the pages read in this call. DailyLimit may interrupt: what was read so
    far is returned through the exception's .partial."""
    got = {}
    try:
        for run in windows(pages):
            span = list(range(run[0], run[-1] + 1))
            parts = split_pages(gem.ask(pages_pdf(doc, span)), len(span))
            if parts is not None:
                part_of = dict(zip(span, parts))
                for i in run:
                    got[i + 1] = part_of[i]
            else:                                    # page-break count wrong: ask one page at a time
                for i in run:
                    one = split_pages(gem.ask(pages_pdf(doc, [i])), 1)
                    got[i + 1] = one[0] if one else ""
    except DailyLimit as exc:
        exc.partial = got
        raise
    return got


# ---------------------------------------------------------------- markdown pages
def body_pages(body):
    """-> (head, [(page_no, marker_line, content)]) for a body made of '<!-- page N -->' sections."""
    ms = list(MARKER.finditer(body))
    head = body[:ms[0].start()] if ms else body
    out = []
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(body)
        out.append((int(m.group(1)), m.group(0), body[m.end():end].strip("\n")))
    return head, out


def build_body(head, sections):
    return head + "\n".join(f"{marker}\n\n{content}\n" for _, marker, content in sections)


def apply(front, body, doc, got):
    head, sections = body_pages(body)
    lists = {k: list(front.get(k) or []) for k in ("cloud_replaced", "cloud_unchecked", "cloud_kept_local", "cloud_empty")}
    new = []
    for no, marker, content in sections:
        if no in got:
            cloud = got[no]
            pdf_text = doc[no - 1].get_text("text")
            prefix = "\n\n".join(re.findall(r"<!-- table continues from page \d+ -->", content))
            if not cloud:
                lists["cloud_empty"].append(no)
            else:
                chosen, source, _ = hc.choose_page(pdf_text, content, cloud)
                if source == "cloud":
                    content = f"{prefix}\n\n{cloud}".strip() if prefix else cloud
                    lists["cloud_unchecked" if len(pdf_text.strip()) < 80 else "cloud_replaced"].append(no)
                else:
                    lists["cloud_kept_local"].append(no)
        new.append((no, marker, content))
    front["cloud_pages"] = [p for p in front.get("cloud_pages") or [] if p not in got]
    for k, v in lists.items():
        if v:
            front[k] = sorted(set(v))
    front["cloud_model"] = MODEL
    front["cloud_date"] = date.today().isoformat()
    if front.get("needs_review") and not set(front.get("scanned_pages") or []) - set(lists["cloud_unchecked"]):
        front["needs_review"] = "scanned pages read by Gemini; figures not checked against a text layer"
    return front, build_body(head, new)


# ---------------------------------------------------------------- main
def _sort_key(front):
    d = str(front.get("date") or "")
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", d)
    return (m.group(3) + m.group(2) + m.group(1)) if m else "00000000"


def candidates():
    paths = [p for p in KB_ROOT.glob("*.md") if p.name not in SKIP_NAMES]
    paths += list((KB_ROOT / "cea").rglob("*.md")) + list((KB_ROOT / "gna" / "documents").rglob("*.md"))
    found = []
    for p in paths:
        front, _ = split(p.read_text(encoding="utf-8"))
        if front and front.get("converter") == table_aware.CONVERTER and front.get("cloud_pages") and front.get("original_url"):
            found.append((_sort_key(front), p))
    return [p for _, p in sorted(found, key=lambda x: x[0], reverse=True)]


def main():
    import pymupdf
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set; nothing to do.")
        return
    state = _load(STATE_FILE)
    gem = Gemini()
    start = time.time()
    files = pages_done = 0
    down = {}
    todo = candidates()
    print(f"{len(todo)} files have pages for the cloud step; used today on {MODEL}: {used_today()}/{DAILY_CAP}", flush=True)
    stop = ""
    for path in todo:
        rel = path.relative_to(KB_ROOT).as_posix()
        if state.get(rel, {}).get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        if (time.time() - start) / 60 > BUDGET_MINUTES:
            stop = "time budget used up"
            break
        front, body = split(path.read_text(encoding="utf-8"))
        host = urlparse(front["original_url"]).netloc
        if down.get(host, 0) >= 5:
            continue
        print(f"-> {rel}: {len(front['cloud_pages'])} pages", flush=True)
        try:
            pdf = download_pdf(front["original_url"])
            down[host] = 0
        except Exception as exc:  # noqa: BLE001
            down[host] = down.get(host, 0) + 1
            print(f"   download failed: {str(exc)[:150]}", flush=True)
            continue
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        try:
            got = read_cloud(gem, doc, front["cloud_pages"])
        except DailyLimit as exc:
            got = exc.partial
            stop = f"daily limit: {exc}"
        except Exception as exc:  # noqa: BLE001
            entry = state.setdefault(rel, {})
            entry.update(attempts=entry.get("attempts", 0) + 1, error=str(exc)[:200])
            _save(STATE_FILE, state)
            print(f"   failed: {str(exc)[:150]}", flush=True)
            doc.close()
            continue
        if got:
            front, body = apply(front, body, doc, got)
            path.write_text(join(front, body.lstrip("\n")), encoding="utf-8")
            pages_done += len(got)
            files += 1
            print(f"   read {len(got)} pages: replaced {len(front.get('cloud_replaced') or [])}, kept local "
                  f"{len(front.get('cloud_kept_local') or [])}, unchecked scans {len(front.get('cloud_unchecked') or [])}, "
                  f"empty {len(front.get('cloud_empty') or [])} (file totals)", flush=True)
        doc.close()
        if stop:
            break
    if stop:
        print(f"Stopped: {stop}", flush=True)
    left = len(candidates())
    print(f"\nCloud step: {files} files, {pages_done} pages read with {gem.requests} requests "
          f"({used_today()}/{DAILY_CAP} used today); {left} files still have pages to read "
          f"({(time.time() - start) / 60:.0f} min).", flush=True)


if __name__ == "__main__":
    main()
