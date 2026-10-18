# Native and bare React Native builds (no EAS)

shipstores uploads existing `.ipa` and `.aab` files; it does not need Expo to
produce them. Build and sign with your project's normal tools, then use the same
listing and review tools as the [README workflows](../README.md#workflows).

Before starting, configure the [store credentials](../README.md#credentials).
Local iOS builds also need macOS, Xcode, an Apple Developer team and working
distribution signing. Android needs your project's JDK/Android SDK and a release
signing configuration using its upload key. Keep keys and signing passwords out
of the repository. An App Store Connect API key authorizes uploads; it does not
replace the signing certificate and provisioning profile used by Xcode.

## iOS: archive, then export an IPA

Run from the directory containing the Xcode workspace. For bare React Native,
start at the project root, install the project's JavaScript and CocoaPods
dependencies as usual, then `cd ios`. For a native project, use its workspace
directory instead. Replace `MyApp`, the workspace and the team ID below with
your own values; the scheme must be shared and support archiving.

```bash
cd ios  # bare React Native, from the project root
xcodebuild \
  -workspace MyApp.xcworkspace \
  -scheme MyApp \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath "$PWD/build/MyApp.xcarchive" \
  archive
```

If the native project has no workspace, replace `-workspace MyApp.xcworkspace`
with `-project MyApp.xcodeproj`. Configure signing in Xcode first. Automatic
signing may need an authenticated developer account and
`-allowProvisioningUpdates`; manual signing needs the correct profiles for the
app and any extensions.

Save this minimal automatic-signing example as `ExportOptions.plist` alongside
the workspace. Replace `YOURTEAMID` with the signing team's ID:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key>
  <string>app-store-connect</string>
  <key>destination</key>
  <string>export</string>
  <key>signingStyle</key>
  <string>automatic</string>
  <key>teamID</key>
  <string>YOURTEAMID</string>
</dict>
</plist>
```

Use the export options appropriate to your installed Xcode (`xcodebuild -help`
lists supported keys and methods); older versions may call the distribution
method `app-store`. For manual signing, export a plist from Xcode's Organizer
with the provisioning-profile mapping rather than copying the automatic example.

```bash
xcodebuild -exportArchive \
  -archivePath "$PWD/build/MyApp.xcarchive" \
  -exportPath "$PWD/build/export" \
  -exportOptionsPlist "$PWD/ExportOptions.plist"
```

The export directory contains the signed `.ipa`. Ask the MCP client to call
`apple_upload_build(ipa_path="/absolute/path/to/ios/build/export/MyApp.ipa")`,
using the actual exported filename. Uploading is not submitting for review.

Then:

1. Use `apple_list_apps` for the `app_id` (create the app record first if needed).
2. Poll `apple_list_builds(app_id=...)` until the uploaded build is `VALID`.
3. Create or select the store version with `apple_create_version` /
   `apple_list_versions`, then `apple_attach_build(version_id=..., build_id=...)`.
4. Complete the listing, screenshots, privacy, age rating and review details
   using the [iOS workflow](../README.md#workflows).
5. Only after confirmation, call `apple_submit_for_review(version_id=...)`.

## Android: build a signed release bundle

For bare React Native, run from the project root. A native Android project
should run the Gradle wrapper from its own root (omit `cd android` if already
there):

```bash
cd android
./gradlew bundleRelease
```

For the usual `app` module, the result is
`app/build/outputs/bundle/release/app-release.aab`. Flavors, module names and
custom Gradle configuration can change both the task (for example,
`bundleProductionRelease`) and output path. Verify release signing is configured
before uploading; a debug-signed build is not a substitute.

Ask the client to call
`play_upload_bundle(package_name="com.example.myapp", aab_path="/absolute/path/to/android/app/build/outputs/bundle/release/app-release.aab", track="internal")`
with your package and actual file. **Confirm the track first:** the default
`status="completed"` commits the edit and releases to that track's testers;
`track="production"` can publish to users. This is not a file-only upload.

Continue with `play_update_listing`, screenshots and the `play_content_*` /
`play_data_safety_fill` tools from the [Android workflow](../README.md#workflows).
Check `play_submission_status` for outstanding declarations or review work;
when ready and confirmed, use `play_submit_for_review`. Promotion with
`play_promote_release` is a separate external action: do not promote an internal
test release to production until the user explicitly approves it.

These are local build instructions and MCP tool names, not a shell script that
submits automatically. The build must match the registered bundle ID/package,
and each upload needs a new build number/version code.

References: [Apple's command-line build/export guide](https://developer.apple.com/library/archive/technotes/tn2339/_index.html),
[Xcode archive export files](https://help.apple.com/xcode/mac/current/en.lproj/deva1f2ab5a2.html),
and [React Native's signed Android bundle guide](https://reactnative.dev/docs/signed-apk-android).
