"""Report layout of the yearly estimate, per vehicle: the grouping (flat
mapping purpose -> group purpose, e.g. two office purposes counted as
"business" in the printout for the employer) and the categories whose visited
destinations are listed. Stored per vehicle, sanitised, and kept in step when a
purpose is renamed, merged or deleted."""

from datetime import datetime

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (2, 'EV2', 'dev2')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    for name, color in (("Business", "#1f6feb"), ("Private", "#238636"), ("Mixed", "#8b949e"),
                        ("Office A", "#d29922"), ("Office B", "#a371f7")):
        db.execute("INSERT INTO purpose_meta (name, color) VALUES (?, ?)", (name, color))
    today = datetime.now().strftime("%Y-%m-%dT00:00:01")
    for purpose, km, dest in (("Business", 10.0, "Site 1"), ("Office A", 20.0, "HQ"),
                              ("Office B", 5.0, ""), ("Private", 7.0, None),
                              ("Mixed", 12.0, "Workshop"), ("Mixed", 8.0, " Workshop "),
                              ("Mixed", 3.0, None)):
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, purpose, destination) "
                   "VALUES ('dev1', ?, ?, ?, ?, ?)",
                   (today, today, km, purpose, dest))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _layout(client):
    d = client.get("/api/analysis/yearly-purposes").get_json()
    return d["groups"], d["dest_list"]


def _groups(client):
    return _layout(client)[0]


def test_yearly_returns_layout_colors_and_destinations(client):
    d = client.get("/api/analysis/yearly-purposes").get_json()
    assert d["groups"] == {} and d["dest_list"] == []
    assert d["colors"]["Office A"] == "#d29922"
    by = {p["purpose"]: p for p in d["purposes"]}
    assert set(by) == {"Business", "Office A", "Office B", "Private", "Mixed"}
    mixed = {x["name"]: (x["trips"], x["km"]) for x in by["Mixed"]["destinations"]}
    assert mixed == {"Workshop": (2, 20.0), "": (1, 3.0)}   # trimmed, empty = no destination
    assert by["Private"]["destinations"] == [{"name": "", "trips": 1, "km": 7.0}]


def test_save_roundtrip_is_per_vehicle(client, db):
    r = client.post("/api/analysis/purpose-groups",
                    json={"groups": {"Office A": "Business", "Office B": "Business"}})
    assert r.get_json()["groups"] == {"Office A": "Business", "Office B": "Business"}
    assert _groups(client) == {"Office A": "Business", "Office B": "Business"}
    db.execute("UPDATE users SET active_vehicle_id = 2 WHERE id = 1")
    db.commit()
    assert _groups(client) == {}


def test_dest_list_saved_without_touching_groups(client):
    client.post("/api/analysis/purpose-groups", json={"groups": {"Office A": "Business"}})
    r = client.post("/api/analysis/purpose-groups",
                    json={"dest_list": ["Mixed", " Mixed ", "", 3, "Private"]})
    assert r.get_json()["dest_list"] == ["Mixed", "Private"]
    assert _layout(client) == ({"Office A": "Business"}, ["Mixed", "Private"])


def test_save_sanitises(client):
    r = client.post("/api/analysis/purpose-groups", json={"groups": {
        "Office A": "Office B", "Office B": "Business",   # chain -> flattened
        "Private": "Private",                             # self mapping dropped
        "X": "Y", "Y": "X",                               # cycle dropped
        " ": "Business", "Bad": 3,                        # junk dropped
    }})
    assert r.get_json()["groups"] == {"Office A": "Business", "Office B": "Business"}


def test_rename_merge_delete_follow(client):
    client.post("/api/analysis/purpose-groups",
                json={"groups": {"Office A": "Business", "Office B": "Business"},
                      "dest_list": ["Business", "Mixed"]})
    # rename the group: members and its destination list follow to the new name
    client.post("/api/admin/rename", json={"field": "purpose", "old_name": "Business", "new_name": "Work"})
    assert _layout(client) == ({"Office A": "Work", "Office B": "Work"}, ["Work", "Mixed"])
    # rename a member: it keeps its group
    client.post("/api/admin/rename", json={"field": "purpose", "old_name": "Office A", "new_name": "Office North"})
    assert _groups(client) == {"Office North": "Work", "Office B": "Work"}
    # merge a member into an ungrouped purpose: the target keeps its own row
    client.post("/api/admin/merge", json={"field": "purpose", "source": "Office B", "target": "Private"})
    assert _groups(client) == {"Office North": "Work"}
    # merge a listed category into another: the target keeps its own setting
    client.post("/api/admin/merge", json={"field": "purpose", "source": "Mixed", "target": "Private"})
    assert _layout(client)[1] == ["Work"]
    # delete the group: its members become rows of their own, the list flag goes
    client.post("/api/admin/delete-value", json={"field": "purpose", "name": "Work", "confirm": "Work"})
    assert _layout(client) == ({}, [])


def test_print_opts_saved_and_returned(client):
    """The printout switches (hide destinations / projection / percent / bar)
    are stored per vehicle, only the known keys, only when true."""
    d = client.get("/api/analysis/yearly-purposes").get_json()
    assert d["print_opts"] == {}
    r = client.post("/api/analysis/purpose-groups",
                    json={"print_opts": {"hide_dest": True, "hide_estimate": "yes",
                                         "hide_pct": False, "bogus": True}})
    assert r.get_json()["print_opts"] == {"hide_dest": True}
    d = client.get("/api/analysis/yearly-purposes").get_json()
    assert d["print_opts"] == {"hide_dest": True}
    assert d["groups"] == {} and d["dest_list"] == []   # untouched
    r = client.post("/api/analysis/purpose-groups",
                    json={"print_opts": {"hide_estimate": True, "hide_bar": True}})
    assert r.get_json()["print_opts"] == {"hide_estimate": True, "hide_bar": True}
    r = client.post("/api/analysis/purpose-groups", json={"print_opts": "nonsense"})
    assert r.get_json()["print_opts"] == {}
