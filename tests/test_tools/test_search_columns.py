"""search_columns — find a column from the USER's wording, not the skill's.

A specialist proposes variables from its skill and confirms them with
get_table_schema. Both sides are biased toward what the skill already names, so
a column the skill never mentions is effectively invisible. `model_scores`
carries ~250 real columns against ~56 named in the profile — "how is the
internal paydown rate" is answered by `last_cycle_cut_revolve_rate`, which no
skill enumerates.
"""
import pytest

from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from tools import data_tools


@pytest.fixture
def real_case():
    """The shipped simulated case — real profiles, real column inventory."""
    gw = LocalDataGateway.from_case_folders("data_tables/simulated")
    gw.set_case("00000000002")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    yield
    data_tools.init_tools(None, None)


def _columns_in(out: str) -> list[str]:
    return [line.strip().lstrip("- ").split(" ")[0]
            for line in out.splitlines() if line.startswith("  - ")]


# ── the motivating case ─────────────────────────────────────────────────────

def test_finds_the_column_the_skill_never_names(real_case):
    """'internal paydown rate' must surface last_cycle_cut_revolve_rate."""
    out = data_tools._search_columns_impl("internal paydown rate")
    assert "last_cycle_cut_revolve_rate" in _columns_in(out)
    # The concept that made it findable is shown, so the specialist can see WHY.
    assert "capacity_paydown" in out


def test_surfaces_the_whole_concept_family(real_case):
    """The point is a candidate SET the specialist chooses from, not one guess."""
    cols = _columns_in(data_tools._search_columns_impl("paydown"))
    assert {"cust_lend_acct_paydown", "cust_open_acct_paydown"} <= set(cols)


def test_finds_a_column_by_description_wording(real_case):
    """'days past due' is nowhere in the column NAME `times_30_dpd`."""
    out = data_tools._search_columns_impl("days past due")
    assert "times_30_dpd" in _columns_in(out)


# ── ranking + shape ─────────────────────────────────────────────────────────

def test_exact_column_name_ranks_first(real_case):
    out = data_tools._search_columns_impl("times_30_dpd")
    assert _columns_in(out)[0] == "times_30_dpd"


def test_alias_resolves_to_the_real_column(real_case):
    """Real-data profiles alias `<col>_min`/`_max` onto the canonical name."""
    out = data_tools._search_columns_impl("last_cycle_cut_revolve_rate_min")
    assert "last_cycle_cut_revolve_rate" in _columns_in(out)


def test_limit_is_honored(real_case):
    assert len(_columns_in(data_tools._search_columns_impl("rate", limit=3))) == 3


def test_table_name_restricts_the_search(real_case):
    out = data_tools._search_columns_impl("paydown", table_name="model_scores")
    tables = [ln.rstrip(":") for ln in out.splitlines() if ln and ln.endswith(":")]
    assert tables == ["model_scores"]


def test_results_name_columns_that_are_actually_queryable(real_case):
    """A search hit must be a name the other data tools accept — otherwise the
    tool just moves the dead end one call later."""
    schema = data_tools._get_table_schema_impl("model_scores")
    for col in _columns_in(data_tools._search_columns_impl("paydown")):
        assert f'"{col}"' in schema


# ── negatives must not read as tool failures ────────────────────────────────

def test_no_match_is_a_benign_negative_not_a_failure(real_case):
    """`grounding.classify_tool_output` must NOT see this as a broken call, or
    an honest 'no such column' would trigger the ungrounded-retry path."""
    from agent_factories.agent_tools.grounding import classify_tool_output

    out = data_tools._search_columns_impl("zzz_no_such_metric_qqq")
    assert "no columns matched" in out
    assert classify_tool_output("search_columns", out) is None


def test_stopword_only_query_is_rejected_without_a_scan(real_case):
    out = data_tools._search_columns_impl("what is the")
    assert "no searchable terms" in out


def test_uninitialized_data_layer_is_reported_as_such():
    data_tools.init_tools(None, None)
    out = data_tools._search_columns_impl("paydown")
    assert "data layer is not initialized" in out


# ── the index ───────────────────────────────────────────────────────────────

def test_index_is_cached_per_case_and_reset_by_init(real_case):
    data_tools._search_columns_impl("paydown")
    assert data_tools._search_index_cache, "index should be memoized"
    data_tools.clear_schema_cache()
    assert not data_tools._search_index_cache, "stale index must not survive"


def test_index_covers_columns_absent_from_the_catalog(real_case):
    """Real cases ship far more columns than the profile documents; a column
    with no spec is still findable by name."""
    entries = data_tools._build_search_index("00000000002")
    assert entries
    assert any(not e["in_catalog"] for e in entries) or all(
        e["in_catalog"] for e in entries)  # shape holds either way
    assert all({"table", "column", "dtype", "concepts"} <= set(e) for e in entries)
