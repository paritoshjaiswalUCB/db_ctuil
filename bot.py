"""Regulatory KB bot.

Reads urls.csv, discovers PDF links per row (via the matching adapter),
downloads each PDF, converts it to markdown with the local `marker` library,
tags it with YAML frontmatter (best-effort metadata extracted via regex),
and writes the result to Google Drive.

Fully offline conversion - no external API calls except downloading the
source PDFs themselves.
"""

import csv
import json
import logging
import re
import sys
import time
import unicodedata
from datetime import datetime, date
from pathlib import Path

import requests
import yaml

from adapters import ctu, ctu_gna
from config import BASE_DIR, GNA_DIR, KB_ROOT, SYSTEM_DIR, tesseract_cmd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

URLS_CSV = BASE_DIR / "urls.csv"
LOG_FILE = BASE_DIR / "conversion.log"
REPORT_FILE = BASE_DIR / "conversion_report.md"
OUTPUT_DIR = KB_ROOT

ADAPTERS = {
    "CTU": ctu,
    "CTU_GNA": ctu_gna,
}

DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
    "Referer": "https://ctuil.in/",
}

MAX_DOWNLOAD_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("regulatory_kb_bot")
logger.setLevel(logging.INFO)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_stream_handler)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_pdf(url):
    """Download a PDF with retry/backoff. Returns bytes, raises on failure."""
    backoff = INITIAL_BACKOFF_SECONDS
    last_exc = None

    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            resp = requests.get(url, headers=DOWNLOAD_HEADERS, timeout=60)
            resp.raise_for_status()
            content = resp.content
            if not content.startswith(b"%PDF"):
                raise ValueError(
                    f"response does not look like a PDF (magic bytes: {content[:8]!r})"
                )
            return content
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything
            last_exc = exc
            logger.warning(
                f"  download attempt {attempt}/{MAX_DOWNLOAD_RETRIES} failed for {url}: {exc}"
            )
            if attempt < MAX_DOWNLOAD_RETRIES:
                time.sleep(backoff)
                backoff *= 2

    raise RuntimeError(f"failed to download {url} after {MAX_DOWNLOAD_RETRIES} attempts: {last_exc}")


# ---------------------------------------------------------------------------
# PDF -> markdown conversion (hybrid: direct text extraction, OCR fallback)
# ---------------------------------------------------------------------------
#
# marker-pdf's ML pipeline (layout + OCR models) is impractically slow on this
# machine (7.5GB RAM, no GPU) - a single 16-page scanned PDF ran for 15+
# minutes of CPU time without finishing its first stage. Most of the source
# PDFs are born-digital (have a real text layer) and don't need OCR at all;
# only genuinely scanned pages need it. So: extract embedded text per page
# directly (pypdfium2, no ML), and only fall back to Tesseract OCR (fast,
# CPU-friendly, no VLM) for pages with no usable text layer.
#
# Known limitation (accepted for now - revisit only if it turns out to matter
# for downstream LLM/RAG quality): some bilingual PDFs use legacy fonts with
# broken Hindi encoding, and OCR here runs English-only, so Hindi portions in
# the output can come out as garbled text. English content is unaffected.

MIN_TEXT_LAYER_CHARS = 20  # below this, treat the page as scanned/no text layer

_pytesseract_configured = False


def _configure_pytesseract():
    global _pytesseract_configured
    if not _pytesseract_configured:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd()
        _pytesseract_configured = True


def convert_pdf_to_markdown(pdf_path):
    """Return (markdown_text, used_ocr)."""
    import pypdfium2 as pdfium
    import pytesseract

    _configure_pytesseract()

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page_texts = []
        used_ocr = False

        for i in range(len(pdf)):
            page = pdf[i]
            text = page.get_textpage().get_text_bounded().strip()

            if len(text) < MIN_TEXT_LAYER_CHARS:
                used_ocr = True
                bitmap = page.render(scale=2.0)
                pil_image = bitmap.to_pil()
                text = pytesseract.image_to_string(pil_image).strip()

            page_texts.append(text)
    finally:
        pdf.close()

    markdown_text = "\n\n---\n\n".join(page_texts)
    return markdown_text, used_ocr


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

DATE_PATTERNS = [
    r"\b(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})\b",   # DD.MM.YYYY / DD-MM-YYYY / DD/MM/YYYY
    r"\b(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+\d{4})\b",  # 5th September 2026
]

REFERENCE_PATTERNS = [
    r"(Order No\.?\s*[:\-]?\s*[\w/\-]+)",
    r"(Petition No\.?\s*[:\-]?\s*[\w/\-]+)",
    r"(Meeting No\.?\s*[:\-]?\s*[\w/\-]+)",
    r"(\d+(?:st|nd|rd|th)\s+(?:CMETS|Meeting)[\w\s\-]*)",
]


def extract_metadata(markdown_text, fallback_title):
    """Best-effort metadata extraction from the first ~500 words of text."""
    words = markdown_text.split()
    excerpt = " ".join(words[:500])

    needs_review = False

    # Title: the source filename is almost always a clean, human-written
    # description of the document (e.g. "Minutes of 39th CMETS-ER"). OCR'd
    # text is noisy (garbled letterhead text, bilingual headers, stray
    # characters), so prefer the filename and only fall back to sniffing the
    # document body when the filename itself isn't informative.
    title = fallback_title.strip()
    if len(title) <= 5 or title.replace(" ", "").isdigit():
        title = None
        for line in markdown_text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped and len(stripped) > 5 and stripped.isascii():
                title = stripped
                break
        if not title:
            title = fallback_title or "untitled"
        needs_review = True

    # Date
    doc_date = None
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, excerpt)
        if match:
            doc_date = match.group(1)
            break
    if not doc_date:
        needs_review = True

    # Document reference (order/petition/meeting number)
    doc_reference = None
    for pattern in REFERENCE_PATTERNS:
        match = re.search(pattern, excerpt, re.IGNORECASE)
        if match:
            doc_reference = match.group(1).strip()
            break
    if not doc_reference:
        needs_review = True

    return {
        "title": title,
        "date": doc_date or "",
        "doc_reference": doc_reference or "",
        "needs_review": needs_review,
    }


# ---------------------------------------------------------------------------
# Filename / frontmatter helpers
# ---------------------------------------------------------------------------

def slugify(value, max_length=120):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s\-]", "", value).strip().lower()
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:max_length] or "untitled"


def build_filename(repository, title):
    return f"{slugify(repository)}_{slugify(title)}.md"


def existing_original_url(path):
    """Best-effort read of the original_url frontmatter field of an existing file."""
    try:
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        front = yaml.safe_load(parts[1])
        return front.get("original_url") if front else None
    except Exception:
        return None


def write_markdown_with_frontmatter(output_path, markdown_text, frontmatter):
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    content = f"---\n{fm_yaml}---\n\n{markdown_text}"
    output_path.write_text(content, encoding="utf-8")


def output_dir_for(adapter, repository, url):
    """CMETS minutes stay at the top of the Drive folder (unchanged layout);
    GNA and transmission-status documents go under gna/documents/<source>/."""
    if repository == "CTU":
        return OUTPUT_DIR
    sub = adapter.output_subdir(url) if hasattr(adapter, "output_subdir") else repository.lower()
    return GNA_DIR / "documents" / sub


def load_converted_urls():
    """Set of original_url values already present in the output folder.

    Lets weekly re-runs skip PDFs already converted, so they only download
    and OCR newly published documents instead of redoing all of them.
    """
    if not OUTPUT_DIR.exists():
        return set()
    urls = set()
    for path in OUTPUT_DIR.rglob("*.md"):
        if "_system" in path.parts:
            continue
        url = existing_original_url(path)
        if url:
            urls.add(url)
    return urls


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_row(row_index, url, repository, report):
    logger.info(f"\n=== Row {row_index}: {repository} -> {url} ===")

    adapter = ADAPTERS.get(repository)
    if adapter is None:
        logger.error(f"No adapter found for repository '{repository}', skipping row.")
        report["rows"].append({
            "row": row_index, "url": url, "repository": repository,
            "discovered": 0, "succeeded": 0, "failed": 0,
            "failures": [f"no adapter for repository '{repository}'"],
        })
        return

    try:
        pdf_urls = adapter.discover_pdfs(url)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Discovery failed for {url}: {exc}")
        report["rows"].append({
            "row": row_index, "url": url, "repository": repository,
            "discovered": 0, "succeeded": 0, "failed": 0,
            "failures": [f"discovery error: {exc}"],
        })
        return

    logger.info(f"Discovered {len(pdf_urls)} PDF(s).")

    out_dir = output_dir_for(adapter, repository, url)
    already_converted = load_converted_urls()

    row_report = {
        "row": row_index, "url": url, "repository": repository,
        "discovered": len(pdf_urls), "succeeded": 0, "failed": 0, "skipped": 0,
        "failures": [], "needs_review_files": [],
    }

    for i, pdf_url in enumerate(pdf_urls, start=1):
        if pdf_url in already_converted:
            logger.info(f"[{i}/{len(pdf_urls)}] Skipping (already converted): {pdf_url}")
            row_report["skipped"] += 1
            continue

        logger.info(f"[{i}/{len(pdf_urls)}] Processing {pdf_url}")
        try:
            pdf_bytes = download_pdf(pdf_url)
            logger.info(f"  Downloaded ({len(pdf_bytes)} bytes).")

            tmp_pdf_path = BASE_DIR / "_tmp_download.pdf"
            tmp_pdf_path.write_bytes(pdf_bytes)

            try:
                table_markdown = adapter.convert_pdf(tmp_pdf_path) if hasattr(adapter, "convert_pdf") else None
                if table_markdown:
                    markdown_text, used_ocr = table_markdown, False
                else:
                    markdown_text, used_ocr = convert_pdf_to_markdown(tmp_pdf_path)
            finally:
                tmp_pdf_path.unlink(missing_ok=True)

            logger.info(
                f"  Converted to markdown ({len(markdown_text)} chars"
                f"{', via OCR' if used_ocr else ''})."
            )

            fallback_title = Path(pdf_url).stem.replace("%20", " ")
            metadata = extract_metadata(markdown_text, fallback_title)
            needs_review = metadata["needs_review"] or used_ocr

            frontmatter = {
                "source": repository,
                "source_page": url,
                "original_url": pdf_url,
                "converted_date": date.today().isoformat(),
                "title": metadata["title"],
                "date": metadata["date"],
                "doc_reference": metadata["doc_reference"],
                "needs_review": needs_review,
            }

            out_dir.mkdir(parents=True, exist_ok=True)
            filename = build_filename(repository, metadata["title"])
            output_path = out_dir / filename

            # Re-running the bot on the same source URL should overwrite the
            # existing file, not pile up numbered duplicates. Only treat it
            # as a genuine collision (two different documents landing on the
            # same slug) when the existing file's original_url differs.
            counter = 2
            while output_path.exists() and existing_original_url(output_path) != pdf_url:
                output_path = out_dir / f"{filename[:-3]}-{counter}.md"
                counter += 1

            write_markdown_with_frontmatter(output_path, markdown_text, frontmatter)
            logger.info(f"  Saved -> {output_path}")

            row_report["succeeded"] += 1
            if needs_review:
                row_report["needs_review_files"].append(str(output_path.name))

        except Exception as exc:  # noqa: BLE001
            logger.error(f"  FAILED: {exc}")
            row_report["failed"] += 1
            row_report["failures"].append(f"{pdf_url}: {exc}")

    report["rows"].append(row_report)


def write_report(report):
    lines = ["# Conversion Report", "", f"Generated: {datetime.now().isoformat(timespec='seconds')}", ""]

    total_discovered = sum(r["discovered"] for r in report["rows"])
    total_succeeded = sum(r["succeeded"] for r in report["rows"])
    total_failed = sum(r["failed"] for r in report["rows"])
    total_skipped = sum(r.get("skipped", 0) for r in report["rows"])

    lines.append("## Summary")
    lines.append(f"- Total PDFs discovered: {total_discovered}")
    lines.append(f"- Already converted (skipped): {total_skipped}")
    lines.append(f"- Newly converted successfully: {total_succeeded}")
    lines.append(f"- Failed: {total_failed}")

    backfill_state_file = SYSTEM_DIR / "backfill_state.json"
    if backfill_state_file.exists():
        try:
            cutoff = json.loads(backfill_state_file.read_text(encoding="utf-8"))["min_date"]
            lines.append(f"- CTU historical backfill cutoff after this run: {cutoff} (moves ~100 days further back each weekly run)")
        except Exception:
            pass

    lines.append("")

    for r in report["rows"]:
        lines.append(f"## Row {r['row']}: {r['repository']} — {r['url']}")
        lines.append(f"- Discovered: {r['discovered']}")
        lines.append(f"- Already converted (skipped): {r.get('skipped', 0)}")
        lines.append(f"- Newly converted: {r['succeeded']}")
        lines.append(f"- Failed: {r['failed']}")
        if r["failures"]:
            lines.append("- Failure reasons:")
            for f in r["failures"]:
                lines.append(f"  - {f}")
        if r.get("needs_review_files"):
            lines.append(f"- Files flagged `needs_review: true` ({len(r['needs_review_files'])}):")
            for fname in r["needs_review_files"]:
                lines.append(f"  - {fname}")
        lines.append("")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"\nReport written to {REPORT_FILE}")


def main():
    logger.info(f"Starting regulatory KB bot run at {datetime.now().isoformat(timespec='seconds')}")
    logger.info(f"Output directory: {OUTPUT_DIR}")

    report = {"rows": []}

    with open(URLS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=1):
            process_row(i, row["url"].strip(), row["repository"].strip(), report)

    write_report(report)
    logger.info("Done.")


if __name__ == "__main__":
    main()
