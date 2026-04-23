# Current Architecture (snapshot 2026-04-23)

Snapshot of the system before the skills/parallel-paths refactor. Captures the per-question pipeline and the agent/tool boundaries as they exist today.

```mermaid
flowchart TD
    User([Reviewer question]) --> Main["main.py<br/>run_question(question, mode, pillar)"]

    Main --> Avail["list_domain_skills()<br/>= 7 domain modules"]
    Main --> Orch["Orchestrator.plan_team(question, mode)"]

    Orch -->|mode == report| All["All specialists,<br/>sub_q = root question verbatim"]
    Orch -->|mode == chat| Sel["_select_team()<br/>LLM call · sees full data catalog<br/>+ specialist roster + warmth"]
    Sel -->|N == 1| Single["Single specialist,<br/>sub_q = root"]
    Sel -->|N &gt; 1| Split["_split_sub_questions()<br/>LLM call · per-specialist sub-questions"]
    All --> Plan["TeamAssignment list"]
    Single --> Plan
    Split --> Plan

    Plan --> Loop["For each assignment (sequential):<br/>load_domain_skill +<br/>SessionRegistry.get_or_create"]
    Loop --> Spec["BaseSpecialist.run(sub_q, root_q, mode)"]
    Spec --> ToolUse["data_tools:<br/>list_available_tables<br/>get_table_schema<br/>query_table"]
    ToolUse --> GW["SimulatedDataGateway<br/>(per-case in-memory tables)"]
    Spec --> SpecOut["SpecialistOutput<br/>findings · evidence ·<br/>implications · data_gaps"]

    SpecOut --> Compare["GeneralSpecialist.compare()<br/>LLM call · pairwise comparison<br/>over selected specialists"]
    Compare --> Review["ReviewReport<br/>resolved · open_conflicts ·<br/>cross_domain_insights ·<br/>data_requests_made"]

    SpecOut --> Synth["Orchestrator.synthesize()<br/>LLM call · merges specialists +<br/>review into final answer"]
    Review --> Synth
    Synth --> Final["FinalOutput<br/>answer · data_gaps · blocked_steps ·<br/>specialists_consulted · sub_questions"]

    Final --> Format["ChatAgent.format_for_reviewer()"]
    Format --> Out([Markdown answer to reviewer])

    subgraph FW["Firewall stack (every LLM call routes through here)"]
        FWStack["FirewallStack.call(system_prompt, user_message)"]
        Adapter["OpenAIAdapter / SafeChainAdapter"]
        FWStack --> Adapter
    end

    Sel -.-> FWStack
    Split -.-> FWStack
    Spec -.-> FWStack
    Compare -.-> FWStack
    Synth -.-> FWStack

    classDef llm fill:#e0f2fe,stroke:#0369a1
    classDef tool fill:#dcfce7,stroke:#15803d
    classDef data fill:#fef3c7,stroke:#92400e
    classDef out fill:#f3e8ff,stroke:#6b21a8
    class Sel,Split,Spec,Compare,Synth llm
    class ToolUse tool
    class GW data
    class Final,Out out
```

## Notes

- **Synchronous throughout.** `firewall.call`, all agents, the per-assignment loop, and the per-pair comparison loop are blocking. No `asyncio`, no threads.
- **One mode flag drives planning.** `mode == "report"` short-circuits team selection (everyone gets the root question). `mode == "chat"` runs the two-step LLM planning (select → split).
- **Domain skills are Python.** `skills/domain/*.py` modules expose a `get_skill()` factory returning a `DomainSkill` dataclass (`system_prompt`, `data_hints`, `risk_signals`, `decision_focus`, `prompt_overlay`).
- **Tools live in `tools/data_tools.py`.** Specialists call `query_table` directly through normal Python — there is no LLM-driven tool-call protocol yet.
- **Session warmth is a tiebreaker.** `SessionRegistry` tracks which specialists have been instantiated; `_select_team` is told about warmth but instructed not to let it override data relevance.
- **No prior-report consultation.** Today nothing reads `results/<case-id>/`. The team workflow is the only answer source.

## What this diagram is for

Reference snapshot for the next-version design (`2026-04-23-orchestrator-skills-refactor-design.md`, forthcoming). Compare side-by-side to see what the parallel Reports-path + Balancing-skill change adds and what stays the same.
