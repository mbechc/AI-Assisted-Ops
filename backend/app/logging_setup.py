"""Log format shared by all containers. Lines carry run/task context so the portal can follow one run."""
import contextlib
import contextvars
import logging
import os

_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("steward_log_ctx", default={})
FORMAT = "%(asctime)s %(levelname)s [%(component)s] %(name)s%(ctx)s: %(message)s"


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        c = _ctx.get()
        record.ctx = "".join(f" {k}={v}" for k, v in c.items())
        record.component = os.environ.get("WORKER_NAME") and f"worker-{os.environ['WORKER_NAME']}" or \
            os.environ.get("STEWARD_COMPONENT", "app")
        return True


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_steward_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.addFilter(_ContextFilter())
    root.handlers = [handler]
    root.setLevel(level)
    root._steward_configured = True  # type: ignore[attr-defined]
    logging.getLogger("httpx").setLevel(logging.WARNING)


@contextlib.contextmanager
def log_context(**values):
    token = _ctx.set({**_ctx.get(), **{k: v for k, v in values.items() if v is not None}})
    try:
        yield
    finally:
        _ctx.reset(token)
