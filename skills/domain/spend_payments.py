"""Spend & Payments domain skill — payment trends, delinquency, spend spikes."""

from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="spend_payments",
        system_prompt=(
            "You are a spend and payments analyst. You examine monthly transaction volumes, "
            "payment patterns, delinquency history, and spend spikes. Identify customers "
            "showing early delinquency signals or unusual spending behaviour.\n\n"
            "DATA NOTE: Three tables are available:\n"
            "  - txn_monthly: monthly transaction aggregates. Columns [month (YYYY-MM-DD), "
            "spend_total, txn_count, category].\n"
            "  - spends: transaction-level spend data. Columns [spend_date (YYYY-MM-DD), amount, "
            "merchant_name, merchant_industry, merchant_risk_score, spend_concentration, "
            "rnn_spend_score, spend_divergence_index, customer_industry].\n"
            "  - payments: payment records with success/return status. Columns [card_number, "
            "payment_date, payment_amount, payment_bank_account, return_flag, return_reason].\n\n"
            "TIME WINDOWS: Interpret 'recent' relative to the data cut-off date (see pillar config). "
            "When the question references a window like 'last 3 months', filter rows where "
            "spend_date / payment_date is within that window before the cut-off. "
            "If you cannot find data for the requested window, query ALL data first to see "
            "the actual date range available before concluding data is missing."
        ),
        data_hints=["txn_monthly", "spends", "payments"],
        interpretation_guide=(
            "Rising spend with declining/returned payments is a classic early-warning pattern. "
            "Look for minimum-payment-only behaviour, sudden spend spikes, and payment returns. "
            "Filter rows by spend_date / payment_date to answer time-scoped questions."
        ),
        risk_signals=[
            "payment < minimum due for 2+ months",
            "spend spike > 3x average",
            "declining payment ratio trend",
            "days-past-due increasing",
        ],
    )
