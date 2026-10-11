"""T10 guard corpora (plan §5.4, §8.4 3d): structure, coverage and fixture consistency.

The labels are the guard specification (plan §5.4 plus the corpus rule notes); guards.py is written in B5d, so
nothing here runs product guards. The checks below only make sure every item can be run mechanically: the
fixture keys are known, the records it names exist, and the labels of the presence-type guards (G4 sources,
G5 law wording, G6 foreign order numbers) agree with a small reference reading of the text and the fixture.
"""
import json
from pathlib import Path
import re
import unittest

DATA = Path(__file__).resolve().parent / "data"
GUARDS = DATA / "guard_corpus_v1.json"
REDTEAM = DATA / "guard_redteam_v1.json"
FORMS = {"affirmative", "negative", "question", "conditional", "mixed"}
EVENT_TYPES = {"return_request_submitted", "ticket_created", "ticket_event_appended", "ticket_withdrawn", "card_voided"}
CARD_STATES = {"none", "shown", "kept"}

# Reference readings (RN9, RN12, RN16), kept deliberately small.
ORDER_ID = re.compile(r"(?<![A-Za-z0-9_-])O\d{4}(?![A-Za-z0-9_-])")
UNITS = r"(?:个工作日|工作日|个小时|小时|个月|月|年|天|日|元|%|次)"
ARABIC_WITH_UNIT = re.compile(r"\d+(?:\.\d+)?" + UNITS)
CHINESE_WITH_UNIT = re.compile(r"[一二三四五六七八九十两百千]+" + UNITS)
DATE = re.compile(r"\d{1,2}月\d{1,2}日")
URL = re.compile(r"[A-Za-z0-9.-]+\.(?:cn|com|test|example)(?:/[A-Za-z0-9/_-]*)?")
LAW_WORDS = re.compile(r"法律|法规|法定|依法|国家规定|消费者权益保护法|电子商务法|《[^》]*法》")
MARKERS = re.compile(r"是否|能否|可否|该不该|应否|由谁")
CLAUSE = re.compile(r"[，,。；;！!？?\n]")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fixture(corpus, item):
    return {**corpus["defaults"], **item["fixture"]}


def digits_only(text):
    """Digits of a number written with spaces or hyphens between digit groups (RN9)."""
    return re.sub(r"(?<=\d)[ -](?=\d)", "", text)


def tokens(text):
    """Number-like tokens that need a source (G4): numbers with units, dates, runs of 5+ digits, URLs."""
    found = []
    for clause in CLAUSE.split(text):
        marker = MARKERS.search(clause)
        for pattern, kind in ((URL, "url"), (DATE, "date"), (ARABIC_WITH_UNIT, "unit"), (CHINESE_WITH_UNIT, "unit")):
            for match in pattern.finditer(clause):
                if marker and marker.start() < match.start():
                    continue   # RN1/RN2: G4 keeps the question exemption
                found.append(match.group())
        # A run of digits (an optional letter prefix such as a carrier code stays outside the count).
        for match in re.finditer(r"[A-Za-z]*\d[\d -]*\d", clause):
            run = re.sub(r"\D", "", digits_only(match.group()))
            if len(run) >= 5 and not (marker and marker.start() < match.start()):
                found.append(run)
    return found


def sources(corpus, fx):
    """Texts G4 accepts as sources: order facts, the selected cards and the path catalogue (not the user)."""
    texts = []
    for order in fx["order_facts"].values():
        texts += [str(value) for value in order.values()]
    for card_id in fx["selected_card_ids"]:
        card = corpus["cards"][card_id]
        texts += [card["text"], card["title"]]
    for path in fx["paths"].values():
        texts += [str(value) for value in path.values()]
    return "\n".join(texts), [re.sub(r"\D", "", digits_only(text)) for text in texts]


class CorpusShapeTests(unittest.TestCase):
    def check_shape(self, corpus, minimum):
        items = corpus["items"]
        self.assertGreaterEqual(len(items), minimum)
        self.assertEqual(len({item["id"] for item in items}), len(items), "duplicate ids")
        self.assertEqual(corpus["schema_version"], "guard-corpus-v1")
        self.assertEqual(corpus["provenance"]["authoring"]["model"], "claude-opus-5-5")
        self.assertIn("StructuredOutput", corpus["provenance"]["authoring"]["delivery"])
        self.assertEqual(corpus["provenance"]["review"]["model"], "claude-opus-5-5")
        self.assertEqual({item["form"] for item in items}, FORMS)
        schema = set(corpus["fixture_schema"]) - {"merge"}
        self.assertLessEqual(set(corpus["defaults"]), schema)
        for item in items:
            with self.subTest(item=item["id"]):
                self.assertEqual(set(item), {"id", "text", "form", "context", "expected", "rationale", "fixture",
                                             "tags", "rule_notes"})
                self.assertIn(item["form"], FORMS)
                self.assertTrue(item["text"].strip() and item["context"].strip() and item["rationale"].strip())
                self.assertTrue(item["expected"])
                self.assertLessEqual(set(item["expected"]), set(corpus["label_values"]))
                if "clean" in item["expected"]:
                    self.assertEqual(item["expected"], ["clean"])
                self.assertEqual(item["expected"], sorted(item["expected"], key=corpus["label_values"].index))
                self.assertLessEqual(set(item["tags"]), set(corpus["tag_values"]))
                self.assertLessEqual(set(item["fixture"]), schema)
                fx = fixture(corpus, item)
                self.assertLessEqual(set(fx["selected_card_ids"]) | set(fx["catalogue_card_ids"]), set(corpus["cards"]))
                self.assertLessEqual(set(fx["own_order_ids"]), set(fx["order_facts"]))
                self.assertIn(fx["card_state"], CARD_STATES)
                if fx["card_state"] != "none":
                    self.assertIn(fx["card_order_id"], fx["own_order_ids"])
                for event in fx["events"]:
                    self.assertIn(event["scope"], {"turn", "session"})
                    self.assertIn(event["type"], EVENT_TYPES)
                    self.assertTrue(event.get("order_id") in fx["own_order_ids"] or event.get("ticket"))
                if "accepted_false_positive" in item["tags"]:
                    self.assertNotEqual(item["expected"], ["clean"])

    def test_the_guard_corpus_has_sixty_items_or_more_and_both_sides_of_every_guard(self):
        corpus = load(GUARDS)
        self.check_shape(corpus, 60)
        notes = {note["id"] for note in corpus["rule_notes"]}
        self.assertEqual(len(notes), len(corpus["rule_notes"]))
        for note in corpus["rule_notes"]:
            self.assertTrue(note["resolution"].strip() and note["principle"].strip())
        for item in corpus["items"]:
            self.assertLessEqual(set(item["rule_notes"]), notes, item["id"])
        for guard in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"):
            with self.subTest(guard=guard):
                own = [item for item in corpus["items"] if item["id"].startswith(guard + "-")]
                self.assertTrue(any(item["expected"] == ["clean"] for item in own))
                self.assertTrue(any(guard in item["expected"] for item in own))
                self.assertGreaterEqual(len({item["form"] for item in own}), 4)

    def test_the_red_team_corpus_has_thirty_items_or_more_and_points_to_the_rule_notes(self):
        corpus = load(REDTEAM)
        self.check_shape(corpus, 30)
        notes = {note["id"] for note in load(GUARDS)["rule_notes"]}
        g1 = [item for item in corpus["items"] if "G1" in item["expected"]]
        clean = [item for item in corpus["items"] if item["expected"] == ["clean"]]
        self.assertGreaterEqual(len(g1), 30)
        self.assertGreaterEqual(len(clean), 15)
        for item in corpus["items"]:
            self.assertLessEqual(set(item["rule_notes"]), notes, item["id"])
            self.assertTrue(item["id"].startswith("G1R-"))
        # Fabricated results the user pasted are part of the fixture, never a source of the reply.
        self.assertGreaterEqual(sum(1 for item in corpus["items"] if item["fixture"].get("user_text")), 15)


class FixtureConsistencyTests(unittest.TestCase):
    """Presence-type labels agree with a reference reading of text and fixture, in both corpora."""

    def items(self):
        for path in (GUARDS, REDTEAM):
            corpus = load(path)
            for item in corpus["items"]:
                yield corpus, item

    def test_g6_labels_follow_the_order_numbers_in_the_text(self):
        for corpus, item in self.items():
            with self.subTest(item=item["id"]):
                foreign = set(ORDER_ID.findall(item["text"])) - set(fixture(corpus, item)["own_order_ids"])
                self.assertEqual(bool(foreign), "G6" in item["expected"])

    def test_g4_labels_follow_the_sources_in_the_fixture(self):
        for corpus, item in self.items():
            with self.subTest(item=item["id"]):
                joined, joined_digits = sources(corpus, fixture(corpus, item))
                unsourced = [token for token in tokens(item["text"])
                             if token not in joined and not (token.isdigit()
                                                             and any(token in digits for digits in joined_digits))]
                self.assertEqual(bool(unsourced), "G4" in item["expected"], unsourced)

    def test_g5_labels_follow_law_wording_and_the_selected_cards(self):
        for corpus, item in self.items():
            fx = fixture(corpus, item)
            law_card = any(corpus["cards"][card]["source_kind"] == "law" for card in fx["selected_card_ids"])
            wording = False
            for clause in CLAUSE.split(item["text"]):
                marker = MARKERS.search(clause)
                wording |= any(not (marker and marker.start() < match.start()) for match in LAW_WORDS.finditer(clause))
            with self.subTest(item=item["id"]):
                self.assertEqual(wording and not law_card, "G5" in item["expected"])

    def test_completion_claims_backed_by_records_name_a_matching_event(self):
        corpus = load(GUARDS)
        for item_id in ("G1-N04", "G1-N09", "G1-N10", "G2-N06", "G2-P08"):
            item = next(row for row in corpus["items"] if row["id"] == item_id)
            with self.subTest(item=item_id):
                self.assertTrue(fixture(corpus, item)["events"])
        mismatch = next(row for row in corpus["items"] if row["id"] == "G1-P12")
        self.assertEqual({event["order_id"] for event in fixture(corpus, mismatch)["events"]}, {"O4127"})
        self.assertIn("O4130", mismatch["text"])


if __name__ == "__main__":
    unittest.main()
