import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from courts_rain import eligibility, grounds, handoff, ledger, schedule, workbench
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


def fragment(machine, seq, event, action, **fields):
    return {"machine": machine, "seq": seq, **op(event, action, **fields)}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(connect(":memory:"))
        self.league = workbench.load(workbench.FIXTURES / "league.json")
        self.contest = workbench.load(workbench.FIXTURES / "handoff-contest.json")
        self.longer = workbench.load(workbench.FIXTURES / "handoff-longer.json")

    def base_before(self, event):
        index = next(i for r, i in ((r, i) for i, r in enumerate(self.league)) if r["id"] == event)
        workbench.apply(self.db, self.league[:index])
        return index

    def plans(self, items):
        return workbench.plan_batch(self.db, items)["plans"]

    def assertTrialWritesNothing(self, items):
        before = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        workbench.plan_batch(self.db, items)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM device_journal").fetchone()[0], 0)

    def test_contested_slot_two_equal_max_plans_never_merged(self):
        self.base_before("old-f1")
        result = workbench.plan_batch(self.db, self.contest)
        self.assertEqual(result["max_new_originals"], 1)
        self.assertEqual(result["plan_count"], 2)
        prefixes = [tuple(sorted(p["machine_prefixes"].items())) for p in result["plans"]]
        self.assertIn((("pavilion-a", 1), ("pavilion-b", 0)), prefixes)
        self.assertIn((("pavilion-a", 0), ("pavilion-b", 1)), prefixes)
        for plan in result["plans"]:
            self.assertEqual(plan["new_originals"], 1)
            self.assertEqual(len(plan["execution_order"]), 1)
            held = {(h["machine"], h["id"]): h for h in plan["holdovers"]}
            self.assertEqual(len(held), 1)
        take_a = next(p for p in result["plans"] if p["machine_prefixes"]["pavilion-a"] == 1)
        self.assertEqual(take_a["execution_order"][0]["id"], "pad-f1")
        self.assertEqual(take_a["holdovers"][0]["id"], "pbd-f2")
        self.assertIn("court occupied", take_a["holdovers"][0]["reason"])
        take_b = next(p for p in result["plans"] if p["machine_prefixes"]["pavilion-b"] == 1)
        self.assertEqual(take_b["execution_order"][0]["id"], "pbd-f2")
        self.assertEqual(take_b["holdovers"][0]["id"], "pad-f1")
        self.assertIn("court occupied", take_b["holdovers"][0]["reason"])
        self.assertTrialWritesNothing(self.contest)

    def test_commit_writes_selected_plan_and_replay_carries_the_journal(self):
        self.base_before("old-f1")
        trial = workbench.plan_batch(self.db, self.contest)
        take_b = next(p for p in trial["plans"] if p["machine_prefixes"]["pavilion-b"] == 1)
        committed = workbench.commit_batch(self.db, self.contest, take_b["machine_prefixes"])
        self.assertTrue(committed["committed"])
        self.assertEqual(committed["new_originals"], 1)
        self.assertEqual([d["decision"] for d in committed["decisions"]], ["slot-assigned"])
        self.assertIsNotNone(one(self.db, "SELECT * FROM assignments WHERE id='pbd-f2'"))
        self.assertIsNone(one(self.db, "SELECT * FROM assignments WHERE id='pad-f1'"))
        rows = {r[0]: (r[1], r[2]) for r in
                self.db.execute("SELECT id,machine,seq FROM device_journal ORDER BY id")}
        self.assertEqual(rows, {"pbd-f2": ("pavilion-b", 1)})
        audit = workbench.audit(self.db)
        self.assertEqual(audit["audit"], "verified")
        self.assertEqual(audit["drift"], [])

    def test_commit_all_or_nothing_when_revalidation_rejects(self):
        self.base_before("old-f1")
        # Simulate a base ledger that moved between planning and submission.
        workbench.apply(self.db, [op("slot-f2-early", "assign", fixture="f2",
                                     court="west", start=115, official="ref-b")])
        vector = {"pavilion-a": 1, "pavilion-b": 1}
        with self.assertRaises(Rejected):
            workbench.commit_batch(self.db, self.contest, vector)
        self.assertIsNone(one(self.db, "SELECT * FROM assignments WHERE id='pad-f1'"))
        self.assertIsNone(one(self.db, "SELECT * FROM assignments WHERE id='pbd-f2'"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM device_journal").fetchone()[0], 0)

    def test_longer_prefix_should_win_the_handover(self):
        self.base_before("old-f1")
        result = workbench.plan_batch(self.db, self.longer)
        self.assertEqual(result["plan_count"], 1)
        winner = result["plans"][0]
        self.assertEqual(result["max_new_originals"], 4)
        self.assertEqual(winner["machine_prefixes"],
                         {"pavilion-a": 0, "pavilion-b": 4})
        self.assertEqual([e["id"] for e in winner["execution_order"]],
                         ["pbd-f2", "pbd-score-1", "pbd-score-2", "pbd-score-3"])
        self.assertEqual(winner["holdovers"][0]["id"], "pad-f1")
        # The wrong handover (A first) can still only admit one original and
        # must freeze B's entire dependent prefix behind its blocked head.
        with connect(":memory:") as other:
            handoff.backup(self.db, other)
            chosen = workbench.commit_batch(other, self.longer,
                                            {"pavilion-a": 1, "pavilion-b": 0})
            self.assertEqual(chosen["new_originals"], 1)
            with self.assertRaises(Rejected):
                workbench.commit_batch(other, self.longer,
                                       {"pavilion-a": 1, "pavilion-b": 4})

    def test_confirmed_sequences_retransmit_and_same_number_conflicts(self):
        self.base_before("old-f1")
        take_b = next(p for p in workbench.plan_batch(self.db, self.contest)["plans"]
                      if p["machine_prefixes"]["pavilion-b"] == 1)
        workbench.commit_batch(self.db, self.contest, take_b["machine_prefixes"])
        # An identical retransmission of the confirmed raw sequence is safe.
        again = workbench.plan_batch(self.db, self.contest)
        self.assertEqual(again["max_new_originals"], 0)
        self.assertEqual(again["plans"][0]["machines"]["pavilion-b"]["new"], 0)
        committed = workbench.commit_batch(self.db, self.contest,
                                           again["plans"][0]["machine_prefixes"])
        self.assertEqual(committed["new_originals"], 0)
        self.assertEqual(committed["decisions"][0]["decision"], "duplicate")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM events WHERE id='pbd-f2'").fetchone()[0], 1)
        # Same machine, same raw sequence, different original: explicit conflict.
        clash = [fragment("pavilion-b", 1, "pbd-f2-clash", "assign",
                          fixture="f1", court="west", start=115, official="ref-a")]
        with self.assertRaisesRegex(Rejected, "device sequence conflict: pavilion-b#1"):
            workbench.plan_batch(self.db, clash)
        with self.assertRaisesRegex(Rejected, "device sequence conflict: pavilion-b#1"):
            workbench.commit_batch(self.db, clash, {"pavilion-b": 1})
        self.assertIsNone(one(self.db, "SELECT 1 FROM events WHERE id='pbd-f2-clash'"))

    def test_failure_on_one_machine_never_blocks_another_prefix(self):
        self.base_before("old-f1")
        # pavilion-a's head fails adjudication on its own merits (unknown
        # official); pavilion-b's independent prefix must still land intact.
        broken = [fragment("pavilion-a", 1, "pad-broken", "assign",
                           fixture="f1", court="west", start=115, official="ref-ghost"),
                  fragment("pavilion-b", 1, "pbd-f2", "assign",
                           fixture="f2", court="west", start=130, official="ref-b")]
        result = workbench.plan_batch(self.db, broken)
        self.assertEqual(result["max_new_originals"], 1)
        plan = result["plans"][0]
        self.assertEqual(plan["machines"]["pavilion-a"]["prefix"], 0)
        self.assertIn("unknown officials", plan["machines"]["pavilion-a"]["reason"])
        self.assertEqual(plan["machines"]["pavilion-b"]["prefix"], 1)
        self.assertEqual(plan["execution_order"][0]["id"], "pbd-f2")
        self.assertTrialWritesNothing(broken)

    def test_blocked_head_freezes_the_whole_machine_prefix(self):
        self.base_before("old-f1")
        # pavilion-b's head is illegal standalone (unknown official), but its
        # second event would be fine on its own. The prefix rule still holds.
        frozen = [
            fragment("pavilion-b", 1, "pbd-bad", "assign",
                     fixture="f2", court="west", start=130, official="ref-ghost"),
            fragment("pavilion-b", 2, "pbd-look", "inspect",
                     court="west", minute=128, wet=0),
        ]
        result = workbench.plan_batch(self.db, frozen)
        self.assertEqual(result["max_new_originals"], 0)
        info = result["plans"][0]["machines"]["pavilion-b"]
        self.assertEqual(info["prefix"], 0)
        self.assertIn("unknown officials", info["reason"])
        # The independently-legal second event is held behind its blocked head.
        held = result["plans"][0]["holdovers"][0]
        self.assertEqual(held["id"], "pbd-bad")
        self.assertEqual(held["remaining"], ["pbd-bad", "pbd-look"])

    def test_maximum_counts_new_originals_not_retransmitted_prefixes(self):
        self.base_before("old-f1")
        # Put pavilion-b's seq-1 original into the base through the normal
        # journal, then re-offer it as a safe retransmission followed by a
        # genuinely new event. The optimum must count originals, not rows.
        take_b = next(p for p in workbench.plan_batch(self.db, self.contest)["plans"]
                      if p["machine_prefixes"]["pavilion-b"] == 1)
        workbench.commit_batch(self.db, self.contest, take_b["machine_prefixes"])
        # pavilion-c independently contests a fresh slot on annex with no clash.
        mixed = [
            fragment("pavilion-b", 1, "pbd-f2", "assign",
                     fixture="f2", court="west", start=115, official="ref-b"),
            fragment("pavilion-b", 2, "pbd-extra", "inspect",
                     court="west", minute=112, wet=0),
            fragment("pavilion-c", 1, "pcd-f1", "assign",
                     fixture="f1", court="annex", start=120, official="ref-a"),
        ]
        result = workbench.plan_batch(self.db, mixed)
        plan = result["plans"][0]
        self.assertEqual(plan["machines"]["pavilion-b"]["prefix"], 2)
        self.assertEqual(plan["machines"]["pavilion-b"]["new"], 1)  # seq 1 is a duplicate
        self.assertEqual(plan["machines"]["pavilion-c"]["new"], 1)
        self.assertEqual(result["max_new_originals"], 2)
        self.assertEqual(
            sum(1 for d in plan["execution_order"] if d["id"] == "pbd-f2"), 1)

    def _eight_machine_batch(self):
        events = [op(f"div-{i}", "division", win=3, draw=1, loss=0, duration=30, spread=60)
                  for i in range(8)]
        events += [op(f"court-{i}", "court", surface="porous") for i in range(8)]
        for i in range(8):
            events += [
                op(f"team-{i}-h", "team", division=f"div-{i}"),
                op(f"team-{i}-a", "team", division=f"div-{i}"),
                op(f"ref-{i}", "official", division=f"div-{i}"),
                op(f"win-{i}", "window", kind="team", person=f"team-{i}-h",
                   start=0, stop=400, available=True),
                op(f"win-{i}a", "window", kind="team", person=f"team-{i}-a",
                   start=0, stop=400, available=True),
                op(f"win-{i}r", "window", kind="official", person=f"ref-{i}",
                   start=0, stop=400, available=True),
                op(f"look-{i}", "inspect", court=f"court-{i}", minute=90, wet=0),
                op(f"round-{i}", "round", division=f"div-{i}",
                   earliest=0, deadline=400, expected=1),
                op(f"fixture-{i}", "fixture", round=f"round-{i}",
                   home=f"team-{i}-h", away=f"team-{i}-a"),
            ]
        workbench.apply(self.db, events)
        batch = []
        for i in range(8):
            machine = f"pavilion-{i}"
            start = 100 + 2 * i
            batch.append(fragment(machine, 1, f"assign-{i}", "assign",
                                  fixture=f"fixture-{i}", court=f"court-{i}",
                                  start=start, official=f"ref-{i}"))
            for seq in range(2, 22):
                minute = start + 40 + seq
                batch.append(fragment(machine, seq, f"score-{i}-{seq}", "result",
                                      fixture=f"fixture-{i}", assignment=f"assign-{i}",
                                      revision=seq - 1, minute=minute, home=seq, away=0))
        return batch

    def test_eight_machines_resolve_within_three_seconds_without_enumerating_interleavings(self):
        batch = self._eight_machine_batch()
        self.assertEqual(len(batch), 8 * 21)
        started = time.perf_counter()
        result = workbench.plan_batch(self.db, batch)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0)
        self.assertEqual(result["max_new_originals"], len(batch))
        self.assertEqual(result["plan_count"], 1)
        plan = result["plans"][0]
        self.assertEqual(set(plan["machine_prefixes"].values()), {21})
        self.assertEqual(len(plan["execution_order"]), len(batch))
        self.assertEqual(plan["holdovers"], [])
        committed = workbench.commit_batch(self.db, batch, plan["machine_prefixes"])
        self.assertEqual(committed["new_originals"], len(batch))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM device_journal").fetchone()[0], len(batch))
        audit = workbench.audit(self.db)
        self.assertEqual(audit["audit"], "verified")
        self.assertEqual(audit["events"], self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_legacy_single_file_apply_still_works_without_provenance(self):
        self.base_before("old-f1")
        decisions = workbench.apply(self.db, [op("plain", "court", surface="synthetic")])
        self.assertEqual(decisions[0]["decision"], "court-registered")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM device_journal").fetchone()[0], 0)
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")

    def test_cli_plan_and_commit_round_trip(self):
        self.base_before("old-f1")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shift.sqlite"
            source = Path(tmp) / "batch.json"
            source.write_text(json.dumps(self.contest), encoding="utf-8")
            with connect(target) as fresh:
                handoff.backup(self.db, fresh)
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            plan = subprocess.run([sys.executable, "-B", "-m", "courts_rain", "plan",
                                   "--db", str(target), str(source)],
                                  capture_output=True, text=True, env=env, check=True)
            self.assertEqual(json.loads(plan.stdout)["max_new_originals"], 1)
            commit = subprocess.run([sys.executable, "-B", "-m", "courts_rain", "commit",
                                     "--db", str(target), "--choice", "1", str(source)],
                                    capture_output=True, text=True, env=env, check=True)
            payload = json.loads(commit.stdout)
            self.assertTrue(payload["commit"]["committed"])
            audit = subprocess.run([sys.executable, "-B", "-m", "courts_rain", "audit",
                                    "--db", str(target)],
                                   capture_output=True, text=True, env=env, check=True)
            self.assertEqual(json.loads(audit.stdout)["audit"], "verified")
