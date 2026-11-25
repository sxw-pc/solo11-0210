"""Shift handoff for offline collection machines.

Each pavilion's recorder exports its own ordered raw sequence. A machine can
only ever contribute a *continuous prefix* of its export: once an event is
rejected by the existing business adjudication, nothing later on that machine
may pass.  Machines must never be ordered by export timestamps; planning
works out the largest legal intake from the base ledger's actual state and
the cross-machine interactions of the events themselves.

Trial runs use an isolated in-memory copy of the live database, so no court,
slot, sequence or journal row is written during read-only planning.
"""
from contextlib import contextmanager
from dataclasses import dataclass

from .db import Rejected, backup, connect, judge, require


@dataclass(frozen=True)
class Entry:
    machine: str
    seq: int
    record: dict


@dataclass(frozen=True)
class Exports:
    machines: dict          # machine -> tuple[Entry, ...] in raw sequence order
    by_id: dict             # event id -> (machine, seq)


def parse_exports(items):
    """Parse a flat export fragment list into per-machine sequences.

    Each fragment carries ``machine`` and ``seq`` alongside the normal event
    fields (``id``/``op``). No export timestamp is used anywhere.
    """
    require(isinstance(items, list), "batch must be an array of fragments")
    grouped = {}
    by_id = {}
    for fragment in items:
        require(isinstance(fragment, dict), "fragment must be an object")
        machine = fragment.get("machine")
        raw = fragment.get("seq")
        require(isinstance(machine, str) and bool(machine.strip()), "fragment needs a machine")
        require(type(raw) is int and raw >= 1, "fragment needs a positive sequence")
        record = {k: v for k, v in fragment.items() if k not in ("machine", "seq")}
        require(isinstance(record.get("id"), str) and bool(record["id"].strip()), "fragment needs an event id")
        if record["id"] in by_id:
            other = by_id[record["id"]]
            require(other == (machine, raw),
                    f"event {record['id']} exported twice with different provenance")
        else:
            by_id[record["id"]] = (machine, raw)
        bucket = grouped.setdefault(machine, {})
        require(raw not in bucket, f"duplicate export sequence: {machine}#{raw}")
        bucket[raw] = Entry(machine, raw, record)
    machines = {}
    for machine, bucket in grouped.items():
        ordered = tuple(bucket[seq] for seq in sorted(bucket))
        require(tuple(entry.seq for entry in ordered) == tuple(range(1, len(ordered) + 1)),
                f"machine {machine} export is not a continuous sequence from 1")
        machines[machine] = ordered
    return Exports(machines=machines, by_id=by_id)


@contextmanager
def scratch(base):
    """An isolated, read-only-to-the-base copy used for trial adjudication."""
    with connect(":memory:") as trial:
        backup(base, trial)
        yield trial


def replay(db, entries, actions):
    """Apply machine prefixes one machine at a time, preserving each order."""
    decisions = []
    for entry in entries:
        decisions.append(judge(db, entry.record, actions, (entry.machine, entry.seq)))
    return decisions


def _rejected_reason(exc):
    return str(exc)


def _standalone_prefixes(exports, actions, base):
    """Each machine's reachable prefix against an untouched base copy."""
    ceilings = {}
    for machine, entries in exports.machines.items():
        with scratch(base) as trial:
            applied = 0
            reason = None
            for entry in entries:
                try:
                    verdict = judge(trial, entry.record, actions, (machine, entry.seq))
                except Rejected as exc:
                    text = _rejected_reason(exc)
                    # Provenance conflicts are hard batch errors, not a
                    # prefix boundary the operator could plan around.
                    if text.startswith("device sequence conflict"):
                        raise
                    reason = text
                    break
                applied += 1 if verdict["decision"] != "duplicate" else 0
            ceilings[machine] = {
                "length": len(entries) if reason is None else entries[applied].seq - 1,
                "new": applied,
                "reason": reason,
            }
    return ceilings


def _round_robin(exports, lengths, actions, base):
    """Interleave per-machine prefixes, freezing a machine at first refusal."""
    order = sorted(exports.machines)
    entries = {m: list(exports.machines[m]) for m in order}
    position = {m: 0 for m in order}
    chosen = {m: [] for m in order}
    frozen = set()
    with scratch(base) as trial:
        progressed = True
        while progressed:
            progressed = False
            for machine in order:
                if machine in frozen or position[machine] >= lengths[machine]:
                    continue
                entry = entries[machine][position[machine]]
                try:
                    judge(trial, entry.record, actions, (machine, entry.seq))
                except Rejected:
                    frozen.add(machine)
                    continue
                chosen[machine].append(entry)
                position[machine] += 1
                progressed = True
    return chosen


def _reachable(exports, actions, base, machine, length, other, other_length, cache):
    """Can ``machine`` take its first ``length`` entries after ``other``'s prefix?"""
    if length == 0:
        return True
    key = (other, other_length, machine, length)
    if key in cache:
        return cache[key]
    entries = exports.machines[machine][:length]
    with scratch(base) as trial:
        for entry in exports.machines[other][:other_length]:
            judge(trial, entry.record, actions, (entry.machine, entry.seq))
        ok = True
        for entry in entries:
            try:
                judge(trial, entry.record, actions, (machine, entry.seq))
            except Rejected:
                ok = False
                break
    cache[key] = ok
    return ok


def _best_vectors(exports, ceilings, actions, base):
    """Search the per-machine prefix-length space; pairs bound each other.

    Interleavings themselves are never enumerated. Each length pair is tested
    on a scratch database, and memoised prefix reachability drives a depth
    first search over machines.
    """
    names = sorted(exports.machines)
    reach_cache = {}
    count_cache = {}

    def new_count(machine, length):
        key = (machine, length)
        if key in count_cache:
            return count_cache[key]
        with scratch(base) as trial:
            total = 0
            for entry in exports.machines[machine][:length]:
                verdict = judge(trial, entry.record, actions, (machine, entry.seq))
                total += verdict["decision"] != "duplicate"
        count_cache[key] = total
        return total

    def search(index, vector):
        if index == len(names):
            total = sum(new_count(m, length) for m, length in vector.items())
            if total > best["new"]:
                best["new"] = total
                best["vectors"] = [dict(vector)]
            elif total == best["new"]:
                best["vectors"].append(dict(vector))
            return
        machine = names[index]
        for length in range(ceilings[machine]["length"], -1, -1):
            if all(_reachable(exports, actions, base, machine, length, other, vector[other], reach_cache)
                   and _reachable(exports, actions, base, other, vector[other], machine, length, reach_cache)
                   for other in names[:index]):
                vector[machine] = length
                search(index + 1, vector)
        vector.pop(machine, None)

    best = {"new": -1, "vectors": []}
    search(0, {})
    return best["vectors"], best["new"]


def _materialize(exports, vector, actions, base):
    """Replay one chosen length vector and collect the execution order."""
    plan = []
    machines = {}
    valid = True
    with scratch(base) as trial:
        for machine in sorted(exports.machines):
            entries = exports.machines[machine]
            target = vector[machine]
            applied = 0
            new = 0
            reason = None
            for entry in entries:
                if applied >= target:
                    break
                try:
                    verdict = judge(trial, entry.record, actions, (machine, entry.seq))
                except Rejected as exc:
                    reason = _rejected_reason(exc)
                    break
                plan.append(entry)
                if verdict["decision"] != "duplicate":
                    new += 1
                applied += 1
            if applied != target:
                valid = False
            machines[machine] = {
                "prefix": applied, "exported": len(entries), "new": new, "reason": reason,
            }
        # Probe every machine's blocked head on a fresh copy that already
        # contains the whole plan, so cross-machine blocking is visible.
        head_reasons = {}
        with scratch(trial) as planned:
            for machine in sorted(exports.machines):
                applied = machines[machine]["prefix"]
                entries = exports.machines[machine]
                if applied >= len(entries):
                    continue
                head = entries[applied]
                try:
                    judge(planned, head.record, actions, (machine, head.seq))
                except Rejected as exc:
                    head_reasons[(machine, head.seq)] = _rejected_reason(exc)
    held = []
    for machine in sorted(exports.machines):
        entries = exports.machines[machine]
        applied = machines[machine]["prefix"]
        later = entries[applied:]
        if not later:
            continue
        blocked = later[0]
        reason = head_reasons.get((machine, blocked.seq), "prefix held back for a later shift")
        held.append({"machine": machine, "seq": blocked.seq, "id": blocked.record["id"],
                     "reason": reason, "remaining": [e.record["id"] for e in later]})
    for machine, info in machines.items():
        if info["reason"] is None and info["prefix"] < info["exported"]:
            info["reason"] = head_reasons.get((machine, exports.machines[machine][info["prefix"]].seq))
    return plan, machines, held, valid


def plan_batch(db, exports_or_items, actions):
    """Read-only trial: maximum legal new originals and every max-size plan."""
    exports = parse_exports(exports_or_items) if not isinstance(exports_or_items, Exports) else exports_or_items
    ceilings = _standalone_prefixes(exports, actions, db)
    joint = _round_robin(
        exports, {m: c["length"] for m, c in ceilings.items()}, actions, db)
    if all(len(joint[m]) == ceilings[m]["length"] for m in exports.machines):
        vector = {m: ceilings[m]["length"] for m in exports.machines}
        vectors = [vector]
    else:
        vectors, _ = _best_vectors(exports, ceilings, actions, db)

    plans = []
    for vector in vectors:
        ordered, machines, held, valid = _materialize(exports, vector, actions, db)
        if not valid:
            continue
        plans.append({
            "machine_prefixes": {m: machines[m]["prefix"] for m in sorted(machines)},
            "new_originals": sum(item["new"] for item in machines.values()),
            "execution_order": [
                {"machine": e.machine, "seq": e.seq, "id": e.record["id"]} for e in ordered
            ],
            "holdovers": held,
            "machines": machines,
        })
    require(plans, "no executable plan could be derived")
    return {
        "max_new_originals": plans[0]["new_originals"],
        "plan_count": len(plans),
        "plans": plans,
        "standalone": ceilings,
        "machines": sorted(exports.machines),
    }


def commit_batch(db, exports_or_items, vector, actions):
    """Re-validate a chosen plan on the current state, then commit atomically.

    Every rejected event rolls the whole transaction back; no partial write is
    possible. The plan is replayed in its declared legal order first, so a base
    ledger that changed after planning cannot be silently overwritten.
    """
    exports = parse_exports(exports_or_items) if not isinstance(exports_or_items, Exports) else exports_or_items
    require(isinstance(vector, dict), "plan must map machine to prefix length")
    chosen = []
    for machine in sorted(exports.machines):
        length = vector.get(machine, 0)
        require(type(length) is int and 0 <= length <= len(exports.machines[machine]),
                f"plan length out of range for {machine}")
        chosen.extend(exports.machines[machine][:length])

    with scratch(db) as trial:
        replay(trial, chosen, actions)

    # One transaction for the whole handoff: any rejected event rolls back
    # every machine's writes, so no partial intake is ever visible.
    decisions = []
    db.execute("BEGIN IMMEDIATE")
    try:
        for entry in chosen:
            decisions.append(judge(db, entry.record, actions, (entry.machine, entry.seq)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {
        "committed": True,
        "new_originals": sum(d["decision"] != "duplicate" for d in decisions),
        "decisions": decisions,
        "execution_order": [
            {"machine": e.machine, "seq": e.seq, "id": e.record["id"]} for e in chosen
        ],
    }
