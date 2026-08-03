import pathlib
import unittest

import ai


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


class HostedProviderPresetTests(unittest.TestCase):
    def test_managed_gateway_is_a_visible_provider_preset(self):
        preset = next(item for item in ai.PRESETS if item["key"] == "caddie_hosted")
        self.assertEqual(preset["name"], "Caddie 托管 AI")
        self.assertEqual(preset["type"], "openai")
        self.assertEqual(preset["models"], ["deepseek-chat"])
        self.assertTrue(preset["base_url"].startswith("https://"))
        self.assertTrue(preset["base_url"].endswith("/gateway/v1"))

    def test_managed_gateway_guide_explains_token_and_privacy(self):
        self.assertIn("caddie_hosted:{fit:", HTML)
        self.assertIn("个人 Token", HTML)
        self.assertIn("不保存请求与回复正文", HTML)


if __name__ == "__main__":
    unittest.main()
