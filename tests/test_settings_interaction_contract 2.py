from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "caddie-ui.css").read_text(encoding="utf-8")


class SettingsInteractionContractTests(unittest.TestCase):
    def test_settings_dialogs_render_above_full_screen_workspace(self):
        self.assertIn(".settings-workspace", CSS)
        self.assertIn("body:has(.app.settings-mode) > .mask { z-index: 400; }", CSS)

    def test_hidden_settings_actions_are_really_hidden(self):
        self.assertIn("[hidden] { display: none !important; }", CSS)
        self.assertIn("add.hidden=id!=='settings-ai'", HTML)

    def test_settings_search_and_primary_actions_are_wired(self):
        required = (
            'oninput="filterSettingsNav(this.value)"',
            'onclick="openProvModal()"',
            "onclick=\"openAgentMigration()\"",
            "onclick=\"loadAgentIntegrations()\"",
            "onclick=\"loadUpdateSettings(true)\"",
            "onclick=\"loadPrivacySettings(true)\"",
        )
        for hook in required:
            self.assertIn(hook, HTML)

    def test_external_agent_page_explains_purpose_and_onboarding(self):
        self.assertIn('class="agent-guide"', HTML)
        self.assertIn("外部 Agent 是什么", HTML)
        self.assertIn("怎么开始", HTML)
        self.assertIn("选择接入方式", HTML)
        self.assertIn("数据与权限", HTML)
        self.assertIn("可选迁移工具", HTML)
        self.assertIn(".agent-howto", CSS)

    def test_external_agent_catalog_has_brands_prices_and_official_links(self):
        for icon in ("codex", "claude", "workbuddy"):
            self.assertIn(f"/static/agent-icons/{icon}.png", HTML)
        self.assertIn("套餐价格 ↗", HTML)
        self.assertIn("官网与下载 ↗", HTML)
        self.assertIn("配置原文", HTML)
        self.assertIn(".agent-integration-plan", CSS)

    def test_external_agent_launch_allowlist_hides_deferred_clients(self):
        self.assertIn("new Set(['codex','claude_desktop','workbuddy'])", HTML)
        self.assertIn("clients:visibleClients", HTML)
        self.assertNotIn("QoderWork", HTML)
        self.assertNotIn("Kimi Work", HTML)
        self.assertNotIn("TRAE Work", HTML)

    def test_external_agent_manual_covers_configuration_verification_and_recovery(self):
        self.assertIn('id="agentManual"', HTML)
        self.assertIn('const AGENT_SETUP_GUIDES={', HTML)
        self.assertIn("renderAgentSetupGuide", HTML)
        self.assertIn("openAgentSetupGuide", HTML)
        self.assertIn("怎样才算验证成功？", HTML)
        self.assertIn("没有成功时，按顺序检查", HTML)
        self.assertIn("verify_connection", HTML)
        self.assertIn("工具数量不是 0", HTML)
        self.assertIn(".agent-manual-layout", CSS)

    def test_provider_capabilities_use_clickable_choice_grid(self):
        self.assertIn('class="modal provider-modal"', HTML)
        self.assertIn('class="field provider-capability-field"', HTML)
        self.assertIn('.capability-picker{display:grid;', CSS)
        self.assertIn('.capability-picker label:has(input:checked)', CSS)

    def test_provider_guide_has_official_key_and_pricing_links(self):
        self.assertIn('id="providerGuide"', HTML)
        self.assertIn('const PROVIDER_GUIDES={', HTML)
        for provider in ("anthropic", "openai", "deepseek", "qwen", "doubao", "moonshot", "zhipu", "siliconflow", "ollama"):
            self.assertIn(f"  {provider}:{{", HTML)
        self.assertIn("申请 API Key", HTML)
        self.assertIn("查看官方价格", HTML)
        self.assertIn("价格核对：2026-07-31", HTML)

    def test_model_selection_report_is_available_inside_settings(self):
        self.assertIn('class="model-guide-section"', HTML)
        self.assertIn('id="modelGuideDetails"', HTML)
        self.assertIn("toggleModelGuideDetails", HTML)
        self.assertIn("openProvModalWithPreset", HTML)
        self.assertIn("国内默认", HTML)
        self.assertIn("预算怎么估算？", HTML)
        self.assertIn("API 的代价与常见问题", HTML)
        self.assertIn("连续失败自动熔断", HTML)
        self.assertIn(".model-plan-grid", CSS)


if __name__ == "__main__":
    unittest.main()
