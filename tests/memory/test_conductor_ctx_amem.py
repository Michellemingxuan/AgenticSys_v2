"""The AppContext built per turn must carry the Amem handle + scope so the
distiller and finalize seams can write, and _assemble_input can read."""
import inspect
import runner.turn.conductor as conductor


def test_assemble_input_sets_amem_fields_source():
    src = inspect.getsource(conductor.TurnRunner._assemble_input)
    # The construction must thread the Amem handle, config, case id, session id.
    assert "_amem=" in src
    assert "_amem_cfg=" in src
    assert "_case_id=" in src
    assert "_session_id=" in src
