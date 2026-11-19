# Subscriptions and review submissions on App Store Connect

What it cost to learn while shipping an app with auto-renewable subscriptions.

**The metadata bot rejects for a missing EULA without looking at the binary.** An app with an auto-renewable subscription needs a working Terms of Use link in the store listing; without it you get `3.1.2 Business: Payments - Subscriptions` minutes after submitting. No new build needed: `apple_update_listing` with a subscription block in the description (plan name, price, period, renewal, how to cancel) plus Apple's standard EULA link fixes it.

```
Terms of Use (EULA): https://www.apple.com/legal/internet-services/itunes/dev/stdeula/
```

A custom EULA is the other path: instead of the link, the text goes in the License Agreement field (`/v1/apps/{id}/endUserLicenseAgreement`, `null` when using Apple's standard one).

**A version lives in one submission at a time.** Resubmitting a rejected version returns `409 ITEM_PART_OF_ANOTHER_SUBMISSION` while the old submission exists. Cancel with `PATCH /v1/reviewSubmissions/{id}` and `{"canceled": true}` (`apple_cancel_submission`); the state stays `CANCELING` for ~15 s before the version is free.

**Cancelling the submission burns the subscription with it, and the public API can't bring it back.** The `subscriptionVersion` becomes `DEVELOPER_REJECTED`, and from then on:

- `POST /v1/subscriptionSubmissions` answers *"has no pending version for submission"*;
- editing localization, price or `reviewNote` does **not** create a new version;
- adding it to a submission (the valid relationship is `subscriptionVersion`, not `subscription`) fails with `STATE_ERROR.ENTITY_STATE_INVALID`.

Only the console recreates the subscription items: the "Add for Review" button on the subscription (and on the group) creates the new version and attaches it to an open draft. `apple_submit_for_review` does that for you when the API answers "no pending version" (see below). Done by hand from App Store Connect → Distribution → App Review, the items land in a separate **draft** submission. That draft won't submit alone — *"add an app version for the selected platform"*: in-app purchases always travel with a version. If the version is already in another submission, regroup:

1. cancel the submission that only has the version and wait for it to leave `CANCELING`;
2. `POST /v1/reviewSubmissionItems` with `appStoreVersion` pointing at the draft;
3. `PATCH` the draft with `{"submitted": true}`.

You end with one submission holding three items: the version, the subscription group and the subscription.

**`apple_submit_for_review` takes the subscriptions along.** By default (`include_subscriptions=True`) it uses the app's open draft submission (or creates one), adds the version, then `POST /v1/subscriptionSubmissions` for every subscription in `READY_TO_SUBMIT` and `POST /v1/subscriptionGroupSubmissions` for their group, and only then sends `submitted: true`. Order matters: an app's first subscription is only accepted with the version already in the draft, and without the group the submission gets stuck. Before submitting it counts the draft's items; if one is missing it stops without submitting (sending the version alone gets a 2.1 rejection for an in-app purchase that was not submitted). A group failure becomes a warning, since a group that already went through review may refuse a new item. When the API answers "no pending version" (after a canceled submission), the item goes in through the console's "Add for Review" button, which needs a logged-in dedicated browser.

A known refusal: with plans priced in a single territory, the API answered `409 IAP_SUBMISSION_NOT_ALLOWED_MISSING_PRICING_DATA` ("missing pricing for the following territories") and the group `SUBSCRIPTION_GROUP_SUBMISSION_NOT_ALLOWED`, while the console accepted the same plans. The tool then stops before submitting. Pricing every territory through the API (`apple_set_subscription_price`) should unblock it but has not been verified; otherwise use "Add for Review" on the version, each plan and the group in the console, always picking the same draft, then submit the draft.
