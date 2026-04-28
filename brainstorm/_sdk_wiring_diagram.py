"""Generates brainstorm/sdk-wiring-diagram.png — a block diagram of the SDK
wiring boundary. Run `python brainstorm/_sdk_wiring_diagram.py`."""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle

# Color palette
OURS = "#cfe5ff"
OURS_DARK = "#1a4d8a"
SDK = "#d4f1d4"
SDK_DARK = "#1a6b1a"
SIDE = "#ffe2c2"
SIDE_DARK = "#a85a00"
EXTERNAL = "#f4cdd6"
EXTERNAL_DARK = "#9b1d3d"

# Canvas — wider + taller for less cramped layout
fig, ax = plt.subplots(figsize=(14, 11))
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.axis("off")


def box(x, y, w, h, text, color, edge, *, bold=False, fontsize=9, italic=False):
    p = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.4,rounding_size=0.7",
        linewidth=1.4, edgecolor=edge, facecolor=color,
    )
    ax.add_patch(p)
    weight = "bold" if bold else "normal"
    style = "italic" if italic else "normal"
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fontsize, fontweight=weight, color="#222", fontstyle=style)


def arrow(x1, y1, x2, y2, *, style="-|>", color="#444", lw=1.4, ls="-"):
    a = FancyArrowPatch(
        (x1, y1), (x2, y2),
        arrowstyle=style, mutation_scale=14,
        color=color, linewidth=lw, linestyle=ls,
    )
    ax.add_patch(a)


# ── Title ────────────────────────────────────────────────────────────
ax.text(50, 96.5, "AgenticSys v2 — SDK Wiring Boundary",
        ha="center", fontsize=16, fontweight="bold")
ax.text(50, 93.5,
        "what the OpenAI Agents SDK does for us  vs  what we wire ourselves",
        ha="center", fontsize=9.5, color="#555", fontstyle="italic")

# ════════════════════════════════════════════════════════════════════
# LEFT COLUMN — request flow (pipeline)
# ════════════════════════════════════════════════════════════════════
LEFT_X, LEFT_W = 4, 56

# 1. Pre-orchestrator
box(LEFT_X, 84, LEFT_W, 4.5,
    "main.py — argparse · data init · init_tools(gateway, catalog, logger) · "
    "build_session_clients",
    OURS, OURS_DARK, fontsize=8.5)

# 2. ChatAgent.screen / redact
box(LEFT_X, 77, LEFT_W, 4.5,
    "ChatAgent.screen / redact / relevance_check    "
    "(uses FirewalledChatShim — NOT the Agents SDK)",
    OURS, OURS_DARK, fontsize=8.5)

# 3. Orchestrator.run
box(LEFT_X, 70, LEFT_W, 4.5,
    "Orchestrator.run(question, case_folder)\n"
    "build AppContext   ·   try/except AgentsException   ·   emit EventLogger events",
    OURS, OURS_DARK, fontsize=8.5, bold=True)

# 4. SDK boundary container
sdk_x, sdk_y, sdk_w, sdk_h = 1.5, 22, LEFT_W + 5, 45
sdk_box = FancyBboxPatch(
    (sdk_x, sdk_y), sdk_w, sdk_h,
    boxstyle="round,pad=0.6,rounding_size=1.2",
    linewidth=2.2, edgecolor=SDK_DARK, facecolor="#f1faf1",
    linestyle="--",
)
ax.add_patch(sdk_box)
ax.text(sdk_x + 1.5, sdk_y + sdk_h - 1.6,
        "▼ SDK BOUNDARY (openai-agents)",
        ha="left", va="top", fontsize=9.5, fontweight="bold", color=SDK_DARK,
        fontstyle="italic")

# 5. Runner.run inside SDK
box(LEFT_X, 60, LEFT_W, 4,
    "Runner.run(orchestrator_agent, question, context=ctx)",
    SDK, SDK_DARK, fontsize=9, bold=True)

# 6. Agent loop description
box(LEFT_X, 53.5, LEFT_W, 4.5,
    "Agent loop:    LLM call → parse tool_calls → dispatch (in parallel)\n"
    "→ feed tool results back → repeat → emit FinalAnswer",
    SDK, SDK_DARK, fontsize=8.3)

# 7. Tools header
ax.text(LEFT_X + LEFT_W / 2, 50.5,
        "Tools registered on the orchestrator agent  (9 total — all our code)",
        ha="center", fontsize=8.2, color=SDK_DARK, fontstyle="italic")

# 8. Three tool boxes (the redacting_tool wrappers)
TY, TH = 39, 9.5
gap = 1.5
each_w = (LEFT_W - 2 * gap) / 3

box(LEFT_X, TY, each_w, TH,
    "redacting_tool wraps:\n7 specialist agents\n"
    "(creditrisk, modeling,\nbureau, capacity_afford,\n"
    "spend_payments, wcc,\ncrossbu, customer_rel)",
    OURS, OURS_DARK, fontsize=7.3)

box(LEFT_X + each_w + gap, TY, each_w, TH,
    "redacting_tool wraps:\nreport_agent\n"
    "(scans case folder\nvia fs_list_files\nand fs_read_file)",
    OURS, OURS_DARK, fontsize=7.5)

box(LEFT_X + 2 * (each_w + gap), TY, each_w, TH,
    "redacting_tool wraps:\ngeneral_specialist\n"
    "(reviews specialist\noutputs for\ncontradictions)",
    OURS, OURS_DARK, fontsize=7.5)

# 9. Tool wrapper description (italic note)
ax.text(LEFT_X + LEFT_W / 2, 36.5,
        "every redacting_tool:   sanitize_message(input)  →  Runner.run(inner_agent)  →  redact_payload(output)",
        ha="center", fontsize=7.6, color=OURS_DARK, fontstyle="italic")

# 10. Inner-agent tools (the actual @function_tool functions)
box(LEFT_X, 27.5, (LEFT_W - 2) / 2, 6.5,
    "Specialist agents call:\n"
    "list_available_tables · get_table_schema · query_table\n"
    "(tools/data_tools.py — module-level state via init_tools)",
    OURS, OURS_DARK, fontsize=7.5)

box(LEFT_X + (LEFT_W - 2) / 2 + 2, 27.5, (LEFT_W - 2) / 2, 6.5,
    "Report agent calls:\n"
    "fs_list_files · fs_read_file\n"
    "(tools/fs_tools.py — RunContext-aware)",
    OURS, OURS_DARK, fontsize=7.5)

# 11. Post-SDK return
box(LEFT_X, 14.5, LEFT_W, 4.5,
    "result.final_output (FinalAnswer Pydantic)   →   "
    "redact_payload   →   ChatAgent.format(final)   →   stdout",
    OURS, OURS_DARK, fontsize=8.3, bold=True)

# 12. β fallback note
ax.text(LEFT_X + LEFT_W / 2, 11,
        "On AgentsException: Orchestrator._trace_extraction_fallback(exc) walks\n"
        "exc.run_data.new_items to recover completed ToolCallOutputItems →\n"
        "stitches a coverage-aware fallback FinalAnswer.",
        ha="center", fontsize=7.5, color="#444", fontstyle="italic")


# ════════════════════════════════════════════════════════════════════
# RIGHT COLUMN — firewall side-channel + auxiliary
# ════════════════════════════════════════════════════════════════════
RIGHT_X, RIGHT_W = 64, 33

# 1. FirewalledAsyncOpenAI header
box(RIGHT_X, 84, RIGHT_W, 4.5,
    "FirewalledAsyncOpenAI",
    SIDE, SIDE_DARK, fontsize=10, bold=True)

# 2. Three responsibilities
box(RIGHT_X, 73, RIGHT_W, 9.5,
    "every chat.completions.create:\n\n"
    "(a) sanitize_message on outbound messages\n"
    "(b) retry-with-guidance on FirewallRejection\n"
    "       (FIREWALL_GUIDANCE injected, max_retries)\n"
    "(c) shared asyncio.Semaphore caps concurrency",
    SIDE, SIDE_DARK, fontsize=7.8)

# 3. SDK adapter
box(RIGHT_X, 65, RIGHT_W, 5.5,
    "OpenAIChatCompletionsModel\n"
    "(SDK adapter consuming our wrapped client)",
    SDK, SDK_DARK, fontsize=8.3)

# 4. Real OpenAI
box(RIGHT_X, 57, RIGHT_W, 5,
    "openai.AsyncOpenAI  →  api.openai.com",
    EXTERNAL, EXTERNAL_DARK, fontsize=8.5, bold=True)

# 5. Side note about creation
ax.text(RIGHT_X + RIGHT_W / 2, 53,
        "Constructed once per session in build_session_clients(...)\n"
        "and shared by every Agent + the ChatAgent shim.",
        ha="center", fontsize=7.5, color="#444", fontstyle="italic")

# 6. ChatAgent shim
box(RIGHT_X, 39, RIGHT_W, 8.5,
    "ChatAgent (case_agents/chat_agent.py)\n"
    "uses FirewalledChatShim → same firewalled client\n"
    "but bypasses the Agents SDK entirely.\n"
    "screen / redact / relevance_check / converse",
    OURS, OURS_DARK, fontsize=7.7)

# 7. EventLogger
box(RIGHT_X, 27.5, RIGHT_W, 8.5,
    "EventLogger (logger/event_logger.py)\n"
    "Hand-emitted JSONL events:\n"
    "orchestrator_run_start/done/blocked,\n"
    "firewall_rejection/blocked, tool_call, tool_result.\n"
    "(SDK has its own tracing, not consumed here)",
    OURS, OURS_DARK, fontsize=7.5)

# 8. Spec / plan refs
ax.text(RIGHT_X + RIGHT_W / 2, 22,
        "see  docs/specs/2026-04-28-...-design.md\n"
        "and  docs/plans/2026-04-28-...-migration.md",
        ha="center", fontsize=7, color="#666", fontstyle="italic")


# ════════════════════════════════════════════════════════════════════
# ARROWS — main pipeline flow
# ════════════════════════════════════════════════════════════════════
arrow(LEFT_X + LEFT_W / 2, 84, LEFT_X + LEFT_W / 2, 81.5)        # 1→2
arrow(LEFT_X + LEFT_W / 2, 77, LEFT_X + LEFT_W / 2, 74.5)        # 2→3
arrow(LEFT_X + LEFT_W / 2, 70, LEFT_X + LEFT_W / 2, 64)          # 3→Runner
arrow(LEFT_X + LEFT_W / 2, 60, LEFT_X + LEFT_W / 2, 58)          # Runner→loop
arrow(LEFT_X + LEFT_W / 2, 53.5, LEFT_X + LEFT_W / 2, 48.5)      # loop→tools header
arrow(LEFT_X + LEFT_W / 2, 39, LEFT_X + LEFT_W / 2, 34)          # tools→inner tools
arrow(LEFT_X + LEFT_W / 2, 27.5, LEFT_X + LEFT_W / 2, 19)        # inner→result

# ════════════════════════════════════════════════════════════════════
# ARROWS — side channel (firewall feeds every LLM call)
# ════════════════════════════════════════════════════════════════════
# Curved-feel arrow showing every LLM call in the SDK loop hits the firewall
arrow(LEFT_X + LEFT_W + 0.5, 56, RIGHT_X - 0.5, 78,
      color=SIDE_DARK, lw=1.2, ls="--")
ax.text((LEFT_X + LEFT_W + RIGHT_X) / 2, 71,
        "every LLM call",
        ha="center", fontsize=7.5, color=SIDE_DARK, fontstyle="italic",
        fontweight="bold")

# ChatAgent shim → firewall (also)
arrow(RIGHT_X + RIGHT_W / 2, 47.5, RIGHT_X + RIGHT_W / 2, 73,
      color=SIDE_DARK, lw=1, ls="--")
ax.text(RIGHT_X + RIGHT_W + 0.5, 60, "ChatAgent\nshim path",
        ha="left", fontsize=6.8, color=SIDE_DARK, fontstyle="italic")

# Internal firewall flow
arrow(RIGHT_X + RIGHT_W / 2, 73, RIGHT_X + RIGHT_W / 2, 70.5, color=SIDE_DARK)
arrow(RIGHT_X + RIGHT_W / 2, 65, RIGHT_X + RIGHT_W / 2, 62, color=SDK_DARK)


# ════════════════════════════════════════════════════════════════════
# Legend
# ════════════════════════════════════════════════════════════════════
LX, LY = 4, 3

def legend_swatch(x, y, color, edge, label_text):
    p = FancyBboxPatch(
        (x, y), 2.5, 1.6,
        boxstyle="round,pad=0.05,rounding_size=0.2",
        linewidth=1, edgecolor=edge, facecolor=color,
    )
    ax.add_patch(p)
    ax.text(x + 3, y + 0.8, label_text, va="center", fontsize=7.8, color="#222")


legend_swatch(LX, LY, OURS, OURS_DARK, "our code (we wire this)")
legend_swatch(LX + 24, LY, SDK, SDK_DARK, "SDK-managed (auto)")
legend_swatch(LX + 47, LY, SIDE, SIDE_DARK, "firewall client (we wire)")
legend_swatch(LX + 73, LY, EXTERNAL, EXTERNAL_DARK, "external API")

out = Path(__file__).parent / "sdk-wiring-diagram.png"
plt.savefig(out, dpi=190, bbox_inches="tight", facecolor="white")
print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
