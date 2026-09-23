# fanmoot-mcp

A standards-compliant **Model Context Protocol** server that exposes
[Fanmoot](https://fanmoot.com) — where fans debate football — as tools,
resources and prompts, with its own OAuth 2.1 authorization server so a fan can
sign in *or sign up* from the client and then post in their own name.

Spec era **`2025-11-25`**, Streamable HTTP, on `POST|GET /mcp`.

> **In progress.** This README fills out as the milestones land. Through `S6`
> the server answers football questions to anyone who connects, a fan can link
> their Fanmoot account to any client, a linked fan can post, reply, react,
> predict and follow by tool call, and a host that supports MCP Apps renders
> four interactive views. See `../Amazon Hackathon.md` §6 for the milestone
> list.

## What it talks to

The server is a **client of Fanmoot's public API** (`https://fanmoot.com/v1/*`),
not of its database. It holds no model of Fanmoot's domain and shares no
connection pool with it. Everything the server can possibly do, a fan could do
through the public API.

Its own small PostgreSQL holds **OAuth state and nothing else**.

## What it can answer

Twelve read tools, no authentication. A client that connects can ask who is
playing tonight, what the score is, and what fans are saying about it.

| Tool | What a fan is asking |
|---|---|
| `find_club` | "the gunners", "spurs", "los blancos" → the club |
| `find_competition` | "the Prem" → the competition and its clubs |
| `list_matches` | "who are Arsenal playing", "how did they get on" |
| `upcoming_matches` | "what's on this weekend", grouped by day |
| `get_match` | "what's happening in the Brighton game" |
| `live_scores` | "what's the score", "is anything on" |
| `read_match_conversation` | "what are fans saying about it" |
| `read_thread` | one take and the argument under it |
| `search_fanmoot` | anything the others cannot place |
| `trending_takes` | "what's everyone talking about" |
| `club_news` | "any news about the transfer" |
| `match_mood` | "are fans happy", as three numbers and a sentence |

Four things hold across all of them, and they are the difference between a
voice surface and a REST API with a JSON-RPC envelope:

- **Nouns a fan says, not ids.** `get_match` takes `"arsenal vs spurs"`, a
  fanmoot.com link, or a UUID. A name that could mean three clubs comes back as
  a question — "Do you mean Arsenal, Aston Villa or Athletic Club?" — never as
  a guess.
- **An answer that can be read aloud.** Every result carries
  `structuredContent` for a machine *and* a `content[0].text` a model can speak
  unedited: "Arsenal are two-one up at the Emirates, sixty-seventh minute."
  One module writes all of them (`app/voice.py`), so the server has one voice.
- **Bounded output.** Five by default, twenty at the cap, declared in each
  tool's JSON Schema — and what was left out is said: "There are four more
  fixtures."
- **A score nobody observed is never stated.** Fanmoot marks a scoreline
  `assumed` when a server-side clock started the match and no source has
  confirmed anything since. Those come back as "under way, but nobody has
  confirmed a score yet".

`match_mood` is the one tool that is computed rather than fetched: it samples a
match's **public** thread and labels it with the server's own Bedrock call —
counts out with labels never stored, public moots only and never a handle, and
the moots framed as data rather than as instructions. With no model configured
it answers from an offline lexicon and says `"method": "lexicon"` in its
payload, so the tool is never simply down.

## What it can do for a linked fan

Seven more tools, each behind an OAuth scope the fan ticked on the consent
screen. These are what make Fanmoot a place to argue rather than a place to
read.

| Tool | What a fan is asking | Scope |
|---|---|---|
| `whoami` | "who am I on Fanmoot", "did that connect" | `fan.read` |
| `post_moot` | "post that", optionally anchored to a match | `moot.write` |
| `reply_to_moot` | answering somebody, in their thread | `moot.write` |
| `react_to_moot` | respect, heat, tactics, banter, offside | `moot.write` |
| `predict_match` | "call it two-one" | `predict.write` |
| `my_predictions` | what is called, and what is still open | `fan.read` |
| `follow` | a club, a competition or a fan | `fan.write` |

Three promises hold across the ones that write, and each is tested:

- **A retried frame posts once.** Every compose carries an idempotency key
  hashed from the grant, the JSON-RPC request id and the take itself. A client
  that retries after a dropped socket gets the moot the first attempt created;
  two different takes can never collide.
- **A held take is never reported as live.** Fanmoot's football-topicality gate
  can park a post for a moderator, and the answer says so, in Fanmoot's own
  words.
- **Nothing is trimmed.** A dictated take past the character cap comes back as
  "shall I shorten it?" rather than as a truncated opinion published under
  somebody's name.

Refusals from upstream reach the fan as something they can act on: an
unconfirmed email address says where to look for the link, a prediction after
kickoff says a take is still possible, and a rate limit says how long to wait.

## What it can show

Eight of the tools have a visual answer as well as a spoken one. A host that
supports the **MCP Apps** extension (`io.modelcontextprotocol/ui`, spec
`2026-01-26`) renders them; a host that does not gets the same sentence and the
same `structuredContent` it always got, which is the graceful degradation the
extension asks for.

| View | Rendered for | What it is |
|---|---|---|
| `ui://fanmoot/scoreboard` | `get_match`, `live_scores` | Crests, the scoreline, the live minute |
| `ui://fanmoot/thread` | `read_match_conversation`, `read_thread`, `trending_takes` | The takes, **and a composer that replies** |
| `ui://fanmoot/fixtures` | `list_matches`, `upcoming_matches` | A matchweek grouped by day |
| `ui://fanmoot/mood` | `match_mood` | The sentiment split as one bar and three counts |

The thread view is the one that is not a picture of an answer: its composer
calls `reply_to_moot` back through the host as a `tools/call`, on the same
connection and the same OAuth grant, so a fan reads the argument and answers it
without leaving the card.

Each view is a single self-contained HTML document. The declared CSP has **no**
`connectDomains` — the views never fetch, because everything they draw arrives
in a `ui/notifications/tool-result` — and one `resourceDomains` entry, Fanmoot's
own origin, for crests. Colours come from the host's theme variables where it
passes them and from Fanmoot's Floodlit palette where it does not; every text
pairing is measured against WCAG AA in the test suite rather than asserted.

## Authentication

The server is its **own OAuth 2.1 authorization server**. A fan signs in — or
signs up — on a Fanmoot-branded page here, chooses what the client may do, and
the client gets a token. It is never a Fanmoot access token: what a client holds
is an opaque handle to a grant row, and the server exchanges it for a
short-lived Fanmoot token per call. A leaked client token is revocable, expires
in an hour, and is meaningless to `fanmoot.com`.

| Endpoint | What |
|---|---|
| `/.well-known/oauth-protected-resource` | RFC 9728 — what `/mcp` is, and who authorizes it (also served at `…/oauth-protected-resource/mcp`) |
| `/.well-known/oauth-authorization-server` | RFC 8414 — every endpoint, and `code_challenge_methods_supported: ["S256"]` |
| `/.well-known/openid-configuration` | the same document, because some clients probe this path first |
| `GET /authorize` | authorization code + PKCE (S256 only) |
| `POST /token` | code exchange and rotating refresh |
| `POST /oauth/register` | RFC 7591 dynamic client registration |
| `GET /oauth/signup` | a **new fan** creating an account mid-flow |
| `POST /oauth/revoke` | RFC 7009 |
| `GET /oauth/connected` | a fan's connected apps, and a button to disconnect each |

Four scopes, because "let this thing use my account" is not one decision:
`fan.read`, `moot.write`, `predict.write`, `fan.write`. The consent screen names
each one in the words a fan reads and lets them untick any of it; the grant
records what was ticked, not what was asked for.

### Any client, not just Alexa

There are two tiers of client registration, and they land in the same table with
the same scopes:

- **Self-registering** — Claude, ChatGPT, Cursor and MCP Inspector register
  themselves through **RFC 7591** at `POST /oauth/register`, or identify
  themselves by a URL serving their own metadata (**SEP-991**, the mechanism
  `2025-11-25` recommends). Registration is open, with the guards that implies:
  per-address rate limiting, `https` redirect URIs plus `http://localhost` and
  `http://127.0.0.1` for loopback callbacks, a server-generated `client_id`, and
  a TTL that reaps registrations that never complete a grant.
- **Pre-seeded** — Alexa+ has no dynamic registration, so its credentials come
  from `FMCP_OAUTH_CLIENTS` and are upserted by `client_id` at every boot.
  Register *every* redirect URI from the Developer Hub: the presented one is
  matched exactly and never substituted for a default.

### What a client sees without a token

Nothing changes for the twelve read tools — they take no credential at all, and
"what's the score" is answerable without linking anything. A call to a tool that
acts as the fan comes back **401** with an RFC 9728 challenge naming the
protected-resource document, which is what Alexa starts account linking from and
what every other client discovers the authorization server from. A grant that is
missing one scope gets **403 `insufficient_scope`** naming what to ask for, so a
read-only link is upgraded rather than failed.

Without `FMCP_SECRET_ENCRYPTION_KEY` the server will not hold a fan's Fanmoot
session at all: `/authorize` answers 503 and the read tools carry on. That is the
safe direction, and `/healthz` reports it as `grant_storage: disabled`.

## Run it locally

```bash
make install          # uv venv + dependencies
cp .env.example .env  # then set FMCP_DATABASE_URL
make migrate
make dev              # http://localhost:8080
```

Or in Docker, which is what production runs:

```bash
make up               # docker-compose.yml + docker-compose.dev.yml
curl -s localhost:8080/healthz | python3 -m json.tool
```

`make check` is what CI runs — `ruff`, `mypy --strict`, `pytest`. A milestone
task is ticked only when it passes.

## Tests run offline, against a recorded Fanmoot

`fixtures/` holds real `/v1` responses captured from production by `make
record`. The suite drives the actual client — real timeouts, real retry policy,
real error mapping — against a transport that replays them, so it is fast,
deterministic and still asserting against shapes the API genuinely produced.
Nothing in `make check` touches the network or needs a database.

```bash
make record           # re-capture (needs network); writes fixtures/manifest.json
make record ARGS=-v   # …and print what each call returned
```

The authenticated endpoints are not recorded live — doing so would write a real
fan's handle and email into this repository — so their fixtures are hand-built
from Fanmoot's own schemas and marked `synthetic` in the manifest. `/v1/auth/*`
is stubbed per test for the same reason, one step further: a recording of a
sign-in is a recording of somebody's credentials being accepted.

The OAuth tests need *rows*, so they substitute an in-memory SQLite database and
create the schema from the models. That exercises the real queries — an
authorization code really is found by its hash, a replayed refresh token really
does revoke a family — and still leaves `make check` with nothing to install.

## Connect a client

The endpoint is `https://mcp.fanmoot.com/mcp` — one path, three methods:
`POST` sends a JSON-RPC frame and gets an SSE stream back, `GET` opens the
stream the server pushes notifications down, `DELETE` ends the session. There
is no trailing slash and no redirect.

Locally, with `make dev` running in another shell:

```bash
make inspect          # MCP Inspector against http://127.0.0.1:8080/mcp
make inspect MCP_URL=https://mcp.fanmoot.com/mcp
```

Full client-by-client configuration — Claude, ChatGPT, Cursor, Alexa+ — lands
with `S7-6`.

### What the transport does

- **Protocol `2025-11-25`**, the era Alexa+ implements. The SDK also ships the
  newer one; serving only that would fail the track, so the negotiated version
  is asserted in `tests/test_mcp_transport.py`.
- **Sessions**, with `Last-Event-ID` resumability: a client whose SSE stream
  drops reconnects quoting the last event it saw and is sent what it missed.
- **`Origin` validation**, returning `403` — a spec MUST and the defence
  against DNS rebinding. Non-browser clients send no `Origin` and are
  unaffected; browser origins are configured with `FMCP_MCP_ALLOWED_ORIGINS`.
- **Every JSON-RPC frame logged** with a request id, and one record per tool
  call with its duration and outcome. An inbound `X-Request-ID` is honoured, so
  a frame in the demo's trace panel and a line in these logs are the same
  event. No frame payloads and no tool arguments are ever logged.
- **Errors that teach**: a bad argument comes back as a tool execution error
  carrying a sentence a model can read aloud — "I still need club for that." —
  not a protocol error that ends the turn.

## Licence

MIT. See [LICENSE](LICENSE).
