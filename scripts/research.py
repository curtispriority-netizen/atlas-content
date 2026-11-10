"""Stage verified Atlas cards and optionally publish them to the existing feed."""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from preflight import ROOT, PDF_NAME, read_json, weekly_work
from research_appraisal import BudgetExceeded, OpenAI, digest, evaluate, reading_schema
from research_sources import (
    MAX_SOURCE, Drive, Literature, Review, Unavailable, doi, extract_pdf, pmid,
    sufficient_full_text, title_key,
)

PAPER_ID = re.compile(r"[A-Za-z0-9-]{1,80}")


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

    def publish(self, papers, studies, day, timestamp):
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
            if not weekly_work(self.root, day):
                raise Unavailable("edition_already_exists")
            year, week, _ = day.isocalendar()
            edition_id = f"{year}-W{week:02}"
            path = f"editions/{edition_id}.json"
            cards, sources = [], []
            for number, (record, draft) in enumerate(studies, 1):
                card, source = copy.deepcopy((draft["study"], draft["citation"]))
                card["item_id"] = f"{edition_id}-{number}"
                source["id"] = f"study-{number}"
                card["source_ids"] = [source["id"]]
                cards.append(card)
                sources.append(source)
                self.shown["shown"].append({"key": record.doi or record.pmid, "title": record.title,
                                            "edition": edition_id, "where": "items"})
            verdict = f"{len(cards)} independently checked full-text " + (
                "study" if len(cards) == 1 else "studies"
            ) + " in this edition."
            write_json(self.root / path, {"schema_version": 1, "edition_id": edition_id,
                       "published_at": timestamp, "verdict": verdict, "quiet_week": False,
                       "items": cards, "on_the_radar": [], "filtered_out": [], "sources": sources})
            self.editions["editions"].append({"id": edition_id, "path": path,
                                             "published_at": timestamp, "item_count": len(cards),
                                             "verdict": verdict})
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
        self.papers, self.studies, self.seen, self.ids = [], [], set(), set()
        self.retried = set()
        self.attempts = 0
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
        if len(article.encode()) > MAX_SOURCE:
            raise Review("full_source_too_large")
        fingerprint = digest([record.doi or record.pmid, level, url, article, self.protocol])
        job = self.journal.data["jobs"].setdefault(fingerprint, {
            "record": {"doi": record.doi, "pmid": record.pmid, "title": record.title},
            "source": {"level": level, "url": url, "text_sha256": sha256(article.encode()).hexdigest()},
        })
        if self.retry_holds and fingerprint not in self.retried and (
            job.get("hold") or (job.get("decision") and not job["decision"]["agrees"])
        ):
            for field in ("primary", "blind_verification", "decision", "hold", "hold_fields"):
                job.pop(field, None)
            self.retried.add(fingerprint)
            self.journal.save()
        if job.get("hold"):
            raise Review(job["hold"])
        try:
            payload = {"record": {key: getattr(record, key) for key in ("doi", "pmid", "title", "citation")},
                       "source_level": level, "article": article}
            decision = evaluate(self.model, fingerprint, job, self.journal, article,
                                payload, self.rules, self.schema)
        except Review as error:
            job["hold"] = str(error)
            if error.fields:
                job["hold_fields"] = error.fields
            self.journal.save()
            raise
        if not decision["agrees"] or decision["disagreements"]:
            raise Review("material_appraisal_disagreement")
        if level != "full_text":
            raise Review("abstract_only")
        verification = {"status": "agrees", "checked_by": "independent second pass (full text)",
                        "checked_at": self.now.isoformat(), "disagreements": [], "external": [],
                        "external_searched": "External ratings not searched for this automated edition."}
        if anchor:
            verification.update(external=anchor["external"], external_searched=anchor["external_searched"])
        card = copy.deepcopy(job["primary"]["card"])
        card.update(type="study", format=2, item_id=f"mine-{paper_id or 'weekly'}",
                    read_level=level, source_ids=["paper"],
                    full_text={"status": "read", "worth_getting": False,
                               "why": "Read the full source in two independent passes."})
        card["appraisal"]["verification"] = verification
        if record.published:
            card["first_published"] = record.published
        if not self.card_validator.is_valid(card):
            raise Review("atlas_card_schema")
        citation = {"id": "paper", "citation": record.citation, "verified": True, "url": record.url}
        citation.update({key: value for key, value in (("doi", record.doi), ("pmid", record.pmid)) if value})
        draft = {"study": card, "citation": citation, "added_at": added_at}
        if paper_id:
            self.papers.append((paper_id, draft))
            self.ids.add(paper_id)
        else:
            self.studies.append((record, draft))
        self.seen.update(record.keys)

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
            self.hold(key, reason, subject, notify=not discovered or reason not in (
                "abstract_only", "insufficient_source", "full_source_too_large",
            ))
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
        if not record.open_access:
            raise Review("pdf_open_access_unconfirmed")
        anchor = self.anchors.get(record.doi or record.pmid)
        if (not anchor or not anchor.get("reviewed_by") or not anchor.get("external_searched")
                or not isinstance(anchor.get("external"), list)
                or anchor.get("pdf_sha256") != sha256(pdf_bytes).hexdigest()):
            raise Review("pdf_external_anchor_review_needed")
        if any(r.get("agrees") is not True for r in anchor["external"]):
            raise Review("external_rating_disagreement")
        self.candidate(record, paper_id, lambda: ("full_text", record.url, article),
                       self.now.isoformat(), anchor=anchor)

    def discover(self):
        if not weekly_work(self.feed.root, self.today):
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
        found = self.literature.discover(self.config["query"], self.today,
                                        self.config["lookback_days"], self.config["search_limit"])
        attempted = set()
        for record in (*resumed, *sorted(found, key=lambda r: not r.open_access)):
            if len(self.studies) >= 3 or self.attempts >= self.config["max_candidates"]:
                break
            if (not record.keys or record.excluded or self.feed.duplicate(record)
                    or record.keys & (self.seen | attempted)):
                continue
            attempted.update(record.keys)
            key = self.record_key(record)
            self.attempt(key, record.doi or record.pmid,
                         lambda: self.discovery_candidate(record, pending), static=True, discovered=True)

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
        self.config["max_candidates"] = max(1, limit - 3)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stage", "publish"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--retry-holds", action="store_true")
    parser.add_argument("--benchmark-pmid")
    args = parser.parse_args()
    if args.benchmark_pmid and (args.mode != "stage" or not pmid(args.benchmark_pmid)):
        parser.error("benchmark requires stage mode and a numeric PMID")
    if args.mode == "publish" and (os.environ.get("ATLAS_PUBLISH_ENABLED") != "true"
                                   or os.environ.get("GITHUB_EVENT_NAME") != "schedule"
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
        git(state, "add", "--", "research-state.json")
        if git(state, "diff", "--cached", "--name-only"):
            git(state, "commit", "-m", "Checkpoint Atlas research")
            git(state, "push", "origin", "HEAD:refs/heads/main")

    journal = Journal(state / "research-state.json", persist)
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
        drive = None if args.benchmark_pmid else Drive(
            os.environ["ATLAS_DRIVE_READER_JSON"], os.environ["ATLAS_DRIVE_FOLDER_ID"])
        pipeline = Pipeline(feed, journal, Literature(), drive, model, now, run, config,
                            read_json(anchors) if anchors.exists() else {}, args.retry_holds)
        try:
            if args.benchmark_pmid:
                pipeline.benchmark(args.benchmark_pmid)
            else:
                pipeline.process()
        except BudgetExceeded:
            run["reason"] = "run_or_month_budget"
        run["verified"] = len(pipeline.papers) + len(pipeline.studies)
        if args.benchmark_pmid:
            if pipeline.studies:
                run["preview"] = {"benchmark": pipeline.studies[0][1]}
            changed = []
        else:
            changed = feed.publish(pipeline.papers, pipeline.studies, pipeline.today, now.isoformat())
        if changed:
            # No model-supplied content is printed in the public Actions log.
            if subprocess.run([sys.executable, "-X", "utf8", str(root / "scripts/validate.py")],
                              capture_output=True).returncode:
                raise Unavailable("atlas_schema_or_style_validation_failed")
            if args.mode == "publish":
                git(root, "add", "--", *changed)
                git(root, "commit", "-m", "Publish independently verified Atlas research")
                git(root, "push", "origin", "HEAD:refs/heads/main")
                run["published"] = run["verified"]
                run["publication_commit"] = git(root, "rev-parse", "HEAD")
            else:
                run["preview"] = {path: read_json(root / path) for path in changed
                                  if path.startswith(("editions/", "papers/")) and path != "papers/index.json"}
        if run.get("source_errors"):
            run["status"] = "partial"
        elif run.get("reason") == "run_or_month_budget":
            run["status"] = "budget_limited"
        else:
            run["status"] = "needs_review" if run["new_holds"] else "complete"
    except Unavailable as error:
        run.update(status="failed", reason=str(error))
        problem = error
    except Exception as error:
        run.update(status="failed", reason=type(error).__name__)
        problem = error
    journal.save()
    if run.get("publication_commit") and os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            print(f"published={run['publication_commit']}", file=output)
    print(f"Atlas research {run['status']}. Verified: {run.get('verified', 0)}. "
          f"Published: {run.get('published', 0)}. New holds: {run['new_holds']}.")
    if problem or run["new_holds"] or run["status"] in ("partial", "budget_limited"):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Unavailable, ValueError):
        sys.exit("Atlas research could not start or checkpoint. Check workflow setup and private state.")
