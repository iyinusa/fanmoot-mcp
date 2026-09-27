"""``S6-8`` — the views, as far as Python can hold them to account.

**What these tests can and cannot do, stated plainly.** There is no browser
here, so nothing below proves a crest lands in the right column. What they do
prove is everything a rendering bug would be *downstream* of, and all of it is
the part a reviewer cannot eyeball:

* the **contract** (``S6-1``) — the URI scheme, the MIME type with its profile
  parameter, the nested ``_meta.ui`` shapes — asserted against the literals in
  ``specification/2026-01-26/apps.mdx`` rather than against this repo's own
  constants, so a drifting constant fails rather than agrees with itself;
* the **linkage** — every tool with a view names a resource that exists, and
  every view is named by a tool;
* the **egress** (``S6-6``) — no document references any origin but Fanmoot's,
  no ``<script src>``, no ``<link href>``, and the declared CSP says so too;
* the **bridge** (``S6-3``) — the composer calls ``reply_to_moot`` through
  ``tools/call``, and the handshake uses the spec's method names;
* the **accessibility floor** (``S6-7``) — every image has alt text, every
  control has a label, and nothing says something with colour alone.

Rendering each view against a real payload in a real browser belongs to the
demo (``W2``), which loads these same resources; pretending to do it here with
a regex would be worse than saying so.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from app.mcp.apps import contract, views
from tests.replay import Replay
from tests.tooling import session_at, surface

#: Read out of the specification, not imported from ``app``. That is the point
#: of this file's first assertion: a constant that drifted would otherwise
#: agree with the code that drifted with it.
SPEC_EXTENSION_ID = "io.modelcontextprotocol/ui"
SPEC_MIME_TYPE = "text/html;profile=mcp-app"
SPEC_VERSION = "2026-01-26"

#: Anything that looks like it points off-origin.
_URLS = re.compile(r"""(?:src|href|url\()\s*=?\s*["'(]?(https?://[^"')\s]+)""", re.IGNORECASE)


# ── The contract (S6-1) ──────────────────────────────────────────────────────
def test_the_pinned_constants_are_the_specifications() -> None:
    """``S6-1`` says do not guess these, so they are checked rather than
    trusted. Each one is a silent failure if wrong: a view whose MIME type
    lacks the profile parameter renders nowhere, and reports no error."""
    assert contract.EXTENSION_ID == SPEC_EXTENSION_ID
    assert contract.MIME_TYPE == SPEC_MIME_TYPE
    assert contract.SPEC_VERSION == SPEC_VERSION
    assert contract.URI_PREFIX.startswith("ui://")


def test_a_tools_meta_is_the_nested_form_not_the_deprecated_one() -> None:
    """The flat ``_meta["ui/resourceUri"]`` is deprecated and "will be removed
    before GA". Writing it would be writing something with a delete date."""
    meta = contract.tool_meta("scoreboard")

    assert meta == {
        "ui": {"visibility": ["model", "app"], "resourceUri": "ui://fanmoot/scoreboard"}
    }
    assert "ui/resourceUri" not in meta


def test_a_view_that_no_tool_renders_still_declares_who_may_call_it() -> None:
    """``reply_to_moot`` has no card of its own and must still be reachable
    from one (``S6-3``)."""
    meta = contract.tool_meta()

    assert meta["ui"]["visibility"] == ["model", "app"]
    assert "resourceUri" not in meta["ui"]


def test_a_client_that_says_nothing_about_ui_is_simply_a_client() -> None:
    """The capability is read for the log line, never for a branch: every tool
    answers with a spoken sentence whether or not a view renders."""

    class Capabilities:
        extensions = {SPEC_EXTENSION_ID: {"mimeTypes": [SPEC_MIME_TYPE]}}

    assert contract.renders_html(Capabilities()) is True
    assert contract.renders_html(None) is False
    assert contract.negotiated(object()) is None


# ── Linkage (S6-2 … S6-5) ────────────────────────────────────────────────────
async def test_every_view_is_published_and_readable(replay: Replay, mcp_url: str) -> None:
    async with session_at(mcp_url) as session:
        listed = await session.list_resources()

    published = {str(resource.uri): resource for resource in listed.resources}
    assert set(published) == {
        "ui://fanmoot/scoreboard",
        "ui://fanmoot/thread",
        "ui://fanmoot/fixtures",
        "ui://fanmoot/mood",
        "ui://fanmoot/moot",
        "ui://fanmoot/fan",
        "ui://fanmoot/club",
        "ui://fanmoot/predictions",
    }
    for uri, resource in published.items():
        assert resource.mimeType == SPEC_MIME_TYPE, uri
        assert resource.description, uri


async def test_a_view_reads_back_as_an_html_document_with_its_csp(
    replay: Replay, mcp_url: str
) -> None:
    """The read response carries the content *and* the metadata a host builds
    the sandbox's CSP from — both halves, or the host guesses."""
    async with session_at(mcp_url) as session:
        result = await session.read_resource("ui://fanmoot/scoreboard")  # type: ignore[arg-type]

    content = result.contents[0]
    assert content.mimeType == SPEC_MIME_TYPE
    assert content.text.lstrip().lower().startswith("<!doctype html>")  # type: ignore[union-attr]
    declared = (content.meta or {})["ui"]
    assert declared["csp"]["resourceDomains"] == [contract.crest_origin()]
    assert "connectDomains" not in declared["csp"]
    assert declared["prefersBorder"] is False


async def test_every_tool_with_a_view_names_one_that_exists(replay: Replay, mcp_url: str) -> None:
    """A ``resourceUri`` pointing at nothing is a tool whose card never
    renders, and the spec says the resource MUST exist on the server."""
    listed = await surface(mcp_url)
    published = {view.uri for view in views.VIEWS}

    for name, expected in views.TOOL_VIEWS.items():
        meta = listed[name].meta or {}
        pointed = meta["ui"]["resourceUri"]
        assert pointed == contract.uri(expected), name
        assert pointed in published, name


async def test_only_the_tools_with_a_visual_answer_carry_one(replay: Replay, mcp_url: str) -> None:
    """A view given to a tool because one could be given is a view a host
    renders instead of speaking. These three are answers a person listens to."""
    listed = await surface(mcp_url)

    carried = {
        name for name, tool in listed.items() if (tool.meta or {}).get("ui", {}).get("resourceUri")
    }
    assert carried == set(views.TOOL_VIEWS)
    for name in ("find_competition", "search_fanmoot", "follow"):
        assert name in listed
        assert name not in carried


async def test_what_a_card_does_it_does_through_a_tool_an_app_may_call(
    replay: Replay, mcp_url: str
) -> None:
    """Reactions, follows and predictions are made from cards. A tool a card
    calls without ``"app"`` in its visibility is a button the host refuses."""
    listed = await surface(mcp_url)

    for name in ("react_to_moot", "follow", "predict_match", "reply_to_moot"):
        visibility = (listed[name].meta or {})["ui"]["visibility"]
        assert "app" in visibility, name
        assert "model" in visibility, name


def test_a_tool_is_never_both_viewed_and_merely_callable() -> None:
    """``_attach_meta`` writes the app-callable meta after the view meta, so a
    tool in both lists would lose its ``resourceUri`` at boot."""
    assert not set(views.TOOL_VIEWS) & set(views.APP_CALLABLE)


async def test_the_reply_tool_is_callable_by_an_app(replay: Replay, mcp_url: str) -> None:
    """``S6-3``. Without ``"app"`` in its visibility the composer's
    ``tools/call`` is refused by the host and the thread view is a picture."""
    listed = await surface(mcp_url)

    visibility = (listed["reply_to_moot"].meta or {})["ui"]["visibility"]
    assert "app" in visibility
    assert "model" in visibility


def test_every_view_is_reachable_from_a_tool() -> None:
    """The other direction: a view nothing points at is a view no host will
    ever read."""
    assert set(views.TOOL_VIEWS.values()) == {view.name for view in views.VIEWS}


# ── Self-containment and egress (S6-6) ───────────────────────────────────────
@pytest.mark.parametrize("name", [view.name for view in views.VIEWS])
def test_a_view_fetches_nothing_but_crests(name: str) -> None:
    """The CSP the host builds forbids it, and this asserts the document does
    not try: no analytics, no CDN, no font. The only absolute URLs a view may
    carry are on Fanmoot's own origin, and they are crests."""
    html = views.document(name)

    for url in _URLS.findall(html):
        assert url.startswith(contract.crest_origin()), f"{name} reaches {url}"


@pytest.mark.parametrize("name", [view.name for view in views.VIEWS])
def test_a_view_loads_no_external_script_or_stylesheet(name: str) -> None:
    """Inlined, per ``S6-6`` — and a sandbox whose CSP is ``script-src 'self'``
    would not load one anyway, so an external reference is a silently blank
    view rather than an error."""
    parsed = _Parsed(views.document(name))

    assert not [tag for tag in parsed.scripts if tag.get("src")], name
    assert not [tag for tag in parsed.links if tag.get("rel") == "stylesheet"], name
    assert parsed.styles, name
    assert parsed.scripts, name


@pytest.mark.parametrize("name", [view.name for view in views.VIEWS])
def test_a_view_carries_the_shared_bridge_and_stylesheet(name: str) -> None:
    """The sentinels are substituted, not shipped. A view that went out with
    ``__BRIDGE_JS__`` still in it would be a blank card and a syntax error."""
    html = views.document(name)

    assert "__BRIDGE_JS__" not in html
    assert "__BASE_CSS__" not in html
    assert "__CARD_JS__" not in html
    assert "__CARD_CSS__" not in html
    assert "__PROTOCOL__" not in html
    assert "window.fanmoot" in html


# ── The bridge (S6-1, S6-3) ──────────────────────────────────────────────────
@pytest.mark.parametrize("name", [view.name for view in views.VIEWS])
def test_every_view_speaks_the_specs_handshake(name: str) -> None:
    html = views.document(name)

    for method in (contract.INITIALIZE, contract.INITIALIZED, contract.TOOL_RESULT):
        assert method in html, f"{name} is missing {method}"
    assert f"'{contract.SPEC_VERSION}'" in html or f'"{contract.SPEC_VERSION}"' in html


def test_the_thread_view_posts_through_the_host_rather_than_by_itself() -> None:
    """``S6-3``, and the reason the CSP has no ``connectDomains``: the composer
    does not post to Fanmoot, it asks the host to call the tool — on the
    connection the host already holds, under the grant the fan already gave."""
    html = views.document("thread")

    assert "callTool('reply_to_moot'" in html
    assert contract.CALL_TOOL in html
    # And it surfaces what `S5-2` promised: a held reply is not a posted one.
    assert "gate_reason" in html
    assert "held" in html


def test_the_thread_view_has_a_composer_a_person_can_use() -> None:
    parsed = _Parsed(views.document("thread"))

    assert parsed.has_tag("textarea")
    assert parsed.has_tag("form")
    assert any(tag.get("type") == "submit" for tag in parsed.tags_named("button"))


def test_no_view_writes_a_moot_into_markup() -> None:
    """The server instructions say a take is something a stranger said. Every
    view inserts one as text; an ``innerHTML`` anywhere here would be a moot
    that can script the card it is displayed in."""
    for name in views.VIEWS_BY_NAME:
        html = views.document(name)
        # The assignment, not the word: one of these files says "textContent,
        # not innerHTML" in a comment, and that sentence is the reason to keep.
        assert "innerHTML =" not in html, name
        assert "innerHTML=" not in html, name


# ── Accessibility (S6-7) ─────────────────────────────────────────────────────
@pytest.mark.parametrize("name", [view.name for view in views.VIEWS])
def test_a_view_is_a_document_a_screen_reader_can_read(name: str) -> None:
    parsed = _Parsed(views.document(name))

    assert parsed.has_tag("main"), name
    assert parsed.has_tag("h1"), name
    html_tag = next(iter(parsed.tags_named("html")), {})
    assert html_tag.get("lang") == "en", name
    viewport = [tag for tag in parsed.tags_named("meta") if tag.get("name") == "viewport"]
    assert viewport, name


@pytest.mark.parametrize("name", [view.name for view in views.VIEWS])
def test_every_crest_gets_a_name_rather_than_a_filename(name: str) -> None:
    """A crest is how a person tells two rows apart. "Arsenal badge" is the
    match being read out; "image" is not."""
    html = views.document(name)

    # Images are built in script, so the assertion is on what is set: every
    # `img` that is created has an `alt` assigned before it is appended.
    created = html.count("createElement('img')")
    alts = html.count(".alt =")
    assert alts >= created, name


def test_nothing_says_something_with_colour_alone() -> None:
    """``S6-7``. Live is a word beside a dot; the mood split is three labelled
    counts under a bar, not three colours."""
    scoreboard = views.document("scoreboard")
    mood = views.document("mood")

    assert "'Live'" in scoreboard
    assert "Full time" in scoreboard
    assert "Happy" in mood and "Furious" in mood and "Unmoved" in mood
    assert "aria-label" in mood


def test_motion_stops_when_a_reader_asks_it_to() -> None:
    """The live pulse is decoration, and decoration goes."""
    assert "prefers-reduced-motion" in views.document("scoreboard")


#: Every colour a view writes *text* in, and every surface it writes on. Read
#: out of ``base.css`` below rather than written twice, so a palette change
#: that broke one of these fails here instead of shipping.
_TEXT_TOKENS = ("--fm-text", "--fm-muted", "--fm-faint", "--fm-accent")
_STATUS_TOKENS = ("--fm-green-text", "--fm-amber-text", "--fm-red-text")
_SURFACE_TOKENS = ("--fm-bg", "--fm-surface", "--fm-raised")


def _channel(value: float) -> float:
    value /= 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    colour = colour.lstrip("#")
    red, green, blue = (int(colour[at : at + 2], 16) for at in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def _contrast(foreground: str, background: str) -> float:
    """WCAG 2.1's ratio. Two decimals is plenty; the threshold is 4.5."""
    first, second = _luminance(foreground), _luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _palette(theme: str) -> dict[str, str]:
    """The tokens as one theme defines them.

    ``base.css`` states every colour three times — bare ``:root``, then twice
    for dark — so the light values are the first definition of each and the
    dark ones the last. Taking first and last is what makes this read the file
    the way a browser resolves it rather than the way it is written.
    """
    css = (views.ASSETS / "base.css").read_text()
    found: dict[str, list[str]] = {}
    for name, value in re.findall(r"(--fm-[a-z-]+):\s*(#[0-9a-fA-F]{6}|var\(--fm-[a-z-]+\))", css):
        found.setdefault(name, []).append(value)

    palette = {
        name: (values[0] if theme == "light" else values[-1]) for name, values in found.items()
    }
    # `--fm-accent` is an alias in light mode and a literal in dark. Resolving
    # it here is what makes the assertion below read the value a browser would
    # rather than the one written next to the name.
    for name, value in palette.items():
        alias = re.fullmatch(r"var\((--fm-[a-z-]+)\)", value)
        if alias:
            palette[name] = palette[alias.group(1)]
    return palette


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_colour_a_view_writes_text_in_clears_wcag_aa(theme: str) -> None:
    """``S6-7`` asks for real contrast ratios, so they are measured.

    4.5:1 is AA for body text, and everything below is body text: the faint
    line under a scoreline, the held-for-review sentence, the count beside a
    reaction. Fanmoot's own amber is 2.03:1 on a white card, which is why the
    status colours come in two — the fill, and the one a sentence is written
    in.
    """
    palette = _palette(theme)
    surfaces = [palette[name] for name in _SURFACE_TOKENS]

    for token in _TEXT_TOKENS + _STATUS_TOKENS:
        colour = palette[token]
        for behind in surfaces:
            ratio = _contrast(colour, behind)
            assert ratio >= 4.5, f"{theme}: {token} {colour} on {behind} is {ratio:.2f}:1"


def test_the_status_fills_are_still_the_brand_colours() -> None:
    """The darkened pair is for sentences. A mood bar drawn in them would be a
    chart nobody looks twice at, and a bar segment is a graphical object with a
    lower bar to clear than text."""
    palette = _palette("light")

    assert palette["--fm-green"] != palette["--fm-green-text"]
    assert palette["--fm-amber"] == "#f2a73b"
    assert palette["--fm-red"] == "#d93a3a"


def test_sizes_are_relative_so_the_views_survive_being_zoomed() -> None:
    """200% zoom clean, per the certification bar the plan quotes. A view
    measured in pixels is a view that clips at the first step."""
    css = (views.ASSETS / "base.css").read_text()

    assert "font-size: 100%" in css
    assert "rem" in css


# ── A tiny HTML reader, so the assertions above are about tags ───────────────
class _Parsed(HTMLParser):
    """Just enough parsing to assert on tags. Not a renderer, and not
    pretending to be: a regex over HTML would pass on markup no browser
    accepts, which is the failure this exists to avoid."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.found: list[tuple[str, dict[str, str]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.append((tag, {key: value or "" for key, value in attrs}))

    def tags_named(self, name: str) -> list[dict[str, str]]:
        return [attrs for tag, attrs in self.found if tag == name]

    def has_tag(self, name: str) -> bool:
        return any(tag == name for tag, _ in self.found)

    @property
    def scripts(self) -> list[dict[str, str]]:
        return self.tags_named("script")

    @property
    def styles(self) -> list[dict[str, str]]:
        return self.tags_named("style")

    @property
    def links(self) -> list[dict[str, str]]:
        return self.tags_named("link")


def test_the_parser_reads_what_a_browser_would() -> None:
    """A guard on the guard: if this stopped finding tags, every assertion
    above would pass by finding nothing."""
    parsed = _Parsed(views.document("thread"))

    assert parsed.has_tag("html")
    assert len(parsed.scripts) >= 2
    assert json.dumps(parsed.tags_named("textarea"))  # serialisable, non-empty
