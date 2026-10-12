"""Status remains coarse; machine stop codes provide a truthful projection."""


def stop_category(code: str) -> str:
    if code in {"no_progress_breaker", "no_effective_progress_breaker"}:
        return "no_progress"
    if code == "cost_budget_exhausted":
        return "cost_exhausted"
    if code == "verification_unavailable":
        return "verification_unavailable"
    if code in {"max_iterations", "iteration_limit"}:
        return "execution_limit"
    if code in {"user_pause", "user_interrupt", "host_interrupted"}:
        return "interrupted"
    return "unknown" if code else "none"
