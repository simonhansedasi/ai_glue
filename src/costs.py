# Cost per 1M tokens in USD: (input_rate, output_rate)
COSTS = {
    # Anthropic
    "claude-opus-4-7":          (15.00, 75.00),
    "claude-sonnet-4-6":        (3.00,  15.00),
    "claude-haiku-4-5":         (0.80,  4.00),
    # OpenAI
    "gpt-4o":                   (2.50,  10.00),
    "gpt-4o-mini":              (0.15,  0.60),
    "gpt-4-turbo":              (10.00, 30.00),
    "gpt-3.5-turbo":            (0.50,  1.50),
    # Groq (open source hosted)
    "llama3-70b-8192":          (0.59,  0.79),
    "mixtral-8x7b-32768":       (0.27,  0.27),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    # Exact match first, then prefix match (handles versioned IDs like claude-haiku-4-5-20251001)
    rates = COSTS.get(model)
    if rates is None:
        for key in COSTS:
            if model.startswith(key):
                rates = COSTS[key]
                break
    if rates is None:
        return 0.0
    inp_rate, out_rate = rates
    return (input_tokens * inp_rate + output_tokens * out_rate) / 1_000_000
