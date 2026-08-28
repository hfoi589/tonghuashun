"""Small fail-closed standard-library HTTP transport for fixed local origins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)


class SafeHttpError(RuntimeError):
    """A fixed transport failure that carries no request or response detail."""

    def __init__(self) -> None:
        super().__init__("SAFE_HTTP_REQUEST_FAILED")


class SafeHttpStatusError(SafeHttpError):
    """An HTTP status failure carrying only its numeric status."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status


class _Response(Protocol):
    status: int

    def read(self, size: int = -1) -> bytes: ...


_OpenCallable = Callable[[Request, float], _Response]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None


def _effective_port(parsed: SplitResult) -> int:
    try:
        explicit = parsed.port
    except ValueError:
        raise SafeHttpError() from None
    if explicit is not None:
        return explicit
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    raise SafeHttpError() from None


def _default_opener() -> OpenerDirector:
    # Supplying an explicit empty ProxyHandler disables environment and macOS
    # system proxy discovery. Redirects are rejected at the first response.
    return build_opener(ProxyHandler({}), _RejectRedirects())


@dataclass(frozen=True)
class SafeHttpResponse:
    status: int
    body: bytes
    headers: object | None = field(default=None, repr=False)


class SafeHttpTransport:
    """Fetch bounded bodies from one exact prevalidated origin."""

    def __init__(
        self,
        base_url: str,
        *,
        max_body_bytes: int,
        opener: _OpenCallable | None = None,
    ) -> None:
        if not isinstance(max_body_bytes, int) or max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        try:
            parsed = urlsplit(base_url)
            port = _effective_port(parsed)
        except SafeHttpError:
            raise ValueError("invalid fixed HTTP origin") from None
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("invalid fixed HTTP origin")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = port
        self._max_body_bytes = max_body_bytes
        if opener is None:
            director = _default_opener()

            def open_request(request: Request, timeout: float) -> _Response:
                return director.open(request, timeout=timeout)  # type: ignore[return-value]

            self._opener = open_request
        else:
            self._opener = opener

    def request(self, request: Request, timeout_seconds: float) -> SafeHttpResponse:
        response: _Response | None = None
        try:
            response = self._opener(request, timeout_seconds)
            status = getattr(response, "status", getattr(response, "code", 200))
            if not isinstance(status, int) or not 100 <= status <= 599:
                raise SafeHttpError()
            self._validate_final_url(response, request.full_url)
            body = self._bounded_read(response)
            return SafeHttpResponse(
                status=status,
                body=body,
                headers=getattr(response, "headers", None),
            )
        except SafeHttpStatusError:
            raise
        except HTTPError as error:
            status = error.code if isinstance(error.code, int) else 500
            raise SafeHttpStatusError(status) from None
        except (SafeHttpError, TimeoutError, URLError, OSError, ValueError):
            raise SafeHttpError() from None
        except Exception:
            raise SafeHttpError() from None
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        raise SafeHttpError() from None

    def _validate_final_url(self, response: _Response, request_url: str) -> None:
        geturl = getattr(response, "geturl", None)
        final_url = geturl() if callable(geturl) else request_url
        if not isinstance(final_url, str):
            raise SafeHttpError()
        try:
            parsed = urlsplit(final_url)
            port = _effective_port(parsed)
        except (SafeHttpError, ValueError):
            raise SafeHttpError() from None
        if (
            parsed.scheme != self._scheme
            or parsed.hostname != self._host
            or port != self._port
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SafeHttpError()

    def _bounded_read(self, response: _Response) -> bytes:
        limit = self._max_body_bytes + 1
        try:
            body = response.read(limit)
        except TypeError:
            # Compatibility for narrow injected test doubles. Real
            # http.client responses honor the size argument above.
            body = response.read()
        if not isinstance(body, bytes) or len(body) > self._max_body_bytes:
            raise SafeHttpError()
        return body
