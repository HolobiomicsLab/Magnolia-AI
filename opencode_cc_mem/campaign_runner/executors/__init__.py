from .contracts import ExecContext, LimitInfo, RunOutcome, SessionIdentity, UsageRecord

__all__ = [
    "ExecContext",
    "ExecutorRefused",
    "LimitInfo",
    "RunOutcome",
    "SessionIdentity",
    "UsageRecord",
    "executor_for",
]


def __getattr__(name):
    if name in {"ExecutorRefused", "executor_for"}:
        from .registry import ExecutorRefused, executor_for

        return {"ExecutorRefused": ExecutorRefused, "executor_for": executor_for}[name]
    raise AttributeError(name)
