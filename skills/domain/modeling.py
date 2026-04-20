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
            "IMPORTANT — PERFORMANCE: The model_scores table has 266 columns. "
            "ALWAYS use the `columns` parameter of query_table to request ONLY the "
            "columns you need (e.g. columns='trans_month,credit_loss_prob,tot_struct_risk_score,"
            "positive_events'). Never query all columns — it is slow and the LLM context "
            "cannot handle 266 columns per row. First use get_table_schema to see what "
            "columns exist, then select 5-15 relevant ones per query_table call."
        ),
        data_hints=["model_scores"],
        interpretation_guide=(
            "Falling scores over consecutive periods signal deterioration. "
            "Divergence between model score and bureau score may indicate model staleness "
            "or emerging risk not yet reflected in bureau. "
            "Key columns: trans_month (time), credit_loss_prob, tot_struct_risk_score, "
            "cbr_score, positive_events, times_30_dpd, delnqncy_ind_intrnl."
        ),
        risk_signals=[
            "score drop > 50 points in 3 months",
            "model score diverges from bureau score by > 100 points",
            "score in bottom decile",
        ],
    )
