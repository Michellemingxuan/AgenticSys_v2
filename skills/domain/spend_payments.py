"""Spend & Payments domain skill — payment trends, delinquency, spend spikes."""

from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="spend_payments",
        system_prompt=(
            "You are a spend and payments analyst. You examine monthly transaction volumes, "
            "payment patterns, delinquency history, and spend spikes. Identify customers "
            "showing early delinquency signals or unusual spending behaviour.\n\n"
            "DATA NOTE: Two tables are available:\n"
            "  - txn_monthly: columns [month (date), spend_total, txn_count, category]. "
            "The `month` column is a date like '2024-09-19' — use it to filter recent months.\n"
            "  - pmts_detail: columns [date, month, amount, merchant_name, merchant_industry, "
            "merchant_risk_score, spend_concentration, rnn_spend_score, spend_divergence_index, "
            "customer_industry]. The `date` column is YYYY-MM-DD; `month` is a month name label. "
            "Use these to identify recent transactions and payment patterns.\n\n"
            "If you cannot find data for a specific time window, query ALL data first to see "
            "what time range IS available before concluding data is missing. Never say 'no data' "
            "without first inspecting the actual rows."
        ),
        data_hints=["txn_monthly", "pmts_detail"],
        interpretation_guide=(
            "Rising spend with declining payments is a classic early-warning pattern. "
            "Look for minimum-payment-only behaviour and sudden spend spikes that may "
            "indicate financial stress or fraud. "
            "When the question references a recent time window (e.g. 'last 3 months'), "
            "filter rows by the `date` or `month` column — first query without a filter "
            "to see the actual date range available in the data."
        ),
        risk_signals=[
            "payment < minimum due for 2+ months",
            "spend spike > 3x average",
            "declining payment ratio trend",
            "days-past-due increasing",
        ],
    )
