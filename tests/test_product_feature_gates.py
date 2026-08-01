import sys
import os
from pathlib import Path

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import product_features
import server


def test_career_growth_journal_is_visible_but_locked():
    feature = product_features.get_feature("career_growth_journal")
    assert feature["status"] == "coming_soon"
    assert feature["allow_routing"] is False
    assert feature["allow_task_creation"] is False
    assert feature["label"] == "职业成长日志"


def test_autumn_opportunity_library_is_visible_but_locked_and_write_guarded():
    feature = product_features.get_feature("autumn_opportunity_library")
    assert feature["status"] == "coming_soon"
    assert feature["allow_routing"] is False
    assert feature["allow_task_creation"] is False
    try:
        server.search_opportunity_sources(q="27届秋招")
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail == "秋招机会库尚未开放，敬请期待"
    else:
        raise AssertionError("秋招机会库搜索必须被服务端拒绝")


def test_hidden_feature_requires_explicit_development_override():
    old = os.environ.get("CADDIE_ENABLE_HIDDEN_FEATURES")
    try:
        os.environ["CADDIE_ENABLE_HIDDEN_FEATURES"] = "autumn_opportunity_library"
        feature = product_features.get_feature("autumn_opportunity_library")
        assert feature["status"] == "available"
        assert feature["allow_routing"] is True
        assert feature["allow_task_creation"] is True
    finally:
        if old is None:
            os.environ.pop("CADDIE_ENABLE_HIDDEN_FEATURES", None)
        else:
            os.environ["CADDIE_ENABLE_HIDDEN_FEATURES"] = old


def test_hidden_opportunity_plans_do_not_leak_into_global_calendar():
    original = server.db.list_interview_schedule
    try:
        server.db.list_interview_schedule = lambda *_args, **_kwargs: [
            {"id": 1, "kind": "interview"},
            {"id": 2, "kind": "opportunity_plan"},
        ]
        assert server.calendar()["items"] == [{"id": 1, "kind": "interview"}]
    finally:
        server.db.list_interview_schedule = original


def test_legacy_worklog_is_hidden_and_write_guarded():
    feature = product_features.get_feature("legacy_worklog")
    assert feature["status"] == "hidden"
    try:
        server._require_feature_available("legacy_worklog")
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("旧工作记录写入必须被服务端拒绝")


def test_application_materials_are_visible_but_locked_and_write_guarded():
    feature = product_features.get_feature("application_materials")
    assert feature["status"] == "coming_soon"
    assert feature["allow_routing"] is False
    assert feature["allow_task_creation"] is False
    try:
        server.save_browser_capture(server.BrowserCaptureIn(
            company="示例公司", page_url="https://example.com/apply", structure=[]
        ))
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail == "网申材料库尚未开放，敬请期待"
    else:
        raise AssertionError("隐藏后的网申采集写入必须被服务端拒绝")

    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    static_html = html.split("<script>", 1)[0]
    assert 'data-feature="application_materials" data-label="网申材料库"' in static_html
    assert "'application-materials':'application_materials'" in html
    assert "featureAvailable('application_materials')" in html
    assert "guideFunction('application-materials','archive','网申材料库'" in html


def test_public_registry_exposes_release_state():
    registry = server.product_feature_registry()["features"]
    assert registry["autumn_opportunity_library"]["status"] == "coming_soon"
    assert registry["career_growth_journal"]["status"] == "coming_soon"
    assert registry["legacy_worklog"]["status"] == "hidden"
    assert registry["application_materials"]["status"] == "coming_soon"
    assert registry["interview_growth_analysis"]["status"] == "coming_soon"
    assert registry["email_inbox"]["status"] == "available"
    assert registry["email_ai_triage"]["status"] == "hidden"


def test_email_arrival_is_available_but_ai_triage_is_write_guarded():
    inbox = product_features.get_feature("email_inbox")
    assert inbox["status"] == "available"
    assert inbox["allow_routing"] is True
    try:
        server.email_analyze(days=30)
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail == "AI 邮件归类与自动写入尚未开放"
    else:
        raise AssertionError("AI 邮件归类接口必须被服务端拒绝")


def test_email_page_only_starts_basic_mail_sync():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "Caddie 邮箱</div><h1>不错过新的求职邮件" in html
    assert "setTimeout(()=>fetchEmail(true),0);" in html
    assert "if(STATE.emailAnalysisCache)await analyzeEmails" not in html
    assert 'onclick="openHandledEmails()"' not in html
    assert "AI 正在归纳" not in html


def test_frontend_replaces_old_worklog_entry_with_locked_growth_journal():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-feature="career_growth_journal"' in html
    assert "if(b.dataset.feature){showLockedFeature(b.dataset.feature);return;}" in html
    assert "职业成长日志" in html
    assert "敬请期待" in html
    assert "onclick=\"go('worklog')\">工作记录</button>" not in html
    assert "if(STATE.view==='worklog') return renderWorklog(v);" not in html


def test_frontend_shows_locked_autumn_opportunity_library_entries():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    static_nav = 'data-feature="autumn_opportunity_library" data-label="秋招机会库"'
    assert static_nav in html.split("<script>", 1)[0]
    assert "tracks.insertAdjacentHTML('beforebegin'" in html
    assert "SAVED_APP_VIEW==='opportunities'?'guide'" in html
    assert "if(!viewAvailable(v)){showLockedFeature(featureForView(v));return;}" in html
    assert "showLockedFeature('autumn_opportunity_library')" in html
    assert "guideFunction('opportunities','radar','秋招机会库'" in html
    assert "可以先从「秋招机会库」转入一条岗位" not in html


def test_interview_growth_analysis_is_visible_but_locked_and_write_guarded():
    feature = product_features.get_feature("interview_growth_analysis")
    assert feature["status"] == "coming_soon"
    assert feature["allow_routing"] is False
    assert feature["allow_task_creation"] is False

    try:
        server.create_interview_growth_analysis_v2(server.InterviewGrowthAnalysisIn(
            role_family="AI产品经理",
            selected_round_ids=[],
        ))
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail == "综合分析与训练尚未开放，敬请期待"
    else:
        raise AssertionError("综合分析与训练的新建任务必须被服务端拒绝")

    js = (ROOT / "static" / "interview-system.js").read_text(encoding="utf-8")
    assert "function growthFeatureAvailable()" in js
    assert "showLockedFeature('interview_growth_analysis')" in js
    assert "iv-growth-entry-locked" in js
    assert "const growthLocked = !growthFeatureAvailable();" in js
    assert "if (!growthLocked)" in js


if __name__ == "__main__":
    test_career_growth_journal_is_visible_but_locked()
    test_autumn_opportunity_library_is_visible_but_locked_and_write_guarded()
    test_hidden_feature_requires_explicit_development_override()
    test_hidden_opportunity_plans_do_not_leak_into_global_calendar()
    test_legacy_worklog_is_hidden_and_write_guarded()
    test_application_materials_are_visible_but_locked_and_write_guarded()
    test_public_registry_exposes_release_state()
    test_email_arrival_is_available_but_ai_triage_is_write_guarded()
    test_email_page_only_starts_basic_mail_sync()
    test_frontend_replaces_old_worklog_entry_with_locked_growth_journal()
    test_frontend_shows_locked_autumn_opportunity_library_entries()
    test_interview_growth_analysis_is_visible_but_locked_and_write_guarded()
    print("PRODUCT_FEATURE_GATES_OK")
