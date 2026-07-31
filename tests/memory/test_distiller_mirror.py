"""Guard: the mid-turn Amem `working`-level mirror was removed from the
distiller seam (Phase 3 — durable per-specialist memory design). KPs now
persist ONLY via the batched end-of-turn write (`write_specialist_memory`
from conductor._persist_to_amem); Amem's `working` level is no longer used
by this integration. See docs/superpowers/specs/
2026-07-26-durable-per-specialist-memory-design.md, section 2.4."""
import agent_factories.agent_tools.distiller_pass as dp


def test_distiller_pass_no_longer_defines_mirror_kp():
    assert not hasattr(dp, "_mirror_kp")


def test_distiller_pass_does_not_import_mirror_kp_working():
    import inspect
    src = inspect.getsource(dp)
    assert "mirror_kp_working" not in src
    assert "_mirror_kp(" not in src


def test_the_working_level_mirror_no_longer_exists_at_all():
    """It outlived its removal from the distiller seam: still defined and
    exported from `memory`, never called, so the store held zero
    `knowledge_point` records while the function implied otherwise. Deleted —
    bind that so it cannot drift back as a second, unused storage path."""
    import memory
    import memory.writer as writer

    assert not hasattr(writer, "mirror_kp_working")
    assert not hasattr(memory, "mirror_kp_working")
    assert "mirror_kp_working" not in getattr(memory, "__all__", ())
