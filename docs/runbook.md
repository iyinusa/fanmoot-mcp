# fanmoot-mcp runbook

For whoever operates `https://mcp.fanmoot.com`. Each section is a task you do,
in the order you do it. Everything named here is in this repository. Nothing
needs to be remembered.

| Task | Section |
|---|---|
| First deploy | [Deploy](#deploy) |
| Something broke after a deploy | [Roll back](#roll-back) |
| Rotate the encryption key | [Rotate the encryption key](#rotate-the-encryption-key) |
| Alexa can't link accounts | [Re-register Alexa's clients](#re-register-alexas-clients) |
| `make verify-live` went red | [When Fanmoot's API changes shape](#when-fanmoots-api-changes-shape) |
| The server feels slow | [The latency budget](#the-latency-budget) |
| Publish to the MCP registry | [Publishing to the MCP registry](#publishing-to-the-mcp-registry) |

---

## What is running

- **One container, one process, one worker.** An MCP session lives in the
  memory of the process that opened it. A second worker or a second replica
  would get requests carrying a session id it has never seen, and would answer
  them with 404s, intermittently and only under load. Keep **replicas at 1** in
  Dokploy. `make latency` shows one worker clears the budget.
- **Traefik in front**, from Dokploy, terminating TLS and routing
  `mcp.fanmoot.com` to the `mcp` service on port 8080 over `dokploy-network`.
  There is no `ports:` mapping, and there must not be one.
- **A managed PostgreSQL** with seven OAuth tables and nothing else. Migrations
  run in the container's start command before gunicorn starts.
- **fanmoot.com's public API** upstream. There is no database link to Fanmoot.

The container logs JSON, one line per request and one per tool call, each
tagged with `request_id`. Credentials are redacted before any line is written
(`app/logging_config.py`).

---

## Deploy

### 1. Create the service

In Dokploy, create a **Compose** application from this repository, using
`docker-compose.yml` (never `docker-compose.dev.yml`). Set replicas to 1.

### 2. Set the environment

In Dokploy's environment tab. Every variable is documented in `.env.example`.

| Variable | Value |
|---|---|
| `FMCP_ENV` | `production` |
| `FMCP_PUBLIC_BASE_URL` | `https://mcp.fanmoot.com`. No trailing slash. Every discovery document is built from it. |
| `FMCP_DATABASE_URL` | `postgresql+asyncpg://USER:PASS@HOST:5432/fanmoot_mcp?ssl=require` |
| `FMCP_SECRET_ENCRYPTION_KEY` | a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Store a copy in the password manager. Losing it signs every fan out. |
| `FMCP_MCP_ALLOWED_ORIGINS` | `https://alexaplus.fanmoot.com` (the web demo) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | an IAM user allowed `bedrock:InvokeModel` on `amazon.nova-lite-v1:0` in `us-east-1`. Without them `match_mood` falls back to the word list and says so. |
| `FMCP_OAUTH_CLIENTS` | leave empty. Alexa's toolkit is not available to us (§2.0), and every other client registers itself. |

### 3. Route the domain

In Dokploy's Domains tab, add `mcp.fanmoot.com` → service `mcp`, port `8080`,
HTTPS on, with a Let's Encrypt certificate. Point the DNS `A` record for
`mcp.fanmoot.com` at the Dokploy host first, or the certificate challenge fails.

### 4. Check it from outside

```bash
make smoke                                   # everything below, against https://mcp.fanmoot.com
make latency ARGS="--drive https://mcp.fanmoot.com/mcp"
```

`make smoke` checks TLS and HSTS, `/healthz`, all three discovery documents,
a Streamable HTTP session negotiating `2025-11-25`, the 401 challenge, the
Origin guard and the legal pages. It creates nothing. Every line must pass.
`grant storage enabled` failing means the encryption key is not set, and
`no degraded dependency` failing names the dependency in its output.

Then connect one real client from the matrix in the README and post a take as a
throwaway account. That is the acceptance test the suite cannot run.

### Traefik notes

Dokploy's defaults are right. These are the things not to change:

- **Do not add a buffering or compression middleware to this router** unless
  it excludes `text/event-stream`. Every `/mcp` answer is an SSE stream. A
  proxy that holds it until it ends turns a streamed answer into a timeout.
- **Leave the entrypoint's `writeTimeout` at 0** (Traefik's default). A `GET
  /mcp` stream stays open for as long as the client listens.
- **Do not trust `X-Forwarded-For` from the internet.** The OAuth rate limiter
  keys on the first `X-Forwarded-For` entry. Traefik's default overwrites what
  a client sends. Adding the internet to `forwardedHeaders.trustedIPs` would let
  a caller pick their own rate-limit key.
- **HTTP → HTTPS redirect on.** HSTS is sent by the application
  (`app/middleware.py`) whenever `FMCP_PUBLIC_BASE_URL` is `https`. Traefik
  does not need to add it.
- **No sticky sessions needed** at one replica. If this ever scales out,
  sessions need affinity on the `Mcp-Session-Id` header, and a cookie will not
  do because MCP clients do not keep cookies.

---

## Roll back

1. In Dokploy, redeploy the previous commit. The image rebuilds from that ref.
2. Migrations only move forward on start. If the release being rolled back
   added a migration, first run
   `docker compose exec mcp alembic downgrade -1` on the running container, then
   redeploy. (There is one migration so far, so today this never applies.)
3. `make smoke`.

Rolling back does not sign anybody out. Grants live in the database, not in
the image.

---

## Rotate the encryption key

`FMCP_SECRET_ENCRYPTION_KEY` wraps each fan's Fanmoot session token. It takes a
comma-separated list. The first key encrypts, and every key decrypts.

**Routine rotation. Nobody is signed out:**

1. Generate a new key.
2. Set `FMCP_SECRET_ENCRYPTION_KEY=<new>,<old>` and redeploy. Everything
   written from now on uses the new key.
3. Re-wrap what is stored:
   ```bash
   docker compose exec mcp python scripts/rotate_keys.py
   ```
   It reports how many sessions it re-wrapped. It is safe beside live
   traffic: a row the server rewrote in the meantime is skipped, because it is
   already under the new key. A non-zero "unreadable" count means some
   sessions were wrapped by a key that is no longer in the list.
4. Set `FMCP_SECRET_ENCRYPTION_KEY=<new>` and redeploy.

**After a suspected leak.** Signing everyone out is the point here:

1. Set `FMCP_SECRET_ENCRYPTION_KEY=<new>` alone and redeploy.
2. `docker compose exec mcp python scripts/rotate_keys.py --revoke-unreadable`
   ends every link the old key held, so each client is asked to reconnect
   cleanly instead of failing on its next call.

Browser sign-ins in progress at the moment of any rotation have to start again.
The flow is signed with the first key only, so this affects fifteen minutes of
sign-ins at most.

---

## Re-register Alexa's clients

Only relevant once the Alexa+ toolkit is open to us. It is not today (§2.0).

- Alexa's credentials live in `FMCP_OAUTH_CLIENTS` and are upserted by
  `client_id` on every boot. To change one, edit the JSON and redeploy.
- **Register every redirect URI** the Alexa+ Developer Hub lists. The one Alexa
  sends is matched exactly and never substituted. A missing URI shows up as a
  "redirect URI not registered" page with no other symptom.
- Alexa caches the authorization server metadata **when the add-on is
  deployed**. If you change any endpoint or the issuer, redeploy the add-on as
  well as this server.

---

## When Fanmoot's API changes shape

`make verify-live` replays every recording in `fixtures/` against production.
It runs from your machine, not in CI. Run it before a deploy, and whenever a
tool starts failing in a way the test suite doesn't show.

| Result | Meaning | Do |
|---|---|---|
| green | Every shape this server reads is still there. | Nothing. |
| warning (exit 3) | Fields moved, but everything still parses. | `make record`, review the diff in `fixtures/`, `make check`, commit. |
| red (exit 1), **BROKEN … no longer parses (field)** | A field a tool reads was removed, renamed or retyped. A tool will fail in production. | Fix the mirror in `app/fanmoot/models.py` (and whatever reads it), `make record`, `make check`, deploy. |
| red, **was 200, now 4xx/5xx** | An endpoint moved or is down. | Check fanmoot.com's release notes and `/healthz`. |
| GONE lines | A recorded match or moot no longer exists. | Nothing urgent. The next `make record` picks new ones. |

Run it by hand any time with `make verify-live ARGS=-v`.

---

## The latency budget

Alexa+ wants an answer within 500 ms. The server's share is **400 ms at p95
per tool**, and a tool over that is a bug.

```bash
docker compose exec mcp python scripts/latency.py            # last 24 h of real calls, server-side
docker compose exec mcp python scripts/latency.py --since 2  # last two hours
make latency ARGS="--drive https://mcp.fanmoot.com/mcp"       # from your machine, client-side
```

Both exit 1 when a tool is over budget. If one is:

- **Everything slow at once** means fanmoot.com is slow. Check its `/healthz`.
  The warm-up loop (`FMCP_WARM_INTERVAL_S`, default 5 s) keeps the live
  digest, the fortnight's fixtures and the catalogue in cache, so a cold
  upstream shows up first in the tools outside that set.
- **`match_mood` alone** means Bedrock. A reading is cached per thread for five
  minutes, and live matches are pre-read by the warm-up. A slow `match_mood`
  on a quiet thread is one model call. It falls back to the word list after
  `FMCP_BEDROCK_TIMEOUT_S`.
- **Authenticated tools alone** means the token exchange with Fanmoot, which
  is cached for 14 minutes per grant (`FMCP_OAUTH_UPSTREAM_ACCESS_TTL_S`).

---

## Publishing to the MCP registry

`server.json` describes the server for the
[MCP registry](https://registry.modelcontextprotocol.io). It publishes under
`com.fanmoot.mcp/fanmoot`, a namespace this host proves it owns by serving a
public key at `/.well-known/mcp-registry-auth`. That needs no DNS change.

One-time setup:

```bash
openssl genpkey -algorithm Ed25519 -out mcp-registry-key.pem     # keep this file private
PUBLIC_KEY="$(openssl pkey -in mcp-registry-key.pem -pubout -outform DER | tail -c 32 | base64)"
echo "v=MCPv1; k=ed25519; p=${PUBLIC_KEY}"                       # → FMCP_REGISTRY_AUTH
```

Set that line as `FMCP_REGISTRY_AUTH` in Dokploy and redeploy. Then
`curl https://mcp.fanmoot.com/.well-known/mcp-registry-auth` should print it.

Each publish:

```bash
PRIVATE_KEY="$(openssl pkey -in mcp-registry-key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n')"
mcp-publisher login http --domain mcp.fanmoot.com --private-key "${PRIVATE_KEY}"
mcp-publisher publish          # from the repository root, where server.json is
```

`mcp-publisher` is the registry's CLI. Install it as its README describes.
The registry rejects a version it has already seen, so bump `version` in
`pyproject.toml`, `app/__init__.py` and `server.json` together first.
`tests/test_registry.py` fails if they disagree.

Never commit `mcp-registry-key.pem`. The public line is safe to show anyone,
and the private key is not.
