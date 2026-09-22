"""The Fanmoot API client.

Everything this server knows about football arrives through here, over HTTPS,
from ``fanmoot.com/v1`` (HAD-1). There is no SQLAlchemy model of Fanmoot's
domain in this repo and no share of its connection pool — which is what makes
`fanmoot-mcp` a genuine third-party integration rather than a second front end
bolted onto one database, and what makes it auditable on its own: everything it
can possibly do, a fan could do through the public API.
"""
