import json
import sqlite3
import unittest

from courts_rain import eligibility, grounds, ledger, schedule, workbench
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
