"""Data Manager Agent — data-side governance.

Fronts the `SimulatedDataGateway` + `DataCatalog` for everything downstream
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

import re
from pathlib import Path

from datalayer import adapter
from datalayer.catalog import DataCatalog
from datalayer.gateway import SimulatedDataGateway
from llm.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from skills.loader import load_skill as _load_skill
from tools.data_tools import query_table


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


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
        gateway: SimulatedDataGateway,
        catalog: DataCatalog,
        llm: FirewalledModel,
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
            },
        )
        return diff

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
