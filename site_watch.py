"""Weekly check that ctuil.in has not changed shape underneath the scraper.

Two things are watched, and both are reported by check_health.py:

  1. New pages: any page in the site menu that was not there on the previous
     run. A new CTU page may hold documents the bot does not know about.
  2. Dried-up pages: a page we collect from (urls.csv) that listed documents
     last run but lists none now. That is what a layout change looks like.

The previous run's page list and per-page document counts are kept in
_system/site_watch.json. The first run only records a baseline.
"""

import csv
import json
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from adapters.ctu_gna import BASE_URL, UA, discover_pdfs
from config import BASE_DIR, SYSTEM_DIR

STATE_FILE = SYSTEM_DIR / "site_watch.json"
URLS_FILE = BASE_DIR / "urls.csv"

# menu links that are not documents pages
IGNORED_PREFIXES = ("uploads/", "global-search", "login", "sitemap", "latestnews", "photo-gallery")


def menu_pages():
    resp = requests.get(BASE_URL + "/", headers={"User-Agent": UA}, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    pages = set()
    for a in soup.find_all("a", href=True):
        full = urljoin(BASE_URL + "/", a["href"].strip())
        parsed = urlparse(full)
        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue
        path = unquote(parsed.path).strip("/")
        if not path or path.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".zip")):
            continue
        if path.lower().startswith(IGNORED_PREFIXES):
            continue
        pages.add(path)
    return pages


def tracked_urls():
    with open(URLS_FILE, encoding="utf-8-sig", newline="") as f:
        return [r["url"] for r in csv.DictReader(f) if r["repository"] == "CTU_GNA" and not r["url"].lower().endswith(".pdf")]


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def check():
    """Returns a list of problem sentences (empty when nothing changed)."""
    pages = menu_pages()
    counts = {}
    for url in tracked_urls():
        try:
            counts[url] = len(discover_pdfs(url))
        except Exception as exc:  # one broken page should not hide the others
            counts[url] = None
            print(f"site_watch: could not read {url}: {exc}")

    previous = load_state()
    problems = []
    if previous is not None:
        new_pages = sorted(pages - set(previous["pages"]))
        if new_pages:
            problems.append(
                "New page(s) appeared on ctuil.in and are not being collected yet: "
                + ", ".join(f"{BASE_URL}/{p}" for p in new_pages)
            )
        dried = [
            u for u, n in counts.items()
            if n == 0 and (previous["counts"].get(u) or 0) > 0
        ]
        if dried:
            problems.append(
                "These pages listed documents last run but none now (the page layout may have changed): "
                + ", ".join(dried)
            )

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"pages": sorted(pages), "counts": counts}, indent=1), encoding="utf-8"
    )
    return problems


if __name__ == "__main__":
    for p in check() or ["no changes"]:
        print(p)
