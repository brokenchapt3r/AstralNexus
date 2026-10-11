"""One scoped operation identity for recovery phases and idempotent charging."""
from functools import wraps


def recovery_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        runtime = kwargs.get("orchestrator") or (args[0] if args else None)
        recorder = getattr(runtime, "trace_recorder", None)
        scope = getattr(recorder, "recovery_scope", None)
        if scope is None:
            return function(*args, **kwargs)
        with scope():
            return function(*args, **kwargs)
    return wrapped
