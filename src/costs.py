# Cost per 1M tokens in USD: (input_rate, output_rate[, cache_read_rate]).
# Anthropic rates: first-party API prices from the claude-api reference, cached 2026-10-06 (re-check when models change).
# Cache reads default to 0.1x the input rate; the models whose cache-read price differs are listed explicitly.
# Cache writes are billed at 1.25x the input rate (5-minute cache, the default).
COSTS = {
    # Anthropic
    "claude-fable-5-1":         (10.00, 50.00, 0.25),
    "claude-mythos-5-1":        (10.00, 50.00),
    "claude-fable-5":           (10.00, 50.00),
    "claude-opus-5-5":          (4.00,  20.00, 0.20),
    "claude-opus-5":            (5.00,  25.00),
    "claude-opus-4-8":          (5.00,  25.00),
    "claude-opus-4-7":          (5.00,  25.00),
    "claude-opus-4-6":          (5.00,  25.00),
    "claude-sonnet-5-5":        (2.00,  10.00, 0.20),
    "claude-sonnet-5":          (2.00,  10.00),
    "claude-sonnet-4-6":        (3.00,  15.00),
    "claude-haiku-5-5":         (0.10,  0.50),   # prompts up to 100K tokens; $0.50 / $2.50 beyond (not modelled)
    "claude-haiku-4-5":         (1.00,  5.00),
    # OpenAI
    "gpt-4o":                   (2.50,  10.00),
    "gpt-4o-mini":              (0.15,  0.60),
    "gpt-4-turbo":              (10.00, 30.00),
    "gpt-3.5-turbo":            (0.50,  1.50),
    # Groq (open source hosted)
    "llama3-70b-8192":          (0.59,  0.79),
    "mixtral-8x7b-32768":       (0.27,  0.27),
}

CACHE_WRITE_MULTIPLIER = 1.25
DEFAULT_CACHE_READ_MULTIPLIER = 0.1


def _rates(model: str):
    """Exact match first, then the LONGEST key the model starts with (so 'claude-sonnet-5-5-<date>' never lands on
    'claude-sonnet-5'); handles versioned IDs like claude-haiku-4-5-20251001."""
    if model in COSTS:
        return COSTS[model]
    keys = [k for k in COSTS if model.startswith(k)]
    return COSTS[max(keys, key=len)] if keys else None


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    """USD estimate. input_tokens is the UNCACHED input only (what Anthropic reports as `input_tokens`);
    cache reads and writes are billed separately and passed in separately."""
    rates = _rates(model)
    if rates is None:
        return 0.0
    inp_rate, out_rate = rates[0], rates[1]
    read_rate = rates[2] if len(rates) > 2 else inp_rate * DEFAULT_CACHE_READ_MULTIPLIER
    return (input_tokens * inp_rate + output_tokens * out_rate
            + cache_read_tokens * read_rate + cache_write_tokens * inp_rate * CACHE_WRITE_MULTIPLIER) / 1_000_000
