"""Head-to-head test of PDF readers on known hard pages, scored with the strict no-loss check of convert/cloud_pass.py.

Readers: local (the KB's own hybrid converter; Tesseract on scanned pages), gemini (as in the cloud step), docling, marker.
Each reader runs in its own GitHub job (they need different packages) and writes one markdown file per page:

    python -m convert.engine_test read local      -> convert/engine_out/local/<sample>_p<N>.md   (also gemini)
    python -m convert.engine_test read docling
    python -m convert.engine_test read marker
    python -m convert.engine_test score           -> convert/engine_out/report.md

Scoring. Typed pages: the PDF's own text layer is the truth; for every reader we count how many of its numbers and words
(3+ letters, running header/footer left out) are missing, and how many numbers the reader has that the PDF does not.
A page "passes" only if nothing is missing. Scanned pages have no truth to compare with, so every pair of readers is
compared on figures, and disagreements are listed for checking against the page image.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "convert" / "engine_out"
PDFS = OUT / "pdf"
SAMPLES = {
    "sr47": ("https://ctuil.in/uploads/ists_consultation_meeting/minutes/17846341613447th CMETs -SR MoM.pdf",
             list(range(37, 46))),
    "wr46": ("https://ctuil.in/uploads/ists_consultation_meeting/minutes/179298757736MoM 46th CMETS-WR.pdf",
             list(range(69, 79))),
    "nr47": ("https://ctuil.in/uploads/ists_consultation_meeting/minutes/178455837823Minutes or 47th CMETS NR meeting "
             "held on 20-05-2026 & 24-06-2026 .pdf", [23]),
    "sr42": ("https://ctuil.in/uploads/ists_consultation_meeting/minutes/177199679842Minutes of meeting for 42nd CMETS-SR.pdf",
             list(range(137, 144))),
    "npc18": ("https://cea.nic.in/api/public/link-report/file?path=/home/cea_website/reports/general/en/Minutes%20of%20the"
              "%2018th%20Meeting%20of%20NPC%20held%20on%2018_09_2026%20at%20Rajgir%2C%20Bihar_1792578801113.pdf&mode=view",
              list(range(230, 239))),
}
ONLY = [s for s in os.environ.get("ENGINE_SAMPLES", "").split(",") if s] or list(SAMPLES)


def full_pdf(name):
    """Download once; -> path of the whole PDF."""
    from bot import download_pdf
    PDFS.mkdir(parents=True, exist_ok=True)
    path = PDFS / f"{name}.pdf"
    if not path.exists():
        path.write_bytes(download_pdf(SAMPLES[name][0]))
    return path


def subset_pdf(name):
    """-> path of a PDF holding only the test pages, in order (readers that work on whole files get only these)."""
    import pymupdf
    path = PDFS / f"{name}_subset.pdf"
    if not path.exists():
        doc, out = pymupdf.open(str(full_pdf(name))), pymupdf.open()
        for p in SAMPLES[name][1]:
            out.insert_pdf(doc, from_page=p - 1, to_page=p - 1)
        out.save(str(path))
    return path


def save(engine, name, page, md):
    d = OUT / engine
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}_p{page}.md").write_text(md or "", encoding="utf-8")


def timing(engine, seconds, pages):
    f = OUT / engine / "_timing.json"
    t = json.loads(f.read_text()) if f.exists() else {}
    t["seconds"] = t.get("seconds", 0) + seconds
    t["pages"] = t.get("pages", 0) + pages
    f.write_text(json.dumps(t))


# ---------------------------------------------------------------- readers
def read_local():
    import pymupdf
    from convert import cloud_pass as cp
    from convert import hybrid_convert as hc
    from convert import table_aware
    cp.used_today = lambda: 0                 # the test's few requests are not counted in the KB's usage file
    cp.count_request = lambda: None
    gem = cp.Gemini() if os.environ.get("GEMINI_API_KEY") else None
    for name in ONLY:
        doc, pages = pymupdf.open(str(full_pdf(name))), SAMPLES[name][1]
        t0 = time.time()
        local = {r["idx"] + 1: r["md"] for r in hc.convert_pages(doc, [p - 1 for p in pages])}
        for p in pages:
            if cp._is_scanned(doc[p - 1]):
                local[p] = table_aware._ocr(doc[p - 1])
            save("local", name, p, local.get(p, ""))
        timing("local", time.time() - t0, len(pages))
        if gem:
            t0 = time.time()
            got = cp.read_cloud(gem, doc, pages)
            for p in pages:
                save("gemini", name, p, got.get(p, ""))
            timing("gemini", time.time() - t0, len(pages))


def read_docling():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption
    opts = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.table_structure_options.do_cell_matching = True
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    for name in ONLY:
        pages = SAMPLES[name][1]
        t0 = time.time()
        d = conv.convert(str(subset_pdf(name))).document
        for k, p in enumerate(pages, 1):
            save("docling", name, p, d.export_to_markdown(page_no=k))
        timing("docling", time.time() - t0, len(pages))


def read_marker():
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered
    conv = PdfConverter(artifact_dict=create_model_dict(), config={"paginate_output": True})
    for name in ONLY:
        pages = SAMPLES[name][1]
        t0 = time.time()
        text = text_from_rendered(conv(str(subset_pdf(name))))[0]
        parts = re.split(r"\n*\{(\d+)\}-{48}\n*", "\n\n" + text)
        found = {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
        for k, p in enumerate(pages):
            save("marker", name, p, found.get(k, ""))
        timing("marker", time.time() - t0, len(pages))


# ---------------------------------------------------------------- scoring
def clean(md):
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)        # image links (marker/docling) carry file names, not content
    return md.replace("\\_", "_")


def score():
    import pymupdf
    from convert import cloud_pass as cp
    engines = [e for e in ("local", "gemini", "docling", "marker") if (OUT / e).is_dir()]
    get = lambda e, n, p: clean((OUT / e / f"{n}_p{p}.md").read_text(encoding="utf-8")) if (OUT / e / f"{n}_p{p}.md").exists() else None
    tot = {e: {"pages": 0, "pass": 0, "num_missing": 0, "word_missing": 0, "num_extra": 0, "table_rows": 0, "absent": 0}
           for e in engines}
    detail, scans = [], []
    for name in ONLY:
        doc = pymupdf.open(str(full_pdf(name)))
        running = cp.running_numbers(doc), cp.running_words(doc)
        for p in SAMPLES[name][1]:
            page = doc[p - 1]
            if cp._is_scanned(page):
                reads = {e: get(e, name, p) for e in engines if get(e, name, p)}
                nums = {e: cp.guard_numbers(md) for e, md in reads.items()}
                row = [f"### {name} p{p} (scanned): figures read " +
                       ", ".join(f"{e} {sum(n.values())}" for e, n in nums.items())]
                es = list(reads)
                for i, a in enumerate(es):
                    for b in es[i + 1:]:
                        cov, sup = cp.scan_agreement(reads[a], reads[b])
                        only_a = {k: v - nums[b][k] for k, v in nums[a].items() if nums[b][k] < v}
                        only_b = {k: v - nums[a][k] for k, v in nums[b].items() if nums[a][k] < v}
                        row.append(f"- {a} vs {b}: {a}'s figures found in {b} {cov:.2f}, {b}'s in {a} {sup:.2f}; "
                                   f"only {a}: {dict(list(only_a.items())[:10])}; only {b}: {dict(list(only_b.items())[:10])}")
                scans.append("\n".join(row))
                continue
            truth, wtruth = cp.guard_truth(page, running[0]), cp.word_truth(page, running[1])
            pdf_nums = cp.guard_numbers(page.get_text("text"))
            cells = []
            for e in engines:
                md = get(e, name, p)
                t = tot[e]
                t["pages"] += 1
                if md is None or not md.strip():
                    t["absent"] += 1
                    cells.append(f"{e}: EMPTY")
                    continue
                got = cp.guard_numbers(md)
                nm = sum(max(0, c - got[k]) for k, c in truth.items())
                wm = cp.missing_words(wtruth, md)
                ne = sum(max(0, c - pdf_nums[k]) for k, c in got.items())
                rows = sum(1 for ln in md.splitlines() if ln.lstrip().startswith("|") and not re.match(r"^\s*\|[\s:|-]+\|\s*$", ln))
                t["num_missing"] += nm
                t["word_missing"] += sum(wm.values())
                t["num_extra"] += ne
                t["table_rows"] += rows
                t["pass"] += int(nm == 0 and not wm)
                miss_n = [k for k, c in truth.items() if got[k] < c][:6]
                cells.append(f"{e}: numbers missing {nm} {miss_n if nm else ''}, words missing {sum(wm.values())} "
                             f"{dict(list(wm.items())[:8]) if wm else ''}, extra numbers {ne}, table rows {rows}")
            detail.append(f"### {name} p{p}: {sum(truth.values())} numbers, {sum(wtruth.values())} words in the PDF\n"
                          + "\n".join(f"- {c}" for c in cells))
    lines = ["# Reader head-to-head (strict no-loss check)", "",
             "Typed pages: a page passes only if every number and every word of the PDF's text layer is kept.", "",
             "| reader | typed pages | pass | numbers missing | words missing | numbers not in PDF | table rows | empty | sec/page |",
             "|---|---|---|---|---|---|---|---|---|"]
    for e in engines:
        t = tot[e]
        tf = OUT / e / "_timing.json"
        tm = json.loads(tf.read_text()) if tf.exists() else {}
        spp = f"{tm['seconds'] / tm['pages']:.1f}" if tm.get("pages") else "?"
        lines.append(f"| {e} | {t['pages']} | {t['pass']} | {t['num_missing']} | {t['word_missing']} | {t['num_extra']} | "
                     f"{t['table_rows']} | {t['absent']} | {spp} |")
    lines += ["", "## Typed pages", "", *detail, "", "## Scanned pages (no text layer: readers compared with each other)", "", *scans]
    report = "\n".join(lines)
    (OUT / "report.md").write_text(report, encoding="utf-8")
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "score":
        score()
    else:
        {"local": read_local, "docling": read_docling, "marker": read_marker}[sys.argv[2]]()
