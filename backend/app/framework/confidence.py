LLM_RAISE_MARGIN = 5
LLM_LOWER_WEIGHT = 0.6


def combine(rule: int, llm: int | None) -> int:
    """The LLM may lower confidence substantially but only raise it by a small margin."""
    if llm is None:
        return int(rule)
    if llm >= rule:
        return int(min(llm, rule + LLM_RAISE_MARGIN, 100))
    return int(round(rule - LLM_LOWER_WEIGHT * (rule - llm)))
