"""Diagnose the cloud step's guard: for chosen pages of one PDF, ask Gemini, convert locally, and report, for each version,
which numbers and which words of the PDF text layer it misses, with the PDF line around each missing number.
Writes convert/diag_out/ (report.md, p<N>_cloud.md, p<N>_local.md, p<N>_pdftext.txt). Does not touch Drive.

    DIAG_URL=<pdf url> DIAG_PAGES=3,6,10 python -m convert.cloud_diag
"""

import os
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import download_pdf  # noqa: E402
from convert import cloud_pass as cp  # noqa: E402
from convert import hybrid_convert as hc  # noqa: E402

OUT = ROOT / "convert" / "diag_out"
WORD = re.compile(r"[A-Za-z]{3,}")


def words(text):
    return Counter(w.lower() for w in WORD.findall(re.sub(r"<!--.*?-->", "", text, flags=re.S)))


def coverage(truth, text):
    got = words(text)
    total = sum(truth.values())
    return (sum(min(c, got[k]) for k, c in truth.items()) / total) if total else 1.0, {k: c - got[k] for k, c in truth.items() if got[k] < c}


def context(pdf_text, num):
    for line in pdf_text.splitlines():
        if num in line.replace(",", ""):
            return line.strip()[:120]
    return ""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pages = [int(p) for p in os.environ["DIAG_PAGES"].split(",")]
    pdf = OUT / "doc.pdf"
    pdf.write_bytes(download_pdf(os.environ["DIAG_URL"]))
    doc = pymupdf.open(str(pdf))
    running = cp.running_numbers(doc)
    from convert import table_aware
    local = {r["idx"] + 1: r["md"] for r in hc.convert_pages(doc, [p - 1 for p in pages])}
    for p in pages:
        if cp._is_scanned(doc[p - 1]):
            local[p] = table_aware._ocr(doc[p - 1])
    run_all = (running, cp.running_words(doc))
    cp.used_today = lambda: 0
    cp.count_request = lambda: None
    gem = cp.Gemini()
    got = cp.read_cloud(gem, doc, pages)
    lines = [f"# Guard diagnosis ({cp.MODEL}, {gem.requests} requests); running header/footer numbers: {sorted(running)}", ""]
    for p in pages:
        text, cloud, loc = doc[p - 1].get_text("text"), got.get(p, ""), local.get(p, "")
        truth = cp.guard_truth(doc[p - 1], running)
        c_n, l_n = cp.guard_numbers(cloud), cp.guard_numbers(loc)
        miss_c = {k: v - c_n[k] for k, v in truth.items() if c_n[k] < v}
        miss_l = {k: v - l_n[k] for k, v in truth.items() if l_n[k] < v}
        extra_c = {k: v - truth[k] for k, v in c_n.items() if truth[k] < v}
        wt = words(text)
        wc, wmiss_c = coverage(wt, cloud)
        wl, wmiss_l = coverage(wt, loc)
        decision = cp._decide(doc[p - 1], run_all, loc, cloud)[0] if cloud else "cloud_empty"
        if cp._is_scanned(doc[p - 1]):
            cov, sup = cp.scan_agreement(loc, cloud)
            t_n = cp.guard_numbers(loc)
            lines += [f"## page {p} (scanned)  (decision: {decision})",
                      f"- Tesseract figures found in Gemini: {cov:.3f}; Gemini figures found in Tesseract: {sup:.3f}",
                      f"- Tesseract-only figures: {dict(list({k: v - c_n[k] for k, v in t_n.items() if c_n[k] < v}.items())[:15])}",
                      f"- Gemini-only figures: {dict(list({k: v - t_n[k] for k, v in c_n.items() if t_n[k] < v}.items())[:15])}", ""]
            (OUT / f"p{p}_cloud.md").write_text(cloud, encoding="utf-8")
            (OUT / f"p{p}_local.md").write_text(loc, encoding="utf-8")
            continue
        lines += [f"## page {p}  (decision: {decision})",
                  f"- numbers: {sum(truth.values())}; cloud recall {cp.recall(truth, cloud):.3f}, local {cp.recall(truth, loc):.3f}",
                  f"- words: {sum(wt.values())}; cloud coverage {wc:.3f}, local {wl:.3f}",
                  f"- numbers missing in cloud: {miss_c}",
                  *[f"    - {k}: `{context(text, k)}`" for k in list(miss_c)[:8]],
                  f"- numbers missing in local: {miss_l}",
                  f"- numbers in cloud but not in PDF: {dict(list(extra_c.items())[:10])}",
                  f"- words missing in cloud (first 25): {dict(list(wmiss_c.items())[:25])}",
                  f"- words missing in local (first 25): {dict(list(wmiss_l.items())[:25])}", ""]
        (OUT / f"p{p}_cloud.md").write_text(cloud, encoding="utf-8")
        (OUT / f"p{p}_local.md").write_text(loc, encoding="utf-8")
        (OUT / f"p{p}_pdftext.txt").write_text(text, encoding="utf-8")
    pdf.unlink()
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
