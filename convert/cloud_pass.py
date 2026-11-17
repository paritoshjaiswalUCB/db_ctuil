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


# ---------------------------------------------------------------- number guard
# order-search's guard (hybrid_convert.choose_page) with two corrections found on CEA/CTU minutes (diagnosis 15 Nov 2026):
# (1) a number is counted whether or not a unit is glued to it ("1150kV" and "1150 kV" are the same 1150);
# (2) numbers in a running header/footer (e.g. the CEA file number "13/3/2019 ... I/71340/2026" printed on every page) are
#     not expected in the page body, so they are left out.
GUARD_VERSION = 3          # 2: running header/footer numbers ignored; 3: word check, scans compared with Tesseract
GUARD_NUM = re.compile(r"(?<![\d.,])\d[\d,]*(?:\.\d+)?")
MARGIN_PT = 60


def guard_numbers(text):
    from collections import Counter
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    toks = [t.replace(",", "").rstrip(".") for t in GUARD_NUM.findall(body)]
    return Counter(t for t in toks if len(t.replace(".", "")) >= 3 or "." in t)


def _margin_text(page):
    h = page.rect.height
    return "\n".join(b[4] for b in page.get_text("blocks") if b[6] == 0 and (b[1] > h - MARGIN_PT * 1.5 or b[3] < MARGIN_PT))


def running_numbers(doc):
    """Numbers printed in the top/bottom margin of at least half the pages (running header/footer)."""
    from collections import Counter
    if len(doc) < 4:
        return set()
    seen = Counter()
    for page in doc:
        seen.update(set(guard_numbers(_margin_text(page))))
    return {k for k, c in seen.items() if c >= 0.5 * len(doc)}


def guard_truth(page, running):
    truth = guard_numbers(page.get_text("text"))
    if running:
        margin = guard_numbers(_margin_text(page))
        for k in running:
            truth[k] -= margin.get(k, 0)
    return +truth


def recall(truth, md):
    total = sum(truth.values())
    if not total:
        return 1.0
    out = guard_numbers(md)
    return sum(min(c, out[k]) for k, c in truth.items()) / total


# Word check (guard v3, 17 Nov 2026): the number check alone let Gemini drop sentences without figures ("Members agreed
# for the same"). Every word of 3+ letters in the PDF text layer must also be in Gemini's version; allowed exceptions are
# words of the running header/footer and pieces of words hyphenated across a line break ("connectiv-" + "ity").
WORD = re.compile(r"[A-Za-z]{3,}")
WORD_SLACK = 0             # no word may be lost beyond what the local version also misses (user rule: no information loss)


def guard_words(text):
    from collections import Counter
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    body = re.sub(r"(?<=[A-Za-z])-\s*(?=[A-Za-z])", "", body)   # "non-solar", "non-\nsolar", "connectiv-\nity" alike
    return Counter(w.lower() for w in WORD.findall(body))


def running_words(doc):
    from collections import Counter
    if len(doc) < 4:
        return set()
    seen = Counter()
    for page in doc:
        seen.update(set(guard_words(_margin_text(page))))
    return {k for k, c in seen.items() if c >= 0.5 * len(doc)}


def word_truth(page, running_w):
    truth = guard_words(page.get_text("text"))
    if running_w:
        margin = guard_words(_margin_text(page))
        for k in running_w:
            truth[k] -= margin.get(k, 0)
    return +truth


def missing_words(truth, md):
    out = guard_words(md)
    longer = [w for w in out if len(w) > 3]
    miss = {}
    for k, c in truth.items():
        if out[k] >= c:
            continue
        if any(k in w and w != k for w in longer):
            continue                                 # a piece of a word that was hyphenated or split across lines
        miss[k] = c - out[k]
    return miss


def choose(truth, local_md, cloud_md, wtruth=None):
    """-> (source, reason). Gemini's version is used only if it keeps every number and every word the local version keeps."""
    rc, rl = recall(truth, cloud_md), recall(truth, local_md)
    if not (rc >= 0.999 or rc >= rl):
        return "local", f"numbers {rc:.3f} < local {rl:.3f}"
    if wtruth is not None:
        mc, ml = sum(missing_words(wtruth, cloud_md).values()), sum(missing_words(wtruth, local_md).values())
        if mc > ml + WORD_SLACK:
            return "local", f"{mc} words missing (local {ml})"
    return "cloud", "ok"


# Scanned pages have no text layer, so Gemini is compared with the Tesseract reading instead: two independent readers.
# If they agree on the figures, Gemini's (better structured) version is used. If not, both readings are kept on the page
# and the page is listed in cloud_scan_disagree, so nothing rests on one unchecked reading.
SCAN_COVERED = 1.0         # every figure Tesseract read must be in Gemini's version (diagnosis 17 Nov: Gemini drops tables on scans)
SCAN_SUPPORTED = 0.95      # nearly every figure of Gemini's version must also be in Tesseract's reading


def scan_agreement(tess_md, cloud_md):
    t, c = guard_numbers(tess_md), guard_numbers(cloud_md)
    nt, nc = sum(t.values()), sum(c.values())
    common = sum(min(v, c[k]) for k, v in t.items())
    covered = common / nt if nt else 1.0             # Tesseract's figures found in Gemini (nothing dropped)
    supported = common / nc if nc else 1.0           # Gemini's figures found in Tesseract (nothing invented)
    return covered, supported


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


LISTS = ("cloud_replaced", "cloud_kept_local", "cloud_scan_agreed", "cloud_scan_disagree", "cloud_empty")
SECOND_READING = "<!-- second reading of this scanned page (Tesseract OCR): the two readings disagree on figures; check the PDF -->"


def _is_scanned(page):
    return len(page.get_text("text").strip()) < 80


def _decide(page, running, local_md, cloud_md):
    """-> (list name, page content) for one page with both a local and a Gemini version."""
    rn, rw = running
    if _is_scanned(page):
        covered, supported = scan_agreement(local_md, cloud_md)
        if covered >= SCAN_COVERED and supported >= SCAN_SUPPORTED:
            return "cloud_scan_agreed", cloud_md
        return "cloud_scan_disagree", f"{cloud_md}\n\n{SECOND_READING}\n\n{local_md}"
    source, _ = choose(guard_truth(page, rn), local_md, cloud_md, word_truth(page, rw))
    return ("cloud_replaced", cloud_md) if source == "cloud" else ("cloud_kept_local", local_md)


def _finish(front, lists):
    for k in LISTS:
        if lists[k]:
            front[k] = sorted(set(lists[k]))
        else:
            front.pop(k, None)
    front.pop("cloud_unchecked", None)
    front["cloud_guard"] = GUARD_VERSION
    front["cloud_model"] = MODEL
    front["cloud_date"] = date.today().isoformat()
    if lists["cloud_scan_disagree"]:
        front["needs_review"] = (f"scanned pages {sorted(set(lists['cloud_scan_disagree']))}: Gemini and Tesseract disagree "
                                 "on figures; both readings are kept on the page")
    return front


def apply(front, body, doc, got):
    head, sections = body_pages(body)
    lists = {k: [p for p in (front.get(k) or []) if p not in got] for k in LISTS}
    running = (running_numbers(doc), running_words(doc))
    new = []
    for no, marker, content in sections:
        if no in got:
            prefix = "\n\n".join(re.findall(r"<!-- table continues from page \d+ -->", content))
            if not got[no]:
                lists["cloud_empty"].append(no)
            else:
                name, chosen = _decide(doc[no - 1], running, content, got[no])
                lists[name].append(no)
                if name != "cloud_kept_local":
                    content = f"{prefix}\n\n{chosen}".strip() if prefix else chosen
        new.append((no, marker, content))
    front["cloud_pages"] = [p for p in front.get("cloud_pages") or [] if p not in got]
    return _finish(front, lists), build_body(head, new)


def needs_recheck(front):
    return (front.get("cloud_guard") or 0) < GUARD_VERSION and bool(front.get("cloud_replaced") or front.get("cloud_unchecked"))


def recheck(front, body, doc):
    """Pages accepted under an older guard are checked again with the current one; no Gemini requests are needed. The
    local version is rebuilt from the PDF (and Tesseract for scanned pages); a page that fails gets it back."""
    head, sections = body_pages(body)
    old = set(front.get("cloud_replaced") or []) | set(front.get("cloud_unchecked") or [])
    lists = {k: [p for p in (front.get(k) or []) if p not in old] for k in LISTS}
    running = (running_numbers(doc), running_words(doc))
    rebuilt = {r["idx"] + 1: r["md"] for r in hc.convert_pages(doc, [p - 1 for p in sorted(old)])}
    new, restored = [], 0
    for no, marker, content in sections:
        if no in old:
            prefix = "\n\n".join(re.findall(r"<!-- table continues from page \d+ -->", content))
            cloud_md = re.sub(r"<!-- table continues from page \d+ -->", "", content).strip()
            local_md = table_aware._ocr(doc[no - 1]) if _is_scanned(doc[no - 1]) else rebuilt.get(no, "")
            name, chosen = _decide(doc[no - 1], running, local_md, cloud_md)
            lists[name].append(no)
            restored += name == "cloud_kept_local"
            content = f"{prefix}\n\n{chosen}".strip() if prefix else chosen
        new.append((no, marker, content))
    return _finish(front, lists), build_body(head, new), restored


# ---------------------------------------------------------------- main
def _sort_key(front):
    d = str(front.get("date") or "")
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", d)
    return (m.group(3) + m.group(2) + m.group(1)) if m else "00000000"


def requeue(front):
    """Pages kept local under an older number guard go back into cloud_pages."""
    kept = front.get("cloud_kept_local") or []
    if kept and (front.get("cloud_guard") or 0) < 2:          # guard 1 rejected pages for footer numbers: ask again
        front["cloud_pages"] = sorted(set(front.get("cloud_pages") or []) | set(kept))
        front.pop("cloud_kept_local")
    front.setdefault("cloud_pages", [])
    front["cloud_pages"] = front["cloud_pages"] or []
    return front


def candidates():
    paths = [p for p in KB_ROOT.glob("*.md") if p.name not in SKIP_NAMES]
    paths += list((KB_ROOT / "cea").rglob("*.md")) + list((KB_ROOT / "gna" / "documents").rglob("*.md"))
    found = []
    for p in paths:
        front, _ = split(p.read_text(encoding="utf-8"))
        if (front and front.get("converter") == table_aware.CONVERTER and front.get("original_url")
                and (needs_recheck(front) or requeue(front)["cloud_pages"])):
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
    files = pages_done = rechecked = 0
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
        front = requeue(front)
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
        if needs_recheck(front):
            front, body, restored = recheck(front, body, doc)
            path.write_text(join(front, body.lstrip("\n")), encoding="utf-8")
            rechecked += 1
            print(f"   re-checked earlier Gemini pages: {restored} given back the local version; agreed scans "
                  f"{len(front.get('cloud_scan_agreed') or [])}, disagreeing scans {len(front.get('cloud_scan_disagree') or [])}",
                  flush=True)
        if not front["cloud_pages"] or stop:
            doc.close()
            continue
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
            print(f"   read {len(got)} pages (file totals: " + ", ".join(
                f"{k.removeprefix('cloud_')} {len(front.get(k) or [])}" for k in LISTS) + ")", flush=True)
        doc.close()
        # after the daily limit the loop goes on only to re-check earlier pages (no Gemini requests needed)
    if stop:
        print(f"Gemini stopped: {stop}", flush=True)
    left = len(candidates())
    print(f"\nCloud step: re-checked {rechecked} files; {files} files, {pages_done} pages read with {gem.requests} requests "
          f"({used_today()}/{DAILY_CAP} used today); {left} files still have pages to read "
          f"({(time.time() - start) / 60:.0f} min).", flush=True)


if __name__ == "__main__":
    main()
