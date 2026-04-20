"""Modeling domain skill — score trajectories and model signals."""

from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="modeling",
        system_prompt=(
            "You are a model-performance and score-trajectory analyst. You interpret "
            "model scores, track score migration over time, and identify customers whose "
            "risk trajectory is deteriorating or improving. Compare model outputs to "
            "bureau data for consistency.\n\n"
            "IMPORTANT — PERFORMANCE: The model_scores table has ~265 columns. "
            "ALWAYS use the `columns` parameter of query_table to request ONLY the "
            "columns you need. ALWAYS include `trans_month` when analyzing time-bounded "
            "questions. Example: columns='trans_month,positive_events,credit_loss_prob'. "
            "Never query all columns at once.\n\n"
            "TIME HANDLING: `trans_month` is a real YYYY-MM-DD date representing the "
            "model scoring run date. Use it to answer questions like 'last 18 months', "
            "'recent 6 months'. For time-bounded questions: retrieve ALL rows with "
            "trans_month included, then count / aggregate rows whose trans_month falls "
            "within the requested window relative to the data cut-off date. Do NOT "
            "conclude 'no data' without inspecting the actual trans_month values first."
        ),
        data_hints=["model_scores"],
        interpretation_guide=(
            "Falling scores over consecutive periods signal deterioration. "
            "Divergence between model score and bureau score may indicate model staleness "
            "or emerging risk not yet reflected in bureau. "
            "Key columns: trans_month (date), credit_loss_prob, tot_struct_risk_score, "
            "cbr_score, positive_events, times_30_dpd, delnqncy_ind_intrnl."
        ),
        risk_signals=[
            "score drop > 50 points in 3 months",
            "model score diverges from bureau score by > 100 points",
            "score in bottom decile",
        ],
    )
