# The Alexa+ add-on manifest

> **This manifest has never been deployed or validated.** The Alexa+ MCP
> Toolkit, which is the only thing that can deploy or validate it, is in
> preview with select partners. Amazon confirmed on the hackathon forum that it
> is not available to participants. Nothing here has been run through
> `alexa-ai`, certified, or published, and Fanmoot is not an Alexa+ add-on.

[`addon.json`](addon.json) is written field by field to the schema in Amazon's
[MCP QuickStart](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-quickstart.html).
It is here to show that this server meets Alexa's
[account-linking contract](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-account-linking.html).
It does not claim a shipped add-on. The only check it has had is
`tests/test_addon.py`, which tests it against the limits that page states.

## What it says, and why

| Field | Value | Why |
|---|---|---|
| `integrations[0]…uri` | `https://mcp.fanmoot.com/mcp` | Alexa requires the protected-resource metadata's `resource` to match this URL exactly, and `resource` is the MCP endpoint (`Settings.resource_uri`), not the bare host. The test holds the two together. |
| `distributionCountries`, locale | `US`, `en-US` | The toolkit docs support nothing else today. |
| `name.spokenForm` | IPA `ˈfænmuːt` | Optional. Included so "Fanmoot" is not read as "fan moat". |
| `examplePhrases` | four | Drawn from the demo's scenarios (`W4-2`, `W4-3`, `W4-4`, `W4-6`). The demo reuses them word for word as its suggested prompts. |
| `privacyPolicyUrl`, `termsOfUseUrl` | `/privacy`, `/terms` | Served by this server (`app/legal.py`). |
| `icons.light`, `carouselImages` | `/static/brand/` | The schema requires all six icon sizes and at least one 600×900 carousel image. All seven are resized from Fanmoot's existing 512 px mark. They are placeholders to satisfy the schema, not store artwork. `icons.dark` and `bannerImages` are optional and left empty. |

## What account linking would need

None of this can be done today. It is written down so the gap is known when
the toolkit opens.

1. Deploy the add-on and take its client ID and redirect URIs from the Alexa+
   Developer Hub.
2. Seed them through `FMCP_OAUTH_CLIENTS`. Register **every** redirect URI, not
   one, because Alexa's differs by the region a device was registered in. See
   [Re-register Alexa's clients](../docs/runbook.md#re-register-alexas-clients).
3. Run `alexa-ai configure-account-linking --addon-id <id> --stage development --client-id <id>`.

The server side is already built and tested: PKCE S256 only, exact
redirect-URI matching with several URIs per client, `resource` on authorize and
token, refresh tokens always, and a 401 challenge that starts linking. See
**Compliance** in the [main README](../README.md#compliance).
