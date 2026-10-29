"""Build one markdown card per substation, with a change log.

Sources (all from CTU's own pages, latest published version of each):
  Annex-C   capacity, margin and bay figures per substation
  Annex-B   bay-wise list of the applications connected / pending at each substation
  RTM/TBCB  monthly status of ISTS elements (new substations, lines, ICTs, bays...)

A card pulls together, for one substation:
  capacity granted / allocated, margin still available ("idle"), spare bays,
  commissioning date (COD) of the substation where CTU reports one,
  the applications connected there, the lines that name it, the assets at it,
  and the other substations those lines reach.

Elements are tied to substations by matching names in the element text, so
those sections are marked as text-matched. Each run diffs against last run's
state and appends differences to the card's change log; cards are rewritten
only when their content changes.
"""

import csv
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from adapters import ctu_gna  # noqa: E402
from application_cards import MENTIONS_END, MENTIONS_START, _existing_mentions, diff  # noqa: E402
from bot import download_pdf  # noqa: E402
from config import GNA_DIR, SYSTEM_DIR  # noqa: E402
from linking import compact_key, normalize_substation, normalize_text_for_substations, substation_text_pattern  # noqa: E402
from substation_tables import ANNEX_C_FIELDS, parse_annex_b, parse_annex_c, parse_rtm_tbcb  # noqa: E402

CARDS_DIR = GNA_DIR / "substations"
SNAPSHOT_DIR = GNA_DIR / "snapshots"
STATE_FILE = SYSTEM_DIR / "substations_state.json"
APPLICATIONS_STATE = SYSTEM_DIR / "applications_state.json"
TMP_DIR = ROOT / "_tmp_substations"

RENEWABLE_PAGE = "https://ctuil.in/renewable-energy"
RTM_PAGE = "https://ctuil.in/rtmtbcb"
REGIONS = ["NR", "WR", "ER", "SR", "NER"]

LABELS = {
    "cluster_potential_mw": "Cluster potential (MW)",
    "granted_solar_mw": "Connectivity granted - solar (MW)",
    "granted_nonsolar_mw": "Connectivity granted - non-solar (MW)",
    "bays_220kv_allocated": "220 kV bays allocated",
    "bays_400kv_allocated": "400 kV bays allocated",
    "margin_solar_mw": "Margin still available - solar (MW)",
    "margin_nonsolar_mw": "Margin still available - non-solar (MW)",
    "spare_bays_220kv": "Spare 220 kV bays",
    "spare_bays_400kv": "Spare 400 kV bays",
    "pipeline_solar_reg41_mw": "Pipeline (Reg 4.1) - solar (MW)",
    "pipeline_nonsolar_reg41_mw": "Pipeline (Reg 4.1) - non-solar (MW)",
    "pipeline_nonsolar_total_mw": "Pipeline - non-solar total (MW)",
}


def _slug(key):
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "unnamed"


def _fmt(v):
    if v is None:
        return ""
    return str(int(v)) if isinstance(v, float) and v == int(v) else str(v)


def _parse_date(s):
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", s or "")
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y += 2000 if y < 100 else 0
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# --------------------------------------------------------------------- fetching

def latest_sources():
    """URLs of the newest Annex-B, Annex-C and RTM/TBCB report per region."""
    renewable = ctu_gna.discover_pdfs(RENEWABLE_PAGE)
    annex = {}
    for u in renewable:
        name = u.rsplit("/", 1)[1].lower()
        for tag in ("annex-b", "annex-c"):
            if tag in name and tag not in annex:  # listing is newest-first
                annex[tag] = u
    missing = {"annex-b", "annex-c"} - set(annex)
    if missing:
        raise RuntimeError(f"bay-allocation page no longer lists {sorted(missing)} - layout may have changed")

    reports = {}
    for u in ctu_gna.discover_pdfs(RTM_PAGE):
        name = u.rsplit("/", 1)[1]
        kind = re.search(r"(RTM|TBCB)", name, re.I)
        region = re.search(r"(?<![A-Za-z])(NER|NR|ER|WR|SR)(?![A-Za-z])", name)
        if kind and region:
            reports.setdefault((kind.group(1).upper(), region.group(1)), u)
    expected = {(k, r) for k in ("RTM", "TBCB") for r in REGIONS}
    if expected - set(reports):
        raise RuntimeError(f"RTM/TBCB page no longer lists {sorted(expected - set(reports))}")
    return annex["annex-b"], annex["annex-c"], reports


def fetch_all():
    TMP_DIR.mkdir(exist_ok=True)
    b_url, c_url, reports = latest_sources()
    try:
        paths = {}
        for tag, url in (("annex_b", b_url), ("annex_c", c_url)):
            paths[tag] = TMP_DIR / f"{tag}.pdf"
            paths[tag].write_bytes(download_pdf(url))
        annex_c = parse_annex_c(paths["annex_c"])
        annex_b = parse_annex_b(paths["annex_b"])
        elements = []
        sources = {"annex_b": b_url, "annex_c": c_url}
        for (kind, region), url in sorted(reports.items()):
            p = TMP_DIR / f"{kind}_{region}.pdf"
            p.write_bytes(download_pdf(url))
            elements.extend(parse_rtm_tbcb(p, kind, region)["elements"])
            sources[f"{kind}_{region}"] = url
    finally:
        for f in TMP_DIR.glob("*.pdf"):
            f.unlink(missing_ok=True)
        try:
            TMP_DIR.rmdir()
        except OSError:
            pass
    if not annex_c["substations"] or not annex_b["applications"] or not elements:
        raise RuntimeError("a substation source parsed to zero rows - refusing to overwrite cards")
    return annex_c, annex_b, elements, sources


# --------------------------------------------------------------------- joining

def build_records(annex_c, annex_b, elements):
    """substation key -> record (fields for diffing + structured parts for rendering)."""
    recs = {}
    for s in annex_c["substations"]:
        key = normalize_substation(s["substation_raw"])[0]
        if key in recs:
            recs[key]["flags"].add("substation listed twice in Annex-C")
            continue
        recs[key] = {"raw": s["substation_raw"], "key": key, "region": s["region"], "state": s["state"],
                     "system_identified": s["system_identified"], "capacity": {f: s[f] for f in ANNEX_C_FIELDS},
                     "apps": [], "elements": [], "connected": {}, "flags": set()}
        if not s["system_identified"]:
            recs[key]["flags"].add("transmission system yet to be identified by CTU")

    by_compact = {compact_key(k): k for k in recs}
    for a in annex_b["applications"]:
        k = by_compact.get(compact_key(normalize_substation(a["substation_raw"])[0]))
        if k:
            recs[k]["apps"].append(a)

    patterns = {k: substation_text_pattern(k) for k in recs if k}
    for e in elements:
        # the element's own text only: a project title often lists every substation it touches
        text = normalize_text_for_substations(e["element"])
        hits = [k for k, p in patterns.items() if p.search(text)]
        e["substations"] = hits
        for k in hits:
            recs[k]["elements"].append(e)
        if e["element_type"] == "TL" and len(hits) > 1:
            for k in hits:
                for other in hits:
                    if other != k:
                        recs[k]["connected"].setdefault(other, []).append(e["element"])
    return recs


def _app_effective_dates():
    if not APPLICATIONS_STATE.exists():
        return {}
    state = json.loads(APPLICATIONS_STATE.read_text(encoding="utf-8"))["applications"]
    return {i: _parse_date(s["fields"].get("effectiveness.expected_effective_date", "")) for i, s in state.items()}


def element_summary(e):
    if e["status"] == "commissioned":
        return f"commissioned {', '.join(e['commissioned_on'])}; SCOD {e['scod']}"
    return f"{e['status'] or 'status not stated'}; SCOD {e['scod']}; anticipated {e['anticipated'] or 'not stated'}"


def cod_info(rec, app_dates):
    new_sub = [e for e in rec["elements"] if e["element_type"] == "SN"]
    if new_sub:
        e = new_sub[0]
        if e["status"] == "commissioned":
            return f"Commissioned {', '.join(e['commissioned_on'])} (CTU RTM/TBCB report as on {e['as_on']})", "reported"
        return f"Under construction; SCOD {e['scod']}, anticipated {e['anticipated'] or 'not stated'} (as on {e['as_on']})", "reported"
    dates = sorted(d for a in rec["apps"] for i in a["application_ids"] if (d := app_dates.get(i)))
    if dates:
        return (f"Not reported by CTU. Earliest connectivity start among applications here: {dates[0].strftime('%d-%m-%Y')} "
                "(a proxy, not the substation's own COD)"), "proxy"
    return "Not reported by CTU for this substation", "none"


def flatten(rec, app_dates):
    f = {"region": rec["region"], "state": rec["state"]}
    for name, v in rec["capacity"].items():
        f[f"capacity.{name}"] = _fmt(v)
    f["applications.count"] = str(len(rec["apps"]))
    f["applications.connectivity_total_mw"] = _fmt(round(sum(a["connectivity_mw"] or 0 for a in rec["apps"]), 2))
    for a in rec["apps"]:
        ids = ",".join(a["application_ids"]) or f"no-id {a['applicant'][:40]}"
        f[f"application {ids}"] = f"{a['status']}; {_fmt(a['connectivity_mw'])} MW; {a['applicant']}"
    seen = {}
    for e in rec["elements"]:
        base = f"element [{e['element_type']}] {e['element'][:90]}"
        seen[base] = seen.get(base, 0) + 1
        f[base if seen[base] == 1 else f"{base} #{seen[base]}"] = f"{e['kind']} {e['report_region']}: {element_summary(e)}"
    f["cod"] = cod_info(rec, app_dates)[0]
    return f


# --------------------------------------------------------------------- rendering

def render_card(rec, fields, meta, existing_mentions):
    app_ids = sorted({i for a in rec["apps"] for i in a["application_ids"]})
    fm = {
        "type": "gna_substation", "substation": rec["raw"], "substation_key": rec["key"],
        "region": rec["region"], "state": rec["state"], "system_identified": rec["system_identified"],
        "linked_application_ids": app_ids,
        "first_seen": meta["first_seen"], "last_changed": meta["last_changed"],
        "needs_review": bool(rec["flags"]),
    }
    cod_text, cod_kind = cod_info(rec, meta["app_dates"])
    L = [f"# Substation: {rec['raw']}", "", f"{rec['state']} ({rec['region']})", ""]
    if rec["flags"]:
        L += ["> Review: " + "; ".join(sorted(rec["flags"])), ""]
    L += ["## Capacity and margin", "", f"Source: CTU Annex-C dated {meta['annex_c_date']}. "
          "'Margin still available' is the capacity CTU shows as not yet allocated.", "",
          "| Item | Value |", "|---|---|"]
    L += [f"| {LABELS[k]} | {_fmt(rec['capacity'][k]) or '-'} |" for k in ANNEX_C_FIELDS]
    L += ["", "## Commissioning date (COD)", "", cod_text, ""]

    L += ["## Applications connected here", "", f"Source: CTU Annex-B dated {meta['annex_b_date']}. "
          f"{len(rec['apps'])} entries, {_fmt(round(sum(a['connectivity_mw'] or 0 for a in rec['apps']), 2))} MW in total.", ""]
    if rec["apps"]:
        L += ["| Application | Applicant | Status | Type | Voltage | Connectivity (MW) |", "|---|---|---|---|---|---|"]
        for a in rec["apps"]:
            links = ", ".join(f"[{i}](../applications/{i}.md)" for i in a["application_ids"]) or (a["application_no_raw"] or "-")
            L.append(f"| {links} | {a['applicant']} | {a['status']} | {a['project_type']} | {a['voltage']} | {_fmt(a['connectivity_mw']) or '-'} |")
    else:
        L.append("None listed.")

    lines = [e for e in rec["elements"] if e["element_type"] == "TL"]
    assets = [e for e in rec["elements"] if e["element_type"] != "TL"]
    L += ["", "## Transmission lines naming this substation (text-matched)", ""]
    if lines:
        L += ["| Line | Also names | Status |", "|---|---|---|"]
        for e in lines:
            others = ", ".join(sorted(s for s in e["substations"] if s != rec["key"])) or "-"
            L.append(f"| {e['element'].replace('|', '/')} | {others} | {element_summary(e)} |")
    else:
        L.append("None found in the RTM/TBCB status reports.")
    L += ["", "## Assets at this substation (text-matched)", ""]
    if assets:
        L += ["| Type | Asset | Project | Status |", "|---|---|---|---|"]
        for e in assets:
            L.append(f"| {e['element_type_label']} | {e['element'].replace('|', '/')} | {(e['project'] or '').replace('|', '/')[:90]} | {element_summary(e)} |")
    else:
        L.append("None found in the RTM/TBCB status reports.")
    L += ["", "## Substations reached through these lines", ""]
    if rec["connected"]:
        for other, names in sorted(rec["connected"].items()):
            L.append(f"- [{other}]({_slug(other)}.md) via {len(names)} line(s)")
    else:
        L.append("None identified.")

    L += ["", "## Change log", ""]
    if meta["changes"]:
        L += ["| Date | Field | Old | New |", "|---|---|---|---|"]
        for c in meta["changes"]:
            L.append(f"| {c['date']} | {c['field']} | {str(c['old']).replace('|', '/')[:160]} | {str(c['new']).replace('|', '/')[:160]} |")
    else:
        L.append(f"No changes since first seen on {meta['first_seen']}.")
    L += ["", "## Mentioned in", "", MENTIONS_START, existing_mentions or "_Not yet linked._", MENTIONS_END, ""]
    return f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n\n" + "\n".join(L)


def save_snapshots(annex_c, annex_b, elements, today):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    def dump(name, rows, cols):
        with open(SNAPSHOT_DIR / f"{today}_{name}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    dump("annex_c_substations", annex_c["substations"], ["substation_raw", "region", "state", *ANNEX_C_FIELDS])
    dump("annex_b_applications", annex_b["applications"],
         ["substation_raw", "region", "voltage", "application_no_raw", "applicant", "status", "project_type", "connectivity_mw",
          "margin_solar_per_bay_mw", "margin_nonsolar_per_bay_mw", "remarks"])
    dump("rtm_tbcb_elements", elements,
         ["kind", "report_region", "as_on", "project", "element", "element_type", "voltage", "mva", "scod", "status", "anticipated", "tsp"])


# --------------------------------------------------------------------- run

def run(today=None, limit_keys=None, reset=False):
    """reset=True ignores the saved state (use only to redo a first run whose matching was wrong)."""
    today = today or date.today().isoformat()
    annex_c, annex_b, elements, sources = fetch_all()
    save_snapshots(annex_c, annex_b, elements, today)

    recs = build_records(annex_c, annex_b, elements)
    app_dates = _app_effective_dates()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() and not reset else {"substations": {}}
    subs_state = state["substations"]

    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    written = changed = 0
    keys = sorted(recs) if limit_keys is None else [k for k in sorted(recs) if k in set(limit_keys)]
    for key in keys:
        rec = recs[key]
        fields = flatten(rec, app_dates)
        prev = subs_state.get(key)
        if prev is None:
            meta = {"first_seen": today, "last_changed": today, "changes": []}
        else:
            meta = {"first_seen": prev["first_seen"], "last_changed": prev["last_changed"], "changes": prev["changes"]}
            new = diff(prev["fields"], fields)
            if new:
                meta["changes"] = prev["changes"] + [{"date": today, **c} for c in new]
                meta["last_changed"] = today
                changed += 1
        meta.update(annex_c_date=annex_c["release_date"], annex_b_date=annex_b["release_date"], app_dates=app_dates)

        path = CARDS_DIR / f"{_slug(key)}.md"
        text = render_card(rec, fields, meta, _existing_mentions(path))
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            written += 1
        subs_state[key] = {"raw": rec["raw"], "first_seen": meta["first_seen"], "last_changed": meta["last_changed"],
                           "changes": meta["changes"], "fields": fields}

    for key in set(subs_state) - set(recs):  # dropped off Annex-C: log it once, keep the card
        if subs_state[key]["fields"]:
            subs_state[key]["changes"].append({"date": today, "field": "listing", "old": "listed in Annex-C", "new": "no longer listed"})
            subs_state[key]["fields"] = {}
            subs_state[key]["last_changed"] = today

    if limit_keys is None:
        state.update(last_run=today, sources=sources)
        SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"substations": len(keys), "cards_written": written, "substations_with_changes": changed,
            "annex_c_rows": len(annex_c["substations"]), "annex_b_rows": len(annex_b["applications"]), "elements": len(elements),
            "elements_matched_to_a_substation": sum(1 for e in elements if e["substations"])}


if __name__ == "__main__":
    print(run())
