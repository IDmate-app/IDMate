"""Vehicle delete with a 14-day undo.

Deleting a vehicle only removes its row (plus, by foreign-key cascade, its
battery history and per-user access rows). Trips and charges stay untouched.
Before the delete the row and its cascaded children are stored as JSON in
``vehicle_trash``; a restore puts everything back under the same id, so the
users' active/default vehicle pointers fit again."""

import json
from datetime import datetime, timedelta, timezone

KEEP_DAYS = 14


def _now():
    return datetime.now(timezone.utc)


def _rows(db, sql, args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def purge(db):
    cut = (_now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("DELETE FROM vehicle_trash WHERE deleted_at < ?", (cut,))


def preview(db, vehicle_id):
    """What a delete would touch: the vehicle's fields and how many trips and
    charges belong to it. None if the vehicle does not exist."""
    v = db.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
    if not v:
        return None
    device = (v["device"] or "").strip()
    trips = db.execute("SELECT COUNT(*) FROM trips WHERE device = ?",
                       (device,)).fetchone()[0] if device else 0
    charges = db.execute("SELECT COUNT(*) FROM charge_sessions WHERE vehicle_plate = ?",
                         (v["plate"],)).fetchone()[0]
    shared = [r[0] or r[1] for r in db.execute(
        "SELECT name, plate FROM vehicles WHERE device = ? AND id != ?",
        (device, vehicle_id)).fetchall()] if device else []
    return {"id": v["id"], "name": v["name"] or "", "plate": v["plate"],
            "device": device, "trips": trips, "charges": charges,
            "shared_device_with": shared}


def message(p, t):
    """Confirm text for a preview, in the user's language (t = translations)."""
    none = t["vehicle_del_none"]
    lines = [t["vehicle_del_title"].replace("{0}", p["name"] or p["plate"]),
             "",
             f'{t["vehicle_del_name"]}: {p["name"] or none}',
             f'{t["vehicle_del_plate"]}: {p["plate"]}',
             f'{t["vehicle_del_device"]}: {p["device"] or none}',
             t["vehicle_del_affects"].replace("{0}", str(p["trips"]))
                                     .replace("{1}", str(p["charges"]))]
    if p["shared_device_with"]:
        lines.append(t["vehicle_del_shared"].replace("{0}", ", ".join(p["shared_device_with"])))
    lines += ["", t["vehicle_del_keep"]]
    return "\n".join(lines)


def trash(db, vehicle_id, by=None):
    """Snapshot the vehicle and delete it. False if it does not exist."""
    v = db.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
    if not v:
        return False
    payload = {
        "vehicle": dict(v),
        "batteries": _rows(db, "SELECT * FROM vehicle_batteries WHERE vehicle_id = ?", (vehicle_id,)),
        "access": _rows(db, "SELECT * FROM user_vehicle_access WHERE vehicle_id = ?", (vehicle_id,)),
        "active_users": [r["id"] for r in db.execute(
            "SELECT id FROM users WHERE active_vehicle_id = ?", (vehicle_id,))],
        "default_users": [r["id"] for r in db.execute(
            "SELECT id FROM users WHERE default_vehicle_id = ?", (vehicle_id,))],
    }
    db.execute(
        "INSERT INTO vehicle_trash (vehicle_id, label, payload, deleted_at, deleted_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (vehicle_id, v["name"] or v["plate"], json.dumps(payload),
         _now().strftime("%Y-%m-%d %H:%M:%S"), by))
    db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    purge(db)
    return True


def listing(db):
    purge(db)
    out = []
    for r in db.execute("SELECT * FROM vehicle_trash ORDER BY deleted_at DESC").fetchall():
        v = json.loads(r["payload"])["vehicle"]
        deleted = datetime.strptime(r["deleted_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        left = KEEP_DAYS - (_now() - deleted).days
        out.append({"id": r["id"], "label": r["label"], "name": v.get("name") or "",
                    "plate": v.get("plate"), "device": v.get("device") or "",
                    "deleted_at": r["deleted_at"], "days_left": max(left, 0)})
    return out


def _insert(db, table, row):
    cols = list(row)
    db.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
               [row[c] for c in cols])


def restore(db, trash_id):
    """Put a trashed vehicle back. Returns None on success, else an error key:
    'not_found', 'plate_taken' or 'id_taken'."""
    purge(db)
    r = db.execute("SELECT * FROM vehicle_trash WHERE id = ?", (trash_id,)).fetchone()
    if not r:
        return "not_found"
    p = json.loads(r["payload"])
    v = p["vehicle"]
    if db.execute("SELECT 1 FROM vehicles WHERE plate = ?", (v["plate"],)).fetchone():
        return "plate_taken"
    if db.execute("SELECT 1 FROM vehicles WHERE id = ?", (v["id"],)).fetchone():
        return "id_taken"
    cols = {c[1] for c in db.execute("PRAGMA table_info(vehicles)")}
    _insert(db, "vehicles", {k: val for k, val in v.items() if k in cols})
    for b in p.get("batteries", []):
        if not db.execute("SELECT 1 FROM vehicle_batteries WHERE id = ?", (b["id"],)).fetchone():
            _insert(db, "vehicle_batteries", b)
    users = {u[0] for u in db.execute("SELECT id FROM users")}
    for a in p.get("access", []):
        if a["user_id"] in users:
            db.execute("INSERT OR IGNORE INTO user_vehicle_access (user_id, vehicle_id) VALUES (?, ?)",
                       (a["user_id"], a["vehicle_id"]))
    # Point users back only where the pointer still dangles or is empty.
    for col, key in (("active_vehicle_id", "active_users"), ("default_vehicle_id", "default_users")):
        for uid in p.get(key, []):
            db.execute(
                f"UPDATE users SET {col} = ? WHERE id = ? AND ({col} IS NULL OR {col} = ? "
                f"OR {col} NOT IN (SELECT id FROM vehicles))",
                (v["id"], uid, v["id"]))
    db.execute("DELETE FROM vehicle_trash WHERE id = ?", (trash_id,))
    return None
