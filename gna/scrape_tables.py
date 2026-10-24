"""Scrape CTU's structured GNA tables (HTML, no PDFs) into row dicts.

Sources:
  - Connectivity to be Made Effective: form POST to the page itself, page=1..N
  - GNA Application Status: AJAX endpoint per sub-report; limit=5000 returns
    every row in one response, so no pagination loop is needed
"""

import re
import time

import requests
from bs4 import BeautifulSoup

from linking import extract_ids_from_table_cell

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

EFFECTIVENESS_URL = "https://ctuil.in/connectivity-effective-list"
GNA_STATUS_PAGE = "https://www.ctuil.in/nswsapi/gna"
GNA_SUBREPORTS = [  # (url slug, activeRegion value the page sends)
    ("connectivity", "connectivity"),
    ("connectivitytwo", "connectivitytwo"),
    ("enhancement", "enhancement"),
    ("stage-one", "Stage-one"),
    ("stage-two", "stage-two"),
]


def _table_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return [], []
    header = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if tds:
            rows.append([td.get_text(" ", strip=True) for td in tds])
    return header, rows


def fetch_effectiveness_list():
    session = requests.Session()
    headers = {"User-Agent": UA, "Referer": EFFECTIVENESS_URL}
    records, page, last_page = [], 1, None
    while True:
        resp = session.post(
            EFFECTIVENESS_URL,
            headers=headers,
            data={
                "sort_field": "ConnectivityToBeMadeEffective.exp_date", "sort_type": "DESC",
                "myid": "", "page": str(page), "task": "", "type_of_project": "",
                "region": "", "state": "", "from_date": "", "to_date": "",
            },
            timeout=60,
        )
        resp.raise_for_status()
        if last_page is None:
            nums = [int(n) for n in re.findall(r"gotopage\((\d+)\)", resp.text)]
            last_page = max(nums) if nums else 1
        _, rows = _table_rows(resp.text)
        for r in rows:
            if len(r) < 10:
                continue
            records.append({
                "source": "connectivity_effective_list",
                "expected_effective_date": r[1],
                "region": r[2],
                "state": r[3],
                "substation": r[4],
                "application_no_raw": r[5],
                "application_ids": extract_ids_from_table_cell(r[5]),
                "applicant": r[6],
                "generation_type": r[7],
                "installed_capacity_mw": r[8],
                "deemed_gna_mw": r[9],
            })
        if page >= last_page:
            break
        page += 1
        time.sleep(0.4)
    return records


def fetch_gna_status():
    headers = {
        "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Referer": GNA_STATUS_PAGE,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    records = []
    for slug, region in GNA_SUBREPORTS:
        resp = requests.post(
            f"https://www.ctuil.in/nswsapi/gna/connectivityreport/{slug}?p=ajax",
            headers=headers,
            data={"activeRegion": region, "page": "1", "limit": "5000", "sort_field": "norg",
                  "sort_type": "DESC", "search_keyword": "", "mtoa_start_date": ""},
            timeout=120,
        )
        resp.raise_for_status()
        header, rows = _table_rows(resp.text)
        for r in rows:
            row = dict(zip(header, r))
            app_raw = row.get("Application No.", "")
            records.append({
                "source": f"gna_status_{slug}",
                "sub_report": slug,
                "applicant": row.get("Name of the Applicant", ""),
                "application_no_raw": app_raw,
                "application_ids": extract_ids_from_table_cell(app_raw),
                "application_date": row.get("Application Date", ""),
                "status": row.get("Status", ""),
                "fields": {k: v for k, v in row.items()
                           if k not in ("Sr.No.", "Name of the Applicant", "Application No.", "Application Date", "Status")},
            })
        time.sleep(0.4)
    return records
