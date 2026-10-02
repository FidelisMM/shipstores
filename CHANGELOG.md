# Changelog

## Unreleased

- `apple_reply_review` is now two-step: the default call validates and returns a preview without sending; it sends only with the `confirm_token` from that preview, a hash of the exact text, attachments and submission, so nothing can change between preview and send, and a token sends only once (#12). Suggested by u/QuanTradin on r/mcp.
- Tools marked "External action" now carry MCP tool annotations (`destructiveHint`, `openWorldHint`) so clients can gate them.

## 0.2.0

- Toolsets: `SHIPSTORES_TOOLSETS` (or `[server] toolsets` in `config.toml`) exposes only the tool groups you need: `apple`, `play` and/or `eas`. Core diagnostics stay on, and the default is still all 60 tools. Thanks @berkay-byte for the first community contribution (#11).
- CI now runs the unit tests.

## 0.1.0 — first public release

60 tools. Install with `uvx shipstores`.

- App Store Connect: builds, versions, listing, screenshots, subscriptions, age rating, categories, price, availability, privacy label, review submission and cancellation, TestFlight internal invites.
- App Review: read rejection messages, reply with attachments and resubmit the rejected version.
- Google Play: bundles, tracks, listing, screenshots, contact details, signing, App content declarations and data safety via console automation.
- Expo/EAS: start, poll, download and submit builds (iOS submit via altool).
