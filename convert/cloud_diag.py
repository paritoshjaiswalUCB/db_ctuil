"""Diagnose the number guard: for chosen pages of one PDF, ask Gemini, convert locally, and report which numbers of the PDF
text layer each version misses. Writes convert/diag_out/ (report.md, p<N>_cloud.md, p<N>_local.md). Does not touch Drive.

    DIAG_URL=<pdf url> DIAG_PAGES=3,6,10 python -m convert.cloud_diag
"""

import os
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import download_pdf  # noqa: E402
from convert import cloud_pass as cp  # noqa: E402
from convert import hybrid_convert as hc  # noqa: E402

OUT = ROOT / "convert" / "diag_out"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pages = [int(p) for p in os.environ["DIAG_PAGES"].split(",")]
    pdf = OUT / "doc.pdf"
    pdf.write_bytes(download_pdf(os.environ["DIAG_URL"]))
    doc = pymupdf.open(str(pdf))
    local = {r["idx"] + 1: r["md"] for r in hc.convert_pages(doc, [p - 1 for p in pages])}
    cp.used_today = lambda: 0
    cp.count_request = lambda: None
    gem = cp.Gemini()
    got = cp.read_cloud(gem, doc, pages)
    lines = [f"# Number-guard diagnosis ({cp.MODEL}, {gem.requests} requests)", ""]
    for p in pages:
        text, cloud, loc = doc[p - 1].get_text("text"), got.get(p, ""), local.get(p, "")
        truth, c_n, l_n = hc._numbers(text), hc._numbers(cloud), hc._numbers(loc)
        miss_c = {k: v - c_n[k] for k, v in truth.items() if c_n[k] < v}
        miss_l = {k: v - l_n[k] for k, v in truth.items() if l_n[k] < v}
        extra_c = {k: v - truth[k] for k, v in c_n.items() if truth[k] < v}
        lines += [f"## page {p}",
                  f"- numbers in PDF text: {sum(truth.values())}; cloud recall {hc.numeric_recall(text, cloud):.3f}, "
                  f"local recall {hc.numeric_recall(text, loc):.3f}",
                  f"- missing in cloud: {miss_c}", f"- missing in local: {miss_l}",
                  f"- in cloud but not in PDF: {extra_c}", ""]
        (OUT / f"p{p}_cloud.md").write_text(cloud, encoding="utf-8")
        (OUT / f"p{p}_local.md").write_text(loc, encoding="utf-8")
        (OUT / f"p{p}_pdftext.txt").write_text(text, encoding="utf-8")
    pdf.unlink()
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
