from llm.firewall_stack import sanitize_message, redact_payload
from pydantic import BaseModel

def test_sanitize_message_masks_case_id():
    assert sanitize_message("CASE-12345 review") == "[CASE-ID] review"

def test_sanitize_message_masks_long_digits():
    assert sanitize_message("acct 1234567890 details") == "acct ***MASKED*** details"

def test_redact_payload_walks_nested_dict():
    payload = {"meta": {"case": "CASE-9999"}, "items": ["acct 1234567"]}
    out = redact_payload(payload)
    assert out["meta"]["case"] == "[CASE-ID]"
    assert out["items"][0] == "acct ***MASKED***"

def test_redact_payload_pydantic_roundtrip():
    class M(BaseModel):
        note: str
    out = redact_payload(M(note="CASE-42"))
    assert isinstance(out, M)
    assert out.note == "[CASE-ID]"
