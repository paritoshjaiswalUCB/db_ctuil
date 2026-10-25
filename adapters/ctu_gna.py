"""Adapter for CTU's GNA / connectivity and transmission-status document pages.

Each listing page (and its archive page, where CTU has one) is a separate
row in urls.csv with repository CTU_GNA. Documents are collected from the
page's own content area only: every CTU page also carries ~34 sitewide menu
links to unrelated PDFs, and those live outside section.regionsTabsSection.
"""

import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from adapters.ctu import escape_bare_ampersands

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
BASE_URL = "https://ctuil.in"
CONTENT_SELECTOR = "section.regionsTabsSection"

# listing page path (lower-case, no leading slash) -> output subfolder under gna/documents
SOURCE_SUBDIRS = {
    "gna2022updates": "applications_granted",
    "gna2022updates__archive": "applications_granted",
    "renewable-energy": "bay_allocation_margin",
    "revocation": "revocation_monitoring",
    "reallocation_meetings": "reallocation_meetings",
    "complianceandfc": "land_fc_compliance",
    "rtmtbcb": "rtm_tbcb_project_status",
    "ists-schemes": "ists_scheme_approvals",
    "regenerators": "effectiveness_monthly",
    "regenerators_archive": "effectiveness_monthly",
    "substation-bulk-consumers": "bulk_consumer_substations",
    "tentative-coordinates-of-re-ps": "substation_coordinates",
}


def _page_key(url):
    return urlparse(url).path.strip("/").lower()


def output_subdir(url):
    if url.lower().split("?")[0].endswith(".pdf"):
        return "reference"
    return SOURCE_SUBDIRS.get(_page_key(url), "other")


def discover_pdfs(url):
    if url.lower().split("?")[0].endswith(".pdf"):
        return [url]

    resp = requests.get(url, headers={"User-Agent": UA, "Referer": BASE_URL + "/"}, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(escape_bare_ampersands(resp.text), "html.parser")
    content = soup.select_one(CONTENT_SELECTOR)
    if content is None:
        raise RuntimeError(f"content area '{CONTENT_SELECTOR}' not found on {url} - page layout may have changed")

    links = []
    for a in content.find_all("a", href=True):
        full = urljoin(BASE_URL + "/", a["href"].strip())
        if full.lower().split("?")[0].endswith(".pdf") and full not in links:
            links.append(full)
    time.sleep(0.3)
    return links
