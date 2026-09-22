"""The MCP server core — spec ``2025-11-25``, Streamable HTTP.

This package is the protocol half of the service. The football half lives in
``app/fanmoot`` and knows nothing about MCP; the tools that join the two arrive
with ``S3``. Splitting them that way is what keeps the transport testable
without a fixture of Fanmoot's API, and the API client testable without a
JSON-RPC session.

What is here, and why each piece exists rather than being left to the SDK:

* ``server``     — the ``FastMCP`` instance: identity, instructions, and the
                   error mapping every tool inherits (``S2-1``, ``S2-6``).
* ``instructions``— what the model is told this server is for, written to be
                   spoken (``S2-1``, HAD-7).
* ``transport``  — the Streamable HTTP mount, its session manager and lifespan
                   (``S2-2``).
* ``security``   — ``Origin`` validation, which is a ``2025-11-25`` MUST and a
                   DNS-rebinding defence (``S2-3``).
* ``events``     — the event store behind ``Last-Event-ID`` resumability
                   (``S2-2``).
* ``frames``     — every JSON-RPC frame, in and out, as a log line (``S2-5``).
* ``telemetry``  — one ``tool_calls`` record per call (``S2-5``, HAD-12).
* ``errors``     — an exception turned into a sentence a model can read aloud.
"""
