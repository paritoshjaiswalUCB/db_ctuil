"""Re-convert documents made by the old plain-text converter with the table-aware one (phase 1, local only).

Run from the repo root on GitHub (reconvert workflow): python -m convert.reconvert

Candidates, in this order: CMETS minutes (top of the folder), CEA committee files (cea/), then GNA documents that have
no markdown table at all. A file is done once its frontmatter says `converter: hybrid-v1`. Each file is downloaded again
from its `original_url`, converted, and rewritten in place: the body is replaced, the frontmatter is kept and gets the
converter fields (cloud_pages lists the pages a later cloud step should re-read).

Progress is kept in _system/reconvert_state.json, so a run that stops on its time budget continues next time. A file
that fails 3 times is left as it is and reported. If many downloads in a row fail, the source site is assumed down and
that site's files are skipped for the rest of the run (not counted as failures); other sites carry on.
"""

import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import KB_ROOT, SYSTEM_DIR  # noqa: E402
from convert import table_aware  # noqa: E402
from bot import download_pdf  # noqa: E402

STATE_FILE = SYSTEM_DIR / "reconvert_state.json"
BUDGET_MINUTES = float(os.environ.get("RECONVERT_BUDGET_MIN", "240"))
MAX_ATTEMPTS = 3
SITE_DOWN_AFTER = 8  # consecutive download failures from one site
SKIP_NAMES = {"README.md", "NEEDS_ATTENTION.md"}
TABLE_ROW = re.compile(r"^\|.*\|\s*$", re.M)


def split(text):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return yaml.safe_load(text[4:end]) or {}, text[end + 5:]


def join(front, body):
    return f"---\n{yaml.safe_dump(front, sort_keys=False, allow_unicode=True)}---\n\n{body}"


def candidates():
    top = sorted(p for p in KB_ROOT.glob("*.md") if p.name not in SKIP_NAMES)
    cea = sorted((KB_ROOT / "cea").rglob("*.md"))
    gna = sorted((KB_ROOT / "gna" / "documents").rglob("*.md"))
    for group, paths in (("cmets", top), ("cea", cea), ("gna", gna)):
        for p in paths:
            text = p.read_text(encoding="utf-8")
            front, body = split(text)
            if not front or front.get("converter") == table_aware.CONVERTER or not front.get("original_url"):
                continue
            if group == "gna" and TABLE_ROW.search(body):
                continue  # already converted with tables by the GNA table converter
            yield p, front


def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def main():
    state = load_state()
    start = time.time()
    done = failed = skipped = 0
    down_streak = {}  # site -> consecutive download failures
    tmp = ROOT / "_tmp_reconvert.pdf"
    for path, front in candidates():
        rel = path.relative_to(KB_ROOT).as_posix()
        entry = state.get(rel, {})
        if entry.get("attempts", 0) >= MAX_ATTEMPTS:
            skipped += 1
            continue
        if (time.time() - start) / 60 > BUDGET_MINUTES:
            print("Time budget used up; the next run continues.", flush=True)
            break
        host = urlparse(front["original_url"]).netloc
        if down_streak.get(host, 0) >= SITE_DOWN_AFTER:
            continue
        print(f"-> {rel}", flush=True)
        try:
            tmp.write_bytes(download_pdf(front["original_url"]))
        except Exception as exc:  # noqa: BLE001
            down_streak[host] = down_streak.get(host, 0) + 1
            print(f"   download failed ({down_streak[host]} in a row from {host}): {exc}", flush=True)
            if down_streak[host] >= SITE_DOWN_AFTER:
                print(f"{host} looks down; skipping its files for the rest of this run.", flush=True)
                continue
            if re.search(r"\b(502|503|504)\b|timed out|Connection", str(exc)):
                continue  # the site is unavailable, not the file: try again next run without counting an attempt
            entry.update(attempts=entry.get("attempts", 0) + 1, error=f"download: {str(exc)[:200]}")
            state[rel] = entry
            failed += 1
            continue
        down_streak[host] = 0
        try:
            md, meta = table_aware.convert(tmp)
        except Exception as exc:  # noqa: BLE001
            entry.update(attempts=entry.get("attempts", 0) + 1, error=f"convert: {str(exc)[:200]}")
            state[rel] = entry
            failed += 1
            print(f"   conversion failed: {exc}", flush=True)
            continue
        finally:
            tmp.unlink(missing_ok=True)
        front.update(meta)
        front["reconverted_date"] = date.today().isoformat()
        if meta["scanned_pages"]:
            front["needs_review"] = True
        path.write_text(join(front, md), encoding="utf-8")
        state[rel] = {"status": "done", "date": date.today().isoformat(), "pages": meta["pages"], "tables": meta["tables"]}
        done += 1
        print(f"   {meta['pages']} pages, {meta['tables']} tables, {len(meta['cloud_pages'])} for the cloud step", flush=True)
        if done % 20 == 0:
            save_state(state)
    save_state(state)
    remaining = sum(1 for p, _ in candidates()
                    if state.get(p.relative_to(KB_ROOT).as_posix(), {}).get("attempts", 0) < MAX_ATTEMPTS)
    print(f"\nRe-converted {done}, failed {failed}, given up {skipped}, still to do {remaining} "
          f"({(time.time() - start) / 60:.0f} min).", flush=True)


if __name__ == "__main__":
    main()
