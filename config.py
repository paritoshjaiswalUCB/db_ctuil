"""Paths and tool locations shared by every script.

Defaults match this PC (Google Drive for Desktop at G:). On GitHub Actions the
workflow sets KB_ROOT to a local working copy of the Drive folder and
installs Tesseract on PATH, so the same code runs unchanged in both places.
"""

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

KB_ROOT = Path(os.environ.get("KB_ROOT", r"G:\My Drive\db_cmets_meetings"))
GNA_DIR = KB_ROOT / "gna"

_WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def tesseract_cmd():
    return os.environ.get("TESSERACT_CMD") or shutil.which("tesseract") or _WINDOWS_TESSERACT
