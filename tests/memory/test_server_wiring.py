import server


def test_server_exposes_amem_globals():
    assert hasattr(server, "_AMEM")
    assert hasattr(server, "_AMEM_CFG")
    # In dev with no Qdrant running, the health check fails → NullAmemManager.
    from memory.null_manager import NullAmemManager
    from Amem.core.manager import AmemManager
    assert isinstance(server._AMEM, (NullAmemManager, AmemManager))


def test_case_session_has_session_fields():
    from dataclasses import fields
    names = {f.name for f in fields(server.CaseSession)}
    assert "session_id" in names
    assert "current_turn_id" in names
