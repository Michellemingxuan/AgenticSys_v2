from agent_factories.orchestrator_agent import _render_team_roster

class _StubAgent:
    def __init__(self, name): self.name = name

class _StubCatalog:
    def get_description(self, t): return "Modeling features."
    def concepts_for_tables(self, tables): return ["oop", "exposure_leverage"]

def test_roster_lists_concepts_for_tagged_specialist():
    roster = _render_team_roster([_StubAgent("modeling")], catalog=_StubCatalog())
    assert "concepts you can direct:" in roster
    assert "oop" in roster
    assert "pass `concepts=" in roster  # footer instruction present
