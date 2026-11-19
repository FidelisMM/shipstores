# Flutter: build, upload, then submit

shipstores accepts built IPA/AAB files; you do not need Expo or EAS. Configure
[store credentials](../README.md#credentials) first. Use your own registered
bundle ID/package name and increment the build number before another upload.
Signing keys, keystores and store credentials stay outside the repository.

## Build in your Flutter project

For iOS, use macOS with Xcode and release signing configured:

```sh
flutter build ipa --release
# If your project uses an existing export configuration, use this instead:
flutter build ipa --release --export-options-plist=/path/to/ExportOptions.plist
```

The archive is in `build/ios/archive/`; choose the actual `.ipa` written under
`build/ios/ipa/`. Do not upload the `.xcarchive` directory. See Flutter's
[iOS release guide](https://docs.flutter.dev/deployment/ios) for signing and
export configuration.

For Android, configure release/upload signing, then build:

```sh
flutter build appbundle --release
```

Find the `.aab` under `build/app/outputs/bundle/release/` (the current Flutter
guide names it `app.aab`; inspect your build output, especially with flavours).
See Flutter's [Android release guide](https://docs.flutter.dev/deployment/android).
An APK is not an AAB. Add your project's flavour/target options when needed.

## iOS: upload and attach the processed build

The following are **MCP tool calls**, not shell commands. Replace the fictional
paths and IDs; paths must be accessible to the machine running shipstores.

```json
{"tool":"apple_upload_build","arguments":{"ipa_path":"/absolute/path/to/flutter-app/build/ios/ipa/your-app.ipa","platform":"ios"}}
```

`apple_upload_build` invokes `xcrun altool`, so the upload host needs Apple's
command-line tooling too. Uploading does not submit the app for review.

Use `apple_list_apps` to identify the app, then `apple_list_builds` until the
chosen build's `processingState` is `VALID`. Create/find an App Store version
with `apple_create_version` / `apple_list_versions`, then call
`apple_attach_build(version_id, build_id)`. A build ID and a version ID are
different; do not pass the app ID in either position.

Continue with the README's [New iOS app workflow](../README.md#workflows):
listing, app information, age rating, price/availability, privacy, screenshots
and review details. Check the target version and metadata with the owner before
`apple_submit_for_review(version_id)`: **this sends an external review submission**.
App creation or incomplete console forms still need the browser steps shown there.

## Android: confirm the track before uploading

**`play_upload_bundle` commits a release, not just a file upload.** Confirm the
package, track and status with the owner first. This example deliberately uses
an internal-track **draft** rather than releasing to testers or production:

```json
{"tool":"play_upload_bundle","arguments":{"package_name":"com.example.flutterdemo","aab_path":"/absolute/path/to/flutter-app/build/app/outputs/bundle/release/app.aab","track":"internal","status":"draft"}}
```

Retain the returned `version_code`. Complete the listing with
`play_update_listing`, screenshots with `play_upload_screenshots`, contact details
and the required `play_content_*` / `play_data_safety_*` declarations. These
tools can commit external changes too; confirm their inputs before calling.

Use `play_track_status` and `play_submission_status` to inspect the result.
Follow [the shared Android workflow](../README.md#workflows); finishing declarations
does not itself release the draft. Confirm before `play_promote_release` or
`play_submit_for_review`. A production promotion is public; do not infer permission
from a successful build or upload. First-time setup and account-specific review
requirements can still require Play Console interaction.

No Flutter builds, signed uploads or live submissions are performed by this guide.
