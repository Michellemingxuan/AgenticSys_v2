from llm.firewall_stack import sanitize_message, redact_payload
from pydantic import BaseModel

def test_sanitize_message_masks_case_id():
    assert sanitize_message("CASE-12345 review") == "[CASE-ID] review"

def test_sanitize_message_masks_long_digits():
    # 15 digits — a card number. Still masked.
    assert sanitize_message("acct 378282246310005 details") == "acct ***MASKED*** details"


def test_sanitize_message_leaves_a_case_id_intact():
    """A knowledge-base case_id is an 11-12 digit run. Masking it removed the
    one thing a prior-case lookup exists to return — the reviewer got a
    similar case with no way to look it up."""
    assert sanitize_message("see case 402906382014") == "see case 402906382014"
    assert sanitize_message("see case 11854808010") == "see case 11854808010"


def test_the_threshold_sits_between_a_case_id_and_a_card_number():
    """The whole relaxation rests on that gap. If either end moves — longer
    case ids, or a shorter account number — this stops being safe."""
    assert sanitize_message("9" * 12) == "9" * 12          # case id: passes
    assert sanitize_message("9" * 13) == "***MASKED***"    # first masked length
    assert sanitize_message("9" * 15) == "***MASKED***"    # card number

def test_redact_payload_walks_nested_dict():
    payload = {"meta": {"case": "CASE-9999"}, "items": ["acct 378282246310005"]}
    out = redact_payload(payload)
    assert out["meta"]["case"] == "[CASE-ID]"
    assert out["items"][0] == "acct ***MASKED***"

def test_redact_payload_pydantic_roundtrip():
    class M(BaseModel):
        note: str
    out = redact_payload(M(note="CASE-42"))
    assert isinstance(out, M)
    assert out.note == "[CASE-ID]"
