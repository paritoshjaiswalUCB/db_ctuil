# regulatory-kb-bot

A self-updating knowledge base of Indian transmission-sector documents from CTUIL
(ctuil.in), kept as markdown on Google Drive and searchable by meaning. This file
explains what the database is, where everything lives, how it updates, and how to
build a similar one. State as of **1 November 2026**.

## 1. What the database is

CTUIL (Central Transmission Utility of India) publishes consultation minutes,
connectivity / General Network Access (GNA) lists, substation margin sheets and
project-status reports as PDFs and web tables. This project:

1. finds those documents on ctuil.in every week,
2. converts each to markdown (tables kept) with a small metadata header,
3. builds **one card per GNA application** and **one card per substation**, each
   with a change log, and links every document to the applications, applicants and
   substations it mentions,
4. keeps a vector index so a question can be answered from the documents.

### What is in it

| Area | Folder (under the Drive root) | Count | Notes |
|---|---|---|---|
| CMETS minutes (ISTS consultation meetings, all five regions) | root `*.md` | ~549 | 2006 to date. The per-region gap list in the Drive README is out of date and was never verified file by file. |
| Applications granted (monthly lists) | `gna/documents/applications_granted` | 148 | current + archive |
| Bay allocation and margin (Annex-B, Annex-C) | `gna/documents/bay_allocation_margin` | 7 | latest sheet 01.10.2026 |
| Effectiveness lists | `gna/documents/effectiveness_monthly` | 29 | current + archive |
| ISTS scheme approvals | `gna/documents/ists_scheme_approvals` | 199 | |
| RTM/TBCB project status | `gna/documents/rtm_tbcb_project_status` | 110 | monthly, by region |
| Reallocation meetings | `gna/documents/reallocation_meetings` | 26 | |
| Revocation monitoring | `gna/documents/revocation_monitoring` | 19 | |
| Land / financial-closure compliance | `gna/documents/land_fc_compliance` | 6 | |
| Bulk-consumer substations | `gna/documents/bulk_consumer_substations` | 16 | |
| Substation coordinates | `gna/documents/substation_coordinates` | 10 | |
| Bidding calendar | `gna/documents/bidding_calendar` | 42 | |
| GNA transition | `gna/documents/gna_transition` | 44 | |
| Relinquishment charges | `gna/documents/relinquishment_charges` | 10 | |
| Eligible entities; effectiveness-of-GNA meetings | `gna/documents/eligible_entities`, `effectiveness_of_gna_meetings` | 2 + 2 | |
| Rolling plan updates | `gna/documents/rolling_plan` | 13 | |
| Regulations, procedures, advisories, policy | `gna/documents/regulations_procedures` | 124 | |
| Reference PDFs | `gna/documents/reference` | 3 | |
| **Application cards** | `gna/applications/<id>.md` | ~2,668 | details, change log, documents citing it |
| **Substation cards** | `gna/substations/<name>.md` | 102 | capacity, margin, bays, applications, lines, assets, change log |
| Weekly snapshots | `gna/snapshots/*.csv` | | raw tables, used to detect changes |

Deliberately **not** included (see `ctuil_site_inventory.csv` for the full site map):
TTC/ATC declarations (reserved for a separate operations dataset), the three
`annual-rolling-plan/*` pages (they load by script and need their own downloader),
formats, annual reports, communication-planning meetings, tenders, and the
login-only Connectivity Monitoring Portal.

## 2. Where things live

| Place | Holds | Role |
|---|---|---|
| **Google Drive folder `db_cmets_meetings`** (Berkeley account; shows on the PC as `G:\My Drive\db_cmets_meetings`) | all data, plus `_system/` (state, health history, search index) | **Master copy of the data.** |
| **This folder** (on the PC) | code only | Working copy for editing and testing. |
| **GitHub repo `paritoshjaiswalUCB/db_ctuil`** (branch `main`) | the same code, the schedule, and the Drive sign-in as the secret `RCLONE_CONF` | **Runs the weekly job.** Holds no data. |

Rule of thumb: Drive is the data, GitHub is the worker, this folder is the workbench.
Change code here, push to GitHub, and the next run uses it.

## 3. The weekly run

`.github/workflows/weekly-update.yml` runs every **Tuesday 11:00 IST**
(cron `30 5 * * 2`), on request (Actions tab, "Run workflow"), and when the workflow
file itself changes. Steps, in order:

1. Install Tesseract, rclone and the Python packages.
2. Write the rclone config from the `RCLONE_CONF` secret.
3. **Pull** the Drive folder to a temporary `kb/` (`rclone copy`).
4. `bot.py`: read `urls.csv`, find new PDFs on each page, convert, write markdown.
5. `gna/application_cards.py`: re-scrape the GNA tables, diff, update cards and change logs.
6. `gna/substation_cards.py`: parse the latest Annex-B/C and RTM/TBCB, update substation cards.
7. `gna/link_documents.py`: tag documents with application IDs, applicants and substations; fill each card's "Mentioned in".
8. `rag/build_index.py`: embed new or changed files into the index (incremental).
9. `check_health.py` (with `site_watch.py`): look for failures, a stalled scraper, new pages on ctuil.in, or pages that suddenly list nothing.
10. **Push** changes back to Drive. Nothing is deleted on Drive except a cleared `NEEDS_ATTENTION.md`.
11. Fail the run if `NEEDS_ATTENTION.md` exists. The run shows as failed in the Actions tab (and GitHub's usual failure email applies if enabled in the owner's notification settings).

A normal run takes about 1-2 hours; the first run after adding many pages took ~2 hours.

### Reading the result
- Green run and no `NEEDS_ATTENTION.md` in the Drive folder: healthy.
- `NEEDS_ATTENTION.md` appears (also in the failed run's log): it says what to look at, e.g. a PDF that failed to convert, a new ctuil.in page, or a page that now lists no documents.
- The first run after `site_watch.py` was added only records a baseline of site pages and will not alert.

### State files (in Drive `_system/`, never edit by hand)
`backfill_state.json`, `run_health.json`, `applications_state.json`,
`substations_state.json`, `site_watch.json`, `rag_index/` (embeddings, chunks, manifest).

## 4. Code map

| File | Purpose |
|---|---|
| `urls.csv` | The list of pages / PDFs to watch (`url,repository`). Add a row to collect a new page. |
| `adapters/ctu.py` | CMETS pages: discover PDFs, extract title / date / reference. |
| `adapters/ctu_gna.py` | GNA and reference pages. `SOURCE_SUBDIRS` maps each page to its output folder. |
| `bot.py` | Download, convert (PDF text, OCR when there is no text layer, tables via pdfplumber), tag, write markdown. |
| `gna/scrape_tables.py` | Scrapes CTU's HTML tables (effectiveness list, GNA status). |
| `gna/substation_tables.py` | Cleans and parses Annex-B / Annex-C / RTM-TBCB PDFs (garbled glyphs, split names, multi-page tables). |
| `gna/linking.py` | Name normalisation shared by the card and linking code. |
| `gna/application_cards.py`, `gna/substation_cards.py`, `gna/link_documents.py` | Cards and cross-links, as in section 3. |
| `rag/build_index.py`, `rag/query.py` | Build and query the index (model `all-MiniLM-L6-v2`). |
| `check_health.py`, `site_watch.py` | Alerts. |
| `ctuil_site_inventory.csv` | Map of every ctuil.in page and why it is or is not collected. |
| `config.py` | Paths. `KB_ROOT` defaults to the Drive folder on this PC; the workflow overrides it with an environment variable. |
| `run_full_backfill.py` | One-off helper from the first historical backfill; not used by the weekly run. |

## 5. Searching it

A Claude Code skill named `regulatory-kb` (in `C:\Users\admin\.claude\skills\regulatory-kb\`) calls
`rag/query.py "<question>" --top-k 8`. Each hit has the text, `source_file`, `full_path`,
`type` (`gna_application` / `gna_substation` for cards), title, date, original URL and a
`needs_review` flag. `needs_review: true` means the file was OCR'd, so check digits
against the source PDF. For an exact lookup, open `gna/applications/<id>.md` or
`gna/substations/<name>.md` directly.

## 6. Secrets and access

- Drive access is an **rclone** config using the owner's own Google OAuth client
  (Google Cloud project `iecc-db-ctuil`, Desktop app, audience Internal, inside the berkeley.edu organisation).
- On GitHub it is the repository secret **`RCLONE_CONF`**. Only the owner can set it; paste the
  contents of `rclone.conf` by hand.
- On this PC the files are in `C:\Users\admin\.regkb\` (`rclone.conf`, `google_client.txt` and older
  leftovers). They are **not** in this repo and **not** in Drive. Never paste their contents into chat or commit them.
- If the Drive sign-in stops working: run `rclone authorize "drive"` on the PC with the client
  id and secret from `google_client.txt`, put the new token into `rclone.conf`, and update the `RCLONE_CONF` secret.

## 7. Rules learned the hard way

- **Run the writing steps in one place only.** If the cards or conversion run on the PC while GitHub
  also runs, Drive ends up with two files of the same name (the Drive app shows one as `name (1).md`) and
  the old one goes stale. This happened once with 583 card files and the state files and was repaired by
  hand. Let GitHub do the writing; use the PC for reading and editing code.
- **Do not write thousands of files to `G:` in a burst.** Google Drive for desktop failed with
  WinError 1450 ("insufficient system resources") on this PC after one.
- Drive is the master: do not delete from it unless you mean to. Deleted files stay in Drive's trash for 30 days.
- CTU file names can contain a bare `&` (for example `Conn&LTA`); HTML parsers turn `&LT` into `<`. Ampersands are escaped before parsing.
- PDFs from CTU often have doubled glyphs and stray repeated labels in tables; `dedupe_chars` (tolerance 0.3) and the repeated-artifact removal in `gna/substation_tables.py` handle that. Do not raise the tolerance: it eats real double letters.
- CTU's own Annex-C has one known discrepancy: the Maharashtra / WR "pipeline non-solar total" is 1,200 MW lower than its rows (the "Others (Nagpur)" row). It is reported by the validator, not a bug here.

## 8. Known limits

- Substation lines and assets come from **text matching** of RTM/TBCB element text; about 40% of the 748 elements matched a substation, so a card can miss lines. Substation COD is a labelled proxy unless CTU reports one.
- 16 Annex-B rows have no application ID, so they appear on the substation card but have no application card.
- The CMETS region coverage table has not been rebuilt since the archive was added.
- Scanned documents (`needs_review: true`) and old Hindi text may have transcription errors.
- A weekly backup of the Drive folder to somewhere the owner controls is not set up (decision deferred).
- TTC/ATC and the three annual-rolling-plan pages are deferred (section 1).

## 9. Day-to-day tasks

| I want to... | Do this |
|---|---|
| Run the update now | GitHub, repo `db_ctuil`, Actions tab, "Weekly update", "Run workflow". |
| Collect another ctuil.in page | Add a row `url,CTU_GNA` to `urls.csv`; for a new output folder add it to `SOURCE_SUBDIRS` in `adapters/ctu_gna.py`; push. |
| Check health | Look for `NEEDS_ATTENTION.md` in the Drive folder; open the latest run in the Actions tab. |
| Change code | Edit here, test with `KB_ROOT` set to a scratch folder (not `G:`), commit and push to `main`. |
| Search | Ask Claude Code a question that mentions CTU / GNA / CMETS (the `regulatory-kb` skill answers it) or run `rag/query.py`. |

## 10. Building a similar database for another source

1. **Map the site first.** Copy `ctuil_site_inventory.csv`'s idea: list every page, what it holds, whether it needs a login, and what you will collect or leave out. Check that a page's documents are in its HTML; script-loaded pages need their own downloader.
2. **Write an adapter** like `adapters/ctu_gna.py` (`discover_pdfs(url)`, an output sub-folder per page). Reuse `bot.py` unchanged.
3. **Pick the unit of a "card".** Here it is the application and the substation: a thing that appears across many documents and changes over time. Keep a state file and a change log per card.
4. **Keep state next to the data** (`_system/` on Drive) so any machine can run the job.
5. **Copy the workflow**, change `DRIVE_PATH`, create a new Google OAuth client and rclone config, and store it as the `RCLONE_CONF` secret of the new repo.
6. **Keep the health check.** The silent failures were the dangerous ones (a scraper that quietly finds nothing).
7. Test the table parsers on a few real files and print validation sums before trusting the output.
