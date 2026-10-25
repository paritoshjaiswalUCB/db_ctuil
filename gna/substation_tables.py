"""Parsers for the PDF tables behind the substation cards.

  Annex-C  substation-wise connectivity, margins, bays and pipeline
  Annex-B  bay-wise list of applications at each substation
  RTM/TBCB monthly status of ISTS project elements (substations, lines, ICTs...)

pdfplumber reads cells by position, so blank cells stay blank instead of
shifting later values into the wrong column (plain text extraction does that).
Merged cells (a substation name spanning several application rows, a project
name spanning its elements) arrive as one filled row followed by blanks, so
they're forward-filled.
"""

import re

import pdfplumber

from linking import extract_ids_from_table_cell

REGIONS = {"NR", "ER", "WR", "SR", "NER"}


def _clean(cell):
    return re.sub(r"\s+", " ", (cell or "").replace("\n", " ")).strip()


def _num(cell):
    s = _clean(cell).replace(",", "")
    if s in ("", "NA", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _repeated_artifacts(pages):
    """(text, x0, top) of words printed at the same spot on most pages.

    Some CTU exports carry a stray label (e.g. "Bhadla PS" at the same
    coordinates on every page of Annex-B) that lands inside whatever table
    cell sits beneath it. Real table text never repeats at identical
    coordinates across most pages, so these are safe to drop. Header rows do
    repeat, which is fine: dropping them only loses text the parsers re-find
    from the first page's header.
    """
    if len(pages) < 4:
        return set()
    seen = {}
    for page in pages:
        for w in {(w["text"], round(w["x0"]), round(w["top"])) for w in page.extract_words()}:
            seen[w] = seen.get(w, 0) + 1
    # region codes and numbers can legitimately sit at the same spot on many pages
    return {w for w, n in seen.items()
            if n >= len(pages) * 0.6 and w[0] not in REGIONS and not re.search(r"\d", w[0])}


def _clean_pages(pdf):
    """Pages with doubled characters and repeated stray labels removed."""
    # text drawn twice for a bold effect; the default 1pt tolerance also eats the
    # second letter of a narrow double ("ll" in Mills), so keep it tight
    pages = [p.dedupe_chars(tolerance=0.3) for p in pdf.pages]
    artifacts = _repeated_artifacts(pages)
    # Where the stray label overlaps real text, the page's words come out merged
    # ("BBhhacahdalau"), so word matching misses it. Match by character instead:
    # same glyph at the same coordinates as the label seen cleanly on other pages.
    stray_chars = set()
    for page in pages:
        for w in page.extract_words(return_chars=True):
            if (w["text"], round(w["x0"]), round(w["top"])) in artifacts:
                for c in w["chars"]:  # the label's own glyphs only, not neighbouring text
                    stray_chars.add((c["text"], round(c["x0"], 1), round(c["top"], 1)))
    cleaned = []
    for i, page in enumerate(pages):
        if stray_chars and i > 0:
            page = page.filter(lambda o: not (o.get("object_type") == "char"
                                              and (o["text"], round(o["x0"], 1), round(o["top"], 1)) in stray_chars))
        cleaned.append(page)
    return cleaned


def _rows(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:1])
        rows = []
        for page in _clean_pages(pdf):
            for table in page.extract_tables():
                rows.extend(table)
    return rows, text


def _md_cell(cell):
    return _clean(cell).replace("|", "\\|")


def pdf_to_markdown_tables(pdf_path):
    """Markdown with real tables for table-heavy PDFs, or None if no tables found.

    Text outside tables is kept in reading order around them. A table whose
    column count matches the previous page's last table is treated as a
    continuation, so a list spanning 30 pages becomes one table with one header.
    """
    parts, found_any = [], False
    open_table_cols = None  # column count of the table still "open" from the previous page
    with pdfplumber.open(pdf_path) as pdf:
        for page in _clean_pages(pdf):
            tables = page.find_tables()
            boxes = [t.bbox for t in tables]
            outside = page.filter(lambda o: not (o.get("object_type") == "char" and any(
                b[0] <= o["x0"] and o["x1"] <= b[2] and b[1] <= o["top"] and o["bottom"] <= b[3] for b in boxes)))
            blocks = [(t.bbox[1], "table", t.extract()) for t in tables]
            for line in (outside.extract_text_lines() if hasattr(outside, "extract_text_lines") else []):
                blocks.append((line["top"], "text", line["text"]))
            for _, kind, content in sorted(blocks, key=lambda b: b[0]):
                if kind == "text":
                    if re.fullmatch(r"\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*", content, re.I):
                        continue  # page number - don't let it split a continuing table
                    if content.strip():
                        parts.append(content.strip())
                        open_table_cols = None
                    continue
                rows = [[_md_cell(c) for c in r] for r in content if any(_clean(c) for c in r)]
                if not rows:
                    continue
                found_any = True
                ncols = max(len(r) for r in rows)
                rows = [r + [""] * (ncols - len(r)) for r in rows]
                if open_table_cols == ncols:
                    parts[-1] += "\n" + "\n".join("| " + " | ".join(r) + " |" for r in rows)
                else:
                    header, body = rows[0], rows[1:]
                    md = ["| " + " | ".join(header) + " |", "|" + "---|" * ncols]
                    md += ["| " + " | ".join(r) + " |" for r in body]
                    parts.append("\n".join(md))
                open_table_cols = ncols
    return "\n\n".join(parts) if found_any else None


def normalize_voltage(cell):
    """"220 kV" from cells like "220 KV", "222200 KkVV" (doubled) or "220 220 kV"."""
    s = _clean(cell)
    m = re.search(r"(?<!\d)(\d{3})(?!\d)", s)
    if m:
        return f"{m.group(1)} kV"
    m = re.match(r"(\d)\1(\d)\2(\d)\3", s)  # every character printed twice
    return f"{m.group(1)}{m.group(2)}{m.group(3)} kV" if m else s


def release_date(text):
    m = re.search(r"Date\s*:\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})", text)
    return m.group(1).replace("/", ".").replace("-", ".") if m else ""


# --------------------------------------------------------------------- Annex-C

ANNEX_C_FIELDS = [
    "cluster_potential_mw", "granted_solar_mw", "granted_nonsolar_mw",
    "bays_220kv_allocated", "bays_400kv_allocated", "margin_solar_mw", "margin_nonsolar_mw",
    "spare_bays_220kv", "spare_bays_400kv", "pipeline_solar_reg41_mw", "pipeline_nonsolar_reg41_mw",
    "pipeline_nonsolar_total_mw",
]


def parse_annex_c(pdf_path):
    rows, text = _rows(pdf_path)
    substations, subtotals = [], []
    potential, current_state = None, None
    for r in rows:
        r = [_clean(c) for c in r] + [""] * (15 - len(r))
        label = r[0]
        if label.lower().startswith("subtotal") or label.lower() == "total":
            subtotals.append({"label": label, **{f: _num(v) for f, v in zip(ANNEX_C_FIELDS, r[3:15])}})
            continue
        if r[1] not in REGIONS or not label:
            continue
        state = r[2]
        if state != current_state:
            potential, current_state = None, state
        if _num(r[3]) is not None:
            potential = _num(r[3])
        rec = {"substation_raw": label, "region": r[1], "state": state,
               "system_identified": "yet to be identified" not in label.lower()}
        rec.update({f: _num(v) for f, v in zip(ANNEX_C_FIELDS, r[3:15])})
        rec["cluster_potential_mw"] = potential
        substations.append(rec)
    return {"release_date": release_date(text), "substations": substations, "subtotals": subtotals}


def validate_annex_c(parsed):
    """Compare parsed substation sums with CTU's printed state subtotals.

    Returns a list of (subtotal label, field, parsed sum, printed value) mismatches.
    The cluster potential is a merged cell repeated per cluster, so it's skipped.
    """
    issues = []
    subs = parsed["substations"]
    for st in parsed["subtotals"]:
        m = re.match(r"subtotal\s*\((.+)\)", st["label"], re.I)
        if not m:
            continue
        key = m.group(1).strip().lower()
        group = [s for s in subs if s["state"].lower() == key or s["region"].lower() == key]
        if not group:
            issues.append((st["label"], "*", None, "no matching substations"))
            continue
        for f in ANNEX_C_FIELDS[1:]:
            printed = st.get(f)
            if printed is None:
                continue
            parsed_sum = sum(s[f] or 0 for s in group)
            if abs(parsed_sum - printed) > max(1.0, abs(printed) * 0.005):
                issues.append((st["label"], f, round(parsed_sum, 2), printed))
    return issues


# --------------------------------------------------------------------- Annex-B

def parse_annex_b(pdf_path):
    rows, text = _rows(pdf_path)
    header_idx = None
    apps = []
    substation, voltage = None, None
    for r in rows:
        cells = [_clean(c) for c in r]
        if "Application ID" in cells and "Substation" in cells:
            names = cells
            header_idx = {
                "substation": names.index("Substation"),
                "region": names.index("Region"),
                "app": names.index("Application ID"),
                "applicant": next(i for i, n in enumerate(names) if n.startswith("Name of Applicant")),
                "status": next(i for i, n in enumerate(names) if n.startswith("Application Status")),
                "type": next(i for i, n in enumerate(names) if n.startswith("Project Type")),
                "voltage": next(i for i, n in enumerate(names) if n.startswith("Voltage level")),
                "connectivity": next(i for i, n in enumerate(names) if n.startswith("Connectivity (MW)")),
            }
            margin_cols = [i for i, n in enumerate(names) if n.startswith("Margins for")]
            header_idx["margin_solar"], header_idx["margin_nonsolar"] = (margin_cols + [None, None])[:2]
            header_idx["remarks"] = names.index("Remarks") if "Remarks" in names else None
            continue
        if header_idx is None or len(cells) <= header_idx["app"]:
            continue
        if cells[header_idx["region"]] not in REGIONS:
            continue
        if cells[header_idx["substation"]]:
            substation = cells[header_idx["substation"]]
        if cells[header_idx["voltage"]]:
            voltage = normalize_voltage(cells[header_idx["voltage"]])
        raw_ids = cells[header_idx["app"]]
        get = lambda k: cells[header_idx[k]] if header_idx.get(k) is not None and header_idx[k] < len(cells) else ""
        apps.append({
            "substation_raw": substation, "region": cells[header_idx["region"]], "voltage": voltage,
            "application_no_raw": raw_ids, "application_ids": extract_ids_from_table_cell(raw_ids),
            "applicant": get("applicant"), "status": get("status"), "project_type": get("type"),
            "connectivity_mw": _num(get("connectivity")),
            "margin_solar_per_bay_mw": _num(get("margin_solar")), "margin_nonsolar_per_bay_mw": _num(get("margin_nonsolar")),
            "remarks": get("remarks"),
        })
    return {"release_date": release_date(text), "applications": apps}


# --------------------------------------------------------------------- RTM / TBCB

ELEMENT_TYPES = {
    "SN": "new substation", "SA": "substation augmentation (ICT/reactor)", "TL": "transmission line",
    "BE": "bay / equipment", "BAY EXT.": "bay extension", "REC": "reconductoring", "STAT": "STATCOM",
}


def parse_commissioning(cell):
    s = _clean(cell)
    charged = re.findall(r"[Cc]harged(?: on)?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", s)
    if charged:
        return {"status": "commissioned", "dates": charged, "anticipated": ""}
    if s.lower().startswith("completed") or s.lower() == "charged":
        return {"status": "completed", "dates": [], "anticipated": ""}
    return {"status": "under construction" if s else "", "dates": [], "anticipated": s}


def parse_rtm_tbcb(pdf_path, kind, region):
    rows, text = _rows(pdf_path)
    as_on = ""
    m = re.search(r"As on\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})", text)
    if m:
        as_on = m.group(1)
    col = None
    elements, project_no, project_name = [], None, None
    for r in rows:
        cells = [_clean(c) for c in r]
        joined = " ".join(cells)
        if "Element" in joined and "SCOD" in joined:
            col = {}
            for i, n in enumerate(cells):
                nl = n.lower()
                if nl.startswith("name of the transmission"): col["name"] = i
                elif nl.startswith("element type"): col["type"] = i
                elif nl.startswith("voltage"): col["voltage"] = i
                elif nl == "mva": col["mva"] = i
                elif nl == "scod": col["scod"] = i
                elif nl.startswith("anticipated"): col["anticipated"] = i
                elif nl.startswith("name of tsp"): col["tsp"] = i
                elif nl == "region": col["region"] = i
                elif nl.startswith("remarks"): col["remarks"] = i
            continue
        if col is None or "name" not in col or len(cells) <= col["name"]:
            continue
        get = lambda k: cells[col[k]] if k in col and col[k] < len(cells) else ""
        name = get("name")
        if not name:
            continue
        sno = cells[0]
        etype = get("type").upper()
        if sno and not etype:
            project_no, project_name = sno, name
            continue
        comm = parse_commissioning(get("anticipated"))
        elements.append({
            "kind": kind, "report_region": region, "as_on": as_on,
            "project_no": project_no, "project": project_name, "element": name,
            "element_type": etype, "element_type_label": ELEMENT_TYPES.get(etype, etype.lower()),
            "voltage": get("voltage"), "mva": get("mva"), "scod": get("scod"),
            "status": comm["status"], "commissioned_on": comm["dates"], "anticipated": comm["anticipated"],
            "tsp": get("tsp"), "region": get("region"), "remarks": get("remarks"),
        })
    return {"as_on": as_on, "elements": elements}
