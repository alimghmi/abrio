from contextvars import ContextVar, Token
from uuid import UUID, uuid4

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str | None]:
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def normalize_request_id(value: str | None) -> str:
    if value:
        try:
            return str(UUID(value.strip()))
        except (TypeError, ValueError):
            pass

    return str(uuid4())
