"""Execution-local HTTP attempt observations. No URLs or credentials are retained."""
from contextlib import contextmanager, suppress
from contextvars import ContextVar

_OBSERVER = ContextVar('family_http_observer', default=None)


@contextmanager
def observe_http(observer):
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def http_attempt():
    observer = _OBSERVER.get()
    if observer is not None:
        with suppress(Exception):
            observer()


def request(client, method, *args, **kwargs):
    """Observe immediately before IO, after argument/token validation."""
    http_attempt()
    return getattr(client, method)(*args, **kwargs)
