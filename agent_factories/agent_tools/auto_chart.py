"""Auto-chart renderer: build charts from a specialist's parsed tool-output
series with NO LLM. Scheduled fire-and-forget by agent_tool._runner in
parallel with the distiller. Extracted from tools/agent_tool.py
(see the decomposition design spec)."""
from __future__ import annotations

from pathlib import Path

from agent_factories.agent_tools.series_extract import _ParsedSeries, _parse_series_from_tool_outputs
from tools.viz_renderer import render_chart, _infer_unit


def _table_owners() -> dict[str, str]:
    """``{real table name: specialist that owns it}``, from the skills' own
    ``data_hints``.

    Derived rather than declared twice, so adding a table to a skill is enough.
    (``runner.turn.input_assembly`` derives the same map for the orchestrator's
    variable-routing hint; it is not imported here because ``runner`` imports
    ``agent_factories``, not the other way round.) Imports are local and
    guarded so a chart path can never be what breaks a turn.
    """
    owners: dict[str, str] = {}
    try:
        from skills.domain.loader import list_domain_skills, load_domain_skill
        from tools.data_tools import _resolve_real_table
    except Exception:  # noqa: BLE001
        return owners
    for skill_name in list_domain_skills():
        skill = load_domain_skill(skill_name)
        if not skill:
            continue
        for table in skill.data_hints or []:
            owners.setdefault(table, skill.name)
            try:
                owners.setdefault(_resolve_real_table(table), skill.name)
            except Exception:  # noqa: BLE001
                pass
    return owners


def record_chart_pending(app_ctx, specialist: str, topic: str) -> None:
    """Remember that a `chart_pending` placeholder was emitted for this key.

    `chart_pending` fires per specialist DURING the turn (the auto-chart task
    starts the moment a specialist returns), but the real `chart` events are
    emitted at END of turn from `_collect_turn_charts`, which dedups identical
    figures across specialists. Anything dedup drops therefore leaves a
    placeholder the frontend clears on a matching `chart` event that never
    arrives — it hangs as a second, permanently-loading card next to the real
    one, which is what "two specialists drew the same plot" looks like on
    screen. `_finalize` diffs this set against what it actually emitted and
    retracts the difference. Best-effort: never break a render over bookkeeping.
    """
    try:
        pending = getattr(app_ctx, "_charts_pending", None)
        if pending is None:
            pending = set()
            setattr(app_ctx, "_charts_pending", pending)
        pending.add((specialist, topic))
    except Exception:  # noqa: BLE001
        pass


def _drop_foreign_series(parsed, name: str, app_ctx, logger):
    """Keep only the series this specialist is entitled to CHART.

    Specialists may query any table — the cross-domain peek is deliberate and
    useful in the TEXT of a finding. Charting it is different: the chart is the
    owning specialist's deliverable, and when a peeker renders it too the turn
    shows the same figure twice. Measured live: `bureau` trended
    `credit_loss_prob_max` and `tot_struct_risk_score_max` (CDSS / TSR —
    `modeling`'s metrics) alongside its own FICO series, so the turn emitted 13
    charts and, because cross-specialist dedup is first-writer-wins, the CDSS
    and TSR plots ended up attributed to `bureau` while `modeling` — which
    owns them and analysed them properly — showed none.

    Only suppressed when the OWNER also ran this turn: if `modeling` wasn't on
    the team, `bureau`'s peek is the only source of that chart and dropping it
    would lose the figure entirely. Unknown table, unknown owner, or
    owner-not-dispatched all keep the series — this never removes a chart
    nobody else is going to draw.
    """
    called = getattr(app_ctx, "_domain_specialists_called", None)
    called = called if isinstance(called, set) else set()
    if not called:
        return parsed
    owners = _table_owners()
    if not owners:
        return parsed
    kept, dropped = [], []
    for ps in parsed:
        table = (getattr(ps, "table_name", "") or "").strip()
        owner = owners.get(table) if table else None
        if owner and owner != name and owner in called:
            dropped.append((ps.column_name, table, owner))
            continue
        kept.append(ps)
    if dropped and logger:
        logger.log("auto_chart_foreign_series_dropped", {
            "specialist": name,
            "n_dropped": len(dropped),
            "dropped": [{"column": c, "table": t, "owner": o}
                        for c, t, o in dropped],
        })
    return kept


async def _auto_chart_from_tool_outputs(
    app_ctx, name: str, tool_outputs: str,
) -> int:
    """Render charts from specialist tool outputs — no LLM needed.

    Parses summarize_trend / batch_summarize_trend / summarize_by_group
    results, determines the chart kind from the data shape, injects
    thresholds from the catalog, and renders. Runs as a fire-and-forget
    task in parallel with the distiller.
    """

    logger = getattr(app_ctx, "logger", None)
    kb = getattr(app_ctx, "_specialist_kb", None)
    case_folder = getattr(app_ctx, "case_folder", None)
    turn_id = getattr(app_ctx, "_turn_id", None)
    turn_seq = getattr(app_ctx, "_turn_seq", None)
    catalog = getattr(app_ctx, "_catalog", None)
    emit_event = getattr(app_ctx, "_emit_event", None)

    if kb is None or case_folder is None:
        if logger:
            logger.log("auto_chart_skipped", {
                "specialist": name, "reason": "no_kb_or_case_folder"})
        return 0

    try:
        parsed = _parse_series_from_tool_outputs(tool_outputs)
    except Exception as exc:
        if logger:
            logger.log("auto_chart_parse_failed", {
                "specialist": name, "error": str(exc)[:200]})
        return 0

    if not parsed:
        if logger:
            logger.log("auto_chart_skipped", {
                "specialist": name, "reason": "no_series_parsed",
                "tool_outputs_chars": len(tool_outputs)})
        return 0

    # Drop series belonging to another specialist's tables BEFORE the split, so
    # the log below reflects what is actually charted rather than what was
    # parsed.
    parsed = _drop_foreign_series(parsed, name, app_ctx, logger)
    if not parsed:
        if logger:
            logger.log("auto_chart_skipped", {
                "specialist": name, "reason": "all_series_foreign"})
        return 0

    # Group series by key_field type (period vs group)
    trend_series = [ps for ps in parsed if ps.key_field == "period"]
    group_series = [ps for ps in parsed if ps.key_field in ("group", "merchant")]

    if logger:
        logger.log("auto_chart_series_parsed", {
            "specialist": name,
            "n_trend_series": len(trend_series),
            "n_group_series": len(group_series),
            "column_names": [ps.column_name for ps in parsed],
            "series_sizes": [len(ps.lookup) for ps in parsed],
        })

    charts_rendered = 0
    charts_dir = Path(case_folder) / "charts"

    try:
        charts_rendered = _render_auto_charts(
            trend_series, group_series, name, charts_dir,
            kb, turn_id, catalog, logger, emit_event, turn_seq,
            app_ctx=app_ctx,
        )
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.log("auto_chart_failed", {
                "specialist": name,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
            })

    if charts_rendered and logger:
        logger.log("auto_chart_rendered", {
            "specialist": name,
            "n_charts": charts_rendered,
            "turn_id": turn_id,
        })
    return charts_rendered


def _render_auto_charts(
    trend_series, group_series, name, charts_dir,
    kb, turn_id, catalog, logger, emit_event=None, turn_seq=None,
    app_ctx=None,
) -> int:

    def _emit_pending(topic: str, kind: str) -> None:
        # Fire `chart_pending` BEFORE the render so the frontend shows a
        # "working on the plot…" placeholder during the gap until the
        # end-of-turn `chart` event lands. Keyed by (specialist, topic) to
        # match that eventual event so the placeholder clears cleanly. The
        # auto-chart task runs concurrently (right after the specialist
        # finishes, not at end-of-turn), so this fires early enough to be
        # useful. Mirrors make_chart's pending emit (tools/data_viz_tools.py).
        if callable(emit_event):
            try:
                emit_event("chart_pending", {
                    "specialist": name, "topic": topic, "kind": kind,
                })
                record_chart_pending(app_ctx, name, topic)
            except Exception:  # noqa: BLE001 - emit must never break rendering
                pass

    charts_rendered = 0

    # ── Trend charts ──
    # Dedup: drop series that are identical (same column + same data).
    # This catches the case where the specialist calls summarize_trend
    # twice on the same table/column, producing two copies of the same
    # data that would otherwise render as a misleading dual-axis chart.
    deduped_trend: list[_ParsedSeries] = []
    seen_signatures: set[tuple] = set()
    for ps in trend_series:
        sig = (ps.column_name, ps.table_name,
               tuple(sorted(ps.lookup.items())))
        if sig not in seen_signatures:
            seen_signatures.add(sig)
            deduped_trend.append(ps)
    trend_series = deduped_trend

    # Disambiguate column names so chart legends are clear.
    # 1. Generic names ("Amount") get prefixed with table context
    #    when multiple trend series are present.
    # 2. Same column_name from different tables gets table-prefixed.
    # Preserve original column names before disambiguation (for
    # threshold lookup against catalog keys).
    orig_col_names: dict[int, str] = {
        id(ps): ps.column_name for ps in trend_series
    }
    _GENERIC_COLS = {"amount", "value", "balance", "total", "count"}
    _TABLE_LABEL = {"spends": "Spend", "payments": "Payment",
                    "spends_data": "Spend"}
    if len(trend_series) > 1:
        for ps in trend_series:
            if (ps.column_name
                    and ps.column_name.lower() in _GENERIC_COLS
                    and ps.table_name):
                prefix = _TABLE_LABEL.get(ps.table_name, ps.table_name)
                ps.column_name = f"{prefix} {ps.column_name}"
    col_counts: dict[str, int] = {}
    for ps in trend_series:
        c = ps.column_name or ""
        col_counts[c] = col_counts.get(c, 0) + 1
    for ps in trend_series:
        if ps.column_name and col_counts.get(ps.column_name, 0) > 1:
            if ps.table_name:
                ps.column_name = f"{ps.table_name} {ps.column_name}"

    # Build chart groups:
    #   1 series → single "trend" chart
    #   2 series, same unit + similar range → "trend" (shared y-axis)
    #   2 series, different units or ranges → "trend_dual" (dual y-axes)
    #   3+ series → individual "trend" charts (separate tabs in the UI)

    def _series_range(ps: _ParsedSeries) -> tuple[float, float] | None:
        vals = []
        for v in ps.lookup.values():
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        return (min(vals), max(vals)) if vals else None

    def _ranges_compatible(r0, r1) -> bool:
        """True when two value ranges are close enough to share one y-axis."""
        if r0 is None or r1 is None:
            return False
        all_max = max(r0[1], r1[1])
        all_min = min(r0[0], r1[0])
        span = all_max - all_min
        if span == 0:
            return True
        # Each series should cover at least 20% of the combined span,
        # otherwise the smaller one gets squashed flat.
        s0 = r0[1] - r0[0]
        s1 = r1[1] - r1[0]
        return min(s0, s1) / span >= 0.2

    if trend_series:
        if len(trend_series) == 2:
            u0 = _infer_unit(orig_col_names.get(id(trend_series[0]),
                             trend_series[0].column_name))
            u1 = _infer_unit(orig_col_names.get(id(trend_series[1]),
                             trend_series[1].column_name))
            same_unit = u0 and u1 and u0 == u1
            similar_range = _ranges_compatible(
                _series_range(trend_series[0]),
                _series_range(trend_series[1]),
            )
            if same_unit and similar_range:
                chart_groups = [("trend", trend_series)]
            else:
                chart_groups = [("trend_dual", trend_series)]
        elif len(trend_series) >= 3:
            chart_groups = [("trend", [ps]) for ps in trend_series]
        else:
            chart_groups = [("trend", trend_series)]

        for kind, group in chart_groups:
            y_fields = []
            for i, ps in enumerate(group):
                y_fields.append(ps.column_name or (
                    "value" if len(group) == 1 else f"series_{i}"))

            if len(group) == 1 and group[0].column_name:
                topic = f"{name}_{group[0].column_name}_trend"
            elif len(group) == 2:
                seen_cols: list[str] = []
                for ps in group:
                    if ps.column_name and ps.column_name not in seen_cols:
                        seen_cols.append(ps.column_name)
                cols = "_and_".join(seen_cols)
                topic = f"{cols}_trajectory"
            else:
                topic = f"{name}_trend"

            # Build points array
            all_periods: list = []
            seen: set = set()
            for ps in group:
                for k in ps.lookup:
                    if k not in seen:
                        all_periods.append(k)
                        seen.add(k)

            points = []
            for period in all_periods:
                row: dict = {"period": period}
                for i, ps in enumerate(group):
                    row[y_fields[i]] = ps.lookup.get(period)
                points.append(row)

            if len(points) < 4:
                continue

            # Inject thresholds from catalog. The y_field may have been
            # prefixed (e.g. "Spend Amount") so also try the original
            # column_name from the parsed series and catalog aliases.
            if catalog and hasattr(catalog, "get_thresholds"):
                # Build a map: y_field → original column_name for fallback
                yf_to_orig: dict[str, str] = {}
                for i, ps in enumerate(group):
                    yf_to_orig[y_fields[i]] = orig_col_names.get(
                        id(ps), ps.column_name or "")

                for cat_table in catalog.list_tables():
                    thresholds = catalog.get_thresholds(cat_table)
                    if not thresholds:
                        continue
                    aliases = catalog.column_aliases(cat_table)
                    for yf in y_fields:
                        th_key = (f"threshold_{yf}"
                                  if len(y_fields) > 1 else "threshold")
                        if any(p.get(th_key) is not None for p in points):
                            continue
                        # Try: exact match, original column_name, alias
                        match_val = None
                        for candidate in (yf, yf_to_orig.get(yf, "")):
                            if candidate in thresholds:
                                match_val = thresholds[candidate]["value"]
                                break
                            # Check if candidate is an alias
                            for canon, alias_list in aliases.items():
                                if (candidate in alias_list
                                        and canon in thresholds):
                                    match_val = thresholds[canon]["value"]
                                    break
                            if match_val is not None:
                                break
                        if match_val is not None:
                            for p in points:
                                p[th_key] = match_val

            # Build informative claim + source_call from data
            first_period = points[0]["period"] if points else "?"
            last_period = points[-1]["period"] if points else "?"
            claim_parts = []
            source_parts = []
            for yf in y_fields:
                vals = [p.get(yf) for p in points if p.get(yf) is not None]
                if vals:
                    first_v = vals[0]
                    last_v = vals[-1]
                    peak_v = max(vals)
                    trough_v = min(vals)
                    claim_parts.append(
                        f"{yf}: {first_v} → {last_v} "
                        f"(peak {peak_v}, trough {trough_v})"
                    )
                source_parts.append(
                    f"summarize_trend('{yf}', period='month')"
                )
            claim = (
                f"{first_period} to {last_period}: "
                + "; ".join(claim_parts)
            ) if claim_parts else f"{name} trend over {len(points)} periods"

            kp_dict = {
                "topic": topic,
                "claim": claim,
                "numbers": points,
                "viz": {"kind": kind, "x_field": "period", "y_fields": y_fields},
                "source_call": ", ".join(source_parts),
                "captured_at_turn": turn_id,
                "captured_at_seq": turn_seq,
                "confidence": "high",
            }
            # vega_spec is regenerated at emit time (finalize._build_chart_payload),
            # not stored on the KP — keeps the KB / distilled memory lean.
            _emit_pending(topic, kind)
            img_path = render_chart(kp_dict, charts_dir, turn_id=turn_id, logger=logger)
            if img_path:
                kp_dict["image_path"] = img_path
                kb.setdefault(name, []).append(kp_dict)
                charts_rendered += 1

    # ── Group/bar charts ──
    for ps in group_series:
        if len(ps.lookup) < 4:
            continue

        def _to_num(v):
            if isinstance(v, (int, float)):
                return v
            try:
                s = str(v).replace(",", "").replace("$", "").strip()
                return float(s) if s else 0
            except (ValueError, TypeError):
                return 0

        points = [
            {ps.key_field: k, "value": _to_num(v)}
            for k, v in ps.lookup.items()
        ]
        topic_slug = ps.column_name or f"{name}_breakdown"
        # Build informative claim from data
        sorted_items = sorted(points, key=lambda x: x["value"], reverse=True)
        top_item = sorted_items[0] if sorted_items else {}
        top_label = top_item.get(ps.key_field, "?")
        top_val = top_item.get("value", 0)
        total = sum(p["value"] for p in points)
        top_share = (top_val / total * 100) if total else 0
        bar_claim = (
            f"Top {ps.key_field}: {top_label} ({top_val:,.0f}, "
            f"{top_share:.0f}% of total) across {len(points)} groups"
        )
        # Use horizontal bars (share) for readable long category names
        kp_dict = {
            "topic": topic_slug,
            "claim": bar_claim,
            "numbers": points,
            "viz": {"kind": "share", "x_field": ps.key_field, "y_fields": ["value"]},
            "source_call": f"summarize_by_group('{ps.table_name}', '{ps.key_field}', '{ps.column_name}', op='sum')",
            "captured_at_turn": turn_id,
            "captured_at_seq": turn_seq,
            "confidence": "high",
        }
        # vega_spec is regenerated at emit time (finalize._build_chart_payload),
        # not stored on the KP — keeps the KB / distilled memory lean.
        _emit_pending(topic_slug, "share")
        img_path = render_chart(kp_dict, charts_dir, turn_id=turn_id, logger=logger)
        if img_path:
            kp_dict["image_path"] = img_path
            kb.setdefault(name, []).append(kp_dict)
            charts_rendered += 1

    return charts_rendered
