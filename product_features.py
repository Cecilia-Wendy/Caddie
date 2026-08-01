"""Single source of truth for product release gates."""

from copy import deepcopy
import os


FEATURES = {
    "email_inbox": {
        "status": "available",
        "label": "邮箱",
        "message": "邮箱到达与提醒已开放",
        "allow_routing": True,
        "allow_task_creation": True,
    },
    "email_ai_triage": {
        "status": "hidden",
        "label": "AI 邮件归类",
        "message": "AI 邮件归类与自动写入尚未开放",
        "allow_routing": False,
        "allow_task_creation": False,
    },
    "autumn_opportunity_library": {
        "status": "coming_soon",
        "label": "秋招机会库",
        "message": "秋招机会库尚未开放，敬请期待",
        "allow_routing": False,
        "allow_task_creation": False,
    },
    "career_growth_journal": {
        "status": "coming_soon",
        "label": "职业成长日志",
        "message": "职业成长日志尚未开放，敬请期待",
        "allow_routing": False,
        "allow_task_creation": False,
    },
    "legacy_worklog": {
        "status": "hidden",
        "label": "工作记录",
        "message": "原工作记录已下线；历史数据仍保留，后续将由职业成长日志承接",
        "allow_routing": False,
        "allow_task_creation": False,
    },
    "application_materials": {
        "status": "coming_soon",
        "label": "网申材料库",
        "message": "网申材料库尚未开放，敬请期待",
        "allow_routing": False,
        "allow_task_creation": False,
    },
    "interview_growth_analysis": {
        "status": "coming_soon",
        "label": "综合分析与训练",
        "message": "综合分析与训练尚未开放，敬请期待",
        "allow_routing": False,
        "allow_task_creation": False,
    },
}


def _development_override_enabled(key: str) -> bool:
    """Allow an explicit local opt-in without weakening release defaults."""
    enabled = {
        item.strip()
        for item in os.environ.get("CADDIE_ENABLE_HIDDEN_FEATURES", "").split(",")
        if item.strip()
    }
    return key in enabled


def get_feature(key: str) -> dict:
    feature = deepcopy(FEATURES.get(key, {
        "status": "hidden",
        "label": key,
        "message": "该功能尚未开放",
        "allow_routing": False,
        "allow_task_creation": False,
    }))
    if feature["status"] != "available" and _development_override_enabled(key):
        feature.update({
            "status": "available",
            "allow_routing": True,
            "allow_task_creation": True,
        })
    return feature


def public_features() -> dict:
    return {key: get_feature(key) for key in FEATURES}
