"""
Caddie 上下文装配器。

生成端点不再自己零散拼资料，而是把 intent + 实体 id 交给这里统一组装。
"""
import hashlib
import json
import re

import db
import interview_store

_INBOX_QUERY_TERMS = (
    "收件箱", "刚上传", "刚导入",
    "新增资料", "新加的资料", "最近资料",
)


def _clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n...（已截断）"


def _section(title, body):
    body = (body or "").strip()
    if not body:
        return ""
    return f"\n## {title}\n{body}\n"


def _user_profile_text():
    conn = db.get_db()
    row = conn.execute("SELECT value FROM app_settings WHERE key='user_profile'").fetchone()
    conn.close()
    if not row:
        return ""
    try:
        profile = json.loads(row["value"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    lines = [
        f"姓名：{profile.get('name') or ''}",
        f"常用称呼：{profile.get('preferred_name') or ''}",
        f"所在地：{profile.get('location') or ''}",
        f"目标岗位：{profile.get('target_roles') or ''}",
        f"目标城市：{profile.get('target_locations') or ''}",
        f"个人简介：{profile.get('bio') or ''}",
    ]
    educations = []
    for item in profile.get("education") or []:
        if not isinstance(item, dict) or not item.get("institution"):
            continue
        date_range = "—".join(filter(None, [item.get("start_date"), item.get("end_date")]))
        educations.append("\n".join(filter(None, [
            f"- {item.get('institution')} · {item.get('degree') or ''} · {item.get('major') or ''}",
            f"  时间：{date_range}" if date_range else "",
            f"  辅修：{item.get('minor')}" if item.get("minor") else "",
            f"  GPA：{item.get('gpa')}" if item.get("gpa") else "",
            f"  排名：{item.get('ranking')}" if item.get("ranking") else "",
            ("  核心课程：" + "；".join(
                filter(None, [
                    f"{course.get('name')}{('（' + course.get('grade') + '）') if course.get('grade') else ''}"
                    for course in item.get("courses") or [] if isinstance(course, dict)
                ])
            )) if item.get("courses") else "",
            ("  奖项荣誉：" + "；".join(
                filter(None, [
                    " · ".join(filter(None, [
                        honor.get("name"), honor.get("issuer"), honor.get("level"),
                        honor.get("date"), honor.get("description"),
                    ]))
                    for honor in item.get("honors") or [] if isinstance(honor, dict)
                ])
            )) if item.get("honors") else "",
            ("  校园经历：" + "；".join(
                filter(None, [
                    " · ".join(filter(None, [
                        activity.get("organization"), activity.get("role"),
                        "—".join(filter(None, [activity.get("start_date"), activity.get("end_date")])),
                        activity.get("description"),
                    ]))
                    for activity in item.get("activities") or [] if isinstance(activity, dict)
                ])
            )) if item.get("activities") else "",
        ])))
    if educations:
        lines.append("教育经历：\n" + "\n".join(educations))
    return "\n".join(line for line in lines if not line.endswith("："))


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


def _is_inbox_query(query):
    value = (query or "").strip()
    if not value:
        return False
    if any(term in value for term in _INBOX_QUERY_TERMS):
        return True
    # "不要让我重新上传" describes a retrieval failure; it must not switch
    # the whole turn into recent-inbox mode merely because it contains "新上传".
    return bool(re.search(r"(?<!重)新(?:上传|导入)(?:的)?(?:资料|文件|文档)", value))


def _recent_inbox_sources(limit=12):
    result = []
    for brief in db.list_sources(limit=limit):
        source = db.get_source(brief.get("id"))
        if source:
            result.append(source)
    return result


def _format_inbox_sources(items):
    parts = []
    for src in items:
        analysis = {}
        try:
            analysis = json.loads(src.get("analysis_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        key_points = analysis.get("key_points") if isinstance(analysis, dict) else []
        key_text = "\n".join(f"- {point}" for point in (key_points or [])[:6])
        parts.append(
            f"### [收件箱资料 #{src.get('id')}] "
            f"{src.get('title') or src.get('file_name') or '未命名资料'}\n"
            f"类型：{src.get('source_type') or 'other'}；状态：{src.get('status') or 'raw'}；"
            f"更新时间：{src.get('updated_at') or src.get('created_at') or ''}\n"
            f"摘要：{src.get('summary') or '（尚未生成摘要，请直接阅读内容摘录）'}\n"
            + (f"关键点：\n{key_text}\n" if key_text else "")
            + f"内容摘录：\n{_clip(src.get('content'), 1800)}"
        )
    return "\n\n".join(parts)


def _asset_refs(asset_type=None, track_id=None, project_id=None):
    return [
        {"type": "asset", "id": a.get("id"), "asset_type": a.get("asset_type"), "title": a.get("title")}
        for a in db.list_assets(asset_type=asset_type, track_id=track_id, project_id=project_id, limit=20)
    ]


def _company_key(value):
    """Normalize common legal-name suffixes so sibling job tracks can meet."""
    text = re.sub(r"[\s·•\-—_（）()【】\[\]]+", "", (value or "").lower())
    for suffix in ("股份有限公司", "有限责任公司", "有限公司", "证券股份", "证券公司"):
        text = text.replace(suffix, "")
    return text


def _career_material(limit=14000):
    parts = []
    for exp in db.get_experiences():
        projects = []
        for brief in exp.get("projects") or []:
            project = db.get_project(brief.get("id")) or brief
            projects.append(
                f"### {project.get('name') or '未命名项目'}\n"
                f"一句话：{project.get('one_liner') or '（未填写）'}\n"
                f"{_clip(project.get('document'), 1400)}"
            )
        parts.append(
            f"## {exp.get('company') or '未填写公司'} · {exp.get('role') or '未填写岗位'}"
            f"（{exp.get('start_date') or ''} ~ {exp.get('end_date') or '至今'}）\n"
            + ("\n\n".join(projects) if projects else "（暂无项目文档）")
        )
    return _clip("\n\n".join(parts), limit)


def _resume_material(track_id, limit=15000):
    """Collect actual resume text, preferring the current job's version."""
    candidates = []
    for item in db.list_resume_versions(track_id):
        row = dict(item)
        row["relationship"] = "current_track"
        candidates.append(row)
    for item in db.list_recent_resume_versions(limit=8):
        if item.get("track_id") == track_id:
            continue
        row = dict(item)
        row["relationship"] = "recent_fallback"
        candidates.append(row)

    parts, refs, seen = [], [], set()
    for item in candidates:
        text = (item.get("extracted_text") or "").strip()
        identity = item.get("pdf_hash") or item.get("docx_hash") or text[:500]
        if not text or not identity or identity in seen:
            continue
        seen.add(identity)
        relation = "当前岗位绑定" if item.get("relationship") == "current_track" else "其他岗位最近版本"
        track_label = " · ".join(filter(None, [
            item.get("track_company"), item.get("track_role")
        ]))
        parts.append(
            f"## [{relation}] {item.get('version_name') or '未命名简历'}"
            + (f"（{track_label}）" if track_label else "") + "\n"
            f"状态：{item.get('status') or '未标记'}\n"
            f"{_clip(text, 4200)}"
        )
        refs.append({
            "type": "resume_version", "id": item.get("id"),
            "title": item.get("version_name") or "未命名简历",
            "relationship": item.get("relationship"),
        })
        if len(parts) >= 4:
            break

    for brief in db.list_sources(source_type="resume", limit=5):
        source = db.get_source(brief.get("id"))
        text = (source or {}).get("content") or ""
        identity = (source or {}).get("content_hash") or text[:500]
        if not text.strip() or identity in seen:
            continue
        seen.add(identity)
        parts.append(
            f"## [资料库简历] {source.get('title') or source.get('file_name') or '未命名简历'}\n"
            f"{_clip(text, 4200)}"
        )
        refs.append({
            "type": "source", "id": source.get("id"),
            "title": source.get("title") or source.get("file_name"),
            "relationship": "resume_source",
        })
        if len(parts) >= 5:
            break
    return _clip("\n\n".join(parts), limit), refs


def _sibling_track_material(track, limit=12000):
    current_id = track.get("id")
    company_key = _company_key(track.get("company"))
    if not company_key:
        return "", []

    siblings = []
    for item in db.list_job_tracks():
        if item.get("id") == current_id:
            continue
        other_key = _company_key(item.get("company"))
        if not other_key or not (
            other_key == company_key or other_key in company_key or company_key in other_key
        ):
            continue
        siblings.append(item)

    parts, refs = [], []
    for sibling in siblings[:5]:
        sid = sibling.get("id")
        refs.append({
            "type": "job_track", "id": sid,
            "title": f"{sibling.get('company') or ''} {sibling.get('role') or sibling.get('target') or ''}".strip(),
            "relationship": "same_company_sibling",
        })
        blocks = [
            f"## 同公司岗位 #{sid}：{sibling.get('company') or ''} · "
            f"{sibling.get('role') or sibling.get('target') or ''}",
            f"岗位备注：{sibling.get('notes') or '（无）'}",
            f"JD：\n{_clip(sibling.get('jd'), 2600) or '（未录入）'}",
        ]
        owned_knowledge = [
            item for item in db.list_knowledge_items(track_id=sid)
            if item.get("scope_type") == "track" and item.get("track_id") == sid
        ]
        if owned_knowledge:
            blocks.append("岗位准备材料：\n" + "\n\n".join(
                f"### {item.get('title') or '未命名文档'}（knowledge_id={item.get('id')}）\n"
                f"{_clip(item.get('content'), 1800)}"
                for item in owned_knowledge[:5]
            ))
            refs.extend({
                "type": "knowledge_item", "id": item.get("id"), "title": item.get("title"),
                "relationship": "same_company_sibling",
            } for item in owned_knowledge[:5])
        assets = db.list_assets(track_id=sid, limit=5)
        if assets:
            blocks.append("岗位生成资产：\n" + "\n\n".join(
                f"### [{item.get('asset_type') or 'asset'}] {item.get('title') or '未命名资产'}\n"
                f"{_clip(item.get('body'), 1400)}" for item in assets
            ))
        resumes = db.list_resume_versions(sid)
        if resumes:
            blocks.append("岗位简历版本：\n" + "\n\n".join(
                f"### {item.get('version_name') or '未命名简历'}\n"
                f"{_clip(item.get('extracted_text'), 1400)}" for item in resumes[:2]
            ))
        parts.append("\n".join(blocks))
    return _clip("\n\n".join(parts), limit), refs


def build_job_research_context(track_id, query, limit=32000):
    """Build the evidence bundle used by job research and knowledge coaching.

    Unlike a narrow track search, this deliberately includes the user's real
    career record and same-company sibling tracks. A request such as "参考同
    公司另一个岗位的准备材料" must not degrade into asking the user to paste
    information that is already in Caddie.
    """
    track = db.get_job_track(track_id)
    if not track:
        return {"text": "", "refs": [], "stats": {}}

    base = build_context(
        track_id=track_id, intent="job_research_and_knowledge", query=query
    )
    sections = [base.get("text") or ""]
    refs = list(base.get("refs") or [])

    career = _career_material()
    if career:
        sections.append(_section(
            "个人真实经历与项目",
            "以下内容已经存在于 Caddie，不得声称未获得个人经历。\n\n" + career,
        ))

    resume_text, resume_refs = _resume_material(track_id)
    if resume_text:
        sections.append(_section(
            "简历原文与岗位版本",
            "以下是 Caddie 已提取的真实简历文本。回答岗位适配、面试准备或经历问题时必须优先读取，"
            "不得要求用户重复上传。\n\n" + resume_text,
        ))
        refs.extend(resume_refs)

    sibling_text, sibling_refs = _sibling_track_material(track)
    if sibling_text:
        sections.append(_section(
            "同公司其他岗位与既有准备材料",
            "用户提到同公司其他岗位时，应优先使用这里的真实资料，不得要求重复粘贴。\n\n"
            + sibling_text,
        ))
        refs.extend(sibling_refs)

    # Track-scoped retrieval is precise, while a second global pass can recover
    # personal projects or sibling-track documents whose IDs differ.
    global_matches = []
    try:
        from retrieval import hybrid_search
        global_matches = hybrid_search(query, track_id=None, limit=10)
    except Exception:
        pass
    seen = {(x.get("type"), x.get("id")) for x in refs}
    fresh_matches = []
    for item in global_matches:
        key = (item.get("entity_type"), item.get("entity_id"))
        if key in seen:
            continue
        seen.add(key)
        fresh_matches.append(item)
        refs.append({
            "type": item.get("entity_type"), "id": item.get("entity_id"),
            "title": item.get("title"), "retrieval_score": item.get("score"),
            "match_reason": item.get("match_reason"), "relationship": "global_retrieval",
        })
    if fresh_matches:
        sections.append(_section("跨库补充检索", "\n\n".join(
            f"### {item.get('title') or item.get('entity_type')}\n"
            f"{_clip(item.get('snippet'), 1200)}" for item in fresh_matches
        )))

    unique_refs = []
    seen_refs = set()
    for ref in refs:
        key = (ref.get("type"), ref.get("id"))
        if key in seen_refs:
            continue
        seen_refs.add(key)
        unique_refs.append(ref)
    return {
        "text": _clip("\n".join(sections), limit),
        "refs": unique_refs,
        "stats": {
            "career_experiences": len(db.get_experiences()),
            "resume_documents": len(resume_refs),
            "sibling_tracks": sum(
                1 for ref in sibling_refs if ref.get("type") == "job_track"
            ),
            "references": len(unique_refs),
        },
    }


def infer_track_scope(query: str | None, history_text: str | None = None) -> int | None:
    """Infer one unambiguous job workspace from company, role, or document names."""
    from retrieval import normalize_query

    text = normalize_query("\n".join(filter(None, [history_text, query]))).lower()
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None
    tracks = db.list_job_tracks()
    scores = {int(item["id"]): 0 for item in tracks}
    generic_words = (
        "项目", "案例", "分析", "设计", "研究", "论文", "公司",
        "面试", "准备", "知识", "介绍", "整理", "报告",
    )
    for track in tracks:
        tid = int(track["id"])
        company = re.sub(r"\s+", "", str(track.get("company") or "").lower())
        role = re.sub(r"\s+", "", str(track.get("role") or "").lower())
        target = re.sub(r"\s+", "", str(track.get("target") or "").lower())
        if company and company in compact:
            scores[tid] += 8
        if role and role in compact:
            scores[tid] += 10
        if target and len(target) >= 3 and target in compact:
            scores[tid] += 5

    for item in db.list_knowledge_items():
        tid = item.get("track_id")
        if not tid or int(tid) not in scores:
            continue
        title = re.sub(r"\s+", "", str(item.get("title") or "").lower())
        if not title:
            continue
        if title in compact:
            scores[int(tid)] += 14
            continue
        reduced = title
        for word in generic_words:
            reduced = reduced.replace(word, " ")
        terms = re.findall(r"[a-z][a-z0-9+.-]{1,}|[\u4e00-\u9fff]{2,}", reduced)
        scores[int(tid)] += min(12, sum(6 for term in set(terms) if term in compact))

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked or ranked[0][1] < 8:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 3:
        return None
    return ranked[0][0]


def build_context(track_id=None, project_id=None, intent="general", query=None):
    refs = []
    sections = [f"# Caddie 生成上下文\n意图：{intent or 'general'}\n"]
    profile_text = _user_profile_text()
    if profile_text:
        sections.append(_section("个人基础资料", profile_text))
    inbox_mode = _is_inbox_query(query)

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

    if inbox_mode:
        recent_sources = _recent_inbox_sources()
        if recent_sources:
            refs.extend({
                "type": "source", "id": item.get("id"),
                "title": item.get("title") or item.get("file_name"),
                "inbox_recent": True,
            } for item in recent_sources)
            sections.append(_section(
                "最近收件箱资料（用户明确要求读取）",
                "以下资料已经真实存在于 Caddie 收件箱。不得回答“没有收到”或要求用户重新粘贴。"
                "请先说明识别到了哪些资料，再判断它们应关联到哪段经历/项目；信息不足时只追问缺失字段，"
                "任何写库动作仍需用户确认。\n\n"
                + _format_inbox_sources(recent_sources),
            ))
        else:
            sections.append(_section(
                "最近收件箱资料（用户明确要求读取）",
                "当前收件箱确实为空，可以请用户上传或粘贴资料。",
            ))

    feedback = []
    feedback.extend(db.list_feedback_notes(scope="global"))
    if track_id:
        feedback.extend(db.list_feedback_notes(scope="track", scope_id=track_id))
    if project_id:
        feedback.extend(db.list_feedback_notes(scope="project", scope_id=project_id))
    sections.append(_section("生效反馈约束", _feedback_lines(feedback)))

    if track_id:
        knowledge = db.list_knowledge_items(track_id=track_id)
        if knowledge:
            folders = {
                item.get("id"): item.get("name") or "未分类"
                for item in db.list_knowledge_folders(track_id=track_id)
            }
            catalog = []
            for item in knowledge[:120]:
                folder_name = folders.get(item.get("folder_id"), "未分类")
                catalog.append(
                    f"- [knowledge_item #{item.get('id')}] "
                    f"[{folder_name}] {item.get('title') or '未命名知识'}"
                )
                refs.append({
                    "type": "knowledge_item", "id": item.get("id"),
                    "title": item.get("title"), "catalog_only": True,
                    "match_reason": "当前岗位知识目录",
                })
            sections.append(_section(
                "当前岗位完整知识目录",
                "这是当前岗位工作台真实挂载的知识目录。目录中的文档均已存在。"
                "正文尚未进入本轮上下文，不等于文档不存在；不得因为正文未命中而要求用户重新上传。"
                "当用户点名目录、文件夹或文档时，应先依据本目录确认存在性，再读取检索命中的正文。\n"
                + "\n".join(catalog),
            ))

        # A focused query is handled by hybrid retrieval below. Full bodies are
        # omitted here only to control context size; the complete title catalog
        # above must remain visible so retrieval misses cannot become false
        # "document does not exist" claims.
        if knowledge and not query:
            scope_names = {"global": "个人通用", "domain": "行业/职能", "company": "公司", "track": "岗位专属"}
            lines = []
            for item in knowledge[:30]:
                scope = scope_names.get(item.get("scope_type"), "知识")
                body = "\n" + _clip(item.get("content"), 1000)
                lines.append(f"### [{scope}] {item.get('title') or '未命名知识'}\n"
                             f"主题：{item.get('topic') or '未分类'}；掌握度：{item.get('mastery') or 'learning'}{body}")
                refs.append({"type": "knowledge_item", "id": item.get("id"), "title": item.get("title")})
            sections.append(_section("岗位继承知识", "\n\n".join(lines)))

        gaps = db.list_track_gaps(track_id)
        if gaps:
            status_names = {"have": "已具备", "partial": "部分具备", "missing": "待补齐"}
            gap_lines = []
            for gap in gaps:
                gap_lines.append(
                    f"- [{status_names.get(gap.get('my_status'), '待判断')}] "
                    f"{gap.get('requirement') or ''}"
                    + (f"；备注：{gap.get('note')}" if gap.get("note") else "")
                )
                refs.append({"type": "track_gap", "id": gap.get("id"), "title": gap.get("requirement")})
            sections.append(_section("岗位差距清单", "\n".join(gap_lines)))

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

    # In global chat, listing every generated asset makes unrelated old outputs
    # look authoritative and floods the source chips. Focused retrieval below
    # will still bring back a relevant asset when the query actually matches it.
    assets = _asset_refs(track_id=track_id, project_id=project_id) if (track_id or project_id) else []
    if assets:
        sections.append(_section("已有生成资产", "\n".join(f"- [{a['asset_type']}] {a['title']} (asset_id={a['id']})" for a in assets)))
        refs.extend(assets)

    if query and not inbox_mode:
        from retrieval import hybrid_search, normalize_query
        matches = hybrid_search(query, track_id=track_id, limit=12)
        # Title lookup is deterministic and complements semantic retrieval.
        # A user naming several existing documents must not lose one merely
        # because another document scored higher in the vector search.
        if track_id:
            normalized_query = normalize_query(query).lower()
            compact_query = re.sub(r"\s+", "", normalized_query)
            generic_title_words = (
                "项目", "案例", "分析", "设计", "研究", "论文", "公司",
                "面试", "准备", "知识", "介绍", "整理", "报告",
            )
            existing_keys = {
                (item.get("entity_type"), int(item.get("entity_id")))
                for item in matches if item.get("entity_id") is not None
            }
            title_matches = []
            for item in db.list_knowledge_items(track_id=track_id):
                title = item.get("title") or ""
                compact_title = re.sub(r"\s+", "", title.lower())
                ascii_terms = re.findall(r"[a-z][a-z0-9+.-]{1,}", compact_title)
                reduced = compact_title
                for word in generic_title_words:
                    reduced = reduced.replace(word, " ")
                reduced_terms = re.findall(r"[\u4e00-\u9fff]{2,}", reduced)
                named = (
                    any(term in compact_query for term in ascii_terms)
                    or any(term in compact_query for term in reduced_terms)
                    or compact_title in compact_query
                )
                key = ("knowledge_item", int(item.get("id")))
                if named and key not in existing_keys:
                    title_matches.append({
                        "entity_type": "knowledge_item",
                        "entity_id": int(item.get("id")),
                        "title": title,
                        "snippet": item.get("content") or "",
                        "metadata": {
                            "track_id": item.get("track_id"),
                            "scope": item.get("scope_type"),
                            "topic": item.get("topic"),
                        },
                        "dense_score": 0.0,
                        "score": 2.0,
                        "exact_title_match": True,
                        "match_reason": "当前岗位标题命中",
                    })
                    existing_keys.add(key)
            matches = title_matches + matches
        if matches:
            sections.append(_section("混合检索命中", "\n\n".join(
                f"### {item.get('title') or item.get('entity_type')}\n"
                f"来源：{item.get('entity_type')} #{item.get('entity_id')}；"
                f"{'标题精确命中，以下为文档正文' if item.get('exact_title_match') else '语义相似度：' + format(item.get('dense_score', 0), '.3f')}\n"
                f"{_clip(item.get('snippet'), 7000 if item.get('exact_title_match') else 1000)}"
                for item in matches
            )))
            refs.extend({"type": item.get("entity_type"), "id": item.get("entity_id"),
                         "title": item.get("title"), "retrieval_score": item.get("score"),
                         "match_reason": item.get("match_reason")}
                        for item in matches)

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


def _revision(value):
    """A stable content fingerprint for an Agent-read object.

    It is deliberately derived from the fields an Agent receives, rather than a
    database timestamp. This lets a client detect a real semantic change even
    when two writes happen within the same second.
    """
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _agent_object(object_type, item, detail="brief", content_limit=12000):
    """Turn one Caddie record into a versioned Agent-read object.

    ``brief`` is the default because an Agent needs an accurate map before it
    needs an entire document. The revision always hashes the unabridged record,
    so omitted text never makes a later overwrite appear safe.
    """
    if not item:
        return None

    if object_type == "job_track":
        canonical = {
            "company": item.get("company"), "role": item.get("role"),
            "target": item.get("target"), "status": item.get("status"),
            "priority": item.get("priority"), "notes": item.get("notes"),
            "jd": item.get("jd") or "",
        }
        content = dict(canonical)
        content["jd"] = _clip(canonical["jd"], content_limit if detail == "full" else 3200)
        title = " ".join(filter(None, [item.get("company"), item.get("role") or item.get("target")])).strip()
    elif object_type == "project":
        canonical = {
            "name": item.get("name"), "one_liner": item.get("one_liner"),
            "technologies": item.get("technologies"), "keywords": item.get("keywords"),
            "document": item.get("document") or "",
            "experience": item.get("experience") or {},
        }
        content = dict(canonical)
        content["document"] = _clip(canonical["document"], content_limit if detail == "full" else 1600)
        title = item.get("name") or "未命名项目"
    elif object_type == "knowledge_item":
        canonical = {
            "title": item.get("title"), "scope_type": item.get("scope_type"),
            "topic": item.get("topic"), "mastery": item.get("mastery"),
            "content": item.get("content") or "",
        }
        content = dict(canonical)
        content["content"] = _clip(canonical["content"], content_limit if detail == "full" else 800)
        title = item.get("title") or "未命名知识"
    elif object_type == "source":
        canonical = {
            "title": item.get("title") or item.get("file_name"),
            "source_type": item.get("source_type"), "summary": item.get("summary"),
            "status": item.get("status"), "content": item.get("content") or "",
        }
        content = dict(canonical)
        content["content"] = _clip(canonical["content"], content_limit if detail == "full" else 600)
        title = item.get("title") or item.get("file_name") or "未命名资料"
    elif object_type == "resume_version":
        canonical = {
            "version_name": item.get("version_name"), "track_id": item.get("track_id"),
            "status": item.get("status"), "change_summary": item.get("change_summary"),
            "extracted_text": item.get("extracted_text") or "",
        }
        content = dict(canonical)
        content["extracted_text"] = _clip(canonical["extracted_text"], content_limit if detail == "full" else 1200)
        title = item.get("version_name") or "未命名简历版本"
    elif object_type == "interview_round":
        documents = item.get("documents") or []
        transcripts = item.get("transcript_sources") or []
        canonical = {
            "track_id": item.get("track_id"),
            "round_number": item.get("round_number"),
            "round_name": item.get("round_name"),
            "round_type": item.get("round_type"),
            "interview_mode": item.get("interview_mode"),
            "language": item.get("language"),
            "status": item.get("status"),
            "notes": item.get("notes") or "",
            "documents": [
                {
                    "id": doc.get("document_id") or doc.get("id"),
                    "title": doc.get("title"),
                    "document_type": doc.get("document_type"),
                    "body": doc.get("body") or "",
                    "use_type": doc.get("use_type"),
                }
                for doc in documents
            ],
            "transcript_sources": [
                {
                    "id": source.get("id"),
                    "title": source.get("title"),
                    "source_kind": source.get("source_kind"),
                    "raw_text": source.get("raw_text") or "",
                }
                for source in transcripts
            ],
        }
        content = dict(canonical)
        per_item_limit = max(1200, content_limit // max(1, len(documents) + len(transcripts)))
        if detail != "full":
            per_item_limit = 1600
        content["documents"] = [
            {**doc, "body": _clip(doc.get("body"), per_item_limit)}
            for doc in canonical["documents"]
        ]
        content["transcript_sources"] = [
            {**source, "raw_text": _clip(source.get("raw_text"), per_item_limit)}
            for source in canonical["transcript_sources"]
        ]
        title = item.get("round_name") or f"第 {item.get('round_number') or '?'} 轮面试"
    else:
        return None

    return {
        "type": object_type,
        "id": item.get("id"),
        "title": title,
        "updated_at": item.get("updated_at") or item.get("created_at"),
        "revision": _revision(canonical),
        "content": content,
        "detail": detail,
        "full_content_available": detail != "full",
        "read_ref": {"document_type": object_type, "document_id": item.get("id")},
        "read_policy": "read_only_reconfirm_before_overwrite",
    }


def _feedback_constraints(notes):
    """Expose feedback as machine-readable hard/soft constraints for Agents."""
    hard, soft = [], []
    seen = set()
    for note in notes:
        if note.get("id") in seen:
            continue
        seen.add(note.get("id"))
        directive = (note.get("directive") or note.get("content") or "").strip()
        if not directive:
            continue
        item = {
            "id": note.get("id"), "directive": directive,
            "scope": note.get("scope"), "scope_id": note.get("scope_id"),
            "category": note.get("category"), "polarity": note.get("polarity"),
            "strength": (note.get("strength") or "soft").lower(),
            "updated_at": note.get("updated_at") or note.get("created_at"),
        }
        (hard if item["strength"] == "hard" else soft).append(item)
    return {"hard": hard, "soft": soft}


def _agent_reference_index(track_id=None, project_id=None, knowledge_limit=120, source_limit=20):
    """An index of related material, intentionally without full source bodies."""
    sources, seen_sources = [], set()
    for entity_type, entity_id in (("job_track", track_id), ("project", project_id)):
        if not entity_id:
            continue
        for linked in _related_sources(entity_type, entity_id, limit=source_limit):
            source = linked["source"]
            if source.get("id") in seen_sources:
                continue
            seen_sources.add(source.get("id"))
            sources.append({
                "type": "source", "id": source.get("id"),
                "title": source.get("title") or source.get("file_name") or "未命名资料",
                "source_type": source.get("source_type"), "summary": _clip(source.get("summary"), 500),
                "relation": linked.get("relation"), "reason": linked.get("reason"),
                "read_ref": {"document_type": "source", "document_id": source.get("id")},
            })
    knowledge = []
    if track_id:
        for item in db.list_knowledge_items(track_id=track_id)[:knowledge_limit]:
            knowledge.append({
                "type": "knowledge_item", "id": item.get("id"), "title": item.get("title") or "未命名知识",
                "scope_type": item.get("scope_type"), "topic": item.get("topic"),
                "mastery": item.get("mastery"), "summary": _clip(item.get("content"), 180),
                "read_ref": {"document_type": "knowledge_item", "document_id": item.get("id")},
            })
    gaps = []
    if track_id:
        for gap in db.list_track_gaps(track_id):
            gaps.append({
                "id": gap.get("id"), "requirement": gap.get("requirement"),
                "my_status": gap.get("my_status"), "note": gap.get("note"),
            })
    return {"sources": sources, "knowledge": knowledge, "track_gaps": gaps}


def _agent_context_brief(intent, selected, constraints, reference_index):
    lines = ["# Caddie Agent 工作摘要", f"任务意图：{intent or 'external_agent'}"]
    if selected:
        lines.append("\n## 已选对象（仅这些对象可作为直接上下文）")
        for item in selected:
            content = item.get("content") or {}
            summary = (
                content.get("one_liner") or content.get("jd") or content.get("document")
                or content.get("content") or content.get("extracted_text") or ""
            )
            if item.get("type") == "interview_round":
                transcript = next(iter(content.get("transcript_sources") or []), {})
                document = next(iter(content.get("documents") or []), {})
                summary = (
                    transcript.get("raw_text") or document.get("body")
                    or content.get("notes") or "已读取轮次元数据，暂无逐字稿正文"
                )
            lines.append(f"- [{item.get('type')} #{item.get('id')}] {item.get('title')}\n  摘要：{_clip(summary, 500)}")
    hard = constraints.get("hard") or []
    soft = constraints.get("soft") or []
    if hard:
        lines.append("\n## 硬约束（不可违背）\n" + "\n".join(f"- {x['directive']}" for x in hard))
    if soft:
        lines.append("\n## 偏好（尽量遵守）\n" + "\n".join(f"- {x['directive']}" for x in soft))
    sources = reference_index.get("sources") or []
    if sources:
        lines.append("\n## 关联证据目录（按需读取原文）\n" + "\n".join(
            f"- [source #{x['id']}] {x['title']}：{x.get('summary') or '无摘要'}" for x in sources))
    knowledge = reference_index.get("knowledge") or []
    if knowledge:
        lines.append(
            "\n## 可用知识目录（完整标题索引，按需读取原文）\n"
            "目录中的条目均真实存在。摘要为空或正文尚未读取不代表文档不存在；"
            "不得据此要求用户重新上传。需要具体内容时调用 read_document。\n"
            + "\n".join(
            f"- [knowledge_item #{x['id']}] {x['title']}：{x.get('summary') or '无摘要'}" for x in knowledge))
    gaps = reference_index.get("track_gaps") or []
    if gaps:
        lines.append("\n## 岗位差距\n" + "\n".join(
            f"- [{x.get('my_status') or 'unknown'}] {x.get('requirement') or ''}" for x in gaps))
    lines.append("\n## 读取规则\n先基于本摘要工作；需要核验具体数字、原始证据或完整文档时，使用 read_ref 调用 read_document。")
    return "\n".join(lines)


def _interview_round_record(round_id):
    round_ = interview_store.get_round(round_id)
    if not round_:
        return None
    return {
        **round_,
        "documents": interview_store.list_round_documents(round_id),
        "transcript_sources": interview_store.list_transcript_sources(round_id),
    }


def build_interview_round_context(round_id, intent="interview_review", query=None,
                                  content_limit=42000):
    """Return complete interview evidence plus the related job context."""
    round_record = _interview_round_record(round_id)
    if not round_record:
        raise ValueError("面试轮次不存在")
    base = build_context(
        track_id=round_record.get("track_id"), intent=intent, query=query or "",
    )
    round_object = _agent_object("interview_round", round_record, "full", content_limit)
    sections = [base.get("text") or "", "\n## 本轮面试完整材料"]
    content = round_object.get("content") or {}
    sections.append(
        "\n".join([
            f"轮次：{round_object.get('title')}",
            f"形式：{content.get('round_type') or '未记录'} / {content.get('interview_mode') or '未记录'}",
            f"状态：{content.get('status') or '未记录'}",
            f"备注：{content.get('notes') or '无'}",
        ])
    )
    for source in content.get("transcript_sources") or []:
        sections.append(
            f"\n### 原始逐字稿：{source.get('title') or source.get('source_kind') or source.get('id')}\n"
            f"{source.get('raw_text') or '（空）'}"
        )
    for document in content.get("documents") or []:
        sections.append(
            f"\n### {document.get('title') or document.get('document_type') or document.get('id')}\n"
            f"{document.get('body') or '（空）'}"
        )
    return {
        **base,
        "interview_round_id": round_id,
        "interview_round": round_object,
        "text": "\n".join(sections),
    }


def build_agent_context_package(track_id=None, project_id=None, interview_round_id=None,
                                document_type=None,
                                document_id=None, intent="external_agent", query=None,
                                detail="brief", content_limit=12000):
    """Build a versioned, least-privilege context package for an Agent client.

    This is not a raw database export. It includes only explicitly selected
    records, their version fingerprints, relevant feedback constraints and a
    change cursor that the caller must refresh before proposing an overwrite.
    """
    if detail not in {"brief", "full"}:
        raise ValueError("detail must be brief or full")
    selected = []
    track = db.get_job_track(track_id) if track_id else None
    project = db.get_project(project_id) if project_id else None
    interview_round = _interview_round_record(interview_round_id) if interview_round_id else None
    if interview_round and not track_id:
        track_id = interview_round.get("track_id")
        track = db.get_job_track(track_id) if track_id else None
    if track:
        selected.append(_agent_object("job_track", track, detail, content_limit))
    if project:
        selected.append(_agent_object("project", project, detail, content_limit))
    if interview_round:
        selected.append(_agent_object("interview_round", interview_round, detail, content_limit))

    document = None
    if document_type and document_id:
        readers = {
            "knowledge_item": db.get_knowledge_item,
            "project": db.get_project,
            "source": db.get_source,
            "resume_version": db.get_resume_version,
            "interview_round": _interview_round_record,
        }
        reader = readers.get(document_type)
        if not reader:
            raise ValueError(
                "document_type must be knowledge_item, project, source, resume_version, or interview_round"
            )
        document = reader(document_id)
        if not document:
            raise ValueError("Selected document does not exist")
        obj = _agent_object(document_type, document, detail, content_limit)
        if not any(x and x.get("type") == obj.get("type") and x.get("id") == obj.get("id") for x in selected):
            selected.append(obj)
        if document_type == "project" and not project:
            project_id, project = document_id, document
        if document_type == "interview_round":
            interview_round_id, interview_round = document_id, document
        if document_type in {"knowledge_item", "resume_version", "source", "interview_round"} and not track_id:
            track_id = document.get("track_id")
            track = db.get_job_track(track_id) if track_id else None
            if track and not any(x and x.get("type") == "job_track" and x.get("id") == track_id for x in selected):
                selected.insert(0, _agent_object("job_track", track, detail, content_limit))

    feedback = list(db.list_feedback_notes(scope="global"))
    if track_id:
        feedback.extend(db.list_feedback_notes(scope="track", scope_id=track_id))
    if project_id:
        feedback.extend(db.list_feedback_notes(scope="project", scope_id=project_id))

    constraints = _feedback_constraints(feedback)
    reference_index = _agent_reference_index(track_id, project_id)
    return {
        "protocol_version": "1.0",
        "kind": "caddie_agent_context_package",
        "change_cursor": db.latest_workspace_change_cursor(),
        "scope": {
            "track_id": track_id, "project_id": project_id,
            "interview_round_id": interview_round_id,
            "document_type": document_type, "document_id": document_id,
            "intent": intent, "query": query or "", "detail": detail,
        },
        "objects": [item for item in selected if item],
        "constraints": constraints,
        "reference_index": reference_index,
        "context_text": _agent_context_brief(intent, selected, constraints, reference_index),
        "write_rule": (
            "Draft assets may be saved with provenance. Facts, numbers, contribution boundaries, "
            "feedback rules and document overwrites must be proposed for user confirmation. "
            "Before proposing an overwrite, re-read the target object and compare its revision. "
            "Use read_ref with read_document when a full source is necessary."
        ),
    }
