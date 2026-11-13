"""Adapter for the transmission-planning committee meetings published by CEA (cea.nic.in).

CEA lists each committee's meetings through a JSON feed, one feed per committee:

    https://cea.nic.in/api/public/link-report/getByMappingId/<mapping id>

Each row in urls.csv (repository CEA) is one of those feed URLs. From every meeting we
collect the English minutes of the meeting, any corrigendum / amendment, and the attachments
(additional agendas, agenda and minutes annexures, additional minutes). The main agenda,
meeting notice and Hindi versions are not collected. Files are fetched through CEA's file endpoint.

bot.py asks this module for the title and extra frontmatter of each file
(title_for / extra_frontmatter), because the file URL itself carries no readable name.
"""

import time
from urllib.parse import quote, urlparse

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
BASE_URL = "https://cea.nic.in"

# mapping id -> (output folder under cea/, short name used in titles, full committee name)
COMMITTEES = {
    "377": ("nct", "NCT", "National Committee on Transmission"),
    "440": ("npc", "NPC", "National Power Committee"),
    "620": ("rpc_tp_all_india", "All India",
            "All India Joint Standing Committee on Power System Planning"),
    "621": ("rpc_tp_nr", "NR",
            "Northern Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
    "622": ("rpc_tp_wr", "WR",
            "Western Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
    "623": ("rpc_tp_sr", "SR",
            "Southern Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
    "624": ("rpc_tp_er", "ER",
            "Eastern Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
    "625": ("rpc_tp_ner", "NER",
            "North Eastern Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
}

# Files CEA's server cannot deliver in full. They are skipped so a known gap does not raise an alert
# every week. Re-test occasionally; remove an entry once the file downloads completely.
KNOWN_UNAVAILABLE = {
    "npcMinutes-7.pdf": "about 168 MB; the server cuts the connection partway (tried 2026-11-12/13 from GitHub and from a PC) "
                        "and ignores resume requests, so only a truncated, unreadable file can be fetched",
}

_FILE_META = {}  # file URL -> {"title": ..., "frontmatter": {...}}


def _mapping_id(url):
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def output_subdir(url):
    entry = COMMITTEES.get(_mapping_id(url))
    return entry[0] if entry else "other"


def _file_url(path):
    return f"{BASE_URL}/api/public/link-report/file?path={quote(path)}&mode=view"


def discover_pdfs(url):
    mapping = _mapping_id(url)
    if mapping not in COMMITTEES:
        raise RuntimeError(f"unknown CEA committee feed '{mapping}' - add it to COMMITTEES in adapters/cea.py")
    _, short, full = COMMITTEES[mapping]

    resp = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=60)
    resp.raise_for_status()
    meetings = resp.json()
    if not isinstance(meetings, list) or not meetings:
        raise RuntimeError(f"CEA feed {url} returned no meetings - the site may have changed")

    urls = []
    for m in meetings:
        number = str(m.get("meetingNo") or "").strip() or "unnumbered"
        subject = (m.get("subjectOfMeetingEn") or "").strip().lower()
        # regional feeds hold two numbered series: the older Standing Committee and the newer RPC(TP)
        series = "Standing Committee" if "standing" in subject else "RPC(TP)" if "power committee" in subject else ""
        name = " ".join(x for x in (short, series) if x)
        files = [
            ("minutes of the meeting", m.get("minutesOfMeetingPathEn")),
            ("corrigendum", m.get("corrigendumPathEn")),
        ]
        for a in sorted(m.get("attachments") or [], key=lambda a: a.get("sequenceNo") or 0):
            kind = (a.get("attachmentType") or "attachment").replace("_", " ").lower()
            files.append((f"{kind} {a.get('sequenceNo') or ''}".strip(), a.get("filePathEn")))
        for kind, path in files:
            if not path:
                continue
            if path.rsplit("/", 1)[-1] in KNOWN_UNAVAILABLE:
                continue
            file_url = _file_url(path)
            if file_url in _FILE_META:
                continue
            _FILE_META[file_url] = {
                "title": f"{name} meeting {number} - {kind}",
                "frontmatter": {
                    "committee": full,
                    "meeting_no": number,
                    "document_type": kind,
                    "source_file_name": path.rsplit("/", 1)[-1],
                    "meeting_date": m.get("meetingDate") or "",
                    "subject": (m.get("subjectOfMeetingEn") or "").strip(),
                },
            }
            urls.append(file_url)
    time.sleep(0.3)
    return urls


def title_for(file_url):
    meta = _FILE_META.get(file_url)
    return meta["title"] if meta else None


def extra_frontmatter(file_url):
    meta = _FILE_META.get(file_url)
    return dict(meta["frontmatter"]) if meta else {}
