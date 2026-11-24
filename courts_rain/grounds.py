"""Physical playability at an assignment's starting minute."""
from .db import entity, integer, one, require


# Maximum wetness on the local 0–3 checklist, and evidence life in minutes.
SURFACES = {"acrylic": (0, 60), "porous": (1, 90), "synthetic": (2, 45)}


def add_court(db, event, surface):
    require(isinstance(surface, str) and surface in SURFACES, "unknown surface")
    db.execute("INSERT INTO courts VALUES (?,?)", (event, surface))
    return "court-registered"


def inspect(db, event, court, minute, wet, debris=False):
    entity(db, "courts", court)
    integer(minute, "inspection minute")
    require(integer(wet, "wetness") <= 3 and type(debris) is bool, "invalid inspection")
    db.execute("INSERT INTO inspections VALUES (?,?,?,?,?)", (event, court, minute, wet, debris))
    return {"inspection": event, **condition(db, court, minute)}


def condition(db, court, minute, known_by=None):
    site = entity(db, "courts", court)
    integer(minute, "court minute")
    record = one(db, """SELECT * FROM inspections WHERE court=? AND minute<=?
                         ORDER BY minute DESC LIMIT 1""", court, min(minute, known_by) if known_by is not None else minute)
    limit, lifetime = SURFACES[site["surface"]]
    if record is None:
        return {"status": "unverified", "inspection": None}
    if minute - record["minute"] > lifetime:
        status = "stale"
    elif record["debris"] or record["wet"] > limit:
        status = "closed"
    else:
        status = "open"
    return {"status": status, "inspection": record["id"]}
