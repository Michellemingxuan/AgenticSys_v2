"""Pin / opportunity persistence."""
import importlib

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PIN_DB", str(tmp_path / "pins.db"))
    import datalayer.pin_store as ps
    importlib.reload(ps)   # re-read PIN_DB at import time
    return ps


def test_pin_and_list_round_trip(store):
    store.add_pin("c1", kind="insight", text="Spend spiked 2.6x",
                  turn_id="t2", turn_index=2, source="spending & payment specialist")

    pins = store.list_pins("c1")

    assert len(pins) == 1
    assert pins[0]["text"] == "Spend spiked 2.6x"
    assert pins[0]["turn_index"] == 2
    assert pins[0]["section_key"] is None


def test_pins_are_scoped_to_their_case(store):
    store.add_pin("c1", kind="insight", text="a")
    store.add_pin("c2", kind="insight", text="b")

    assert [p["text"] for p in store.list_pins("c1")] == ["a"]
    assert [p["text"] for p in store.list_pins("c2")] == ["b"]


def test_pinning_the_same_figure_twice_is_idempotent(store):
    """"Pin Figures" pins every figure on the turn at once, so a second
    click must not double the cards."""
    first = store.add_pin("c1", kind="figure", turn_id="t3",
                          specialist="modeling", topic="tsr", chart_url="/c/tsr.png")
    again = store.add_pin("c1", kind="figure", turn_id="t3",
                          specialist="modeling", topic="tsr", chart_url="/c/tsr.png")

    assert first["pin_id"] == again["pin_id"]
    assert len(store.list_pins("c1")) == 1


def test_two_insights_from_one_turn_are_two_pins(store):
    """Unlike figures, insights have no natural key — two different
    sentences from the same turn are two real pins."""
    store.add_pin("c1", kind="insight", text="first", turn_id="t2")
    store.add_pin("c1", kind="insight", text="second", turn_id="t2")

    assert len(store.list_pins("c1")) == 2


def test_same_topic_on_a_different_turn_is_a_separate_pin(store):
    store.add_pin("c1", kind="figure", turn_id="t3", specialist="m", topic="tsr")
    store.add_pin("c1", kind="figure", turn_id="t4", specialist="m", topic="tsr")

    assert len(store.list_pins("c1")) == 2


def test_unknown_kind_is_rejected(store):
    with pytest.raises(ValueError, match="unknown pin kind"):
        store.add_pin("c1", kind="notion", text="x")


def test_delete_removes_only_the_named_pin(store):
    keep = store.add_pin("c1", kind="insight", text="keep")
    drop = store.add_pin("c1", kind="insight", text="drop")

    assert store.delete_pin("c1", drop["pin_id"]) is True
    assert [p["pin_id"] for p in store.list_pins("c1")] == [keep["pin_id"]]


def test_delete_will_not_reach_across_cases(store):
    pin = store.add_pin("c1", kind="insight", text="x")

    assert store.delete_pin("c2", pin["pin_id"]) is False
    assert len(store.list_pins("c1")) == 1


def test_insert_into_section_and_group(store):
    a = store.add_pin("c1", kind="figure", turn_id="t3", specialist="m", topic="tsr")
    store.add_pin("c1", kind="figure", turn_id="t3", specialist="m", topic="bureau")

    store.set_pin_section("c1", a["pin_id"], "modeling")
    grouped = store.pins_by_section("c1")

    assert list(grouped) == ["modeling"]
    assert grouped["modeling"][0]["pin_id"] == a["pin_id"]


def test_removing_from_a_section_ungroups_it(store):
    pin = store.add_pin("c1", kind="figure", turn_id="t3", specialist="m", topic="tsr")
    store.set_pin_section("c1", pin["pin_id"], "modeling")

    store.set_pin_section("c1", pin["pin_id"], None)

    assert store.pins_by_section("c1") == {}


def test_opportunity_round_trip_keeps_its_pin_provenance(store):
    p1 = store.add_pin("c1", kind="insight", text="one")
    p2 = store.add_pin("c1", kind="insight", text="two")

    opp = store.add_opportunity("c1", title="Review RLI decline handling",
                                body="check the sequencing",
                                pin_ids=[p1["pin_id"], p2["pin_id"]])

    listed = store.list_opportunities("c1")
    assert len(listed) == 1
    assert listed[0]["title"] == "Review RLI decline handling"
    assert listed[0]["pin_ids"] == [p1["pin_id"], p2["pin_id"]]
    assert opp["opp_id"] == listed[0]["opp_id"]


def test_delete_opportunity(store):
    opp = store.add_opportunity("c1", title="x")
    assert store.delete_opportunity("c1", opp["opp_id"]) is True
    assert store.list_opportunities("c1") == []


def test_vega_spec_round_trips_as_json(store):
    """The spec is the durable copy of a figure — it carries its data inline,
    so it outlives the chart PNG that rewind deletes."""
    spec = {"mark": "line", "data": {"values": [{"x": 1, "y": 2}]}}
    store.add_pin("c1", kind="figure", turn_id="t1", specialist="m",
                  topic="tsr", vega_spec=spec)

    pin = store.list_pins("c1")[0]

    assert pin["vega_spec"] == spec


def test_pin_without_a_spec_reports_none(store):
    store.add_pin("c1", kind="figure", turn_id="t1", specialist="m", topic="tsr")
    assert store.list_pins("c1")[0]["vega_spec"] is None


def test_vega_spec_column_is_added_to_an_existing_database(tmp_path, monkeypatch):
    """`CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a
    database created before `vega_spec` existed must be migrated, not just
    re-declared."""
    import importlib
    import sqlite3

    db = tmp_path / "legacy.db"
    # A pins table as it looked before the column existed.
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE pins (pin_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,"
        " kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', turn_id TEXT,"
        " turn_index INTEGER, source TEXT NOT NULL DEFAULT '', specialist TEXT,"
        " topic TEXT, chart_url TEXT, section_key TEXT, created_at REAL NOT NULL);"
        "INSERT INTO pins VALUES ('old1','c1','insight','legacy',NULL,NULL,'',"
        "NULL,NULL,NULL,NULL,0.0);"
    )
    con.commit()
    con.close()

    monkeypatch.setenv("PIN_DB", str(db))
    import datalayer.pin_store as ps
    importlib.reload(ps)

    pins = ps.list_pins("c1")

    assert [p["text"] for p in pins] == ["legacy"]
    assert pins[0]["vega_spec"] is None
    # And new writes can use the column.
    ps.add_pin("c1", kind="figure", turn_id="t", specialist="m", topic="x",
               vega_spec={"mark": "bar"})
    assert any(p["vega_spec"] == {"mark": "bar"} for p in ps.list_pins("c1"))


def test_connections_are_closed_not_just_committed(store):
    """`with sqlite3.connect(...)` commits and does NOT close, so every call
    leaked a file descriptor. Measured before the fix: 300 add+list cycles
    leaked 67. On a server with the usual 1024 limit that ends as
    `unable to open database file` — pins that suddenly cannot be created or
    deleted, after working for a while.
    """
    import os

    if not os.path.isdir("/dev/fd"):          # not available on every platform
        import pytest
        pytest.skip("/dev/fd not available")

    def fds() -> int:
        return len(os.listdir("/dev/fd"))

    for _ in range(40):                        # warm up, settle imports
        store.add_pin("c1", kind="insight", text="warm")
    baseline = fds()
    for i in range(120):
        store.add_pin("c1", kind="insight", text=f"pin {i}")
        store.list_pins("c1")

    # A couple of descriptors of noise is fine; unbounded growth is not.
    assert fds() - baseline <= 5, f"leaked {fds() - baseline} descriptors"


def test_values_read_after_the_connection_closes(store):
    """Every helper that returns a rowcount must read it INSIDE the block —
    the connection is gone by the time the caller sees the value."""
    pin = store.add_pin("c1", kind="figure", turn_id="t1", specialist="s",
                        topic="tp")
    opp = store.add_opportunity("c1", title="t")

    assert store.set_pin_section("c1", pin["pin_id"], "bureau") is True
    assert store.delete_pin("c1", pin["pin_id"]) is True
    assert store.delete_pin("c1", "no-such-pin") is False
    assert store.delete_opportunity("c1", opp["opp_id"]) is True
    assert store.delete_opportunity("c1", "no-such-opp") is False


def test_a_pin_whose_turn_was_rewound_can_still_be_deleted(store):
    """Pins deliberately survive a rewind — they are review deliverables, not
    turn state — so an orphan must remain removable by hand. Nothing in the
    delete path consults the turn."""
    pin = store.add_pin("c1", kind="figure", turn_id="gone-turn",
                        specialist="s", topic="orphan")

    assert store.delete_pin("c1", pin["pin_id"]) is True
    assert store.list_pins("c1") == []


# ── retraction ───────────────────────────────────────────────────────────

def test_rewinding_a_turn_retracts_its_pins_rather_than_deleting_them(store):
    """Pinning is a deliberate "this matters"; rewind is one click on a turn
    card. Deleting here would destroy filed work as a side effect of a casual
    action, and the usual reason to rewind is to re-ask a question better."""
    keep = store.add_pin("c1", kind="insight", text="from another turn",
                         turn_id="t1")
    gone = store.add_pin("c1", kind="figure", turn_id="t2", specialist="s",
                         topic="tsr")

    assert store.retract_turns("c1", ["t2"]) == 1

    by_id = {p["pin_id"]: p for p in store.list_pins("c1")}
    assert by_id[gone["pin_id"]]["retracted"] is True
    assert by_id[keep["pin_id"]]["retracted"] is False
    assert len(by_id) == 2, "nothing should have been deleted"


def test_retraction_lifts_a_pin_out_of_its_report_section(store):
    """The one destructive part, and the right one: a retracted figure left
    sitting in a report section is the compliance problem."""
    pin = store.add_pin("c1", kind="figure", turn_id="t2", specialist="s",
                        topic="tsr")
    store.set_pin_section("c1", pin["pin_id"], "modeling")
    assert store.pins_by_section("c1")["modeling"]

    store.retract_turns("c1", ["t2"])

    assert store.pins_by_section("c1") == {}
    assert store.list_pins("c1")[0]["section_key"] is None


def test_a_retracted_pin_never_reappears_in_a_report_section(store):
    """Belt and braces: even if `section_key` survived somehow, a retracted
    pin must not be merged into the report."""
    pin = store.add_pin("c1", kind="figure", turn_id="t2", specialist="s",
                        topic="tsr")
    store.set_pin_section("c1", pin["pin_id"], "bureau")
    store.retract_turns("c1", ["t2"])
    store.set_pin_section("c1", pin["pin_id"], "bureau")   # re-inserted

    assert store.pins_by_section("c1") == {}


def test_retraction_is_idempotent(store):
    store.add_pin("c1", kind="insight", text="x", turn_id="t2")
    assert store.retract_turns("c1", ["t2"]) == 1
    assert store.retract_turns("c1", ["t2"]) == 0


def test_retraction_ignores_empty_and_unknown_turn_ids(store):
    store.add_pin("c1", kind="insight", text="x", turn_id="t1")
    assert store.retract_turns("c1", []) == 0
    assert store.retract_turns("c1", None) == 0
    assert store.retract_turns("c1", ["no-such-turn"]) == 0
    assert store.list_pins("c1")[0]["retracted"] is False


def test_retraction_does_not_reach_across_cases(store):
    store.add_pin("c1", kind="insight", text="mine", turn_id="t2")
    store.add_pin("c2", kind="insight", text="theirs", turn_id="t2")

    store.retract_turns("c1", ["t2"])

    assert store.list_pins("c2")[0]["retracted"] is False


def test_delete_retracted_removes_only_retracted_pins(store):
    keep = store.add_pin("c1", kind="insight", text="live", turn_id="t1")
    store.add_pin("c1", kind="insight", text="dead", turn_id="t2")
    store.retract_turns("c1", ["t2"])

    assert store.delete_retracted("c1") == 1
    assert [p["pin_id"] for p in store.list_pins("c1")] == [keep["pin_id"]]


def test_full_clear_deletes_every_pin(store):
    """"Clear this case" is an explicit reset; finding the pins still there
    afterwards would be the surprise."""
    store.add_pin("c1", kind="insight", text="a", turn_id="t1")
    store.add_pin("c1", kind="figure", turn_id="t2", specialist="s", topic="x")
    store.add_pin("c2", kind="insight", text="other case", turn_id="t1")

    assert store.delete_all_pins("c1") == 2
    assert store.list_pins("c1") == []
    assert len(store.list_pins("c2")) == 1


def test_question_is_stored_as_stable_provenance(store):
    """`turn_index` is positional and renumbers on rewind, so a pin captured
    as "Turn 3" starts pointing at a different turn. The question does not."""
    store.add_pin("c1", kind="figure", turn_id="t1", turn_index=3,
                  specialist="s", topic="tsr",
                  question="how did TSR and CDSS react?")

    assert store.list_pins("c1")[0]["question"] == "how did TSR and CDSS react?"


def test_columns_are_added_to_a_database_created_before_them(tmp_path, monkeypatch):
    import importlib
    import sqlite3

    db = tmp_path / "legacy2.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE pins (pin_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,"
        " kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', turn_id TEXT,"
        " turn_index INTEGER, source TEXT NOT NULL DEFAULT '', specialist TEXT,"
        " topic TEXT, chart_url TEXT, section_key TEXT, created_at REAL NOT NULL);"
        "INSERT INTO pins VALUES ('old','c1','insight','legacy',NULL,NULL,'',"
        "NULL,NULL,NULL,NULL,0.0);"
    )
    con.commit(); con.close()

    monkeypatch.setenv("PIN_DB", str(db))
    import datalayer.pin_store as ps
    importlib.reload(ps)

    pin = ps.list_pins("c1")[0]
    assert pin["question"] is None
    assert pin["retracted"] is False       # NOT NULL DEFAULT 0 backfills
