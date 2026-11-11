import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from unittest.mock import patch

import test_research as base
from research import Journal, main, write_json
from research_questions import answer_problems, card_view, known_keys, parse_marker
from research_sources import Review, Unavailable

RECORD = base.RECORD
OTHER = replace(RECORD, doi="10.1234/other", pmid="22222", title="Loading review for tendon pain",
                citation="Smith AB. Loading review for tendon pain. Example Journal 2024.",
                abstract="A review of 14 trials of tendon loading.", published="2024-03-01")
OWNED = replace(RECORD, doi="10.1234/owned", pmid="33333", title="A paper already in the library", published="2023")
RETRACTED = replace(RECORD, doi="10.1234/gone", pmid="44444", title="A retracted trial",
                    publication_types="retracted publication", published="2022")
PAPER = "00abcdef"
MARKER = {"kind": "question", "id": "0a1b2c3d", "question": "Isometrics for Achilles pain?", "context": "Runners",
          "region": "lower limb", "already_in_library": [OWNED.doi], "linked_items": [],
          "papers": [{"id": PAPER, "doi": RECORD.doi, "title": RECORD.title}]}
OPEN = {"ATLAS_PUBLISH_ENABLED": "true", "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "schedule"}


class Library:
    def __init__(self, level="full_text", down=False):
        self.level, self.down, self.queries = level, down, []

    def search(self, query, size):
        if self.down:
            raise Unavailable("connection_failure")
        self.queries.append(query)
        return [replace(RECORD, published="2025-01-01"), OTHER, OWNED, RETRACTED]

    def discover(self, *args):
        return [replace(OTHER, doi="10.1234/weekly", pmid="55555", title="Weekly exercise trial")]

    def identify(self, paper_doi, paper_pmid, title):
        return RECORD

    def source(self, record):
        if self.level != "full_text":
            return self.level, record.url, base.ARTICLE[:900]
        return "full_text", record.url, base.ARTICLE


class Folder:
    def __init__(self, files):
        self.files = files  # name -> marker

    def entries(self):
        return [{"id": f"id-{name}", "name": name, "md5Checksum": base.sha256(self.download({"name": name}, 0)).hexdigest()}
                for name in self.files]

    def download(self, entry, limit):
        value = self.files[entry["name"]]
        return value if isinstance(value, bytes) else json.dumps(value).encode()


class QuestionTest(base.ResearchTest):
    def setUp(self):
        super().setUp()
        self.answer = lambda cards: {
            "answer": "Isometrics are not clearly better than other loading. Evidence is thin.",
            "certainty": "very low", "provisional": False, "actions": ["Keep loading pain-guided."],
            "comparison": [{"item_id": card["item_id"], "design": "Randomised trial", "n": "Not reported",
                            "finding": "Uncertain between-group effect on pain", "verdict": "unclear"}
                           for card in cards],
            "agreement": [], "conflicts": []}
        self.repaired = None
        self.picks = {"picks": [{"n": n, "priority": "core", "design": "Systematic review",
                                 "why": "Pools 14 trials of tendon loading."} for n in (0, 0, 99)],
                      "gaps": ["No head-to-head trial in athletes.", "Only 3 trials exceed 12 months."]}

    def generate(self, url, *, headers, body):
        request = json.loads(body)
        phase = request["text"]["format"]["name"]
        if not phase.startswith("question"):
            return super().generate(url, headers=headers, body=body)
        payload = json.loads(request["input"])
        self.phases.append(phase)
        if phase == "question_search":
            value = {"pico": dict.fromkeys("pico", "x"), "queries": ["achilles AND isometric", "tendinopathy"]}
        elif phase == "question_picks":
            self.offered = payload["papers"]
            value = self.picks
        else:
            self.cards = payload["cards"]
            value = self.answer(self.cards) if phase == "question_answer" else self.repaired(self.cards)
        self.assertTrue(base.Draft202012Validator(request["text"]["format"]["schema"]).is_valid(value))
        return json.dumps({"status": "completed", "model": request["model"],
                           "usage": {"input_tokens": 3000, "output_tokens": 500},
                           "output": [{"type": "message", "content": [
                               {"type": "output_text", "text": json.dumps(value)}]}]}).encode()

    def stage(self, files, env=OPEN, library=None, run="1", argv=("--queue-only",), auto=True):
        state = self.root / "private"
        state.mkdir(exist_ok=True)
        write_json(self.root / "scripts/research_config.json", {**base.CONFIG, "auto_publish": auto, "critic": True})
        self.phases.clear()
        stdout = StringIO()
        with patch("research.ROOT", self.root), patch("research.Literature", return_value=library or Library()), \
                patch("research.Drive", return_value=Folder(files)), patch("research.git") as git, \
                patch.dict(os.environ, {"GITHUB_RUN_ID": run, "GITHUB_RUN_ATTEMPT": "1",
                                        "ATLAS_DRIVE_READER_JSON": "{}", "ATLAS_DRIVE_FOLDER_ID": "x", **env}), \
                patch.object(sys, "argv", ["research", "stage", "--state", str(state), *argv]), \
                redirect_stdout(stdout), redirect_stderr(StringIO()):
            git.side_effect = lambda path, *args: (state.as_posix() if args[:2] == ("rev-parse", "--show-toplevel")
                                                   else "c" * 40 if args[0] == "rev-parse" else "")
            code = main()
        journal = Journal(state / "research-state.json")
        return code, journal.data["runs"][f"{run}.1"], journal, stdout.getvalue()

    def question(self):
        return json.loads((self.root / "questions/0a1b2c3d.json").read_text(encoding="utf-8"))

    def test_question_gets_checked_cards_a_real_reading_list_and_an_answer_from_those_cards(self):
        files = {"question__0a1b2c3d__Isometrics.txt": MARKER}
        code, run, journal, out = self.stage(files)
        self.assertEqual(0, code, out)
        self.assertEqual(["primary", "critic", "question_search", "question_picks", "question_answer"], self.phases)
        self.assertIn("Questions answered: 1.", out)
        doc = self.question()
        # The answer rests only on the card just checked, which the app can open.
        self.assertEqual([f"mine-{PAPER}"], [card["item_id"] for card in self.cards])
        self.assertEqual([f"mine-{PAPER}"], doc["answer"]["based_on"])
        self.assertTrue(doc["answer"]["provisional"])  # one study, whatever the model said
        self.assertTrue((self.root / f"papers/{PAPER}.json").is_file())
        # Suggestions are real records only: nothing owned, retracted, in the marker, repeated or invented.
        self.assertEqual([OTHER.title], [paper["title"] for paper in self.offered])
        self.assertEqual([(OTHER.doi, OTHER.citation, "2024")],
                         [(row["key"], row["citation"], row["year"]) for row in doc["suggestions"]])
        self.assertEqual("Pools 14 trials of tendon loading.", doc["suggestions"][0]["why"])
        self.assertEqual(["No head-to-head trial in athletes."], doc["gaps"])  # the gap with invented numbers is gone
        self.assertIn("AI-checked, not clinician-reviewed", doc["search_summary"])
        # Nothing new: no model calls, nothing rewritten. A second copy of the marker changes nothing.
        code, run, journal, out = self.stage(files, run="2")
        self.assertEqual((0, [], 0), (code, self.phases, run["questions"]))
        files["question__0a1b2c3d__Isometrics again.txt"] = {**MARKER, "context": "Sent twice"}
        self.stage(files, run="3")
        self.assertEqual(["question_search", "question_picks", "question_answer"], self.phases)
        self.stage(files, run="4")
        self.assertEqual([], self.phases)

    def test_a_resent_question_is_refreshed_and_new_cards_rebuild_only_the_answer(self):
        files = {"question__0a1b2c3d__Isometrics.txt": {**MARKER, "papers": []}}
        self.stage(files)
        self.assertEqual(["question_search", "question_picks"], self.phases)
        self.assertNotIn("answer", self.question())  # no checked card yet
        self.assertEqual([RECORD.doi], [row["key"] for row in self.question()["suggestions"]])
        # The same marker, but a paper is added to the question through a queue marker.
        files["queue__batch__1.txt"] = {"kind": "queue", "question_id": "0a1b2c3d",
                                        "papers": [{"id": PAPER, "doi": RECORD.doi}]}
        self.stage(files, run="2")
        self.assertEqual(["primary", "critic", "question_answer"], self.phases)
        self.assertEqual([f"mine-{PAPER}"], self.question()["answer"]["based_on"])
        self.assertEqual([], self.question()["suggestions"])  # it is in the library now
        # A changed marker (new md5) searches again.
        files["question__0a1b2c3d__Isometrics.txt"] = {**MARKER, "papers": [], "context": "Sprinters"}
        self.stage(files, run="3")
        self.assertEqual(["question_search", "question_picks", "question_answer"], self.phases)

    def test_an_answer_with_an_invented_number_or_unknown_card_is_repaired_once_then_left_out(self):
        files = {"question__0a1b2c3d__Isometrics.txt": MARKER}
        good = self.answer
        self.answer = lambda cards: {**good(cards), "actions": ["Hold 5 x 45 s daily."]}
        self.repaired = good
        code, run, journal, out = self.stage(files)
        self.assertEqual((0, "question_answer_repair"), (code, self.phases[-1]))
        self.assertEqual(["Keep loading pain-guided."], self.question()["answer"]["actions"])

        def unknown(cards):
            reply = good(cards)
            reply["comparison"].append({**reply["comparison"][0], "item_id": "mine-ffffffff"})
            return reply

        self.answer = self.repaired = unknown
        files["question__0a1b2c3d__Isometrics.txt"] = {**MARKER, "context": "Changed"}
        (self.root / "questions/0a1b2c3d.json").unlink()
        code, run, journal, out = self.stage(files, run="2")
        self.assertEqual(1, code)  # the clinician is told
        self.assertIn("question_answer_rejected", self.reasons(journal.data["holds"]))
        self.assertNotIn("answer", self.question())
        self.assertNotIn("ffffffff", out)

    def test_answer_checks_numbers_per_card_caps_and_working_notes(self):
        views = {"mine-a": card_view("mine-a", {"title": "Trial 1", "results": [{"result": "Pain fell 12 points"}]}),
                 "mine-b": card_view("mine-b", {"title": "Review", "key_findings": ["30 trials pooled"]})}
        reply = {"answer": "Pain fell 12 points across 30 trials.", "certainty": "low", "provisional": False,
                 "actions": ["I could not find a dose.", " ".join(["word"] * 23)], "agreement": [], "conflicts": [],
                 "comparison": [{"item_id": "mine-a", "design": "RCT", "n": "30", "finding": "12 points",
                                 "verdict": "unclear"}]}
        problems = " | ".join(answer_problems(reply, views))
        for expected in ("actions[0] contains working notes", "actions[1] is 23 words",
                         "comparison[0] numbers not on its own card: 30"):
            self.assertIn(expected, problems)
        self.assertNotIn("not on any card", problems)
        reply["comparison"][0]["n"] = "Not reported"
        reply["actions"] = []
        self.assertEqual([], answer_problems(reply, views))

    def test_a_question_typed_into_the_details_box_is_used_as_the_question(self):
        marker = parse_marker(json.dumps({**MARKER, "question": "", "context": "Do NSAIDs help? \\n"}).encode())
        self.assertEqual(("Do NSAIDs help?", ""), (marker["question"], marker["context"]))
        with self.assertRaisesRegex(Review, "invalid_question_marker"):
            parse_marker(json.dumps({**MARKER, "question": "", "context": " "}).encode())

    def test_bad_markers_are_held_and_library_keys_ignore_ids(self):
        for bad in (b"not json", b"[]", json.dumps({**MARKER, "kind": "queue"}).encode(),
                    json.dumps({**MARKER, "id": "../x"}).encode(), json.dumps({**MARKER, "papers": {}}).encode()):
            with self.subTest(bad=bad), self.assertRaisesRegex(Review, "invalid_question_marker"):
                parse_marker(bad)
        code, run, journal, out = self.stage({"question__bad__x.txt": b"{"})
        self.assertEqual(1, code)
        self.assertIn("invalid_question_marker", self.reasons(journal.data["holds"]))
        self.assertEqual({"10.1234/x", "12345678", "a title"},
                         known_keys([{"id": "99999999", "doi": "10.1234/x", "title": "A title"}, "12345678"]))

    def test_nothing_is_published_or_spent_on_questions_while_the_gate_is_closed(self):
        files = {"question__0a1b2c3d__Isometrics.txt": MARKER}
        for env, auto in (({**OPEN, "ATLAS_PUBLISH_ENABLED": "false"}, True),
                          ({**OPEN, "GITHUB_REF": "refs/heads/other"}, True), (OPEN, False)):
            with self.subTest(env=env, auto=auto):
                self.stage(files, env=env, auto=auto, run=str(len(env)) + str(auto))
                self.assertFalse([phase for phase in self.phases if phase.startswith("question")])
                self.assertFalse((self.root / "questions").exists())
                self.assertFalse((self.root / f"papers/{PAPER}.json").exists())

    def test_paywalled_question_papers_are_skipped_quietly_and_reported_as_unread(self):
        files = {"question__0a1b2c3d__Isometrics.txt": MARKER}
        code, run, journal, out = self.stage(files, library=Library("abstract"))
        self.assertEqual((0, 0), (code, run["new_holds"]))
        self.assertIn("abstract_only", self.reasons(journal.data["holds"]))
        doc = self.question()
        self.assertNotIn("answer", doc)
        self.assertTrue(doc["gaps"][0].startswith("1 of 1 papers found were not read"))
        self.assertEqual(["question_search", "question_picks"], self.phases)

    def test_a_failed_search_leaves_the_question_for_the_next_run(self):
        files = {"question__0a1b2c3d__Isometrics.txt": {**MARKER, "papers": []}}
        library = Library(down=True)
        code, run, journal, out = self.stage(files, library=library)
        self.assertEqual("partial", run["status"])
        self.assertFalse((self.root / "questions/0a1b2c3d.json").exists())
        library.down = False
        self.stage(files, library=library, run="2")
        self.assertEqual(1, len(self.question()["suggestions"]))

    def test_delete_marker_removes_the_card_and_its_answer_rows_and_it_is_never_rebuilt(self):
        files = {"question__0a1b2c3d__Isometrics.txt": MARKER}
        self.stage(files)
        self.assertEqual([f"mine-{PAPER}"], self.question()["answer"]["based_on"])
        files[f"delete__{PAPER}.txt"] = b""
        code, run, journal, out = self.stage(files, run="2")
        self.assertEqual((0, 1), (code, run["deleted"]), out)
        self.assertFalse((self.root / f"papers/{PAPER}.json").exists())
        self.assertEqual([], json.loads((self.root / "papers/index.json").read_text(encoding="utf-8"))["papers"])
        self.assertNotIn("answer", self.question())
        self.assertIn(PAPER, journal.data["deleted"])
        code, run, journal, out = self.stage(files, run="3")
        self.assertEqual((0, [], 0), (code, self.phases, run["deleted"]))
        self.assertFalse((self.root / f"papers/{PAPER}.json").exists())

    def test_daily_run_skips_the_weekly_search(self):
        self.stage({})
        self.assertEqual([], list((self.root / "editions").glob("*.json")))
        self.stage({}, run="2", argv=())
        self.assertEqual(1, len(list((self.root / "editions").glob("*.json"))))


# The inherited tests already run in test_research.
for name in dir(base.ResearchTest):
    if name.startswith("test"):
        setattr(QuestionTest, name, None)
