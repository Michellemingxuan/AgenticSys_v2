"""Data Manager Agent — data-side governance.

Fronts the `LocalDataGateway` + `DataCatalog` for everything downstream
of the specialists. Two responsibilities:

  1. `query(...)`           — wrap `tools.data_tools.query_table` and apply
                              the redact.md patterns (mask 6+-digit runs +
                              CASE-IDs) to every string field before return.
  2. `describe_catalog()`   — return the catalog's prompt-context, framed
                              by the `workflow/data_catalog.md` skill for
                              downstream consumers (Orchestrator at team-
                              construction + synthesis time).

The agent is intentionally synchronous for the hot query path — the gateway
is in-memory today and wrapping every row in an `await` buys nothing. The
`describe_catalog()` method is also sync; it becomes async when the catalog
grows an LLM-driven describe-step in a later phase.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from datalayer import adapter
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from logger.event_logger import EventLogger
from skills.loader import load_skill as _load_skill
from tools.data_tools import _query_table_impl as query_table


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


def _parse_json(text, default):
    """Extract the first JSON object from *text*, returning *default* on failure."""
    try:
        m = re.search(r"\{.*\}", str(text), re.DOTALL)
        return json.loads(m.group(0)) if m else default
    except (ValueError, TypeError):
        return default


# Shared patterns with gateway/firewall_stack._sanitize_message + case_scrubber.
# Kept as module constants so they can be re-used by other redact-aware code
# paths without duplicating the regex.
_DIGIT_RUN_RE = re.compile(r"\d{6,}")
_DIGIT_RUN_MASK = "***MASKED***"
_CASE_ID_RE = re.compile(r"CASE-\d+")
_CASE_ID_MASK = "[CASE-ID]"


class DataManagerAgent:
    """Governed fronting of the case-data layer."""

    def __init__(
        self,
        gateway: LocalDataGateway,
        catalog: DataCatalog,
        llm: Any,
        logger: EventLogger,
    ):
        self.gateway = gateway
        self.catalog = catalog
        self.llm = llm
        self.logger = logger
        self._catalog_prompt = _load_skill(_WORKFLOW_DIR / "data_catalog.md").body

    def query(
        self,
        table: str,
        filter_column: str = "",
        filter_value: str = "",
        filter_op: str = "eq",
        columns: str = "",
    ) -> str:
        """Pull rows from the named table and return a redacted string.

        Delegates to `tools.data_tools.query_table` (the same tool the LLM
        sees via `bind_tools`), then applies redact patterns on the
        returned string. Returns a plain string so the return value can be
        fed directly into a system prompt without extra serialization.
        """
        self.logger.log(
            "data_manager_query",
            {
                "table": table,
                "filter_column": filter_column,
                "filter_op": filter_op if filter_column and filter_value else None,
            },
        )
        raw = query_table(
            table_name=table,
            filter_column=filter_column,
            filter_value=filter_value,
            filter_op=filter_op,
            columns=columns,
        )
        return self._redact(raw)

    def describe_catalog(self) -> str:
        """Return the catalog prompt-context (case-filtered when a case is
        active), preceded by the data_catalog skill body.
        """
        if self.catalog is None:
            return self._catalog_prompt

        case_schema = self._build_case_schema()
        context = self.catalog.to_prompt_context(case_schema=case_schema)
        return f"{self._catalog_prompt}\n\n{context}".rstrip()

    def _build_case_schema(self) -> dict[str, list[str]] | None:
        """Return {table: [real_col_names]} for the current case, or None if
        no case is active (falls back to full-catalog rendering).
        """
        if self.gateway.get_case_id() is None:
            return None
        schema: dict[str, list[str]] = {}
        for table in self.gateway.list_tables():
            rows = self.gateway.query(table) or []
            schema[table] = list(rows[0].keys()) if rows else []
        return schema

    def sync_catalog(self, case_id: str) -> adapter.Diff:
        """Reconcile a real case folder against the canonical catalog.

        Auto-aliased matches and new columns are persisted to the YAML
        profiles. Ambiguous matches are returned but NOT persisted —
        callers (typically the data_catalog_sync skill) resolve them with
        human input.
        """
        self.logger.log("data_manager_sync_start", {"case_id": case_id})
        canonical = {
            table: self.catalog._profiles[table]["columns"]
            for table in self.catalog.list_tables()
        }
        diff = adapter.reconcile_case(self.gateway, canonical, case_id)
        adapter.apply_diff(diff, self.catalog)
        self.logger.log(
            "data_manager_sync_done",
            {
                "case_id": case_id,
                "auto": len(diff.auto_aliased),
                "ambiguous": len(diff.ambiguous),
                "new": len(diff.new),
                "new_tables": len(diff.new_tables),
                "value_drift": len(diff.value_drift),
            },
        )
        return diff

    async def draft_description(
        self,
        table: str,
        column: str,
        samples: list,
        sibling_columns: list[str] | None = None,
        dtype: str | None = None,
    ) -> str:
        """Use the LLM to draft a one-sentence column description.

        Falls back to an empty string when the LLM is unavailable or the
        firewall blocks the call. Caller is responsible for the regex-based
        fallback (``adapter._draft_description``) if it wants one.
        """
        if self.llm is None or not hasattr(self.llm, "ainvoke"):
            return ""

        sample_strs = [
            str(s)[:80] for s in samples[:10] if s is not None and s != ""
        ]
        samples_block = ", ".join(sample_strs) if sample_strs else "(no non-null samples)"
        dtype_block = f"Inferred dtype: {dtype}\n" if dtype else ""
        sibling_block = ""
        if sibling_columns:
            sibling_block = (
                f"Other columns in table '{table}': "
                f"{', '.join(sibling_columns[:25])}\n"
            )

        user = (
            f"Table: {table}\n"
            f"Column: {column}\n"
            f"{dtype_block}"
            f"Sample values: {samples_block}\n"
            f"{sibling_block}\n"
            "Write ONE sentence describing this column for use by credit-risk "
            "analysts. Mention units (USD, %, days, count, etc.) if you can "
            "infer them from the samples or name. Do NOT speculate beyond "
            "what the name and samples support. Return only the sentence — "
            "no quotes, no preamble."
        )

        result = await self.llm.ainvoke(
            system_prompt=(
                "You are a senior data steward writing concise column "
                "descriptions for a credit-risk analytics catalog."
            ),
            user_message=user,
        )
        if result.status != "success" or not result.data:
            return ""
        text = str(result.data.get("response", "")).strip()
        return text.strip('"').strip("'").strip()

    async def draft_table_description(
        self,
        table: str,
        column_names: list[str],
    ) -> str:
        """Use the LLM to draft a one-sentence table description."""
        if self.llm is None or not hasattr(self.llm, "ainvoke"):
            return ""

        cols_block = ", ".join(column_names[:30])
        user = (
            f"Table: {table}\n"
            f"Columns: {cols_block}\n\n"
            "Write ONE sentence describing what this table contains, for "
            "use by credit-risk analysts. Return only the sentence."
        )
        result = await self.llm.ainvoke(
            system_prompt=(
                "You are a senior data steward writing concise table "
                "descriptions for a credit-risk analytics catalog."
            ),
            user_message=user,
        )
        if result.status != "success" or not result.data:
            return ""
        text = str(result.data.get("response", "")).strip()
        return text.strip('"').strip("'").strip()

    async def match_column(self, real_col, real_samples, canonical_candidates) -> dict:
        """Match a real column name to one canonical candidate via the LLM.

        Returns ``{"canonical_col": str | None, "confidence": float}``.
        Degrades gracefully to ``{canonical_col: None, confidence: 0.0}``
        when the LLM is unavailable.
        """
        if self.llm is None or not hasattr(self.llm, "ainvoke"):
            return {"canonical_col": None, "confidence": 0.0}
        user_message = (
            "Match a real data column to one canonical column, or decline.\n"
            f"Real column: {real_col}\nSample values: {real_samples[:10]}\n"
            f"Canonical candidates: {canonical_candidates}\n"
            'Reply ONLY JSON: {"canonical_col": <name or null>, "confidence": <0..1>}'
        )
        result = await self.llm.ainvoke(
            system_prompt="You are a data steward matching real column names to a canonical schema.",
            user_message=user_message,
        )
        if result.status != "success" or not result.data:
            return {"canonical_col": None, "confidence": 0.0}
        text = str(result.data.get("response", "")).strip()
        out = _parse_json(text, default={"canonical_col": None, "confidence": 0.0})
        try:
            out["confidence"] = float(out.get("confidence") or 0.0)
        except (TypeError, ValueError):
            out["confidence"] = 0.0
        return out

    async def polish_description(self, var_name, raw_description, knowledge_brief) -> str:
        """Rewrite a column description to be clearer and more specific.

        Returns *raw_description* unchanged when the LLM is unavailable.
        The prompt instructs the model not to invent facts or thresholds.
        """
        if self.llm is None or not hasattr(self.llm, "ainvoke"):
            return raw_description
        user_message = (
            "Rewrite this variable description to be clear and specific, preserving "
            "its meaning. Do NOT add thresholds or invent facts. Use the house "
            f"knowledge brief for grounding.\nVariable: {var_name}\n"
            f"Description: {raw_description}\nKnowledge brief: {knowledge_brief}\n"
            "Reply with the improved description only."
        )
        result = await self.llm.ainvoke(
            system_prompt="You are a senior data steward improving column descriptions for a credit-risk analytics catalog.",
            user_message=user_message,
        )
        if result.status != "success" or not result.data:
            return raw_description
        text = str(result.data.get("response", "")).strip()
        return text or raw_description

    async def normalize_threshold_text(self, text) -> dict | None:
        """Parse a threshold sentence the regex couldn't handle.

        Returns a dict with ``risk_threshold`` and ``risk_direction`` keys,
        or ``None`` when the LLM is unavailable, the reply is unparseable,
        or — critically — when the LLM invents a numeric value not present
        in the source *text* (no-invent invariant).
        """
        if not text or self.llm is None or not hasattr(self.llm, "ainvoke"):
            return None
        user_message = (
            "Extract the risk threshold from this sentence into JSON. Do NOT invent "
            "numbers — use only values present in the text.\n"
            f"Sentence: {text}\n"
            'Reply ONLY JSON: {"risk_threshold": <number or [lo,hi]>, '
            '"risk_direction": "above"|"below"|"range"}'
        )
        result = await self.llm.ainvoke(
            system_prompt="You are a data steward extracting numeric risk thresholds from text into structured JSON.",
            user_message=user_message,
        )
        if result.status != "success" or not result.data:
            return None
        resp_text = str(result.data.get("response", "")).strip()
        parsed = _parse_json(resp_text, default=None)
        if not parsed:
            return None
        # Invariant guard: every numeric value returned MUST appear in the source.
        nums_in_text = set(re.findall(r"-?\d+(?:\.\d+)?", text))
        vals = parsed.get("risk_threshold")
        vals = vals if isinstance(vals, list) else [vals]
        for v in vals:
            if v is None:
                return None
            try:
                if (
                    str(v) not in nums_in_text
                    and str(int(v)) not in nums_in_text
                    and (f"{v:g}" not in nums_in_text)
                ):
                    return None
            except (TypeError, ValueError):
                return None
        return parsed

    def verify_description(
        self,
        table: str,
        column: str,
        new_text: str | None = None,
    ) -> None:
        """Mark a column's description as human-verified.

        If ``new_text`` is provided, the description is overwritten first.
        ``description_pending`` is flipped to ``False`` in both cases.
        """
        patch: dict = {"columns": {column: {"description_pending": False}}}
        if new_text is not None:
            patch["columns"][column]["description"] = new_text
        self.catalog.write_profile_patch(table, patch)
        self.logger.log(
            "data_manager_verify_desc",
            {"table": table, "column": column, "edited": new_text is not None},
        )

    @staticmethod
    def _redact(text: str) -> str:
        """Apply the redact-skill patterns to a data payload.

        Two regex layers (case_scrubber.scrub is case-id-specific and only
        masks a known literal, so DataManager uses its own broader
        `CASE-\\d+` pattern for general output):

          - `CASE-\\d+`            → [CASE-ID]
          - 6+-digit runs          → ***MASKED***

        Mirrors the FirewallStack's `_sanitize_message` so data flowing to
        LLM prompts carries the same masking the firewall applies at the
        LLM-call boundary. Redundant on clean inputs, defensive on messy
        ones.
        """
        masked = _CASE_ID_RE.sub(_CASE_ID_MASK, text)
        return _DIGIT_RUN_RE.sub(_DIGIT_RUN_MASK, masked)
