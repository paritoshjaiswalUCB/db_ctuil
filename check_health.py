"""Health check for the weekly regulatory-kb-bot run.

Reads the just-written conversion_report.md, appends this run's numbers to a
rolling history file, and decides whether anything looks wrong:

  1. Any conversion failures in this run (a PDF was found but couldn't be
     downloaded/converted) - alerts immediately, every time it happens.
  2. Zero newly-converted documents for several consecutive runs in a row
     (default: 3) - on its own this can be normal (new minutes are
     infrequent), but a long enough streak of zero new OR zero discovered
     is also what you'd see if CTU changed their site and the scraper quietly
     broke. Flagged as a "worth a look" signal, not a hard failure.

When something looks wrong, writes NEEDS_ATTENTION.md (both here and in the
Drive output folder, so it's visible wherever you look) and prints an ALERT
line that weekly_update.ps1 checks to decide whether to pop a toast
notification. When everything's healthy, removes any stale alert file left
over from a previous run so old alerts don't linger after the problem clears.
"""

import json
import re
from datetime import datetime
from config import BASE_DIR, KB_ROOT
REPORT_FILE = BASE_DIR / "conversion_report.md"
HEALTH_FILE = BASE_DIR / "run_health.json"
ALERT_FILE_LOCAL = BASE_DIR / "NEEDS_ATTENTION.md"
ALERT_FILE_DRIVE = KB_ROOT / "NEEDS_ATTENTION.md"

ZERO_NEW_STREAK_THRESHOLD = 3  # consecutive runs with 0 newly converted before flagging
MAX_HISTORY = 20


def parse_report():
    text = REPORT_FILE.read_text(encoding="utf-8")

    def grab(label):
        match = re.search(rf"{label}:\s*(\d+)", text)
        return int(match.group(1)) if match else None

    return {
        "date": datetime.now().isoformat(timespec="seconds"),
        "discovered": grab("Total PDFs discovered"),
        "skipped": grab(r"Already converted \(skipped\)"),
        "succeeded": grab("Newly converted successfully"),
        "failed": grab("Failed"),
    }


def load_history():
    if HEALTH_FILE.exists():
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    return []


def save_history(history):
    HEALTH_FILE.write_text(json.dumps(history[-MAX_HISTORY:], indent=2), encoding="utf-8")


def write_alert(message):
    content = f"# Needs attention\n\nGenerated: {datetime.now().isoformat(timespec='seconds')}\n\n{message}\n"
    ALERT_FILE_LOCAL.write_text(content, encoding="utf-8")
    try:
        ALERT_FILE_DRIVE.write_text(content, encoding="utf-8")
    except Exception as exc:  # Drive might be unavailable - local copy still exists
        print(f"(could not write alert to Drive folder: {exc})")


def clear_alert():
    for f in (ALERT_FILE_LOCAL, ALERT_FILE_DRIVE):
        try:
            if f.exists():
                f.unlink()
        except Exception:
            pass


def main():
    this_run = parse_report()
    history = load_history()
    history.append(this_run)
    save_history(history)

    problems = []

    if this_run["failed"]:
        problems.append(
            f"{this_run['failed']} PDF(s) failed to convert in this run. "
            f"Check conversion.log / conversion_report.md for the error details."
        )

    recent = history[-ZERO_NEW_STREAK_THRESHOLD:]
    if len(recent) == ZERO_NEW_STREAK_THRESHOLD and all(r["succeeded"] == 0 for r in recent):
        problems.append(
            f"No newly converted documents for the last {ZERO_NEW_STREAK_THRESHOLD} runs in a row "
            f"(back to {recent[0]['date']}). This can be normal (new minutes are infrequent), "
            f"but it's also what you'd see if CTU changed their site and discovery quietly broke - "
            f"worth a quick manual check of ctuil.in/ists-consultation-meeting."
        )

    if problems:
        write_alert("\n\n".join(f"- {p}" for p in problems))
        print("ALERT: " + " | ".join(problems))
    else:
        clear_alert()
        print(
            f"OK: discovered={this_run['discovered']}, succeeded={this_run['succeeded']}, "
            f"failed={this_run['failed']}"
        )


if __name__ == "__main__":
    main()
