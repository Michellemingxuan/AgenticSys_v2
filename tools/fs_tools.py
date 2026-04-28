"""Filesystem tools for the report agent. Confined to the active case folder."""
from __future__ import annotations

from pathlib import Path

from agents import RunContextWrapper, function_tool

from case_agents.app_context import AppContext


@function_tool
async def fs_list_files(ctx: RunContextWrapper[AppContext]) -> str:
    folder = ctx.context.case_folder
    if folder is None or not folder.exists():
        return "No case folder available."
    files = [p.name for p in folder.iterdir() if p.is_file()]
    return "\n".join(sorted(files)) if files else "Folder is empty."


@function_tool
async def fs_read_file(ctx: RunContextWrapper[AppContext], filename: str) -> str:
    folder = ctx.context.case_folder
    if folder is None:
        return "No case folder available."
    target = (folder / filename).resolve()
    # Confine to case_folder to prevent path traversal.
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return f"Access denied: '{filename}' is outside the case folder."
    if not target.exists() or not target.is_file():
        return f"File not found: {filename}"
    return target.read_text()
