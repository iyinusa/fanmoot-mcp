"""One HTTP client for the whole process.

A single ``httpx.AsyncClient``, created in the lifespan and never per request.
That is not tidiness: it is the connection pool. A voice turn has a budget of a
few hundred milliseconds (HAD-6) and a fresh TLS handshake to ``fanmoot.com``
spends most of it. Keep-alive means the second tool call in a turn pays for
neither DNS nor TLS.

Failures are translated at this boundary, not at the tool. Every error below
carries a sentence a model can read aloud, because per ``2025-11-25`` an input
problem comes back as a tool *execution* error the model can correct itself
from, not a protocol error (HAD-7, S2-6). The mapping from an HTTP status to
one of these types is the whole reason this module exists rather than callers
holding raw responses.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.logging_config import get_logger
from app.settings import get_settings

log = get_logger("fanmoot.client")

#: Split, not a single number. Connecting is a network round trip that either
#: happens quickly or is not going to; reading is Fanmoot doing work. Two
#: seconds to connect fails fast when the host is unreachable, four to read
#: leaves room for a cold query without blowing the turn.
_CONNECT_TIMEOUT_S = 2.0
_READ_TIMEOUT_S = 4.0

#: Retried once, and only these. A 4xx is an answer — retrying a 404 produces a
#: second 404 and spends half the latency budget doing it. A connect error or a
#: 5xx is the other thing: a proxy restart, a rolling deploy, one bad worker,
#: all of which a single immediate retry usually clears.
_RETRY_STATUSES = frozenset({500, 502, 503, 504})


class FanmootError(Exception):
    """Base for everything this client raises.

    ``message`` is written to be spoken. A caller may hand it to a model
    unedited, so it never contains a URL, a status code or a stack.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FanmootUnavailable(FanmootError):
    """Fanmoot did not answer, or answered with a 5xx after a retry."""


class FanmootNotFound(FanmootError):
    """The thing asked for is not there — a 404."""


class FanmootUnauthorized(FanmootError):
    """A 401 or 403. The fan's grant has expired, been revoked, or never
    covered this."""


class FanmootRateLimited(FanmootError):
    """A 429. ``retry_after`` is seconds, when Fanmoot said."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class FanmootRejected(FanmootError):
    """A 4xx that is not one of the above — most often a 422.

    Fanmoot's own message is carried through verbatim where it has one, because
    it is written for a fan and is very often the most useful sentence
    available: "That handle is taken", "A moot cannot be empty".
    """


class Reply:
    """One upstream response, already classified.

    ``not_modified`` is the case worth naming: a conditional request that came
    back ``304`` has **no body**, and the caller is expected to keep whatever it
    already parsed rather than ask again (HAD-6, S1-3).
    """

    __slots__ = ("data", "etag", "not_modified")

    def __init__(
        self, *, data: Any = None, etag: str | None = None, not_modified: bool = False
    ) -> None:
        self.data = data
        self.etag = etag
        self.not_modified = not_modified


class FanmootClient:
    """Typed access to ``fanmoot.com/v1``.

    Construct once per process. ``endpoints.py`` holds the per-endpoint
    wrappers; this class knows only about HTTP.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # `transport` is how the suite replays recorded fixtures (HAD-14). It
        # is the only seam: everything else about this client — the timeouts,
        # the retry, the error mapping — is the code that runs in production,
        # so a test exercises the real policy against a real recorded body.
        self._base_url = base_url.rstrip("/")
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT_S,
            read=_READ_TIMEOUT_S,
            # A write is a small JSON body and the pool is warm, so neither
            # needs its own budget; they inherit the overall ceiling.
            write=_READ_TIMEOUT_S,
            pool=timeout_s if timeout_s is not None else _READ_TIMEOUT_S,
        )
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
            headers={
                # Identifies this server in Fanmoot's own logs. When a tool
                # misbehaves, the operator of the *other* service can see which
                # integration did it without guessing.
                "User-Agent": "fanmoot-mcp (+https://mcp.fanmoot.com)",
                "Accept": "application/json",
            },
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        token: str | None = None,
        etag: str | None = None,
    ) -> Reply:
        """A GET, optionally conditional and optionally as a signed-in fan."""
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        response = await self._send(
            "GET", path, params=_clean(params), headers=headers, token=token
        )
        if response.status_code == httpx.codes.NOT_MODIFIED:
            return Reply(etag=response.headers.get("etag") or etag, not_modified=True)
        return Reply(data=_decode(response), etag=response.headers.get("etag"))

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> Reply:
        """A POST. Never retried — see ``_send``."""
        response = await self._send("POST", path, params=_clean(params), json=json, token=token)
        return Reply(data=_decode(response))

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if token:
            request_headers["Authorization"] = f"Bearer {token}"

        # The retry is for GETs only. Every write in §5 is one a fan hears the
        # result of, and `post_moot` guards its own duplicates with an
        # idempotency key derived from the MCP request id — a blind retry here
        # would sit underneath that and could post twice on a 502 that
        # Fanmoot had in fact accepted.
        attempts = 2 if method == "GET" else 1

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self._http.request(
                    method, path, params=params, json=json, headers=request_headers
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    log.info("fanmoot_retry", path=path, reason=type(exc).__name__)
                    continue
                log.warning("fanmoot_unreachable", path=path, error=str(exc))
                raise FanmootUnavailable(
                    "I can't reach Fanmoot at the moment. Try again in a few seconds."
                ) from exc

            if response.status_code in _RETRY_STATUSES and attempt + 1 < attempts:
                log.info("fanmoot_retry", path=path, status=response.status_code)
                continue

            _raise_for_status(response)
            return response

        # Only reachable when the last attempt was itself a retryable status,
        # since a connect failure on the last attempt raises above.
        raise FanmootUnavailable(
            "Fanmoot is having trouble right now. Try again in a few seconds."
        ) from last_error


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop ``None`` values.

    Every wrapper in ``endpoints.py`` declares its full query signature with
    optional arguments defaulting to ``None``; without this, an unset filter
    would be sent as the literal string "None" and Fanmoot would reject it.
    """
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _decode(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        # A 200 that is not JSON means something in front of Fanmoot answered
        # instead of Fanmoot — a proxy error page, a captive portal, a
        # maintenance splash. Reported as unavailable, because that is what it
        # is, rather than as a parse bug in this repo.
        raise FanmootUnavailable("Fanmoot sent something I couldn't read.") from exc


def _detail(response: httpx.Response) -> str:
    """Fanmoot's own message for this failure, if it sent one.

    Worth reaching for: its API writes errors for fans — "A moot cannot be
    empty", "That handle is taken" — and those sentences are better than
    anything this module could invent.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return ""


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return

    detail = _detail(response)

    if status == httpx.codes.NOT_FOUND:
        raise FanmootNotFound(detail or "I couldn't find that on Fanmoot.")
    if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        raise FanmootUnauthorized(
            detail or "That needs you to be signed in to Fanmoot, and the sign-in didn't hold."
        )
    if status == httpx.codes.TOO_MANY_REQUESTS:
        retry_after = _retry_after(response)
        wait = f" Try again in {retry_after} seconds." if retry_after else " Try again shortly."
        raise FanmootRateLimited(
            (detail or "Fanmoot is asking me to slow down.") + wait, retry_after=retry_after
        )
    if status < 500:
        raise FanmootRejected(detail or "Fanmoot wouldn't accept that.")

    log.warning("fanmoot_server_error", status=status, path=str(response.request.url.path))
    raise FanmootUnavailable("Fanmoot is having trouble right now. Try again in a few seconds.")


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


_client: FanmootClient | None = None


def get_client() -> FanmootClient:
    """The process-wide client, built on first use.

    Lazily, for the same reason the database engine is: tests configure the
    environment before importing the app, and a module-level client would open
    a pool inside every process that merely imports this module.
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = FanmootClient(
            base_url=settings.fanmoot_api_base_url, timeout_s=settings.fanmoot_timeout_s
        )
    return _client


def set_client(client: FanmootClient | None) -> None:
    """Install a client — the lifespan's hook, and the tests'."""
    global _client
    _client = client


async def close_client() -> None:
    """Close the pool on shutdown so the process exits without dangling sockets."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
