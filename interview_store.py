"""Persistent store for Caddie's v2 interview workspace.

This module deliberately sits beside the legacy db helpers.  New interview
features write here; legacy tables remain readable during the migration.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import db


_KNOWLEDGE_TOPIC_RULES = (
    ("AI与Agent", (
        "ai", "agent", "智能体", "大模型", "llm", "蒸馏",
        "cursor", "claude code", "codex", "qoder", "copilot", "dify",
    )),
    ("AI工具", ("cursor", "claude", "codex", "qoder", "copilot", "dify")),
    ("产品与用户", ("产品经理", "产品设计", "用户", "需求", "迭代", "竞品")),
    ("数据分析", ("数据", "指标", "sql", "python", "模型", "预测", "gmv")),
    ("项目协同", ("pmo", "产研", "项目管理", "协同", "里程碑", "优先级", "推进")),
    ("金融与投行", ("ipo", "投行", "审核", "问询", "估值", "债券", "资本市场")),
    ("电商业务", ("电商", "tiktok shop", "商家", "交易", "履约", "内容电商")),
)


def infer_question_topics(text):
    lowered = str(text or "").lower()
    return [
        label for label, terms in _KNOWLEDGE_TOPIC_RULES
        if any(term in lowered for term in terms)
    ]


def is_ai_concept_question(text):
    lowered = str(text or "").lower()
    if "AI与Agent" not in infer_question_topics(lowered):
        return False
    concept_markers = (
        "怎么看", "怎么理解", "为什么", "区别", "比较", "价值", "趋势",
        "好在哪里", "优劣", "评价", "认知", "影响", "意味着什么",
    )
    project_markers = (
        "这个项目", "项目中", "怎么做", "如何落地", "如何推进",
        "怎么定义", "服务的用户", "怎么部署", "做出来", "具体负责",
    )
    return (
        any(marker in lowered for marker in concept_markers)
        and not any(marker in lowered for marker in project_markers)
    )


def _row(row):
    return dict(row) if row else None


def _rows(items):
    return [dict(item) for item in items]


def init_schema(c):
    """Idempotent schema installation, called by db.init_db()."""
    c.execute("""CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        document_type TEXT NOT NULL,
        body TEXT DEFAULT '', body_format TEXT DEFAULT 'markdown',
        scope_type TEXT DEFAULT 'global',
        track_id INTEGER REFERENCES job_tracks(id) ON DELETE CASCADE,
        round_id INTEGER,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        folder_key TEXT, source_type TEXT DEFAULT 'manual',
        source_ref_type TEXT, source_ref_id INTEGER,
        current_version INTEGER DEFAULT 1, editable INTEGER DEFAULT 1,
        locked_source INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
        metadata_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS document_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        version INTEGER NOT NULL, title TEXT NOT NULL, body TEXT DEFAULT '',
        change_summary TEXT, created_by TEXT DEFAULT 'user', source_run_id INTEGER,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(document_id, version)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS document_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        to_type TEXT NOT NULL, to_id INTEGER NOT NULL, relation TEXT NOT NULL,
        note TEXT, created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(from_document_id,to_type,to_id,relation)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id INTEGER NOT NULL REFERENCES job_tracks(id) ON DELETE CASCADE,
        round_number INTEGER NOT NULL DEFAULT 1, round_name TEXT, round_type TEXT,
        interview_mode TEXT DEFAULT 'unknown', language TEXT DEFAULT 'zh',
        scheduled_at TEXT, started_at TEXT, ended_at TEXT, status TEXT DEFAULT 'scheduled',
        preparation_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        organized_transcript_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        answer_sheet_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        review_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        next_plan_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        inherited_from_round_id INTEGER REFERENCES interview_rounds(id) ON DELETE SET NULL,
        notes TEXT, created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(track_id,round_number)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS round_document_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES interview_rounds(id) ON DELETE CASCADE,
        document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        use_type TEXT NOT NULL DEFAULT 'reference', purpose TEXT,
        inherited_from_round_id INTEGER,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(round_id,document_id,use_type)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES interview_rounds(id) ON DELETE CASCADE,
        participant_key TEXT NOT NULL, display_name TEXT, role TEXT NOT NULL DEFAULT 'unknown',
        organization_role TEXT, confidence REAL, confirmed INTEGER DEFAULT 0,
        metadata_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(round_id,participant_key)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transcript_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES interview_rounds(id) ON DELETE CASCADE,
        source_kind TEXT NOT NULL, title TEXT, raw_text TEXT, file_path TEXT, content_hash TEXT,
        locked INTEGER DEFAULT 1, metadata_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transcript_segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL REFERENCES transcript_sources(id) ON DELETE CASCADE,
        round_id INTEGER NOT NULL REFERENCES interview_rounds(id) ON DELETE CASCADE,
        segment_order INTEGER NOT NULL,
        participant_id INTEGER REFERENCES interview_participants(id) ON DELETE SET NULL,
        raw_text TEXT NOT NULL, edited_text TEXT, start_ms INTEGER, end_ms INTEGER,
        confidence REAL, confirmed INTEGER DEFAULT 0, metadata_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(source_id,segment_order)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS exam_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES interview_rounds(id) ON DELETE CASCADE,
        question_order INTEGER NOT NULL, parent_question_id INTEGER REFERENCES exam_questions(id) ON DELETE CASCADE,
        asker_participant_id INTEGER REFERENCES interview_participants(id) ON DELETE SET NULL,
        original_question TEXT NOT NULL, normalized_question TEXT, question_type TEXT,
        ability_key TEXT, intent TEXT, evaluation_criteria TEXT, difficulty TEXT,
        evidence_segment_ids_json TEXT DEFAULT '[]', tags TEXT, confirmed INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active', created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS question_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL REFERENCES exam_questions(id) ON DELETE CASCADE,
        participant_id INTEGER REFERENCES interview_participants(id) ON DELETE SET NULL,
        is_self INTEGER DEFAULT 0, original_answer TEXT, organized_answer TEXT,
        evidence_segment_ids_json TEXT DEFAULT '[]', interviewer_signal TEXT,
        confirmed INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS question_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL REFERENCES exam_questions(id) ON DELETE CASCADE,
        answer_id INTEGER REFERENCES question_answers(id) ON DELETE CASCADE,
        directness_score REAL, structure_score REAL, evidence_score REAL,
        relevance_score REAL, credibility_score REAL, overall_score REAL,
        strengths TEXT, weaknesses TEXT, better_approach TEXT, better_answer TEXT,
        evidence_segment_ids_json TEXT DEFAULT '[]', inference_level TEXT DEFAULT 'inferred',
        status TEXT DEFAULT 'current', created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_question_answer_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL REFERENCES exam_questions(id) ON DELETE CASCADE,
        answer_type TEXT NOT NULL DEFAULT 'user',
        body TEXT NOT NULL DEFAULT '',
        coaching TEXT,
        instruction TEXT,
        source_answer_id INTEGER REFERENCES question_answers(id) ON DELETE SET NULL,
        model_provider TEXT,
        model_name TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        created_by TEXT DEFAULT 'user',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(question_id,answer_type,version)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_question_coaching_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL REFERENCES exam_questions(id) ON DELETE CASCADE,
        source_answer_version INTEGER DEFAULT 0,
        interviewer_intent TEXT,
        why_asked TEXT,
        answer_summary TEXT,
        strengths_json TEXT DEFAULT '[]',
        issues_json TEXT DEFAULT '[]',
        satisfaction_criteria_json TEXT DEFAULT '[]',
        answer_strategy TEXT,
        improved_answer TEXT,
        followups_json TEXT DEFAULT '[]',
        evidence_gaps_json TEXT DEFAULT '[]',
        confidence_note TEXT,
        instruction TEXT,
        model_provider TEXT,
        model_name TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(question_id,version)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_question_coaching_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL REFERENCES exam_questions(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        proposal_json TEXT DEFAULT '{}',
        applied INTEGER DEFAULT 0,
        coaching_version INTEGER DEFAULT 0,
        answer_version INTEGER DEFAULT 0,
        model_provider TEXT,
        model_name TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES interview_rounds(id) ON DELETE CASCADE,
        label TEXT NOT NULL, probability REAL, confidence TEXT,
        positive_evidence_json TEXT DEFAULT '[]', negative_evidence_json TEXT DEFAULT '[]',
        uncertainty_json TEXT DEFAULT '[]', model_provider TEXT, model_name TEXT,
        prompt_version TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL UNIQUE REFERENCES interview_rounds(id) ON DELETE CASCADE,
        actual_result TEXT NOT NULL, result_at TEXT, evidence_type TEXT, evidence_text TEXT,
        user_note TEXT, calibration_note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS review_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES interview_rounds(id) ON DELETE CASCADE,
        question_id INTEGER REFERENCES exam_questions(id) ON DELETE SET NULL,
        action_type TEXT NOT NULL, target_type TEXT, target_id INTEGER, title TEXT NOT NULL,
        detail TEXT, priority TEXT DEFAULT 'medium', status TEXT DEFAULT 'proposed',
        proposed_payload_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_question_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL REFERENCES exam_questions(id) ON DELETE CASCADE,
        tag_type TEXT NOT NULL,
        tag_value TEXT NOT NULL,
        confidence REAL,
        source TEXT DEFAULT 'ai',
        confirmed INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(question_id,tag_type,tag_value)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_question_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL REFERENCES exam_questions(id) ON DELETE CASCADE,
        entity_type TEXT NOT NULL,
        entity_id INTEGER,
        entity_title TEXT NOT NULL,
        relation TEXT DEFAULT 'asked_about',
        aspect TEXT,
        confidence REAL,
        source TEXT DEFAULT 'ai',
        confirmed INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(question_id,entity_type,entity_id,relation)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_growth_analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        role_family TEXT NOT NULL,
        role_subtype TEXT,
        target_track_id INTEGER REFERENCES job_tracks(id) ON DELETE SET NULL,
        target_jd TEXT,
        date_from TEXT,
        date_to TEXT,
        selected_round_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'draft',
        progress INTEGER DEFAULT 0,
        stage TEXT,
        summary TEXT,
        profile_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        trend_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        preparation_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        mock_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        answer_sheet_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        reference_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        source_snapshot_json TEXT DEFAULT '{}',
        error_message TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_growth_mock_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        analysis_id INTEGER NOT NULL REFERENCES interview_growth_analyses(id) ON DELETE CASCADE,
        session_key TEXT NOT NULL UNIQUE,
        status TEXT DEFAULT 'active',
        question_target INTEGER DEFAULT 10,
        started_at TEXT DEFAULT (datetime('now','localtime')),
        ended_at TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(analysis_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS interview_growth_mock_voice_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        analysis_id INTEGER NOT NULL REFERENCES interview_growth_analyses(id) ON DELETE CASCADE,
        session_key TEXT NOT NULL,
        message_id INTEGER,
        audio_path TEXT NOT NULL,
        mime_type TEXT,
        duration_ms INTEGER DEFAULT 0,
        transcript_raw TEXT,
        transcript_edited TEXT,
        metrics_json TEXT DEFAULT '{}',
        provider TEXT,
        model TEXT,
        status TEXT DEFAULT 'saved',
        error_message TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_growth_mock_voice_analysis
        ON interview_growth_mock_voice_turns(analysis_id,id)""")
    c.execute("""CREATE TABLE IF NOT EXISTS legacy_entity_map (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        legacy_type TEXT NOT NULL, legacy_id INTEGER NOT NULL,
        new_type TEXT NOT NULL, new_id INTEGER NOT NULL, migration_version TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(legacy_type,legacy_id,new_type,migration_version)
    )""")
    _backfill_question_taxonomy(c)


def _growth_payload(row, include_documents=False):
    item = _row(row)
    if not item:
        return None
    for key, default in (("selected_round_ids_json", []), ("source_snapshot_json", {})):
        raw = item.pop(key, None)
        try:
            item[key.removesuffix("_json")] = json.loads(raw or json.dumps(default))
        except (TypeError, json.JSONDecodeError):
            item[key.removesuffix("_json")] = default
    if include_documents:
        item["documents"] = {}
        for name in ("profile", "trend", "preparation", "mock", "answer_sheet", "reference"):
            did = item.get(f"{name}_document_id")
            item["documents"][name] = get_document(did) if did else None
    return item


def create_growth_analysis(data):
    conn = _conn()
    cur = conn.execute("""INSERT INTO interview_growth_analyses
        (title,role_family,role_subtype,target_track_id,target_jd,date_from,date_to,
         selected_round_ids_json,status,progress,stage,source_snapshot_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        data["title"].strip(), data["role_family"].strip(),
        (data.get("role_subtype") or "").strip() or None,
        data.get("target_track_id"), data.get("target_jd"), data.get("date_from"),
        data.get("date_to"), json.dumps(data.get("selected_round_ids") or [], ensure_ascii=False),
        data.get("status") or "draft", int(data.get("progress") or 0),
        data.get("stage") or "等待生成",
        json.dumps(data.get("source_snapshot") or {}, ensure_ascii=False),
    ))
    analysis_id = cur.lastrowid
    conn.commit(); conn.close()
    return analysis_id


def update_growth_analysis(analysis_id, data):
    allowed = {
        "title", "role_family", "role_subtype", "target_track_id", "target_jd",
        "date_from", "date_to", "status", "progress", "stage", "summary",
        "profile_document_id", "trend_document_id", "preparation_document_id",
        "mock_document_id", "answer_sheet_document_id", "reference_document_id",
        "error_message",
    }
    values = {key: value for key, value in data.items() if key in allowed}
    if "selected_round_ids" in data:
        values["selected_round_ids_json"] = json.dumps(data["selected_round_ids"] or [], ensure_ascii=False)
    if "source_snapshot" in data:
        values["source_snapshot_json"] = json.dumps(data["source_snapshot"] or {}, ensure_ascii=False)
    if not values:
        return get_growth_analysis(analysis_id)
    conn = _conn()
    assignments = ",".join(f"{key}=?" for key in values)
    conn.execute(f"UPDATE interview_growth_analyses SET {assignments},updated_at=datetime('now','localtime') WHERE id=?",
                 (*values.values(), analysis_id))
    conn.commit(); conn.close()
    return get_growth_analysis(analysis_id)


def get_growth_analysis(analysis_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM interview_growth_analyses WHERE id=?", (analysis_id,)).fetchone()
    conn.close()
    return _growth_payload(row, include_documents=True)


def list_growth_analyses(limit=50):
    conn = _conn()
    rows = conn.execute("SELECT * FROM interview_growth_analyses ORDER BY updated_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [_growth_payload(row) for row in rows]


def get_growth_mock_session(analysis_id):
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM interview_growth_mock_sessions WHERE analysis_id=?",
        (analysis_id,),
    ).fetchone()
    conn.close()
    return _row(row)


def start_growth_mock_session(analysis_id, question_target=10, reset=False):
    conn = _conn()
    current = conn.execute(
        "SELECT * FROM interview_growth_mock_sessions WHERE analysis_id=?",
        (analysis_id,),
    ).fetchone()
    session_key = f"growth-mock-{analysis_id}"
    if current and reset:
        conn.execute("DELETE FROM interview_growth_mock_sessions WHERE analysis_id=?", (analysis_id,))
        current = None
    if not current:
        conn.execute("""INSERT INTO interview_growth_mock_sessions
            (analysis_id,session_key,status,question_target)
            VALUES (?,?,?,?)""", (
            analysis_id, session_key, "active", max(1, int(question_target or 10)),
        ))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM interview_growth_mock_sessions WHERE analysis_id=?",
        (analysis_id,),
    ).fetchone()
    conn.close()
    return _row(row)


def update_growth_mock_session(analysis_id, data):
    allowed = {"status", "question_target", "ended_at"}
    values = {key: value for key, value in data.items() if key in allowed}
    if not values:
        return get_growth_mock_session(analysis_id)
    conn = _conn()
    assignments = ",".join(f"{key}=?" for key in values)
    conn.execute(
        f"UPDATE interview_growth_mock_sessions SET {assignments},updated_at=datetime('now','localtime') WHERE analysis_id=?",
        (*values.values(), analysis_id),
    )
    conn.commit(); conn.close()
    return get_growth_mock_session(analysis_id)


def _growth_voice_payload(row):
    item = _row(row)
    if not item:
        return None
    try:
        item["metrics"] = json.loads(item.pop("metrics_json", "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        item["metrics"] = {}
    return item


def create_growth_mock_voice_turn(data):
    conn = _conn()
    cursor = conn.execute("""INSERT INTO interview_growth_mock_voice_turns
        (analysis_id,session_key,message_id,audio_path,mime_type,duration_ms,
         transcript_raw,transcript_edited,metrics_json,provider,model,status,error_message)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        data["analysis_id"], data["session_key"], data.get("message_id"),
        data["audio_path"], data.get("mime_type"), int(data.get("duration_ms") or 0),
        data.get("transcript_raw"), data.get("transcript_edited"),
        json.dumps(data.get("metrics") or {}, ensure_ascii=False),
        data.get("provider"), data.get("model"), data.get("status", "saved"),
        data.get("error_message"),
    ))
    voice_id = cursor.lastrowid
    conn.commit(); conn.close()
    return voice_id


def update_growth_mock_voice_turn(voice_id, data):
    allowed = {
        "message_id", "duration_ms", "transcript_raw", "transcript_edited",
        "provider", "model", "status", "error_message",
    }
    values = {key: value for key, value in data.items() if key in allowed}
    if "metrics" in data:
        values["metrics_json"] = json.dumps(data.get("metrics") or {}, ensure_ascii=False)
    if not values:
        return get_growth_mock_voice_turn(voice_id)
    conn = _conn()
    assignments = ",".join(f"{key}=?" for key in values)
    conn.execute(
        f"UPDATE interview_growth_mock_voice_turns SET {assignments},updated_at=datetime('now','localtime') WHERE id=?",
        (*values.values(), voice_id),
    )
    conn.commit(); conn.close()
    return get_growth_mock_voice_turn(voice_id)


def get_growth_mock_voice_turn(voice_id):
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM interview_growth_mock_voice_turns WHERE id=?", (voice_id,)
    ).fetchone()
    conn.close()
    return _growth_voice_payload(row)


def list_growth_mock_voice_turns(analysis_id):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM interview_growth_mock_voice_turns WHERE analysis_id=? ORDER BY id",
        (analysis_id,),
    ).fetchall()
    conn.close()
    return [_growth_voice_payload(row) for row in rows]


def clear_growth_mock_voice_turns(analysis_id):
    conn = _conn()
    rows = conn.execute(
        "SELECT audio_path FROM interview_growth_mock_voice_turns WHERE analysis_id=?",
        (analysis_id,),
    ).fetchall()
    conn.execute("DELETE FROM interview_growth_mock_voice_turns WHERE analysis_id=?", (analysis_id,))
    conn.commit(); conn.close()
    return [row["audio_path"] for row in rows if row["audio_path"]]


def _backfill_question_taxonomy(c):
    """Add knowledge topics and correct conceptual AI questions conservatively."""
    rows = c.execute("""SELECT id,original_question,normalized_question,intent,
        question_type FROM exam_questions WHERE status='active'""").fetchall()
    for row in rows:
        question_id = row["id"]
        text = " ".join(str(row[key] or "") for key in (
            "original_question", "normalized_question", "intent"
        ))
        for topic in infer_question_topics(text):
            c.execute("""INSERT OR IGNORE INTO interview_question_tags
                (question_id,tag_type,tag_value,confidence,source,confirmed)
                VALUES (?,?,?,?,?,?)""", (
                    question_id, "知识领域", topic, 0.9, "system", 0,
                ))
        if is_ai_concept_question(text) and row["question_type"] in {
            "项目深挖", "业务理解", "行为面试", "其他",
        }:
            c.execute("""UPDATE exam_questions SET question_type='AI认知',
                ability_key='AI产品判断',updated_at=datetime('now','localtime')
                WHERE id=?""", (question_id,))


def _conn():
    return db.get_db()


def create_document(data):
    conn = _conn()
    cur = conn.execute("""INSERT INTO documents
        (title,document_type,body,body_format,scope_type,track_id,round_id,project_id,
         folder_key,source_type,source_ref_type,source_ref_id,editable,locked_source,status,metadata_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        data['title'].strip(), data['document_type'], data.get('body') or '',
        data.get('body_format') or 'markdown', data.get('scope_type') or 'global',
        data.get('track_id'), data.get('round_id'), data.get('project_id'),
        data.get('folder_key'), data.get('source_type') or 'manual',
        data.get('source_ref_type'), data.get('source_ref_id'),
        int(data.get('editable', True)), int(data.get('locked_source', False)),
        data.get('status') or 'active', json.dumps(data.get('metadata') or {}, ensure_ascii=False)))
    did = cur.lastrowid
    conn.execute("""INSERT INTO document_versions
        (document_id,version,title,body,change_summary,created_by)
        VALUES (?,?,?,?,?,?)""", (did, 1, data['title'].strip(), data.get('body') or '',
                                    data.get('change_summary') or '创建文档', data.get('created_by') or 'user'))
    conn.commit(); conn.close()
    return did


def get_document(did, include_archived=False):
    conn = _conn()
    q = "SELECT * FROM documents WHERE id=?" + ("" if include_archived else " AND status!='archived'")
    item = _row(conn.execute(q, (did,)).fetchone())
    conn.close()
    return item


def list_documents(track_id=None, round_id=None, document_type=None, folder_key=None,
                   include_archived=False, project_id=None):
    conn = _conn(); q = "SELECT * FROM documents WHERE 1=1"; args = []
    if not include_archived: q += " AND status!='archived'"
    for col, value in (("track_id", track_id), ("round_id", round_id),
                       ("project_id", project_id), ("document_type", document_type),
                       ("folder_key", folder_key)):
        if value is not None:
            q += f" AND {col}=?"; args.append(value)
    q += " ORDER BY updated_at DESC,id DESC"
    items = _rows(conn.execute(q, args).fetchall()); conn.close(); return items


def update_question_text(question_id, question):
    conn = _conn()
    cur = conn.execute(
        """UPDATE exam_questions
           SET normalized_question=?,
               updated_at=datetime('now','localtime')
           WHERE id=? AND status='active'""",
        (question, question_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def list_document_candidates(track_id, round_id=None):
    """Documents a user may reference while preparing one interview round.

    We deliberately return references rather than cloning content.  A document
    from another job track can therefore inform this round without silently
    changing its original ownership.
    """
    conn = _conn()
    rows = _rows(conn.execute("""SELECT * FROM documents
        WHERE status!='archived' AND (track_id=? OR scope_type IN ('global','domain','company') OR document_type='project_pitch')
        ORDER BY CASE WHEN track_id=? THEN 0 ELSE 1 END, updated_at DESC, id DESC""",
        (track_id, track_id)).fetchall())
    conn.close()
    return rows


def migrate_legacy_documents():
    """Expose existing knowledge, assets and legacy reviews as v2 documents.

    This is deliberately an adapter migration: legacy records are never
    deleted or rewritten.  ``legacy_entity_map`` makes the import idempotent
    and preserves a trace back to the original record.
    """
    # Read all legacy rows first, then close that connection before performing
    # writes. SQLite otherwise keeps a read lock open while a new connection
    # tries to add documents, which is especially painful on a large vault.
    conn = _conn()
    existing_tables = {x['name'] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    existing_maps = {(x['legacy_type'], x['legacy_id']) for x in _rows(conn.execute(
        "SELECT legacy_type,legacy_id FROM legacy_entity_map WHERE new_type='document' AND migration_version='v2'").fetchall())}
    pending = []
    if 'knowledge_items' in existing_tables:
        pending += [('knowledge_item', item, 'professional_knowledge', item.get('title'), item.get('content'), item.get('scope_type'), item.get('topic'))
                    for item in _rows(conn.execute("SELECT * FROM knowledge_items WHERE COALESCE(status,'active')!='archived'").fetchall())]
    if 'assets' in existing_tables:
        mapping = {'self_intro': 'interview_answer', 'pitch': 'project_pitch', 'knowledge_card': 'professional_knowledge', 'gap_analysis': 'job_analysis', 'resume': 'resume_material'}
        pending += [('asset', item, mapping.get(item.get('asset_type'), 'generated_asset'), item.get('title'), item.get('body'), 'track' if item.get('track_id') else 'global', item.get('asset_type'))
                    for item in _rows(conn.execute("SELECT * FROM assets WHERE COALESCE(status,'draft')!='archived'").fetchall())]
    if 'projects' in existing_tables:
        pending += [('project', item, 'project_pitch', item.get('name'), item.get('document'), 'global', 'project')
                    for item in _rows(conn.execute("SELECT * FROM projects WHERE COALESCE(document,'')!=''").fetchall())]
    if 'interview_reviews' in existing_tables:
        for item in _rows(conn.execute("SELECT * FROM interview_reviews").fetchall()):
            body = '\n\n'.join(x for x in [item.get('summary'), item.get('source_text')] if x)
            pending.append(('interview_review', item, 'review_report', item.get('title') or '历史面试复盘', body, 'track' if item.get('track_id') else 'global', 'legacy_review'))
    conn.close()
    created = 0
    for legacy_type, item, document_type, title, body, scope_type, folder_key in pending:
        if (legacy_type, item['id']) in existing_maps:
            continue
        did = create_document({
            'title': title or f'历史资料 {item["id"]}', 'document_type': document_type,
            'body': body or '', 'scope_type': scope_type or 'global',
            'track_id': item.get('track_id'), 'project_id': item.get('project_id'),
            'folder_key': folder_key, 'source_type': 'legacy_migration',
            'source_ref_type': legacy_type, 'source_ref_id': item['id'],
            'change_summary': '迁移既有资料为统一可编辑文档', 'created_by': 'system',
        })
        map_conn = _conn()
        map_conn.execute("INSERT OR IGNORE INTO legacy_entity_map (legacy_type,legacy_id,new_type,new_id,migration_version) VALUES (?,?,?,?,?)", (legacy_type, item['id'], 'document', did, 'v2'))
        map_conn.commit(); map_conn.close()
        created += 1
    return created


def update_document(did, data, expected_version=None):
    conn = _conn(); current = _row(conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone())
    if not current: conn.close(); return None, 'not_found'
    if current['locked_source']: conn.close(); return None, 'locked'
    if expected_version is not None and int(expected_version) != int(current['current_version']):
        conn.close(); return current, 'conflict'
    title = data.get('title', current['title']).strip(); body = data.get('body', current['body'])
    version = current['current_version'] + 1
    conn.execute("""UPDATE documents SET title=?,body=?,current_version=?,updated_at=datetime('now','localtime') WHERE id=?""", (title, body, version, did))
    conn.execute("""INSERT INTO document_versions (document_id,version,title,body,change_summary,created_by)
        VALUES (?,?,?,?,?,?)""", (did, version, title, body, data.get('change_summary') or '编辑文档', data.get('created_by') or 'user'))
    conn.commit(); conn.close(); return get_document(did), None


def list_document_versions(did):
    conn = _conn(); rows = _rows(conn.execute("SELECT * FROM document_versions WHERE document_id=? ORDER BY version DESC", (did,)).fetchall()); conn.close(); return rows


def archive_document(did):
    conn = _conn()
    cur = conn.execute(
        "UPDATE documents SET status='archived',updated_at=datetime('now','localtime') WHERE id=?",
        (did,),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def restore_document(did):
    conn = _conn()
    cur = conn.execute(
        "UPDATE documents SET status='active',updated_at=datetime('now','localtime') WHERE id=?",
        (did,),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def create_round(track_id, data):
    conn = _conn()
    n = data.get('round_number')
    if n is None:
        n = (conn.execute("SELECT COALESCE(MAX(round_number),0)+1 FROM interview_rounds WHERE track_id=?", (track_id,)).fetchone()[0])
    cur = conn.execute("""INSERT INTO interview_rounds
      (track_id,round_number,round_name,round_type,interview_mode,language,scheduled_at,status,notes,inherited_from_round_id)
      VALUES (?,?,?,?,?,?,?,?,?,?)""", (track_id,n,data.get('round_name'),data.get('round_type'),data.get('interview_mode') or 'unknown',data.get('language') or 'zh',data.get('scheduled_at'),data.get('status') or 'scheduled',data.get('notes'),data.get('inherited_from_round_id')))
    rid=cur.lastrowid; conn.commit(); conn.close(); return rid


def get_round(rid):
    conn = _conn(); item = _row(conn.execute("SELECT * FROM interview_rounds WHERE id=?", (rid,)).fetchone()); conn.close(); return item


def list_rounds(track_id):
    conn = _conn(); items = _rows(conn.execute("SELECT * FROM interview_rounds WHERE track_id=? ORDER BY round_number", (track_id,)).fetchall()); conn.close(); return items


def update_round(rid, data):
    current=get_round(rid)
    if not current: return None
    allowed=('round_name','round_type','interview_mode','language','scheduled_at','started_at','ended_at','status','notes','preparation_document_id','organized_transcript_document_id','answer_sheet_document_id','review_document_id','next_plan_document_id')
    changes={key:data[key] for key in allowed if key in data}
    if not changes: return current
    sets=','.join(f"{key}=?" for key in changes)+",updated_at=datetime('now','localtime')"
    conn=_conn(); conn.execute(f"UPDATE interview_rounds SET {sets} WHERE id=?", (*changes.values(),rid)); conn.commit(); conn.close(); return get_round(rid)


def ensure_round_documents(rid):
    """Create the editable working documents every round needs exactly once."""
    round_ = get_round(rid)
    if not round_:
        return None
    defaults = (
        ('preparation_document_id', 'round_preparation', f"第 {round_['round_number']} 轮本轮准备",
         "# 本轮目标\n\n- \n\n# 保留上一轮有效内容\n\n- \n\n# 本轮新增重点\n\n- \n\n# 需要验证的问题\n\n- \n"),
        ('organized_transcript_document_id', 'organized_transcript', f"第 {round_['round_number']} 轮结构化逐字稿",
         "# 结构化逐字稿\n\n原始资料会保留不动；请在这里修正说话人、上下文和必要的口语整理。\n"),
        ('answer_sheet_document_id', 'answer_sheet', f"第 {round_['round_number']} 轮面试答卷",
         "# 面试答卷\n\n拆解完成后，题目、本人回答、追问与现场信号会同步到这里；你可以直接编辑。\n"),
    )
    updates = {}
    for field, doc_type, title, body in defaults:
        if not round_.get(field):
            did = create_document({
                'title': title, 'document_type': doc_type, 'body': body,
                'scope_type': 'track', 'track_id': round_['track_id'], 'round_id': rid,
                'source_type': 'system', 'created_by': 'system',
                'change_summary': '新建面试轮次工作文档',
            })
            updates[field] = did
            link_round_document(rid, did, 'generated', '本轮工作文档')
    return update_round(rid, updates) if updates else round_


def link_round_document(rid, did, use_type='reference', purpose=None, inherited_from_round_id=None):
    conn=_conn(); conn.execute("""INSERT OR IGNORE INTO round_document_links
      (round_id,document_id,use_type,purpose,inherited_from_round_id) VALUES (?,?,?,?,?)""", (rid,did,use_type,purpose,inherited_from_round_id)); conn.commit(); conn.close()


def list_round_documents(rid):
    conn=_conn(); rows=_rows(conn.execute("""SELECT l.*,d.title,d.document_type,d.body,d.track_id,d.round_id,
       d.source_type,d.source_ref_type,d.source_ref_id,d.metadata_json,d.updated_at
       FROM round_document_links l JOIN documents d ON d.id=l.document_id
       WHERE l.round_id=? AND d.status!='archived' ORDER BY l.use_type,d.updated_at DESC""",(rid,)).fetchall()); conn.close(); return rows


def remove_round_document(rid, did):
    """Remove one document from a round without damaging shared source material.

    The mandatory preparation document is reset to a fresh document instead of
    leaving the round without its working surface. Round-owned copies are
    archived; shared reference documents are only unlinked.
    """
    conn = _conn()
    round_ = _row(conn.execute("SELECT * FROM interview_rounds WHERE id=?", (rid,)).fetchone())
    link = _row(conn.execute(
        "SELECT * FROM round_document_links WHERE round_id=? AND document_id=?",
        (rid, did),
    ).fetchone())
    document = _row(conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone())
    if not round_ or not link or not document or document.get('status') == 'archived':
        conn.close()
        return None
    is_core = round_.get('preparation_document_id') == did
    conn.execute("DELETE FROM round_document_links WHERE round_id=? AND document_id=?", (rid, did))
    # Only archive documents created as isolated copies for this round. A
    # reference may still be used by the job knowledge base or another round.
    if document.get('round_id') == rid or link.get('use_type') != 'reference':
        conn.execute(
            "UPDATE documents SET status='archived',updated_at=datetime('now','localtime') WHERE id=?",
            (did,),
        )
    if is_core:
        conn.execute(
            "UPDATE interview_rounds SET preparation_document_id=NULL,updated_at=datetime('now','localtime') WHERE id=?",
            (rid,),
        )
    conn.commit()
    conn.close()
    if is_core:
        ensure_round_documents(rid)
    return {'action': 'reset' if is_core else 'removed', 'document_id': did}


def round_workspace(rid):
    round_=get_round(rid)
    if not round_: return None
    track=db.get_job_track(round_['track_id'])
    return {'round':round_, 'track':track, 'documents':list_round_documents(rid), 'all_rounds':list_rounds(round_['track_id'])}


def create_transcript_source(rid, data):
    conn=_conn()
    cur=conn.execute("""INSERT INTO transcript_sources
      (round_id,source_kind,title,raw_text,file_path,content_hash,locked,metadata_json)
      VALUES (?,?,?,?,?,?,?,?)""", (rid,data['source_kind'],data.get('title'),data.get('raw_text'),
        data.get('file_path'),data.get('content_hash'),int(data.get('locked',True)),json.dumps(data.get('metadata') or {},ensure_ascii=False)))
    sid=cur.lastrowid; conn.commit(); conn.close(); return sid


def list_transcript_sources(rid):
    conn=_conn(); items=_rows(conn.execute("SELECT * FROM transcript_sources WHERE round_id=? ORDER BY id",(rid,)).fetchall());conn.close();return items


def get_transcript_source(rid, source_id):
    conn = _conn()
    item = _row(conn.execute(
        "SELECT * FROM transcript_sources WHERE id=? AND round_id=?",
        (source_id, rid),
    ).fetchone())
    conn.close()
    return item


def delete_transcript_source(rid, source_id):
    """Delete source evidence and generated segments, preserving edited documents."""
    conn = _conn()
    cur = conn.execute(
        "DELETE FROM transcript_sources WHERE id=? AND round_id=?",
        (source_id, rid),
    )
    remaining_count = conn.execute(
        "SELECT COUNT(*) AS n FROM transcript_sources WHERE round_id=?",
        (rid,),
    ).fetchone()["n"]
    conn.commit()
    conn.close()
    return cur.rowcount > 0, remaining_count


def upsert_participant(rid, data):
    conn=_conn()
    current = _row(conn.execute("SELECT * FROM interview_participants WHERE round_id=? AND participant_key=?", (rid, data['participant_key'])).fetchone())
    # Manual role confirmation wins over a later automatic re-parse.
    if current and current.get('confirmed') and not data.get('confirmed'):
        conn.close(); return current
    conn.execute("""INSERT INTO interview_participants
      (round_id,participant_key,display_name,role,organization_role,confidence,confirmed,metadata_json)
      VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(round_id,participant_key) DO UPDATE SET
      display_name=excluded.display_name,role=excluded.role,organization_role=excluded.organization_role,
      confidence=excluded.confidence,confirmed=excluded.confirmed,metadata_json=excluded.metadata_json,
      updated_at=datetime('now','localtime')""", (rid,data['participant_key'],data.get('display_name'),data.get('role') or 'unknown',data.get('organization_role'),data.get('confidence'),int(data.get('confirmed',False)),json.dumps(data.get('metadata') or {},ensure_ascii=False)))
    item=_row(conn.execute("SELECT * FROM interview_participants WHERE round_id=? AND participant_key=?",(rid,data['participant_key'])).fetchone());conn.commit();conn.close();return item


def list_participants(rid):
    conn=_conn();items=_rows(conn.execute("SELECT * FROM interview_participants WHERE round_id=? ORDER BY CASE role WHEN 'self' THEN 0 WHEN 'interviewer' THEN 1 WHEN 'other_candidate' THEN 2 ELSE 3 END,id",(rid,)).fetchall());conn.close();return items


def replace_segments(source_id, rid, segments):
    """Replace only machine-generated segmentation; never alter source.raw_text."""
    conn=_conn(); conn.execute("DELETE FROM transcript_segments WHERE source_id=?", (source_id,))
    for order, segment in enumerate(segments, 1):
        conn.execute("""INSERT INTO transcript_segments
          (source_id,round_id,segment_order,participant_id,raw_text,edited_text,confidence,confirmed,metadata_json)
          VALUES (?,?,?,?,?,?,?,?,?)""", (source_id,rid,order,segment.get('participant_id'),segment['raw_text'],
            segment.get('edited_text'),segment.get('confidence'),int(segment.get('confirmed',False)),json.dumps(segment.get('metadata') or {},ensure_ascii=False)))
    conn.commit(); conn.close()


def list_segments(rid):
    conn=_conn();items=_rows(conn.execute("""SELECT s.*,p.participant_key,p.display_name,p.role
       FROM transcript_segments s LEFT JOIN interview_participants p ON p.id=s.participant_id
       WHERE s.round_id=? ORDER BY s.source_id,s.segment_order""",(rid,)).fetchall());conn.close();return items


def replace_exam(rid, questions):
    """Replace derived exam objects after a fresh decomposition, preserving raw sources."""
    conn=_conn(); conn.execute("DELETE FROM exam_questions WHERE round_id=?",(rid,))
    for order, question in enumerate(questions,1):
        tags = question.get('tags')
        if isinstance(tags, (dict, list)):
            tags = json.dumps(tags, ensure_ascii=False)
        cur=conn.execute("""INSERT INTO exam_questions
          (round_id,question_order,asker_participant_id,original_question,normalized_question,question_type,ability_key,intent,evidence_segment_ids_json,tags,confirmed)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",(rid,order,question.get('asker_participant_id'),question['original_question'],question.get('normalized_question'),question.get('question_type'),question.get('ability_key'),question.get('intent'),json.dumps(question.get('evidence_segment_ids') or []),tags,int(question.get('confirmed',False))))
        qid=cur.lastrowid
        for answer in question.get('answers') or []:
            conn.execute("""INSERT INTO question_answers
              (question_id,participant_id,is_self,original_answer,organized_answer,evidence_segment_ids_json,interviewer_signal,confirmed)
              VALUES (?,?,?,?,?,?,?,?)""",(qid,answer.get('participant_id'),int(answer.get('is_self',False)),answer.get('original_answer'),answer.get('organized_answer'),json.dumps(answer.get('evidence_segment_ids') or []),answer.get('interviewer_signal'),int(answer.get('confirmed',False))))
    conn.commit();conn.close()


def get_exam(rid):
    conn=_conn(); questions=_rows(conn.execute("SELECT * FROM exam_questions WHERE round_id=? AND status='active' ORDER BY question_order",(rid,)).fetchall())
    for question in questions:
        try:
            question['tags'] = json.loads(question.get('tags') or '{}')
        except (TypeError, ValueError):
            question['tags'] = {}
        question['answers']=_rows(conn.execute("""SELECT a.*,p.participant_key,p.display_name,p.role FROM question_answers a
           LEFT JOIN interview_participants p ON p.id=a.participant_id WHERE a.question_id=? ORDER BY a.id""",(question['id'],)).fetchall())
        question['tag_items'] = _rows(conn.execute(
            "SELECT * FROM interview_question_tags WHERE question_id=? ORDER BY tag_type,confirmed DESC,id",
            (question['id'],)).fetchall())
        question['entity_links'] = _rows(conn.execute(
            "SELECT * FROM interview_question_links WHERE question_id=? ORDER BY confirmed DESC,confidence DESC,id",
            (question['id'],)).fetchall())
        _attach_answer_workspace(conn, question)
    conn.close();return questions


def _attach_answer_workspace(conn, question):
    """Attach immutable evidence and editable answer versions to a question."""
    raw_answers = question.get("answers")
    if raw_answers is None:
        raw_answers = _rows(conn.execute("""SELECT a.*,p.participant_key,p.display_name,p.role
            FROM question_answers a LEFT JOIN interview_participants p ON p.id=a.participant_id
            WHERE a.question_id=? ORDER BY a.id""", (question["id"],)).fetchall())
        question["answers"] = raw_answers
    versions = _rows(conn.execute("""SELECT * FROM interview_question_answer_versions
        WHERE question_id=? ORDER BY answer_type,version DESC,id DESC""",
        (question["id"],)).fetchall())
    latest = {}
    for item in versions:
        latest.setdefault(item["answer_type"], item)
    self_answer = next((item for item in raw_answers if item.get("is_self")), None)
    evidence_body = ""
    if self_answer:
        evidence_body = (
            self_answer.get("organized_answer")
            or self_answer.get("original_answer")
            or ""
        )
    user_version = latest.get("user")
    ai_version = latest.get("ai")
    coaching = _row(conn.execute("""SELECT * FROM interview_question_coaching_versions
        WHERE question_id=? ORDER BY version DESC,id DESC LIMIT 1""",
        (question["id"],)).fetchone())
    if coaching:
        for field in (
            "strengths_json", "issues_json", "satisfaction_criteria_json",
            "followups_json", "evidence_gaps_json",
        ):
            try:
                coaching[field.removesuffix("_json")] = json.loads(
                    coaching.get(field) or "[]"
                )
            except (TypeError, ValueError):
                coaching[field.removesuffix("_json")] = []
    coaching_messages = _rows(conn.execute("""SELECT * FROM
        interview_question_coaching_messages WHERE question_id=?
        ORDER BY id""", (question["id"],)).fetchall())
    for message in coaching_messages:
        try:
            message["proposal"] = json.loads(message.get("proposal_json") or "{}")
        except (TypeError, ValueError):
            message["proposal"] = {}
    question["answer_versions"] = versions
    question["answer_workspace"] = {
        "evidence_answer": evidence_body,
        "evidence_answer_id": self_answer.get("id") if self_answer else None,
        "current_answer": (
            user_version.get("body") if user_version else evidence_body
        ),
        "current_version": user_version.get("version") if user_version else 0,
        "ai_answer": ai_version.get("body") if ai_version else "",
        "ai_coaching": ai_version.get("coaching") if ai_version else "",
        "ai_version": ai_version.get("version") if ai_version else 0,
        "question_coaching": coaching,
        "coaching_messages": coaching_messages,
        "has_detected_answer": bool(evidence_body.strip()),
    }
    return question


def update_question_enrichment(question_id, data):
    allowed = ('normalized_question', 'question_type', 'ability_key', 'intent',
               'evaluation_criteria', 'difficulty', 'tags', 'confirmed')
    changes = {key: data[key] for key in allowed if key in data and data[key] is not None}
    if not changes:
        return None
    sets = ','.join(f"{key}=?" for key in changes) + ",updated_at=datetime('now','localtime')"
    conn = _conn()
    conn.execute(f"UPDATE exam_questions SET {sets} WHERE id=?", (*changes.values(), question_id))
    conn.commit()
    item = _row(conn.execute("SELECT * FROM exam_questions WHERE id=?", (question_id,)).fetchone())
    conn.close()
    return item


def get_question(question_id):
    conn = _conn()
    item = _row(conn.execute("""SELECT q.*,r.track_id,r.round_number,r.round_name,
        t.company,t.role FROM exam_questions q
        JOIN interview_rounds r ON r.id=q.round_id
        JOIN job_tracks t ON t.id=r.track_id WHERE q.id=?""", (question_id,)).fetchone())
    conn.close()
    if not item:
        return None
    question = next(
        (q for q in get_exam(item["round_id"]) if q["id"] == question_id),
        None,
    )
    if not question:
        return item
    # get_exam attaches answers and editable versions; retain the joined
    # company, role and round context needed by the standalone question page.
    return {**item, **question}


def save_question_answer_version(question_id, data):
    question = get_question(question_id)
    if not question:
        return None
    answer_type = data.get("answer_type") or "user"
    if answer_type not in {"user", "ai"}:
        raise ValueError("unsupported answer type")
    body = str(data.get("body") or "").strip()
    if not body:
        raise ValueError("answer body is empty")
    conn = _conn()
    current = conn.execute("""SELECT COALESCE(MAX(version),0) FROM
        interview_question_answer_versions WHERE question_id=? AND answer_type=?""",
        (question_id, answer_type)).fetchone()[0]
    cur = conn.execute("""INSERT INTO interview_question_answer_versions
        (question_id,answer_type,body,coaching,instruction,source_answer_id,
         model_provider,model_name,version,created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            question_id, answer_type, body, data.get("coaching"),
            data.get("instruction"), data.get("source_answer_id"),
            data.get("model_provider"), data.get("model_name"), current + 1,
            data.get("created_by") or ("ai" if answer_type == "ai" else "user"),
        ))
    conn.commit()
    item = _row(conn.execute(
        "SELECT * FROM interview_question_answer_versions WHERE id=?",
        (cur.lastrowid,)).fetchone())
    conn.close()
    return item


def save_question_coaching_version(question_id, data):
    """Persist one immutable deep-coaching result for the current answer."""
    if not get_question(question_id):
        return None
    conn = _conn()
    current = conn.execute("""SELECT COALESCE(MAX(version),0)
        FROM interview_question_coaching_versions WHERE question_id=?""",
        (question_id,)).fetchone()[0]
    cur = conn.execute("""INSERT INTO interview_question_coaching_versions
        (question_id,source_answer_version,interviewer_intent,why_asked,
         answer_summary,strengths_json,issues_json,satisfaction_criteria_json,
         answer_strategy,improved_answer,followups_json,evidence_gaps_json,
         confidence_note,instruction,model_provider,model_name,version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            question_id, data.get("source_answer_version") or 0,
            data.get("interviewer_intent"), data.get("why_asked"),
            data.get("answer_summary"),
            json.dumps(data.get("strengths") or [], ensure_ascii=False),
            json.dumps(data.get("issues") or [], ensure_ascii=False),
            json.dumps(data.get("satisfaction_criteria") or [], ensure_ascii=False),
            data.get("answer_strategy"), data.get("improved_answer"),
            json.dumps(data.get("followups") or [], ensure_ascii=False),
            json.dumps(data.get("evidence_gaps") or [], ensure_ascii=False),
            data.get("confidence_note"), data.get("instruction"),
            data.get("model_provider"), data.get("model_name"), current + 1,
        ))
    conn.commit()
    item = _row(conn.execute(
        "SELECT * FROM interview_question_coaching_versions WHERE id=?",
        (cur.lastrowid,)).fetchone())
    conn.close()
    return item


def save_question_coaching_message(question_id, data):
    """Persist one user/assistant turn in the question's coaching thread."""
    if not get_question(question_id):
        return None
    role = str(data.get("role") or "").strip()
    if role not in {"user", "assistant"}:
        raise ValueError("unsupported coaching message role")
    content = str(data.get("content") or "").strip()
    if not content:
        raise ValueError("coaching message is empty")
    conn = _conn()
    cur = conn.execute("""INSERT INTO interview_question_coaching_messages
        (question_id,role,content,proposal_json,applied,coaching_version,
         answer_version,model_provider,model_name)
        VALUES (?,?,?,?,?,?,?,?,?)""", (
            question_id, role, content,
            json.dumps(data.get("proposal") or {}, ensure_ascii=False),
            int(bool(data.get("applied"))),
            data.get("coaching_version") or 0,
            data.get("answer_version") or 0,
            data.get("model_provider"), data.get("model_name"),
        ))
    conn.commit()
    item = _row(conn.execute("""SELECT * FROM
        interview_question_coaching_messages WHERE id=?""",
        (cur.lastrowid,)).fetchone())
    conn.close()
    try:
        item["proposal"] = json.loads(item.get("proposal_json") or "{}")
    except (TypeError, ValueError):
        item["proposal"] = {}
    return item


def mark_question_coaching_message_applied(message_id):
    conn = _conn()
    conn.execute("""UPDATE interview_question_coaching_messages
        SET applied=1 WHERE id=?""", (message_id,))
    conn.commit()
    item = _row(conn.execute("""SELECT * FROM
        interview_question_coaching_messages WHERE id=?""",
        (message_id,)).fetchone())
    conn.close()
    return item


def get_question_coaching_message(message_id):
    conn = _conn()
    item = _row(conn.execute("""SELECT * FROM
        interview_question_coaching_messages WHERE id=?""",
        (message_id,)).fetchone())
    conn.close()
    if not item:
        return None
    try:
        item["proposal"] = json.loads(item.get("proposal_json") or "{}")
    except (TypeError, ValueError):
        item["proposal"] = {}
    return item


def save_question_classification(question_id, data):
    """Replace the editable classification without changing the original question."""
    question = get_question(question_id)
    if not question:
        return None
    conn = _conn()
    for field in ("question_type", "ability_key", "intent"):
        if field in data:
            conn.execute(
                f"UPDATE exam_questions SET {field}=?,updated_at=datetime('now','localtime') WHERE id=?",
                (data.get(field), question_id),
            )
    if "tags" in data:
        conn.execute("DELETE FROM interview_question_tags WHERE question_id=?", (question_id,))
        for tag in data.get("tags") or []:
            value = str(tag.get("value") or "").strip()
            tag_type = str(tag.get("type") or "topic").strip()
            if not value:
                continue
            conn.execute("""INSERT INTO interview_question_tags
                (question_id,tag_type,tag_value,confidence,source,confirmed)
                VALUES (?,?,?,?,?,?)""", (
                    question_id, tag_type, value, tag.get("confidence"),
                    tag.get("source") or "user", int(tag.get("confirmed", True)),
                ))
    if "links" in data:
        conn.execute("DELETE FROM interview_question_links WHERE question_id=?", (question_id,))
        for link in data.get("links") or []:
            title = str(link.get("entity_title") or "").strip()
            entity_type = str(link.get("entity_type") or "").strip()
            if not title or not entity_type:
                continue
            conn.execute("""INSERT INTO interview_question_links
                (question_id,entity_type,entity_id,entity_title,relation,aspect,
                 confidence,source,confirmed)
                VALUES (?,?,?,?,?,?,?,?,?)""", (
                    question_id, entity_type, link.get("entity_id"), title,
                    link.get("relation") or "asked_about", link.get("aspect"),
                    link.get("confidence"), link.get("source") or "user",
                    int(link.get("confirmed", True)),
                ))
    confirmed = data.get("confirmed")
    if confirmed is not None:
        conn.execute("""UPDATE exam_questions SET confirmed=?,
            updated_at=datetime('now','localtime') WHERE id=?""",
            (int(bool(confirmed)), question_id))
    conn.commit()
    conn.close()
    return get_question(question_id)


def list_linked_questions(entity_type, entity_id=None, confirmed_only=True, limit=100):
    conn = _conn()
    where = ["l.entity_type=?"]
    args = [entity_type]
    if entity_id is not None:
        where.append("l.entity_id=?")
        args.append(entity_id)
    if confirmed_only:
        where.append("l.confirmed=1")
    args.append(max(1, min(int(limit or 100), 300)))
    items = _rows(conn.execute(f"""SELECT q.*,l.entity_type,l.entity_id,l.entity_title,
        l.aspect,l.confirmed AS link_confirmed,r.track_id,r.round_number,r.round_name,
        t.company,t.role
        FROM interview_question_links l
        JOIN exam_questions q ON q.id=l.question_id
        JOIN interview_rounds r ON r.id=q.round_id
        JOIN job_tracks t ON t.id=r.track_id
        WHERE {' AND '.join(where)} AND q.status='active'
        ORDER BY q.updated_at DESC,q.id DESC LIMIT ?""", args).fetchall())
    conn.close()
    return items


def replace_question_reviews(rid, reviews):
    conn=_conn()
    qids=[x['id'] for x in _rows(conn.execute("SELECT id FROM exam_questions WHERE round_id=?",(rid,)).fetchall())]
    if qids:
        conn.execute("DELETE FROM question_reviews WHERE question_id IN (%s)" % ','.join('?'*len(qids)),qids)
    for review in reviews:
        conn.execute("""INSERT INTO question_reviews
          (question_id,answer_id,directness_score,structure_score,evidence_score,relevance_score,credibility_score,overall_score,strengths,weaknesses,better_approach,better_answer,evidence_segment_ids_json,inference_level)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(review['question_id'],review.get('answer_id'),review.get('directness_score'),review.get('structure_score'),review.get('evidence_score'),review.get('relevance_score'),review.get('credibility_score'),review.get('overall_score'),review.get('strengths'),review.get('weaknesses'),review.get('better_approach'),review.get('better_answer'),json.dumps(review.get('evidence_segment_ids') or []),review.get('inference_level') or 'inferred'))
    conn.commit();conn.close()


def list_question_reviews(rid):
    conn=_conn(); items=_rows(conn.execute("""SELECT r.*,q.question_order,q.original_question,q.normalized_question
       FROM question_reviews r JOIN exam_questions q ON q.id=r.question_id WHERE q.round_id=? ORDER BY q.question_order,r.id""",(rid,)).fetchall());conn.close();return items


def create_prediction(rid, data):
    conn=_conn();cur=conn.execute("""INSERT INTO interview_predictions
      (round_id,label,probability,confidence,positive_evidence_json,negative_evidence_json,uncertainty_json,model_provider,model_name,prompt_version)
      VALUES (?,?,?,?,?,?,?,?,?,?)""",(rid,data['label'],data.get('probability'),data.get('confidence'),json.dumps(data.get('positive_evidence') or [],ensure_ascii=False),json.dumps(data.get('negative_evidence') or [],ensure_ascii=False),json.dumps(data.get('uncertainty') or [],ensure_ascii=False),data.get('model_provider'),data.get('model_name'),data.get('prompt_version')));pid=cur.lastrowid;conn.commit();conn.close();return pid


def latest_prediction(rid):
    conn=_conn();item=_row(conn.execute("SELECT * FROM interview_predictions WHERE round_id=? ORDER BY id DESC LIMIT 1",(rid,)).fetchone());conn.close();return item


def replace_review_actions(rid, actions):
    conn=_conn();conn.execute("DELETE FROM review_actions WHERE round_id=? AND status='proposed'",(rid,))
    for action in actions:
        conn.execute("""INSERT INTO review_actions (round_id,question_id,action_type,target_type,target_id,title,detail,priority,status,proposed_payload_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",(rid,action.get('question_id'),action['action_type'],action.get('target_type'),action.get('target_id'),action['title'],action.get('detail'),action.get('priority') or 'medium','proposed',json.dumps(action.get('proposed_payload') or {},ensure_ascii=False)))
    conn.commit();conn.close()


def list_review_actions(rid):
    conn=_conn();items=_rows(conn.execute("SELECT * FROM review_actions WHERE round_id=? ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,id DESC",(rid,)).fetchall());conn.close()
    for item in items:
        try:
            item["proposed_payload"] = json.loads(item.get("proposed_payload_json") or "{}")
        except Exception:
            item["proposed_payload"] = {}
    return items


def update_review_action(action_id, data):
    allowed = {"title", "detail", "priority", "status"}
    changes = {k: v for k, v in data.items() if k in allowed}
    if not changes:
        conn = _conn()
        item = _row(conn.execute("SELECT * FROM review_actions WHERE id=?", (action_id,)).fetchone())
        conn.close()
        return item
    sets = ",".join(f"{key}=?" for key in changes)
    conn = _conn()
    cur = conn.execute(
        f"UPDATE review_actions SET {sets},updated_at=datetime('now','localtime') WHERE id=?",
        (*changes.values(), action_id),
    )
    item = _row(conn.execute("SELECT * FROM review_actions WHERE id=?", (action_id,)).fetchone())
    conn.commit()
    conn.close()
    return item if cur.rowcount else None


def set_review_action_target(action_id, target_type, target_id, proposed_payload):
    conn = _conn()
    cur = conn.execute(
        """UPDATE review_actions
           SET target_type=?,target_id=?,proposed_payload_json=?,
               updated_at=datetime('now','localtime')
           WHERE id=?""",
        (
            target_type,
            target_id,
            json.dumps(proposed_payload or {}, ensure_ascii=False),
            action_id,
        ),
    )
    item = _row(conn.execute(
        "SELECT * FROM review_actions WHERE id=?", (action_id,)
    ).fetchone())
    conn.commit()
    conn.close()
    return item if cur.rowcount else None


def apply_review_action(action_id):
    """Accept a proposed action and append it to this round's editable plan."""
    conn = _conn()
    action = _row(conn.execute("SELECT * FROM review_actions WHERE id=?", (action_id,)).fetchone())
    if not action:
        conn.close(); return None
    round_ = _row(conn.execute("SELECT * FROM interview_rounds WHERE id=?", (action['round_id'],)).fetchone())
    conn.close()
    if not round_:
        return None
    did = round_.get('next_plan_document_id') or round_.get('preparation_document_id')
    doc = get_document(did) if did else None
    if not doc:
        did = create_document({'title': f"第 {round_['round_number']} 轮后续训练", 'document_type': 'next_plan',
            'body': '# 下一轮行动计划\n', 'scope_type': 'track', 'track_id': round_['track_id'], 'round_id': round_['id'],
            'source_type': 'system', 'created_by': 'system', 'change_summary': '创建复盘行动计划'})
        update_round(round_['id'], {'next_plan_document_id': did})
        link_round_document(round_['id'], did, 'generated', '复盘行动计划')
        doc = get_document(did)
    addition = f"\n\n## {action['title']}\n\n{action.get('detail') or ''}\n\n- 状态：待完成\n"
    update_document(did, {'body': (doc.get('body') or '') + addition, 'change_summary': '采纳复盘行动', 'created_by': 'user'})
    try:
        payload = json.loads(action.get("proposed_payload_json") or "{}")
    except Exception:
        payload = {}
    payload["plan_document_id"] = did
    conn = _conn()
    conn.execute(
        "UPDATE review_actions SET status='accepted',proposed_payload_json=?,updated_at=datetime('now','localtime') WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), action_id),
    )
    saved = _row(conn.execute("SELECT * FROM review_actions WHERE id=?", (action_id,)).fetchone())
    conn.commit(); conn.close()
    saved["proposed_payload"] = payload
    return {'action': saved, 'document_id': did}


def set_outcome(rid, data):
    conn=_conn();conn.execute("""INSERT INTO interview_outcomes (round_id,actual_result,result_at,evidence_type,evidence_text,user_note,calibration_note)
      VALUES (?,?,?,?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET actual_result=excluded.actual_result,result_at=excluded.result_at,evidence_type=excluded.evidence_type,evidence_text=excluded.evidence_text,user_note=excluded.user_note,calibration_note=excluded.calibration_note,updated_at=datetime('now','localtime')""",(rid,data['actual_result'],data.get('result_at'),data.get('evidence_type'),data.get('evidence_text'),data.get('user_note'),data.get('calibration_note')));conn.commit();conn.close()


def get_outcome(rid):
    conn=_conn();item=_row(conn.execute("SELECT * FROM interview_outcomes WHERE round_id=?",(rid,)).fetchone());conn.close();return item


def interview_library(query=None, track_id=None):
    """Read model for the global archive, not a second action workspace."""
    conn = _conn()
    where = ["1=1"]; args = []
    if track_id is not None:
        where.append("r.track_id=?"); args.append(track_id)
    if query:
        where.append("(t.company LIKE ? OR t.role LIKE ? OR r.round_name LIKE ? OR q.normalized_question LIKE ? OR q.ability_key LIKE ?)")
        like = f"%{query.strip()}%"; args.extend([like] * 5)
    rounds = _rows(conn.execute(f"""SELECT r.*,t.company,t.role,o.actual_result,o.evidence_text,o.user_note,o.calibration_note,
        p.label AS prediction_label,p.probability AS prediction_probability,
        COUNT(DISTINCT q.id) AS question_count
        FROM interview_rounds r JOIN job_tracks t ON t.id=r.track_id
        LEFT JOIN interview_outcomes o ON o.round_id=r.id
        LEFT JOIN interview_predictions p ON p.id=(SELECT id FROM interview_predictions ip WHERE ip.round_id=r.id ORDER BY id DESC LIMIT 1)
        LEFT JOIN exam_questions q ON q.round_id=r.id AND q.status='active'
        WHERE {' AND '.join(where)}
        GROUP BY r.id ORDER BY COALESCE(r.scheduled_at,r.created_at) DESC,r.id DESC""", args).fetchall())
    questions = _rows(conn.execute(f"""SELECT q.*,r.track_id,r.round_number,r.round_name,t.company,t.role,
        COUNT(a.id) AS answer_count
        FROM exam_questions q JOIN interview_rounds r ON r.id=q.round_id JOIN job_tracks t ON t.id=r.track_id
        LEFT JOIN question_answers a ON a.question_id=q.id AND a.is_self=1
        WHERE {' AND '.join(where)}
        GROUP BY q.id ORDER BY q.updated_at DESC,q.id DESC LIMIT 300""", args).fetchall())
    for question in questions:
        question["tag_items"] = _rows(conn.execute(
            "SELECT * FROM interview_question_tags WHERE question_id=? ORDER BY tag_type,confirmed DESC,id",
            (question["id"],)).fetchall())
        question["entity_links"] = _rows(conn.execute(
            "SELECT * FROM interview_question_links WHERE question_id=? ORDER BY confirmed DESC,confidence DESC,id",
            (question["id"],)).fetchall())
        _attach_answer_workspace(conn, question)
    outcomes = _rows(conn.execute("""SELECT o.actual_result,p.label,p.probability
        FROM interview_outcomes o JOIN interview_rounds r ON r.id=o.round_id
        LEFT JOIN interview_predictions p ON p.id=(SELECT id FROM interview_predictions ip WHERE ip.round_id=r.id ORDER BY id DESC LIMIT 1)
        WHERE o.actual_result IN ('passed','failed')""").fetchall())
    conn.close()
    comparable = [x for x in outcomes if x.get('label') in ('likely_pass','likely_fail')]
    hits = sum(1 for x in comparable if (x['label']=='likely_pass' and x['actual_result']=='passed') or (x['label']=='likely_fail' and x['actual_result']=='failed'))
    return {"rounds": rounds, "questions": questions, "calibration": {"sample_size": len(comparable), "hit_count": hits, "accuracy": round(hits / len(comparable) * 100, 1) if comparable else None}}
