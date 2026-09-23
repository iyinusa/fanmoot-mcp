"""Smoke-test a deployed server over its public URL (`S7-9`, `S7-10`).

    make smoke                                  # https://mcp.fanmoot.com
    make smoke URL=http://127.0.0.1:8080        # a local `make dev`

Everything a client meets before it has an account, checked from the outside
with nothing but HTTP. No MCP SDK, so a pass here is also the "raw
Streamable HTTP session" row of the client matrix: if this script can hold a
session, so can ``curl``.

1. TLS, and the headers a public origin owes its callers (HSTS, nosniff).
2. ``/healthz``: up, with no degraded dependency and grant storage enabled.
3. All three discovery documents, agreeing with each other and with the URL.
4. A Streamable HTTP session: ``initialize`` negotiates exactly ``2025-11-25``,
   ``tools/list`` returns the whole surface, and a read tool answers.
5. An authenticated tool without a token is a 401 carrying an RFC 9728
   challenge. That is how every client finds the authorization server.
6. A browser ``Origin`` nobody allowed is refused with a 403.
7. ``/privacy`` and ``/terms`` are served, and the consent pages cannot be framed.

Nothing is created: no client is registered and no fan is signed in. Every
check runs, and the exit status is 1 if any of them failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

PROTOCOL = "2025-11-25"
EXPECTED_TOOLS = 19
MCP_ACCEPT = "application/json, text/event-stream"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'' if ok or not detail else f' — {detail}'}")
    return ok


def rpc_body(response: httpx.Response) -> dict[str, Any]:
    """One JSON-RPC message out of either a JSON body or an SSE stream."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            data = line[5:].strip() if line.startswith("data:") else ""
            # An event with no data is the resumability primer (SEP-1699): an
            # id for `Last-Event-ID`, and nothing to parse.
            if not data:
                continue
            message: dict[str, Any] = json.loads(data)
            if "id" in message:
                return message
        return {}
    return dict(response.json())


def run(base: str) -> int:
    http = httpx.Client(base_url=base, timeout=15.0, follow_redirects=False)
    https = base.startswith("https://")

    print(f"\n{base}\n")
    print("transport")
    health = http.get("/healthz")
    if https:
        check(True, "TLS handshake")
        check(
            "max-age=" in health.headers.get("strict-transport-security", ""),
            "HSTS on every response",
            health.headers.get("strict-transport-security", "absent"),
        )
    check(health.headers.get("x-content-type-options") == "nosniff", "nosniff")

    print("health")
    body = health.json() if health.status_code == 200 else {}
    check(health.status_code == 200, "/healthz answers 200", str(health.status_code))
    check(body.get("status") == "ok", "no degraded dependency", json.dumps(body.get("checks")))
    check(
        body.get("checks", {}).get("grant_storage") == "ok",
        "grant storage enabled (FMCP_SECRET_ENCRYPTION_KEY set)",
    )

    print("discovery")
    resource = f"{base}/mcp"
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        prm = http.get(path)
        doc = prm.json() if prm.status_code == 200 else {}
        check(
            doc.get("resource") == resource
            and (doc.get("authorization_servers") or [None])[0] == base,
            f"{path} names {resource}, authorized by {base}",
            json.dumps(doc)[:200],
        )
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        response = http.get(path)
        doc = response.json() if response.status_code == 200 else {}
        check(
            doc.get("issuer") == base
            and doc.get("code_challenge_methods_supported") == ["S256"]
            and bool(doc.get("registration_endpoint")),
            f"{path}: issuer, S256 only, DCR advertised",
            json.dumps(doc)[:200],
        )

    print("streamable http")
    headers = {"Accept": MCP_ACCEPT, "Content-Type": "application/json"}
    init = http.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "fanmoot-mcp-smoke", "version": "1"},
            },
        },
    )
    result = rpc_body(init).get("result", {}) if init.status_code == 200 else {}
    session_id = init.headers.get("mcp-session-id", "")
    check(
        result.get("protocolVersion") == PROTOCOL,
        f"initialize negotiates {PROTOCOL}",
        f"{init.status_code} {result.get('protocolVersion')}",
    )
    check(bool(session_id), "a session id is issued")

    if session_id:
        headers = {**headers, "Mcp-Session-Id": session_id, "Mcp-Protocol-Version": PROTOCOL}
        http.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        listing = rpc_body(
            http.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
        )
        tools = listing.get("result", {}).get("tools", [])
        check(
            len(tools) == EXPECTED_TOOLS,
            f"tools/list has all {EXPECTED_TOOLS} tools",
            str(len(tools)),
        )

        called = rpc_body(
            http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "upcoming_matches", "arguments": {}},
                },
            )
        ).get("result", {})
        check(
            called.get("isError") is False and bool(called.get("structuredContent")),
            "a read tool answers with no account",
            json.dumps(called)[:200],
        )

        refused = http.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "post_moot", "arguments": {"body": "smoke test"}},
            },
        )
        challenge = refused.headers.get("www-authenticate", "")
        check(
            refused.status_code == 401 and "resource_metadata=" in challenge,
            "post_moot without a token is a 401 naming the resource metadata",
            f"{refused.status_code} {challenge}",
        )
        http.delete("/mcp", headers=headers)

    evil = http.post(
        "/mcp",
        headers={
            "Accept": MCP_ACCEPT,
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
        },
        json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
    )
    check(evil.status_code == 403, "an unlisted browser Origin is refused", str(evil.status_code))

    print("pages")
    for path in ("/privacy", "/terms"):
        page = http.get(path)
        check(page.status_code == 200, f"{path} is served", str(page.status_code))
    signin = http.get("/privacy")
    check(
        "frame-ancestors 'none'" in signin.headers.get("content-security-policy", ""),
        "HTML cannot be framed",
    )

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "url", nargs="?", default="https://mcp.fanmoot.com", help="the server's origin"
    )
    args = parser.parse_args()
    return run(args.url.rstrip("/").removesuffix("/mcp"))


if __name__ == "__main__":
    sys.exit(main())
