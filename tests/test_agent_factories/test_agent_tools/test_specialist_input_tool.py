import types

from agent_factories.agent_tools.specialist_input_tool import assemble_specialist_input


def test_cold_specialist_input_is_bare_question():
    ctx = types.SimpleNamespace(_specialist_kps={}, _episodic_records=[])
    out, n = assemble_specialist_input(
        ctx, "modeling", "which scores breached?", concepts=None,
        catalog=None, data_hints=["model_scores"], logger=None)
    assert out == "which scores breached?"
    assert n == 0
