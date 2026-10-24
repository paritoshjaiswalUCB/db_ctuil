"""Build one markdown card per application, with a change log.

Each run:
  1. scrapes the two structured sources (effectiveness list, GNA status),
  2. saves the raw rows as dated CSV snapshots,
  3. flattens each application's fields and compares them with the state
     saved last run, appending any differences to that application's change log,
  4. rewrites a card only when its content actually changed, so unchanged
     cards keep their file timestamp and aren't re-indexed every week.

"Mentioned in" links to CMETS / GNA documents are added separately by the
linking pass (link_documents.py), which edits only that section of a card.
"""

import csv
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from config import GNA_DIR, SYSTEM_DIR  # noqa: E402
from linking import normalize_applicant, normalize_substation  # noqa: E402
from scrape_tables import fetch_effectiveness_list, fetch_gna_status  # noqa: E402

CARDS_DIR = GNA_DIR / "applications"
SNAPSHOT_DIR = GNA_DIR / "snapshots"
STATE_FILE = SYSTEM_DIR / "applications_state.json"
INDEX_CSV = GNA_DIR / "applications_index.csv"

MENTIONS_START = "<!-- mentions:start -->"
MENTIONS_END = "<!-- mentions:end -->"

EFF_FIELDS = ["expected_effective_date", "substation", "region", "state",
              "generation_type", "installed_capacity_mw", "deemed_gna_mw"]


def _noid_key(row):
    basis = f"{normalize_applicant(row.get('applicant'))}|{row.get('substation','')}|{row.get('installed_capacity_mw','')}"
    return "NOID-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


def save_snapshots(eff_rows, gna_rows, today):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_DIR / f"{today}_connectivity_effective_list.csv", "w", newline="", encoding="utf-8-sig") as f:
        cols = ["application_no_raw", "applicant"] + EFF_FIELDS
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(eff_rows)
    with open(SNAPSHOT_DIR / f"{today}_gna_application_status.csv", "w", newline="", encoding="utf-8-sig") as f:
        extra = sorted({k for r in gna_rows for k in r["fields"]})
        cols = ["sub_report", "application_no_raw", "applicant", "application_date", "status"] + extra
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in gna_rows:
            w.writerow({**r, **r["fields"]})


def flatten(eff_rows, gna_rows):
    """application id -> {field: value} for this run, plus per-id applicant names."""
    apps = {}

    def entry(app_id):
        return apps.setdefault(app_id, {"applicants": set(), "fields": {}, "eff": [], "flags": set(), "co_ids": set()})

    for r in eff_rows:
        ids = r["application_ids"] or [_noid_key(r)]
        for app_id in ids:
            e = entry(app_id)
            e["applicants"].add(r["applicant"])
            e["eff"].append(r)
            e["co_ids"].update(i for i in ids if i != app_id)
            if app_id.startswith("NOID-"):
                e["flags"].add("no application number published")

    for r in gna_rows:
        ids = r["application_ids"] or [_noid_key(r)]
        for app_id in ids:
            e = entry(app_id)
            e["applicants"].add(r["applicant"])
            e["co_ids"].update(i for i in ids if i != app_id)
            prefix = f"gna_status.{r['sub_report']}"
            e["fields"][f"{prefix}.status"] = r["status"] or "(blank)"
            e["fields"][f"{prefix}.application_date"] = r["application_date"]
            for k, v in r["fields"].items():
                e["fields"][f"{prefix}.{k}"] = v

    for app_id, e in apps.items():
        if e["eff"]:
            for fld in EFF_FIELDS:
                vals = sorted({(x.get(fld) or "").strip() for x in e["eff"]} - {""})
                e["fields"][f"effectiveness.{fld}"] = " / ".join(vals)
            if len(e["eff"]) > 1:
                e["fields"]["effectiveness.entries"] = str(len(e["eff"]))
    return apps


def diff(old, new):
    changes = []
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k), new.get(k)
        if o != n:
            changes.append({"field": k, "old": o if o is not None else "(not listed)", "new": n if n is not None else "(no longer listed)"})
    return changes


def render_card(app_id, rec, existing_mentions):
    f = rec["fields"]
    subs = sorted({s for s in f.get("effectiveness.substation", "").split(" / ") if s}
                  | {v for k, v in f.items() if ("Injection point" in k or "Interconnection point" in k) and v and v != "-"})
    fm = {
        "type": "gna_application",
        "application_id": app_id,
        "applicant": rec["applicant"],
        "applicant_key": normalize_applicant(rec["applicant"]),
        "also_listed_with_ids": sorted(rec["co_ids"]),
        "substations": subs,
        "substation_keys": sorted({normalize_substation(s)[0] for s in subs}),
        "first_seen": rec["first_seen"],
        "last_changed": rec["last_changed"],
        "needs_review": bool(rec["flags"]),
    }
    lines = [f"# Application {app_id} — {rec['applicant']}", ""]
    if rec["flags"]:
        lines += ["> Review: " + "; ".join(sorted(rec["flags"])), ""]
    lines += ["## Current details", "", "| Source / field | Value |", "|---|---|"]
    for k in sorted(f):
        lines.append(f"| {k} | {str(f[k]).replace('|', '/')} |")
    lines += ["", "## Change log", ""]
    if rec["changes"]:
        lines += ["| Date | Field | Old | New |", "|---|---|---|---|"]
        for c in rec["changes"]:
            lines.append(f"| {c['date']} | {c['field']} | {str(c['old']).replace('|', '/')} | {str(c['new']).replace('|', '/')} |")
    else:
        lines.append(f"No changes since first seen on {rec['first_seen']}.")
    lines += ["", "## Mentioned in", "", MENTIONS_START, existing_mentions or "_Not yet linked._", MENTIONS_END, ""]
    return f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n\n" + "\n".join(lines)


def _existing_mentions(path):
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if MENTIONS_START in text and MENTIONS_END in text:
        return text.split(MENTIONS_START, 1)[1].split(MENTIONS_END, 1)[0].strip()
    return None


def card_filename(app_id):
    return f"{app_id}.md"


def run(today=None, limit_ids=None):
    today = today or date.today().isoformat()
    eff_rows = fetch_effectiveness_list()
    gna_rows = fetch_gna_status()
    save_snapshots(eff_rows, gna_rows, today)

    current = flatten(eff_rows, gna_rows)
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"applications": {}}
    apps_state = state["applications"]

    # applications that dropped off every source still get a "no longer listed" entry
    for app_id in set(apps_state) - set(current):
        current[app_id] = {"applicants": {apps_state[app_id]["applicant"]}, "fields": {}, "flags": set(), "co_ids": set(apps_state[app_id].get("co_ids", []))}

    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    written = changed_apps = 0
    ids = sorted(current) if limit_ids is None else [i for i in sorted(current) if i in set(limit_ids)]
    for app_id in ids:
        cur = current[app_id]
        prev = apps_state.get(app_id)
        applicant = sorted(cur["applicants"], key=len)[-1] if cur["applicants"] else (prev or {}).get("applicant", "")
        if prev is None:
            rec = {"first_seen": today, "last_changed": today, "changes": []}
        else:
            rec = {"first_seen": prev["first_seen"], "last_changed": prev["last_changed"], "changes": prev["changes"]}
            new_changes = diff(prev["fields"], cur["fields"])
            if new_changes:
                rec["changes"] = prev["changes"] + [{"date": today, **c} for c in new_changes]
                rec["last_changed"] = today
                changed_apps += 1
        rec.update({"applicant": applicant, "fields": cur["fields"], "flags": cur["flags"], "co_ids": cur["co_ids"]})

        path = CARDS_DIR / card_filename(app_id)
        text = render_card(app_id, rec, _existing_mentions(path))
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            written += 1
        apps_state[app_id] = {"applicant": applicant, "first_seen": rec["first_seen"], "last_changed": rec["last_changed"],
                              "changes": rec["changes"], "fields": cur["fields"], "co_ids": sorted(cur["co_ids"])}

    if limit_ids is None:
        state["last_run"] = today
        SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        write_index(apps_state)
    return {"applications": len(ids), "cards_written": written, "apps_with_changes": changed_apps,
            "effectiveness_rows": len(eff_rows), "gna_status_rows": len(gna_rows)}


def write_index(apps_state):
    with open(INDEX_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["application_id", "applicant", "substation", "expected_effective_date", "gna_status",
                    "first_seen", "last_changed", "changes_logged", "card"])
        for app_id in sorted(apps_state):
            s = apps_state[app_id]
            fl = s["fields"]
            status = "; ".join(f"{k.split('.')[1]}={v}" for k, v in sorted(fl.items()) if k.startswith("gna_status.") and k.endswith(".status"))
            w.writerow([app_id, s["applicant"], fl.get("effectiveness.substation", ""), fl.get("effectiveness.expected_effective_date", ""),
                        status, s["first_seen"], s["last_changed"], len(s["changes"]), f"applications/{card_filename(app_id)}"])


if __name__ == "__main__":
    print(run())
