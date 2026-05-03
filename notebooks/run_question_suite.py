"""Run the test-question suite against the SDK pipeline.

Mirrors the wiring in notebooks/test_chat_mode.ipynb (sections §0–§8 + §14)
and steps through every entry in `questions.json`. For each case it captures:

  1. ChatAgent.screen   — passed / reason / redacted question
  2. ChatAgent.relevance_check — in-scope verdict
  3. Runner.run on the orchestrator agent — the team-construction tool calls
     the orchestrator emitted, the report_agent's ReportDraft, every
     specialist's SpecialistOutput (including the inner data-tool calls
     captured in the EventLogger session log), and the orchestrator's final
     synthesized FinalAnswer.
  4. Follow-up turns — chained via `result.to_input_list()` so specialist
     memory and orchestrator history persist across the chain.

Outputs:

  - `logs/question-suite.jsonl`         — full event stream from EventLogger
                                          (engineer-facing detail).
  - `logs/question_suite/<name>.json`   — structured per-case file with
                                          turns: [ {screen, relevance,
                                          orchestrator_flow, final_answer} ]
                                          (engineer-facing).
  - `logs/question_suite/<name>.md`     — human-readable narrative report
                                          (non-engineer-facing). One section
                                          per turn: question check → team
                                          consulted → what each expert said
                                          → final answer + flags.

Run:

    python notebooks/run_question_suite.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import nest_asyncio

nest_asyncio.apply()

# Pin CWD + sys.path so all relative paths (config/, data_tables/, reports/,
# logs/) resolve against the project root regardless of where this is run from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env before importing anything that needs OPENAI_API_KEY.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agents import Runner
from agents.exceptions import AgentsException
from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem

from agent_factories.app_context import AppContext
from agent_factories.chat_agent import ChatAgent
from agent_factories.data_manager_agent import DataManagerAgent
from agent_factories.helper_tools import build_helper_tools
from config.pillar_loader import PillarLoader
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from llm.factory import FirewalledChatShim, build_session_clients
from llm.firewall_stack import FirewallStack, redact_payload
from logger.event_logger import EventLogger
from orchestrator.orchestrator import Orchestrator
from tools.data_tools import init_tools


SUITE_PATH = Path(__file__).with_name("questions_1.json")
PER_CASE_DIR = PROJECT_ROOT / "logs" / "question_suite"
PER_CASE_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────

def _safe_dump(obj):
    """Coerce a Pydantic / dict / list / arbitrary object into JSON-safe form."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _safe_dump(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_dump(v) for v in obj]
    return obj


def _serialize_orchestrator_flow(items):
    """Walk RunResult.new_items and emit a tight per-step record.

    Captures three step kinds the user cares about:
      - tool_call       — the orchestrator dispatched a sub-agent (specialist /
                          report_agent / general_specialist) with a sub_question.
      - tool_output     — the sub-agent returned a structured payload
                          (SpecialistOutput | ReportDraft | ReviewReport),
                          paired to its tool_call by call_id.
      - message         — orchestrator-emitted text (rare; the FinalAnswer is
                          the final structured output, not a message).
    """
    rows = []
    # Index tool-call names by call_id so tool_outputs can name their caller.
    call_name_by_id: dict[str, str] = {}
    for item in items:
        raw = getattr(item, "raw_item", None)
        if isinstance(item, ToolCallItem):
            name = (
                getattr(raw, "name", None)
                or (raw.get("name") if isinstance(raw, dict) else None)
                or "?"
            )
            call_id = (
                getattr(raw, "call_id", None)
                or (raw.get("call_id") if isinstance(raw, dict) else None)
            )
            args_str = (
                getattr(raw, "arguments", None)
                or (raw.get("arguments") if isinstance(raw, dict) else "{}")
            )
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {"raw": args_str}
            sub_q = args.get("sub_question") or args.get("input") or args
            if call_id:
                call_name_by_id[call_id] = name
            rows.append({
                "kind": "tool_call",
                "tool": name,
                "sub_question": sub_q,
                "call_id": call_id,
            })
        elif isinstance(item, ToolCallOutputItem):
            call_id = (raw.get("call_id") if isinstance(raw, dict) else None)
            rows.append({
                "kind": "tool_output",
                "tool": call_name_by_id.get(call_id, "?"),
                "call_id": call_id,
                "output": _safe_dump(item.output),
            })
        elif isinstance(item, MessageOutputItem):
            text = ""
            if isinstance(raw, dict):
                content = raw.get("content", [])
                if isinstance(content, list):
                    text = "".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
            rows.append({"kind": "message", "text": text})
    return rows


# ── Human-readable markdown rendering ──────────────────────────────────────
#
# The JSON log captures everything; the markdown is the version a reviewer /
# stakeholder can actually read. It walks each turn and tells the story:
#   what was asked → was it allowed in → which experts were consulted with
#   what sub-question → what each expert said (in their own prose, not raw
#   JSON) → what the final answer is.

_OUTCOME_LABELS = {
    "ok": "Completed",
    "screen_rejected": "Rejected at the safety / scope screen",
    "out_of_scope": "Allowed in but ruled out of scope",
    "orchestrator_error": "Pipeline error — see error field",
}


def _excerpt(text, max_len=400):
    """Trim a long string for display, marking the cut."""
    if not text:
        return ""
    s = str(text).strip()
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip() + " …"


def _render_specialist_output(payload):
    """SpecialistOutput → markdown bullets. Skips empty fields."""
    lines = []
    findings = payload.get("findings") or ""
    evidence = payload.get("evidence") or []
    implications = payload.get("implications") or []
    data_gaps = payload.get("data_gaps") or []
    raw_data = payload.get("raw_data") or {}

    if findings:
        lines.append(f"  - **Findings:** {_excerpt(findings, 600)}")
    if evidence:
        lines.append("  - **Evidence:**")
        for e in evidence[:5]:
            lines.append(f"      - {_excerpt(e, 300)}")
        if len(evidence) > 5:
            lines.append(f"      - … and {len(evidence) - 5} more")
    if implications:
        lines.append("  - **Implications:**")
        for imp in implications[:3]:
            lines.append(f"      - {_excerpt(imp, 300)}")
    if data_gaps:
        lines.append("  - **Data gaps:**")
        for g in data_gaps[:3]:
            lines.append(f"      - {_excerpt(g, 300)}")
    if raw_data:
        # Just hint that raw data was attached, don't dump it.
        keys = list(raw_data.keys())[:4]
        lines.append(
            f"  - **Raw data attached:** keys = {keys}"
            + ("" if len(raw_data) <= 4 else f" + {len(raw_data) - 4} more")
        )
    return "\n".join(lines) if lines else "  - (no structured fields returned)"


def _render_report_draft(payload):
    """ReportDraft → markdown bullets."""
    lines = []
    coverage = payload.get("coverage", "?")
    answer = payload.get("answer") or ""
    excerpts = payload.get("evidence_excerpts") or []
    files = payload.get("files_consulted") or []

    lines.append(f"  - **Report coverage:** {coverage}")
    if files:
        lines.append(f"  - **Files consulted:** {', '.join(files)}")
    if answer:
        lines.append(f"  - **Report's answer:** {_excerpt(answer, 600)}")
    if excerpts:
        lines.append("  - **Quoted from the reports:**")
        for ex in excerpts[:5]:
            lines.append(f"      - {_excerpt(ex, 250)}")
        if len(excerpts) > 5:
            lines.append(f"      - … and {len(excerpts) - 5} more")
    return "\n".join(lines)


def _render_review_report(payload):
    """ReviewReport (general_specialist) → markdown bullets."""
    lines = []
    resolved = payload.get("resolved") or []
    open_conflicts = payload.get("open_conflicts") or []
    insights = payload.get("cross_domain_insights") or []
    if resolved:
        lines.append(f"  - **Resolved contradictions:** {len(resolved)}")
        for r in resolved[:3]:
            lines.append(f"      - {_excerpt(r.get('contradiction', ''), 250)}")
    if open_conflicts:
        lines.append(f"  - **Open conflicts:** {len(open_conflicts)}")
        for c in open_conflicts[:3]:
            lines.append(f"      - {_excerpt(c.get('contradiction', ''), 250)}")
    if insights:
        lines.append("  - **Cross-domain insights:**")
        for ins in insights[:3]:
            lines.append(f"      - {_excerpt(ins, 250)}")
    return "\n".join(lines) if lines else "  - (no contradictions / cross-domain insights)"


def _render_final_answer(final):
    """FinalAnswer payload → readable answer block."""
    if not isinstance(final, dict):
        return str(final)
    lines = []
    answer = final.get("answer") or ""
    flags = final.get("flags") or []
    pull = final.get("data_pull_request")
    lines.append(f"**Answer:** {_excerpt(answer, 1200)}")
    if flags:
        lines.append("")
        lines.append("**Flags / caveats:**")
        for f in flags:
            lines.append(f"  - {_excerpt(f, 300)}")
    if pull and pull.get("needed"):
        lines.append("")
        lines.append("**Suggested data pull:**")
        lines.append(f"  - reason: {pull.get('reason', '')}")
        if pull.get("would_pull"):
            lines.append(f"  - would pull: {', '.join(pull.get('would_pull', []))}")
        lines.append(f"  - severity: {pull.get('severity', '')}")
    return "\n".join(lines)


def _render_turn_md(turn, idx):
    """One turn → markdown section, with [STAGE] tags so the flow is scannable."""
    out = []
    out.append(f"## Turn {idx + 1} — \"{turn['question']}\"")
    out.append("")

    # ── [QUESTION CHECK] ────────────────────────────────────────────────
    out.append("### [QUESTION CHECK]")
    out.append("")
    sc = turn.get("screen", {})
    out.append("- **Safety / PII screen:** "
               + ("passed" if sc.get("passed") else f"rejected — {sc.get('reason', '')}"))
    if sc.get("redacted_question") and sc.get("redacted_question") != turn["question"]:
        out.append(f"  - Redacted to: `{sc.get('redacted_question')}`")
    rc = turn.get("relevance_check")
    if rc is not None:
        out.append("- **Scope check:** "
                   + ("in scope" if rc.get("in_scope") else f"out of scope — {rc.get('reason', '')}"))
    outcome = turn.get("outcome", "?")
    out.append(f"- **Outcome:** {_OUTCOME_LABELS.get(outcome, outcome)}")
    if turn.get("error"):
        out.append(f"  - Error: `{turn['error']}`")
    out.append("")

    # If we never reached the orchestrator, stop here.
    if outcome != "ok":
        return "\n".join(out)

    flow = turn.get("orchestrator_flow", [])
    calls = [r for r in flow if r["kind"] == "tool_call"]
    outputs_by_call = {r.get("call_id"): r for r in flow if r["kind"] == "tool_output"}

    # ── [TEAM CONSTRUCTION] ─────────────────────────────────────────────
    if calls:
        out.append("### [TEAM CONSTRUCTION]")
        out.append("")
        out.append(f"Orchestrator selected {len(calls)} tool call(s) on this turn:")
        out.append("")
        for c in calls:
            sub_q = c.get("sub_question")
            sub_q_text = (
                json.dumps(sub_q, default=str) if not isinstance(sub_q, str) else sub_q
            )
            role = (
                "report agent"
                if c["tool"] == "report_agent"
                else "cross-specialist reviewer"
                if c["tool"] == "general_specialist"
                else f"{c['tool']} specialist"
            )
            out.append(f"- **{c['tool']}** ({role})")
            out.append(f"  - sub-question: {_excerpt(sub_q_text, 240)}")
        out.append("")

    # ── [REPORT AGENT ANALYSIS] / [SPECIALIST ANALYSIS] / [GENERAL SPECIALIST REVIEW] ──
    # Per-expert findings, in the order the orchestrator received them.
    for c in calls:
        result_row = outputs_by_call.get(c.get("call_id"))
        if result_row is None:
            continue
        tool = c["tool"]
        payload = result_row.get("output")
        if not isinstance(payload, dict):
            payload = {}
        if tool == "report_agent":
            out.append(f"### [REPORT AGENT ANALYSIS]")
            out.append("")
            out.append(_render_report_draft(payload))
        elif tool == "general_specialist":
            out.append(f"### [GENERAL SPECIALIST REVIEW]")
            out.append("")
            out.append(_render_review_report(payload))
        else:
            out.append(f"### [SPECIALIST ANALYSIS — {tool}]")
            out.append("")
            out.append(_render_specialist_output(payload))
        out.append("")

    # ── [FINAL SYNTHESIS] ───────────────────────────────────────────────
    final = turn.get("final_answer")
    if final:
        out.append("### [FINAL SYNTHESIS]")
        out.append("")
        out.append(_render_final_answer(final))
        out.append("")

    return "\n".join(out)


def render_case_md(case_log):
    """Whole case → markdown document."""
    lines = []
    lines.append(f"# {case_log['name']}")
    lines.append("")
    if case_log.get("note"):
        lines.append(f"_Note: {case_log['note']}_")
        lines.append("")
    lines.append(f"**Seed question:** \"{case_log['question']}\"")
    n_turns = len(case_log.get("turns", []))
    lines.append(f"**Turns:** {n_turns}  ")
    lines.append("")
    lines.append("---")
    lines.append("")
    for i, turn in enumerate(case_log.get("turns", [])):
        lines.append(_render_turn_md(turn, i))
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# ── Per-question runner ────────────────────────────────────────────────────

async def run_one_turn(
    turn_id,
    question,
    *,
    chat_agent,
    orch,
    ctx,
    prior_result=None,
):
    """Screen, relevance-check, run the orchestrator, capture flow."""
    log = {"turn_id": turn_id, "question": question, "is_followup": prior_result is not None}

    # Stage A — screen
    verdict = await chat_agent.screen(question)
    log["screen"] = {
        "passed": verdict.passed,
        "reason": verdict.reason,
        "redacted_question": verdict.redacted_question,
    }
    if not verdict.passed:
        log["outcome"] = "screen_rejected"
        return None, log

    # Stage B — relevance check
    in_scope, reason = await chat_agent.relevance_check(verdict.redacted_question)
    log["relevance_check"] = {"in_scope": in_scope, "reason": reason}
    if not in_scope:
        log["outcome"] = "out_of_scope"
        return None, log

    # Stage C — orchestrator run (multi-turn aware)
    if prior_result is not None:
        run_input = prior_result.to_input_list() + [
            {"role": "user", "content": verdict.redacted_question}
        ]
    else:
        run_input = verdict.redacted_question

    try:
        result = await Runner.run(orch.orchestrator_agent, run_input, context=ctx)
    except AgentsException as e:
        log["outcome"] = "orchestrator_error"
        log["error"] = f"{type(e).__name__}: {e}"
        return None, log

    # Stage D — capture flow + final answer (redacted)
    log["orchestrator_flow"] = _serialize_orchestrator_flow(result.new_items)
    final = redact_payload(result.final_output)
    log["final_answer"] = _safe_dump(final)
    log["outcome"] = "ok"
    return result, log


# ── Top-level orchestration ────────────────────────────────────────────────

async def main():
    suite = json.loads(SUITE_PATH.read_text())

    case_id = suite["case_id"]
    pillar_name = suite["pillar"]
    model_name = suite["model"]
    backend = suite["backend"]
    session_id = suite.get("session_id", "question-suite")

    print(f"PROJECT_ROOT: {PROJECT_ROOT}")
    print(f"OPENAI_API_KEY set: {bool(os.environ.get('OPENAI_API_KEY'))}")
    print(f"case_id={case_id}  pillar={pillar_name}  model={model_name}  backend={backend}")

    # Build pipeline (mirrors notebook §2-§4)
    logger = EventLogger(session_id=session_id, log_dir=str(PROJECT_ROOT / "logs"))
    firewall = FirewallStack(logger=logger, max_retries=2, concurrency_cap=8)
    clients = build_session_clients(firewall, model_name=model_name, backend=backend)
    chat_llm = FirewalledChatShim(clients)

    gw = LocalDataGateway.from_case_folders(str(PROJECT_ROOT / "data_tables" / "real"))
    catalog = DataCatalog(profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"))
    assert catalog.list_tables(), "DataCatalog loaded 0 profiles — check path."
    init_tools(gw, catalog, logger=logger)
    gw.set_case(case_id)

    # Sync the catalog with this case's real data BEFORE building agents
    data_mgr = DataManagerAgent(gateway=gw, catalog=catalog, llm=None, logger=logger)
    diff = data_mgr.sync_catalog(case_id)
    print(
        f"sync_catalog: auto={len(diff.auto_aliased)} drift={len(diff.value_drift)} "
        f"ambig={len(diff.ambiguous)} new_cols={len(diff.new)} new_tables={len(diff.new_tables)}"
    )

    pillar_yaml = PillarLoader().load(pillar_name) or {}
    chat_agent = ChatAgent(chat_llm, logger, tools=build_helper_tools())
    orch = Orchestrator(
        llm=None,
        logger=logger,
        registry=None,
        pillar=pillar_name,
        pillar_config=pillar_yaml,
        catalog=catalog,
        gateway=gw,
        clients=clients,
    )

    # Per-case loop
    for case in suite["test_cases"]:
        name = case["name"]
        question = case["question"]
        followups = case.get("followups", [])
        note = case.get("note", "")

        print()
        print("=" * 78)
        print(f"CASE: {name}")
        print(f"Q   : {question}")
        if note:
            print(f"NOTE: {note}")
        print("=" * 78)

        # Fresh AppContext per case so specialist memory + numeric vocab reset
        ctx = AppContext(
            gateway=gw,
            case_folder=PROJECT_ROOT / "reports" / case_id,
            logger=logger,
        )
        case_log = {"name": name, "question": question, "note": note, "turns": []}

        result, turn_log = await run_one_turn(
            f"{name}__t0",
            question,
            chat_agent=chat_agent,
            orch=orch,
            ctx=ctx,
        )
        case_log["turns"].append(turn_log)
        print(f"  t0 outcome: {turn_log['outcome']}")

        # Chain follow-ups (preserving ctx so specialist memory carries forward)
        for i, fu_q in enumerate(followups, start=1):
            if result is None:
                print(f"  skipping follow-up {i} — prior result is None")
                break
            result, turn_log = await run_one_turn(
                f"{name}__t{i}",
                fu_q,
                chat_agent=chat_agent,
                orch=orch,
                ctx=ctx,
                prior_result=result,
            )
            case_log["turns"].append(turn_log)
            print(f"  t{i} outcome: {turn_log['outcome']} — {fu_q!r}")

        # Engineer-facing structured detail
        json_path = PER_CASE_DIR / f"{name}.json"
        json_path.write_text(json.dumps(case_log, indent=2, default=str))
        # Non-engineer-facing narrative
        md_path = PER_CASE_DIR / f"{name}.md"
        md_path.write_text(render_case_md(case_log))
        print(f"  wrote {json_path.relative_to(PROJECT_ROOT)}")
        print(f"        {md_path.relative_to(PROJECT_ROOT)}")

    print()
    print("Done.")
    print(f"  Event-stream log : logs/{session_id}.jsonl")
    print(f"  Per-case logs    : logs/question_suite/<case_name>.json")


if __name__ == "__main__":
    asyncio.run(main())
