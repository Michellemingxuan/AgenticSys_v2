from pathlib import Path
from models.app_context import AppContext


def test_app_context_has_amem_fields():
    ctx = AppContext(gateway=None, case_folder=Path("."), logger=None)
    assert ctx._amem is None
    assert ctx._amem_cfg is None
    assert ctx._case_id is None
    assert ctx._session_id is None
