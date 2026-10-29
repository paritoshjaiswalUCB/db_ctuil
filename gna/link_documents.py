"""Link documents, application cards and substation cards to each other.

For every converted document (CMETS minutes at the top of the folder, and the
GNA documents under gna/documents/) this adds three frontmatter lists:

  linked_application_ids  application IDs found in the text
  linked_applicants       applicant names found in the text (matched on the
                          normalised legal name, so "Pvt. Ltd." = "Private Limited")
  linked_substations      substations found in the text (only those with a card)

and then fills the "Mentioned in" section of each application and substation
card with the documents that mention it, newest first. Only files whose tags or
mentions actually changed are rewritten, so unchanged files keep their
timestamp and are not re-indexed.

Legacy application numbers (9-digit, SW99... codes) are too generic to find in
free text, so only the ones already known from the CTU tables are matched.
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from application_cards import MENTIONS_END, MENTIONS_START  # noqa: E402
from config import GNA_DIR, KB_ROOT, SYSTEM_DIR  # noqa: E402
from linking import (  # noqa: E402
    APP_ID_PATTERN, find_known_ids_in_text, normalize_applicant, normalize_text_for_substations, substation_text_pattern,
)

APPLICATION_CARDS = GNA_DIR / "applications"
SUBSTATION_CARDS = GNA_DIR / "substations"
SKIP_NAMES = {"README.md", "NEEDS_ATTENTION.md"}
MAX_TAGS_PER_LIST = 300  # very long lists (e.g. the effectiveness table) are cut in the frontmatter only
MAX_MENTIONS_PER_CARD = 30
MAX_APPLICANT_WORDS = 12


def _slug(key):
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "unnamed"


def split_frontmatter(text):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    try:
        front = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return None, text
    return front, text[end + 5:]


def join_frontmatter(front, body):
    return f"---\n{yaml.safe_dump(front, sort_keys=False, allow_unicode=True)}---\n{body}"


def document_paths():
    for p in sorted(KB_ROOT.rglob("*.md")):
        rel = p.relative_to(KB_ROOT).parts
        if p.name in SKIP_NAMES or rel[0] in ("_system",):
            continue
        if rel[:2] in (("gna", "applications"), ("gna", "substations"), ("gna", "snapshots")):
            continue
        yield p


def _doc_date(front):
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", str(front.get("date") or ""))
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    try:
        return date.fromisoformat(str(front.get("converted_date") or ""))
    except ValueError:
        return date.min


def _title(front, path):
    return re.sub(r"^\d{6,}", "", str(front.get("title") or path.stem)).strip() or path.stem


# ---------------------------------------------------------------- matchers

class Matchers:
    def __init__(self):
        apps_state = json.loads((SYSTEM_DIR / "applications_state.json").read_text(encoding="utf-8"))["applications"]
        self.known_ids = {i for i in apps_state if not i.startswith("NOID-")}
        self.legacy_ids = {i for i in self.known_ids if not APP_ID_PATTERN.fullmatch(i)}
        self.applicant_names = {}
        for s in apps_state.values():
            key = normalize_applicant(s["applicant"])
            if len(key.split()) >= 2:
                self.applicant_names.setdefault(key, s["applicant"])
        self.applicant_last_words = {k.split()[-1] for k in self.applicant_names}

        subs_state = json.loads((SYSTEM_DIR / "substations_state.json").read_text(encoding="utf-8"))["substations"]
        self.substations = {k: v["raw"] for k, v in subs_state.items() if k}
        self.sub_patterns = {k: (substation_text_pattern(k), k.split()[0]) for k in self.substations}

    def application_ids(self, text):
        return sorted(set(APP_ID_PATTERN.findall(text)) | set(find_known_ids_in_text(text, self.legacy_ids)))

    def applicants(self, text):
        words = normalize_applicant(text).split()
        found = set()
        for i, w in enumerate(words):
            if w not in self.applicant_last_words:
                continue
            for n in range(min(MAX_APPLICANT_WORDS, i + 1), 1, -1):  # longest match wins at each end position
                cand = " ".join(words[i - n + 1:i + 1])
                if cand in self.applicant_names:
                    found.add(self.applicant_names[cand])
                    break
        return sorted(found)

    def substations_in(self, text):
        norm = normalize_text_for_substations(text)
        return sorted(k for k, (pat, first) in self.sub_patterns.items() if first in norm and pat.search(norm))


# ---------------------------------------------------------------- tagging

def tag_documents(m):
    """Add/refresh the three tag lists. Returns per-document info for the mentions pass."""
    info, rewritten = [], 0
    for path in document_paths():
        text = path.read_text(encoding="utf-8")
        front, body = split_frontmatter(text)
        if front is None:
            continue
        tags = {
            "linked_application_ids": m.application_ids(body),
            "linked_applicants": m.applicants(body),
            "linked_substations": m.substations_in(body),
        }
        new_front = {k: v for k, v in front.items() if k not in tags}
        for k, v in tags.items():
            if v:
                new_front[k] = v[:MAX_TAGS_PER_LIST]
        if len(tags["linked_application_ids"]) > MAX_TAGS_PER_LIST or len(tags["linked_applicants"]) > MAX_TAGS_PER_LIST:
            new_front["linked_lists_truncated"] = True
        if new_front != front:
            path.write_text(join_frontmatter(new_front, body), encoding="utf-8")
            rewritten += 1
        info.append({"path": path, "title": _title(front, path), "date": _doc_date(front),
                     "date_text": str(front.get("date") or ""), "source": str(front.get("source") or ""), **tags})
    return info, rewritten


# ---------------------------------------------------------------- mentions

def _mention_lines(docs, card_path):
    docs = sorted(docs, key=lambda d: d["date"], reverse=True)
    lines = []
    for d in docs[:MAX_MENTIONS_PER_CARD]:
        rel = os.path.relpath(d["path"], card_path.parent).replace("\\", "/")
        when = d["date_text"] or "undated"
        lines.append(f"- [{d['title']}]({rel}) - {when}")
    if len(docs) > MAX_MENTIONS_PER_CARD:
        lines.append(f"- ...and {len(docs) - MAX_MENTIONS_PER_CARD} older documents")
    return lines


def _write_mentions(card_path, block):
    text = card_path.read_text(encoding="utf-8")
    if MENTIONS_START not in text or MENTIONS_END not in text:
        return False
    head, rest = text.split(MENTIONS_START, 1)
    tail = rest.split(MENTIONS_END, 1)[1]
    new = f"{head}{MENTIONS_START}\n{block}\n{MENTIONS_END}{tail}"
    if new == text:
        return False
    card_path.write_text(new, encoding="utf-8")
    return True


def fill_cards(m, info):
    by_id, by_sub, by_applicant = {}, {}, {}
    for d in info:
        for i in d["linked_application_ids"]:
            by_id.setdefault(i, []).append(d)
        for s in d["linked_substations"]:
            by_sub.setdefault(s, []).append(d)
        for a in d["linked_applicants"]:
            by_applicant.setdefault(normalize_applicant(a), []).append(d)

    changed = 0
    for card in sorted(APPLICATION_CARDS.glob("*.md")):
        front, _ = split_frontmatter(card.read_text(encoding="utf-8"))
        if not front:
            continue
        docs = {id(d): d for d in by_id.get(str(front["application_id"]), [])}
        for other in front.get("also_listed_with_ids") or []:
            docs.update({id(d): d for d in by_id.get(str(other), [])})
        lines = []
        sub_links = [f"[{m.substations[k]}](../substations/{_slug(k)}.md)" for k in front.get("substation_keys") or [] if k in m.substations]
        if sub_links:
            lines.append("Substation: " + ", ".join(sub_links))
        applicant_docs = [d for d in by_applicant.get(front.get("applicant_key", ""), []) if id(d) not in docs]
        lines += ["", "**Documents that cite this application number:**"] if lines else ["**Documents that cite this application number:**"]
        lines += _mention_lines(docs.values(), card) if docs else ["- None found."]
        if applicant_docs:
            lines += ["", "**Other documents that name the applicant (no application number):**"] + _mention_lines(applicant_docs, card)
        changed += _write_mentions(card, "\n".join(lines))

    for card in sorted(SUBSTATION_CARDS.glob("*.md")):
        front, _ = split_frontmatter(card.read_text(encoding="utf-8"))
        if not front:
            continue
        docs = by_sub.get(front["substation_key"], [])
        changed += _write_mentions(card, "\n".join(_mention_lines(docs, card) if docs else ["- None found."]))
    return changed


def run():
    m = Matchers()
    info, rewritten = tag_documents(m)
    cards_changed = fill_cards(m, info)
    return {"documents_scanned": len(info), "documents_retagged": rewritten, "cards_updated": cards_changed,
            "documents_with_application_ids": sum(1 for d in info if d["linked_application_ids"]),
            "documents_with_applicants": sum(1 for d in info if d["linked_applicants"]),
            "documents_with_substations": sum(1 for d in info if d["linked_substations"])}


if __name__ == "__main__":
    print(run())
