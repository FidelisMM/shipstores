# Subscriptions and review submissions on App Store Connect

What it cost to learn while shipping an app with auto-renewable subscriptions.

**The metadata bot rejects for a missing EULA without looking at the binary.** An app with an auto-renewable subscription needs a working Terms of Use link in the store listing; without it you get `3.1.2 Business: Payments - Subscriptions` minutes after submitting. No new build needed: `apple_update_listing` with a subscription block in the description (plan name, price, period, renewal, how to cancel) plus Apple's standard EULA link fixes it.

```
Terms of Use (EULA): https://www.apple.com/legal/internet-services/itunes/dev/stdeula/
```

A custom EULA is the other path: instead of the link, the text goes in the License Agreement field (`/v1/apps/{id}/endUserLicenseAgreement`, `null` when using Apple's standard one).

**A version lives in one submission at a time.** Resubmitting a rejected version returns `409 ITEM_PART_OF_ANOTHER_SUBMISSION` while the old submission exists. Cancel with `PATCH /v1/reviewSubmissions/{id}` and `{"canceled": true}` (`apple_cancel_submission`); the state stays `CANCELING` for ~15 s before the version is free.

**Cancelling the submission burns the subscription with it, and the API can't bring it back.** The `subscriptionVersion` becomes `DEVELOPER_REJECTED`, and from then on:

- `POST /v1/subscriptionSubmissions` answers *"has no pending version for submission"*;
- editing localization, price or `reviewNote` does **not** create a new version;
- adding it to a submission (the valid relationship is `subscriptionVersion`, not `subscription`) fails with `STATE_ERROR.ENTITY_STATE_INVALID`.

Only the console recreates the subscription items (App Store Connect → Distribution → App Review), and they land in a separate **draft** submission. That draft won't submit alone — *"add an app version for the selected platform"*: in-app purchases always travel with a version. If the version is already in another submission, regroup:

1. cancel the submission that only has the version and wait for it to leave `CANCELING`;
2. `POST /v1/reviewSubmissionItems` with `appStoreVersion` pointing at the draft;
3. `PATCH` the draft with `{"submitted": true}`.

You end with one submission holding three items: the version, the subscription group and the subscription.

**`apple_submit_for_review` sends only the version.** Subscriptions need their own `subscriptionVersion` item in the same `reviewSubmission`, created before `submitted: true`.
