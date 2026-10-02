# Changelog

## Unreleased

- Tests for `apple_console.set_privacy`: category × purpose × protection expansion and the validation errors. Thanks @GreedyC (#14).

## 0.2.0

- Toolsets: `SHIPSTORES_TOOLSETS` (or `[server] toolsets` in `config.toml`) exposes only the tool groups you need: `apple`, `play` and/or `eas`. Core diagnostics stay on, and the default is still all 60 tools. Thanks @berkay-byte for the first community contribution (#11).
- CI now runs the unit tests.

## 0.1.0 — first public release

60 tools. Install with `uvx shipstores`.

- App Store Connect: builds, versions, listing, screenshots, subscriptions, age rating, categories, price, availability, privacy label, review submission and cancellation, TestFlight internal invites.
- App Review: read rejection messages, reply with attachments and resubmit the rejected version.
- Google Play: bundles, tracks, listing, screenshots, contact details, signing, App content declarations and data safety via console automation.
- Expo/EAS: start, poll, download and submit builds (iOS submit via altool).
