"""config/tuning.yaml -> os.environ, with inline-env-wins (setdefault) semantics."""
import os

import pytest

from config.tuning_loader import _MAP, apply_tuning

_KEYS = ["EPISODIC_TURNS", "AMEM_CONSOLIDATE_EVERY_N",
         "AMEM_ACTIVE_KP_THRESHOLD", "AMEM_ACTIVE_KP_KEEP",
         "TURN_WALL_CLOCK_S", "QUEUED_TURN_MAX_WAIT_S", "SCREEN_TIMEOUT_S",
         "SCREEN_STALL_RETRY_S", "SAFECHAIN_STALL_RETRY_S",
         "ORCH_PLAN_TIMEOUT_S", "REVIEWER_TIMEOUT_S",
         "SPECIALIST_TIMEOUT_S", "REPORT_AGENT_TIMEOUT_S",
         "DISTILLER_TIMEOUT_S", "DISTILLER_DRAIN_TIMEOUT_S",
         "SAFECHAIN_CALL_TIMEOUT_S", "AMEM_READ_TIMEOUT_S",
         "AMEM_WRITE_TIMEOUT_S", "AMEM_ACTIVE_LOAD_TIMEOUT_S"]
# Derived, so a knob added to the loader can never be left out of the fixture
# and leak between tests — which is how the first version of the tests below
# passed individually and failed as a suite.
_KEYS += [name for name in _MAP.values() if name not in _KEYS]


@pytest.fixture
def clean_env():
    saved = {k: os.environ.get(k) for k in _KEYS}
    for k in _KEYS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_apply_tuning_sets_env_from_yaml(tmp_path, clean_env):
    y = tmp_path / "t.yaml"
    y.write_text("memory:\n  episodic_turns: 4\n  active_kp_threshold: 7\n"
                 "  active_kp_keep: 3\n")
    applied = apply_tuning(str(y))
    assert os.environ["EPISODIC_TURNS"] == "4"
    assert os.environ["AMEM_ACTIVE_KP_THRESHOLD"] == "7"
    assert os.environ["AMEM_ACTIVE_KP_KEEP"] == "3"
    assert applied == {"EPISODIC_TURNS": "4", "AMEM_ACTIVE_KP_THRESHOLD": "7",
                       "AMEM_ACTIVE_KP_KEEP": "3"}


def test_inline_env_wins_over_yaml(tmp_path, clean_env):
    y = tmp_path / "t.yaml"
    y.write_text("memory:\n  episodic_turns: 4\n")
    os.environ["EPISODIC_TURNS"] = "99"           # as if set inline / in .env
    applied = apply_tuning(str(y))
    assert os.environ["EPISODIC_TURNS"] == "99"    # not overridden
    assert "EPISODIC_TURNS" not in applied


def test_missing_or_bad_file_is_noop(tmp_path, clean_env):
    assert apply_tuning(str(tmp_path / "nope.yaml")) == {}
    assert "EPISODIC_TURNS" not in os.environ


def test_apply_tuning_sets_the_timeout_env_vars(tmp_path, clean_env):
    y = tmp_path / "t.yaml"
    y.write_text("timeouts:\n  turn_wall_clock_s: 400\n  specialist_s: 60\n"
                 "  amem_read_s: 2.5\n")
    applied = apply_tuning(str(y))
    assert os.environ["TURN_WALL_CLOCK_S"] == "400"
    assert os.environ["SPECIALIST_TIMEOUT_S"] == "60"
    assert os.environ["AMEM_READ_TIMEOUT_S"] == "2.5"      # floats survive
    assert set(applied) == {"TURN_WALL_CLOCK_S", "SPECIALIST_TIMEOUT_S",
                            "AMEM_READ_TIMEOUT_S"}


def test_inline_env_wins_for_timeouts_too(tmp_path, clean_env):
    y = tmp_path / "t.yaml"
    y.write_text("timeouts:\n  specialist_s: 60\n")
    os.environ["SPECIALIST_TIMEOUT_S"] = "15"
    apply_tuning(str(y))
    assert os.environ["SPECIALIST_TIMEOUT_S"] == "15"


def test_shipped_tuning_yaml_declares_every_timeout_knob():
    """The shipped file is the discovery surface — a knob wired in the loader
    but absent from the YAML is invisible to whoever needs to change it."""
    import yaml as _yaml
    from pathlib import Path

    from config.tuning_loader import _MAP

    data = _yaml.safe_load(Path("config/tuning.yaml").read_text())
    declared = set(data.get("timeouts") or {})
    wired = {k.split(".", 1)[1] for k in _MAP if k.startswith("timeouts.")}
    assert wired - declared == set(), f"wired but undocumented: {wired - declared}"
    assert declared - wired == set(), f"in YAML but not wired: {declared - wired}"


def test_shipped_timeouts_nest_inside_the_turn_fence():
    """Every phase fence must be tighter than the outer turn fence, or the
    fence fires first and the phase's clearer error never reaches the user."""
    import yaml as _yaml
    from pathlib import Path

    t = _yaml.safe_load(Path("config/tuning.yaml").read_text())["timeouts"]
    outer = t["turn_wall_clock_s"]
    for phase in ("screen_s", "orch_plan_s", "specialist_s", "report_agent_s",
                  "distiller_s", "safechain_call_s"):
        assert t[phase] < outer, f"{phase}={t[phase]} does not fit in {outer}"


def test_active_kp_threshold_reads_env(monkeypatch):
    # The loader module reads AMEM_ACTIVE_KP_THRESHOLD at import; reload to prove
    # the wiring, then reload back so other modules' bound value is unaffected.
    import importlib
    import memory.loader as loader
    monkeypatch.setenv("AMEM_ACTIVE_KP_THRESHOLD", "3")
    importlib.reload(loader)
    assert loader.ACTIVE_KP_THRESHOLD == 3
    monkeypatch.delenv("AMEM_ACTIVE_KP_THRESHOLD", raising=False)
    importlib.reload(loader)
    assert loader.ACTIVE_KP_THRESHOLD == 100


def test_a_stalled_call_fits_inside_every_agent_that_must_survive_one():
    """The arithmetic that ties the call layer to the phase layer.

    One safechain call costs at most `safechain_stall_retry_s + safechain_call_s`
    (the short first attempt, then a fresh one with the full budget). An agent
    whose fence is BELOW that can never complete a stalled call — which is
    exactly how report_agent died at 100.01s twice in the private env.

    `distiller_s` is excluded on purpose: it is fire-and-forget and bounded by
    `distiller_drain_s`, so a stalled distiller is dropped by design.
    """
    import yaml as _yaml
    from pathlib import Path

    t = _yaml.safe_load(Path("config/tuning.yaml").read_text())["timeouts"]
    worst_call = t["safechain_stall_retry_s"] + t["safechain_call_s"]
    for agent in ("specialist_s", "report_agent_s"):
        assert t[agent] > worst_call, (
            f"{agent}={t[agent]} cannot hold one stalled call ({worst_call}s)")


def test_the_retry_budget_covers_a_healthy_call_with_wide_margin():
    """SUPERSEDES an earlier bound of >=135s, which required the retry to be
    able to RIDE OUT the 126-131s stall plateau. That was dropped deliberately
    on 2026-08-14 in favour of betting that a re-issued request ESCAPES the
    wedge — supported by concurrent divergence (`distiller.bureau` 5.8s while
    `distiller.modeling` hung 120s, same pool, same moment).

    Under that bet the budget only has to cover a healthy call: p99 is 7.5s in
    dev, 2.1-5.6s in prod. The bet is falsified by `safechain_retry_stalled` in
    the log; if that fires in prod, restore the >=135 bound."""
    import yaml as _yaml
    from pathlib import Path

    t = _yaml.safe_load(Path("config/tuning.yaml").read_text())["timeouts"]
    assert t["safechain_call_s"] >= 30, "no margin over a healthy call"


def test_the_stall_fence_clears_normal_call_latency_by_a_wide_margin():
    """p99 per-call latency is 7.5s in dev and 2.1-5.6s in prod. The fence must
    stay far above that or it starts abandoning healthy calls and paying twice."""
    import yaml as _yaml
    from pathlib import Path

    t = _yaml.safe_load(Path("config/tuning.yaml").read_text())["timeouts"]
    assert t["safechain_stall_retry_s"] >= 25
    assert t["safechain_stall_retry_s"] < t["safechain_call_s"]


# ── failing loudly ──────────────────────────────────────────────────────────
#
# From a private-env investigation: the knowledge base reported "not
# configured" while tuning.yaml looked right. Every way this loader can fail
# was silent, and the parse error is TOTAL — one bad line discards the file,
# timeouts and memory knobs included, with no output at all.

def test_a_parse_error_is_reported_and_names_the_file(tmp_path, clean_env, capsys):
    bad = tmp_path / "bad.yaml"
    # The natural, unquoted way to write a `module:callable` value — the ": "
    # makes it invalid YAML, and this is the edit someone makes on a server.
    bad.write_text("knowledge_base:\n"
                   "  client: /abs/path/kb.py: answer_question\n"
                   "memory:\n  episodic_turns: 7\n")

    assert apply_tuning(str(bad)) == {}
    err = capsys.readouterr().err
    assert "FAILED TO PARSE" in err
    assert str(bad) in err
    # The blast radius is the point: an unrelated knob is gone too.
    assert "EPISODIC_TURNS" not in os.environ


def test_a_missing_file_says_so(tmp_path, clean_env, capsys):
    assert apply_tuning(str(tmp_path / "absent.yaml")) == {}
    assert "not found" in capsys.readouterr().err


def test_a_quoted_client_value_parses(tmp_path, clean_env):
    good = tmp_path / "good.yaml"
    good.write_text('knowledge_base:\n  client: "/abs/path/kb.py:answer_question"\n')
    applied = apply_tuning(str(good))
    assert applied["KNOWLEDGE_BASE_CLIENT"] == "/abs/path/kb.py:answer_question"


def test_an_empty_value_is_skipped_not_written(tmp_path, clean_env):
    """Writing "" makes the variable present-but-empty: it reads as unset to
    consumers while blocking every later supplier. Leave it alone instead."""
    y = tmp_path / "empty.yaml"
    y.write_text('knowledge_base:\n  enabled: true\n  client: ""\n')

    applied = apply_tuning(str(y))
    assert "KNOWLEDGE_BASE_CLIENT" not in applied
    assert "KNOWLEDGE_BASE_CLIENT" not in os.environ

    # ...so a later supplier can still provide it.
    os.environ.setdefault("KNOWLEDGE_BASE_CLIENT", "pkg:answer_question")
    assert os.environ["KNOWLEDGE_BASE_CLIENT"] == "pkg:answer_question"


def test_values_are_stripped(tmp_path, clean_env):
    y = tmp_path / "pad.yaml"
    y.write_text('knowledge_base:\n  client: "  pkg:answer_question  "\n')
    assert apply_tuning(str(y))["KNOWLEDGE_BASE_CLIENT"] == "pkg:answer_question"


def test_whitespace_only_counts_as_empty(tmp_path, clean_env):
    y = tmp_path / "ws.yaml"
    y.write_text('knowledge_base:\n  json_path: "   "\n')
    assert apply_tuning(str(y)) == {}
