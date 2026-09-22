"""The four scopes, in the words a fan reads and the words a tool enforces.

Four, not one, because "let this thing use my Fanmoot account" is not a single
decision: reading who you are is not posting as you, and posting a take is not
following a club. A voice surface makes that worse rather than better — a fan
cannot see what happened — so the split is the honest minimum.

They mirror §5's table exactly, which is what makes ``TOOL_SCOPES`` below
checkable: every authenticated tool `S5` registers must appear there, and a tool
that does not appear is a tool the gate treats as public.

**The descriptions are the consent screen.** They are written for someone
standing in a kitchen deciding whether to press Allow, so they say what the
add-on will be able to *do* — "post takes as you" — rather than naming an
endpoint or a verb. They are also what a client shows in its own connector list,
so they have to read correctly out of context.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Who the fan is: handle, club, standing, follower counts, and what they have
#: predicted. Reads nothing private — this is the public profile plus their own
#: predictions.
FAN_READ = "fan.read"
#: Posting, replying, quoting and reacting — anything that puts words on Fanmoot
#: under the fan's handle.
MOOT_WRITE = "moot.write"
#: Calling a scoreline before kickoff.
PREDICT_WRITE = "predict.write"
#: Following a club, a competition or another fan.
FAN_WRITE = "fan.write"


@dataclass(frozen=True, slots=True)
class Scope:
    """One scope, as three audiences need it.

    ``summary`` is the line on the consent screen, ``detail`` the smaller line
    under it, and ``name`` the string in the token.
    """

    name: str
    summary: str
    detail: str


#: Declared order is display order on the consent screen: read first, then the
#: writes in the order a fan is likely to care about them.
SCOPES: tuple[Scope, ...] = (
    Scope(
        name=FAN_READ,
        summary="See who you are on Fanmoot",
        detail=(
            "Your handle, your club, how long you have been signed to it, and your own predictions."
        ),
    ),
    Scope(
        name=MOOT_WRITE,
        summary="Post takes as you",
        detail="Post, reply, quote and react under your handle. Every take shows on your profile.",
    ),
    Scope(
        name=PREDICT_WRITE,
        summary="Call scorelines for you",
        detail="Predict a result before kickoff. Predictions are public once the match starts.",
    ),
    Scope(
        name=FAN_WRITE,
        summary="Follow clubs and fans for you",
        detail=(
            "Follow a club, a competition or another fan. It cannot unfollow or change your club."
        ),
    ),
)

SCOPES_BY_NAME: dict[str, Scope] = {scope.name: scope for scope in SCOPES}

#: Every scope this server issues, space-separated — the ``scopes_supported`` of
#: both discovery documents (`S4-2`, `S4-3`).
SUPPORTED = " ".join(scope.name for scope in SCOPES)

SUPPORTED_SET = frozenset(SCOPES_BY_NAME)

#: Which scope each authenticated tool needs (§5's table), read by the gate in
#: ``app.oauth.bearer`` to decide whether a ``tools/call`` may proceed.
#:
#: It is declared here, ahead of the tools themselves, on purpose: the gate has
#: to answer before the tool runs — a 401 is an HTTP response, not a tool
#: result — so the mapping cannot live on the tool. `S5` registers the
#: functions; this is the contract it registers against, and a test there
#: asserts the two agree.
#:
#: The twelve read tools are absent and that is the point: they take no
#: credential at all, and a server that challenged for one would be a server
#: nobody could ask the score without linking an account first.
TOOL_SCOPES: dict[str, str] = {
    "whoami": FAN_READ,
    "my_predictions": FAN_READ,
    "post_moot": MOOT_WRITE,
    "reply_to_moot": MOOT_WRITE,
    "react_to_moot": MOOT_WRITE,
    "predict_match": PREDICT_WRITE,
    "follow": FAN_WRITE,
}


def parse(raw: str | None) -> list[str]:
    """A space-separated scope parameter as a de-duplicated, ordered list.

    Order follows ``SCOPES`` rather than the request, so a token's ``scope``
    string is stable whatever order a client asked in — which makes a grant row
    comparable by string and a consent screen list things the same way twice.
    """
    if not raw:
        return []
    asked = {part for part in raw.replace(",", " ").split() if part}
    return [scope.name for scope in SCOPES if scope.name in asked]


def unknown(raw: str | None) -> list[str]:
    """The scopes in ``raw`` this server does not issue.

    Named separately from ``parse`` because RFC 6749 wants ``invalid_scope``
    rather than a silent narrowing: a client that asked for ``admin`` should be
    told no, not handed a token that quietly lacks it.
    """
    if not raw:
        return []
    asked = [part for part in raw.replace(",", " ").split() if part]
    return sorted({part for part in asked if part not in SUPPORTED_SET})


def describe(names: list[str]) -> list[Scope]:
    """The scope objects for these names, in display order."""
    wanted = set(names)
    return [scope for scope in SCOPES if scope.name in wanted]


def required_for(tool: str) -> str | None:
    """The scope ``tool`` needs, or None when it needs no credential."""
    return TOOL_SCOPES.get(tool)
