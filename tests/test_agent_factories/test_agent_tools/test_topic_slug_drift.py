"""Topic-slug drift: a re-captured topic must come back under the SAME slug.

`_active_kps` supersedes on exact string equality, so `tsr_cdss_trajectory`
does not supersede `cdss_tsr_trajectory` — both sit in the digest as separate
cached topics and `kb_lookup` on the original returns the stale claim while the
fresh one hides under a name the specialist has no reason to guess. Observed 9x
on one topic across 40 turns in case 366132845011.

Two layers are pinned here: the deterministic snap (authoritative) and the
prompt block that lets the distiller get it right on its own.
"""
import asyncio
from types import SimpleNamespace

from agent_factories.agent_tools.distiller_pass import (
    _distill_and_persist,
    _snap_topic,
    _topic_key,
)
from tools.kb_tools import _active_kps


# ── the snap ──────────────────────────────────────────────────────────────

def test_permuted_slug_snaps_back_to_the_existing_one():
    assert _snap_topic("tsr_cdss_trajectory", ["cdss_tsr_trajectory"]) == "cdss_tsr_trajectory"
    # separators and case are not part of the identity
    assert _snap_topic("CDSS-TSR-Trajectory", ["cdss_tsr_trajectory"]) == "cdss_tsr_trajectory"


def test_snap_leaves_genuinely_different_topics_alone():
    # A wrong snap silently destroys a claim by superseding an unrelated topic,
    # so the rule is exact token-set equality — never stemming or substrings.
    assert _snap_topic("fico_trajectory", ["cdss_tsr_trajectory"]) == "fico_trajectory"
    assert _snap_topic("cdss_trajectory", ["cdss_tsr_trajectory"]) == "cdss_trajectory"
    assert _snap_topic("cdss_tsr_trend", ["cdss_tsr_trajectory"]) == "cdss_tsr_trend"
    # a superset of tokens is a different question, not a permutation
    assert _snap_topic("cdss_tsr_fico_trajectory", ["cdss_tsr_trajectory"]) \
        == "cdss_tsr_fico_trajectory"


def test_snap_is_a_noop_without_a_prior_or_a_usable_slug():
    assert _snap_topic("cdss_tsr_trajectory", []) == "cdss_tsr_trajectory"
    assert _snap_topic("cdss_tsr_trajectory", ["cdss_tsr_trajectory"]) == "cdss_tsr_trajectory"
    assert _snap_topic("", ["cdss_tsr_trajectory"]) == ""
    assert _snap_topic("___", ["cdss_tsr_trajectory"]) == "___"


def test_topic_key_ignores_order_and_separators():
    assert _topic_key("cdss_tsr_trajectory") == _topic_key("tsr-cdss-trajectory")
    assert _topic_key("a_b") != _topic_key("a_b_c")


# ── supersession actually fires afterwards ────────────────────────────────

def test_snapped_topic_supersedes_instead_of_forking_the_digest():
    kps = [
        {"topic": "cdss_tsr_trajectory", "claim": "old", "captured_at_seq": 1},
        {"topic": _snap_topic("tsr_cdss_trajectory", ["cdss_tsr_trajectory"]),
         "claim": "fresh", "captured_at_seq": 2},
    ]
    active = _active_kps(kps)
    assert len(active) == 1                  # one topic, not two
    assert active[0]["claim"] == "fresh"     # and the digest shows the CURRENT claim


# ── the narrow direct-insert path ─────────────────────────────────────────

def test_narrow_path_snaps_a_reworded_sub_question():
    # `_slug_topic` builds the slug from the sub-question's wording, so a
    # re-ask in a different order would otherwise fork the topic.
    kb = {"spend_payments": [{"topic": "total_spend_payment_status",
                              "claim": "old", "captured_at_seq": 1}]}
    ctx = SimpleNamespace(logger=None, _distiller=object(), _specialist_kb=kb,
                          _turn_id="t2", _turn_seq=2, _node_trace_store=None)
    narrow = SimpleNamespace(findings="Spend is $4,120; 2 payments missed.", evidence=[])

    n = asyncio.run(_distill_and_persist(
        ctx, "spend_payments", "what is the payment status and total spend", narrow))

    assert n == 1
    assert kb["spend_payments"][-1]["topic"] == "total_spend_payment_status"
    assert len(_active_kps(kb["spend_payments"])) == 1


# ── the distiller path, end to end ────────────────────────────────────────

def test_distiller_emitting_a_permuted_slug_is_snapped_and_logged(monkeypatch):
    """The real failure from case 366132845011: the distiller returned
    `tsr_cdss_trajectory` for a topic already stored as `cdss_tsr_trajectory`."""
    import agent_factories.agent_tools.distiller_pass as dp

    events: list = []

    class _Logger:
        def log(self, event, payload):
            events.append((event, payload))

    kb = {"modeling": [{"topic": "cdss_tsr_trajectory", "claim": "old",
                        "captured_at_seq": 1, "captured_at_turn": "t1"}]}
    ctx = SimpleNamespace(logger=_Logger(), _distiller=object(), _specialist_kb=kb,
                          _turn_id="t2", _turn_seq=2, _node_trace_store=None,
                          case_folder=None, _catalog=None)

    seen_input: list = []

    class _Runner:
        @staticmethod
        async def run(agent, prompt, **kwargs):
            seen_input.append(prompt)
            return SimpleNamespace(final_output=SimpleNamespace(knowledge_points=[
                {"topic": "tsr_cdss_trajectory", "claim": "fresh",
                 "numbers": [], "viz": None, "source_call": "", "confidence": "high"},
            ]))

    monkeypatch.setattr(dp, "Runner", _Runner)
    # Long + series-flavoured so it takes the distiller path, not the narrow one.
    wide = SimpleNamespace(
        findings="CDSS and TSR trend together across 2024-01 to 2024-12; "
                 "the monthly trend shows TSR falling from 0.81 to 0.62 while "
                 "CDSS drifts from 712 to 690 over the same window.",
        evidence=[])

    n = asyncio.run(_distill_and_persist(ctx, "modeling", "how did tsr move?", wide))

    assert n == 1
    # The prompt carried the existing slug...
    assert "cdss_tsr_trajectory" in seen_input[0]
    # ...and the snap fixed it anyway when the distiller ignored it.
    assert kb["modeling"][-1]["topic"] == "cdss_tsr_trajectory"
    assert len(_active_kps(kb["modeling"])) == 1
    assert _active_kps(kb["modeling"])[0]["claim"] == "fresh"
    # Visible in the JSONL so drift frequency stays auditable.
    assert any(e == "distiller_topic_snapped"
               and p.get("emitted") == "tsr_cdss_trajectory"
               and p.get("snapped_to") == "cdss_tsr_trajectory"
               for e, p in events)


# ── the prompt block ──────────────────────────────────────────────────────

def test_distiller_input_lists_existing_slugs_for_reuse():
    import inspect

    import agent_factories.agent_tools.distiller_pass as dp

    src = inspect.getsource(dp._distill_and_persist)
    assert "Topic slugs already in this specialist's KB" in src
    assert "reuse " in src and "EXACTLY" in src
    # Only the ACTIVE set — superseded entries share their slug with the
    # active one, so listing them would just repeat names.
    assert "_active_kps(kb.get(name, []))" in src
    # The prompt is bounded so a long case can't crowd out the payload...
    assert "existing_topics[-_MAX_EXISTING_TOPICS:]" in src


def test_snap_checks_every_topic_even_those_cut_from_the_prompt():
    """The prompt is truncated for token cost; the deterministic snap is not.
    An old topic must not be allowed to drift just because its slug fell off
    the end of the listing."""
    import agent_factories.agent_tools.distiller_pass as dp

    oldest = "cdss_tsr_trajectory"
    kb = {"modeling": [{"topic": oldest, "claim": "old", "captured_at_seq": 0}] + [
        {"topic": f"filler_topic_{i}", "claim": "x", "captured_at_seq": i + 1}
        for i in range(dp._MAX_EXISTING_TOPICS + 10)]}

    active = [kp["topic"] for kp in _active_kps(kb["modeling"])]
    assert oldest not in active[-dp._MAX_EXISTING_TOPICS:]   # off the prompt
    assert _snap_topic("tsr_cdss_trajectory", active) == oldest   # still snapped


def test_distillation_skill_states_the_reuse_rule():
    from pathlib import Path

    body = Path("skills/workflow/distillation.md").read_text()
    assert "Reuse an existing slug EXACTLY" in body
    assert "Same tokens in a different order" in body
