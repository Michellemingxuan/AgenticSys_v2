from tools.node_trace.pricing import compute_cost


def test_known_model_cost_basic():
    # gpt-4o-mini: input $0.15/M, output $0.60/M
    # 1000 prompt + 500 completion = 0.00015 + 0.00030 = 0.00045
    cost = compute_cost(
        model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500,
    )
    assert abs(cost - 0.00045) < 1e-9


def test_cached_tokens_discount():
    # gpt-4o-mini cached input: $0.075/M (half of fresh)
    # 1000 prompt, 400 cached -> 600 fresh * 0.15 + 400 cached * 0.075
    cost = compute_cost(
        model="gpt-4o-mini",
        prompt_tokens=1000,
        cached_input_tokens=400,
        completion_tokens=0,
    )
    expected = 600 * 0.15 / 1_000_000 + 400 * 0.075 / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_unknown_model_returns_zero_no_raise():
    assert compute_cost(model="some-weird-model", prompt_tokens=1, completion_tokens=1) == 0.0


def test_none_model_returns_zero():
    assert compute_cost(model=None, prompt_tokens=1000) == 0.0
