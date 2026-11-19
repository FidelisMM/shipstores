import unittest

from shipstores import data_safety

ACM = "PSL_SUPPORTED_ACCOUNT_CREATION_METHODS"
OUTSIDE = "PSL_OUTSIDE_APP_ACCOUNT_TYPES"


def _checked(responses: dict[tuple[str, str], str], question: str) -> set[str]:
    return {rid for (qid, rid), v in responses.items() if qid == question and v == "true"}


class AccountQuestionsTests(unittest.TestCase):
    def test_default_is_password_and_oauth_and_leaves_external_accounts_alone(self) -> None:
        r = data_safety.build_responses({})
        self.assertEqual(_checked(r, ACM), {"PSL_ACM_USER_ID_PASSWORD", "PSL_ACM_OAUTH"})
        # the other methods are written empty, so the exported template's checks do not stick
        self.assertEqual(r[(ACM, "PSL_ACM_USER_ID_OTHER_AUTH")], "")
        self.assertNotIn(("PSL_HAS_OUTSIDE_APP_ACCOUNTS", ""), r)

    def test_aliases_and_response_ids_mix(self) -> None:
        r = data_safety.build_responses({}, account_methods=["OAuth", "PSL_ACM_USER_ID_OTHER_AUTH"])
        self.assertEqual(_checked(r, ACM), {"PSL_ACM_OAUTH", "PSL_ACM_USER_ID_OTHER_AUTH"})

    def test_no_sign_up_with_external_login(self) -> None:
        r = data_safety.build_responses(
            {},
            account_deletion_url="https://example.com/delete",
            account_methods="none",
            external_accounts=True,
            external_account_types=["other"],
            external_account_description="Created by the provider.",
        )
        self.assertEqual(_checked(r, ACM), {"PSL_ACM_NONE"})
        self.assertNotIn(("PSL_ACCOUNT_DELETION_URL", ""), r)
        self.assertEqual(r[("PSL_DATA_DELETION_URL", "")], "https://example.com/delete")
        self.assertEqual(r[("PSL_HAS_OUTSIDE_APP_ACCOUNTS", "")], "true")
        self.assertEqual(_checked(r, OUTSIDE), {"PSL_OUTSIDE_APP_ACCOUNT_TYPE_OTHER"})
        self.assertEqual(r[("PSL_OUTSIDE_APP_ACCOUNT_TYPE_SPECIFY", "")], "Created by the provider.")

    def test_other_method_writes_its_description(self) -> None:
        r = data_safety.build_responses({}, account_methods=["other"], account_creation_other="Invite code")
        self.assertEqual(r[("PSL_ACM_SPECIFY", "")], "Invite code")

    def test_external_accounts_false_clears_the_types(self) -> None:
        r = data_safety.build_responses({}, external_accounts=False)
        self.assertEqual(r[("PSL_HAS_OUTSIDE_APP_ACCOUNTS", "")], "false")
        self.assertEqual(_checked(r, OUTSIDE), set())

    def test_invalid_combinations(self) -> None:
        for kwargs in (
            {"account_methods": ["none", "oauth"]},
            {"account_methods": ["other"]},
            {"account_methods": ["unknown"]},
            {"external_accounts": True},
            {"external_account_types": ["other"]},
            {"external_accounts": True, "external_account_types": ["other"]},
            {"external_accounts": False, "external_account_types": ["enterprise"]},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    data_safety.build_responses({}, **kwargs)


if __name__ == "__main__":
    unittest.main()
