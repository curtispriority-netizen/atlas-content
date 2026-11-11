"""Stage verified Atlas cards and optionally publish them to the existing feed."""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from preflight import EDITION_MAX, ROOT, PDF_NAME, edition_items, read_json, weekly_work
from research_appraisal import (PRIMARY, SCREEN, BudgetExceeded, OpenAI, apply_edits, brief, digest, evaluate, object_schema,
                                reading_schema, strip_refs)
from card_style import check_card
from research_review import LABELS, render_review, review_entry
from research_sources import (
    MAX_SOURCE, Drive, Literature, Review, Unavailable, doi, extract_pdf, pmid,
    render_passages, sufficient_full_text, title_key,
)

PAPER_ID = re.compile(r"[A-Za-z0-9-]{1,80}")
UNREVIEWED = "not clinician-reviewed"
REVIEWED = "approved by a clinician before publication"
SCREEN_SCHEMA = object_schema({"ranked": {"type": "array", "items": {"type": "integer"}}})


class SourceUnavailable(Unavailable):
    pass


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def git(root, *args):
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    except subprocess.CalledProcessError:
        raise Unavailable(f"git_{args[0]}_failed") from None


CHALLENGE_LABEL = "Challenge pick: chosen to test usual physio practice."


class Journal:
    def __init__(self, path, persist=lambda: None):
        self.path = path
        self.persist = persist
        self.data = read_json(path) if path.exists() else {
            "charges": [], "jobs": {}, "holds": {}, "runs": {},
        }

    def save(self):
        write_json(self.path, self.data)
        self.persist()

    def expire_validation_failures(self, now):
        cutoff = now - timedelta(days=30)
        for job in self.data["jobs"].values():
            failures = job.get("validation_failures", {})
            for phase, failure in list(failures.items()):
                if datetime.fromisoformat(failure["checked_at"]) <= cutoff:
                    del failures[phase]
            if not failures:
                job.pop("validation_failures", None)


class Feed:
    def __init__(self, root):
        self.root = root
        self.papers = read_json(root / "papers/index.json")
        self.editions = read_json(root / "index.json")
        self.shown = read_json(root / "shown.json")
        self.paper_ids = {row["id"] for row in self.papers["papers"]}
        self.keys = {row["key"].lower() for row in self.shown["shown"]}
        self.titles = set()
        for index in (self.papers["papers"], self.editions["editions"]):
            for entry in index:
                for source in read_json(root / entry["path"])["sources"]:
                    self.keys.update(k for k in (doi(source.get("doi")), pmid(source.get("pmid")),
                                                 doi(source.get("url"))) if k)
                    if match := re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", source.get("url", "")):
                        self.keys.add(match[1])
                if entry.get("title"):
                    self.titles.add(title_key(entry["title"]))
        for row in self.shown["shown"]:
            if row.get("title"):
                self.titles.add(title_key(row["title"]))

    def duplicate(self, record):
        return bool(self.keys & record.keys) or title_key(record.title) in self.titles

    def publish(self, papers, studies, day, timestamp, reviewed=True, quiet=False):
        changed = []
        for paper_id, draft in papers:
            path = f"papers/{paper_id}.json"
            if (self.root / path).exists() or paper_id in self.paper_ids:
                raise Unavailable("paper_already_exists")
            write_json(self.root / path, {"schema_version": 1, "paper_id": paper_id,
                                        "added_at": draft["added_at"], "study": draft["study"],
                                        "sources": [draft["citation"]]})
            self.papers["papers"].append({"id": paper_id, "path": path,
                                          "added_at": draft["added_at"], "title": draft["study"]["title"]})
            changed.append(path)
        if papers:
            write_json(self.root / "papers/index.json", self.papers)
            changed.append("papers/index.json")
        if studies:
            # A later run in the same ISO week adds its cards to that week's edition.
            fresh = weekly_work(self.root, day)
            year, week, _ = day.isocalendar()
            edition_id = f"{year}-W{week:02}"
            path = f"editions/{edition_id}.json"
            edition = None if fresh else read_json(self.root / path)
            cards, sources = ([], []) if fresh else (edition["items"], edition["sources"])
            if len(cards) + len(studies) > EDITION_MAX:
                raise Unavailable("edition_full")
            taken = {card["item_id"] for card in cards} | {source["id"] for source in sources}
            number = len(cards)
            for record, draft in studies:
                number += 1
                while f"{edition_id}-{number}" in taken or f"study-{number}" in taken:
                    number += 1
                card, source = copy.deepcopy((draft["study"], draft["citation"]))
                card["item_id"] = f"{edition_id}-{number}"
                source["id"] = f"study-{number}"
                card["source_ids"] = [source["id"]]
                cards.append(card)
                sources.append(source)
                self.shown["shown"].append({"key": record.doi or record.pmid, "title": record.title,
                                            "edition": edition_id, "where": "items"})
            verdict = f"{len(cards)} full-text " + ("study" if len(cards) == 1 else "studies") + (
                ": drafted by one AI reading with numbers checked against the source, then approved by a "
                "clinician before publication. Not independently verified by a second reader." if reviewed else
                ": AI-appraised, then checked by a second AI model and by code against the source. "
                "AI-checked, not clinician-reviewed. Treat judgements as a first draft.")
            if fresh:
                write_json(self.root / path, {"schema_version": 1, "edition_id": edition_id,
                           "published_at": timestamp, "verdict": verdict, "quiet_week": quiet,
                           "items": cards, "on_the_radar": [], "filtered_out": [], "sources": sources})
                self.editions["editions"].append({"id": edition_id, "path": path,
                                                 "published_at": timestamp, "item_count": len(cards),
                                                 "verdict": verdict})
            else:
                verdict = (f"{len(cards)} full-text studies. Each card states how it was checked: "
                           "AI-checked unless it says a clinician approved it.")
                edition.update(published_at=timestamp, verdict=verdict,
                               quiet_week=bool(edition.get("quiet_week") or quiet))
                write_json(self.root / path, edition)
                (entry,) = [row for row in self.editions["editions"] if row["id"] == edition_id]
                entry.update(published_at=timestamp, item_count=len(cards), verdict=verdict)
            self.editions["latest"] = max(entry["id"] for entry in self.editions["editions"])
            write_json(self.root / "index.json", self.editions)
            write_json(self.root / "shown.json", self.shown)
            changed.extend((path, "index.json", "shown.json"))
        return changed


class Pipeline:
    def __init__(self, feed, journal, literature, drive, model, now, run, config,
                 anchors=None, retry_holds=False):
        self.feed, self.journal, self.literature = feed, journal, literature
        self.drive, self.model, self.now, self.run = drive, model, now, run
        self.today = now.astimezone(ZoneInfo("Australia/Hobart")).date()
        self.config, self.anchors, self.retry_holds = config, anchors or {}, retry_holds
        self.staged, self.seen, self.ids = [], set(), set()
        self.retried = set()
        self.attempts = 0
        self.max_new, self.backfilled = None, False
        self.rules = "\n\n".join((feed.root / name).read_text(encoding="utf-8")
                                  for name in ("APPRAISAL.md", "CARD_STYLE.md"))
        defs = read_json(feed.root / "schema/edition.schema.json")["$defs"]
        self.schema = reading_schema(defs)
        study = copy.deepcopy(defs["study"])
        study["properties"]["item_id"]["pattern"] = r"^mine-[A-Za-z0-9-]+$"
        self.card_validator = Draft202012Validator({**study, "$defs": defs})
        self.protocol = digest([self.rules, self.schema,
                                (Path(__file__).parent / "research_appraisal.py").read_text(encoding="utf-8")])

    def hold(self, key, reason, subject, notify=True):
        previous = self.journal.data["holds"].get(key, {})
        if previous.get("reason") != reason:
            self.journal.data["holds"][key] = {
                "reason": reason, "subject": subject, "first_seen": self.now.isoformat(),
            }
            self.run["new_holds"] += int(notify)
            self.run["new_exclusions"] = self.run.get("new_exclusions", 0) + int(not notify)
            self.journal.save()

    def candidate(self, record, paper_id, source, added_at, anchor=None, benchmark=False):
        if not benchmark and self.feed.duplicate(record):
            if paper_id and paper_id not in self.feed.paper_ids:
                raise Review("duplicate_with_different_atlas_id")
            return
        if record.keys & self.seen or (paper_id and paper_id in self.ids):
            if paper_id and paper_id not in self.ids:
                raise Review("duplicate_with_different_atlas_id")
            return
        try:
            level, url, article = source()
        except Unavailable as error:
            raise SourceUnavailable(str(error)) from None
        if benchmark and level != "full_text":
            raise Review("benchmark_full_text_required")
        if level != "full_text":
            # Abstract-only sources can never be published, so do not pay to appraise them.
            raise Review("abstract_only")
        if len(article.encode()) > MAX_SOURCE:
            raise Review("full_source_too_large")
        fingerprint = digest([record.doi or record.pmid, level, url, article, self.protocol])
        job = self.journal.data["jobs"].setdefault(fingerprint, {
            "record": {"doi": record.doi, "pmid": record.pmid, "title": record.title},
            "source": {"level": level, "url": url, "text_sha256": sha256(article.encode()).hexdigest()},
        })
        review = self.journal.data.setdefault("review", {})
        if fingerprint in review:
            return
        if self.retry_holds and fingerprint not in self.retried and job.get("hold"):
            for field in ("primary", "hold", "hold_fields"):
                job.pop(field, None)
            self.retried.add(fingerprint)
            self.journal.save()
        if job.get("hold"):
            raise Review(job["hold"])
        try:
            payload = {"record": {key: getattr(record, key) for key in ("doi", "pmid", "title", "citation")},
                       "source_level": level, "article": render_passages(article)}
            reading = evaluate(self.model, fingerprint, job, self.journal, article,
                               payload, self.rules, self.schema, bool(self.config.get("critic")),
                               lambda draft: self.valid(self.finish(draft, level, record, paper_id)))
        except Review as error:
            job["hold"] = str(error)
            if error.fields:
                job["hold_fields"] = error.fields
            self.journal.save()
            raise
        finally:
            if benchmark:
                self.run["benchmark_checks"] = {"reading_passed_checks": "primary" in job,
                                                "warnings": len(job.get("warnings") or [])}
                failure = (job.get("validation_failures") or {}).get("primary") or {}
                if "primary" not in job and failure:
                    # Check names only; no model text reaches the public log.
                    self.run["benchmark_checks"]["held_by"] = ",".join(sorted(
                        {row["reason"] for row in failure.get("all_problems") or []} or {failure["reason"]}))
        card = self.finish(reading["card"], level, record, paper_id)
        card["appraisal"]["verification"] = self.verification(job, anchor)
        if not self.card_validator.is_valid(card):
            raise Review("atlas_card_schema")
        citation = {"id": "paper", "citation": record.citation, "verified": True, "url": record.url}
        citation.update({key: value for key, value in (("doi", record.doi), ("pmid", record.pmid)) if value})
        entry = review_entry(fingerprint, record, paper_id, card, citation, added_at, reading, article, self.now)
        entry["warnings"] = job.get("warnings") or []
        entry["critic"] = job.get("critic") or {}
        entry["benchmark"] = benchmark
        review[fingerprint] = entry
        self.staged.append(fingerprint)
        self.ids.add(paper_id) if paper_id else None
        self.seen.update(record.keys)
        self.journal.save()

    def valid(self, card):
        card["appraisal"]["verification"] = {"status": "not checked"}
        return self.card_validator.is_valid(card)

    def finish(self, draft, level, record, paper_id):
        """The published shape of a reading's card, without verification."""
        card = strip_refs(draft)
        card.update(type="study", format=2, item_id=f"mine-{paper_id or 'weekly'}",
                    read_level=level, source_ids=["paper"],
                    full_text={"status": "read", "worth_getting": False,
                               "why": "Read the full source by AI; numbers checked against it."})
        if record.published:
            card["first_published"] = record.published
        return card

    def verification(self, job, anchor):
        """What was checked, and every open issue, shown on the card itself."""
        critic = job.get("critic") or {}
        done = critic.get("status") == "done"
        rows = [{"item": brief(edit["path"].replace("_", " "), 8), "first": edit["before"],
                 "second": edit["after"], "why": edit["why"], "resolved": "Second reader's version used."}
                for edit in critic.get("kept", [])]
        rows += [{"item": "Second reader's concern", "first": "Not raised in the first reading",
                  "second": "Raised by the second reader", "why": concern, "resolved": "left open"}
                 for concern in critic.get("concerns", [])]
        rows += [{"item": "Automated check", "first": row["check"].replace("_", " "),
                  "second": brief(", ".join(row["fields"]), 12),
                  "why": brief(LABELS.get(row["check"], "An automated check flagged this card."), 25),
                  "resolved": "left open"}
                 for row in job.get("warnings") or [] if row["check"] != "card_style_validation_failed"]
        result = {"status": "disagreements" if rows else "agrees" if done else "not checked",
                  "checked_by": ("AI critic pass by a second model (full text); " if done else "one AI reading; ")
                                + "every number checked against the passages it cites; " + UNREVIEWED,
                  "checked_at": self.now.isoformat(), "disagreements": rows, "external": [],
                  "external_searched": "External ratings not searched for this automated edition."}
        if anchor:
            result.update(external=anchor["external"], external_searched=anchor["external_searched"])
        return result

    def attempt(self, key, subject, action, static=False, discovered=False):
        if self.attempts >= self.config["max_candidates"]:
            return
        if static and key in self.journal.data["holds"] and not self.retry_holds:
            return
        self.attempts += 1
        try:
            action()
        except SourceUnavailable as error:
            self.run.setdefault("source_errors", {})[key] = {"reason": str(error), "subject": subject}
        except Review as error:
            reason = str(error)
            # Papers the search found are skipped quietly. Only papers the clinician added notify.
            self.hold(key, reason, subject, notify=not discovered)
        else:
            if key in self.journal.data["holds"]:
                del self.journal.data["holds"][key]
                self.journal.save()

    def queue(self):
        for entry in self.drive.entries():
            if self.attempts >= self.config["max_candidates"]:
                break
            name = entry["name"]
            key = digest([entry["id"], entry.get("md5Checksum"), name, self.protocol])
            if name.startswith("queue__") and name.endswith(".txt"):
                try:
                    marker = json.loads(self.drive.download(entry, 256_000))
                    if marker.get("kind") != "queue" or not isinstance(marker.get("papers"), list):
                        raise Review("invalid_queue_marker")
                except (UnicodeError, ValueError, AttributeError, Review):
                    self.hold(key, "invalid_queue_marker", name)
                    continue
                if marker.get("question_id"):
                    # The legacy question job owns these, including linked answers.
                    continue
                for paper in marker["papers"]:
                    if not isinstance(paper, dict):
                        self.hold(key, "invalid_queue_paper", name)
                        continue
                    paper_id = paper.get("id")
                    if not isinstance(paper_id, str) or not PAPER_ID.fullmatch(paper_id):
                        self.hold(key, "invalid_queue_paper_id", name)
                        continue
                    if paper_id in self.feed.paper_ids or paper_id in self.ids:
                        continue
                    if self.attempts >= self.config["max_candidates"]:
                        break
                    self.queue_paper(paper, key)
            elif match := PDF_NAME.fullmatch(name):
                paper_id = match[1]
                if (PAPER_ID.fullmatch(paper_id) and paper_id not in self.feed.paper_ids
                        and paper_id not in self.ids):
                    # Revisit anchor holds when the private review file changes.
                    self.attempt(digest([key, digest(self.anchors)]), name,
                                 lambda: self.pdf(entry, paper_id, name[10:-4]), static=True)

    def queue_paper(self, paper, marker_key):
        key = digest([marker_key, paper["id"]])
        if any(paper.get(field) is not None and not isinstance(paper[field], (str, int))
               for field in ("doi", "pmid", "url", "title")):
            self.hold(key, "invalid_queue_paper_metadata", paper["id"])
            return
        try:
            record = self.literature.identify(doi(str(paper.get("doi") or paper.get("url") or "")),
                                              pmid(paper.get("pmid")), str(paper.get("title") or ""))
        except Unavailable as error:
            self.run.setdefault("source_errors", {})[key] = {"reason": str(error), "subject": paper["id"]}
            return
        except Review as error:
            self.hold(key, str(error), paper["id"])
            return
        self.attempt(digest([key, self.record_key(record)]), record.doi or record.pmid, lambda: self.candidate(
            record, paper["id"], lambda: self.literature.source(record), self.now.isoformat()),
            static=True)

    def record_key(self, record):
        return digest([record.doi or record.pmid, record.pmcid, record.open_access,
                       record.abstract, record.revision, self.protocol])

    def pdf(self, entry, paper_id, title):
        pdf_bytes = self.drive.download(entry, 25_000_000)
        article = extract_pdf(pdf_bytes)
        if not sufficient_full_text(article):
            raise Review("insufficient_pdf_text")
        first_page = article.split("\f")[0]
        record = self.literature.identify(doi(first_page), "", title)
        if len(record.title) < 12 or title_key(record.title) not in title_key(first_page):
            raise Review("pdf_identity_mismatch")
        if not record.open_access and not self.config.get("allow_paywalled_uploads"):
            raise Review("pdf_open_access_unconfirmed")
        # An external-ratings review is optional. When one exists for this exact PDF it is shown on the card.
        anchor = self.anchors.get(record.doi or record.pmid)
        if (not anchor or not anchor.get("reviewed_by") or not anchor.get("external_searched")
                or not isinstance(anchor.get("external"), list)
                or anchor.get("pdf_sha256") != sha256(pdf_bytes).hexdigest()):
            anchor = None
        self.candidate(record, paper_id, lambda: ("full_text", record.url, article),
                       self.now.isoformat(), anchor=anchor)

    def discover(self):
        target = min(self.config.get("weekly_target", 3), EDITION_MAX)
        have = edition_items(self.feed.root, self.today)
        room = target - have if self.max_new is None else min(target - have, self.max_new)
        if room <= 0:
            return
        pending = self.journal.data.setdefault("pending_discovery", {})
        resumed = []
        for key, (paper_doi, paper_pmid) in list(pending.items())[:self.config["max_candidates"]]:
            if {paper_doi, paper_pmid} & self.feed.keys:
                del pending[key]
                continue
            try:
                resumed.append(self.literature.identify(paper_doi, paper_pmid, ""))
            except Unavailable as error:
                self.run.setdefault("source_errors", {})[key] = {"reason": str(error), "subject": key}
            except Review as error:
                del pending[key]
                self.hold(key, str(error), key)
        start, attempted = len(self.staged), set()

        def usable(records):
            return [record for record in records
                    if record.keys and not record.excluded and not self.feed.duplicate(record)]

        def work(records, limit):
            for record in records:
                if len(self.staged) - start >= limit or self.attempts >= self.config["max_candidates"]:
                    break
                if (not record.keys or record.excluded or self.feed.duplicate(record)
                        or record.keys & (self.seen | attempted)):
                    continue
                attempted.update(record.keys)
                key = self.record_key(record)
                self.attempt(key, record.doi or record.pmid,
                             lambda: self.discovery_candidate(record, pending), static=True, discovered=True)

        self.challenge(usable, work, min(self.config.get("challenge_slots", 0), room))
        found = usable(self.literature.discover(self.config["query"], self.today,
                                                self.config["lookback_days"], self.config["search_limit"]))
        work((*resumed, *sorted(self.screen(found), key=lambda r: not r.open_access)), room)
        # Quiet week: top up to the weekly minimum with older, well-cited open-access papers not shown before.
        floor = min(self.config.get("weekly_minimum", 0) - have, room)
        if (len(self.staged) - start >= floor or not self.config.get("backfill_days")
                or self.attempts >= self.config["max_candidates"]):
            return
        try:
            older = usable(self.literature.backfill(self.config["query"], self.today, self.config["lookback_days"],
                                                    self.config["backfill_days"], self.config["search_limit"]))
        except Unavailable:
            return
        before = len(self.staged)
        work(self.screen(older, "backfill"), floor)
        self.run["backfill"] = {"offered": len(older), "staged": len(self.staged) - before}
        self.backfilled = len(self.staged) > before

    def challenge(self, usable, work, slots):
        """Reserved slots for papers picked to test the clinician's habits, from a wider search than the main one.
        Skipped unless the screen ranked them: an unranked paper is not a challenge pick."""
        if slots <= 0 or not self.config.get("challenge_query") or not self.config.get("challenge_focus"):
            return
        try:
            found = usable(self.literature.discover(
                self.config["challenge_query"], self.today,
                self.config.get("challenge_days", self.config["lookback_days"]), self.config["search_limit"]))
        except Unavailable:
            return
        ranked = self.screen(found, "challenge", self.config["challenge_focus"])
        if "screened_challenge" not in self.run:
            return
        before = len(self.staged)
        work(ranked, slots)
        for fingerprint in self.staged[before:]:
            card = self.journal.data["review"][fingerprint]["card"]
            note = card.get("evidence_note") or []
            words = " ".join([note] if isinstance(note, str) else note).split()
            card["evidence_note"] = " ".join(CHALLENGE_LABEL.split() + words[:30 - len(CHALLENGE_LABEL.split())])
        self.run["challenge"] = {"offered": len(found), "staged": len(self.staged) - before}

    def screen(self, records, tag=None, focus=None):
        """One cheap call ranks abstracts against the clinician's focus. Any failure keeps the search order."""
        focus = focus or self.config.get("focus")
        if not focus or len(records) < 2:
            return records
        papers = [{"n": n, "title": r.title, "types": r.publication_types, "abstract": r.abstract[:1800]}
                  for n, r in enumerate(records)]
        try:
            reply = self.model.generate(digest(["screen", self.today.isoformat(), *([tag] if tag else [])]), PRIMARY, "screen", SCREEN,
                                        {"focus": focus, "papers": papers}, SCREEN_SCHEMA, 3000)
        except (Review, Unavailable):
            return records
        order = list(dict.fromkeys(n for n in reply["ranked"] if 0 <= n < len(records)))
        self.run["screened_" + tag if tag else "screened"] = {"offered": len(records), "kept": len(order)}
        return [records[n] for n in order]

    def discovery_candidate(self, record, pending):
        key = record.doi or record.pmid
        pending[key] = [record.doi, record.pmid]
        try:
            self.candidate(record, None, lambda: self.literature.source(record), self.now.isoformat())
        except Review:
            pending.pop(key)
            raise

    def process(self):
        # Leave capacity for weekly search even if the Drive queue has a backlog.
        limit = self.config["max_candidates"]
        self.config["max_candidates"] = max(1, limit - max(3, self.config.get("weekly_minimum", 0)))
        try:
            self.queue()
        finally:
            self.config["max_candidates"] = limit
        self.discover()

    def benchmark(self, paper_pmid):
        record = self.literature.identify("", paper_pmid, "")
        self.attempt(
            digest(["benchmark", self.record_key(record)]), paper_pmid,
            lambda: self.candidate(record, None, lambda: self.literature.source(record),
                                   self.now.isoformat(), benchmark=True),
        )


def approved(journal, ids, edits=None, reviewed=True):
    """Pending review entries matching the approved fingerprints, as (papers, studies) ready to publish."""
    edits = edits or {}
    for prefix in edits:
        if not any(fingerprint.startswith(prefix) or prefix.startswith(fingerprint) for fingerprint in ids):
            raise Unavailable("card_edits_for_unapproved_id")
    papers, studies = [], []
    for fingerprint in ids:
        found = [entry for key, entry in journal.data.get("review", {}).items() if key.startswith(fingerprint)]
        if len(found) != 1 or found[0]["status"] != "pending":
            raise Unavailable("promote_id_not_pending_or_ambiguous")
        entry = found[0]
        card = entry["card"]
        for prefix, changes in edits.items():
            if entry["fingerprint"].startswith(prefix):
                if not isinstance(changes, dict) or not changes:
                    raise Unavailable("card_edit_path_invalid")
                card = apply_edits(card, changes)
                entry["edits"] = changes
        verification = (card.get("appraisal") or {}).get("verification")
        if reviewed and verification:
            card = copy.deepcopy(card)
            verification = card["appraisal"]["verification"]
            verification["checked_by"] = verification.get("checked_by", "").replace(UNREVIEWED, REVIEWED) + (
                "; edited by the clinician" if entry.get("edits") else "")
        draft = {"study": card, "citation": entry["citation"], "added_at": entry["added_at"]}
        if entry["paper_id"]:
            papers.append((entry["paper_id"], draft))
        else:
            studies.append((SimpleNamespace(**entry["record"]), draft))
    return papers, studies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stage", "promote"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--retry-holds", action="store_true")
    parser.add_argument("--benchmark-pmid")
    parser.add_argument("--max-new", type=int, help="Cap on search-found cards for this run")
    parser.add_argument("--promote-ids", default="", help="Comma-separated fingerprint prefixes (12+ characters)")
    parser.add_argument("--card-edits", default="", help="JSON: {fingerprint prefix: {path: new value}}")
    args = parser.parse_args()
    if args.benchmark_pmid and (args.mode != "stage" or not pmid(args.benchmark_pmid)):
        parser.error("benchmark requires stage mode and a numeric PMID")
    ids = []
    if args.mode == "promote":
        ids = [part.strip() for part in args.promote_ids.split(",") if part.strip()]
        if (not ids or any(len(part) < 12 or not re.fullmatch(r"[a-f0-9]+", part) for part in ids)
                or os.environ.get("ATLAS_PUBLISH_ENABLED") != "true"
                or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or os.environ.get("GITHUB_REF") != "refs/heads/main"):
            raise Unavailable("publication_gate_closed")
    root = ROOT
    state = args.state.resolve()
    now = datetime.now(timezone.utc)
    run_id = f'{os.environ["GITHUB_RUN_ID"]}.{os.environ["GITHUB_RUN_ATTEMPT"]}'
    run = {"started_at": now.isoformat(), "mode": args.mode, "status": "running", "new_holds": 0}
    if args.benchmark_pmid:
        run["benchmark_pmid"] = args.benchmark_pmid
    if git(state, "rev-parse", "--show-toplevel").lower() != state.as_posix().lower():
        raise Unavailable("invalid_private_state_checkout")

    def persist():
        git(state, "add", "--", "research-state.json", "reviews")
        if git(state, "diff", "--cached", "--name-only"):
            git(state, "commit", "-m", "Checkpoint Atlas research")
            git(state, "push", "origin", "HEAD:refs/heads/main")

    (state / "reviews").mkdir(exist_ok=True)
    (state / "reviews" / ".keep").touch()
    journal = Journal(state / "research-state.json", persist)
    journal.expire_validation_failures(now)
    journal.data["runs"][run_id] = run
    problem = None
    try:
        config = read_json(root / "scripts/research_config.json")
        anchors = state / "anchors.json"
        model = OpenAI(journal, run_id, None,
                       int(os.environ.get("ATLAS_RUN_MICRO_USD") or "3000000"),
                       int(os.environ.get("ATLAS_MONTH_MICRO_USD") or "15000000"))
        if model.run_limit <= 0 or model.month_limit <= 0:
            raise Unavailable("invalid_budget")
        feed = Feed(root)
        drive = None if args.benchmark_pmid or args.mode == "promote" else Drive(
            os.environ["ATLAS_DRIVE_READER_JSON"], os.environ["ATLAS_DRIVE_FOLDER_ID"])
        pipeline = Pipeline(feed, journal, Literature(), drive, model, now, run, config,
                            read_json(anchors) if anchors.exists() else {}, args.retry_holds)
        pipeline.max_new = args.max_new
        changed, reviewed = [], True
        if args.mode == "promote":
            try:
                edits = json.loads(args.card_edits) if args.card_edits.strip() else {}
            except ValueError:
                raise Unavailable("card_edits_not_json") from None
            if not isinstance(edits, dict):
                raise Unavailable("card_edits_not_json")
            papers, studies = approved(journal, ids, edits)
            run["verified"] = len(papers) + len(studies)
            changed = feed.publish(papers, studies, pipeline.today, now.isoformat())
        else:
            try:
                if args.benchmark_pmid:
                    pipeline.benchmark(args.benchmark_pmid)
                else:
                    pipeline.process()
            except BudgetExceeded:
                run["reason"] = "run_or_month_budget"
            run["staged"] = len(pipeline.staged)
            for fingerprint in pipeline.staged:
                path = state / "reviews" / f"{fingerprint[:12]}.md"
                path.write_text(render_review(journal.data["review"][fingerprint]), encoding="utf-8")
            # Hands-off publishing: cards that meet the style rules go out labelled as AI-checked.
            # Cards that do not stay staged for the clinician. Benchmarks are never published here.
            if (config.get("auto_publish") and not args.benchmark_pmid
                    and os.environ.get("ATLAS_PUBLISH_ENABLED") == "true"
                    and os.environ.get("GITHUB_REF") == "refs/heads/main"):
                ids = [fingerprint for fingerprint in pipeline.staged
                       if not check_card("card", journal.data["review"][fingerprint]["card"])]
                if ids:
                    papers, studies = approved(journal, ids, reviewed=False)
                    run["verified"] = len(papers) + len(studies)
                    reviewed = False
                    changed = feed.publish(papers, studies, pipeline.today, now.isoformat(), reviewed=False,
                                           quiet=pipeline.backfilled)
        if changed:
            # No model-supplied content is printed in the public Actions log.
            if subprocess.run([sys.executable, "-X", "utf8", str(root / "scripts/validate.py")],
                              capture_output=True).returncode:
                raise Unavailable("atlas_schema_or_style_validation_failed")
            git(root, "add", "--", *changed)
            git(root, "commit", "-m", "Publish clinician-approved Atlas research" if reviewed
                else "Publish AI-checked Atlas research")
            git(root, "push", "origin", "HEAD:refs/heads/main")
            run["published"] = run["verified"]
            run["publication_commit"] = git(root, "rev-parse", "HEAD")
            for fingerprint in ids:
                for key, entry in journal.data["review"].items():
                    if key.startswith(fingerprint):
                        entry.update(status="published", published_at=now.isoformat(), reviewed=reviewed,
                                     publication_commit=run["publication_commit"])
        if run.get("source_errors"):
            run["status"] = "partial"
        elif run.get("reason") == "run_or_month_budget":
            run["status"] = "budget_limited"
        else:
            waiting = run.get("staged", 0) - (0 if reviewed else run.get("published", 0))
            run["status"] = "needs_review" if run["new_holds"] or waiting > 0 else "complete"
    except Unavailable as error:
        run.update(status="failed", reason=str(error))
        problem = error
    except Exception as error:
        run.update(status="failed", reason=type(error).__name__)
        problem = error
    if args.benchmark_pmid:
        charges = [charge for charge in journal.data["charges"] if charge["run"] == run_id]
        run["recorded_cost_micro_usd"] = sum(row["cost"] for row in charges if "input_tokens" in row)
        run["unconfirmed_cost_micro_usd"] = sum(row["cost"] for row in charges if "input_tokens" not in row)
    journal.save()
    if run.get("publication_commit") and os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            print(f"published={run['publication_commit']}", file=output)
    print(f"Atlas research {run['status']}. Staged for review: {run.get('staged', 0)}. "
          f"Published: {run.get('published', 0)}. New holds: {run['new_holds']}.")
    if checks := run.get("benchmark_checks"):
        print("Benchmark gates passed: " + ", ".join(f"{name}={passed}" for name, passed in checks.items()))
    if args.benchmark_pmid:
        print(f"Benchmark estimated usage: US${run['recorded_cost_micro_usd'] / 1_000_000:.6f}. "
              f"Unconfirmed reserve: US${run['unconfirmed_cost_micro_usd'] / 1_000_000:.6f}.")
    # Fail (and so notify) only for a broken run, a held paper the clinician added, or the budget.
    if problem or run["new_holds"] or run["status"] == "budget_limited":
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Unavailable, ValueError):
        sys.exit("Atlas research could not start or checkpoint. Check workflow setup and private state.")
