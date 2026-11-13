"""Table-aware PDF -> markdown, used for every new document and for re-converting older ones.

Built on order-search's hybrid converter (hybrid_convert.py): text is kept in reading order, tables become markdown
tables, and each page gets a route. Pages routed to the cloud model (hard tables, rotated or scanned pages) keep their
best local version here and are listed in `cloud_pages`, so a later step can send just those pages to Gemini.
Scanned pages are read with Tesseract meanwhile.

    md, meta = convert(pdf_path)
    meta = {"converter", "pages", "tables", "cloud_pages", "scanned_pages", "image_only_pages"}
"""

import io
import os

import pymupdf

from convert import hybrid_convert as hc

CONVERTER = "hybrid-v1"


def _ocr(page):
    import pytesseract
    from PIL import Image
    pix = page.get_pixmap(dpi=300)
    # --psm 6 keeps each printed line together, so a row label stays with its figures (as in order-search)
    return pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))), config="--psm 6").strip()


def convert(pdf_path):
    doc = pymupdf.open(str(pdf_path))
    try:
        pages = hc.convert_pages(doc, range(len(doc)), ocr=None, path=str(pdf_path), workers=os.cpu_count() or 1)
        scanned = []
        for p in pages:  # the parallel pass leaves scanned pages empty; read them with Tesseract here
            if p["info"].get("scanned"):
                p["md"] = _ocr(doc[p["idx"]])
                scanned.append(p["idx"] + 1)
        image_only = hc.image_only_pages(doc)
    finally:
        doc.close()
    md = "\n".join(f"{p['marker']}\n\n{p['md']}\n" for p in pages)
    meta = {
        "converter": CONVERTER,
        "pages": len(pages),
        "tables": sum(p["info"]["tables"] for p in pages),
        "cloud_pages": [p["idx"] + 1 for p in pages if p["info"]["route"] == "GEMINI"],
        "scanned_pages": scanned,
        "image_only_pages": image_only,
    }
    return md, meta
