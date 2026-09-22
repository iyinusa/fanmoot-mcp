# fanmoot-mcp

A standards-compliant **Model Context Protocol** server that exposes
[Fanmoot](https://fanmoot.com) — where fans debate football — as tools,
resources and prompts, with its own OAuth 2.1 authorization server so a fan can
sign in *or sign up* from the client and then post in their own name.

Spec era **`2025-11-25`**, Streamable HTTP, on `POST|GET /mcp`.

> **In progress.** This README fills out as the milestones land. Through `S3`
> the server answers football questions to anyone who connects; posting,
> replying and predicting need the OAuth server that arrives with `S4` and the
> tools that sit on it in `S5`. See `../Amazon Hackathon.md` §6 for the
> milestone list.

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
from Fanmoot's own schemas and marked `synthetic` in the manifest.

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
