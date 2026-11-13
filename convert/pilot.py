"""Pilot for table-aware re-conversion: convert a few source PDFs with order-search's hybrid converter and report.

Reads convert/pilot_files.csv (kb_path, why, original_url), downloads each PDF, converts it page by page and writes
  convert/pilot_out/<name>.md     the new markdown (scanned pages read by Tesseract, as the cloud step is not used here)
  convert/pilot_out/summary.md    per file: pages, time, tables, pages the cloud model would get, scanned pages
Nothing is written to Drive. Runs on GitHub (pilot-convert workflow) so this PC is not needed.
"""

import csv
import io
import os
import re
import sys
import time
from pathlib import Path

import pymupdf
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hybrid_convert as hc  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "pilot_out"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
WORKERS = os.cpu_count() or 2


def download(url):
    for attempt in range(4):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=180)
            r.raise_for_status()
            if r.content.startswith(b"%PDF"):
                return r.content
            raise ValueError("not a PDF")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"download failed: {last}")


def ocr(page):
    import pytesseract
    from PIL import Image
    pix = page.get_pixmap(dpi=300)
    # --psm 6 keeps each printed line together (as order-search does)
    return pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))), config="--psm 6").strip()


def convert(pdf_path):
    doc = pymupdf.open(pdf_path)
    pages = hc.convert_pages(doc, range(len(doc)), ocr=None, path=pdf_path, workers=WORKERS)
    for p in pages:  # scanned pages come back empty from the parallel pass; read them with Tesseract here
        if p["info"].get("scanned"):
            p["md"] = ocr(doc[p["idx"]])
    image_only = hc.image_only_pages(doc)
    doc.close()
    md = "\n".join(f"{p['marker']}\n\n{p['md']}\n" for p in pages)
    return md, [p["info"] for p in pages], image_only


def main():
    OUT.mkdir(exist_ok=True)
    rows = list(csv.DictReader(open(HERE / "pilot_files.csv", encoding="utf-8")))
    lines = ["# Pilot: table-aware re-conversion", "",
             "| file | why | pages | seconds | tables | pages for cloud model | of which scanned | garbled text layer | image-only pages |",
             "|---|---|---|---|---|---|---|---|---|"]
    totals = dict(pages=0, secs=0.0, tables=0, cloud=0, scanned=0)
    for r in rows:
        name = Path(r["kb_path"]).stem
        print(f"== {name}", flush=True)
        try:
            pdf = OUT / "_tmp.pdf"
            pdf.write_bytes(download(r["original_url"]))
            t0 = time.time()
            md, infos, image_only = convert(str(pdf))
            secs = time.time() - t0
            pdf.unlink()
        except Exception as exc:  # noqa: BLE001
            lines.append(f"| {name} | {r['why']} | FAILED: {str(exc)[:80]} | | | | | | |")
            print("   FAILED", exc, flush=True)
            continue
        (OUT / f"{name}.md").write_text(md, encoding="utf-8")
        n = len(infos)
        tables = sum(i["tables"] for i in infos)
        cloud = sum(1 for i in infos if i["route"] == "GEMINI")
        scanned = sum(1 for i in infos if i.get("scanned"))
        garbled = sum(1 for i in infos if i.get("garbled"))
        lines.append(f"| {name} | {r['why']} | {n} | {secs:.0f} | {tables} | {cloud} | {scanned} | {garbled} | {len(image_only)} |")
        print(f"   {n} pages, {secs:.0f}s, {tables} tables, cloud {cloud} (scanned {scanned})", flush=True)
        for k, v in (("pages", n), ("secs", secs), ("tables", tables), ("cloud", cloud), ("scanned", scanned)):
            totals[k] += v
    lines += ["", f"Totals: {totals['pages']} pages, {totals['secs'] / 60:.1f} min, {totals['tables']} tables, "
                  f"{totals['cloud']} pages for the cloud model ({totals['scanned']} scanned). Runner CPUs: {WORKERS}."]
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
