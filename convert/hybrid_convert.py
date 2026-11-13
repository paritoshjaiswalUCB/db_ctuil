# Copied unchanged from order-search/tools/hybrid_convert.py (9_Tools, 2026-11-14) so the GitHub job can use it.
# If order-search improves its copy, carry the change over here.

"""Local page-by-page PDF -> markdown for digital (text-layer) PDFs, with per-page routing flags.

    from hybrid_convert import convert_pdf
    md, report = convert_pdf(path, pages=range(0, 10))      # 0-based page indices; markers are 1-based '<!-- page N -->'

Per page: text blocks are unwrapped into paragraphs, tables are found with PyMuPDF's find_tables() and written as markdown
tables (cells joined on one line, '-' kept, empty cells kept as empty so columns never shift), and text that sits inside a
table is NOT repeated as prose. Each page gets a route:
    LOCAL    clean text and/or well-formed tables
    GEMINI   complex page: borderless / ragged / wide tables, table running off the page bottom (continues on the next
             page), or a rotated page
    OCR      no usable text layer (scanned); handled with Tesseract and flagged
The caller decides what to do with the GEMINI pages (the pilot compares them against the cloud output).
"""
from __future__ import annotations

import re

import pymupdf

EMPTY_RATIO_COMPLEX = 0.30          # after clean-up; legitimate wide tables (e.g. monthly MoD stack) are 1-5% empty
WRAPPED_RATIO_COMPLEX = 0.15
BOTTOM_MARGIN_PT = 45


BIDI = re.compile("[​-‏‪-‮⁦-⁩﻿­]")      # invisible direction/format marks


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", BIDI.sub("", s or "")).strip()


NUMERIC_CELL = re.compile(r"^[\(\-]?[\d,]+(\.\d+)?%?\)?$")


def _compact(rows: list[list[str]]) -> list[list[str]]:
    """PyMuPDF returns spacer columns and split header rows for ruled financial tables. Clean up without changing content:
    (1) merge adjacent columns that are never both filled in the same row and where one is sparse (merged-cell artefact),
    (2) drop columns that are empty everywhere, (3) fold the header rows (rows above the first numeric row) into one."""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    changed = True
    while changed and len(rows[0]) > 1:
        changed = False
        n = len(rows[0])
        for j in range(n - 1):
            a = [r[j] for r in rows]
            b = [r[j + 1] for r in rows]
            fa, fb = sum(1 for x in a if x), sum(1 for x in b if x)
            if fa == 0 or fb == 0:
                continue
            if all(not (x and y) for x, y in zip(a, b)) and min(fa, fb) <= 0.5 * len(rows):
                for r in rows:
                    r[j] = (r[j] + " " + r[j + 1]).strip()
                    del r[j + 1]
                changed = True
                break
    keep = [j for j in range(len(rows[0])) if any(r[j] for r in rows)]
    rows = [[r[j] for j in keep] for r in rows]
    first_num = next((i for i, r in enumerate(rows) if sum(1 for c in r[1:] if NUMERIC_CELL.match(c.replace(" ", ""))) >= 1), 0)
    if first_num > 1:
        rows = [_fold_header(rows[:first_num])] + rows[first_num:]
    return rows


def _fold_header(head_rows: list[list[str]]) -> list[str]:
    """Fold a multi-row header into one row, 'Group - Sub' per column. A group label printed once above several columns
    (merged cell) is carried over to the columns to its right that have a sub-label in a lower header row."""
    n = len(head_rows[0])
    grid: list[list[str]] = []
    for r in head_rows:
        # a row whose filled cells all sit under filled cells of the row above and read like the continuation of a wrapped
        # label ("of power", "procured", "(Rs/kWh)") belongs to that label, not to a new header level
        filled = [j for j, c in enumerate(r) if c]
        # a continuation only fills columns the row above already fills; a sub-header row below a merged group label fills
        # more columns than the group row (the extra ones are covered by the merge), so it is kept as its own level
        if grid and filled and all(grid[-1][j] for j in filled):
            for j in filled:
                grid[-1][j] = (grid[-1][j] + " " + r[j]).strip()
        else:
            grid.append(list(r))
    for i in range(len(grid) - 1):                      # all rows except the last header row can hold group labels
        carry = ""
        for j in range(n):
            if j == 0:
                continue                                # first column = row-label column; never carried
            if grid[i][j]:
                carry = grid[i][j]
            elif carry and any(grid[k][j] for k in range(i + 1, len(grid))):
                grid[i][j] = carry
            elif not any(grid[k][j] for k in range(i + 1, len(grid))):
                carry = ""                              # no sub-label under this column: the group ended
    out = []
    for j in range(n):
        parts = []
        for i in range(len(grid)):
            c = grid[i][j]
            if c and (not parts or parts[-1] != c):
                parts.append(c)
        out.append(" - ".join(parts))
    return out


def table_quality(rows: list[list[str]]) -> dict:
    """Quality of a compacted table: share of empty cells and rows that look like wrapped continuations."""
    body = rows[1:] or rows
    cells = [c for r in body for c in r]
    empty = sum(1 for c in cells if not c) / max(len(cells), 1)
    wrapped = sum(1 for r in body if (not r[0] or r[0][:1].islower()) and any(r[1:])) / max(len(body), 1)
    return dict(cols=len(rows[0]), empty=empty, wrapped=wrapped)


def _md_table(rows: list[list[str | None]], quality: dict | None = None) -> str:
    rows = [[_clean(c) for c in r] for r in rows if any(_clean(c) for c in r)]
    if not rows:
        return ""
    rows = _compact(rows)
    if quality is not None:
        quality.update(table_quality(rows))
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _numeric_lines(page) -> int:
    n = 0
    for line in page.get_text("text").splitlines():
        if len(re.findall(r"\d[\d,]*\.?\d*", line)) >= 4:
            n += 1
    return n


def convert_page(page, ocr=None) -> tuple[str, dict]:
    info = dict(page=page.number + 1, chars=0, tables=0, route="LOCAL", reasons=[])
    text_all = page.get_text("text")
    info["chars"] = len(text_all.strip())
    if info["chars"] < 80 and page.get_images():
        # scanned page: the cloud model reads it far better than Tesseract (tables, digits); Tesseract text is kept as the
        # fallback if the cloud is unavailable. There is no text layer, so the number guard cannot be applied.
        info["route"] = "GEMINI"
        info["scanned"] = True
        info["reasons"].append("no text layer (scanned)")
        if ocr is None:                               # OCR is done lazily by the caller, only for pages the cloud cannot read
            return "", info
        text = ocr(page)
        text = "\n".join(re.sub(r"^\s*\|+\s*", "", ln) for ln in text.splitlines())   # stray table-border pipes
        return text, info
    bad = sum(1 for ch in text_all if "" <= ch <= "" or ch == "�") + 5 * text_all.count("(cid:")
    if info["chars"] and bad / info["chars"] > 0.05:      # legacy-font PDFs: text layer is gibberish
        info["garbled"] = True
        info["route"] = "GEMINI"
        info["reasons"].append("garbled font text layer")
        return _clean(text_all), info
    if page.rotation not in (0,) or page.rect.width > page.rect.height * 1.15:
        info["reasons"].append("landscape/rotated page")
    tables = []
    try:
        tables = list(page.find_tables().tables)
    except Exception as e:  # noqa: BLE001
        info["reasons"].append(f"table finder failed: {str(e)[:40]}")
    strategy = "lines"
    if not tables and _numeric_lines(page) >= 6:
        try:
            tables = [t for t in page.find_tables(strategy="text").tables if len(t.extract()) >= 3]
            strategy = "text"
            if tables:
                info["reasons"].append("borderless table (text strategy)")
        except Exception:  # noqa: BLE001
            pass
    items: list[tuple[float, float, str, str]] = []      # (y0, y1, kind 'table'|'text', markdown)
    boxes = []
    for t in tables:
        rows = t.extract()
        if not rows:
            continue
        q: dict = {}
        md = _md_table(rows, q)
        if q and q["cols"] > 3 and q["empty"] > EMPTY_RATIO_COMPLEX:
            info["reasons"].append(f"sparse table after clean-up ({q['empty']:.0%} empty cells)")
        if q and q["wrapped"] > WRAPPED_RATIO_COMPLEX:
            info["reasons"].append(f"wrapped row labels ({q['wrapped']:.0%} of rows)")
        if t.bbox[3] > page.rect.height - BOTTOM_MARGIN_PT:
            info["notes"] = info.get("notes", []) + ["table runs to the page bottom (may continue on next page)"]
        if md:
            items.append((t.bbox[1], t.bbox[3], "table", md))
            boxes.append(pymupdf.Rect(t.bbox))
            info["tables"] += 1
            if t.bbox[1] < 130 and "first_table_top" not in info:
                info["first_table_top"] = True
    for b in page.get_text("blocks", sort=True):
        x0, y0, x1, y1, txt = b[0], b[1], b[2], b[3], b[4]
        if b[6] != 0:           # image block
            continue
        r = pymupdf.Rect(x0, y0, x1, y1)
        if any(r.intersects(bx) and (r & bx).get_area() > 0.5 * r.get_area() for bx in boxes):
            continue            # text that belongs to a table
        para = _clean(txt)
        if para:
            items.append((y0, y1, "text", para))
    # Recovery: text blocks swallowed by a table box but not present in the table cells (rotated or merged-cell text, numbers
    # next to the grid) would silently drop numbers. Find them through the numbers missing from what we have so far and
    # append those blocks as plain text after the page content.
    have = _numbers("\n\n".join(m for _, _, _, m in sorted(items, key=lambda x: x[0])))
    truth = _numbers(text_all)
    missing = {k for k, c in truth.items() if have[k] < c}
    if missing and boxes:
        extra = []
        for b in page.get_text("blocks", sort=True):
            if b[6] != 0:
                continue
            r = pymupdf.Rect(b[0], b[1], b[2], b[3])
            if any(r.intersects(bx) and (r & bx).get_area() > 0.5 * r.get_area() for bx in boxes):
                para = _clean(b[4])
                if para and set(_numbers(para)) & missing:
                    extra.append((b[1], b[3], "text", para))
        if extra:
            info["notes"] = info.get("notes", []) + ["text recovered from table area"]
            items.append((page.rect.height + 1, page.rect.height + 2, "text", "<!-- text from the table area not placed in a table cell -->"))
            items.extend((page.rect.height + 3 + k, page.rect.height + 4 + k, "text", e[3]) for k, e in enumerate(extra))
    items.sort(key=lambda x: x[0])
    merged: list[tuple[float, float, str, str]] = []
    for it in items:                    # rejoin a table title that wrapped onto a second block ("... as approved by the" / "Commission")
        if (merged and it[2] == "text" and merged[-1][2] == "text" and re.match(r"(Table|Annexure)\s*\d", merged[-1][3], re.I)
                and not merged[-1][3].rstrip().endswith((".", ":")) and it[0] - merged[-1][1] < 8 and len(it[3]) < 90):
            merged[-1] = (merged[-1][0], it[1], "text", merged[-1][3] + " " + it[3])
        else:
            merged.append(it)
    md = "\n\n".join(m[3] for m in merged)
    if any(k in " ".join(info["reasons"]) for k in ("borderless", "sparse", "wrapped")):
        info["route"] = "GEMINI"
    return md, info


NUM_TOKEN = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def _numbers(text: str):
    from collections import Counter
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    toks = [t.replace(",", "") for t in NUM_TOKEN.findall(body)]
    return Counter(t for t in toks if len(t.replace(".", "")) >= 3 or "." in t)


def numeric_recall(pdf_page_text: str, md: str) -> float:
    """Share of the PDF text layer's numeric tokens that appear in the markdown for the same page (1.0 = none lost)."""
    truth = _numbers(pdf_page_text)
    total = sum(truth.values())
    if not total:
        return 1.0
    out = _numbers(md)
    return sum(min(c, out[k]) for k, c in truth.items()) / total


def choose_page(pdf_page_text: str, local_md: str, cloud_md: str | None) -> tuple[str, str, float]:
    """Number guard for routed pages: use the cloud text only if it keeps every number; otherwise fall back to the local
    text (whose recall is normally 100%). Returns (markdown, source 'cloud'|'local', recall of the chosen text)."""
    if cloud_md is None:
        return local_md, "local", numeric_recall(pdf_page_text, local_md)
    rc, rl = numeric_recall(pdf_page_text, cloud_md), numeric_recall(pdf_page_text, local_md)
    if rc >= 0.999 or rc >= rl:
        return cloud_md, "cloud", rc
    return local_md, "local", rl


def printed_page_numbers(doc) -> dict[int, int]:
    """{pdf page index (0-based): printed page number} for pages whose printed number differs from the PDF position.
    Uses PDF page labels when the file has them, else a lone number or 'Page N [of M]' in the top/bottom margin, accepted
    only when it is consistent with a stable offset over its neighbours (so stray numbers are not mistaken for page numbers)."""
    labels = {}
    for i in range(len(doc)):
        try:
            lab = doc[i].get_label()
        except Exception:  # noqa: BLE001
            lab = ""
        if lab.isdigit():
            labels[i] = int(lab)
    cands = dict(labels)
    if len(cands) < len(doc) * 0.5:
        cands = {}
        for i in range(len(doc)):
            p = doc[i]
            h = p.rect.height
            found = None
            for x0, y0, x1, y1, txt, *_ in p.get_text("blocks"):
                if y0 > h - 70 or y1 < 60:
                    t = _clean(txt)
                    m = re.fullmatch(r"[|\-–—\s]*(?:Page\s+)?(\d{1,4})(?:\s+of\s+\d{1,4})?[|\-–—\s]*", t, re.I)
                    if m:
                        found = int(m.group(1))
            if found is not None:
                cands[i] = found
    if not cands:
        return {}
    from collections import Counter
    offs = Counter(v - (i + 1) for i, v in cands.items())
    off, n = offs.most_common(1)[0]
    if n < 5 or n < 0.3 * len(cands):
        return {}
    # the printed number is only trusted where it agrees with the dominant offset (annual reports have front-matter pages
    # without numbers and sometimes several offsets; tariff orders have offset 0 and need no marker)
    return {i: v for i, v in cands.items() if v - (i + 1) == off and off != 0}


def image_only_pages(doc) -> list[int]:
    """1-based pages that are mainly images or charts (little text, large pictures): their content is NOT in the markdown."""
    out = []
    for i, p in enumerate(doc):
        chars = len(p.get_text("text").strip())
        area = 0.0
        for img in p.get_images(full=True):
            try:
                for r in p.get_image_rects(img[0]):
                    area += r.get_area()
            except Exception:  # noqa: BLE001
                pass
        if area > 0.35 * p.rect.get_area() and chars < 400:
            out.append(i + 1)
    return out


def _worker(args):
    path, idxs = args
    doc = pymupdf.open(path)
    out = [(i,) + convert_page(doc[i], None) for i in idxs]
    doc.close()
    return out


def convert_pages(doc, idxs, ocr=None, progress=None, path=None, workers=1):
    """-> list of dicts per page: {idx, marker, md, info}; md is the local text (before any cloud replacement).
    With path and workers > 1 the page conversions (CPU-bound, ~3 s/page) run in parallel processes."""
    printed = printed_page_numbers(doc)
    out = []
    prev_info = None
    idxs = list(idxs)
    pre = {}
    if path and workers > 1 and len(idxs) > 40 and ocr is None:
        from concurrent.futures import ProcessPoolExecutor
        size = max(10, len(idxs) // (workers * 6))
        slices = [idxs[k:k + size] for k in range(0, len(idxs), size)]
        done = 0
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(_worker, [(str(path), s) for s in slices]):
                for i, md, info in res:
                    pre[i] = (md, info)
                done += len(res)
                if progress:
                    progress(done, len(idxs))
    for k, i in enumerate(idxs):
        md, info = pre[i] if i in pre else convert_page(doc[i], ocr)
        info["printed"] = printed.get(i)
        if prev_info and info.get("first_table_top") and any("page bottom" in n for n in prev_info.get("notes", [])):
            md = f"<!-- table continues from page {i} -->\n\n{md}"
        marker = f"<!-- page {i + 1} | printed {printed[i]} -->" if i in printed else f"<!-- page {i + 1} -->"
        out.append(dict(idx=i, marker=marker, md=md, info=info))
        prev_info = info
        if progress and (k + 1) % 50 == 0:
            progress(k + 1, len(idxs))
    return out


def convert_pdf(path, pages=None, ocr=None):
    doc = pymupdf.open(path)
    idxs = list(pages) if pages is not None else range(len(doc))
    res = convert_pages(doc, idxs, ocr)
    doc.close()
    return "\n".join(f"{r['marker']}\n\n{r['md']}\n" for r in res), [r["info"] for r in res]
