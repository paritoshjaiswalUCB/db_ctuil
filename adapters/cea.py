"""Adapter for the transmission-planning committee meetings published by CEA (cea.nic.in).

CEA lists each committee's meetings through a JSON feed, one feed per committee:

    https://cea.nic.in/api/public/link-report/getByMappingId/<mapping id>

Each row in urls.csv (repository CEA) is one of those feed URLs. From every meeting we
collect the English minutes of the meeting and any corrigendum / amendment; agendas and
attachments are not collected. Files are fetched through CEA's file endpoint.

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
    "620": ("rpc_tp_all_india", "All India Joint Standing Committee",
            "All India Joint Standing Committee on Power System Planning"),
    "621": ("rpc_tp_nr", "NR RPC(TP)/Standing Committee",
            "Northern Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
    "622": ("rpc_tp_wr", "WR RPC(TP)/Standing Committee",
            "Western Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
    "623": ("rpc_tp_sr", "SR RPC(TP)/Standing Committee",
            "Southern Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
    "624": ("rpc_tp_er", "ER RPC(TP)/Standing Committee",
            "Eastern Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
    "625": ("rpc_tp_ner", "NER RPC(TP)/Standing Committee",
            "North Eastern Region Power Committee (Transmission Planning) / Standing Committee on Power System Planning"),
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
        for kind, field in (("minutes of the meeting", "minutesOfMeetingPathEn"), ("corrigendum", "corrigendumPathEn")):
            path = m.get(field)
            if not path:
                continue
            file_url = _file_url(path)
            if file_url in _FILE_META:
                continue
            _FILE_META[file_url] = {
                "title": f"{short} meeting {number} - {kind}",
                "frontmatter": {
                    "committee": full,
                    "meeting_no": number,
                    "document_type": kind,
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
