import json
import sqlite3
import unittest

from courts_rain import eligibility, grounds, ledger, pages, schedule, workbench
from courts_rain.db import Rejected, connect, one


def op(event, action, **fields):
    return {"id": event, "op": action, **fields}


class RainLeagueTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(connect(":memory:"))
        self.league = workbench.load(workbench.FIXTURES / "league.json")
        self.late = workbench.load(workbench.FIXTURES / "late-recall.json")
        self.rejections = json.loads((workbench.FIXTURES / "rejections.json").read_text(encoding="utf-8"))

    def apply(self, *events):
        return workbench.apply(self.db, events)

    def before(self, event):
        index = next(i for i, record in enumerate(self.league) if record["id"] == event)
        self.apply(*self.league[:index])
        return index

    def test_surface_and_late_observation(self):
        self.before("offer-f1")
        self.assertEqual(grounds.condition(self.db, "east", 97)["status"], "open")
        self.assertEqual(grounds.condition(self.db, "east", 100), {"inspection": "east-rain", "status": "closed"})
        self.assertEqual(grounds.condition(self.db, "west", 100)["status"], "open")
        self.assertEqual(grounds.condition(self.db, "annex", 100)["status"], "unverified")
        self.assertEqual(grounds.condition(self.db, "annex", 165)["status"], "open")
        self.assertEqual(grounds.condition(self.db, "annex", 166)["status"], "stale")
        self.apply(op("debris", "inspect", court="west", minute=110, wet=0, debris=True))
        self.assertEqual(grounds.condition(self.db, "west", 110)["status"], "closed")
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(*self.rejections["court_history"])

    def test_roster_and_official_qualification(self):
        self.before("old-f1")
        for case in ("foreign_team", "foreign_ref"):
            with self.assertRaisesRegex(Rejected, "out-of-division"):
                self.apply(*self.rejections[case])
        self.assertIsNone(one(self.db, "SELECT * FROM rounds WHERE id='r-other'"))
        self.assertEqual(workbench.report(self.db, "r1")["fairness"]["spread"], None)

    def test_window_union_and_blackout(self):
        self.before("w-ref-a")
        for event, start, end in (("u1", 100, 115), ("u2", 115, 130)):
            self.apply(op(event, "window", kind="official", person="ref-a", start=start, stop=end, available=True))
        self.assertTrue(eligibility.availability(self.db, "ref-a", 100, 130)["available"])
        self.assertFalse(eligibility.availability(self.db, "ref-a", 100, 131)["available"])
        self.apply(op("u3", "window", kind="official", person="ref-a", start=114, stop=116, available=False))
        self.assertFalse(eligibility.availability(self.db, "ref-a", 100, 130)["available"])
        self.assertTrue(eligibility.availability(self.db, "ref-a", 116, 130)["available"])

    def test_promise_consent_and_at_time_inspection(self):
        idx = self.before("new-f1")
        with self.assertRaisesRegex(Rejected, "missing timely"):
            self.apply(*self.rejections["unsigned_move"])
        with self.assertRaisesRegex(Rejected, "court-unverified"):
            self.apply(op("too-early", "move", promise="offer-f1", minute=119))
        with self.assertRaisesRegex(Rejected, "expired"):
            self.apply(op("too-late", "move", promise="offer-f1", minute=151))
        self.assertEqual(schedule.current(self.db, "f1")["id"], "old-f1")
        self.assertEqual(self.apply(self.league[idx])[0]["decision"], "rain-slot-moved")
        with self.assertRaisesRegex(Rejected, "consumed"):
            self.apply(op("second-use", "move", promise="offer-f1", minute=122))
        self.assertEqual(schedule.current(self.db, "f1")["rev"], 2)
        self.assertEqual(self.apply(self.league[idx])[0]["decision"], "duplicate")

    def test_occupancy_is_checked_before_releasing_old_slot(self):
        self.before("new-f1")
        def second_match(court, official, slot):
            return [op("r2", "round", division="north", earliest=100, deadline=240, expected=1),
                    op("f3", "fixture", round="r2", home="moles", away="hares"),
                    op("new-f3", "assign", fixture="f3", court=court, start=slot, official=official)]
        with self.assertRaisesRegex(Rejected, "participant occupied by f1"):
            self.apply(*second_match("annex", "ref-a", 120))
        self.apply(*second_match("annex", "ref-a", 145))
        with self.assertRaisesRegex(Rejected, "court occupied by f3"):
            self.apply(op("contested", "move", promise="offer-f1", minute=122))
        self.assertEqual(schedule.current(self.db, "f1")["id"], "old-f1")
        self.assertEqual(one(self.db, "SELECT used FROM promises WHERE id='offer-f1'")[0], 0)

    def test_result_revision_and_void(self):
        idx = self.before("score-f2")
        with self.assertRaisesRegex(Rejected, "stale assignment"):
            self.apply(*self.rejections["stale_score"])
        self.apply(*self.league[idx:-1])
        void = op("void", "result", fixture="f1", assignment="new-f1", revision=2, minute=194, home=None, away=None)
        self.apply(void)
        self.assertIn("f1:missing-result", workbench.report(self.db, "r1")["blockers"])
        with self.assertRaisesRegex(Rejected, "stale result revision"):
            self.apply({**void, "id": "again"})
        self.apply({**void, "id": "fixed", "revision": 3, "home": 1, "away": 1})
        self.apply(self.league[-1])
        self.assertEqual([r["points"] for r in ledger.standings(self.db)["north"]], [1] * 4)

    def test_fairness_blocks_complete_but_spread_round(self):
        with connect(":memory:") as boundary:
            exact = [{**r, "spread": 50} if r["id"] == "north" else r for r in self.league]
            workbench.apply(boundary, exact)
            self.assertEqual(workbench.report(boundary, "r1")["fairness"], {"fair": True, "spread": 50, "limit": 50})
        narrow = [{**r, "spread": 49} if r["id"] == "north" else r for r in self.league[:-1]]
        self.apply(*narrow)
        with self.assertRaisesRegex(Rejected, "round-spread"):
            self.apply(self.league[-1])
        self.assertIsNone(ledger.active(self.db, "r1"))
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")

    def test_one_fixture_per_team_per_round(self):
        self.before("f1")
        self.apply(op("opening-fixture", "fixture", round="r1", home="owls", away="foxes"))
        with self.assertRaisesRegex(Rejected, "team already entered"):
            self.apply(op("double-counted", "fixture", round="r1", home="owls", away="moles"))
        self.assertEqual(self.apply(op("second-fixture", "fixture", round="r1", home="hares", away="moles"))[0]["decision"],
                         "fixture-admitted")
        self.assertIn("unassigned:second-fixture", workbench.report(self.db, "r1")["blockers"])

    def test_late_rain_reopening_disputes_consumed_promise(self):
        self.apply(*self.league)
        self.apply(op("reopen", "inspect", court="east", minute=99, wet=0))
        state = workbench.report(self.db, "r1")
        self.assertEqual(state["release"], "review-required")
        self.assertIn("f1:rain-closure-disputed:old-f1", state["blockers"])
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")

    def test_recall_compensates_and_preserves_other_round(self):
        self.apply(*self.league)
        self.apply(op("r2", "round", division="north", earliest=190, deadline=240, expected=1),
                   op("f3", "fixture", round="r2", home="moles", away="hares"),
                   op("annex-refresh", "inspect", court="annex", minute=185, wet=0),
                   op("new-f3", "assign", fixture="f3", court="annex", start=190, official="ref-b"),
                   op("score-f3", "result", fixture="f3", assignment="new-f3", revision=1, minute=220, home=3, away=2),
                   op("publication-2", "settle", round="r2"))
        self.apply(self.late[0])
        held = workbench.report(self.db, "r1")
        self.assertEqual(held["release"], "review-required")
        self.assertIn("f1:unavailable:ref-a", held["blockers"])
        with self.assertRaisesRegex(Rejected, "stale or absent"):
            self.apply(*self.rejections["stale_recall"])
        self.apply(self.late[1])
        self.assertEqual(ledger.standings(self.db)["north"][0],
                         {"team": "moles", "points": 3, "difference": 1, "played": 1, "rank": 1})
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")
        self.assertIsNotNone(one(self.db, "SELECT * FROM entries WHERE settlement='publication-1'"))
        with self.assertRaisesRegex(Rejected, "round held"):
            self.apply(op("release-again", "settle", round="r1"))

    def test_id_conflict_rolls_back_and_audit_detects_projection_drift(self):
        self.apply(*self.league)
        with self.assertRaisesRegex(Rejected, "event ID conflict"):
            self.apply(op("temporary", "court", surface="synthetic"), {**self.league[0], "win": 8})
        self.assertIsNone(one(self.db, "SELECT * FROM courts WHERE id='temporary'"))
        self.db.execute("UPDATE entries SET points=99 WHERE team='owls'")
        replay = workbench.audit(self.db)
        self.assertEqual(replay["drift"], ["entries"])
        self.assertEqual(replay["standings"]["north"][0]["points"], 3)


class ScorePageTests(unittest.TestCase):
    """Raw score-sheet pages for the rained-out f1 replay (annex, minutes 160-190)."""

    def setUp(self):
        self.db = self.enterContext(connect(":memory:"))
        self.league = workbench.load(workbench.FIXTURES / "league.json")
        index = next(i for i, record in enumerate(self.league) if record["id"] == "score-f1")
        workbench.apply(self.db, self.league[:index])

    def apply(self, *events):
        return workbench.apply(self.db, events)

    def page(self, event, origin, start, stop, home, away, assignment="new-f1"):
        return op(event, "page", fixture="f1", assignment=assignment, origin=origin,
                  start=start, stop=stop, home=home, away=away)

    def copies(self):
        return [self.page("page-a", "doc-a", 0, 18, 2, 0),
                self.page("page-b", "doc-b", 12, 30, 1, 2),
                self.page("copy-zero", "doc-c", 12, 18, 0, 0),
                self.page("copy-one", "doc-c", 12, 18, 1, 0)]

    def confirm(self, event, home, away, claims):
        return op(event, "confirm", fixture="f1", assignment="new-f1", origin="doc-c",
                  start=12, stop=18, home=home, away=away, claims=claims)

    def tally(self, event, revision=1, minute=190, assignment="new-f1"):
        return op(event, "tally", fixture="f1", assignment=assignment, revision=revision, minute=minute)

    def test_same_page_set_supports_two_valid_confirmations(self):
        outcomes = {}
        for home, away in ((0, 0), (1, 0)):
            with connect(":memory:") as db:
                index = next(i for i, record in enumerate(self.league) if record["id"] == "score-f1")
                workbench.apply(db, self.league[:index])
                workbench.apply(db, self.copies())
                with self.assertRaisesRegex(Rejected, "score pages contradictory"):
                    workbench.apply(db, [self.tally("tally-early")])
                # the duty confirmation, not page arrival order, picks the scoring copy:
                # copy-zero arrived before copy-one, yet either one can be confirmed
                workbench.apply(db, [self.confirm("confirm-c", home, away, [[0, 0], [1, 0]])])
                analysis = pages.analyze(db, "new-f1")
                self.assertEqual(analysis["status"], "unique")
                expected = (3, 2) if (home, away) == (0, 0) else (2, 2)
                self.assertEqual((analysis["score"]["home"], analysis["score"]["away"]), expected)
                decision = workbench.apply(db, [self.tally("tally-f1")])
                self.assertEqual(decision[0]["decision"], "result-admitted")
                score = schedule.latest_result(db, "new-f1")
                workbench.apply(db, [self.league[-1]])
                replay = workbench.audit(db)
                self.assertEqual(replay["audit"], "verified")
                outcomes[(score["home"], score["away"])] = [row["points"]
                                                            for row in replay["standings"]["north"]]
        self.assertEqual(outcomes, {(3, 2): [3, 1, 1, 0], (2, 2): [1, 1, 1, 1]})

    def test_partial_pages_stay_open_with_witnesses_and_gaps(self):
        self.apply(self.page("page-a", "doc-a", 0, 18, 2, 0))
        report = pages.analyze(self.db, "new-f1")
        self.assertEqual(report["status"], "ambiguous")
        self.assertTrue(report["open"])
        self.assertEqual(report["gaps"], [[18, 30]])
        self.assertEqual(report["bounds"], {"home": [2, None], "away": [0, None]})
        self.assertEqual(report["witnesses"], [[[0, 0, 0], [18, 2, 0], [30, 2, 0]],
                                               [[0, 0, 0], [18, 2, 0], [30, 3, 0]]])
        with self.assertRaisesRegex(Rejected, "score pages ambiguous"):
            self.apply(self.tally("tally-half"))
        self.apply(self.page("page-b", "doc-b", 12, 30, 1, 2))
        report = pages.analyze(self.db, "new-f1")
        self.assertEqual(report["status"], "ambiguous")
        self.assertFalse(report["open"])
        self.assertEqual(report["gaps"], [])
        self.assertEqual(report["bounds"], {"home": [2, 3], "away": [2, 2]})
        self.assertEqual(report["witnesses"], [[[0, 0, 0], [12, 1, 0], [18, 2, 0], [30, 2, 2]],
                                               [[0, 0, 0], [12, 2, 0], [18, 2, 0], [30, 3, 2]]])

    def test_contradictory_pages_name_a_non_coexisting_set(self):
        self.apply(self.page("page-p1", "doc-p1", 0, 10, 3, 0),
                   self.page("page-p2", "doc-p2", 10, 20, 0, 0),
                   self.page("page-p3", "doc-p3", 0, 20, 1, 0))
        report = pages.analyze(self.db, "new-f1")
        self.assertEqual(report["status"], "contradictory")
        self.assertEqual(report["reason"], "inconsistent")
        self.assertEqual(report["conflict"], ["page-p1", "page-p3"])
        with self.assertRaisesRegex(Rejected, "score pages contradictory"):
            self.apply(self.tally("tally-bad"))

    def test_page_interval_must_fit_the_match(self):
        with self.assertRaisesRegex(Rejected, "outside match"):
            self.apply(self.page("page-wide", "doc-w", 0, 31, 0, 0))
        with self.assertRaisesRegex(Rejected, "invalid page home delta"):
            self.apply(self.page("page-negative", "doc-n", 0, 10, -1, 0))

    def test_single_page_billion_score_stays_constant_work(self):
        billion = 10 ** 9
        self.apply(self.page("page-big", "doc-big", 0, 30, billion, billion))
        report = pages.analyze(self.db, "new-f1")
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["score"], {"home": billion, "away": billion})
        self.assertEqual(self.apply(self.tally("tally-big"))[0]["decision"], "result-admitted")
        self.assertEqual(schedule.latest_result(self.db, "new-f1")["home"], billion)

    def test_late_copy_after_confirmation_needs_fresh_confirmation(self):
        self.apply(*self.copies())
        self.apply(self.confirm("confirm-c", 0, 0, [[0, 0], [1, 0]]))
        # retransmitting a confirmed claim does not disturb the group
        self.apply(self.page("copy-zero-again", "doc-c", 12, 18, 0, 0))
        self.assertEqual(pages.analyze(self.db, "new-f1")["status"], "unique")
        # a new claim in the same group suspends the confirmation for scoring
        self.apply(self.page("copy-two", "doc-c", 12, 18, 2, 0))
        report = pages.analyze(self.db, "new-f1")
        self.assertEqual(report["status"], "contradictory")
        self.assertEqual(report["reason"], "unconfirmed-copies")
        self.assertEqual(report["conflict"], ["copy-one", "copy-two", "copy-zero", "copy-zero-again"])
        with self.assertRaisesRegex(Rejected, "score pages contradictory"):
            self.apply(self.tally("tally-stale"))
        # the old binding no longer matches the group; a fresh complete binding is required
        with self.assertRaisesRegex(Rejected, "complete conflict group"):
            self.apply(self.confirm("confirm-stale", 0, 0, [[0, 0], [1, 0]]))
        self.apply(self.confirm("confirm-c2", 0, 0, [[0, 0], [1, 0], [2, 0]]))
        self.assertEqual(self.apply(self.tally("tally-f1"))[0]["decision"], "result-admitted")
        self.assertEqual(schedule.latest_result(self.db, "new-f1")["home"], 3)

    def test_late_patrol_closing_court_blocks_tally_but_not_review(self):
        self.apply(*self.copies())
        self.apply(self.confirm("confirm-c", 0, 0, [[0, 0], [1, 0]]))
        self.assertEqual(pages.analyze(self.db, "new-f1")["status"], "unique")
        self.apply(op("late-soak", "inspect", court="annex", minute=155, wet=3))
        self.assertEqual(pages.analyze(self.db, "new-f1")["status"], "unique")
        with self.assertRaisesRegex(Rejected, "court-closed"):
            self.apply(self.tally("tally-soaked"))

    def test_pages_stay_with_their_occupancy_revision(self):
        self.apply(*self.copies())
        self.apply(self.confirm("confirm-c", 0, 0, [[0, 0], [1, 0]]))
        self.apply(op("soak", "inspect", court="annex", minute=158, wet=3),
                   op("dry-east", "inspect", court="east", minute=150, wet=0),
                   op("offer-again", "promise", fixture="f1", prior="new-f1", court="east",
                      start=195, official="ref-a", expires=190),
                   op("sign-o2", "consent", promise="offer-again", person="owls", minute=170),
                   op("sign-f2", "consent", promise="offer-again", person="foxes", minute=171),
                   op("sign-r2", "consent", promise="offer-again", person="ref-a", minute=172),
                   op("move-again", "move", promise="offer-again", minute=185))
        self.assertEqual(schedule.current(self.db, "f1")["rev"], 3)
        with self.assertRaisesRegex(Rejected, "stale assignment"):
            self.apply(self.tally("tally-old"))
        self.assertEqual(pages.analyze(self.db, "new-f1")["status"], "unique")
        self.assertEqual(pages.analyze(self.db, "move-again")["status"], "ambiguous")
        with self.assertRaisesRegex(Rejected, "score pages ambiguous"):
            self.apply(self.tally("tally-new", assignment="move-again", minute=225))
        self.apply(self.page("page-n", "doc-n", 0, 30, 4, 1, assignment="move-again"))
        decision = self.apply(self.tally("tally-new", assignment="move-again", minute=225))
        self.assertEqual(decision[0]["decision"], "result-admitted")
        self.assertEqual(schedule.latest_result(self.db, "move-again")["home"], 4)

    def test_ledgered_score_survives_late_pages_and_needs_recall_to_change(self):
        self.apply(*self.copies())
        self.apply(self.confirm("confirm-c", 0, 0, [[0, 0], [1, 0]]))
        self.apply(self.tally("tally-f1"))
        self.apply(self.league[-1])
        with self.assertRaisesRegex(Rejected, "recall the active settlement"):
            self.apply(self.tally("tally-rev2", revision=2, minute=195))
        # a late independent page is retained but never rewrites the ledgered score
        self.apply(self.page("page-late", "doc-late", 0, 30, 9, 9))
        self.assertEqual(pages.analyze(self.db, "new-f1")["status"], "contradictory")
        self.assertEqual(schedule.latest_result(self.db, "new-f1")["home"], 3)
        replay = workbench.audit(self.db)
        self.assertEqual(replay["audit"], "verified")
        self.assertEqual(replay["standings"]["north"][0],
                         {"team": "owls", "points": 3, "difference": 1, "played": 1, "rank": 1})



