"""The MCP Apps extension contract, written down rather than remembered (``S6-1``).

Every literal in this module was read out of the specification, not recalled:
``modelcontextprotocol/ext-apps``, ``specification/2026-01-26/apps.mdx``,
**SEP-1865, status Stable (2026-01-26)**. The plan says *do not guess these*,
and the reason is that each one is a silent failure: a view whose ``mimeType``
is ``text/html`` renders nowhere, a tool whose ``_meta`` key is a character out
is a tool with no view and no error.

**Where the prose and the SDK disagree, the SDK's schema wins.** Hosts do not
read the prose; they validate with ``@modelcontextprotocol/ext-apps``'s zod
schemas (``src/generated/schema.ts``). The prose's ``ui/initialize`` example
names the View ``clientInfo``; the schema requires ``appInfo``, and a host
refused the handshake — every card an empty frame in Claude — until the bridge
said ``appInfo``. Likewise ``ui/message`` content is a *list* of blocks. The
render harness now checks these the way the schema does.

So the constants live here, alone, with the sentence from the spec that fixed
each one:

* **Extension identifier** — "This extension is identified as:
  ``io.modelcontextprotocol/ui``".
* **MIME type** — "``mimeType`` MUST be ``text/html;profile=mcp-app``".
* **URI scheme** — "URI MUST start with ``ui://`` scheme".
* **Tool linkage** — "Tools are associated with UI resources through the
  ``_meta.ui`` field", ``{"ui": {"resourceUri": …}}``. The flat
  ``_meta["ui/resourceUri"]`` form is **deprecated** and "will be removed
  before GA", so it is not written here.
* **Visibility** — ``["model", "app"]``. It is the default, and it is declared
  anyway: ``reply_to_moot`` being callable *by the app* is what makes the
  thread view interactive (``S6-3``), and a default is exactly the kind of
  thing that changes underneath a feature that depends on it.

**Why the views are registered unconditionally.** The spec says a server
SHOULD check the client's ``extensions`` capability before registering
UI-enabled tools, and offers a text-only variant otherwise. This server does
not need the variant: every tool already answers with a spoken sentence in
``content[0].text`` and a typed payload in ``structuredContent``, which is the
graceful degradation the same section asks for — "Tools MUST return meaningful
content array even when UI is available". A host that has never heard of MCP
Apps ignores one ``_meta`` key and gets exactly the answer it got before ``S6``.
``negotiated()`` is here for the log line and the tests, not for a branch.
"""

from __future__ import annotations

from typing import Any

from app.settings import get_settings

#: ``specification/2026-01-26/apps.mdx``. Pinned, per ``S6-1``: this is the
#: version the ``_meta`` shapes and bridge method names below were read from,
#: and the one a view announces in its ``ui/initialize``.
SPEC_VERSION = "2026-01-26"

#: The extension identifier clients advertise support with.
EXTENSION_ID = "io.modelcontextprotocol/ui"

#: The one content type the MVP defines. Note the profile parameter — a bare
#: ``text/html`` is not this.
MIME_TYPE = "text/html;profile=mcp-app"

#: Every UI resource this server publishes lives under this prefix.
URI_PREFIX = "ui://fanmoot/"

#: Who may call a tool. ``model`` is the agent; ``app`` is a view served by
#: this same server, which is how the thread view's composer reaches
#: ``reply_to_moot``.
VISIBILITY_ALL: list[str] = ["model", "app"]

# ── The bridge, View → Host (spec §Requests) ─────────────────────────────────
OPEN_LINK = "ui/open-link"
MESSAGE = "ui/message"
REQUEST_DISPLAY_MODE = "ui/request-display-mode"
UPDATE_MODEL_CONTEXT = "ui/update-model-context"
INITIALIZE = "ui/initialize"
INITIALIZED = "ui/notifications/initialized"
SIZE_CHANGED = "ui/notifications/size-changed"

# ── The bridge, Host → View (spec §Notifications) ────────────────────────────
TOOL_INPUT = "ui/notifications/tool-input"
TOOL_INPUT_PARTIAL = "ui/notifications/tool-input-partial"
TOOL_RESULT = "ui/notifications/tool-result"
TOOL_CANCELLED = "ui/notifications/tool-cancelled"
HOST_CONTEXT_CHANGED = "ui/notifications/host-context-changed"

#: The standard MCP subset a view may speak through the host. ``tools/call`` is
#: the one that matters here: it is how the reply composer posts (``S6-3``).
CALL_TOOL = "tools/call"
READ_RESOURCE = "resources/read"

#: Display modes this server's views declare. No ``pip``: a picture-in-picture
#: scoreboard is a lovely idea and not one of these four views is written for a
#: floating 200-pixel box, so claiming it would be claiming something untested.
DISPLAY_MODES: list[str] = ["inline", "fullscreen"]


def uri(name: str) -> str:
    """``"scoreboard"`` → ``"ui://fanmoot/scoreboard"``."""
    return f"{URI_PREFIX}{name}"


def tool_meta(view: str | None = None) -> dict[str, Any]:
    """The ``_meta`` a tool carries to name its view.

    ``view`` of None is a tool with no view of its own that the *app* may still
    call — ``reply_to_moot``, reached from the thread view's composer.
    """
    ui: dict[str, Any] = {"visibility": list(VISIBILITY_ALL)}
    if view is not None:
        ui["resourceUri"] = uri(view)
    return {"ui": ui}


def resource_meta() -> dict[str, Any]:
    """The ``_meta`` every view carries: its CSP, and its border preference.

    **The CSP is the interesting half** (``S6-6``). A host builds the sandbox's
    Content-Security-Policy from exactly these lists and "MUST NOT allow
    undeclared domains", so what is left out is enforced rather than merely
    intended:

    * ``connectDomains`` is **absent**, which the spec reads as
      ``connect-src 'none'``. These views never fetch. Everything they render
      arrives in a ``ui/notifications/tool-result``, which is the whole point of
      the extension — a view that phoned home would be a second, unaudited path
      to Fanmoot beside the one the host can see.
    * ``resourceDomains`` is Fanmoot's own origin and nothing else, because a
      crest is an image on that origin and drawing a club without its badge is
      not a scoreboard. Taken from settings rather than written out, so a
      deployment pointed at a staging Fanmoot declares the origin it will
      actually load from.
    * ``frameDomains`` and ``baseUriDomains`` are absent: no nested iframes,
      and the document's base stays on the sandbox origin.

    ``prefersBorder`` is false and stated rather than omitted, as the spec
    recommends, because these views draw their own card surface and a host
    frame around it is a border around a border.
    """
    return {
        "ui": {
            "csp": {"resourceDomains": [crest_origin()]},
            "prefersBorder": False,
        }
    }


def crest_origin() -> str:
    """The one origin a view is allowed to load anything from."""
    return get_settings().fanmoot_api_base_url


def negotiated(capabilities: Any) -> dict[str, Any] | None:
    """What a client said about MCP Apps in ``initialize``, if anything.

    Read defensively: ``extensions`` is SEP-1724's field and the pinned SDK
    (1.x, the handshake era — HAD-2) has no typed attribute for it, so it
    arrives as an extra on ``ClientCapabilities``. Absent means the client is
    older, or simply does not do this; both are fine, and neither changes what
    gets registered.
    """
    if capabilities is None:
        return None
    extensions = getattr(capabilities, "extensions", None)
    if extensions is None:
        extensions = (getattr(capabilities, "model_extra", None) or {}).get("extensions")
    if not isinstance(extensions, dict):
        return None
    settings = extensions.get(EXTENSION_ID)
    return settings if isinstance(settings, dict) else None


def renders_html(capabilities: Any) -> bool:
    """Whether this client says it can render an MCP App. Observability only."""
    settings = negotiated(capabilities)
    if settings is None:
        return False
    return MIME_TYPE in (settings.get("mimeTypes") or [])
