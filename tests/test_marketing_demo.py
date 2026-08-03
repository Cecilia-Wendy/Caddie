from fastapi.testclient import TestClient

import server


def test_marketing_demo_uses_real_product_shell_and_demo_controller():
    response = TestClient(server.app).get("/marketing-demo")

    assert response.status_code == 200
    assert 'id="marketingDemoRoot"' in response.text
    assert "MARKETING_DEMO_MODE" in response.text
    assert "MARKETING_NATIVE_STAGES" in response.text
    assert "performMarketingStep" in response.text
    assert "data-demo-target=\"open-career-profile\"" in response.text
    assert "data-demo-target=\"open-target-job\"" in response.text
    assert "data-demo-target=\"open-interview-library\"" in response.text
    assert "data-demo-target=\"add-experience\"" in response.text
    assert "data-demo-target=\"add-project\"" in response.text
    assert "data-demo-target=\"add-followup\"" in response.text
    assert "resetMarketingStoryData" in response.text
    assert "用户增长漏斗分析" in response.text
    assert "MARKETING_AGENT_PROMPT" in response.text
    assert "showMarketingAgentReply" in response.text
    assert "showMarketingAgentProposal" in response.text
    assert "prepareMarketingTrackStory" in response.text
    assert "通过数据分析和用户研究" in response.text
    assert "AI 产品质量指标" in response.text
    assert "目标岗位能力模型" in response.text
    assert "从逐字稿进入结构化答卷" in response.text
    assert ".iv-question-detail header" in response.text
    assert "确认考察能力、关联经历与追问角度" in response.text
    assert "关闭逐题复盘，继续完成回流" in response.text
    assert "prepareMarketingFeedbackStory" in response.text
    assert "面试真题已经回流到刚才的项目" in response.text
    assert "查看完整复盘" in response.text
    assert "打开教练工作台继续讨论" in response.text
    assert "采用到左侧继续编辑" in response.text
    assert "更新后的答案已回流并保留在项目中" in response.text
    assert "followupFilter:'all'" in response.text
    assert "expView:'overview'" in response.text
    assert "像聊天一样描述这段经历" in response.text
    assert "确认后才真正写入职业档案" in response.text
    assert "建档完成率为什么会提升？" in response.text
    assert "if(step.fill)" in response.text


def test_marketing_demo_boot_keeps_product_initialization():
    response = TestClient(server.app).get("/marketing-demo")

    assert "if(initMarketingDemo())return" in response.text
    assert "return false;" in response.text
    assert "独立虚构数据" in response.text
    assert "target.click()" in response.text
    assert "marketing-demo-pointer" in response.text
    assert ".project-real-questions .follow-top" in response.text
    assert "fill:'#ivCoachMessage'" in response.text
    assert "STATE.followupOpen=origin.questionId" in response.text
    assert "更新后的答案已回流并保留在项目中" in response.text


if __name__ == "__main__":
    test_marketing_demo_uses_real_product_shell_and_demo_controller()
    test_marketing_demo_boot_keeps_product_initialization()
    print("MARKETING_DEMO_TESTS_OK")
