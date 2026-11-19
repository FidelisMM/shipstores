# Changelog

## Unreleased

- New `apple_wait_build`: blocks until a build leaves PROCESSING (VALID, FAILED or INVALID) and returns its `build_id`, ready for `apple_attach_build`. A build not listed yet right after the upload counts as waiting.
- Tests for `apple_console.set_privacy`: category × purpose × protection expansion and the validation errors. Thanks @GreedyC (#14).
- Tests for `with_retry` in `apple.py` and `play.py`: transient failures, giving up, 5xx vs 4xx on Play and the backoff. Thanks @CodeByPeace (#21).
- Tests for `play_console.parse_json_tail` and `form_url`. Thanks @vgvr0 (#27).
- Tests for `data_safety.build_responses` and `fill_csv`. Thanks @GreedyC (#28).
- Tests for `apple_console.set_availability`: territory normalization and the new-territory flag. Thanks @GreedyC (#29).
- Docs: [native Xcode and Gradle builds](docs/native-builds.md) and a [Flutter workflow](docs/flutter.md). Thanks @GreedyC (#30, #31).
- App Review: `apple_review_messages` no longer returns an empty list when the App Store Connect session expired (401 on `/iris`); it reports the expired session instead, and fills in who sent each message and the reasons per message.
- Expired Apple sessions are detected when the console redirects to its own `/login?authResult=FAILED`, and `store_browser_session` checks the internal API instead of trusting the URL 4 s after load.
- When the dedicated profile is logged out, console automations retry in the browser that browser-harness drives by default before asking for a 2FA login.
- `apple_cancel_submission` also cancels a rejected submission (`UNRESOLVED_ISSUES`) and waits for `CANCELING` to finish, which unlocks subscription images and metadata for editing.
- `apple_reply_review` is now two-step: the default call validates and returns a preview without sending; it sends only with the `confirm_token` from that preview, a hash of the exact text, attachments and submission, so nothing can change between preview and send, and a token sends only once (#12). Suggested by u/QuanTradin on r/mcp.
- Console automation runs directly on the dedicated Chrome over CDP (`cdp_direct.py`, same helpers as browser-harness), so browser-harness is no longer required when the dedicated profile exists. The tab a call leaves open survives to the next call (`play_content_open` → `play_content_answer` → `play_content_save`), a navigation that hangs is retried once on a fresh tab, and a script's `SystemExit` becomes a handled error instead of taking the server down. `websockets` is now a direct dependency.
- The Apple console login survives Chrome restarts: the consoles' cookies are saved in the dedicated profile (`console-session.json`, mode 600) and restored when Chrome starts, since Apple keeps the login in a session cookie that Chrome drops on close. The dedicated Chrome goes back to headless on its own once a login is done (`store_browser_session` closes a leftover login window with `Browser.close`), and `relogin` adds login tabs to an open window instead of killing it.
- The logged-out detector only looks at the page URL on its own line, so a login link in a footer or iframe no longer reopens Chrome with a window.
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
