"""Generate notebooks/test_chat_mode.ipynb — an A1-pipeline integration demo
that exposes every intermediate workflow stage. Run from project root:

    python notebooks/_build_test_chat_mode.py
"""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf

NB_PATH = Path(__file__).parent / "test_chat_mode.ipynb"


def md(s: str) -> nbf.NotebookNode:
    return nbf.v4.new_markdown_cell(s)


def code(s: str) -> nbf.NotebookNode:
    return nbf.v4.new_code_cell(s)


cells: list[nbf.NotebookNode] = []

# 0. Title + intro
cells.append(md(
    "# End-to-End Workflow Demo (post openai-agents migration)\n\n"
    "Exercises the **A1 SDK pipeline** for one reviewer question and exposes every "
    "intermediate result so you can see exactly what each stage produces:\n\n"
    "1. `ChatAgent.screen` — verdict + redacted question\n"
    "2. `ChatAgent.relevance_check` — in-scope / out-of-scope\n"
    "3. `Runner.run(orchestrator_agent, ...)` — single SDK call (replaces the legacy "
    "plan_team → fan-out → compare → synthesize → balance pipeline)\n"
    "4. Inspect `result.new_items`:\n"
    "   - **Tool calls** = the sub-questions the orchestrator dispatched to each "
    "specialist / report_agent / general_specialist\n"
    "   - **Tool outputs** = `SpecialistOutput`, `ReportDraft`, `ReviewReport`\n"
    "5. `redact_payload` + `ChatAgent.format` → final markdown answer\n\n"
    "We bypass `Orchestrator.run` in this notebook and call `Runner.run` directly so "
    "we can introspect the SDK's run-history. The agent graph itself comes from the "
    "same factories that production uses.\n\n"
    "**Architecture refs:** [brainstorm/sdk-wiring.md](../brainstorm/sdk-wiring.md), "
    "[brainstorm/architecture-explanation.md](../brainstorm/architecture-explanation.md)."
))

# 1. Setup
cells.append(md("## 0. Setup"))
cells.append(code(
    "%load_ext dotenv\n"
    "%dotenv\n"
    "%load_ext autoreload\n"
    "%autoreload 2\n\n"
    "import os, sys, json\n"
    "import nest_asyncio; nest_asyncio.apply()\n\n"
    "PROJECT_ROOT = os.path.abspath(os.path.join(os.getcwd(), '..')) \\\n"
    "    if os.path.basename(os.getcwd()) == 'notebooks' else os.getcwd()\n"
    "if PROJECT_ROOT not in sys.path:\n"
    "    sys.path.insert(0, PROJECT_ROOT)\n\n"
    "print(f'PROJECT_ROOT: {PROJECT_ROOT}')\n"
    "print(f'OPENAI_API_KEY set: {bool(os.environ.get(\"OPENAI_API_KEY\"))}')"
))

# 2. Knobs
cells.append(md("## 1. Knobs"))
cells.append(code(
    "# ─── pick a question, pillar, model ───\n"
    "QUESTION = \"Is this case high risk? What are the top concerns?\"\n"
    "PILLAR = \"credit_risk\"\n"
    "MODEL = \"gpt-4o\"\n\n"
    "# ─── data source: 'auto' resolves real → simulated → generator ───\n"
    "DATA_SOURCE = \"auto\""
))

# 3. Build session clients
cells.append(md(
    "## 2. Build session clients (the SDK + firewall layer)\n\n"
    "This is the **same wiring `main.py` uses**: `FirewallStack` holds the semaphore + "
    "retry policy; `build_session_clients` produces a `FirewalledAsyncOpenAI` wrapping "
    "the real `openai.AsyncOpenAI` plus an `OpenAIChatCompletionsModel` adapter for the "
    "Agents SDK; `FirewalledChatShim` preserves `ChatAgent`'s legacy `ainvoke` surface."
))
cells.append(code(
    "from llm.firewall_stack import FirewallStack\n"
    "from llm.factory import build_session_clients, FirewalledChatShim\n"
    "from logger.event_logger import EventLogger\n\n"
    "logger = EventLogger(session_id='nb-demo', log_dir=os.path.join(PROJECT_ROOT, 'logs'))\n"
    "firewall = FirewallStack(logger=logger, max_retries=2, concurrency_cap=8)\n"
    "clients = build_session_clients(firewall, model_name=MODEL)\n"
    "chat_llm = FirewalledChatShim(clients)\n\n"
    "print('clients.firewalled_client :', type(clients.firewalled_client).__name__)\n"
    "print('clients.model              :', type(clients.model).__name__)\n"
    "print('chat_llm                    :', type(chat_llm).__name__)"
))

# 4. Initialize data layer
cells.append(md(
    "## 3. Initialize data layer (gateway → catalog → tools)\n\n"
    "`init_tools(gateway, catalog, logger)` sets the module-level state that the "
    "`@function_tool`-decorated data tools (`list_available_tables`, `get_table_schema`, "
    "`query_table`) read from at call time. The SDK doesn't know about this — it's our "
    "lexical-scope dependency injection (see `brainstorm/sdk-wiring.md` §2.4)."
))
cells.append(code(
    "from datalayer.catalog import DataCatalog\n"
    "from datalayer.gateway import LocalDataGateway\n"
    "from datalayer.generator import DataGenerator\n"
    "from tools.data_tools import init_tools\n"
    "from pathlib import Path\n\n"
    "_DATA = Path(PROJECT_ROOT) / 'data_tables'\n"
    "real_dir, sim_dir = _DATA / 'real', _DATA / 'simulated'\n\n"
    "def _has(p): return p.is_dir() and any(c.is_dir() for c in p.iterdir())\n\n"
    "if DATA_SOURCE == 'real' or (DATA_SOURCE == 'auto' and _has(real_dir)):\n"
    "    gw = LocalDataGateway.from_case_folders(str(real_dir))\n"
    "    src = 'real'\n"
    "elif DATA_SOURCE == 'simulated' or (DATA_SOURCE == 'auto' and _has(sim_dir)):\n"
    "    gw = LocalDataGateway.from_case_folders(str(sim_dir))\n"
    "    src = 'simulated'\n"
    "else:\n"
    "    gen = DataGenerator(seed=42, cases=20); gen.load_profiles()\n"
    "    gw = LocalDataGateway.from_generated(gen.generate_all())\n"
    "    src = 'generator'\n\n"
    "catalog = DataCatalog()\n"
    "init_tools(gw, catalog, logger=logger)\n\n"
    "available = gw.list_case_ids()\n"
    "print(f'data source : {src}')\n"
    "print(f'cases       : {len(available)} (showing first 5: {available[:5]})')\n"
    "case_id = available[0]\n"
    "gw.set_case(case_id)\n"
    "print(f'using case  : {case_id}')\n"
    "print(f'tables      : {gw.list_tables()}')"
))

# 5. Build orchestrator + ChatAgent
cells.append(md(
    "## 4. Build the agent graph + ChatAgent\n\n"
    "`Orchestrator(...)` with `clients=` constructs the **same agent graph production "
    "uses**: 7 specialists + report_agent + general_specialist, all wrapped via "
    "`redacting_tool` and registered as tools on `orchestrator_agent`."
))
cells.append(code(
    "from case_agents.chat_agent import ChatAgent\n"
    "from case_agents.helper_tools import build_helper_tools\n"
    "from config.pillar_loader import PillarLoader\n"
    "from orchestrator.orchestrator import Orchestrator\n\n"
    "pillar_yaml = PillarLoader().load(PILLAR) or {}\n"
    "helper_tools = build_helper_tools()\n"
    "chat_agent = ChatAgent(chat_llm, logger, tools=helper_tools)\n\n"
    "orch = Orchestrator(\n"
    "    llm=None, logger=logger, registry=None,\n"
    "    pillar=PILLAR, pillar_config=pillar_yaml,\n"
    "    catalog=catalog, gateway=gw, clients=clients,\n"
    ")\n\n"
    "agent = orch.orchestrator_agent\n"
    "print(f'orchestrator agent: {agent.name}')\n"
    "print(f'tools registered  : {len(agent.tools)}')\n"
    "for t in agent.tools:\n"
    "    print(f'   • {t.name:<22}  — {t.description[:70]}')"
))

# 6. Stage A — Screen
cells.append(md(
    "## 5. STAGE A — `ChatAgent.screen(question)`\n\n"
    "First boundary. ChatAgent decides whether the question is safe + in scope and "
    "produces a redacted version (CASE-IDs and 6+ digit runs masked)."
))
cells.append(code(
    "import asyncio\n"
    "from IPython.display import Markdown, display\n\n"
    "verdict = asyncio.get_event_loop().run_until_complete(chat_agent.screen(QUESTION))\n\n"
    "print(f'passed              : {verdict.passed}')\n"
    "print(f'reason              : {verdict.reason!r}')\n"
    "print(f'original question   : {QUESTION!r}')\n"
    "print(f'redacted question   : {verdict.redacted_question!r}')\n\n"
    "screened_question = verdict.redacted_question if verdict.passed else None"
))

# 7. Stage B — Relevance check (optional, runs only if screen passed)
cells.append(md(
    "## 6. STAGE B — `ChatAgent.relevance_check(question)`  (in-scope test)\n\n"
    "Independent in-scope/out-of-scope check. The legacy code uses this to decide "
    "whether a question even warrants the orchestrator pipeline. Today's `main.py` "
    "doesn't call it explicitly (`screen` already covers most cases) — surfacing it "
    "here for visibility."
))
cells.append(code(
    "if screened_question is None:\n"
    "    print('Skipping — screen rejected the question.')\n"
    "else:\n"
    "    in_scope, reason = asyncio.get_event_loop().run_until_complete(\n"
    "        chat_agent.relevance_check(screened_question)\n"
    "    )\n"
    "    print(f'in_scope            : {in_scope}')\n"
    "    print(f'reason              : {reason!r}')"
))

# 8. Stage C — Run the orchestrator agent (direct Runner.run for trace access)
cells.append(md(
    "## 7. STAGE C — `Runner.run(orchestrator_agent, …)`\n\n"
    "Single SDK call. The orchestrator agent's instructions tell it to call specialists "
    "in parallel, then synthesize a `FinalAnswer`. We call `Runner.run` directly here "
    "(rather than `Orchestrator.run`) because we want post-run access to "
    "`result.new_items` for the next stages.\n\n"
    "**This is the only LLM-heavy cell in the notebook** — expect it to run for a few "
    "seconds + use API quota."
))
cells.append(code(
    "from agents import Runner\n"
    "from agents.exceptions import AgentsException\n"
    "from case_agents.app_context import AppContext\n"
    "from llm.firewall_stack import redact_payload\n\n"
    "if screened_question is None:\n"
    "    raise SystemExit('Question did not pass screen — nothing to run.')\n\n"
    "ctx = AppContext(gateway=gw, case_folder=Path(PROJECT_ROOT) / 'reports' / case_id,\n"
    "                 logger=logger)\n\n"
    "try:\n"
    "    result = asyncio.get_event_loop().run_until_complete(\n"
    "        Runner.run(orch.orchestrator_agent, screened_question, context=ctx)\n"
    "    )\n"
    "    print(f'Runner.run completed.')\n"
    "    print(f'final_output type : {type(result.final_output).__name__}')\n"
    "    print(f'new_items count   : {len(result.new_items)}')\n"
    "except AgentsException as e:\n"
    "    print(f'Runner.run raised : {type(e).__name__}: {e}')\n"
    "    print('  → in production, Orchestrator.run would invoke the trace-extraction fallback here')\n"
    "    result = None"
))

# 9. Stage D — Tool calls = sub-questions
cells.append(md(
    "## 8. STAGE D — Sub-questions (tool calls the orchestrator emitted)\n\n"
    "Each `ToolCallItem` in `result.new_items` is a tool the orchestrator's LLM "
    "decided to call. The tool name tells us **which specialist** got the call; the "
    "JSON args tell us **what sub-question** they were asked.\n\n"
    "(In the legacy pipeline, this corresponded to `plan_team`'s output — the "
    "`TeamAssignment` list. Now it's emergent from the orchestrator agent's reasoning.)"
))
cells.append(code(
    "from agents.items import ToolCallItem, ToolCallOutputItem, MessageOutputItem\n\n"
    "if result is None:\n"
    "    print('No result; skipping.')\n"
    "else:\n"
    "    tool_calls = [i for i in result.new_items if isinstance(i, ToolCallItem)]\n"
    "    print(f'{len(tool_calls)} tool call(s) emitted by orchestrator:\\n')\n"
    "    for i, item in enumerate(tool_calls, 1):\n"
    "        raw = item.raw_item\n"
    "        name = getattr(raw, 'name', None) or raw.get('name', '?')\n"
    "        args = getattr(raw, 'arguments', None) or raw.get('arguments', '{}')\n"
    "        try:\n"
    "            parsed = json.loads(args) if isinstance(args, str) else args\n"
    "        except json.JSONDecodeError:\n"
    "            parsed = {'raw': args}\n"
    "        sub_q = parsed.get('sub_question') or parsed.get('input') or parsed\n"
    "        print(f'[{i}] → {name}')\n"
    "        print(f'      sub-question: {sub_q}')"
))

# 10. Stage E — Tool outputs = specialist findings
cells.append(md(
    "## 9. STAGE E — Specialist findings (tool outputs)\n\n"
    "Each `ToolCallOutputItem.output` is the **already-deserialized Pydantic** the "
    "wrapped sub-agent returned (`SpecialistOutput`, `ReportDraft`, or `ReviewReport`), "
    "post `redact_payload`. Pairing with the corresponding `ToolCallItem` above, you "
    "see Q → A for each specialist."
))
cells.append(code(
    "if result is None:\n"
    "    print('No result; skipping.')\n"
    "else:\n"
    "    tool_outputs = [i for i in result.new_items if isinstance(i, ToolCallOutputItem)]\n"
    "    print(f'{len(tool_outputs)} tool output(s) returned to the orchestrator:\\n')\n"
    "    for i, item in enumerate(tool_outputs, 1):\n"
    "        raw = item.raw_item\n"
    "        # Recover the tool name from the output item's raw representation\n"
    "        name = (raw.get('name') if isinstance(raw, dict) else None) or '?'\n"
    "        # Try to align with a tool call by call_id\n"
    "        call_id = (raw.get('call_id') if isinstance(raw, dict) else None)\n"
    "        if name == '?' and call_id:\n"
    "            for tc in [x for x in result.new_items if isinstance(x, ToolCallItem)]:\n"
    "                tcr = tc.raw_item\n"
    "                tcid = getattr(tcr, 'call_id', None) or (tcr.get('call_id') if isinstance(tcr, dict) else None)\n"
    "                if tcid == call_id:\n"
    "                    name = getattr(tcr, 'name', None) or tcr.get('name', '?')\n"
    "                    break\n"
    "        out = item.output\n"
    "        print(f'━━━ [{i}] from {name} ' + '━' * (60 - len(str(name))))\n"
    "        if hasattr(out, 'model_dump'):\n"
    "            payload = out.model_dump()\n"
    "        else:\n"
    "            payload = out\n"
    "        print(json.dumps(payload, indent=2, default=str)[:1500])\n"
    "        print()"
))

# 11. Stage F — General specialist's review
cells.append(md(
    "## 10. STAGE F — `general_specialist`'s review (if called)\n\n"
    "Pulled out separately because contradictions / cross-domain insights are useful to "
    "highlight. If the orchestrator chose not to consult `general_specialist`, this "
    "section will be empty."
))
cells.append(code(
    "if result is None:\n"
    "    print('No result; skipping.')\n"
    "else:\n"
    "    review = None\n"
    "    for item in result.new_items:\n"
    "        if not isinstance(item, ToolCallOutputItem):\n"
    "            continue\n"
    "        raw = item.raw_item\n"
    "        call_id = raw.get('call_id') if isinstance(raw, dict) else None\n"
    "        # match by call_id back to a tool_call named 'general_specialist'\n"
    "        for tc in [x for x in result.new_items if isinstance(x, ToolCallItem)]:\n"
    "            tcr = tc.raw_item\n"
    "            tname = getattr(tcr, 'name', None) or (tcr.get('name') if isinstance(tcr, dict) else None)\n"
    "            tcid = getattr(tcr, 'call_id', None) or (tcr.get('call_id') if isinstance(tcr, dict) else None)\n"
    "            if tname == 'general_specialist' and tcid == call_id:\n"
    "                review = item.output\n"
    "                break\n"
    "        if review is not None:\n"
    "            break\n\n"
    "    if review is None:\n"
    "        print('Orchestrator did not call general_specialist for this question.')\n"
    "    else:\n"
    "        payload = review.model_dump() if hasattr(review, 'model_dump') else review\n"
    "        print('ReviewReport:')\n"
    "        print(json.dumps(payload, indent=2, default=str))"
))

# 12. Stage G — Final answer
cells.append(md(
    "## 11. STAGE G — Final answer (post `redact_payload` + `ChatAgent.format`)\n\n"
    "This is what `Orchestrator.run` would have returned: the redacted `FinalAnswer` "
    "rendered as reviewer-facing markdown."
))
cells.append(code(
    "if result is None:\n"
    "    print('No result; skipping.')\n"
    "else:\n"
    "    final = redact_payload(result.final_output)\n"
    "    print('═══ FinalAnswer (Pydantic, redacted) ═══')\n"
    "    print(json.dumps(final.model_dump(), indent=2, default=str)[:2000])\n"
    "    print()\n"
    "    print('═══ Rendered via ChatAgent.format ═══')\n"
    "    display(Markdown(chat_agent.format(final)))"
))

# 13. Stage H — Event log
cells.append(md(
    "## 12. STAGE H — EventLogger trail (optional)\n\n"
    "Hand-emitted JSONL events. Useful for spotting `firewall_rejection`, "
    "`tool_call`, `tool_result`, `orchestrator_run_blocked`, etc."
))
cells.append(code(
    "log_path = Path(PROJECT_ROOT) / 'logs' / f'nb-demo.jsonl'\n"
    "if not log_path.exists():\n"
    "    print(f'No log at {log_path}')\n"
    "else:\n"
    "    from collections import Counter\n"
    "    events = [json.loads(line) for line in log_path.open() if line.strip()]\n"
    "    counts = Counter(e.get('event', '?') for e in events)\n"
    "    print(f'{len(events)} events total in {log_path.name}:')\n"
    "    for k, v in counts.most_common():\n"
    "        print(f'  {v:3d}  {k}')\n"
    "    print()\n"
    "    print('Last 5 events:')\n"
    "    for e in events[-5:]:\n"
    "        print(f\"  {e.get('event','?'):<28}  {json.dumps(e.get('payload', {}))[:80]}\")"
))

# Build notebook
nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {
    "kernelspec": {
        "name": "python3",
        "display_name": "Python 3",
        "language": "python",
    },
    "language_info": {"name": "python"},
}

with open(NB_PATH, "w") as f:
    nbf.write(nb, f)

print(f"wrote {NB_PATH} ({NB_PATH.stat().st_size // 1024} KB, {len(cells)} cells)")
