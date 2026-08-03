import pathlib
import unittest
from unittest import mock

import ai


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


class HostedProviderPresetTests(unittest.TestCase):
    def test_managed_gateway_is_a_visible_provider_preset(self):
        preset = next(item for item in ai.PRESETS if item["key"] == "caddie_hosted")
        self.assertEqual(preset["name"], "Caddie 托管 AI")
        self.assertEqual(preset["type"], "openai")
        self.assertEqual(preset["models"], ["deepseek-v4-flash"])
        self.assertTrue(preset["base_url"].startswith("https://"))
        self.assertTrue(preset["base_url"].endswith("/gateway/v1"))

    def test_managed_gateway_guide_explains_token_and_privacy(self):
        self.assertIn("caddie_hosted:{fit:", HTML)
        self.assertIn("个人 Token", HTML)
        self.assertIn("不保存请求与回复正文", HTML)

    @mock.patch("ai.set_model_profile")
    @mock.patch("ai.set_active")
    @mock.patch("ai.upsert_provider", return_value={"id": "caddie-hosted"})
    @mock.patch("ai.load_config", return_value={"providers": []})
    def test_automatic_install_binds_profiles_to_string_provider_id(
        self, _load_config, _upsert_provider, _set_active, set_model_profile
    ):
        ai.install_hosted_provider()

        self.assertEqual(set_model_profile.call_count, len(ai.MODEL_PROFILE_DEFS))
        for call in set_model_profile.call_args_list:
            self.assertEqual(call.args[1], "caddie-hosted")


if __name__ == "__main__":
    unittest.main()
