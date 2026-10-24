"""Shared helpers for linking GNA records to documents.

Application IDs seen across CTU sources are 10 digits with a known prefix:
  22xxxxxxxx  applications under the GNA Regulations 2022
  12xxxxxxxx  legacy applications under the Connectivity Regulations 2009
  91xxxxxxxx / 92xxxxxxxx  RE at thermal generating stations
They often carry labels like "St-II:", "LTA:" or "Tr. Of Connectivity -", and
several can share one table cell, so extraction scans for the digit pattern
rather than relying on cell boundaries.

Applicant and substation names vary between sources ("Pvt. Ltd." vs "Private
Limited", "Bhadla-II" vs "Bhadla II PS"), so both are normalised to a
comparison key before matching.
"""

import re

APP_ID_PATTERN = re.compile(r"(?<!\d)(?:12|22|91|92)\d{8}(?!\d)")
# 11-digit near-misses (e.g. "22000002431" in a published PDF) - surfaced for review, never auto-linked
SUSPECT_ID_PATTERN = re.compile(r"(?<!\d)(?:12|22|91|92)\d{9}(?!\d)")

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def extract_application_ids(text):
    """Standard-format IDs only - safe to run over free text."""
    return sorted(set(APP_ID_PATTERN.findall(text or "")))


# Legacy numbering (pre-GNA) seen in the structured tables: 9-digit codes like
# 212100038 / 330700007, and alphanumeric codes like SW99xxxx. Too generic to
# pattern-match in free text, so they're only used inside a known table cell
# and, for documents, matched against the set of IDs already known from tables.
LEGACY_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{1,3}\d{4,10}|\d{7,11})(?![A-Za-z0-9])")


def extract_ids_from_table_cell(raw):
    ids = set(APP_ID_PATTERN.findall(raw or ""))
    for tok in LEGACY_TOKEN_PATTERN.findall(raw or ""):
        if not SUSPECT_ID_PATTERN.fullmatch(tok):
            ids.add(tok)
    return sorted(ids)


def find_known_ids_in_text(text, known_ids):
    """Exact-token lookup of IDs already known from structured sources (any format)."""
    if not text or not known_ids:
        return []
    tokens = set(re.findall(r"(?<![A-Za-z0-9])[A-Za-z]{0,3}\d{4,11}(?![A-Za-z0-9])", text))
    return sorted(tokens & set(known_ids))


def extract_suspect_ids(text):
    return sorted(set(SUSPECT_ID_PATTERN.findall(text or "")))


def normalize_applicant(name):
    s = (name or "").upper()
    s = re.sub(r"\bM/S\.?\s*", " ", s)
    s = re.sub(r"\bPVT\b\.?|\bPRIVATE\b", " PRIVATE ", s)
    s = re.sub(r"\bLTD\b\.?|\bLIMITED\b", " LIMITED ", s)
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _roman_suffix_to_digit(s):
    # "bhadla-ii" / "bhadla ii" -> "bhadla 2"; leaves "sec-i" style section markers to the caller
    return re.sub(
        r"(?<=[a-z])[\s\-]+(i{1,3}|iv|vi{0,3}|ix|x)\b",
        lambda m: f" {ROMAN[m.group(1)]}",
        s,
    )


def normalize_substation(name):
    """Return (key, system_identified) for a substation label."""
    s = (name or "").lower()
    system_identified = "yet to be identified" not in s
    s = re.sub(r"\(system yet to be identified\)", " ", s)
    s = s.replace("*", " ")
    s = re.sub(r"\(gis\)|\(pg\)|\bs/s\b|\bss\b|\bsubstation\b|\bpooling station\b|\bpooling\b", " ", s)
    s = re.sub(r"\bists\b|\bps\b|\b\d{3}/\d{3}\s*kv\b|\b\d{3}\s*kv\b", " ", s)
    s = re.sub(r"\(\s*sec[\s\-]*(i{1,3}|iv|v)\s*\)", lambda m: f" sec {ROMAN[m.group(1)]}", s)
    s = re.sub(r"\(new\)", " new", s)
    s = _roman_suffix_to_digit(s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(), system_identified


def substation_text_pattern(key):
    """Regex that finds a normalised substation key in normalised document text,
    without letting "bikaner" match inside "bikaner 2"."""
    parts = key.split()
    body = r"[\s\-]*".join(re.escape(p) for p in parts)
    return re.compile(rf"\b{body}\b(?![\s\-]*(?:\d|sec\b|new\b))")


def normalize_text_for_substations(text):
    s = (text or "").lower()
    s = re.sub(r"\(\s*sec[\s\-]*(i{1,3}|iv|v)\s*\)", lambda m: f" sec {ROMAN[m.group(1)]}", s)
    s = _roman_suffix_to_digit(s)
    s = re.sub(r"[^a-z0-9 \-]+", " ", s)
    return re.sub(r"\s+", " ", s)
