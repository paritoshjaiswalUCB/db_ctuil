"""Run the CTU historical backfill to completion in one sitting.

Normally the weekly scheduled job advances the backfill cutoff by ~100 days
each Tuesday, deliberately pacing the historical pull over several weeks.
This script instead loops bot.py repeatedly (each call still advances the
cutoff the same way), stopping once two consecutive runs find zero newly
converted documents - i.e. we've reached the actual start of CTU's published
history (or the hard floor date, whichever comes first).

After each bot.py run, the (now-incremental) index build also runs, so the
RAG index stays in sync throughout rather than needing one giant rebuild at
the end.
"""

import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYEXE = r"C:\Users\admin\.conda\envs\pdf-tools\python.exe"
BOT_SCRIPT = BASE_DIR / "bot.py"
BUILD_INDEX_SCRIPT = BASE_DIR / "rag" / "build_index.py"
REPORT_FILE = BASE_DIR / "conversion_report.md"

MAX_ITERATIONS = 40  # safety cap - (2025-01-01 to 2015-01-01) / 100 days ~= 36


def run(cmd):
    result = subprocess.run([PYEXE, str(cmd)], cwd=BASE_DIR, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
    return result


def newly_converted_count():
    if not REPORT_FILE.exists():
        return None
    text = REPORT_FILE.read_text(encoding="utf-8")
    match = re.search(r"Newly converted successfully:\s*(\d+)", text)
    return int(match.group(1)) if match else None


def main():
    consecutive_zero_runs = 0

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n{'='*70}\nBackfill iteration {iteration}/{MAX_ITERATIONS}\n{'='*70}")

        run(BOT_SCRIPT)
        new_count = newly_converted_count()
        print(f"\n--- Iteration {iteration}: {new_count} newly converted ---")

        run(BUILD_INDEX_SCRIPT)

        if new_count == 0:
            consecutive_zero_runs += 1
            if consecutive_zero_runs >= 2:
                print(
                    f"\nTwo consecutive runs found nothing new - "
                    f"backfill appears complete after {iteration} iterations."
                )
                break
        else:
            consecutive_zero_runs = 0
    else:
        print(f"\nHit the {MAX_ITERATIONS}-iteration safety cap without two empty runs in a row.")

    print("\nDone. Check backfill_state.json for the final cutoff date reached.")


if __name__ == "__main__":
    main()
