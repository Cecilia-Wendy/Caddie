import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import hosted_access


class HostedAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_data = hosted_access.DATA_DIR
        self.original_state = hosted_access.STATE_PATH
        hosted_access.DATA_DIR = Path(self.temp.name)
        hosted_access.STATE_PATH = hosted_access.DATA_DIR / "hosted_access.json"

    def tearDown(self):
        hosted_access.DATA_DIR = self.original_data
        hosted_access.STATE_PATH = self.original_state
        self.temp.cleanup()

    @patch("hosted_access._security")
    @patch("hosted_access.requests.post")
    def test_activation_stores_credential_in_keychain_not_state(self, post, security):
        security.return_value = Mock(returncode=0, stdout="")
        post.return_value = Mock(
            status_code=200,
            json=lambda: {
                "credential": "caddie_secret_device_token",
                "account_id": "acct_test",
                "expires_at": "2026-08-16T00:00:00+00:00",
            },
        )
        hosted_access.activate("ABCD-EFGH-IJKL")
        state = hosted_access.STATE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("caddie_secret_device_token", state)
        command = security.call_args_list[-1].args
        self.assertIn("add-generic-password", command)
        self.assertIn("caddie_secret_device_token", command)

    def test_uses_unified_caddie_identity(self):
        self.assertEqual(hosted_access.KEYCHAIN_SERVICE, "app.caddie.gateway")
        self.assertEqual(self.original_data.name, ".caddie")


if __name__ == "__main__":
    unittest.main()
