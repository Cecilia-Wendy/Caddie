"""
Caddie 上下文装配器。

生成端点不再自己零散拼资料，而是把 intent + 实体 id 交给这里统一组装。
"""
import json

import db


def _clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n...（已截断）"


def _section(title, body):
    body = (body or "").strip()
    if not body:
        return ""
    return f"\n## {title}\n{body}\n"


def _feedback_lines(notes):
    if not notes:
        return "（暂无生效反馈约束）"
    hard, soft = [], []
    for n in notes:
        directive = (n.get("directive") or n.get("content") or "").strip()
        if not directive:
            continue
        line = f"- {directive}"
        if (n.get("strength") or "").lower() == "hard":
            hard.append(line)
        else:
            soft.append(line)
    chunks = []
    if hard:
        chunks.append("### 铁律（不可违背）\n" + "\n".join(hard))
    if soft:
        chunks.append("### 偏好（尽量遵守）\n" + "\n".join(soft))
    return "\n\n".join(chunks) or "（暂无生效反馈约束）"


def _related_sources(entity_type, entity_id, limit=5):
    out = []
    for link in db.list_source_links(entity_type=entity_type, entity_id=entity_id)[:limit]:
        src = db.get_source(link.get("source_id"))
        if not src:
            continue
        out.append({
            "source": src,
            "relation": link.get("relation"),
            "reason": link.get("reason"),
        })
    return out


def _format_sources(items):
    if not items:
        return ""
    parts = []
    for item in items:
        src = item["source"]
        parts.append(
            f"### {src.get('title') or src.get('file_name') or ('资料 #' + str(src.get('id')))}\n"
            f"类型：{src.get('source_type') or 'other'}；关系：{item.get('relation') or 'related'}\n"
            f"摘要：{src.get('summary') or '（无摘要）'}\n"
            f"相关原因：{item.get('reason') or '（未记录）'}\n"
            f"内容摘录：\n{_clip(src.get('content'), 1200)}"
        )
    return "\n\n".join(parts)


def _asset_refs(asset_type=None, track_id=None, project_id=None):
    return [
        {"type": "asset", "id": a.get("id"), "asset_type": a.get("asset_type"), "title": a.get("title")}
        for a in db.list_assets(asset_type=asset_type, track_id=track_id, project_id=project_id, limit=20)
    ]


def build_context(track_id=None, project_id=None, intent="general"):
    refs = []
    sections = [f"# Caddie 生成上下文\n意图：{intent or 'general'}\n"]

    track = None
    if track_id:
        track = db.get_job_track(track_id)
        if track:
            refs.append({"type": "job_track", "id": track.get("id"), "title": f"{track.get('company') or ''} {track.get('role') or track.get('target') or ''}".strip()})
            sections.append(_section("求职线", "\n".join(filter(None, [
                f"公司：{track.get('company') or ''}",
                f"岗位：{track.get('role') or track.get('target') or ''}",
                f"状态：{track.get('status') or ''}",
                f"备注：{track.get('notes') or ''}",
                "JD：\n" + _clip(track.get("jd"), 2500) if track.get("jd") else "",
            ]))))

    project = None
    if project_id:
        project = db.get_project(project_id)
        if project:
            exp = project.get("experience") or {}
            refs.append({"type": "project", "id": project.get("id"), "title": project.get("name")})
            sections.append(_section("项目事实", "\n".join(filter(None, [
                f"项目：{project.get('name') or ''}",
                f"所属经历：{exp.get('company') or ''} · {exp.get('role') or ''}",
                f"一句话：{project.get('one_liner') or ''}",
                f"技术/工具：{project.get('technologies') or ''}",
                f"关键词：{project.get('keywords') or ''}",
                "项目文档：\n" + _clip(project.get("document"), 5000),
            ]))))

    source_items = []
    if track_id:
        source_items.extend(_related_sources("job_track", track_id))
    if project_id:
        source_items.extend(_related_sources("project", project_id))
    if source_items:
        refs.extend({"type": "source", "id": x["source"].get("id"), "title": x["source"].get("title")} for x in source_items)
        sections.append(_section("关联原始资料", _format_sources(source_items)))

    feedback = []
    feedback.extend(db.list_feedback_notes(scope="global"))
    if track_id:
        feedback.extend(db.list_feedback_notes(scope="track", scope_id=track_id))
    if project_id:
        feedback.extend(db.list_feedback_notes(scope="project", scope_id=project_id))
    sections.append(_section("生效反馈约束", _feedback_lines(feedback)))

    # 个人作品：注入自迭代的「面试关注点库」（让整理/生成主动回答面试官常追问的点）
    if project and project.get("kind") == "personal":
        focus = db.list_focus_points("portfolio")
        if focus:
            lines = []
            for n in focus:
                tag = "【必答】" if (n.get("strength") or "").lower() == "hard" else "-"
                lines.append(f"{tag} {n.get('directive')}")
            sections.append(_section(
                "面试关注点（写这个作品时务必主动覆盖）", "\n".join(lines)))

    assets = _asset_refs(track_id=track_id, project_id=project_id)
    if assets:
        sections.append(_section("已有生成资产", "\n".join(f"- [{a['asset_type']}] {a['title']} (asset_id={a['id']})" for a in assets)))
        refs.extend(assets)

    return {
        "intent": intent,
        "track_id": track_id,
        "project_id": project_id,
        "refs": refs,
        "feedback": feedback,
        "text": "\n".join(s for s in sections if s),
    }


def provenance_payload(context_data):
    return json.dumps({
        "intent": context_data.get("intent"),
        "track_id": context_data.get("track_id"),
        "project_id": context_data.get("project_id"),
        "refs": context_data.get("refs") or [],
    }, ensure_ascii=False)
