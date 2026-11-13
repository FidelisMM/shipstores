# Changelog

## Unreleased

- Tests for `apple_console.set_privacy`: category × purpose × protection expansion and the validation errors. Thanks @GreedyC (#14).
- Tests for `with_retry` in `apple.py` and `play.py`: transient failures, giving up, 5xx vs 4xx on Play and the backoff. Thanks @CodeByPeace (#21).
- Tests for `play_console.parse_json_tail` and `form_url`. Thanks @vgvr0 (#27).
- App Review: `apple_review_messages` no longer returns an empty list when the App Store Connect session expired (401 on `/iris`); it reports the expired session instead, and fills in who sent each message and the reasons per message.
- Expired Apple sessions are detected when the console redirects to its own `/login?authResult=FAILED`, and `store_browser_session` checks the internal API instead of trusting the URL 4 s after load.
- When the dedicated profile is logged out, console automations retry in the browser that browser-harness drives by default before asking for a 2FA login.
- `apple_cancel_submission` also cancels a rejected submission (`UNRESOLVED_ISSUES`) and waits for `CANCELING` to finish, which unlocks subscription images and metadata for editing.

## 0.2.0

- Toolsets: `SHIPSTORES_TOOLSETS` (or `[server] toolsets` in `config.toml`) exposes only the tool groups you need: `apple`, `play` and/or `eas`. Core diagnostics stay on, and the default is still all 60 tools. Thanks @berkay-byte for the first community contribution (#11).
- CI now runs the unit tests.

## 0.1.0 — first public release

60 tools. Install with `uvx shipstores`.

- App Store Connect: builds, versions, listing, screenshots, subscriptions, age rating, categories, price, availability, privacy label, review submission and cancellation, TestFlight internal invites.
- App Review: read rejection messages, reply with attachments and resubmit the rejected version.
- Google Play: bundles, tracks, listing, screenshots, contact details, signing, App content declarations and data safety via console automation.
- Expo/EAS: start, poll, download and submit builds (iOS submit via altool).
