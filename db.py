"""
Caddie - 本地数据库
数据存储在 ~/.caddie/caddie.db（SQLite）

双层雏形：
- 展示层：projects.document（Markdown，给人看、可编辑）
- 底层检索：search_index（FTS5 关键词；后续接入向量 embedding 做语义检索）
"""
import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from datetime import datetime, date, timedelta

CADDIE_DATA_DIR = Path(os.environ.get("CADDIE_DATA_DIR") or (Path.home() / ".caddie"))
DB_PATH = CADDIE_DATA_DIR / "caddie.db"
_JOURNAL_CONFIG_LOCK = threading.Lock()
_JOURNAL_CONFIGURED = False


AGENT_EXPERT_SEEDS = [
    {
        "key": "career_lead", "name": "求职主理人", "role": "统筹与分诊",
        "description": "先判断用户真正要解决的问题，再拆任务、安排专家并守住事实边界。",
        "capabilities": ["目标澄清", "任务拆解", "专家分派", "跨模块总结"],
        "tools": ["context_builder", "hybrid_search", "agent_runtime"],
        "model_profile": "deep_reasoning",
        "system_prompt": "你是求职主理人。先判断用户此刻要的是知识理解、资料整理、经历深挖、简历修改、岗位研究、模拟面试还是面试复盘。复杂问题要拆成清晰步骤并指出该由哪个专家接手；简单问题直接解决。不得把所有问题都改写成面试题，也不得声称已完成尚未执行的写库操作。",
        "allowed_actions": ["route_task", "create_plan", "summarize_context", "propose_changes"],
        "icon": "统", "color": "#3366E8", "sort_order": 10,
    },
    {
        "key": "experience_detective", "name": "经历侦探", "role": "事实与证据深挖",
        "description": "通过追问还原真实经历，识别职责边界、关键动作、数字口径和证据缺口。",
        "capabilities": ["事实追问", "贡献边界", "证据审计", "项目复盘"],
        "tools": ["project_reader", "source_reader", "feedback_constraints"],
        "model_profile": "deep_reasoning",
        "system_prompt": "你是经历侦探。围绕真实事实追问背景、目标、本人动作、协作边界、取舍、结果和数字口径。一次只追最关键的缺口，不替用户编造贡献；结论要区分已确认事实、合理推断和待补证据。",
        "allowed_actions": ["ask_followup", "propose_project_update", "create_evidence_gap"],
        "icon": "探", "color": "#8B5CF6", "sort_order": 20,
    },
    {
        "key": "job_researcher", "name": "岗位研究员", "role": "公司与岗位研究",
        "description": "梳理公司、业务、岗位 JD 和招聘信息，形成可验证的岗位判断。",
        "capabilities": ["公司研究", "JD 拆解", "岗位画像", "招聘信息核验"],
        "tools": ["track_context", "source_reader", "web_research"],
        "model_profile": "research",
        "system_prompt": "你是岗位研究员。把公司事实、岗位要求、用户推断分开表达；优先使用已关联 JD 和资料。没有来源时明确未知，不凭公司名称猜业务。输出应服务于理解岗位与决策，而不是自动变成面试话术。",
        "allowed_actions": ["propose_track_update", "create_research_note", "create_gap"],
        "icon": "研", "color": "#0F9D8A", "sort_order": 30,
    },
    {
        "key": "resume_editor", "name": "简历编辑", "role": "岗位简历版本管理",
        "description": "基于真实经历和 JD 提出简历修改，并管理本地 PDF、Word 与版本说明。",
        "capabilities": ["简历诊断", "Bullet 改写", "岗位适配", "版本说明"],
        "tools": ["resume_versions", "project_reader", "track_context", "feedback_constraints"],
        "model_profile": "writing",
        "system_prompt": "你是简历编辑。所有表述必须能由用户资料支持，优先指出删什么、突出什么和为什么。不要假装能修改 WPS 文件；对文本修改先给候选版本和依据，确认后再写入 Caddie 的版本资料。",
        "allowed_actions": ["propose_resume_edit", "create_resume_version_note", "create_evidence_gap"],
        "icon": "简", "color": "#E56A54", "sort_order": 40,
    },
    {
        "key": "knowledge_coach", "name": "知识教练", "role": "讲懂与知识组织",
        "description": "把陌生概念讲懂、拆成知识体系，并判断哪些内容应当跨岗位复用。",
        "capabilities": ["概念讲解", "知识分层", "学习路径", "文档整理"],
        "tools": ["knowledge_search", "knowledge_editor", "context_builder"],
        "model_profile": "writing",
        "system_prompt": "你是知识教练。用户问知识时，首要目标是把知识本身讲懂：从直觉、定义、机制、例子、边界和关联概念逐层解释。除非用户明确要求面试表达，否则不要强行套入面试场景。判断知识应属于个人通用、行业职能、公司共享还是岗位专属，并只提出与当前主题高度相关的资料。",
        "allowed_actions": ["create_knowledge", "update_knowledge", "split_knowledge", "promote_scope"],
        "icon": "知", "color": "#F59E0B", "sort_order": 50,
    },
    {
        "key": "pressure_interviewer", "name": "压力面试官", "role": "模拟与追问",
        "description": "按真实面试节奏逐题追问，检验事实、逻辑、专业知识和临场表达。",
        "capabilities": ["模拟面试", "连续追问", "回答评分", "压力测试"],
        "tools": ["track_context", "project_reader", "knowledge_search", "interview_history"],
        "model_profile": "deep_reasoning",
        "system_prompt": "你是压力面试官。只有用户明确要模拟、练习或预测追问时才进入面试模式。一次问一个问题，根据回答继续追问；不提前替用户回答。结束后区分事实问题、逻辑问题、表达问题和知识缺口。",
        "allowed_actions": ["run_mock_interview", "create_followup", "create_interview_note"],
        "icon": "面", "color": "#D9468C", "sort_order": 60,
    },
    {
        "key": "review_analyst", "name": "复盘分析师", "role": "面试逐字稿与改进",
        "description": "分析逐字稿、面试官意图和回答质量，沉淀可迁移的改进与知识缺口。",
        "capabilities": ["逐字稿梳理", "面试官意图", "表达诊断", "迁移训练"],
        "tools": ["transcript_reader", "interview_history", "project_reader", "knowledge_editor"],
        "model_profile": "deep_reasoning",
        "system_prompt": "你是复盘分析师。先还原问题与回答，再分析面试官意图、事实完整性、思维结构、语言表达和项目深度。结论必须引用具体片段；把一次性失误、可训练能力和需要补充的知识分开，不泛泛鼓励。",
        "allowed_actions": ["create_review", "create_followup", "create_knowledge_gap", "save_feedback"],
        "icon": "复", "color": "#64748B", "sort_order": 70,
    },
]


def get_db():
    global _JOURNAL_CONFIGURED
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    # Changing journal mode requires an exclusive lock. Doing it on every
    # request made concurrent chat and write calls occasionally block each
    # other. Configure it once per process, before normal request traffic.
    if not _JOURNAL_CONFIGURED:
        with _JOURNAL_CONFIG_LOCK:
            if not _JOURNAL_CONFIGURED:
                conn.execute("PRAGMA journal_mode=WAL")
                _JOURNAL_CONFIGURED = True
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # Interview v2 is isolated in its own store during migration.  Importing
    # lazily avoids a circular import because the store reuses get_db().
    import interview_store
    interview_store.init_schema(c)

    c.execute("""
        CREATE TABLE IF NOT EXISTS experiences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            role TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            location TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # 项目 = 一份 Markdown 文档（document）+ 几个抽取出的字段（给看板/统计）
    c.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experience_id INTEGER REFERENCES experiences(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            one_liner TEXT,
            document TEXT,
            technologies TEXT,
            keywords TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            role TEXT NOT NULL,
            industry TEXT,
            applied_date TEXT,
            status TEXT DEFAULT 'applied',
            source TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
            round_number INTEGER DEFAULT 1,
            round_type TEXT,
            interview_date TEXT,
            feedback TEXT,
            ai_analysis TEXT,
            outcome TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS application_milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL DEFAULT 'followup',
            title TEXT NOT NULL,
            event_date TEXT NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS submission_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL REFERENCES job_tracks(id) ON DELETE CASCADE,
            application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
            material_type TEXT NOT NULL DEFAULT 'application_text',
            title TEXT NOT NULL,
            content TEXT,
            file_path TEXT,
            external_ref TEXT,
            frozen INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS company_submission_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            material_type TEXT NOT NULL DEFAULT 'application_text',
            title TEXT NOT NULL,
            content TEXT,
            file_path TEXT,
            external_ref TEXT,
            current_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS company_submission_material_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL REFERENCES company_submission_materials(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            file_path TEXT,
            external_ref TEXT,
            change_summary TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(material_id,version)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS company_submission_usages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL REFERENCES company_submission_materials(id) ON DELETE CASCADE,
            material_version INTEGER NOT NULL,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE SET NULL,
            application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
            usage_title TEXT,
            submitted_file_path TEXT,
            submitted_ref TEXT,
            notes TEXT,
            used_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS company_application_captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE SET NULL,
            title TEXT,
            page_url TEXT NOT NULL,
            portal_host TEXT,
            structure_json TEXT NOT NULL DEFAULT '[]',
            field_count INTEGER NOT NULL DEFAULT 0,
            captured_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_application_milestones_date ON application_milestones(event_date,application_id)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS interview_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT DEFAULT 'portfolio',
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE SET NULL,
            title TEXT,
            role_type TEXT,
            round TEXT,
            source_text TEXT,
            summary TEXT,
            prediction_json TEXT,
            report_knowledge_id INTEGER,
            tags TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS interview_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER REFERENCES interview_reviews(id) ON DELETE CASCADE,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE SET NULL,
            question_order INTEGER,
            question TEXT,
            answer TEXT,
            intent TEXT,
            answer_review TEXT,
            better_answer TEXT,
            risk_level TEXT,
            ability TEXT,
            tags TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            folder TEXT DEFAULT '未分类',
            workspace_run_id INTEGER,
            workspace_task_key TEXT,
            workspace_mode TEXT DEFAULT 'general',
            track_id INTEGER,
            knowledge_item_id INTEGER,
            gap_id INTEGER,
            target_type TEXT,
            target_id INTEGER,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            is_pinned INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    try:
        c.execute("ALTER TABLE sessions ADD COLUMN folder TEXT DEFAULT '未分类'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE sessions ADD COLUMN workspace_run_id INTEGER")
    except sqlite3.OperationalError:
        pass
    for statement in (
        "ALTER TABLE sessions ADD COLUMN workspace_task_key TEXT",
        "ALTER TABLE sessions ADD COLUMN workspace_mode TEXT DEFAULT 'general'",
        "ALTER TABLE sessions ADD COLUMN track_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN knowledge_item_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN gap_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN experience_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN experience_type TEXT",
        "ALTER TABLE sessions ADD COLUMN target_type TEXT",
        "ALTER TABLE sessions ADD COLUMN target_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE",
        "ALTER TABLE sessions ADD COLUMN is_pinned INTEGER DEFAULT 0",
    ):
        try:
            c.execute(statement)
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_workspace_task_key ON sessions(workspace_task_key)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS experience_change_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            experience_id INTEGER,
            experience_type TEXT NOT NULL DEFAULT 'project',
            target_type TEXT NOT NULL DEFAULT 'project',
            target_id INTEGER NOT NULL,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            operation TEXT NOT NULL DEFAULT 'update',
            field TEXT NOT NULL,
            original_value TEXT,
            proposed_value TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            evidence_source TEXT NOT NULL DEFAULT 'wording_only',
            unverified_claims_json TEXT NOT NULL DEFAULT '[]',
            question_plan_json TEXT NOT NULL DEFAULT '[]',
            revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending_confirmation',
            applied_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    for statement in (
        "ALTER TABLE experience_change_candidates ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE",
        "ALTER TABLE experience_change_candidates ADD COLUMN target_type TEXT NOT NULL DEFAULT 'project'",
        "ALTER TABLE experience_change_candidates ADD COLUMN evidence_source TEXT NOT NULL DEFAULT 'wording_only'",
        "ALTER TABLE experience_change_candidates ADD COLUMN failure_reason TEXT",
    ):
        try: c.execute(statement)
        except sqlite3.OperationalError: pass
    legacy_experience_id = next((x for x in c.execute("PRAGMA table_info(experience_change_candidates)") if x[1] == "experience_id"), None)
    if legacy_experience_id and legacy_experience_id[3]:
        c.execute("DROP INDEX IF EXISTS idx_exp_candidates_scope")
        c.execute("ALTER TABLE experience_change_candidates RENAME TO experience_change_candidates_legacy")
        c.execute("""CREATE TABLE experience_change_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            experience_id INTEGER, experience_type TEXT NOT NULL DEFAULT 'project',
            target_type TEXT NOT NULL DEFAULT 'project', target_id INTEGER NOT NULL,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            operation TEXT NOT NULL DEFAULT 'update', field TEXT NOT NULL, original_value TEXT,
            proposed_value TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '[]',
            evidence_source TEXT NOT NULL DEFAULT 'wording_only', unverified_claims_json TEXT NOT NULL DEFAULT '[]',
            question_plan_json TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending_confirmation', failure_reason TEXT, applied_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT DEFAULT (datetime('now','localtime'))
        )""")
        c.execute("""INSERT INTO experience_change_candidates
            (id,session_id,experience_type,target_type,target_id,project_id,operation,field,original_value,
             proposed_value,evidence_json,evidence_source,unverified_claims_json,question_plan_json,revision,status,
             failure_reason,applied_at,created_at,updated_at)
            SELECT id,session_id,experience_type,'project',target_id,COALESCE(project_id,target_id),operation,field,
             original_value,proposed_value,evidence_json,evidence_source,unverified_claims_json,question_plan_json,
             revision,status,failure_reason,applied_at,created_at,updated_at FROM experience_change_candidates_legacy""")
        c.execute("DROP TABLE experience_change_candidates_legacy")
    c.execute("UPDATE sessions SET target_type='project',target_id=experience_id,project_id=experience_id WHERE workspace_mode='experience' AND project_id IS NULL AND experience_id IS NOT NULL")
    c.execute("UPDATE experience_change_candidates SET project_id=target_id WHERE project_id IS NULL")
    # experience_id is a deprecated compatibility column. New scope and audit
    # semantics are exclusively project_id + target_type + target_id.
    c.execute("DROP INDEX IF EXISTS idx_exp_candidates_scope")
    c.execute("CREATE INDEX IF NOT EXISTS idx_exp_candidates_scope ON experience_change_candidates(session_id,project_id,target_type,target_id,status)")
    c.execute("""CREATE TABLE IF NOT EXISTS experience_workspace_state (
        session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        dispatch_json TEXT NOT NULL DEFAULT '{}',
        question_plan_json TEXT NOT NULL DEFAULT '[]',
        last_expert_key TEXT,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS experience_feedback_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experience_id INTEGER NOT NULL,
            source_interview_id INTEGER,
            feedback_type TEXT NOT NULL,
            finding TEXT NOT NULL,
            affected_field TEXT,
            recommended_action TEXT,
            scope TEXT NOT NULL DEFAULT 'long_term_experience',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending_confirmation',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            confirmed_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            message_id INTEGER,
            file_name TEXT NOT NULL,
            mime_type TEXT,
            file_path TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            extracted_text TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_message_local_files (
            message_id INTEGER NOT NULL,
            candidate_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY(message_id, candidate_id)
        )
    """)

    # 在职日记：像写日记一样记录每天做了什么
    c.execute("""
        CREATE TABLE IF NOT EXISTS work_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT,
            experience_id INTEGER,
            content TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS work_log_project_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_log_id INTEGER REFERENCES work_logs(id) ON DELETE CASCADE,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            relation TEXT DEFAULT 'derived_into',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(work_log_id, project_id, relation)
        )
    """)

    # 面试制品：自我介绍版本、项目话术、知识卡等
    c.execute("""
        CREATE TABLE IF NOT EXISTS interview_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            title TEXT,
            target TEXT,
            content TEXT,
            project_id INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # 资料资产中心：原始资料先沉淀，后续再关联到经历/项目/求职线
    c.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT DEFAULT 'other',
            title TEXT,
            content TEXT,
            file_name TEXT,
            file_path TEXT,
            summary TEXT,
            analysis_json TEXT,
            status TEXT DEFAULT 'raw',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # 求职线：某个目标岗位/公司的一条独立上下文线
    c.execute("""
        CREATE TABLE IF NOT EXISTS job_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT,
            role TEXT,
            target TEXT,
            jd TEXT,
            status TEXT DEFAULT 'active',
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS source_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
            entity_type TEXT,
            entity_id INTEGER,
            relation TEXT,
            reason TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # 邮箱助理：永久记录已归档/已忽略邮件，避免跨页面或重启后重复处理。
    c.execute("""
        CREATE TABLE IF NOT EXISTS email_message_states (
            message_key TEXT PRIMARY KEY,
            account TEXT,
            status TEXT NOT NULL CHECK(status IN ('archived','ignored')),
            subject TEXT,
            sender TEXT,
            message_date TEXT,
            application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
            handled_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_states_account ON email_message_states(account,status)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS email_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_key TEXT NOT NULL UNIQUE,
            account TEXT,
            title TEXT NOT NULL,
            detail TEXT,
            status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','done')),
            created_at TEXT DEFAULT (datetime('now','localtime')),
            completed_at TEXT,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_reminders_status ON email_reminders(status,created_at)")

    # 本地资料发现：扫描结果先进候选池，用户确认后才写入 sources
    c.execute("""
        CREATE TABLE IF NOT EXISTS local_discovery_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            roots_json TEXT,
            options_json TEXT,
            status TEXT DEFAULT 'running',
            total_seen INTEGER DEFAULT 0,
            candidate_count INTEGER DEFAULT 0,
            imported_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            summary TEXT,
            started_at TEXT DEFAULT (datetime('now','localtime')),
            finished_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS local_file_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER REFERENCES local_discovery_runs(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            file_name TEXT,
            extension TEXT,
            size_bytes INTEGER DEFAULT 0,
            modified_at TEXT,
            content_hash TEXT,
            sample_text TEXT,
            source_type TEXT DEFAULT 'other',
            title TEXT,
            summary TEXT,
            confidence REAL DEFAULT 0,
            signals_json TEXT,
            analysis_json TEXT,
            duplicate_source_id INTEGER,
            matched_source_id INTEGER,
            suggested_track_json TEXT,
            status TEXT DEFAULT 'candidate',
            error TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_local_candidates_run ON local_file_candidates(run_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_local_candidates_hash ON local_file_candidates(content_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_local_candidates_path ON local_file_candidates(file_path)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS feedback_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT DEFAULT 'global',
            scope_id INTEGER,
            note_type TEXT DEFAULT 'feedback',
            content TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_type TEXT NOT NULL,
            track_id INTEGER,
            project_id INTEGER,
            title TEXT,
            body TEXT,
            provenance_json TEXT,
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'draft',
            derived_from_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS resume_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL REFERENCES job_tracks(id) ON DELETE CASCADE,
            version_name TEXT NOT NULL,
            docx_path TEXT,
            pdf_path TEXT,
            extracted_text TEXT,
            status TEXT DEFAULT 'editing',
            change_summary TEXT,
            docx_hash TEXT,
            pdf_hash TEXT,
            docx_modified_at TEXT,
            pdf_modified_at TEXT,
            submitted_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER REFERENCES knowledge_folders(id) ON DELETE CASCADE,
            scope_type TEXT DEFAULT 'global',
            domain_key TEXT,
            company TEXT,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE CASCADE,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT,
            scope_type TEXT DEFAULT 'global',
            domain_key TEXT,
            company TEXT,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE CASCADE,
            topic TEXT,
            mastery TEXT DEFAULT 'learning',
            source_type TEXT DEFAULT 'manual',
            source_ref_id INTEGER,
            folder_id INTEGER REFERENCES knowledge_folders(id) ON DELETE SET NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    try:
        c.execute("ALTER TABLE knowledge_items ADD COLUMN folder_id INTEGER REFERENCES knowledge_folders(id) ON DELETE SET NULL")
    except sqlite3.OperationalError:
        pass

    # 统一 Agent Runtime。所有专家都通过任务、运行、事件和候选修改协作；
    # 现有 knowledge_agent_* 表暂时保留为兼容视图的数据来源。
    c.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL DEFAULT 'general',
            title TEXT NOT NULL,
            instruction TEXT NOT NULL,
            object_type TEXT,
            object_id INTEGER,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE SET NULL,
            company TEXT,
            status TEXT DEFAULT 'queued',
            priority TEXT DEFAULT 'normal',
            assigned_expert TEXT,
            conversation_id TEXT,
            context_json TEXT,
            result_summary TEXT,
            created_by TEXT DEFAULT 'user',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            completed_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
            run_type TEXT DEFAULT 'primary',
            expert_key TEXT,
            model_profile TEXT,
            provider_id TEXT,
            model_name TEXT,
            status TEXT DEFAULT 'queued',
            attempt INTEGER DEFAULT 1,
            input_json TEXT,
            output_json TEXT,
            summary TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    for column in ("provider_id TEXT", "model_name TEXT"):
        try:
            c.execute(f"ALTER TABLE agent_runs ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    c.execute("""
        CREATE TABLE IF NOT EXISTS agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
            run_id INTEGER REFERENCES agent_runs(id) ON DELETE CASCADE,
            sequence INTEGER DEFAULT 0,
            event_type TEXT NOT NULL,
            label TEXT NOT NULL,
            detail TEXT,
            status TEXT DEFAULT 'done',
            payload_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS proposed_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
            run_id INTEGER REFERENCES agent_runs(id) ON DELETE CASCADE,
            action_type TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id INTEGER,
            parent_type TEXT,
            parent_id INTEGER,
            proposed_title TEXT,
            proposed_content TEXT,
            diff TEXT,
            reason TEXT,
            scope_type TEXT,
            status TEXT DEFAULT 'pending',
            metadata_json TEXT,
            applied_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # 外部 Agent 与内置 Agent 共用的“事实候选”层。候选事实不能直接覆盖
    # 经历/项目等展示对象，必须经由 proposed_changes 的确认动作进入 confirmed。
    c.execute("""
        CREATE TABLE IF NOT EXISTS career_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL,
            subject_id INTEGER,
            scope_type TEXT DEFAULT 'global',
            scope_id INTEGER,
            predicate TEXT NOT NULL,
            value_text TEXT,
            value_json TEXT,
            state TEXT DEFAULT 'confirmed',
            confidence REAL,
            evidence_json TEXT,
            provenance_json TEXT,
            supersedes_id INTEGER REFERENCES career_facts(id) ON DELETE SET NULL,
            created_by TEXT DEFAULT 'user',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            confirmed_at TEXT
        )
    """)
    # 这是 Agent 的增量同步游标，也是用户修改优先的可审计依据。
    c.execute("""
        CREATE TABLE IF NOT EXISTS workspace_change_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            target_type TEXT,
            target_id INTEGER,
            scope_type TEXT,
            scope_id INTEGER,
            actor_type TEXT DEFAULT 'user',
            actor_key TEXT,
            summary TEXT,
            payload_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    for sql in (
        "ALTER TABLE agent_tasks ADD COLUMN execution_owner TEXT DEFAULT 'internal'",
        "ALTER TABLE agent_tasks ADD COLUMN actor_type TEXT DEFAULT 'user'",
        "ALTER TABLE agent_tasks ADD COLUMN actor_key TEXT",
        "ALTER TABLE agent_runs ADD COLUMN actor_type TEXT DEFAULT 'internal_agent'",
        "ALTER TABLE agent_runs ADD COLUMN actor_key TEXT",
    ):
        try:
            c.execute(sql)
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_object ON agent_tasks(object_type,object_id,status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_track ON agent_tasks(track_id,status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id,id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id,sequence,id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_proposed_changes_task ON proposed_changes(task_id,status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_career_facts_subject ON career_facts(subject_type,subject_id,state)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_workspace_events_scope ON workspace_change_events(scope_type,scope_id,id)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS agent_experts (
            key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT,
            description TEXT,
            capabilities_json TEXT,
            tools_json TEXT,
            model_profile TEXT,
            system_prompt TEXT,
            allowed_actions_json TEXT,
            icon TEXT,
            color TEXT,
            status TEXT DEFAULT 'active',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    for expert in AGENT_EXPERT_SEEDS:
        c.execute("""INSERT OR IGNORE INTO agent_experts
            (key,name,role,description,capabilities_json,tools_json,model_profile,
             system_prompt,allowed_actions_json,icon,color,status,sort_order)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?)""",
            (expert["key"], expert["name"], expert["role"], expert["description"],
             json.dumps(expert["capabilities"], ensure_ascii=False),
             json.dumps(expert["tools"], ensure_ascii=False), expert["model_profile"],
             expert["system_prompt"],
             json.dumps(expert["allowed_actions"], ensure_ascii=False),
             expert["icon"], expert["color"], expert["sort_order"]))

    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL REFERENCES job_tracks(id) ON DELETE CASCADE,
            instruction TEXT NOT NULL,
            current_document_id INTEGER REFERENCES knowledge_items(id) ON DELETE SET NULL,
            status TEXT DEFAULT 'planning',
            summary TEXT,
            review_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_agent_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES knowledge_agent_runs(id) ON DELETE CASCADE,
            action_type TEXT NOT NULL,
            document_id INTEGER REFERENCES knowledge_items(id) ON DELETE SET NULL,
            folder_id INTEGER REFERENCES knowledge_folders(id) ON DELETE SET NULL,
            proposed_title TEXT,
            proposed_content TEXT,
            diff TEXT,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            metadata_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES knowledge_agent_runs(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            label TEXT NOT NULL,
            detail TEXT,
            status TEXT DEFAULT 'done',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    for sql in (
        "ALTER TABLE knowledge_agent_runs ADD COLUMN task_id INTEGER REFERENCES agent_tasks(id) ON DELETE SET NULL",
        "ALTER TABLE knowledge_agent_runs ADD COLUMN agent_run_id INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL",
        "ALTER TABLE knowledge_agent_changes ADD COLUMN proposed_change_id INTEGER REFERENCES proposed_changes(id) ON DELETE SET NULL",
        "ALTER TABLE knowledge_agent_events ADD COLUMN agent_event_id INTEGER REFERENCES agent_events(id) ON DELETE SET NULL",
    ):
        try:
            c.execute(sql)
        except sqlite3.OperationalError:
            pass

    # 把既有知识 Agent 历史一次性纳入统一 Runtime。保留旧记录 ID，前端可平滑过渡。
    legacy_status_to_task = {
        "planning": "active", "editing": "active", "ready": "review",
        "applied": "completed", "failed": "failed", "cancelled": "cancelled",
    }
    legacy_status_to_run = {
        "planning": "running", "editing": "running", "ready": "ready",
        "applied": "completed", "failed": "failed", "cancelled": "cancelled",
    }
    legacy_runs = c.execute("""SELECT kr.*,jt.company,jt.role
        FROM knowledge_agent_runs kr LEFT JOIN job_tracks jt ON jt.id=kr.track_id
        WHERE kr.task_id IS NULL OR kr.agent_run_id IS NULL""").fetchall()
    for legacy in legacy_runs:
        legacy = dict(legacy)
        task_status = legacy_status_to_task.get(legacy.get("status"), "active")
        cur = c.execute("""INSERT INTO agent_tasks
            (task_type,title,instruction,object_type,object_id,track_id,company,status,
             assigned_expert,context_json,result_summary,created_by,created_at,updated_at,completed_at)
            VALUES ('knowledge_edit',?,?,?,?,?,?,?,'knowledge_coach',?,?, 'user',?,?,?)""",
            (f"整理岗位知识：{legacy.get('company') or ''} · {legacy.get('role') or ''}".strip(" ·"),
             legacy.get("instruction") or "", "job_track", legacy.get("track_id"),
             legacy.get("track_id"), legacy.get("company"), task_status,
             json.dumps({"current_document_id": legacy.get("current_document_id")}, ensure_ascii=False),
             legacy.get("summary"), legacy.get("created_at"), legacy.get("updated_at"),
             legacy.get("updated_at") if task_status in {"completed", "failed", "cancelled"} else None))
        task_id = cur.lastrowid
        run_status = legacy_status_to_run.get(legacy.get("status"), "running")
        cur = c.execute("""INSERT INTO agent_runs
            (task_id,run_type,expert_key,model_profile,status,input_json,output_json,summary,
             started_at,completed_at,created_at,updated_at)
            VALUES (?,'primary','knowledge_coach','writing',?,?,?,?,?,?,?,?)""",
            (task_id, run_status,
             json.dumps({"instruction": legacy.get("instruction") or ""}, ensure_ascii=False),
             legacy.get("review_json"), legacy.get("summary"), legacy.get("created_at"),
             legacy.get("updated_at") if run_status in {"completed", "failed", "cancelled"} else None,
             legacy.get("created_at"), legacy.get("updated_at")))
        agent_run_id = cur.lastrowid
        c.execute("UPDATE knowledge_agent_runs SET task_id=?,agent_run_id=? WHERE id=?",
                  (task_id, agent_run_id, legacy["id"]))
        old_events = c.execute("SELECT * FROM knowledge_agent_events WHERE run_id=? ORDER BY id",
                               (legacy["id"],)).fetchall()
        for sequence, old_event in enumerate(old_events, 1):
            old_event = dict(old_event)
            cur = c.execute("""INSERT INTO agent_events
                (task_id,run_id,sequence,event_type,label,detail,status,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (task_id, agent_run_id, sequence, old_event.get("event_type"),
                 old_event.get("label"), old_event.get("detail"), old_event.get("status"),
                 old_event.get("created_at")))
            c.execute("UPDATE knowledge_agent_events SET agent_event_id=? WHERE id=?",
                      (cur.lastrowid, old_event["id"]))
        old_changes = c.execute("SELECT * FROM knowledge_agent_changes WHERE run_id=? ORDER BY id",
                                (legacy["id"],)).fetchall()
        for old_change in old_changes:
            old_change = dict(old_change)
            try:
                change_meta = json.loads(old_change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                change_meta = {}
            cur = c.execute("""INSERT INTO proposed_changes
                (task_id,run_id,action_type,target_type,target_id,parent_type,parent_id,
                 proposed_title,proposed_content,diff,reason,scope_type,status,metadata_json,
                 applied_at,created_at,updated_at)
                VALUES (?,?,?,'knowledge_item',?,'job_track',?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, agent_run_id, old_change.get("action_type"), old_change.get("document_id"),
                 legacy.get("track_id"), old_change.get("proposed_title"),
                 old_change.get("proposed_content"), old_change.get("diff"), old_change.get("reason"),
                 change_meta.get("scope_type"), old_change.get("status"), old_change.get("metadata_json"),
                 old_change.get("updated_at") if old_change.get("status") == "applied" else None,
                 old_change.get("created_at"), old_change.get("updated_at")))
            c.execute("UPDATE knowledge_agent_changes SET proposed_change_id=? WHERE id=?",
                      (cur.lastrowid, old_change["id"]))
    c.execute("""INSERT INTO knowledge_items
        (title,content,scope_type,topic,mastery,source_type,source_ref_id)
        SELECT COALESCE(NULLIF(title,''),'旧知识卡'),content,'global',target,'learning','legacy',id
          FROM interview_items old
         WHERE kind='knowledge'
           AND NOT EXISTS (SELECT 1 FROM knowledge_items k
                            WHERE k.source_type='legacy' AND k.source_ref_id=old.id)""")

    c.execute("""
        CREATE TABLE IF NOT EXISTS followups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            answer TEXT,
            status TEXT DEFAULT 'todo',
            category TEXT,
            asked_count INTEGER DEFAULT 0,
            origin TEXT,
            source_track_id INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # 求职目标工作台：每个目标的差距清单（差距驱动的准备闭环核心）
    c.execute("""
        CREATE TABLE IF NOT EXISTS track_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE CASCADE,
            dimension TEXT,
            requirement TEXT,
            my_status TEXT DEFAULT 'missing',
            severity TEXT DEFAULT 'fixable',
            plan_type TEXT DEFAULT 'none',
            plan_ref_type TEXT,
            plan_ref_id INTEGER,
            status TEXT DEFAULT 'todo',
            note TEXT,
            excluded_from_score INTEGER DEFAULT 0,
            exclusion_reason TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # 秋招机会库：正式投递前的岗位情报池。机会先被核验、排期，再选择是否转入求职线。
    c.execute("""
        CREATE TABLE IF NOT EXISTS job_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            role TEXT NOT NULL,
            direction TEXT,
            company_industry TEXT,
            company_type TEXT,
            batch_type TEXT DEFAULT 'autumn',
            status TEXT DEFAULT 'new',
            priority TEXT DEFAULT 'normal',
            fit_score INTEGER DEFAULT 0,
            source_type TEXT,
            source_title TEXT,
            source_url TEXT,
            apply_url TEXT,
            location TEXT,
            deadline_date TEXT,
            deadline_type TEXT DEFAULT 'hard',
            flow_days INTEGER DEFAULT 4,
            buffer_days INTEGER DEFAULT 1,
            jd TEXT,
            notes TEXT,
            evidence TEXT,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE SET NULL,
            application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS opportunity_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL UNIQUE REFERENCES job_opportunities(id) ON DELETE CASCADE,
            rule_status TEXT DEFAULT 'unverified',
            early_batch_impact TEXT DEFAULT 'unknown',
            locks_choice TEXT DEFAULT 'unknown',
            multi_apply_allowed TEXT DEFAULT 'unknown',
            cooldown_days INTEGER,
            rolling_review TEXT DEFAULT 'unknown',
            referral_required TEXT DEFAULT 'unknown',
            resume_editable TEXT DEFAULT 'unknown',
            assessment_trigger TEXT,
            evidence TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS opportunity_plan_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL REFERENCES job_opportunities(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            action_type TEXT DEFAULT 'prepare',
            due_date TEXT,
            status TEXT DEFAULT 'todo',
            notes TEXT,
            calendar_scope TEXT DEFAULT 'opportunity',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS career_calendar_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER REFERENCES job_tracks(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            item_type TEXT DEFAULT 'task',
            starts_at TEXT NOT NULL,
            duration_minutes INTEGER DEFAULT 30,
            color TEXT DEFAULT 'green',
            target_count INTEGER,
            status TEXT DEFAULT 'todo',
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    try:
        c.execute("ALTER TABLE career_calendar_items ADD COLUMN color TEXT DEFAULT 'green'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE career_calendar_items ADD COLUMN target_count INTEGER")
    except sqlite3.OperationalError:
        pass
    # Early alpha builds required every calendar item to point to one job track.
    # Rebuild once so general plans such as “submit three applications” can live
    # in the same calendar without inventing a fake job.
    track_column = next((x for x in c.execute("PRAGMA table_info(career_calendar_items)").fetchall()
                         if x[1] == "track_id"), None)
    if track_column and track_column[3]:
        c.execute("ALTER TABLE career_calendar_items RENAME TO career_calendar_items_legacy")
        c.execute("""
            CREATE TABLE career_calendar_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER REFERENCES job_tracks(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                item_type TEXT DEFAULT 'task',
                starts_at TEXT NOT NULL,
                duration_minutes INTEGER DEFAULT 30,
                color TEXT DEFAULT 'green',
                target_count INTEGER,
                status TEXT DEFAULT 'todo',
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        c.execute("""INSERT INTO career_calendar_items
            (id,track_id,title,item_type,starts_at,duration_minutes,color,target_count,status,notes,created_at,updated_at)
            SELECT id,track_id,title,item_type,starts_at,duration_minutes,
                   COALESCE(color,'green'),target_count,status,notes,created_at,updated_at
            FROM career_calendar_items_legacy""")
        c.execute("DROP TABLE career_calendar_items_legacy")
    c.execute("CREATE INDEX IF NOT EXISTS idx_opportunity_status ON job_opportunities(status, deadline_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_opportunity_plan_due ON opportunity_plan_items(due_date, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_career_calendar_start ON career_calendar_items(starts_at, status)")

    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            entity_type, entity_id UNINDEXED, title, content,
            tokenize='unicode61'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS semantic_chunks (
            chunk_key TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            title TEXT,
            content TEXT,
            content_hash TEXT,
            vector_json TEXT,
            metadata_json TEXT,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_semantic_entity ON semantic_chunks(entity_type, entity_id)")

    # 迁移：给老库补字段（已存在则忽略）
    for col in (
        "job_description TEXT",
        "gap_analysis TEXT",
        "company_type TEXT",
        "company_industry TEXT",
        "job_type TEXT",
        "apply_url TEXT",
        "remark_tag TEXT",
        "evaluation TEXT",
        "resume_version_id INTEGER",
    ):
        try:
            c.execute(f"ALTER TABLE applications ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_submission_material_track ON submission_materials(track_id, application_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_company_submission_material_company ON company_submission_materials(company,status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_company_submission_usage_material ON company_submission_usages(material_id,used_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_company_application_capture_company ON company_application_captures(company,captured_at)")

    for col in (
        "track_id INTEGER",
        "origin TEXT",
        "content_hash TEXT",
        "content_date TEXT",
        "tags TEXT",
        "confidence REAL",
        "pinned INTEGER DEFAULT 0",
        "superseded_by INTEGER",
        "lang TEXT",
        "ingested_at TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE sources ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    for col in (
        "category TEXT",
        "polarity TEXT",
        "strength TEXT",
        "directive TEXT",
        "original_text TEXT",
        "source_id INTEGER",
        "status TEXT DEFAULT 'active'",
        "impact_json TEXT",
        "normalized_at TEXT",
        "supersedes_id INTEGER",
    ):
        try:
            c.execute(f"ALTER TABLE feedback_notes ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # 追问类型：factual=深挖做过的事 / reflective=反思延伸
    for col in (
        "kind TEXT",
        "category_source TEXT",
        "previous_category TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE followups ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # 作品集（个人项目）：复用 projects 表，kind='personal' 且 experience_id 为空
    for col in (
        "kind TEXT DEFAULT 'work'",
        "repo_url TEXT",
        "demo_url TEXT",
        "cover_source_id INTEGER",
        "build_status TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE projects ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # 求职工作台：上层板块（如 AI 相关）包含多条具体公司 + 岗位求职线
    for col in (
        "persona TEXT",
        "readiness INTEGER DEFAULT 0",
        "priority TEXT DEFAULT 'normal'",
        "track_group TEXT",
        "apply_url TEXT",
        "company_type TEXT",
        "company_industry TEXT",
        "job_type TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE job_tracks ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in (
        "excluded_from_score INTEGER DEFAULT 0",
        "exclusion_reason TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE track_gaps ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in (
        "duration_minutes INTEGER DEFAULT 60",
        "location TEXT",
        "meeting_link TEXT",
        "notes TEXT",
        "updated_at TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE interviews ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    try:
        c.execute("ALTER TABLE applications ADD COLUMN track_id INTEGER")
    except sqlite3.OperationalError:
        pass

    for col in (
        "flow_days INTEGER DEFAULT 4",
        "buffer_days INTEGER DEFAULT 1",
        "track_id INTEGER",
        "application_id INTEGER",
        "company_industry TEXT",
        "company_type TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE job_opportunities ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # Alpha 发布基础设施：产品设置、匿名本地遥测与用户反馈。
    # 遥测与 Agent 审计日志分开，且默认关闭；职业正文不进入这些表。
    c.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0,
            consent_version TEXT,
            consented_at TEXT,
            installation_id TEXT NOT NULL,
            last_upload_at TEXT,
            last_upload_status TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_name TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            installation_id TEXT NOT NULL,
            session_id TEXT,
            entity_type TEXT,
            entity_id_hash TEXT,
            properties_json TEXT NOT NULL DEFAULT '{}',
            client_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'local',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS product_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'other',
            rating INTEGER,
            message TEXT NOT NULL,
            contact TEXT,
            app_version TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_queue_event_time ON telemetry_queue(event_name,client_time)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_product_feedback_created ON product_feedback(created_at,id)")

    # Repair gaps left by early builds where deleting round 1 could leave the
    # next surviving interview labelled round 2.  Keep the persisted canonical
    # sequence aligned for calendar, workspace and exports.
    interview_apps = c.execute(
        "SELECT DISTINCT application_id FROM interviews WHERE application_id IS NOT NULL"
    ).fetchall()
    for app_row in interview_apps:
        interview_rows = c.execute(
            """SELECT id FROM interviews WHERE application_id=?
               ORDER BY COALESCE(round_number,999999),
                        COALESCE(interview_date,'9999-12-31 23:59'),id""",
            (app_row["application_id"],),
        ).fetchall()
        for round_number, interview_row in enumerate(interview_rows, 1):
            c.execute(
                "UPDATE interviews SET round_number=? WHERE id=? AND COALESCE(round_number,0)<>?",
                (round_number, interview_row["id"], round_number),
            )

    conn.commit()
    conn.close()
    if os.environ.get("CADDIE_SEED_DEMO") == "1":
        _seed_if_empty()


def set_application_jd(aid, jd, analysis=None):
    conn = get_db()
    if analysis is None:
        conn.execute("UPDATE applications SET job_description=? WHERE id=?", (jd, aid))
    else:
        conn.execute("UPDATE applications SET job_description=?, gap_analysis=? WHERE id=?", (jd, analysis, aid))
    app = one(conn.execute("SELECT track_id FROM applications WHERE id=?", (aid,)).fetchone()) or {}
    if app.get("track_id") and jd:
        conn.execute("""UPDATE job_tracks SET jd=?,
            updated_at=datetime('now','localtime') WHERE id=?""",
            (jd, app["track_id"]))
    conn.commit(); conn.close()


def rows(r):
    return [dict(x) for x in r]


def one(r):
    return dict(r) if r else None


# ─── 经历 ─────────────────────────────────────────────────────────────────────

def get_experiences():
    conn = get_db()
    exps = rows(conn.execute("SELECT * FROM experiences ORDER BY start_date DESC").fetchall())
    for e in exps:
        e["projects"] = rows(conn.execute(
            "SELECT id,name,one_liner,technologies,updated_at FROM projects WHERE experience_id=? ORDER BY id",
            (e["id"],)).fetchall())
    conn.close()
    return exps


def get_experience(eid):
    conn = get_db()
    item = one(conn.execute("SELECT * FROM experiences WHERE id=?", (eid,)).fetchone())
    conn.close()
    return item


def list_experience_projects(eid):
    conn = get_db()
    result = rows(conn.execute(
        "SELECT * FROM projects WHERE experience_id=? ORDER BY id",
        (eid,),
    ).fetchall())
    conn.close()
    return result


def create_experience(d):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO experiences (company,role,start_date,end_date,location) VALUES (?,?,?,?,?)",
        (d.get("company"), d.get("role"), d.get("start_date"), d.get("end_date"), d.get("location")))
    eid = cur.lastrowid
    conn.commit(); conn.close()
    return eid


def update_experience(eid, d):
    conn = get_db()
    cur = conn.execute(
        """UPDATE experiences
           SET company=?,role=?,start_date=?,end_date=?,location=?,
               updated_at=datetime('now','localtime')
           WHERE id=?""",
        (d.get("company"), d.get("role"), d.get("start_date"),
         d.get("end_date"), d.get("location"), eid),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def _delete_search_entities(conn, entity_type, entity_ids):
    ids = [int(value) for value in entity_ids if value is not None]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM search_index WHERE entity_type=? AND entity_id IN ({marks})",
        (entity_type, *ids),
    )
    conn.execute(
        f"DELETE FROM semantic_chunks WHERE entity_type=? AND entity_id IN ({marks})",
        (entity_type, *ids),
    )


def delete_experience(eid):
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM experiences WHERE id=?", (eid,)).fetchone()
    if not exists:
        conn.close()
        return False
    pids = [r["id"] for r in conn.execute("SELECT id FROM projects WHERE experience_id=?", (eid,)).fetchall()]
    _delete_search_entities(conn, "project", pids)
    conn.execute("DELETE FROM projects WHERE experience_id=?", (eid,))
    conn.execute("DELETE FROM experiences WHERE id=?", (eid,))
    conn.commit(); conn.close()
    return True


# ─── 项目（文档） ─────────────────────────────────────────────────────────────

def _project_text(value, *, multiline=False):
    """Normalize model-produced structured values before binding to SQLite TEXT."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        separator = "\n\n" if multiline else ", "
        parts = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                text = item.strip()
            else:
                text = json.dumps(item, ensure_ascii=False)
            if text:
                parts.append(text)
        return separator.join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _normalize_project_fields(d):
    normalized = dict(d or {})
    for field in ("name", "one_liner", "technologies", "keywords"):
        normalized[field] = _project_text(normalized.get(field))
    normalized["document"] = _project_text(normalized.get("document"), multiline=True)
    return normalized


def get_project(pid):
    conn = get_db()
    p = one(conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())
    if p:
        exp = one(conn.execute("SELECT company,role FROM experiences WHERE id=?", (p["experience_id"],)).fetchone())
        p["experience"] = exp
    conn.close()
    return p


def list_projects(kind=None, experience_id=None):
    conn = get_db()
    q = """SELECT p.*,e.company AS experience_company,e.role AS experience_role
           FROM projects p LEFT JOIN experiences e ON e.id=p.experience_id
           WHERE 1=1"""
    args = []
    if kind is not None:
        q += " AND p.kind=?"
        args.append(kind)
    if experience_id is not None:
        q += " AND p.experience_id=?"
        args.append(experience_id)
    q += " ORDER BY p.updated_at DESC,p.id DESC"
    result = rows(conn.execute(q, args).fetchall())
    conn.close()
    return result


def create_project(exp_id, d):
    d = _normalize_project_fields(d)
    conn = get_db()
    if not conn.execute("SELECT 1 FROM experiences WHERE id=?", (exp_id,)).fetchone():
        conn.close()
        return None
    cur = conn.execute(
        "INSERT INTO projects (experience_id,name,one_liner,document,technologies,keywords) VALUES (?,?,?,?,?,?)",
        (exp_id, d.get("name"), d.get("one_liner"), d.get("document"),
         d.get("technologies"), d.get("keywords")))
    pid = cur.lastrowid
    _reindex_project(conn, pid, d)
    conn.commit(); conn.close()
    return pid


def update_project(pid, d):
    d = _normalize_project_fields(d)
    conn = get_db()
    cur = conn.execute("""
        UPDATE projects SET name=?, one_liner=?, document=?, technologies=?, keywords=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("name"), d.get("one_liner"), d.get("document"),
         d.get("technologies"), d.get("keywords"), pid))
    conn.execute("DELETE FROM search_index WHERE entity_type='project' AND entity_id=?", (pid,))
    _reindex_project(conn, pid, d)
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


EXPERIENCE_PROJECT_FIELDS = {"name", "one_liner", "document", "technologies", "keywords"}


def create_experience_candidate(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO experience_change_candidates
        (session_id,target_type,target_id,project_id,operation,field,original_value,
         proposed_value,evidence_json,evidence_source,unverified_claims_json,question_plan_json,status,failure_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        d["session_id"], "project", d["project_id"], d["project_id"], "update", d["field"], d.get("original_value"), d["proposed_value"],
        json.dumps(d.get("evidence") or [], ensure_ascii=False),
        d.get("evidence_source") or "wording_only",
        json.dumps(d.get("unverified_claims") or [], ensure_ascii=False),
        json.dumps(d.get("question_plan") or [], ensure_ascii=False),
        d.get("status") or "pending_confirmation", d.get("failure_reason")))
    cid = cur.lastrowid; conn.commit(); conn.close(); return cid


def list_experience_candidates(session_id):
    conn = get_db(); items = rows(conn.execute(
        "SELECT * FROM experience_change_candidates WHERE session_id=? ORDER BY id", (session_id,)).fetchall()); conn.close()
    for item in items:
        for key in ("evidence_json", "unverified_claims_json", "question_plan_json"):
            item[key[:-5]] = json.loads(item.get(key) or "[]")
    return items


def apply_experience_candidates(session_id, candidate_ids):
    """Optimistic, field-whitelisted write; all selected changes commit atomically."""
    conn = get_db(); results = []
    try:
        marks = ",".join("?" for _ in candidate_ids) or "NULL"
        items = rows(conn.execute(f"SELECT * FROM experience_change_candidates WHERE session_id=? AND id IN ({marks})", [session_id, *candidate_ids]).fetchall())
        if len(items) != len(set(candidate_ids)): raise ValueError("候选不存在或不属于当前任务")
        for item in items:
            if item["status"] != "pending_confirmation": raise ValueError("候选已处理")
            if item["field"] not in EXPERIENCE_PROJECT_FIELDS: raise ValueError("目标字段不在白名单")
            session = conn.execute("SELECT project_id,target_type,target_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session["target_type"] != "project": raise ValueError("任务未绑定项目")
            project_id = item.get("project_id") or item["target_id"]
            project = one(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
            if int(project_id) != int(session["project_id"] or session["target_id"]): project = None
            if not project: raise ValueError("目标经历不匹配")
            if item.get("evidence_source") not in {"material", "user_statement", "wording_only"}: raise ValueError("候选依据类型无效")
            evidence = json.loads(item.get("evidence_json") or "[]")
            if item.get("evidence_source") != "wording_only" and not evidence: raise ValueError("改变事实的候选缺少依据")
            if json.loads(item.get("unverified_claims_json") or "[]"): raise ValueError("候选仍包含未验证事实")
            if (project.get(item["field"]) or "") != (item.get("original_value") or ""): raise RuntimeError(f"原值已变化:{item['id']}")
            conn.execute(f"UPDATE projects SET {item['field']}=?,updated_at=datetime('now','localtime') WHERE id=?", (item["proposed_value"], project_id))
            conn.execute("UPDATE experience_change_candidates SET status='applied',applied_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?", (item["id"],))
            conn.execute("INSERT INTO workspace_change_events (event_type,target_type,target_id,scope_type,scope_id,actor_type,actor_key,summary,payload_json) VALUES ('experience_candidate_applied','project',?,'project',?,'user','experience_agent',?,?)", (project_id, project_id, f"更新 {item['field']}", json.dumps({"candidate_id":item["id"],"before":item["original_value"],"after":item["proposed_value"]},ensure_ascii=False)))
            results.append({"candidate_id":item["id"],"project_id":project_id,"field":item["field"],"before":item["original_value"],"after":item["proposed_value"],"applied_at":datetime.now().isoformat(timespec="seconds"),"path":f"我的经历 / 项目经历 / {project['name']} / {item['field']}"})
        conn.commit(); return results
    except Exception:
        conn.rollback(); raise
    finally: conn.close()


def save_experience_workspace_state(session_id, project_id, dispatch, question_plan, expert_key):
    conn = get_db(); conn.execute("""INSERT INTO experience_workspace_state
        (session_id,project_id,dispatch_json,question_plan_json,last_expert_key)
        VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET project_id=excluded.project_id,
        dispatch_json=excluded.dispatch_json,question_plan_json=excluded.question_plan_json,
        last_expert_key=excluded.last_expert_key,updated_at=datetime('now','localtime')""",
        (session_id,project_id,json.dumps(dispatch or {},ensure_ascii=False),json.dumps(question_plan or [],ensure_ascii=False),expert_key)); conn.commit(); conn.close()


def get_experience_workspace_state(session_id):
    conn=get_db(); item=one(conn.execute("SELECT * FROM experience_workspace_state WHERE session_id=?",(session_id,)).fetchone()); conn.close()
    if item:
        item["dispatch"]=json.loads(item.get("dispatch_json") or "{}"); item["question_plan"]=json.loads(item.get("question_plan_json") or "[]")
    return item


def set_experience_candidate_status(session_id, candidate_id, status, failure_reason=None):
    if status not in {"pending_confirmation","rejected","failed"}: return False
    conn=get_db(); cur=conn.execute("UPDATE experience_change_candidates SET status=?,failure_reason=?,updated_at=datetime('now','localtime') WHERE id=? AND session_id=? AND status IN ('pending_confirmation','failed')",(status,failure_reason,candidate_id,session_id)); conn.commit(); changed=cur.rowcount>0; conn.close(); return changed


def revise_experience_candidate(session_id, candidate_id, proposed_value, evidence, evidence_source, unverified_claims):
    conn=get_db(); item=one(conn.execute("SELECT * FROM experience_change_candidates WHERE id=? AND session_id=?",(candidate_id,session_id)).fetchone())
    if not item or item.get("status") not in {"pending_confirmation","failed"}: conn.close(); return False
    conn.execute("""UPDATE experience_change_candidates SET proposed_value=?,evidence_json=?,evidence_source=?,
        unverified_claims_json=?,revision=revision+1,status='pending_confirmation',failure_reason=NULL,
        updated_at=datetime('now','localtime') WHERE id=?""",(proposed_value,json.dumps(evidence or [],ensure_ascii=False),evidence_source,json.dumps(unverified_claims or [],ensure_ascii=False),candidate_id)); conn.commit(); conn.close(); return True


def fail_experience_candidate(session_id, candidate_id, proposed_value, evidence, evidence_source, unverified_claims, failure_reason):
    conn=get_db(); cur=conn.execute("""UPDATE experience_change_candidates SET proposed_value=?,evidence_json=?,evidence_source=?,
        unverified_claims_json=?,revision=revision+1,status='failed',failure_reason=?,
        updated_at=datetime('now','localtime') WHERE id=? AND session_id=? AND status='failed'""",
        (proposed_value,json.dumps(evidence or [],ensure_ascii=False),evidence_source,json.dumps(unverified_claims or [],ensure_ascii=False),failure_reason,candidate_id,session_id))
    conn.commit(); changed=cur.rowcount>0; conn.close(); return changed


def delete_project(pid):
    conn = get_db()
    cur = conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    _delete_search_entities(conn, "project", [pid])
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ─── 作品集（个人项目，kind='personal'，无公司经历） ─────────────────────────

def list_portfolio():
    conn = get_db()
    r = rows(conn.execute(
        "SELECT id,name,one_liner,technologies,repo_url,demo_url,cover_source_id,"
        "build_status,updated_at FROM projects WHERE kind='personal' "
        "ORDER BY updated_at DESC, id DESC").fetchall())
    conn.close()
    return r


def create_portfolio_project(d):
    d = _normalize_project_fields(d)
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO projects (experience_id,kind,name,one_liner,document,technologies,"
        "keywords,repo_url,demo_url,cover_source_id,build_status) "
        "VALUES (NULL,'personal',?,?,?,?,?,?,?,?,?)",
        (d.get("name"), d.get("one_liner"), d.get("document"), d.get("technologies"),
         d.get("keywords"), d.get("repo_url"), d.get("demo_url"),
         d.get("cover_source_id"), d.get("build_status") or "building"))
    pid = cur.lastrowid
    _reindex_project(conn, pid, d)
    conn.commit(); conn.close()
    return pid


def update_portfolio_meta(pid, d):
    """只更新作品集专属字段，不碰文档正文（文档走通用 update_project）。"""
    conn = get_db()
    conn.execute(
        "UPDATE projects SET repo_url=?, demo_url=?, cover_source_id=?, build_status=?, "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (d.get("repo_url"), d.get("demo_url"), d.get("cover_source_id"),
         d.get("build_status"), pid))
    conn.commit(); conn.close()


def _reindex_project(conn, pid, d):
    owner = one(conn.execute(
        """SELECT e.company,e.role
           FROM projects p LEFT JOIN experiences e ON e.id=p.experience_id
           WHERE p.id=?""",
        (pid,),
    ).fetchone()) or {}
    content = " ".join(filter(None, [
        owner.get("company", ""), owner.get("role", ""),
        d.get("one_liner", ""), d.get("document", ""),
        d.get("technologies", ""), d.get("keywords", "")]))
    conn.execute(
        "INSERT INTO search_index (entity_type,entity_id,title,content) VALUES (?,?,?,?)",
        ("project", pid, d.get("name", ""), content))


# ─── 投递 ─────────────────────────────────────────────────────────────────────

STATUS_FLOW = ["active", "applied", "screening", "written", "interview", "offer", "rejected"]
STATUS_LABEL = {
    "active": "计划中", "applied": "已投递", "screening": "筛选中", "written": "笔试",
    "interview": "面试中", "offer": "Offer", "rejected": "未通过",
}
STATUS_TRANSITIONS = {
    "active": {"applied", "rejected"},
    "applied": {"active", "screening", "rejected"},
    "screening": {"active", "applied", "written", "interview", "rejected"},
    "written": {"active", "screening", "interview", "rejected"},
    "interview": {"active", "written", "offer", "rejected"},
    "offer": {"active", "interview"},
    "rejected": {"active", "applied"},
}

# 作品集状态
PORTFOLIO_STATUS = {"building": "在做", "live": "已上线", "paused": "搁置"}


def _validated_application_data(d, *, partial=False):
    data = dict(d or {})
    for key in ("company", "role"):
        if partial and key not in data:
            continue
        value = str(data.get(key) or "").strip()
        if not value:
            raise ValueError(f"{key} 不能为空")
        if len(value) > 200:
            raise ValueError(f"{key} 不能超过 200 个字符")
        data[key] = value
    status = data.get("status")
    if status is None and not partial:
        status = "applied"
    if status is not None and status not in STATUS_FLOW:
        raise ValueError("投递状态不正确")
    if status is not None:
        data["status"] = status
    applied_date = data.get("applied_date")
    if applied_date in (None, "") and not partial:
        data["applied_date"] = datetime.now().strftime("%Y-%m-%d")
    elif applied_date not in (None, ""):
        try:
            data["applied_date"] = datetime.strptime(str(applied_date), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("投递日期必须是 YYYY-MM-DD") from exc
    for key, maximum in (
        ("industry", 200), ("source", 200), ("company_type", 100),
        ("company_industry", 200), ("job_type", 100), ("remark_tag", 100),
        ("apply_url", 4000), ("notes", 20_000), ("evaluation", 20_000),
        ("job_description", 100_000),
    ):
        if data.get(key) is not None and len(str(data[key])) > maximum:
            raise ValueError(f"{key} 不能超过 {maximum} 个字符")
    return data


def get_applications():
    ensure_application_tracks()
    conn = get_db()
    apps = rows(conn.execute("SELECT * FROM applications ORDER BY applied_date DESC").fetchall())
    for a in apps:
        a["interviews"] = rows(conn.execute(
            "SELECT * FROM interviews WHERE application_id=? ORDER BY round_number", (a["id"],)).fetchall())
    conn.close()
    return apps


def get_application(aid):
    conn = get_db()
    a = one(conn.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone())
    conn.close()
    return a


def create_application(d):
    d = _validated_application_data(d)
    conn = get_db()
    track_id = d.get("track_id")
    if track_id and not conn.execute("SELECT 1 FROM job_tracks WHERE id=?", (track_id,)).fetchone():
        conn.close()
        raise ValueError("关联的求职线不存在")
    if not track_id:
        existing = conn.execute(
            """SELECT id FROM job_tracks
            WHERE trim(COALESCE(company,''))=trim(COALESCE(?, ''))
              AND trim(COALESCE(role,''))=trim(COALESCE(?, ''))
            ORDER BY id LIMIT 1""", (d.get("company"), d.get("role"))).fetchone()
        track_id = existing["id"] if existing else None
    if not track_id:
        group = _infer_track_group(d)
        cls = classify_company(d.get("company"), d.get("role"))
        job_type = d.get("job_type") or infer_job_type(d.get("role"), d.get("notes"))
        tcur = conn.execute(
            """INSERT INTO job_tracks
            (company,role,target,jd,status,notes,track_group,priority,apply_url,company_type,company_industry,job_type)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d.get("company"), d.get("role"), d.get("industry"),
             d.get("job_description"), "active", "由投递自动建立",
             group, "normal", d.get("apply_url"), cls["company_type"], cls["company_industry"], job_type))
        track_id = tcur.lastrowid
    cls = classify_company(d.get("company"), d.get("role"))
    cur = conn.execute(
        """INSERT INTO applications
        (company,role,industry,applied_date,status,source,notes,track_id,company_type,company_industry,job_type,apply_url,remark_tag,evaluation)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("company"), d.get("role"), d.get("industry"),
         d.get("applied_date"), d.get("status"), d.get("source"), d.get("notes"), track_id,
         d.get("company_type") or cls["company_type"],
         d.get("company_industry") or d.get("industry") or cls["company_industry"],
         d.get("job_type") or infer_job_type(d.get("role"), d.get("notes")),
         d.get("apply_url"), d.get("remark_tag"), d.get("evaluation")))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    return aid


def ensure_track_application(track_id, overrides=None):
    """Return the track's latest application, creating one on first workflow edit."""
    overrides = overrides or {}
    conn = get_db()
    existing = one(conn.execute(
        "SELECT * FROM applications WHERE track_id=? ORDER BY id DESC LIMIT 1",
        (track_id,)).fetchone())
    track = one(conn.execute("SELECT * FROM job_tracks WHERE id=?", (track_id,)).fetchone())
    conn.close()
    if existing:
        return existing, False
    if not track:
        return None, False
    status = overrides.get("status") or track.get("status") or "applied"
    if status not in STATUS_FLOW:
        status = "applied"
    aid = create_application({
        "track_id": track_id,
        "company": track.get("company") or "",
        "role": track.get("role") or "",
        "industry": track.get("target") or track.get("company_industry") or "",
        "applied_date": overrides.get("applied_date") or datetime.now().strftime("%Y-%m-%d"),
        "status": status,
        "source": overrides.get("source") or "求职工作台",
        "notes": track.get("notes"),
        "company_type": track.get("company_type"),
        "company_industry": track.get("company_industry"),
        "job_type": track.get("job_type"),
        "apply_url": track.get("apply_url"),
        "remark_tag": overrides.get("remark_tag"),
        "evaluation": overrides.get("evaluation"),
    })
    return get_application(aid), True


def update_application(aid, d, *, allow_status_correction=False):
    d = _validated_application_data(d)
    conn = get_db()
    current = one(conn.execute(
        "SELECT * FROM applications WHERE id=?", (aid,)).fetchone()) or {}
    if current and d.get("status") != current.get("status") and not allow_status_correction:
        allowed = STATUS_TRANSITIONS.get(current.get("status"), set())
        if d.get("status") not in allowed:
            conn.close()
            raise ValueError(
                f"不允许从 {current.get('status')} 直接流转到 {d.get('status')}"
            )
    cls = classify_company(d.get("company"), d.get("role"))
    notes = d.get("notes") if d.get("notes") is not None else current.get("notes")
    remark_tag = d.get("remark_tag") if d.get("remark_tag") is not None else current.get("remark_tag")
    evaluation = d.get("evaluation") if d.get("evaluation") is not None else current.get("evaluation")
    cur = conn.execute("""UPDATE applications SET company=?,role=?,industry=?,applied_date=?,
        status=?,source=?,notes=?,company_type=?,company_industry=?,job_type=?,apply_url=?,remark_tag=?,evaluation=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("company"), d.get("role"), d.get("industry"), d.get("applied_date"),
         d.get("status"), d.get("source"), notes,
         d.get("company_type") or cls["company_type"],
         d.get("company_industry") or d.get("industry") or cls["company_industry"],
         d.get("job_type") or infer_job_type(d.get("role"), d.get("notes")),
         d.get("apply_url"), remark_tag, evaluation, aid))
    if current.get("track_id"):
        conn.execute(
            """UPDATE job_tracks SET company=?,role=?,target=?,
            track_group=COALESCE(track_group,?),
            jd=COALESCE(NULLIF(?,''),jd),
            company_type=COALESCE(NULLIF(?,''),company_type),
            company_industry=COALESCE(NULLIF(?,''),company_industry),
            job_type=COALESCE(NULLIF(?,''),job_type),
            apply_url=COALESCE(NULLIF(?,''),apply_url),
            updated_at=datetime('now','localtime')
            WHERE id=?""",
            (d.get("company"), d.get("role"), d.get("industry"),
             _infer_track_group(d), current.get("job_description") or "",
             d.get("company_type") or cls["company_type"],
             d.get("company_industry") or d.get("industry") or cls["company_industry"],
             d.get("job_type") or infer_job_type(d.get("role"), d.get("notes")),
             d.get("apply_url") or "",
             current["track_id"]))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def merge_applications(source_application_id, target_application_id, updates=None):
    """Merge a duplicate application into the canonical application."""
    source_application_id = int(source_application_id)
    target_application_id = int(target_application_id)
    if source_application_id == target_application_id:
        raise ValueError("不能把投递记录合并到自身")

    conn = get_db()
    source = one(conn.execute(
        "SELECT * FROM applications WHERE id=?", (source_application_id,)
    ).fetchone())
    target = one(conn.execute(
        "SELECT * FROM applications WHERE id=?", (target_application_id,)
    ).fetchone())
    if not source:
        conn.close()
        raise ValueError(f"待合并投递 #{source_application_id} 不存在")
    if not target:
        conn.close()
        raise ValueError(f"目标投递 #{target_application_id} 不存在")

    updates = dict(updates or {})
    merged = dict(target)
    fill_fields = (
        "company", "role", "industry", "applied_date", "status", "source",
        "notes", "company_type", "company_industry", "job_type", "apply_url",
        "remark_tag", "evaluation", "job_description", "gap_analysis",
    )
    for key in fill_fields:
        if not merged.get(key) and source.get(key):
            merged[key] = source[key]
    for key, value in updates.items():
        if key in fill_fields and value is not None:
            merged[key] = value

    source_notes = (source.get("notes") or "").strip()
    target_notes = (target.get("notes") or "").strip()
    update_notes = (updates.get("notes") or "").strip()
    notes = []
    for value in (target_notes, source_notes, update_notes):
        if value and value not in notes:
            notes.append(value)
    merged["notes"] = "\n".join(notes)

    # A corrected active state must not keep a terminal failure badge.
    terminal_remarks = {"resume_rejected", "first", "second", "final", "rejected"}
    if merged.get("status") != "rejected" and merged.get("remark_tag") in terminal_remarks:
        merged["remark_tag"] = updates.get("remark_tag") or "follow"

    cls = classify_company(merged.get("company"), merged.get("role"))
    conn.execute(
        """UPDATE applications SET company=?,role=?,industry=?,applied_date=?,
        status=?,source=?,notes=?,company_type=?,company_industry=?,job_type=?,
        apply_url=?,remark_tag=?,evaluation=?,job_description=?,gap_analysis=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (
            merged.get("company"), merged.get("role"), merged.get("industry"),
            merged.get("applied_date"), merged.get("status"), merged.get("source"),
            merged.get("notes"), merged.get("company_type") or cls["company_type"],
            merged.get("company_industry") or merged.get("industry") or cls["company_industry"],
            merged.get("job_type") or infer_job_type(merged.get("role"), merged.get("notes")),
            merged.get("apply_url"), merged.get("remark_tag"), merged.get("evaluation"),
            merged.get("job_description"), merged.get("gap_analysis"),
            target_application_id,
        ),
    )
    conn.execute(
        "UPDATE interviews SET application_id=? WHERE application_id=?",
        (target_application_id, source_application_id),
    )
    for table in ("job_opportunities", "email_message_states"):
        conn.execute(
            f"UPDATE {table} SET application_id=? WHERE application_id=?",
            (target_application_id, source_application_id),
        )

    target_track_id = target.get("track_id")
    source_track_id = source.get("track_id")
    if target_track_id:
        conn.execute(
            """UPDATE job_tracks SET company=?,role=?,target=?,
            jd=COALESCE(NULLIF(?,''),jd), apply_url=COALESCE(NULLIF(?,''),apply_url),
            updated_at=datetime('now','localtime') WHERE id=?""",
            (
                merged.get("company"), merged.get("role"), merged.get("industry"),
                merged.get("job_description") or "", merged.get("apply_url") or "",
                target_track_id,
            ),
        )
    conn.execute("DELETE FROM applications WHERE id=?", (source_application_id,))

    # Remove an automatically-created empty duplicate track. Never delete a
    # track that already owns knowledge, documents, interviews, or other work.
    if source_track_id and source_track_id != target_track_id:
        has_other_app = conn.execute(
            "SELECT 1 FROM applications WHERE track_id=? LIMIT 1", (source_track_id,)
        ).fetchone()
        dependent = False
        for table in (
            "track_gaps", "resume_versions", "knowledge_items", "knowledge_folders",
            "knowledge_agent_runs", "documents", "interview_rounds",
            "career_calendar_items", "interview_reviews", "interview_questions",
        ):
            try:
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE track_id=? LIMIT 1", (source_track_id,)
                ).fetchone():
                    dependent = True
                    break
            except sqlite3.OperationalError:
                continue
        if not has_other_app and not dependent:
            conn.execute("DELETE FROM job_tracks WHERE id=?", (source_track_id,))

    conn.commit()
    conn.close()
    return target_application_id


def _infer_track_group(d):
    """给具体岗位一个可编辑的初始板块，不替用户做过细分类。"""
    text = " ".join(str(d.get(k) or "") for k in
                    ("role", "industry", "target", "job_description"))
    if any(x.lower() in text.lower() for x in
           ("ai", "人工智能", "大模型", "算法", "智能体", "agent")):
        return "AI 相关"
    if any(x in text for x in ("数据", "商业分析", "经营分析")):
        return "数据与分析"
    if any(x in text for x in ("金融", "证券", "银行", "保险", "DCM", "债券", "定价发行", "簿记建档", "承销", "固收")):
        return "金融相关"
    return "其他岗位"


def classify_company(company="", role=""):
    """本地优先的公司性质/行业细分归类。后续可接联网搜索或 AI 校准。"""
    text = f"{company or ''} {role or ''}".lower()
    rules = [
        (("字节", "抖音", "tiktok", "tiktok shop", "飞书", "火山"), ("internet", "内容/电商平台")),
        (("阿里", "淘宝", "天猫", "菜鸟", "qoder"), ("internet", "电商/云计算")),
        (("美团", "大众点评"), ("internet", "本地生活")),
        (("腾讯", "微信", "qq"), ("internet", "社交/游戏/云")),
        (("京东",), ("internet", "电商/供应链")),
        (("拼多多", "pdd"), ("internet", "电商")),
        (("小红书",), ("internet", "社区/内容平台")),
        (("快手",), ("internet", "短视频/直播")),
        (("百度",), ("internet", "搜索/AI")),
        (("网易",), ("internet", "内容/游戏")),
        (("公募", "基金"), ("public", "公募基金")),
        (("泰康", "资产", "资管", "信托"), ("insurance_trust", "保险/资管/信托")),
        (("申万宏源", "证券", "券商", "投行", "头部券商", "dcm", "债券", "定价发行", "簿记建档", "承销", "固收"), ("broker", "证券/投行")),
        (("同花顺", "东方财富", "wind", "万得"), ("finance", "金融科技/行情数据")),
        (("银行",), ("bank", "银行")),
        (("保险",), ("insurance_trust", "保险")),
        (("中石油", "中石化", "国家电网", "中国移动", "中国电信", "中国联通", "中国邮政"), ("soe", "央企/基础设施")),
        (("国企", "央企"), ("soe", "国央企")),
        (("咨询", "麦肯锡", "bcg", "贝恩", "德勤", "普华永道", "pwc", "安永", "毕马威"), ("consulting", "咨询/专业服务")),
        (("汽车", "比亚迪", "蔚来", "理想", "小鹏", "特斯拉"), ("manufacturing", "汽车/智能制造")),
    ]
    for keys, val in rules:
        if any(k in text for k in keys):
            return {"company_type": val[0], "company_industry": val[1]}
    return {"company_type": "other", "company_industry": "其他/待确认"}


def infer_job_type(role="", notes=""):
    text = f"{role or ''} {notes or ''}".lower()
    if any(k in text for k in ("日常", "日常实习", "daily")):
        return "daily"
    if any(k in text for k in ("暑期", "summer")):
        return "summer"
    if any(k in text for k in ("提前批", "early")):
        return "autumn_early"
    if any(k in text for k in ("秋招", "校招", "应届")):
        return "autumn"
    if any(k in text for k in ("春招", "spring")):
        return "spring"
    if any(k in text for k in ("实习", "intern")):
        return "daily"
    return "unknown"


def ensure_application_tracks():
    """让每条具体投递天然拥有一条求职线，并兼容已有未关联数据。"""
    conn = get_db()
    apps = rows(conn.execute(
        """SELECT id,company,role,industry,job_description,track_id,apply_url,job_type
        FROM applications ORDER BY id""").fetchall())
    changed = 0
    for app in apps:
        tid = app.get("track_id")
        if tid and conn.execute("SELECT 1 FROM job_tracks WHERE id=?", (tid,)).fetchone():
            continue
        existing = conn.execute(
            """SELECT id FROM job_tracks
            WHERE trim(COALESCE(company,''))=trim(COALESCE(?, ''))
              AND trim(COALESCE(role,''))=trim(COALESCE(?, ''))
            ORDER BY id LIMIT 1""", (app.get("company"), app.get("role"))).fetchone()
        if existing:
            tid = existing["id"]
        else:
            cls = classify_company(app.get("company"), app.get("role"))
            cur = conn.execute(
                """INSERT INTO job_tracks
                (company,role,target,jd,status,notes,track_group,priority,apply_url,company_type,company_industry,job_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (app.get("company"), app.get("role"), app.get("industry"),
                 app.get("job_description"), "active", "由投递自动建立",
                 _infer_track_group(app), "normal", app.get("apply_url"),
                 cls["company_type"], cls["company_industry"],
                 app.get("job_type") or infer_job_type(app.get("role"))))
            tid = cur.lastrowid
        conn.execute(
            "UPDATE applications SET track_id=?,updated_at=datetime('now','localtime') WHERE id=?",
            (tid, app["id"]))
        changed += 1
    if changed:
        conn.commit()
    conn.close()
    return changed


def set_application_status(aid, status, *, allow_skip=False):
    if status not in STATUS_FLOW:
        raise ValueError("投递状态不正确")
    conn = get_db()
    current = conn.execute("SELECT status FROM applications WHERE id=?", (aid,)).fetchone()
    if not current:
        conn.close()
        return False
    if (
        not allow_skip
        and status != current["status"]
        and status not in STATUS_TRANSITIONS.get(current["status"], set())
    ):
        conn.close()
        raise ValueError(f"不允许从 {current['status']} 直接流转到 {status}")
    cur = conn.execute("UPDATE applications SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                       (status, aid))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def set_application_track(aid, track_id):
    """把一条投递关联到某个求职目标（track_id=None 解除关联）。"""
    conn = get_db()
    if track_id and not conn.execute("SELECT 1 FROM job_tracks WHERE id=?", (track_id,)).fetchone():
        conn.close()
        raise ValueError("求职目标不存在")
    cur = conn.execute("UPDATE applications SET track_id=?, updated_at=datetime('now','localtime') WHERE id=?",
                       (track_id, aid))
    if cur.rowcount <= 0:
        conn.close()
        return False
    if track_id:
        app = one(conn.execute(
            "SELECT company,role,industry,job_description FROM applications WHERE id=?", (aid,)).fetchone()) or {}
        conn.execute("""UPDATE job_tracks SET
            company=COALESCE(NULLIF(company,''),?),
            role=COALESCE(NULLIF(role,''),?),
            target=COALESCE(NULLIF(target,''),?),
            track_group=COALESCE(NULLIF(track_group,''),?),
            jd=COALESCE(NULLIF(?,''),jd),
            updated_at=datetime('now','localtime')
            WHERE id=?""",
            (app.get("company"), app.get("role"), app.get("industry"),
             _infer_track_group(app), app.get("job_description") or "", track_id))
    conn.commit(); conn.close()
    return True


def sync_track_from_applications(track_id):
    """把关联投递里的 JD/公司岗位补回求职线；不强制拆差距项。"""
    conn = get_db()
    track = one(conn.execute("SELECT * FROM job_tracks WHERE id=?", (track_id,)).fetchone())
    if not track:
        conn.close()
        return None
    apps = rows(conn.execute(
        """SELECT * FROM applications
        WHERE track_id=?
        ORDER BY CASE WHEN job_description IS NOT NULL AND trim(job_description)<>'' THEN 0 ELSE 1 END,
                 applied_date DESC,id DESC""", (track_id,)).fetchall())
    picked = next((a for a in apps if (a.get("job_description") or "").strip()), None) or (apps[0] if apps else None)
    changed = False
    if picked:
        fields = {
            "company": track.get("company") or picked.get("company"),
            "role": track.get("role") or picked.get("role"),
            "target": track.get("target") or picked.get("industry"),
            "track_group": track.get("track_group") or _infer_track_group(picked),
            "jd": track.get("jd") or picked.get("job_description"),
        }
        if any((fields.get(k) or "") != (track.get(k) or "") for k in fields):
            conn.execute("""UPDATE job_tracks SET company=?,role=?,target=?,track_group=?,jd=?,
                updated_at=datetime('now','localtime') WHERE id=?""",
                (fields["company"], fields["role"], fields["target"],
                 fields["track_group"], fields["jd"], track_id))
            changed = True
    if changed:
        conn.commit()
    out = one(conn.execute("SELECT * FROM job_tracks WHERE id=?", (track_id,)).fetchone())
    conn.close()
    return out


def delete_application(aid):
    conn = get_db()
    cur = conn.execute("DELETE FROM applications WHERE id=?", (aid,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def dashboard_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"]
    by_status = rows(conn.execute("SELECT status,COUNT(*) n FROM applications GROUP BY status").fetchall())
    by_industry = rows(conn.execute(
        "SELECT industry,COUNT(*) n FROM applications WHERE industry IS NOT NULL AND industry<>'' "
        "GROUP BY industry ORDER BY n DESC LIMIT 8").fetchall())
    conn.close()
    return {"total": total, "by_status": by_status, "by_industry": by_industry}


# ─── 会话 / 对话 ──────────────────────────────────────────────────────────────

def list_sessions():
    conn = get_db()
    r = conn.execute("""
        SELECT s.id, s.title, COALESCE(NULLIF(s.folder,''),'未分类') folder,
            s.workspace_run_id, s.workspace_task_key, s.workspace_mode,
            s.track_id, s.knowledge_item_id, s.gap_id, s.experience_id, s.experience_type,
            s.target_type, s.target_id, s.project_id, s.updated_at,
            COALESCE(s.is_pinned,0) is_pinned,
            jt.company track_company, COALESCE(NULLIF(jt.role,''),jt.target) track_role,
            wp.name workspace_project_name,
            we.company experience_company, we.role experience_role,
            (SELECT assigned_expert FROM agent_tasks a
             WHERE a.conversation_id=s.id ORDER BY a.id DESC LIMIT 1) latest_expert_key,
            (SELECT status FROM agent_tasks a
             WHERE a.conversation_id=s.id ORDER BY a.id DESC LIMIT 1) latest_task_status,
            (SELECT COUNT(*) FROM proposed_changes p
             JOIN agent_tasks a ON a.id=p.task_id
             WHERE a.conversation_id=s.id AND p.status='pending') pending_change_count,
            (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS n
        FROM sessions s
        LEFT JOIN job_tracks jt ON jt.id=s.track_id
        LEFT JOIN projects wp ON wp.id=COALESCE(
            s.project_id,
            CASE WHEN s.target_type='project' THEN s.target_id END
        )
        LEFT JOIN experiences we ON we.id=wp.experience_id
        ORDER BY s.updated_at DESC""").fetchall()
    conn.close()
    return rows(r)


def create_session(sid, title="新对话"):
    for attempt in range(4):
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO sessions (id,title) VALUES (?,?)", (sid, title))
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if attempt == 3:
                raise
            time.sleep(0.12 * (attempt + 1))
        finally:
            conn.close()


def resolve_workspace_session(workspace_task_key, *, mode, title, track_id=None,
                              knowledge_item_id=None, gap_id=None,
                              experience_id=None, experience_type=None,
                              target_type=None, target_id=None, project_id=None):
    """Find or create the durable chat session owned by one workspace task identity."""
    conn = get_db()
    item = conn.execute(
        "SELECT * FROM sessions WHERE workspace_task_key=?",
        (workspace_task_key,),
    ).fetchone()
    if not item:
        sid = uuid.uuid4().hex
        conn.execute("""INSERT INTO sessions
            (id,title,folder,workspace_task_key,workspace_mode,track_id,knowledge_item_id,gap_id,
             experience_id,experience_type,target_type,target_id,project_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, title or "Agent 工作台", "Agent 任务", workspace_task_key, mode or "general",
             track_id, knowledge_item_id, gap_id, experience_id, experience_type,
             target_type, target_id, project_id))
        item = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    else:
        # Older durable workspace sessions may already own the canonical task key
        # while lacking the binding columns introduced later.  Fill only missing
        # metadata here; an existing non-null binding is deliberately preserved so
        # the API layer can still reject attempts to reuse a task for another target.
        conn.execute("""UPDATE sessions SET
            workspace_mode=COALESCE(workspace_mode, ?),
            track_id=COALESCE(track_id, ?),
            knowledge_item_id=COALESCE(knowledge_item_id, ?),
            gap_id=COALESCE(gap_id, ?),
            experience_type=COALESCE(experience_type, ?),
            target_type=COALESCE(target_type, ?),
            target_id=COALESCE(target_id, ?),
            project_id=COALESCE(project_id, ?)
            WHERE id=?""",
            (mode or "general", track_id, knowledge_item_id, gap_id,
             experience_type, target_type, target_id, project_id, item["id"]))
        item = conn.execute("SELECT * FROM sessions WHERE id=?", (item["id"],)).fetchone()
    conn.commit()
    result = one(item)
    conn.close()
    return result


def touch_session(sid, title=None):
    conn = get_db()
    if title:
        cur = conn.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
        if cur and (cur["title"] in (None, "", "新对话")):
            conn.execute("UPDATE sessions SET title=?, updated_at=datetime('now','localtime') WHERE id=?",
                         (title[:24], sid))
        else:
            conn.execute("UPDATE sessions SET updated_at=datetime('now','localtime') WHERE id=?", (sid,))
    else:
        conn.execute("UPDATE sessions SET updated_at=datetime('now','localtime') WHERE id=?", (sid,))
    conn.commit(); conn.close()


def update_session(sid, title=None, folder=None, is_pinned=None):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
        conn.close()
        return False
    if title is not None:
        conn.execute("UPDATE sessions SET title=?,updated_at=datetime('now','localtime') WHERE id=?",
                     ((title.strip() or "新任务")[:80], sid))
    if folder is not None:
        conn.execute("UPDATE sessions SET folder=?,updated_at=datetime('now','localtime') WHERE id=?",
                     ((folder.strip() or "未分类")[:40], sid))
    if is_pinned is not None:
        conn.execute("UPDATE sessions SET is_pinned=?,updated_at=datetime('now','localtime') WHERE id=?",
                     (1 if is_pinned else 0, sid))
    conn.commit(); conn.close()
    return True


def promote_workspace_session_to_document(sid, knowledge_item_id, title):
    """Release a completed :new identity by turning its session into the document task."""
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not session or not (session["workspace_task_key"] or "").endswith(":new"):
        conn.close()
        return False
    if session["track_id"]:
        new_key = f"knowledge_coach:track:{session['track_id']}:document:{knowledge_item_id}"
    else:
        new_key = f"knowledge_coach:global:document:{knowledge_item_id}"
    conflict = conn.execute(
        "SELECT id FROM sessions WHERE workspace_task_key=? AND id<>?", (new_key, sid)
    ).fetchone()
    if conflict:
        conn.close()
        return False
    conn.execute("""UPDATE sessions SET workspace_task_key=?, knowledge_item_id=?, title=?,
                    updated_at=datetime('now','localtime') WHERE id=?""",
                 (new_key, knowledge_item_id, (title or "未命名知识")[:28], sid))
    conn.commit(); conn.close()
    return True


def set_session_workspace(sid, run_id):
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET workspace_run_id=?,updated_at=datetime('now','localtime') WHERE id=?",
        (run_id, sid),
    )
    conn.commit(); conn.close()


def get_session(sid):
    conn = get_db()
    item = one(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    conn.close()
    return item


def delete_session(sid):
    conn = get_db()
    paths = conn.execute("SELECT file_path FROM chat_attachments WHERE session_id=?", (sid,)).fetchall()
    conn.execute("""DELETE FROM chat_message_local_files WHERE message_id IN
                    (SELECT id FROM chat_messages WHERE session_id=?)""", (sid,))
    conn.execute("DELETE FROM chat_attachments WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM chat_messages WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit(); conn.close()
    for row in paths:
        try:
            Path(row["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass


def save_message(session_id, role, content):
    conn = get_db()
    cur = conn.execute("INSERT INTO chat_messages (session_id,role,content) VALUES (?,?,?)",
                       (session_id, role, content))
    message_id = cur.lastrowid
    conn.commit(); conn.close()
    return message_id


def bind_message_local_files(message_id, candidate_ids):
    ids = []
    for value in candidate_ids or []:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return
    conn = get_db()
    conn.executemany(
        "INSERT OR IGNORE INTO chat_message_local_files(message_id,candidate_id) VALUES (?,?)",
        [(message_id, candidate_id) for candidate_id in ids],
    )
    conn.commit(); conn.close()


def get_message_local_files(message_ids):
    ids = [int(value) for value in message_ids or [] if str(value).isdigit()]
    if not ids:
        return []
    conn = get_db()
    placeholders = ",".join("?" for _ in ids)
    result = rows(conn.execute(
        f"""SELECT ml.message_id, c.* FROM chat_message_local_files ml
            JOIN local_file_candidates c ON c.id=ml.candidate_id
            WHERE ml.message_id IN ({placeholders})
            ORDER BY ml.message_id, c.id""",
        ids,
    ).fetchall())
    conn.close()
    return result


def get_chat_history(session_id, limit=30):
    conn = get_db()
    r = conn.execute(
        "SELECT id,role,content,created_at FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit)).fetchall()
    result = list(reversed(rows(r)))
    if result:
        ids = [item["id"] for item in result]
        placeholders = ",".join("?" for _ in ids)
        attachments = rows(conn.execute(
            f"SELECT id,message_id,file_name,mime_type,size_bytes FROM chat_attachments WHERE message_id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall())
        grouped = {}
        for item in attachments:
            item["is_image"] = (item.get("mime_type") or "").startswith("image/")
            grouped.setdefault(item.get("message_id"), []).append(item)
        for item in result:
            item["attachments"] = grouped.get(item["id"], [])
        local_files = get_message_local_files(ids)
        local_grouped = {}
        for local_file in local_files:
            local_grouped.setdefault(local_file.get("message_id"), []).append({
                "id": local_file.get("id"),
                "file_name": local_file.get("file_name"),
                "title": local_file.get("title"),
                "file_path": local_file.get("file_path"),
                "is_local": True,
            })
        for item in result:
            item["local_files"] = local_grouped.get(item["id"], [])
    conn.close()
    return result


def create_chat_attachment(data):
    conn = get_db()
    cur = conn.execute("""INSERT INTO chat_attachments
        (file_name,mime_type,file_path,size_bytes,extracted_text) VALUES (?,?,?,?,?)""",
        (data.get("file_name"), data.get("mime_type"), data.get("file_path"),
         data.get("size_bytes") or 0, data.get("extracted_text") or ""))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    return aid


def get_chat_attachments(ids):
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return []
    conn = get_db()
    placeholders = ",".join("?" for _ in ids)
    result = rows(conn.execute(
        f"SELECT * FROM chat_attachments WHERE id IN ({placeholders}) ORDER BY id", ids
    ).fetchall())
    conn.close()
    return result


def bind_chat_attachments(ids, session_id, message_id):
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return
    conn = get_db()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE chat_attachments SET session_id=?,message_id=? WHERE id IN ({placeholders}) AND message_id IS NULL",
        [session_id, message_id, *ids],
    )
    conn.commit(); conn.close()


def delete_unbound_chat_attachment(aid):
    conn = get_db()
    row = conn.execute("SELECT file_path,message_id FROM chat_attachments WHERE id=?", (aid,)).fetchone()
    if not row or row["message_id"] is not None:
        conn.close()
        return None
    conn.execute("DELETE FROM chat_attachments WHERE id=?", (aid,))
    conn.commit(); conn.close()
    return row["file_path"]


def clear_chat(session_id):
    conn = get_db()
    paths = conn.execute(
        "SELECT file_path FROM chat_attachments WHERE session_id=?",
        (session_id,),
    ).fetchall()
    conn.execute("""DELETE FROM chat_message_local_files WHERE message_id IN
                    (SELECT id FROM chat_messages WHERE session_id=?)""", (session_id,))
    conn.execute("DELETE FROM chat_attachments WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
    conn.commit(); conn.close()
    for row in paths:
        try:
            Path(row["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass


def truncate_chat(session_id, from_message_id):
    """Delete one message and everything after it within the same conversation."""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM chat_messages WHERE id=? AND session_id=?",
        (from_message_id, session_id),
    ).fetchone()
    if not row:
        conn.close()
        return False
    paths = conn.execute(
        """SELECT file_path FROM chat_attachments
           WHERE session_id=? AND message_id IN (
               SELECT id FROM chat_messages WHERE session_id=? AND id>=?
           )""",
        (session_id, session_id, from_message_id),
    ).fetchall()
    conn.execute(
        """DELETE FROM chat_message_local_files WHERE message_id IN (
               SELECT id FROM chat_messages WHERE session_id=? AND id>=?
           )""",
        (session_id, from_message_id),
    )
    conn.execute(
        """DELETE FROM chat_attachments
           WHERE session_id=? AND message_id IN (
               SELECT id FROM chat_messages WHERE session_id=? AND id>=?
           )""",
        (session_id, session_id, from_message_id),
    )
    conn.execute(
        "DELETE FROM chat_messages WHERE session_id=? AND id>=?",
        (session_id, from_message_id),
    )
    conn.execute(
        "UPDATE sessions SET updated_at=datetime('now','localtime') WHERE id=?",
        (session_id,),
    )
    conn.commit(); conn.close()
    for item in paths:
        try:
            Path(item["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return True


# ─── 面试记录 ─────────────────────────────────────────────────────────────────

def add_interview(app_id, d):
    conn = get_db()
    last = conn.execute("SELECT MAX(round_number) n FROM interviews WHERE application_id=?",
                        (app_id,)).fetchone()["n"] or 0
    cur = conn.execute("""INSERT INTO interviews
        (application_id,round_number,round_type,interview_date,duration_minutes,
         location,meeting_link,notes,feedback,outcome,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
        (app_id, d.get("round_number") or last + 1, d.get("round_type"),
         d.get("interview_date"), d.get("duration_minutes") or 60,
         d.get("location"), d.get("meeting_link"), d.get("notes"),
         d.get("feedback"), d.get("outcome")))
    iid = cur.lastrowid
    conn.commit(); conn.close()
    return iid


def get_interview(iid):
    conn = get_db()
    result = one(conn.execute(
        """SELECT i.*,a.track_id,a.company,a.role
             FROM interviews i
             JOIN applications a ON a.id=i.application_id
            WHERE i.id=?""",
        (iid,),
    ).fetchone())
    conn.close()
    return result


def update_interview(iid, d):
    conn = get_db()
    cur = conn.execute("""UPDATE interviews SET round_number=?,round_type=?,interview_date=?,
        duration_minutes=?,location=?,meeting_link=?,notes=?,feedback=?,outcome=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("round_number"), d.get("round_type"), d.get("interview_date"),
         d.get("duration_minutes") or 60, d.get("location"),
         d.get("meeting_link"), d.get("notes"), d.get("feedback"),
         d.get("outcome"), iid))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_interview(iid):
    conn = get_db()
    existing = conn.execute(
        "SELECT application_id FROM interviews WHERE id=?",
        (iid,),
    ).fetchone()
    if not existing:
        conn.close()
        return False
    application_id = existing["application_id"]
    cur = conn.execute("DELETE FROM interviews WHERE id=?", (iid,))
    # round_number is displayed throughout the calendar and job workspace.
    # Closing a gap here keeps every consumer on one canonical sequence after
    # a mistaken/obsolete interview is removed.
    remaining = conn.execute(
        """SELECT id FROM interviews
           WHERE application_id=?
           ORDER BY COALESCE(round_number,999999),
                    COALESCE(interview_date,'9999-12-31 23:59'),id""",
        (application_id,),
    ).fetchall()
    for round_number, row in enumerate(remaining, 1):
        conn.execute(
            """UPDATE interviews SET round_number=?,
               updated_at=datetime('now','localtime') WHERE id=?""",
            (round_number, row["id"]),
        )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def list_interview_schedule(date_from=None, date_to=None):
    conn = get_db()
    q = """SELECT i.*,a.company,a.role,a.status AS application_status,a.track_id
           FROM interviews i JOIN applications a ON a.id=i.application_id
           WHERE i.interview_date IS NOT NULL AND trim(i.interview_date)<>''"""
    args = []
    if date_from:
        q += " AND i.interview_date>=?"
        args.append(date_from)
    if date_to:
        q += " AND i.interview_date<?"
        args.append(date_to)
    q += " ORDER BY i.interview_date,i.id"
    result = rows(conn.execute(q, args).fetchall())
    conn.close()
    items = (result + list_application_timeline_schedule(date_from, date_to)
             + list_opportunity_plan_schedule(date_from, date_to)
             + list_career_calendar_items(date_from, date_to))
    return sorted(items, key=lambda x: (str(x.get("interview_date") or ""), x.get("id") or 0))


def list_application_timeline_schedule(date_from=None, date_to=None):
    conn = get_db()
    out = []
    q = """SELECT a.id AS application_id,a.track_id,a.company,a.role,a.status AS application_status,
                  a.applied_date
           FROM applications a
           WHERE a.applied_date IS NOT NULL AND trim(a.applied_date)<>''"""
    args = []
    if date_from:
        q += " AND a.applied_date>=?"
        args.append(str(date_from)[:10])
    if date_to:
        q += " AND a.applied_date<?"
        args.append(str(date_to)[:10])
    for row in rows(conn.execute(q, args).fetchall()):
        out.append({
            **row, "id": row["application_id"], "kind": "application_applied",
            "title": "投递", "round_type": "已投递",
            "interview_date": row["applied_date"] + " 09:00",
        })
    q = """SELECT m.*,a.track_id,a.company,a.role,a.status AS application_status
           FROM application_milestones m JOIN applications a ON a.id=m.application_id
           WHERE m.event_date IS NOT NULL AND trim(m.event_date)<>''"""
    args = []
    if date_from:
        q += " AND m.event_date>=?"
        args.append(date_from)
    if date_to:
        q += " AND m.event_date<?"
        args.append(date_to)
    for row in rows(conn.execute(q, args).fetchall()):
        row["kind"] = "application_milestone"
        row["interview_date"] = row.get("event_date")
        row["round_type"] = row.get("title")
        out.append(row)
    conn.close()
    return out


def list_application_timeline(application_id):
    conn = get_db()
    app = one(conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone())
    if not app:
        conn.close()
        return None
    items = []
    if app.get("applied_date"):
        items.append({
            "id": application_id, "kind": "application_applied", "event_type": "applied",
            "title": "已投递", "event_date": app["applied_date"] + " 09:00",
        })
    for row in rows(conn.execute(
        "SELECT * FROM application_milestones WHERE application_id=? ORDER BY event_date,id",
        (application_id,),
    ).fetchall()):
        items.append({**row, "kind": "application_milestone"})
    for row in rows(conn.execute(
        "SELECT * FROM interviews WHERE application_id=? ORDER BY interview_date,id",
        (application_id,),
    ).fetchall()):
        items.append({
            **row, "kind": "interview", "event_type": "interview",
            "title": row.get("round_type") or f"第 {row.get('round_number') or 1} 轮面试",
            "event_date": row.get("interview_date"),
        })
    conn.close()
    items = [x for x in items if str(x.get("event_date") or "").strip()]
    items.sort(key=lambda x: (str(x.get("event_date")), x.get("id") or 0))
    return {"application": app, "items": items}


def create_application_milestone(application_id, d):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM applications WHERE id=?", (application_id,)).fetchone():
        conn.close()
        raise ValueError("投递不存在")
    cur = conn.execute("""INSERT INTO application_milestones
        (application_id,event_type,title,event_date,notes) VALUES (?,?,?,?,?)""",
        (application_id, d.get("event_type") or "followup", d.get("title"),
         d.get("event_date"), d.get("notes")))
    mid = cur.lastrowid
    conn.commit(); conn.close()
    return mid


def delete_application_milestone(mid):
    conn = get_db()
    cur = conn.execute("DELETE FROM application_milestones WHERE id=?", (mid,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def list_career_calendar_items(date_from=None, date_to=None):
    conn = get_db()
    q = """SELECT c.*,t.company,t.role,t.priority
           FROM career_calendar_items c LEFT JOIN job_tracks t ON t.id=c.track_id
           WHERE c.starts_at IS NOT NULL AND trim(c.starts_at)<>''"""
    args = []
    if date_from:
        q += " AND c.starts_at>=?"
        args.append(date_from)
    if date_to:
        q += " AND c.starts_at<?"
        args.append(date_to)
    q += " ORDER BY c.starts_at,c.id"
    out = rows(conn.execute(q, args).fetchall())
    conn.close()
    for x in out:
        x["kind"] = "career_calendar"
        x["interview_date"] = x.get("starts_at")
        x["round_type"] = x.get("title")
    return out


def create_career_calendar_item(d):
    conn = get_db()
    if d.get("track_id") and not conn.execute("SELECT 1 FROM job_tracks WHERE id=?", (d.get("track_id"),)).fetchone():
        conn.close()
        raise ValueError("关联岗位不存在")
    cur = conn.execute("""INSERT INTO career_calendar_items
        (track_id,title,item_type,starts_at,duration_minutes,color,target_count,status,notes)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (d.get("track_id"), d.get("title"), d.get("item_type") or "task",
         d.get("starts_at"), d.get("duration_minutes") or 30,
         d.get("color") or "green", d.get("target_count"),
         d.get("status") or "todo", d.get("notes")))
    iid = cur.lastrowid
    conn.commit(); conn.close()
    return iid


def update_career_calendar_item(iid, d):
    conn = get_db()
    cur = conn.execute("""UPDATE career_calendar_items
        SET track_id=?,title=?,item_type=?,starts_at=?,duration_minutes=?,color=?,target_count=?,status=?,notes=?,
            updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("track_id"), d.get("title"), d.get("item_type") or "task",
         d.get("starts_at"), d.get("duration_minutes") or 30,
         d.get("color") or "green", d.get("target_count"), d.get("status") or "todo", d.get("notes"), iid))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_career_calendar_item(iid):
    conn = get_db()
    cur = conn.execute("DELETE FROM career_calendar_items WHERE id=?", (iid,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ─── 面试制品（自我介绍版本 / 项目话术）────────────────────────────────────

def list_interview_items(kind=None):
    conn = get_db()
    if kind:
        r = conn.execute("SELECT * FROM interview_items WHERE kind=? ORDER BY updated_at DESC", (kind,)).fetchall()
    else:
        r = conn.execute("SELECT * FROM interview_items ORDER BY updated_at DESC").fetchall()
    conn.close()
    return rows(r)


def get_interview_item(iid):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM interview_items WHERE id=?", (iid,)).fetchone())
    conn.close()
    return x


def create_interview_item(d):
    conn = get_db()
    project_id = d.get("project_id")
    if project_id and not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
        conn.close()
        raise ValueError("关联项目不存在")
    cur = conn.execute(
        "INSERT INTO interview_items (kind,title,target,content,project_id) VALUES (?,?,?,?,?)",
        (d.get("kind"), d.get("title"), d.get("target"), d.get("content"), d.get("project_id")))
    iid = cur.lastrowid
    conn.commit(); conn.close()
    return iid


def update_interview_item(iid, d):
    conn = get_db()
    cur = conn.execute("""UPDATE interview_items SET title=?,target=?,content=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("title"), d.get("target"), d.get("content"), iid))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_interview_item(iid):
    conn = get_db()
    cur = conn.execute("DELETE FROM interview_items WHERE id=?", (iid,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ─── 在职日记 ─────────────────────────────────────────────────────────────────

def list_work_logs(experience_id=None, limit=200):
    conn = get_db()
    if experience_id:
        r = conn.execute("SELECT * FROM work_logs WHERE experience_id=? ORDER BY log_date DESC, id DESC LIMIT ?",
                         (experience_id, limit)).fetchall()
    else:
        r = conn.execute("SELECT * FROM work_logs ORDER BY log_date DESC, id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows(r)


def create_work_log(d):
    conn = get_db()
    cur = conn.execute("INSERT INTO work_logs (log_date,experience_id,content) VALUES (?,?,?)",
                       (d.get("log_date") or datetime.now().strftime("%Y-%m-%d"),
                        d.get("experience_id"), d.get("content")))
    wid = cur.lastrowid
    conn.commit(); conn.close()
    return wid


def delete_work_log(wid):
    conn = get_db()
    conn.execute("DELETE FROM work_logs WHERE id=?", (wid,))
    conn.commit(); conn.close()


def list_work_log_project_links(work_log_id=None, project_id=None):
    conn = get_db()
    q = "SELECT * FROM work_log_project_links WHERE 1=1"
    args = []
    if work_log_id is not None:
        q += " AND work_log_id=?"
        args.append(work_log_id)
    if project_id is not None:
        q += " AND project_id=?"
        args.append(project_id)
    q += " ORDER BY id DESC"
    result = rows(conn.execute(q, args).fetchall())
    conn.close()
    return result


def adopt_worklog_digest(experience_id, digest, mode="create", project_id=None):
    """把一条日记提炼候选采纳为项目，并一次性写入关联与数据缺口。"""
    conn = get_db()
    try:
        conn.execute("BEGIN")
        log_ids = [int(x) for x in (digest.get("covered_log_ids") or [])]
        valid_ids = []
        if log_ids:
            marks = ",".join("?" for _ in log_ids)
            valid_ids = [
                r["id"] for r in conn.execute(
                    f"SELECT id FROM work_logs WHERE experience_id=? AND id IN ({marks})",
                    [experience_id, *log_ids],
                ).fetchall()
            ]

        if mode == "merge":
            project = one(conn.execute(
                "SELECT * FROM projects WHERE id=? AND experience_id=?",
                (project_id, experience_id),
            ).fetchone())
            if not project:
                raise ValueError("找不到要并入的项目，或项目不属于当前经历")
            merged = {
                "name": project.get("name"),
                "one_liner": digest.get("one_liner") or project.get("one_liner"),
                "document": digest.get("document") or project.get("document"),
                "technologies": digest.get("technologies") or project.get("technologies"),
                "keywords": project.get("keywords"),
            }
            merged = _normalize_project_fields(merged)
            conn.execute("""
                UPDATE projects SET name=?,one_liner=?,document=?,technologies=?,keywords=?,
                    updated_at=datetime('now','localtime') WHERE id=?""",
                (merged["name"], merged["one_liner"], merged["document"],
                 merged["technologies"], merged["keywords"], project_id))
            conn.execute("DELETE FROM search_index WHERE entity_type='project' AND entity_id=?", (project_id,))
            _reindex_project(conn, project_id, merged)
            pid = project_id
        else:
            project_data = _normalize_project_fields({
                "name": digest.get("title") or "日记提炼项目",
                "one_liner": digest.get("one_liner"),
                "document": digest.get("document"),
                "technologies": digest.get("technologies"),
                "keywords": digest.get("keywords"),
            })
            cur = conn.execute(
                "INSERT INTO projects (experience_id,name,one_liner,document,technologies,keywords) VALUES (?,?,?,?,?,?)",
                (experience_id, project_data["name"], project_data["one_liner"],
                 project_data["document"], project_data["technologies"],
                 project_data["keywords"]),
            )
            pid = cur.lastrowid
            _reindex_project(conn, pid, project_data)

        for wid in valid_ids:
            conn.execute("""INSERT OR IGNORE INTO work_log_project_links
                (work_log_id,project_id,relation) VALUES (?,?,'derived_into')""", (wid, pid))

        followup_ids = []
        for metric in digest.get("missing_metrics") or []:
            metric = str(metric).strip()
            if not metric:
                continue
            cur = conn.execute("""INSERT INTO followups
                (project_id,question,status,category,origin,kind)
                VALUES (?,?,'todo','数据缺口','worklog_digest','factual')""",
                (pid, f"补充量化数据：{metric}"))
            followup_ids.append(cur.lastrowid)

        conn.commit()
        return {"project_id": pid, "linked_log_ids": valid_ids, "followup_ids": followup_ids}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── 资料资产中心 ─────────────────────────────────────────────────────────────

def list_sources(source_type=None, limit=200):
    conn = get_db()
    if source_type:
        r = conn.execute("""SELECT id,source_type,title,file_name,summary,status,origin,content_hash,
            track_id,tags,confidence,created_at,updated_at
            FROM sources WHERE source_type=? ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (source_type, limit)).fetchall()
    else:
        r = conn.execute("""SELECT id,source_type,title,file_name,summary,status,origin,content_hash,
            track_id,tags,confidence,created_at,updated_at
            FROM sources ORDER BY updated_at DESC, id DESC LIMIT ?""", (limit,)).fetchall()
    conn.close()
    return rows(r)


def get_source(sid):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone())
    conn.close()
    return x


def create_source(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO sources
        (source_type,title,content,file_name,file_path,summary,analysis_json,status,
         track_id,origin,content_hash,content_date,tags,confidence,lang,ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("source_type") or "other", d.get("title"), d.get("content"),
         d.get("file_name"), d.get("file_path"), d.get("summary"),
         d.get("analysis_json"), d.get("status") or "raw",
         d.get("track_id"), d.get("origin"), d.get("content_hash"),
         d.get("content_date"), d.get("tags"), d.get("confidence"),
         d.get("lang"), d.get("ingested_at")))
    sid = cur.lastrowid
    conn.commit(); conn.close()
    return sid


def update_source_analysis(sid, d):
    conn = get_db()
    conn.execute("""UPDATE sources SET source_type=?,title=?,summary=?,analysis_json=?,
        status=?, tags=COALESCE(?,tags), confidence=COALESCE(?,confidence),
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("source_type") or "other", d.get("title"), d.get("summary"),
         d.get("analysis_json"), d.get("status") or "analyzed",
         d.get("tags"), d.get("confidence"), sid))
    conn.commit(); conn.close()


def refresh_source_snapshot(sid, content, content_hash):
    """Refresh searchable text for a linked local file without replacing the original."""
    conn = get_db()
    conn.execute(
        """UPDATE sources
           SET content=?, content_hash=?, updated_at=datetime('now','localtime')
           WHERE id=?""",
        (content, content_hash, sid),
    )
    source = one(conn.execute(
        "SELECT title,file_name FROM sources WHERE id=?", (sid,)
    ).fetchone())
    conn.execute(
        "DELETE FROM search_index WHERE entity_type='source' AND entity_id=?",
        (sid,),
    )
    if source:
        conn.execute(
            """INSERT INTO search_index(entity_type,entity_id,title,content)
               VALUES ('source',?,?,?)""",
            (sid, source.get("title") or source.get("file_name") or "", content or ""),
        )
    conn.commit()
    conn.close()


def delete_source(sid):
    conn = get_db()
    cur = conn.execute("DELETE FROM sources WHERE id=?", (sid,))
    _delete_search_entities(conn, "source", [sid])
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ─── 邮箱助理处理状态 ─────────────────────────────────────────────────────────

def list_email_message_states(account=None):
    conn = get_db()
    if account:
        result = rows(conn.execute(
            "SELECT * FROM email_message_states WHERE account=? ORDER BY handled_at DESC",
            (account,),
        ).fetchall())
    else:
        result = rows(conn.execute(
            "SELECT * FROM email_message_states ORDER BY handled_at DESC"
        ).fetchall())
    conn.close()
    return result


def get_email_message_state(message_key):
    conn = get_db()
    result = one(conn.execute(
        "SELECT * FROM email_message_states WHERE message_key=?",
        (message_key,),
    ).fetchone())
    conn.close()
    return result


def save_email_message_state(message_key, data):
    conn = get_db()
    conn.execute(
        """INSERT INTO email_message_states
           (message_key,account,status,subject,sender,message_date,application_id)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(message_key) DO UPDATE SET
             account=excluded.account,status=excluded.status,subject=excluded.subject,
             sender=excluded.sender,message_date=excluded.message_date,
             application_id=COALESCE(excluded.application_id,email_message_states.application_id),
             updated_at=datetime('now','localtime')""",
        (
            message_key, data.get("account"), data.get("status"),
            data.get("subject"), data.get("sender"), data.get("message_date"),
            data.get("application_id"),
        ),
    )
    conn.commit()
    result = one(conn.execute(
        "SELECT * FROM email_message_states WHERE message_key=?",
        (message_key,),
    ).fetchone())
    conn.close()
    return result


def delete_email_message_state(message_key):
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM email_message_states WHERE message_key=?",
        (message_key,),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def create_email_reminder(message_key, data):
    conn = get_db()
    conn.execute(
        """INSERT INTO email_reminders (message_key,account,title,detail,status)
           VALUES (?,?,?,?, 'todo')
           ON CONFLICT(message_key) DO UPDATE SET
             title=excluded.title,detail=excluded.detail,
             updated_at=datetime('now','localtime')""",
        (message_key, data.get("account"), data.get("title"), data.get("detail")),
    )
    conn.commit(); conn.close()


def list_email_reminders(status="todo", limit=20):
    conn = get_db()
    result = rows(conn.execute(
        """SELECT * FROM email_reminders
           WHERE (? IS NULL OR status=?)
           ORDER BY CASE status WHEN 'todo' THEN 0 ELSE 1 END,created_at DESC LIMIT ?""",
        (status, status, int(limit or 20)),
    ).fetchall())
    conn.close()
    return result


def complete_email_reminder(message_key):
    conn = get_db()
    cur = conn.execute(
        """UPDATE email_reminders SET status='done',
           completed_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
           WHERE message_key=? AND status<>'done'""",
        (message_key,),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def list_source_links(entity_type=None, entity_id=None, source_id=None):
    conn = get_db()
    q = "SELECT * FROM source_links WHERE 1=1"
    args = []
    if entity_type:
        q += " AND entity_type=?"
        args.append(entity_type)
    if entity_id is not None:
        q += " AND entity_id=?"
        args.append(entity_id)
    if source_id is not None:
        q += " AND source_id=?"
        args.append(source_id)
    q += " ORDER BY id DESC"
    r = conn.execute(q, args).fetchall()
    conn.close()
    return rows(r)


def create_source_link(source_id, entity_type, entity_id, relation="evidence_for", reason=None):
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM source_links WHERE source_id=? AND entity_type=? AND entity_id=? AND relation=?",
        (source_id, entity_type, entity_id, relation),
    ).fetchone()
    if existing:
        conn.close()
        return existing["id"]
    cur = conn.execute(
        """INSERT INTO source_links
           (source_id,entity_type,entity_id,relation,reason)
           VALUES (?,?,?,?,?)""",
        (source_id, entity_type, entity_id, relation, reason),
    )
    lid = cur.lastrowid
    conn.commit(); conn.close()
    return lid


def delete_source_link(source_id, entity_type, entity_id):
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM source_links WHERE source_id=? AND entity_type=? AND entity_id=?",
        (source_id, entity_type, entity_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def find_source_by_hash(content_hash):
    if not content_hash:
        return None
    conn = get_db()
    x = one(conn.execute("SELECT * FROM sources WHERE content_hash=? ORDER BY id DESC LIMIT 1",
                         (content_hash,)).fetchone())
    conn.close()
    return x


def create_discovery_run(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO local_discovery_runs
        (roots_json,options_json,status,summary)
        VALUES (?,?,?,?)""",
        (d.get("roots_json"), d.get("options_json"), d.get("status") or "running",
         d.get("summary")))
    rid = cur.lastrowid
    conn.commit(); conn.close()
    return rid


def update_discovery_run(run_id, **patch):
    allowed = {"status", "total_seen", "candidate_count", "imported_count",
               "ignored_count", "error_count", "summary", "finished_at"}
    sets, args = [], []
    for key, value in patch.items():
        if key in allowed:
            sets.append(f"{key}=?")
            args.append(value)
    if not sets:
        return
    sets.append("updated_at=datetime('now','localtime')")
    args.append(run_id)
    conn = get_db()
    conn.execute(f"UPDATE local_discovery_runs SET {','.join(sets)} WHERE id=?", args)
    conn.commit(); conn.close()


def get_discovery_run(run_id):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM local_discovery_runs WHERE id=?", (run_id,)).fetchone())
    conn.close()
    return x


def list_discovery_runs(limit=20):
    conn = get_db()
    r = conn.execute("""SELECT * FROM local_discovery_runs
        ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    conn.close()
    return rows(r)


def upsert_file_candidate(d):
    conn = get_db()
    existing = one(conn.execute("""SELECT * FROM local_file_candidates
        WHERE file_path=? ORDER BY id DESC LIMIT 1""", (d.get("file_path"),)).fetchone())
    if existing and existing.get("content_hash") == d.get("content_hash"):
        incoming_status = d.get("status")
        if existing.get("status") in {"imported", "ignored"} and incoming_status in {None, "candidate", "low_confidence", "duplicate"}:
            incoming_status = existing.get("status")
        conn.execute("""UPDATE local_file_candidates SET run_id=?,file_name=?,extension=?,
            size_bytes=?,modified_at=?,sample_text=?,source_type=?,title=?,summary=?,
            confidence=?,signals_json=?,analysis_json=?,duplicate_source_id=?,
            suggested_track_json=?,status=?,error=?,updated_at=datetime('now','localtime')
            WHERE id=?""",
            (d.get("run_id"), d.get("file_name"), d.get("extension"),
             d.get("size_bytes"), d.get("modified_at"), d.get("sample_text"),
             d.get("source_type") or "other", d.get("title"), d.get("summary"),
             d.get("confidence") or 0, d.get("signals_json"), d.get("analysis_json"),
             d.get("duplicate_source_id"), d.get("suggested_track_json"),
             incoming_status or existing.get("status") or "candidate", d.get("error"),
             existing["id"]))
        cid = existing["id"]
    else:
        cur = conn.execute("""INSERT INTO local_file_candidates
            (run_id,file_path,file_name,extension,size_bytes,modified_at,content_hash,
             sample_text,source_type,title,summary,confidence,signals_json,analysis_json,
             duplicate_source_id,suggested_track_json,status,error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d.get("run_id"), d.get("file_path"), d.get("file_name"),
             d.get("extension"), d.get("size_bytes") or 0, d.get("modified_at"),
             d.get("content_hash"), d.get("sample_text"), d.get("source_type") or "other",
             d.get("title"), d.get("summary"), d.get("confidence") or 0,
             d.get("signals_json"), d.get("analysis_json"), d.get("duplicate_source_id"),
             d.get("suggested_track_json"), d.get("status") or "candidate", d.get("error")))
        cid = cur.lastrowid
    conn.commit(); conn.close()
    return cid


def list_file_candidates(run_id=None, status=None, limit=300):
    conn = get_db()
    q = "SELECT * FROM local_file_candidates WHERE 1=1"
    args = []
    if run_id is not None:
        q += " AND run_id=?"; args.append(run_id)
    if status:
        q += " AND status=?"; args.append(status)
    q += " ORDER BY confidence DESC, updated_at DESC, id DESC LIMIT ?"
    args.append(limit)
    r = conn.execute(q, args).fetchall()
    conn.close()
    return rows(r)


def get_file_candidate(candidate_id):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM local_file_candidates WHERE id=?", (candidate_id,)).fetchone())
    conn.close()
    return x


def update_file_candidate(candidate_id, **patch):
    allowed = {"source_type", "title", "summary", "confidence", "signals_json",
               "analysis_json", "duplicate_source_id", "matched_source_id",
               "suggested_track_json", "status", "error"}
    sets, args = [], []
    for key, value in patch.items():
        if key in allowed:
            sets.append(f"{key}=?")
            args.append(value)
    if not sets:
        return
    sets.append("updated_at=datetime('now','localtime')")
    args.append(candidate_id)
    conn = get_db()
    conn.execute(f"UPDATE local_file_candidates SET {','.join(sets)} WHERE id=?", args)
    conn.commit(); conn.close()


def discovery_stats(run_id=None):
    conn = get_db()
    if run_id is None:
        r = conn.execute("""SELECT status, COUNT(*) n FROM local_file_candidates
            GROUP BY status""").fetchall()
    else:
        r = conn.execute("""SELECT status, COUNT(*) n FROM local_file_candidates
            WHERE run_id=? GROUP BY status""", (run_id,)).fetchall()
    conn.close()
    return {x["status"]: x["n"] for x in r}


# ─── 秋招机会库：投递前机会、规则卡、时间雷达 ───────────────────────────────

OPPORTUNITY_STATUS_LABEL = {
    "new": "新机会",
    "watching": "观察中",
    "shortlisted": "可投递",
    "planned": "已排期",
    "converted": "已转工作台",
    "dismissed": "已放弃",
    "expired": "已过期",
}

OPPORTUNITY_BATCH_LABEL = {
    "early": "提前批",
    "autumn": "秋招正式批",
    "supplement": "补录",
    "spring": "春招",
    "intern": "实习",
    "unknown": "待确认",
}


def _date_obj(value):
    if not value:
        return None
    text = str(value).strip().replace(".", "-").replace("/", "-")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _date_key(d):
    return d.strftime("%Y-%m-%d") if isinstance(d, date) else None


def _today():
    return datetime.now().date()


def _opportunity_rule_unknown(rule):
    if not rule:
        return True
    vals = [
        rule.get("early_batch_impact"),
        rule.get("locks_choice"),
        rule.get("multi_apply_allowed"),
        rule.get("rolling_review"),
        rule.get("referral_required"),
        rule.get("resume_editable"),
    ]
    return (rule.get("rule_status") or "unverified") != "verified" or any((v or "unknown") == "unknown" for v in vals)


def _opportunity_radar(opp, rule=None):
    today = _today()
    deadline = _date_obj(opp.get("deadline_date"))
    flow_days = max(0, int(opp.get("flow_days") or 0))
    buffer_days = max(0, int(opp.get("buffer_days") or 0))
    latest_start = deadline - timedelta(days=flow_days + buffer_days) if deadline else None
    days_to_deadline = (deadline - today).days if deadline else None
    days_to_start = (latest_start - today).days if latest_start else None
    status = opp.get("status") or "new"
    if status == "converted":
        state = "converted"
    elif status == "dismissed":
        state = "dismissed"
    elif deadline and days_to_deadline < 0:
        state = "expired"
    elif latest_start and days_to_start <= 0:
        state = "must_start_today"
    elif latest_start and days_to_start <= 2:
        state = "risk"
    elif deadline and days_to_deadline <= 7:
        state = "this_week"
    elif _opportunity_rule_unknown(rule):
        state = "rules_unverified"
    else:
        state = "open"
    return {
        "state": state,
        "latest_start_date": _date_key(latest_start),
        "days_to_start": days_to_start,
        "days_to_deadline": days_to_deadline,
        "flow_days": flow_days,
        "buffer_days": buffer_days,
    }


def _json_maybe(value, fallback=None):
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _attach_opportunity_aux(conn, opp):
    rule = one(conn.execute("SELECT * FROM opportunity_rules WHERE opportunity_id=?", (opp["id"],)).fetchone())
    plans = rows(conn.execute(
        "SELECT * FROM opportunity_plan_items WHERE opportunity_id=? ORDER BY due_date IS NULL,due_date,id",
        (opp["id"],)).fetchall())
    opp["rules"] = rule or {}
    opp["plan_items"] = plans
    opp["radar"] = _opportunity_radar(opp, rule)
    opp["status_label"] = OPPORTUNITY_STATUS_LABEL.get(opp.get("status"), opp.get("status") or "")
    opp["batch_label"] = OPPORTUNITY_BATCH_LABEL.get(opp.get("batch_type"), opp.get("batch_type") or "")
    opp["evidence_items"] = _json_maybe(opp.get("evidence"), [])
    return opp


def list_job_opportunities(status=None, batch_type=None, q=None):
    conn = get_db()
    sql = "SELECT * FROM job_opportunities WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if batch_type:
        sql += " AND batch_type=?"
        args.append(batch_type)
    if q:
        sql += """ AND (
            company LIKE ? OR role LIKE ? OR direction LIKE ? OR company_industry LIKE ?
            OR company_type LIKE ? OR source_title LIKE ? OR jd LIKE ? OR notes LIKE ?
        )"""
        like = f"%{q}%"
        args.extend([like] * 8)
    sql += """ ORDER BY
        CASE status WHEN 'converted' THEN 8 WHEN 'dismissed' THEN 9 ELSE 0 END,
        CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
        CASE WHEN deadline_date IS NULL OR trim(deadline_date)='' THEN 1 ELSE 0 END,
        deadline_date ASC, updated_at DESC, id DESC"""
    items = rows(conn.execute(sql, args).fetchall())
    out = [_attach_opportunity_aux(conn, x) for x in items]
    conn.close()
    return out


def get_job_opportunity(oid):
    conn = get_db()
    opp = one(conn.execute("SELECT * FROM job_opportunities WHERE id=?", (oid,)).fetchone())
    if opp:
        opp = _attach_opportunity_aux(conn, opp)
    conn.close()
    return opp


def _normalize_opp(d):
    return {
        "company": d.get("company") or "",
        "role": d.get("role") or "",
        "direction": d.get("direction") or d.get("target"),
        "company_industry": d.get("company_industry") or d.get("industry") or d.get("sector"),
        "company_type": d.get("company_type") or d.get("enterprise_type"),
        "batch_type": d.get("batch_type") or "autumn",
        "status": d.get("status") or "new",
        "priority": d.get("priority") or "normal",
        "fit_score": int(d.get("fit_score") or 0),
        "source_type": d.get("source_type"),
        "source_title": d.get("source_title"),
        "source_url": d.get("source_url"),
        "apply_url": d.get("apply_url"),
        "location": d.get("location"),
        "deadline_date": d.get("deadline_date"),
        "deadline_type": d.get("deadline_type") or "hard",
        "flow_days": int(d.get("flow_days") or 4),
        "buffer_days": int(d.get("buffer_days") or 1),
        "jd": d.get("jd") or d.get("job_description"),
        "notes": d.get("notes"),
        "evidence": json.dumps(d.get("evidence"), ensure_ascii=False) if isinstance(d.get("evidence"), (list, dict)) else d.get("evidence"),
    }


def upsert_opportunity_rules_in_conn(conn, oid, d):
    existing = one(conn.execute("SELECT * FROM opportunity_rules WHERE opportunity_id=?", (oid,)).fetchone()) or {}
    vals = {
        "rule_status": d.get("rule_status") or existing.get("rule_status") or "unverified",
        "early_batch_impact": d.get("early_batch_impact") or existing.get("early_batch_impact") or "unknown",
        "locks_choice": d.get("locks_choice") or existing.get("locks_choice") or "unknown",
        "multi_apply_allowed": d.get("multi_apply_allowed") or existing.get("multi_apply_allowed") or "unknown",
        "cooldown_days": d.get("cooldown_days") if d.get("cooldown_days") is not None else existing.get("cooldown_days"),
        "rolling_review": d.get("rolling_review") or existing.get("rolling_review") or "unknown",
        "referral_required": d.get("referral_required") or existing.get("referral_required") or "unknown",
        "resume_editable": d.get("resume_editable") or existing.get("resume_editable") or "unknown",
        "assessment_trigger": d.get("assessment_trigger") or existing.get("assessment_trigger"),
        "evidence": d.get("evidence") or existing.get("evidence"),
    }
    conn.execute("""INSERT INTO opportunity_rules
        (opportunity_id,rule_status,early_batch_impact,locks_choice,multi_apply_allowed,cooldown_days,
         rolling_review,referral_required,resume_editable,assessment_trigger,evidence)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(opportunity_id) DO UPDATE SET
          rule_status=excluded.rule_status, early_batch_impact=excluded.early_batch_impact,
          locks_choice=excluded.locks_choice, multi_apply_allowed=excluded.multi_apply_allowed,
          cooldown_days=excluded.cooldown_days, rolling_review=excluded.rolling_review,
          referral_required=excluded.referral_required, resume_editable=excluded.resume_editable,
          assessment_trigger=excluded.assessment_trigger, evidence=excluded.evidence,
          updated_at=datetime('now','localtime')""",
        (oid, vals["rule_status"], vals["early_batch_impact"], vals["locks_choice"], vals["multi_apply_allowed"],
         vals["cooldown_days"], vals["rolling_review"], vals["referral_required"], vals["resume_editable"],
         vals["assessment_trigger"], vals["evidence"]))


def create_job_opportunity(d):
    conn = get_db()
    x = _normalize_opp(d)
    cur = conn.execute("""INSERT INTO job_opportunities
        (company,role,direction,company_industry,company_type,batch_type,status,priority,fit_score,source_type,source_title,source_url,
         apply_url,location,deadline_date,deadline_type,flow_days,buffer_days,jd,notes,evidence)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (x["company"], x["role"], x["direction"], x["company_industry"], x["company_type"], x["batch_type"], x["status"], x["priority"], x["fit_score"],
         x["source_type"], x["source_title"], x["source_url"], x["apply_url"], x["location"], x["deadline_date"],
         x["deadline_type"], x["flow_days"], x["buffer_days"], x["jd"], x["notes"], x["evidence"]))
    oid = cur.lastrowid
    upsert_opportunity_rules_in_conn(conn, oid, d.get("rules") or {})
    conn.commit()
    conn.close()
    rebuild_opportunity_plan(oid, force=False)
    return oid


def update_job_opportunity(oid, d):
    conn = get_db()
    current = one(conn.execute("SELECT * FROM job_opportunities WHERE id=?", (oid,)).fetchone())
    if not current:
        conn.close()
        return False
    x = {**current, **_normalize_opp(d)}
    conn.execute("""UPDATE job_opportunities SET company=?,role=?,direction=?,company_industry=?,company_type=?,batch_type=?,status=?,priority=?,
        fit_score=?,source_type=?,source_title=?,source_url=?,apply_url=?,location=?,deadline_date=?,deadline_type=?,
        flow_days=?,buffer_days=?,jd=?,notes=?,evidence=?,updated_at=datetime('now','localtime') WHERE id=?""",
        (x["company"], x["role"], x["direction"], x["company_industry"], x["company_type"], x["batch_type"], x["status"], x["priority"], x["fit_score"],
         x["source_type"], x["source_title"], x["source_url"], x["apply_url"], x["location"], x["deadline_date"],
         x["deadline_type"], x["flow_days"], x["buffer_days"], x["jd"], x["notes"], x["evidence"], oid))
    if "rules" in d:
        upsert_opportunity_rules_in_conn(conn, oid, d.get("rules") or {})
    conn.commit()
    conn.close()
    rebuild_opportunity_plan(oid, force=False)
    return True


def update_opportunity_rules(oid, d):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM job_opportunities WHERE id=?", (oid,)).fetchone():
        conn.close()
        return False
    upsert_opportunity_rules_in_conn(conn, oid, d)
    conn.commit(); conn.close()
    return True


def rebuild_opportunity_plan(oid, force=False):
    conn = get_db()
    opp = one(conn.execute("SELECT * FROM job_opportunities WHERE id=?", (oid,)).fetchone())
    if not opp:
        conn.close()
        return []
    if not force and conn.execute("SELECT 1 FROM opportunity_plan_items WHERE opportunity_id=?", (oid,)).fetchone():
        plans = rows(conn.execute("SELECT * FROM opportunity_plan_items WHERE opportunity_id=? ORDER BY due_date,id", (oid,)).fetchall())
        conn.close()
        return plans
    conn.execute("DELETE FROM opportunity_plan_items WHERE opportunity_id=?", (oid,))
    radar = _opportunity_radar(opp)
    deadline = _date_obj(opp.get("deadline_date"))
    latest = _date_obj(radar.get("latest_start_date")) or _today()

    def add(title, action_type, due, notes=""):
        conn.execute("""INSERT INTO opportunity_plan_items
            (opportunity_id,title,action_type,due_date,notes) VALUES (?,?,?,?,?)""",
            (oid, title, action_type, _date_key(due) if due else None, notes))

    add("核验提前批/志愿/重复投递规则", "verify_rules", max(_today(), latest - timedelta(days=1)),
        "规则未知时先核验，再决定是否投递。")
    add("判断岗位匹配与投递优先级", "evaluate_fit", latest,
        "结合 JD、简历和当前投递组合，避免乱投。")
    add("准备网申材料与定制简历", "prepare_materials", latest,
        f"按自然日倒排：流程 {opp.get('flow_days') or 4} 天，缓冲 {opp.get('buffer_days') or 1} 天。")
    if deadline:
        if deadline - timedelta(days=1) >= _today():
            add("预留测评/笔试/附件检查时间", "assessment_buffer", deadline - timedelta(days=1),
                "不按工作日计算，避免最后一天被测评或系统问题卡住。")
        add("最终提交网申", "submit_application", deadline, "截止日前完成，不把 deadline 当开始日。")
    plans = rows(conn.execute("SELECT * FROM opportunity_plan_items WHERE opportunity_id=? ORDER BY due_date,id", (oid,)).fetchall())
    conn.commit(); conn.close()
    return plans


def create_opportunity_plan_item(oid, d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO opportunity_plan_items
        (opportunity_id,title,action_type,due_date,status,notes,calendar_scope)
        VALUES (?,?,?,?,?,?,?)""",
        (oid, d.get("title"), d.get("action_type") or "prepare", d.get("due_date"),
         d.get("status") or "todo", d.get("notes"), d.get("calendar_scope") or "opportunity"))
    pid = cur.lastrowid
    conn.commit(); conn.close()
    return pid


def update_opportunity_plan_item(pid, d):
    conn = get_db()
    conn.execute("""UPDATE opportunity_plan_items SET title=?, action_type=?, due_date=?, status=?, notes=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("title"), d.get("action_type") or "prepare", d.get("due_date"),
         d.get("status") or "todo", d.get("notes"), pid))
    conn.commit(); conn.close()


def delete_opportunity(oid):
    conn = get_db()
    cur = conn.execute("DELETE FROM job_opportunities WHERE id=?", (oid,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def list_opportunity_radar():
    items = list_job_opportunities()
    order = {"must_start_today": 0, "risk": 1, "this_week": 2, "rules_unverified": 3,
             "open": 4, "converted": 8, "dismissed": 9, "expired": 10}
    pr = {"high": 0, "normal": 1, "low": 2}
    return sorted(items, key=lambda x: (
        order.get((x.get("radar") or {}).get("state"), 5),
        (x.get("radar") or {}).get("days_to_deadline") if (x.get("radar") or {}).get("days_to_deadline") is not None else 9999,
        pr.get(x.get("priority"), 1),
    ))


def list_opportunity_plan_schedule(date_from=None, date_to=None):
    conn = get_db()
    q = """SELECT p.*,o.company,o.role,o.batch_type,o.apply_url,o.track_id,o.priority,
                  o.deadline_date,o.id AS opportunity_id
           FROM opportunity_plan_items p JOIN job_opportunities o ON o.id=p.opportunity_id
           WHERE p.due_date IS NOT NULL AND trim(p.due_date)<>''"""
    args = []
    if date_from:
        q += " AND p.due_date>=?"
        args.append(str(date_from)[:10])
    if date_to:
        q += " AND p.due_date<?"
        args.append(str(date_to)[:10])
    q += " ORDER BY p.due_date,p.id"
    out = rows(conn.execute(q, args).fetchall())
    conn.close()
    for x in out:
        x["kind"] = "opportunity_plan"
        x["interview_date"] = (x.get("due_date") or "") + " 09:00"
        x["round_type"] = x.get("title")
        x["application_status"] = "opportunity"
    return out


def convert_opportunity_to_track(oid, create_application_record=False):
    opp = get_job_opportunity(oid)
    if not opp:
        return None
    job_type = {
        "early": "autumn_early",
        "autumn": "autumn",
        "supplement": "autumn",
        "spring": "spring",
        "intern": "daily",
    }.get(opp.get("batch_type"), "unknown")
    notes = "\n".join(x for x in [
        "由秋招机会库转入工作台",
        f"批次：{OPPORTUNITY_BATCH_LABEL.get(opp.get('batch_type'), opp.get('batch_type') or '待确认')}",
        f"截止：{opp.get('deadline_date') or '待确认'}",
        f"规则：{(opp.get('rules') or {}).get('rule_status') or 'unverified'}",
        opp.get("notes") or "",
    ] if x)
    tid = create_job_track({
        "company": opp.get("company"),
        "role": opp.get("role"),
        "target": opp.get("direction"),
        "jd": opp.get("jd"),
        "status": "active",
        "notes": notes,
        "track_group": opp.get("direction") or "秋招机会",
        "priority": opp.get("priority") or "normal",
        "apply_url": opp.get("apply_url"),
        "job_type": job_type,
        "company_type": opp.get("company_type"),
        "company_industry": opp.get("company_industry"),
    })
    aid = None
    if create_application_record:
        aid = create_application({
            "track_id": tid,
            "company": opp.get("company"),
            "role": opp.get("role"),
            "industry": opp.get("direction"),
            "status": "applied",
            "source": "秋招机会库",
            "apply_url": opp.get("apply_url"),
            "job_type": job_type,
            "notes": notes,
        })
    conn = get_db()
    conn.execute("""UPDATE job_opportunities SET status='converted',track_id=?,application_id=?,
        updated_at=datetime('now','localtime') WHERE id=?""", (tid, aid, oid))
    conn.execute("""INSERT INTO opportunity_plan_items
        (opportunity_id,title,action_type,due_date,status,notes)
        VALUES (?,?,?,?,?,?)""",
        (oid, "已转入求职工作台", "convert_track", _date_key(_today()), "done", f"求职线 #{tid}"))
    conn.commit(); conn.close()
    return {"opportunity": get_job_opportunity(oid), "track": get_job_track(tid), "track_id": tid, "application_id": aid}


def list_job_tracks():
    ensure_application_tracks()
    conn = get_db()
    r = conn.execute("""
        SELECT jt.*,a.id AS application_id,a.status AS application_status,
               a.updated_at AS application_updated_at,
               a.applied_date,a.source AS application_source,
               a.remark_tag AS application_remark_tag,
               a.evaluation AS application_evaluation,
               (SELECT MIN(i.interview_date) FROM interviews i
                 WHERE i.application_id=a.id
                   AND i.interview_date>=datetime('now','localtime')) AS next_interview_at
          FROM job_tracks jt
          LEFT JOIN applications a ON a.id=(
            SELECT a2.id FROM applications a2
             WHERE a2.track_id=jt.id ORDER BY a2.id DESC LIMIT 1
          )
         ORDER BY
           CASE COALESCE(a.status,'applied')
             WHEN 'interview' THEN 0 WHEN 'written' THEN 1
             WHEN 'screening' THEN 2 WHEN 'applied' THEN 3
             WHEN 'offer' THEN 4 ELSE 5 END,
           COALESCE(next_interview_at,jt.updated_at) DESC,jt.id DESC
    """).fetchall()
    conn.close()
    return rows(r)


def get_job_track(tid):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM job_tracks WHERE id=?", (tid,)).fetchone())
    conn.close()
    return x


def create_job_track(d):
    conn = get_db()
    cls = classify_company(d.get("company"), d.get("role"))
    cur = conn.execute("""INSERT INTO job_tracks
        (company,role,target,jd,status,notes,track_group,priority,apply_url,company_type,company_industry,job_type)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("company"), d.get("role"), d.get("target"), d.get("jd"),
         d.get("status") or "active", d.get("notes"), d.get("track_group"),
         d.get("priority") or "normal", d.get("apply_url"),
         d.get("company_type") or cls["company_type"],
         d.get("company_industry") or cls["company_industry"],
         d.get("job_type") or infer_job_type(d.get("role"), d.get("notes"))))
    jid = cur.lastrowid
    conn.commit(); conn.close()
    return jid


def update_job_track(tid, d):
    conn = get_db()
    current = one(conn.execute("SELECT company_type,company_industry,job_type FROM job_tracks WHERE id=?", (tid,)).fetchone()) or {}
    cls = classify_company(d.get("company"), d.get("role"))
    conn.execute("""UPDATE job_tracks SET company=?, role=?, target=?, jd=?, status=?,
        notes=?, persona=?, priority=?, track_group=?, apply_url=?, company_type=?, company_industry=?, job_type=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("company"), d.get("role"), d.get("target"), d.get("jd"),
         d.get("status") or "active", d.get("notes"), d.get("persona"),
         d.get("priority") or "normal", d.get("track_group"), d.get("apply_url"),
         d.get("company_type") or current.get("company_type") or cls["company_type"],
         d.get("company_industry") or current.get("company_industry") or cls["company_industry"],
         d.get("job_type") or current.get("job_type") or infer_job_type(d.get("role"), d.get("notes")), tid))
    conn.commit(); conn.close()


def classify_job_track(tid):
    conn = get_db()
    t = one(conn.execute("SELECT company,role FROM job_tracks WHERE id=?", (tid,)).fetchone())
    if not t:
        conn.close()
        return None
    cls = classify_company(t.get("company"), t.get("role"))
    conn.execute("""UPDATE job_tracks SET company_type=?, company_industry=?, job_type=COALESCE(NULLIF(job_type,''),?),
        updated_at=datetime('now','localtime') WHERE id=?""",
        (cls["company_type"], cls["company_industry"], infer_job_type(t.get("role")), tid))
    conn.commit()
    out = one(conn.execute("SELECT * FROM job_tracks WHERE id=?", (tid,)).fetchone())
    conn.close()
    return out


def classify_all_job_tracks():
    conn = get_db()
    tracks = rows(conn.execute("SELECT id,company,role FROM job_tracks").fetchall())
    for t in tracks:
        cls = classify_company(t.get("company"), t.get("role"))
        conn.execute("""UPDATE job_tracks SET company_type=?, company_industry=?, job_type=COALESCE(NULLIF(job_type,''),?),
            updated_at=datetime('now','localtime') WHERE id=?""",
            (cls["company_type"], cls["company_industry"], infer_job_type(t.get("role")), t["id"]))
    conn.commit(); conn.close()
    return len(tracks)


def classify_application(aid):
    conn = get_db()
    a = one(conn.execute("SELECT id,company,role,track_id FROM applications WHERE id=?", (aid,)).fetchone())
    if not a:
        conn.close()
        return None
    cls = classify_company(a.get("company"), a.get("role"))
    jt = infer_job_type(a.get("role"))
    conn.execute("""UPDATE applications SET company_type=?, company_industry=?, job_type=COALESCE(NULLIF(job_type,''),?),
        updated_at=datetime('now','localtime') WHERE id=?""",
        (cls["company_type"], cls["company_industry"], jt, aid))
    if a.get("track_id"):
        conn.execute("""UPDATE job_tracks SET company_type=?, company_industry=?, job_type=COALESCE(NULLIF(job_type,''),?),
            updated_at=datetime('now','localtime') WHERE id=?""",
            (cls["company_type"], cls["company_industry"], jt, a["track_id"]))
    conn.commit()
    out = one(conn.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone())
    conn.close()
    return out


def delete_job_track(tid):
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM job_tracks WHERE id=?", (tid,)).fetchone()
    if not exists:
        conn.close()
        return False
    conn.execute("DELETE FROM track_gaps WHERE track_id=?", (tid,))
    # A row in the unified job workspace represents both the job track and its
    # linked application history. Leaving the application orphaned makes
    # ensure_application_tracks() recreate the deleted row on the next read.
    conn.execute("DELETE FROM applications WHERE track_id=?", (tid,))
    conn.execute("DELETE FROM job_tracks WHERE id=?", (tid,))
    _delete_search_entities(conn, "job_track", [tid])
    conn.commit(); conn.close()
    return True


# ─── 求职目标：差距清单 ───────────────────────────────────────────────────────

def list_track_gaps(track_id):
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM track_gaps WHERE track_id=? ORDER BY "
        "CASE severity WHEN 'blocker' THEN 0 WHEN 'fixable' THEN 1 ELSE 2 END, id", (track_id,)).fetchall()
    conn.close()
    return rows(r)


def get_track_gap(gid):
    conn = get_db()
    row = one(conn.execute("SELECT * FROM track_gaps WHERE id=?", (gid,)).fetchone())
    conn.close()
    return row


def create_track_gap(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO track_gaps
        (track_id,dimension,requirement,my_status,severity,plan_type,plan_ref_type,plan_ref_id,status,note,
         excluded_from_score,exclusion_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("track_id"), d.get("dimension"), d.get("requirement"),
         d.get("my_status") or "missing", d.get("severity") or "fixable",
         d.get("plan_type") or "none", d.get("plan_ref_type"), d.get("plan_ref_id"),
         d.get("status") or "todo", d.get("note"),
         1 if d.get("excluded_from_score") else 0, d.get("exclusion_reason")))
    gid = cur.lastrowid
    conn.commit(); conn.close()
    _recompute_readiness(d.get("track_id"))
    return gid


def update_track_gap(gid, d):
    conn = get_db()
    row = one(conn.execute("SELECT track_id FROM track_gaps WHERE id=?", (gid,)).fetchone())
    fields = ["dimension", "requirement", "my_status", "severity", "plan_type",
              "plan_ref_type", "plan_ref_id", "status", "note",
              "excluded_from_score", "exclusion_reason"]
    sets = ", ".join(f"{f}=?" for f in fields if f in d)
    if sets:
        args = [d[f] for f in fields if f in d] + [gid]
        conn.execute(f"UPDATE track_gaps SET {sets}, updated_at=datetime('now','localtime') WHERE id=?", args)
        conn.commit()
    conn.close()
    if row:
        _recompute_readiness(row["track_id"])


def delete_track_gap(gid):
    conn = get_db()
    row = one(conn.execute("SELECT track_id FROM track_gaps WHERE id=?", (gid,)).fetchone())
    conn.execute("DELETE FROM track_gaps WHERE id=?", (gid,))
    conn.commit(); conn.close()
    if row:
        _recompute_readiness(row["track_id"])


def _recompute_readiness(track_id):
    if not track_id:
        return
    conn = get_db()
    rows_ = conn.execute(
        "SELECT status FROM track_gaps "
        "WHERE track_id=? AND COALESCE(excluded_from_score, 0)=0",
        (track_id,),
    ).fetchall()
    total = len(rows_)
    done = sum(1 for r in rows_ if r["status"] == "done")
    pct = round(done * 100 / total) if total else 0
    conn.execute("UPDATE job_tracks SET readiness=?, updated_at=datetime('now','localtime') WHERE id=?", (pct, track_id))
    conn.commit(); conn.close()


# ─── 统一生成资产 ─────────────────────────────────────────────────────────────

def _next_asset_version(conn, asset_type, track_id=None, project_id=None):
    q = "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM assets WHERE asset_type=?"
    args = [asset_type]
    if track_id is None:
        q += " AND track_id IS NULL"
    else:
        q += " AND track_id=?"
        args.append(track_id)
    if project_id is None:
        q += " AND project_id IS NULL"
    else:
        q += " AND project_id=?"
        args.append(project_id)
    return conn.execute(q, args).fetchone()["v"]


def create_asset(d):
    conn = get_db()
    asset_type = d.get("asset_type") or "other"
    track_id = d.get("track_id")
    project_id = d.get("project_id")
    version = d.get("version") or _next_asset_version(conn, asset_type, track_id, project_id)
    cur = conn.execute("""INSERT INTO assets
        (asset_type,track_id,project_id,title,body,provenance_json,version,status,derived_from_json)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (asset_type, track_id, project_id, d.get("title"), d.get("body"),
         d.get("provenance_json"), version, d.get("status") or "draft",
         d.get("derived_from_json")))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    return aid


def get_asset(aid):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone())
    conn.close()
    return x


def list_assets(asset_type=None, track_id=None, project_id=None, limit=200):
    conn = get_db()
    q = "SELECT * FROM assets WHERE 1=1"
    args = []
    if asset_type:
        q += " AND asset_type=?"
        args.append(asset_type)
    if track_id is not None:
        q += " AND track_id=?"
        args.append(track_id)
    if project_id is not None:
        q += " AND project_id=?"
        args.append(project_id)
    q += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    args.append(limit)
    r = conn.execute(q, args).fetchall()
    conn.close()
    return rows(r)


def update_asset(aid, d):
    conn = get_db()
    conn.execute("""UPDATE assets SET title=?,body=?,status=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("title"), d.get("body"), d.get("status") or "draft", aid))
    conn.commit(); conn.close()


def mark_asset_stale(aid):
    conn = get_db()
    cur = conn.execute("""UPDATE assets SET status='stale',updated_at=datetime('now','localtime')
        WHERE id=? AND status!='stale'""", (aid,))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed


def delete_asset(aid):
    conn = get_db()
    conn.execute("DELETE FROM assets WHERE id=?", (aid,))
    conn.commit(); conn.close()


# ─── 岗位专属本地简历版本 ────────────────────────────────────────────────────

def list_resume_versions(track_id):
    conn = get_db()
    r = conn.execute("""SELECT * FROM resume_versions WHERE track_id=?
        ORDER BY CASE status WHEN 'editing' THEN 0 WHEN 'submitted' THEN 1 ELSE 2 END,
                 COALESCE(submitted_at,updated_at) DESC,id DESC""", (track_id,)).fetchall()
    conn.close()
    return rows(r)


def list_recent_resume_versions(limit=8):
    """Return recent resume text across tracks for Agent context fallback."""
    conn = get_db()
    r = conn.execute("""SELECT rv.*,jt.company AS track_company,jt.role AS track_role
        FROM resume_versions rv
        LEFT JOIN job_tracks jt ON jt.id=rv.track_id
        ORDER BY CASE rv.status WHEN 'submitted' THEN 0 WHEN 'editing' THEN 1 ELSE 2 END,
                 COALESCE(rv.submitted_at,rv.updated_at) DESC,rv.id DESC
        LIMIT ?""", (limit,)).fetchall()
    conn.close()
    return rows(r)


def get_resume_version(rid):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM resume_versions WHERE id=?", (rid,)).fetchone())
    conn.close()
    return x


def create_resume_version(d):
    conn = get_db()
    submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if d.get("status") == "submitted" else None
    cur = conn.execute("""INSERT INTO resume_versions
        (track_id,version_name,docx_path,pdf_path,extracted_text,status,change_summary,
         docx_hash,pdf_hash,docx_modified_at,pdf_modified_at,submitted_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("track_id"), d.get("version_name"), d.get("docx_path"), d.get("pdf_path"),
         d.get("extracted_text"), d.get("status") or "editing", d.get("change_summary"),
         d.get("docx_hash"), d.get("pdf_hash"), d.get("docx_modified_at"),
         d.get("pdf_modified_at"), submitted_at))
    rid = cur.lastrowid
    conn.commit(); conn.close()
    return rid


def update_resume_version(rid, d):
    conn = get_db()
    current = one(conn.execute("SELECT * FROM resume_versions WHERE id=?", (rid,)).fetchone()) or {}
    status = d.get("status") or current.get("status") or "editing"
    submitted_at = current.get("submitted_at")
    if status == "submitted" and not submitted_at:
        submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""UPDATE resume_versions SET version_name=?,docx_path=?,pdf_path=?,
        extracted_text=?,status=?,change_summary=?,docx_hash=?,pdf_hash=?,
        docx_modified_at=?,pdf_modified_at=?,submitted_at=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("version_name"), d.get("docx_path"), d.get("pdf_path"), d.get("extracted_text"),
         status, d.get("change_summary"), d.get("docx_hash"), d.get("pdf_hash"),
         d.get("docx_modified_at"), d.get("pdf_modified_at"), submitted_at, rid))
    conn.commit(); conn.close()


def delete_resume_version(rid):
    conn = get_db()
    conn.execute("DELETE FROM resume_versions WHERE id=?", (rid,))
    conn.commit(); conn.close()


# ─── 岗位投递包 ──────────────────────────────────────────────────────────────

def list_submission_materials(track_id):
    conn = get_db()
    items = rows(conn.execute(
        """SELECT * FROM submission_materials
        WHERE track_id=?
        ORDER BY CASE material_type
          WHEN 'application_text' THEN 0 WHEN 'attachment' THEN 1
          WHEN 'email' THEN 2 WHEN 'proof' THEN 3 ELSE 4 END,
          updated_at DESC,id DESC""",
        (track_id,),
    ).fetchall())
    conn.close()
    return items


def get_submission_material(mid):
    conn = get_db()
    item = one(conn.execute(
        "SELECT * FROM submission_materials WHERE id=?", (mid,)
    ).fetchone())
    conn.close()
    return item


def create_submission_material(d):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO submission_materials
        (track_id,application_id,material_type,title,content,file_path,external_ref,frozen)
        VALUES (?,?,?,?,?,?,?,?)""",
        (
            d.get("track_id"), d.get("application_id"), d.get("material_type") or "application_text",
            d.get("title"), d.get("content"), d.get("file_path"), d.get("external_ref"),
            1 if d.get("frozen", True) else 0,
        ),
    )
    mid = cur.lastrowid
    conn.commit(); conn.close()
    return mid


def update_submission_material(mid, d):
    current = get_submission_material(mid)
    if not current:
        return False
    merged = {**current, **d}
    conn = get_db()
    conn.execute(
        """UPDATE submission_materials SET application_id=?,material_type=?,title=?,
        content=?,file_path=?,external_ref=?,frozen=?,updated_at=datetime('now','localtime')
        WHERE id=?""",
        (
            merged.get("application_id"), merged.get("material_type"), merged.get("title"),
            merged.get("content"), merged.get("file_path"), merged.get("external_ref"),
            1 if merged.get("frozen") else 0, mid,
        ),
    )
    conn.commit(); conn.close()
    return True


def delete_submission_material(mid):
    conn = get_db()
    cur = conn.execute("DELETE FROM submission_materials WHERE id=?", (mid,))
    conn.commit(); conn.close()
    return cur.rowcount > 0


# ─── 公司网申材料库 ────────────────────────────────────────────────────────

def list_company_submission_materials(company):
    conn = get_db()
    items = rows(conn.execute(
        """SELECT m.*,
          (SELECT COUNT(*) FROM company_submission_usages u WHERE u.material_id=m.id) AS usage_count
        FROM company_submission_materials m
        WHERE trim(m.company)=trim(?) AND m.status='active'
        ORDER BY m.updated_at DESC,m.id DESC""", (company,)
    ).fetchall())
    for item in items:
        item['versions'] = rows(conn.execute(
            "SELECT * FROM company_submission_material_versions WHERE material_id=? ORDER BY version DESC",
            (item['id'],),
        ).fetchall())
        item['usages'] = rows(conn.execute(
            """SELECT u.*,t.role,a.applied_date FROM company_submission_usages u
            LEFT JOIN job_tracks t ON t.id=u.track_id
            LEFT JOIN applications a ON a.id=u.application_id
            WHERE u.material_id=? ORDER BY u.used_at DESC,u.id DESC""", (item['id'],)
        ).fetchall())
    conn.close()
    return items


def get_company_submission_material(mid):
    conn = get_db(); item = one(conn.execute(
        "SELECT * FROM company_submission_materials WHERE id=? AND status='active'", (mid,)
    ).fetchone()); conn.close(); return item


def create_company_submission_material(d):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO company_submission_materials
        (company,material_type,title,content,file_path,external_ref)
        VALUES (?,?,?,?,?,?)""",
        (d.get('company'), d.get('material_type') or 'application_text', d.get('title'),
         d.get('content'), d.get('file_path'), d.get('external_ref')),
    )
    mid = cur.lastrowid
    conn.execute(
        """INSERT INTO company_submission_material_versions
        (material_id,version,title,content,file_path,external_ref,change_summary)
        VALUES (?,1,?,?,?,?,?)""",
        (mid, d.get('title'), d.get('content'), d.get('file_path'), d.get('external_ref'),
         d.get('change_summary') or '建立公司网申母版'),
    )
    conn.commit(); conn.close(); return mid


def update_company_submission_material(mid, d):
    current = get_company_submission_material(mid)
    if not current: return None
    merged = {**current, **d}; version = int(current.get('current_version') or 1) + 1
    conn = get_db()
    conn.execute(
        """UPDATE company_submission_materials SET material_type=?,title=?,content=?,file_path=?,
        external_ref=?,current_version=?,updated_at=datetime('now','localtime') WHERE id=?""",
        (merged.get('material_type'), merged.get('title'), merged.get('content'),
         merged.get('file_path'), merged.get('external_ref'), version, mid),
    )
    conn.execute(
        """INSERT INTO company_submission_material_versions
        (material_id,version,title,content,file_path,external_ref,change_summary)
        VALUES (?,?,?,?,?,?,?)""",
        (mid, version, merged.get('title'), merged.get('content'), merged.get('file_path'),
         merged.get('external_ref'), d.get('change_summary') or '更新公司网申母版'),
    )
    conn.commit(); conn.close(); return get_company_submission_material(mid)


def archive_company_submission_material(mid):
    conn = get_db(); cur = conn.execute(
        "UPDATE company_submission_materials SET status='archived',updated_at=datetime('now','localtime') WHERE id=?",
        (mid,),
    ); conn.commit(); conn.close(); return cur.rowcount > 0


def create_company_submission_usage(mid, d):
    material = get_company_submission_material(mid)
    if not material: return None
    conn = get_db(); cur = conn.execute(
        """INSERT INTO company_submission_usages
        (material_id,material_version,track_id,application_id,usage_title,submitted_file_path,submitted_ref,notes,used_at)
        VALUES (?,?,?,?,?,?,?,?,COALESCE(?,datetime('now','localtime')))""",
        (mid, material['current_version'], d.get('track_id'), d.get('application_id'),
         d.get('usage_title'), d.get('submitted_file_path'), d.get('submitted_ref'),
         d.get('notes'), d.get('used_at')),
    ); uid = cur.lastrowid; conn.commit()
    item = one(conn.execute("SELECT * FROM company_submission_usages WHERE id=?", (uid,)).fetchone())
    conn.close(); return item


def create_company_application_capture(d):
    structure = d.get('structure') or []
    conn = get_db(); cur = conn.execute(
        """INSERT INTO company_application_captures
        (company,track_id,title,page_url,portal_host,structure_json,field_count)
        VALUES (?,?,?,?,?,?,?)""",
        (d.get('company'), d.get('track_id'), d.get('title'), d.get('page_url'),
         d.get('portal_host'), json.dumps(structure, ensure_ascii=False),
         sum(len(section.get('fields') or []) for section in structure)),
    ); cid = cur.lastrowid; conn.commit(); conn.close(); return cid


def list_company_application_captures(company):
    conn = get_db(); items = rows(conn.execute(
        """SELECT c.*,t.role FROM company_application_captures c
        LEFT JOIN job_tracks t ON t.id=c.track_id
        WHERE trim(c.company)=trim(?) ORDER BY c.captured_at DESC,c.id DESC""", (company,)
    ).fetchall()); conn.close()
    for item in items:
        try: item['structure'] = json.loads(item.pop('structure_json') or '[]')
        except Exception: item['structure'] = []
    return items


def list_all_company_application_captures():
    conn = get_db(); items = rows(conn.execute(
        """SELECT c.*,t.role,t.company_type,t.company_industry FROM company_application_captures c
        LEFT JOIN job_tracks t ON t.id=c.track_id
        ORDER BY c.captured_at DESC,c.id DESC"""
    ).fetchall()); conn.close()
    for item in items:
        try: item['structure'] = json.loads(item.pop('structure_json') or '[]')
        except Exception: item['structure'] = []
    return items


def list_company_submission_material_companies():
    conn = get_db(); names = [x['company'] for x in rows(conn.execute(
        "SELECT DISTINCT company FROM company_submission_materials WHERE status='active' ORDER BY company"
    ).fetchall())]; conn.close(); return names


def get_company_application_capture(cid):
    conn = get_db(); item = one(conn.execute(
        """SELECT c.*,t.role,t.company_type,t.company_industry FROM company_application_captures c
        LEFT JOIN job_tracks t ON t.id=c.track_id WHERE c.id=?""", (cid,)
    ).fetchone()); conn.close()
    if item:
        try: item['structure'] = json.loads(item.pop('structure_json') or '[]')
        except Exception: item['structure'] = []
    return item


def update_company_application_capture(cid, d):
    current = get_company_application_capture(cid)
    if not current: return None
    structure = d.get('structure', current.get('structure') or [])
    conn = get_db(); conn.execute(
        """UPDATE company_application_captures SET title=?,track_id=?,structure_json=?,field_count=?
        WHERE id=?""",
        (d.get('title', current.get('title')), d.get('track_id', current.get('track_id')),
         json.dumps(structure, ensure_ascii=False),
         sum(len(section.get('fields') or []) for section in structure), cid),
    ); conn.commit(); conn.close(); return get_company_application_capture(cid)


def delete_company_application_capture(cid):
    conn = get_db(); cur = conn.execute("DELETE FROM company_application_captures WHERE id=?", (cid,))
    conn.commit(); conn.close(); return cur.rowcount > 0


def company_application_module_catalog(company):
    """Compare captured modules without forcing different ATS forms into one schema."""
    conn = get_db(); items = rows(conn.execute(
        """SELECT c.id,c.company,c.structure_json,t.company_type,t.company_industry
        FROM company_application_captures c LEFT JOIN job_tracks t ON t.id=c.track_id"""
    ).fetchall())
    profiles = rows(conn.execute(
        """SELECT company,group_concat(DISTINCT company_type) company_types,
        group_concat(DISTINCT company_industry) company_industries
        FROM job_tracks WHERE trim(COALESCE(company,''))<>'' GROUP BY trim(company)"""
    ).fetchall()); conn.close()
    profile_map = {(x.get('company') or '').strip(): x for x in profiles}
    flattened = []
    for item in items:
        try: sections = json.loads(item.get('structure_json') or '[]')
        except Exception: sections = []
        profile = profile_map.get((item.get('company') or '').strip(), {})
        industry_text = f"{item.get('company') or ''} {item.get('company_type') or ''} {item.get('company_industry') or ''} {profile.get('company_types') or ''} {profile.get('company_industries') or ''}"
        if '银行' in industry_text: sector = '银行'
        elif any(x in industry_text.lower() for x in ('finance','financial','证券','基金','保险','资管','投行','券商')): sector = '金融'
        elif any(x in industry_text.lower() for x in ('internet','互联网','科技','ai','人工智能')): sector = '互联网/科技'
        else: sector = '其他'
        for section in sections:
            title = re.sub(r'[\s　]+', '', str(section.get('title') or '未分组')).lower()
            semantic_keys = sorted({str(f.get('semantic_key') or 'other') for f in section.get('fields') or []})
            key = title or '|'.join(semantic_keys)
            flattened.append({'capture_id': item['id'], 'company': item.get('company'), 'sector': sector,
                              'key': key, 'title': section.get('title') or '未分组',
                              'field_count': len(section.get('fields') or []),
                              'archived': bool(section.get('archived'))})
    company_modules = [x for x in flattened if (x.get('company') or '').strip() == company.strip()]
    counts = {}
    for x in company_modules:
        counts.setdefault(x['key'], set()).add(x['capture_id'])
    for x in company_modules:
        x['scope'] = 'company_common' if len(counts[x['key']]) >= 2 else 'capture_unique'
        x['occurrences'] = len(counts[x['key']])
    sector_stats = {}
    for x in flattened:
        bucket = sector_stats.setdefault(x['sector'], {})
        stat = bucket.setdefault(x['key'], {'title': x['title'], 'companies': set(), 'captures': set(), 'field_count': 0})
        stat['companies'].add(x['company']); stat['captures'].add(x['capture_id']); stat['field_count'] = max(stat['field_count'], x['field_count'])
    sectors = []
    for sector, modules in sector_stats.items():
        sectors.append({'sector': sector, 'modules': sorted([
            {'title': v['title'], 'company_count': len(v['companies']), 'capture_count': len(v['captures']), 'field_count': v['field_count']}
            for v in modules.values()
        ], key=lambda x: (-x['company_count'], x['title']))})
    return {'company_modules': company_modules, 'sectors': sorted(sectors, key=lambda x: x['sector'])}


def set_application_resume_version(application_id, resume_version_id):
    conn = get_db()
    current = one(conn.execute(
        "SELECT track_id FROM applications WHERE id=?", (application_id,)
    ).fetchone())
    resume = one(conn.execute(
        "SELECT track_id FROM resume_versions WHERE id=?", (resume_version_id,)
    ).fetchone()) if resume_version_id else None
    if not current or (resume_version_id and (not resume or resume["track_id"] != current["track_id"])):
        conn.close()
        return False
    conn.execute(
        "UPDATE applications SET resume_version_id=?,updated_at=datetime('now','localtime') WHERE id=?",
        (resume_version_id, application_id),
    )
    conn.commit(); conn.close()
    return True


# ─── 分层知识库 ──────────────────────────────────────────────────────────────

KNOWLEDGE_DOMAIN_TERMS = (
    "基金", "固收", "债券", "权益", "量化", "资管", "保险", "证券", "投行", "银行",
    "财务", "估值", "互联网产品", "电商", "运营", "算法", "人工智能", "大模型", "智能体", "数据分析",
)


def knowledge_domain_keys(track):
    values = [track.get("company_industry"), track.get("track_group"), track.get("target")]
    domains = []
    for value in values:
        value = (value or "").strip()
        if value and value not in domains:
            domains.append(value)
        for part in re.split(r"[/、,，|]", value):
            part = part.strip()
            if part and part not in domains:
                domains.append(part)
    haystack = " ".join(str(track.get(k) or "") for k in ("company", "role", "target", "company_industry", "jd"))
    for term in KNOWLEDGE_DOMAIN_TERMS:
        if term in haystack and term not in domains:
            domains.append(term)
    return domains

def list_knowledge_items(track_id=None, scope_type=None, query=None, include_archived=False,
                         company=None, folder_id=None):
    conn = get_db()
    q = "SELECT * FROM knowledge_items WHERE 1=1"
    args = []
    if not include_archived:
        q += " AND status='active'"
    if scope_type:
        q += " AND scope_type=?"
        args.append(scope_type)
    if company:
        q += " AND scope_type='company' AND trim(company)=?"
        args.append(company.strip())
    if folder_id == "unfiled":
        q += " AND folder_id IS NULL"
    elif folder_id is not None:
        q += " AND folder_id=?"
        args.append(int(folder_id))
    if track_id is not None:
        track = one(conn.execute("SELECT * FROM job_tracks WHERE id=?", (track_id,)).fetchone()) or {}
        domains = knowledge_domain_keys(track)
        # A job workspace must not inherit every personal note. Global notes stay
        # discoverable in the knowledge space and hybrid search, but only
        # domain/company/track evidence is mounted into a concrete job dossier.
        inherit = []
        if domains:
            inherit.append("(scope_type='domain' AND domain_key IN (%s))" % ",".join("?" * len(domains)))
            args.extend(domains)
        company = (track.get("company") or "").strip()
        if company:
            inherit.append("(scope_type='company' AND trim(company)=?)")
            args.append(company)
        inherit.append("(scope_type='track' AND track_id=?)")
        args.append(track_id)
        q += " AND (" + " OR ".join(inherit) + ")"
    if query:
        like = f"%{query.strip()}%"
        q += " AND (title LIKE ? OR content LIKE ? OR topic LIKE ?)"
        args.extend([like, like, like])
    q += " ORDER BY CASE scope_type WHEN 'track' THEN 0 WHEN 'company' THEN 1 WHEN 'domain' THEN 2 ELSE 3 END, updated_at DESC,id DESC"
    result = rows(conn.execute(q, args).fetchall())
    conn.close()
    return result


def list_assets_for_agent_task(task_id, limit=100):
    """Return draft assets whose provenance explicitly points to one Agent task.

    SQLite JSON1 is not guaranteed on every local Python build, so this keeps the
    query portable and parses the small local asset set in Python.
    """
    candidates = list_assets(limit=max(200, limit * 4))
    matched = []
    for item in candidates:
        try:
            provenance = json.loads(item.get("provenance_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            provenance = {}
        if str(provenance.get("task_id") or "") == str(task_id):
            matched.append(item)
        if len(matched) >= limit:
            break
    return matched


def get_knowledge_item(kid):
    conn = get_db()
    item = one(conn.execute("SELECT * FROM knowledge_items WHERE id=?", (kid,)).fetchone())
    conn.close()
    return item


def _validate_knowledge_item_folder(conn, d):
    """Validate folder ownership at the persistence boundary.

    API schemas catch most mistakes, but external Agents and maintenance scripts
    can call this data-layer function directly. SQLite will otherwise accept a
    string in an INTEGER column, creating a document that no folder view can
    render. Keep the invariant here so every write path gets the same guard.
    """
    folder_id = d.get("folder_id")
    if folder_id in (None, ""):
        return None
    try:
        folder_id = int(folder_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("知识目录必须使用 Caddie 返回的数字 ID，不能使用目录名称或逻辑键") from exc
    folder = one(conn.execute("SELECT * FROM knowledge_folders WHERE id=?", (folder_id,)).fetchone())
    if not folder:
        raise ValueError("知识目录不存在")
    scope_type = d.get("scope_type") or "global"
    track_id = d.get("track_id")
    if folder.get("scope_type") != scope_type:
        raise ValueError("知识目录作用域与文档作用域不一致")
    if scope_type == "track" and folder.get("track_id") != track_id:
        raise ValueError("知识目录不属于当前岗位")
    if scope_type == "company" and (folder.get("company") or "").strip() != (d.get("company") or "").strip():
        raise ValueError("知识目录不属于当前公司")
    if scope_type == "domain" and (folder.get("domain_key") or "").strip() != (d.get("domain_key") or "").strip():
        raise ValueError("知识目录不属于当前领域")
    return folder_id


def create_knowledge_item(d):
    conn = get_db()
    try:
        folder_id = _validate_knowledge_item_folder(conn, d)
        cur = conn.execute("""INSERT INTO knowledge_items
            (title,content,scope_type,domain_key,company,track_id,topic,mastery,source_type,source_ref_id,folder_id,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d.get("title"), d.get("content"), d.get("scope_type") or "global",
             d.get("domain_key"), d.get("company"), d.get("track_id"), d.get("topic"),
             d.get("mastery") or "learning", d.get("source_type") or "manual",
             d.get("source_ref_id"), folder_id, d.get("status") or "active"))
        kid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return kid


def update_knowledge_item(kid, d):
    conn = get_db()
    try:
        folder_id = _validate_knowledge_item_folder(conn, d)
        conn.execute("""UPDATE knowledge_items SET title=?,content=?,scope_type=?,domain_key=?,
            company=?,track_id=?,topic=?,mastery=?,folder_id=?,status=?,updated_at=datetime('now','localtime')
            WHERE id=?""",
            (d.get("title"), d.get("content"), d.get("scope_type") or "global",
             d.get("domain_key"), d.get("company"), d.get("track_id"), d.get("topic"),
             d.get("mastery") or "learning", folder_id, d.get("status") or "active", kid))
        conn.commit()
    finally:
        conn.close()


def delete_knowledge_item(kid):
    conn = get_db()
    cur = conn.execute("DELETE FROM knowledge_items WHERE id=?", (kid,))
    _delete_search_entities(conn, "knowledge_item", [kid])
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


DEFAULT_TRACK_KNOWLEDGE_FOLDERS = [
    ("company", "公司研究", 10),
    ("role", "岗位理解", 20),
    ("professional", "专业知识", 30),
    ("interview", "面试准备", 40),
    ("review", "面试复盘", 50),
]

DEFAULT_COMPANY_KNOWLEDGE_FOLDERS = [
    ("overview", "公司概况", 10),
    ("organization", "部门与组织", 20),
    ("business", "业务与产品", 30),
    ("applications", "投递记录", 40),
    ("rules", "投递规则", 50),
]


def list_knowledge_folders(track_id=None, company=None, scope_type=None):
    conn = get_db()
    q = "SELECT * FROM knowledge_folders WHERE 1=1"
    args = []
    if track_id is not None:
        q += " AND track_id=?"
        args.append(track_id)
    if company:
        q += " AND trim(company)=?"
        args.append(company.strip())
    if scope_type:
        q += " AND scope_type=?"
        args.append(scope_type)
    q += " ORDER BY sort_order,id"
    result = rows(conn.execute(q, args).fetchall())
    conn.close()
    return result


def get_knowledge_folder(fid):
    conn = get_db()
    item = one(conn.execute("SELECT * FROM knowledge_folders WHERE id=?", (fid,)).fetchone())
    conn.close()
    return item


def create_knowledge_folder(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO knowledge_folders
        (name,parent_id,scope_type,domain_key,company,track_id,sort_order)
        VALUES (?,?,?,?,?,?,?)""",
        (d.get("name"), d.get("parent_id"), d.get("scope_type") or "global",
         d.get("domain_key"), d.get("company"), d.get("track_id"),
         d.get("sort_order") or 0))
    fid = cur.lastrowid
    conn.commit(); conn.close()
    return fid


def ensure_company_knowledge_folders(company):
    company = (company or "").strip()
    if not company:
        return [], []
    existing = list_knowledge_folders(company=company, scope_type="company")
    by_name = {item["name"]: item for item in existing}
    created = []
    for _key, name, sort_order in DEFAULT_COMPANY_KNOWLEDGE_FOLDERS:
        if name in by_name:
            continue
        fid = create_knowledge_folder({
            "name": name,
            "scope_type": "company",
            "company": company,
            "sort_order": sort_order,
        })
        created.append(get_knowledge_folder(fid))
    return list_knowledge_folders(company=company, scope_type="company"), created


def update_knowledge_folder(fid, d):
    conn = get_db()
    conn.execute("""UPDATE knowledge_folders SET name=?,parent_id=?,scope_type=?,
        domain_key=?,company=?,track_id=?,sort_order=?,updated_at=datetime('now','localtime')
        WHERE id=?""",
        (d.get("name"), d.get("parent_id"), d.get("scope_type") or "global",
         d.get("domain_key"), d.get("company"), d.get("track_id"),
         d.get("sort_order") or 0, fid))
    conn.commit(); conn.close()


def delete_knowledge_folder(fid):
    conn = get_db()
    cur = conn.execute("DELETE FROM knowledge_folders WHERE id=?", (fid,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def ensure_track_knowledge_folders(track_id):
    conn = get_db()
    existing = rows(conn.execute(
        "SELECT * FROM knowledge_folders WHERE scope_type='track' AND track_id=? ORDER BY sort_order,id",
        (track_id,)).fetchall())
    names = {x["name"] for x in existing}
    created = []
    for _key, name, order in DEFAULT_TRACK_KNOWLEDGE_FOLDERS:
        if name in names:
            continue
        cur = conn.execute("""INSERT INTO knowledge_folders
            (name,scope_type,track_id,sort_order) VALUES (?,'track',?,?)""",
            (name, track_id, order))
        created.append(cur.lastrowid)
    conn.commit()
    result = rows(conn.execute(
        "SELECT * FROM knowledge_folders WHERE scope_type='track' AND track_id=? ORDER BY sort_order,id",
        (track_id,)).fetchall())
    conn.close()
    return result, created


# ─── 统一 Agent Runtime ─────────────────────────────────────────────────────

AGENT_TASK_STATUSES = {"queued", "active", "review", "completed", "failed", "cancelled"}
AGENT_RUN_STATUSES = {"queued", "running", "ready", "completed", "failed", "cancelled"}


def _agent_expert_payload(row):
    expert = one(row)
    if not expert:
        return expert
    for field in ("capabilities", "tools", "allowed_actions"):
        raw = expert.pop(f"{field}_json", None)
        try:
            expert[field] = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            expert[field] = []
    return expert


def list_agent_experts(active_only=True):
    conn = get_db()
    where = "WHERE status='active'" if active_only else ""
    result = [_agent_expert_payload(row) for row in conn.execute(
        f"SELECT * FROM agent_experts {where} ORDER BY sort_order,key").fetchall()]
    conn.close()
    return result


def get_agent_expert(key):
    conn = get_db()
    result = _agent_expert_payload(conn.execute(
        "SELECT * FROM agent_experts WHERE key=?", (key,)).fetchone())
    conn.close()
    return result


def create_agent_task(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO agent_tasks
        (task_type,title,instruction,object_type,object_id,track_id,company,status,
         priority,assigned_expert,conversation_id,context_json,result_summary,created_by,
         execution_owner,actor_type,actor_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("task_type") or "general", d.get("title") or "未命名任务",
         d.get("instruction") or "", d.get("object_type"), d.get("object_id"),
         d.get("track_id"), d.get("company"), d.get("status") or "queued",
         d.get("priority") or "normal", d.get("assigned_expert"),
         d.get("conversation_id"), d.get("context_json"), d.get("result_summary"),
         d.get("created_by") or "user", d.get("execution_owner") or "internal",
         d.get("actor_type") or "user", d.get("actor_key")))
    task_id = cur.lastrowid
    conn.commit(); conn.close()
    return task_id


def update_agent_task(task_id, **fields):
    allowed = {"title", "instruction", "status", "priority", "assigned_expert",
               "context_json", "result_summary", "conversation_id", "execution_owner",
               "actor_type", "actor_key"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return
    conn = get_db()
    assignments = ",".join(f"{key}=?" for key in values)
    completed = ",completed_at=datetime('now','localtime')" if values.get("status") in {"completed", "failed", "cancelled"} else ""
    conn.execute(f"""UPDATE agent_tasks SET {assignments},updated_at=datetime('now','localtime')
        {completed} WHERE id=?""", (*values.values(), task_id))
    conn.commit(); conn.close()


def create_agent_run(d):
    conn = get_db()
    status = d.get("status") or "queued"
    cur = conn.execute("""INSERT INTO agent_runs
        (task_id,run_type,expert_key,model_profile,provider_id,model_name,status,attempt,input_json,output_json,
         summary,started_at,actor_type,actor_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("task_id"), d.get("run_type") or "primary", d.get("expert_key"),
         d.get("model_profile"), d.get("provider_id"), d.get("model_name"),
         status, d.get("attempt") or 1, d.get("input_json"),
         d.get("output_json"), d.get("summary"),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "running" else None,
         d.get("actor_type") or "internal_agent", d.get("actor_key")))
    run_id = cur.lastrowid
    conn.commit(); conn.close()
    return run_id


def update_agent_run(run_id, **fields):
    allowed = {"status", "expert_key", "model_profile", "provider_id", "model_name",
               "input_json", "output_json", "summary", "actor_type", "actor_key"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return
    conn = get_db()
    status = values.get("status")
    extras = ""
    if status == "running":
        extras += ",started_at=COALESCE(started_at,datetime('now','localtime'))"
    if status in {"completed", "failed", "cancelled"}:
        extras += ",completed_at=datetime('now','localtime')"
    assignments = ",".join(f"{key}=?" for key in values)
    conn.execute(f"""UPDATE agent_runs SET {assignments},updated_at=datetime('now','localtime')
        {extras} WHERE id=?""", (*values.values(), run_id))
    conn.commit(); conn.close()


def create_agent_event(d):
    conn = get_db()
    sequence = d.get("sequence")
    if sequence is None:
        if d.get("run_id") is not None:
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 n FROM agent_events WHERE run_id=?",
                                    (d.get("run_id"),)).fetchone()["n"]
        else:
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 n FROM agent_events WHERE task_id=?",
                                    (d.get("task_id"),)).fetchone()["n"]
    cur = conn.execute("""INSERT INTO agent_events
        (task_id,run_id,sequence,event_type,label,detail,status,payload_json)
        VALUES (?,?,?,?,?,?,?,?)""",
        (d.get("task_id"), d.get("run_id"), sequence, d.get("event_type") or "progress",
         d.get("label") or "执行进度", d.get("detail"), d.get("status") or "done",
         d.get("payload_json")))
    event_id = cur.lastrowid
    conn.commit(); conn.close()
    return event_id


def create_proposed_change(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO proposed_changes
        (task_id,run_id,action_type,target_type,target_id,parent_type,parent_id,
         proposed_title,proposed_content,diff,reason,scope_type,status,metadata_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("task_id"), d.get("run_id"), d.get("action_type"),
         d.get("target_type") or "document", d.get("target_id"), d.get("parent_type"),
         d.get("parent_id"), d.get("proposed_title"), d.get("proposed_content"),
         d.get("diff"), d.get("reason"), d.get("scope_type"),
         d.get("status") or "pending", d.get("metadata_json")))
    change_id = cur.lastrowid
    conn.commit(); conn.close()
    return change_id


def update_proposed_change(change_id, status, target_id=None):
    conn = get_db()
    applied = ",applied_at=datetime('now','localtime')" if status == "applied" else ""
    if target_id is None:
        conn.execute(f"""UPDATE proposed_changes SET status=?,updated_at=datetime('now','localtime')
            {applied} WHERE id=?""", (status, change_id))
    else:
        conn.execute(f"""UPDATE proposed_changes SET status=?,target_id=?,updated_at=datetime('now','localtime')
            {applied} WHERE id=?""", (status, target_id, change_id))
    conn.commit(); conn.close()


def edit_proposed_change(change_id, proposed_title=None, proposed_content=None, reason=None):
    """Edit an unconfirmed candidate without changing the underlying career asset."""
    updates, values = [], []
    for key, value in (("proposed_title", proposed_title), ("proposed_content", proposed_content), ("reason", reason)):
        if value is not None:
            updates.append(f"{key}=?")
            values.append(value)
    if not updates:
        return 0
    # A stored diff is now stale; API readers calculate it against current source.
    updates.extend(["diff=NULL", "updated_at=datetime('now','localtime')"])
    values.append(change_id)
    conn = get_db()
    cur = conn.execute(f"UPDATE proposed_changes SET {','.join(updates)} WHERE id=?", values)
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed


def materialize_planned_proposed_change(change_id, *, run_id, title, content, reason, metadata_json):
    """Turn one planned/generating/failed knowledge item into a reviewable candidate."""
    conn = get_db()
    cur = conn.execute("""UPDATE proposed_changes SET run_id=?,proposed_title=?,proposed_content=?,
        reason=?,metadata_json=?,status='pending',diff=NULL,updated_at=datetime('now','localtime')
        WHERE id=? AND status IN ('planned','generating','failed')""",
        (run_id, title, content, reason, metadata_json, change_id))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed


# ─── 外部 Agent 同步：事实与变更游标 ──────────────────────────────────────────

def create_career_fact(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO career_facts
        (subject_type,subject_id,scope_type,scope_id,predicate,value_text,value_json,state,
         confidence,evidence_json,provenance_json,supersedes_id,created_by,confirmed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("subject_type") or "person", d.get("subject_id"),
         d.get("scope_type") or "global", d.get("scope_id"),
         d.get("predicate") or "note", d.get("value_text"), d.get("value_json"),
         d.get("state") or "confirmed", d.get("confidence"), d.get("evidence_json"),
         d.get("provenance_json"), d.get("supersedes_id"), d.get("created_by") or "user",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S") if d.get("state", "confirmed") == "confirmed" else None))
    fid = cur.lastrowid
    conn.commit(); conn.close()
    return fid


def get_career_fact(fid):
    conn = get_db()
    result = one(conn.execute("SELECT * FROM career_facts WHERE id=?", (fid,)).fetchone())
    conn.close()
    return result


def list_career_facts(subject_type=None, subject_id=None, scope_type=None, scope_id=None,
                      state="confirmed", limit=200):
    conn = get_db()
    q, args = "SELECT * FROM career_facts WHERE 1=1", []
    if subject_type:
        q += " AND subject_type=?"; args.append(subject_type)
    if subject_id is not None:
        q += " AND subject_id=?"; args.append(subject_id)
    if scope_type:
        q += " AND scope_type=?"; args.append(scope_type)
    if scope_id is not None:
        q += " AND scope_id=?"; args.append(scope_id)
    if state:
        q += " AND state=?"; args.append(state)
    q += " ORDER BY updated_at DESC,id DESC LIMIT ?"; args.append(max(1, min(int(limit or 200), 500)))
    result = rows(conn.execute(q, args).fetchall())
    conn.close()
    return result


def create_workspace_change_event(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO workspace_change_events
        (event_type,target_type,target_id,scope_type,scope_id,actor_type,actor_key,summary,payload_json)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (d.get("event_type") or "updated", d.get("target_type"), d.get("target_id"),
         d.get("scope_type"), d.get("scope_id"), d.get("actor_type") or "user",
         d.get("actor_key"), d.get("summary"), d.get("payload_json")))
    event_id = cur.lastrowid
    conn.commit(); conn.close()
    return event_id


def list_workspace_change_events(after_id=0, scope_type=None, scope_id=None, limit=100):
    conn = get_db()
    q, args = "SELECT * FROM workspace_change_events WHERE id>?", [max(0, int(after_id or 0))]
    if scope_type:
        q += " AND scope_type=?"; args.append(scope_type)
    if scope_id is not None:
        q += " AND scope_id=?"; args.append(scope_id)
    q += " ORDER BY id ASC LIMIT ?"; args.append(max(1, min(int(limit or 100), 500)))
    result = rows(conn.execute(q, args).fetchall())
    conn.close()
    return result


def latest_workspace_change_cursor():
    conn = get_db()
    value = conn.execute("SELECT COALESCE(MAX(id),0) AS cursor FROM workspace_change_events").fetchone()["cursor"]
    conn.close()
    return value


def get_agent_task(task_id):
    conn = get_db()
    task = one(conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())
    if task:
        task["runs"] = rows(conn.execute(
            "SELECT * FROM agent_runs WHERE task_id=? ORDER BY id", (task_id,)).fetchall())
        task["events"] = rows(conn.execute(
            "SELECT * FROM agent_events WHERE task_id=? ORDER BY sequence,id", (task_id,)).fetchall())
        task["changes"] = rows(conn.execute(
            "SELECT * FROM proposed_changes WHERE task_id=? ORDER BY id", (task_id,)).fetchall())
    conn.close()
    return task


def list_agent_tasks_for_recovery(task_type, statuses=("queued", "active")):
    """Return durable tasks that may have lost their in-process worker."""
    clean_statuses = tuple(
        status for status in statuses if status in AGENT_TASK_STATUSES
    )
    if not clean_statuses:
        return []
    placeholders = ",".join("?" for _ in clean_statuses)
    conn = get_db()
    result = rows(conn.execute(
        f"""SELECT * FROM agent_tasks
            WHERE task_type=? AND status IN ({placeholders})
            ORDER BY id""",
        (task_type, *clean_statuses),
    ).fetchall())
    conn.close()
    return result


def get_proposed_change(change_id):
    conn = get_db()
    result = one(conn.execute(
        "SELECT * FROM proposed_changes WHERE id=?", (change_id,)).fetchone())
    conn.close()
    return result


def legacy_knowledge_change_for_proposal(proposed_change_id):
    conn = get_db()
    result = one(conn.execute("""SELECT kc.*,kr.task_id,kr.agent_run_id
        FROM knowledge_agent_changes kc
        JOIN knowledge_agent_runs kr ON kr.id=kc.run_id
        WHERE kc.proposed_change_id=?""", (proposed_change_id,)).fetchone())
    conn.close()
    return result


def list_agent_tasks(status=None, object_type=None, object_id=None, track_id=None, limit=50):
    conn = get_db()
    q, args = "SELECT * FROM agent_tasks WHERE 1=1", []
    if status:
        q += " AND status=?"; args.append(status)
    if object_type:
        q += " AND object_type=?"; args.append(object_type)
    if object_id is not None:
        q += " AND object_id=?"; args.append(object_id)
    if track_id is not None:
        q += " AND track_id=?"; args.append(track_id)
    q += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(int(limit or 50), 200)))
    result = rows(conn.execute(q, args).fetchall())
    conn.close()
    return result


def latest_agent_task_for_conversation(conversation_id):
    if not conversation_id:
        return None
    conn = get_db()
    item = one(conn.execute(
        "SELECT * FROM agent_tasks WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone())
    conn.close()
    return get_agent_task(item["id"]) if item else None


def list_agent_tasks_for_conversation(conversation_id, limit=12):
    if not conversation_id:
        return []
    conn = get_db()
    task_ids = [row["id"] for row in conn.execute(
        "SELECT id FROM agent_tasks WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
        (conversation_id, max(1, min(int(limit or 12), 50))),
    ).fetchall()]
    conn.close()
    return [get_agent_task(task_id) for task_id in task_ids]


# ─── 知识空间 Agent：兼容层，实际同步写入统一 Runtime ───────────────────────

def create_knowledge_agent_run(d):
    track = get_job_track(d.get("track_id")) or {}
    task_id = d.get("task_id") or create_agent_task({
        "task_type": "knowledge_edit",
        "title": f"整理岗位知识：{track.get('company') or ''} · {track.get('role') or ''}".strip(" ·"),
        "instruction": d.get("instruction") or "",
        "object_type": "job_track",
        "object_id": d.get("track_id"),
        "track_id": d.get("track_id"),
        "company": track.get("company"),
        "status": "active",
        "assigned_expert": "knowledge_coach",
        "context_json": json.dumps({"current_document_id": d.get("current_document_id")}, ensure_ascii=False),
    })
    agent_run_id = d.get("agent_run_id") or create_agent_run({
        "task_id": task_id, "run_type": "primary", "expert_key": "knowledge_coach",
        "model_profile": "writing", "status": "running",
        "input_json": json.dumps({"instruction": d.get("instruction") or ""}, ensure_ascii=False),
    })
    conn = get_db()
    cur = conn.execute("""INSERT INTO knowledge_agent_runs
        (track_id,instruction,current_document_id,status,summary,review_json,task_id,agent_run_id)
        VALUES (?,?,?,?,?,?,?,?)""",
        (d.get("track_id"), d.get("instruction"), d.get("current_document_id"),
         d.get("status") or "planning", d.get("summary"), d.get("review_json"),
         task_id, agent_run_id))
    rid = cur.lastrowid
    conn.commit(); conn.close()
    return rid


def update_knowledge_agent_run(rid, **fields):
    allowed = {"status", "summary", "review_json"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return
    conn = get_db()
    assignments = ",".join(f"{key}=?" for key in values)
    conn.execute(f"""UPDATE knowledge_agent_runs SET {assignments},
        updated_at=datetime('now','localtime') WHERE id=?""", (*values.values(), rid))
    link = one(conn.execute("SELECT task_id,agent_run_id FROM knowledge_agent_runs WHERE id=?", (rid,)).fetchone())
    conn.commit(); conn.close()
    if link:
        legacy_status = values.get("status")
        run_status = {"planning": "running", "editing": "running", "ready": "ready",
                      "applied": "completed", "failed": "failed", "cancelled": "cancelled"}.get(legacy_status)
        task_status = {"planning": "active", "editing": "active", "ready": "review",
                       "applied": "completed", "failed": "failed", "cancelled": "cancelled"}.get(legacy_status)
        if link.get("agent_run_id"):
            update_agent_run(link["agent_run_id"], **{k: v for k, v in {
                "status": run_status, "summary": values.get("summary"),
                "output_json": values.get("review_json")}.items() if v is not None})
        if link.get("task_id"):
            update_agent_task(link["task_id"], **{k: v for k, v in {
                "status": task_status, "result_summary": values.get("summary")}.items() if v is not None})


def create_knowledge_agent_change(d):
    run = get_knowledge_agent_run(d.get("run_id")) or {}
    meta = {}
    try:
        meta = json.loads(d.get("metadata_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        pass
    proposed_change_id = None
    if run.get("task_id"):
        proposed_change_id = create_proposed_change({
            "task_id": run.get("task_id"), "run_id": run.get("agent_run_id"),
            "action_type": d.get("action_type"), "target_type": "knowledge_item",
            "target_id": d.get("document_id"), "parent_type": "job_track",
            "parent_id": run.get("track_id"), "proposed_title": d.get("proposed_title"),
            "proposed_content": d.get("proposed_content"), "diff": d.get("diff"),
            "reason": d.get("reason"), "scope_type": meta.get("scope_type"),
            "status": d.get("status") or "pending", "metadata_json": d.get("metadata_json"),
        })
    conn = get_db()
    cur = conn.execute("""INSERT INTO knowledge_agent_changes
        (run_id,action_type,document_id,folder_id,proposed_title,proposed_content,
         diff,reason,status,metadata_json,proposed_change_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("run_id"), d.get("action_type"), d.get("document_id"),
         d.get("folder_id"), d.get("proposed_title"), d.get("proposed_content"),
         d.get("diff"), d.get("reason"), d.get("status") or "pending",
         d.get("metadata_json"), proposed_change_id))
    cid = cur.lastrowid
    conn.commit(); conn.close()
    return cid


def update_knowledge_agent_change(cid, status):
    conn = get_db()
    link = one(conn.execute("SELECT proposed_change_id FROM knowledge_agent_changes WHERE id=?", (cid,)).fetchone())
    conn.execute("""UPDATE knowledge_agent_changes SET status=?,
        updated_at=datetime('now','localtime') WHERE id=?""", (status, cid))
    conn.commit(); conn.close()
    if link and link.get("proposed_change_id"):
        update_proposed_change(link["proposed_change_id"], status)


def create_knowledge_agent_event(run_id, event_type, label, detail=None, status="done"):
    run = get_knowledge_agent_run(run_id) or {}
    agent_event_id = None
    if run.get("task_id"):
        agent_event_id = create_agent_event({
            "task_id": run.get("task_id"), "run_id": run.get("agent_run_id"),
            "event_type": event_type, "label": label, "detail": detail, "status": status,
        })
    conn = get_db()
    cur = conn.execute("""INSERT INTO knowledge_agent_events
        (run_id,event_type,label,detail,status,agent_event_id) VALUES (?,?,?,?,?,?)""",
        (run_id, event_type, label, detail, status, agent_event_id))
    event_id = cur.lastrowid
    conn.commit(); conn.close()
    return event_id


def get_knowledge_agent_run(rid):
    conn = get_db()
    run = one(conn.execute("SELECT * FROM knowledge_agent_runs WHERE id=?", (rid,)).fetchone())
    if run:
        run["changes"] = rows(conn.execute(
            "SELECT * FROM knowledge_agent_changes WHERE run_id=? ORDER BY id", (rid,)
        ).fetchall())
        run["events"] = rows(conn.execute(
            "SELECT * FROM knowledge_agent_events WHERE run_id=? ORDER BY id", (rid,)
        ).fetchall())
        run["task"] = one(conn.execute(
            "SELECT * FROM agent_tasks WHERE id=?", (run.get("task_id"),)
        ).fetchone()) if run.get("task_id") else None
        run["agent_run"] = one(conn.execute(
            "SELECT * FROM agent_runs WHERE id=?", (run.get("agent_run_id"),)
        ).fetchone()) if run.get("agent_run_id") else None
    conn.close()
    return run


def apply_knowledge_agent_run(rid, change_ids):
    """Atomically apply selected Agent changes and ignore the rest."""
    conn = get_db()
    try:
        run = one(conn.execute(
            "SELECT * FROM knowledge_agent_runs WHERE id=?", (rid,)
        ).fetchone())
        if not run or run.get("status") not in {"ready", "reviewed"}:
            raise ValueError("这次 Agent 运行已经处理或不存在")
        selected = {int(x) for x in change_ids}
        changes = rows(conn.execute(
            "SELECT * FROM knowledge_agent_changes WHERE run_id=? ORDER BY id", (rid,)
        ).fetchall())
        results = []
        for change in changes:
            if change["id"] not in selected:
                conn.execute("""UPDATE knowledge_agent_changes SET status='ignored',
                    updated_at=datetime('now','localtime') WHERE id=?""", (change["id"],))
                if change.get("proposed_change_id"):
                    conn.execute("""UPDATE proposed_changes SET status='ignored',
                        updated_at=datetime('now','localtime') WHERE id=?""", (change["proposed_change_id"],))
                continue
            if change.get("status") != "pending":
                continue
            if change["action_type"] == "create":
                meta = json.loads(change.get("metadata_json") or "{}")
                scope_type = meta.get("scope_type") if meta.get("scope_type") in {"track", "domain"} else "track"
                cur = conn.execute("""INSERT INTO knowledge_items
                    (title,content,scope_type,track_id,domain_key,topic,mastery,source_type,folder_id,status)
                    VALUES (?,?,?,?,?,?,?,'agent',?,'active')""",
                    (change.get("proposed_title") or "未命名文档",
                     change.get("proposed_content") or "", scope_type,
                     run["track_id"] if scope_type == "track" else None,
                     meta.get("domain_key") if scope_type == "domain" else None,
                     meta.get("topic") or "岗位准备", meta.get("mastery") or "learning",
                     change.get("folder_id") if scope_type == "track" else None))
                document_id = cur.lastrowid
            else:
                current = one(conn.execute(
                    "SELECT * FROM knowledge_items WHERE id=?", (change.get("document_id"),)
                ).fetchone())
                if not current or not (
                    (current.get("scope_type") == "track" and current.get("track_id") == run["track_id"]) or
                    current.get("scope_type") == "domain"
                ):
                    raise ValueError("Agent 只能修改当前岗位或本轮行业知识")
                conn.execute("""UPDATE knowledge_items SET title=?,content=?,folder_id=?,
                    updated_at=datetime('now','localtime') WHERE id=?""",
                    (change.get("proposed_title") or current.get("title"),
                     change.get("proposed_content") if change.get("proposed_content") is not None else current.get("content"),
                     (change.get("folder_id") if change.get("folder_id") is not None else current.get("folder_id"))
                     if current.get("scope_type") == "track" else None,
                     current["id"]))
                document_id = current["id"]
            conn.execute("""UPDATE knowledge_agent_changes SET status='applied',document_id=?,
                updated_at=datetime('now','localtime') WHERE id=?""", (document_id, change["id"]))
            if change.get("proposed_change_id"):
                conn.execute("""UPDATE proposed_changes SET status='applied',target_id=?,
                    applied_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                    WHERE id=?""", (document_id, change["proposed_change_id"]))
            results.append({"change_id": change["id"], "document_id": document_id,
                            "action_type": change["action_type"]})
        conn.execute("""UPDATE knowledge_agent_runs SET status='applied',
            updated_at=datetime('now','localtime') WHERE id=?""", (rid,))
        if run.get("agent_run_id"):
            conn.execute("""UPDATE agent_runs SET status='completed',completed_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime') WHERE id=?""", (run["agent_run_id"],))
        if run.get("task_id"):
            conn.execute("""UPDATE agent_tasks SET status='completed',completed_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime') WHERE id=?""", (run["task_id"],))
        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_assets_stale(project_id=None, track_id=None, global_scope=False, exclude_asset_id=None):
    conn = get_db()
    q = "UPDATE assets SET status='stale',updated_at=datetime('now','localtime') WHERE status!='stale'"
    args = []
    if exclude_asset_id is not None:
        q += " AND id!=?"
        args.append(exclude_asset_id)
    if not global_scope:
        conditions = []
        if project_id is not None:
            conditions.append("project_id=?")
            args.append(project_id)
        if track_id is not None:
            conditions.append("track_id=?")
            args.append(track_id)
        if not conditions:
            conn.close()
            return 0
        q += " AND (" + " OR ".join(conditions) + ")"
    cur = conn.execute(q, args)
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed


def feedback_impacted_assets(scope="global", scope_id=None, limit=200):
    conn = get_db()
    q = "SELECT * FROM assets WHERE 1=1"
    args = []
    scope = scope or "global"
    if scope == "track":
        q += " AND track_id=?"
        args.append(scope_id)
    elif scope == "project":
        q += " AND project_id=?"
        args.append(scope_id)
    elif scope == "asset":
        q += " AND id=?"
        args.append(scope_id)
    elif scope == "experience":
        q += " AND project_id IN (SELECT id FROM projects WHERE experience_id=?)"
        args.append(scope_id)
    elif scope == "global":
        pass
    else:
        q += " AND 1=0"
    q += " ORDER BY updated_at DESC,id DESC LIMIT ?"
    args.append(limit)
    r = conn.execute(q, args).fetchall()
    conn.close()
    return rows(r)


def list_feedback_notes(scope=None, scope_id=None, status="active"):
    conn = get_db()
    q = "SELECT * FROM feedback_notes WHERE 1=1"
    args = []
    if scope:
        q += " AND scope=?"
        args.append(scope)
    if scope_id is not None:
        q += " AND scope_id=?"
        args.append(scope_id)
    if status:
        q += " AND COALESCE(status, 'active')=?"
        args.append(status)
    q += " ORDER BY updated_at DESC, id DESC"
    r = conn.execute(q, args).fetchall()
    conn.close()
    return rows(r)


def get_feedback_note(fid):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM feedback_notes WHERE id=?", (fid,)).fetchone())
    conn.close()
    return x


def create_feedback_note(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO feedback_notes
        (scope,scope_id,note_type,content,category,polarity,strength,directive,original_text,source_id,status,impact_json,normalized_at,supersedes_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("scope") or "global", d.get("scope_id"), d.get("note_type") or "feedback",
         d.get("content"), d.get("category"), d.get("polarity"), d.get("strength"),
         d.get("directive"), d.get("original_text"), d.get("source_id"),
         d.get("status") or "active", d.get("impact_json"), d.get("normalized_at"),
         d.get("supersedes_id")))
    fid = cur.lastrowid
    conn.commit(); conn.close()
    return fid


def update_feedback_note(fid, **patch):
    allowed = {
        "scope", "scope_id", "note_type", "content", "category", "polarity", "strength",
        "directive", "original_text", "source_id", "status", "impact_json",
        "normalized_at", "supersedes_id",
    }
    sets, args = [], []
    for key, value in patch.items():
        if key in allowed:
            sets.append(f"{key}=?")
            args.append(value)
    if not sets:
        return 0
    sets.append("updated_at=datetime('now','localtime')")
    args.append(fid)
    conn = get_db()
    cur = conn.execute(f"UPDATE feedback_notes SET {','.join(sets)} WHERE id=?", args)
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed


def delete_feedback_note(fid):
    conn = get_db()
    cur = conn.execute("DELETE FROM feedback_notes WHERE id=?", (fid,))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed


# ─── 面试关注点库（自迭代 prompt 的内容层） ─────────────────────────────────────

def list_focus_points(scope="portfolio"):
    """当前生效的面试关注点（从逐字稿提炼，注入作品整理/生成 prompt）。"""
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM feedback_notes WHERE note_type='focus_point' AND scope=? "
        "AND COALESCE(status,'active')='active' ORDER BY "
        "CASE WHEN strength='hard' THEN 0 ELSE 1 END, id", (scope,)).fetchall()
    conn.close()
    return rows(r)


def replace_focus_points(items, scope="portfolio"):
    """整体替换关注点库：旧的归档（可回溯），写入新的合并后列表。"""
    conn = get_db()
    conn.execute("UPDATE feedback_notes SET status='archived' "
                 "WHERE note_type='focus_point' AND scope=? "
                 "AND COALESCE(status,'active')='active'", (scope,))
    for it in items:
        directive = (it.get("directive") or it.get("concern") or "").strip()
        if not directive:
            continue
        conn.execute("""INSERT INTO feedback_notes
            (scope,note_type,content,category,strength,directive,original_text,status)
            VALUES (?,?,?,?,?,?,?, 'active')""",
            (scope, "focus_point", directive,
             it.get("dimension") or it.get("category"),
             it.get("strength") or "soft", directive,
             it.get("evidence") or it.get("original_text")))
    conn.commit(); conn.close()


def create_interview_review(d, questions=None):
    """保存一场面试复盘：总览进 interview_reviews，逐题进 interview_questions。"""
    questions = questions or []
    prediction = d.get("prediction_json")
    if isinstance(prediction, (dict, list)):
        prediction = json.dumps(prediction, ensure_ascii=False)
    tags = d.get("tags")
    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(x).strip() for x in tags if str(x).strip())
    conn = get_db()
    cur = conn.execute("""INSERT INTO interview_reviews
        (scope,track_id,title,role_type,round,source_text,summary,prediction_json,report_knowledge_id,tags)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (d.get("scope") or "portfolio", d.get("track_id"), d.get("title"),
         d.get("role_type"), d.get("round"), d.get("source_text"), d.get("summary"),
         prediction, d.get("report_knowledge_id"), tags))
    review_id = cur.lastrowid
    for idx, q in enumerate(questions, 1):
        q_tags = q.get("tags")
        if isinstance(q_tags, (list, tuple)):
            q_tags = ",".join(str(x).strip() for x in q_tags if str(x).strip())
        if not q_tags:
            q_tags = ",".join(str(x).strip() for x in (q.get("ability"), q.get("risk_level")) if str(x or "").strip())
        conn.execute("""INSERT INTO interview_questions
            (review_id,track_id,question_order,question,answer,intent,answer_review,better_answer,risk_level,ability,tags)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (review_id, d.get("track_id"), q.get("question_order") or idx,
             q.get("question"), q.get("answer"), q.get("intent"), q.get("answer_review"),
             q.get("better_answer"), q.get("risk_level"), q.get("ability"), q_tags))
    conn.commit(); conn.close()
    return review_id


def list_interview_reviews(track_id=None, role_type=None, q=None, limit=100):
    conn = get_db()
    sql = """SELECT r.*,
        (SELECT COUNT(*) FROM interview_questions iq WHERE iq.review_id=r.id) AS question_count
        FROM interview_reviews r WHERE 1=1"""
    args = []
    if track_id is not None:
        sql += " AND r.track_id=?"
        args.append(track_id)
    if role_type:
        sql += " AND r.role_type=?"
        args.append(role_type)
    if q:
        like = f"%{q.strip()}%"
        sql += """ AND (r.title LIKE ? OR r.summary LIKE ? OR r.tags LIKE ? OR EXISTS (
            SELECT 1 FROM interview_questions iq WHERE iq.review_id=r.id
            AND (iq.question LIKE ? OR iq.intent LIKE ? OR iq.ability LIKE ? OR iq.tags LIKE ?)
        ))"""
        args.extend([like, like, like, like, like, like, like])
    sql += " ORDER BY r.created_at DESC,r.id DESC LIMIT ?"
    args.append(int(limit or 100))
    result = rows(conn.execute(sql, args).fetchall())
    conn.close()
    return result


# ─── 项目追问题库 ─────────────────────────────────────────────────────────────

def list_followups(project_id=None, status=None):
    conn = get_db()
    q = "SELECT * FROM followups WHERE 1=1"
    args = []
    if project_id is not None:
        q += " AND project_id=?"
        args.append(project_id)
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY COALESCE(category,''), id DESC"
    r = conn.execute(q, args).fetchall()
    conn.close()
    return rows(r)


def get_followup(fid):
    conn = get_db()
    x = one(conn.execute("SELECT * FROM followups WHERE id=?", (fid,)).fetchone())
    conn.close()
    return x


def create_followup(project_id, d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO followups
        (project_id,question,answer,status,category,asked_count,origin,source_track_id,kind,
         category_source,previous_category)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (project_id, d.get("question"), d.get("answer"), d.get("status") or "todo",
         d.get("category"), d.get("asked_count") or 0, d.get("origin") or "manual",
         d.get("source_track_id"), d.get("kind"), d.get("category_source"),
         d.get("previous_category")))
    fid = cur.lastrowid
    conn.commit(); conn.close()
    return fid


def update_followup(fid, d):
    cur = get_followup(fid)
    if not cur:
        return
    merged = {
        "question": d.get("question", cur.get("question")),
        "answer": d.get("answer", cur.get("answer")),
        "status": d.get("status", cur.get("status")),
        "category": d.get("category", cur.get("category")),
        "asked_count": d.get("asked_count", cur.get("asked_count") or 0),
        "origin": d.get("origin", cur.get("origin")),
        "source_track_id": d.get("source_track_id", cur.get("source_track_id")),
        "kind": d.get("kind", cur.get("kind")),
        "category_source": d.get("category_source", cur.get("category_source")),
        "previous_category": d.get("previous_category", cur.get("previous_category")),
    }
    conn = get_db()
    conn.execute("""UPDATE followups SET question=?,answer=?,status=?,category=?,
        asked_count=?,origin=?,source_track_id=?,kind=?,category_source=?,previous_category=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (merged["question"], merged["answer"], merged["status"], merged["category"],
         merged["asked_count"], merged["origin"], merged["source_track_id"], merged["kind"],
         merged["category_source"], merged["previous_category"], fid))
    conn.commit(); conn.close()


def delete_followup(fid):
    conn = get_db()
    conn.execute("DELETE FROM followups WHERE id=?", (fid,))
    conn.commit(); conn.close()


# ─── 总览统计 ─────────────────────────────────────────────────────────────────

def overview_stats():
    conn = get_db()
    g = lambda q: conn.execute(q).fetchone()["n"]
    data = {
        "experiences": g("SELECT COUNT(*) n FROM experiences"),
        "projects": g("SELECT COUNT(*) n FROM projects"),
        "applications": g("SELECT COUNT(*) n FROM applications"),
        "interviewing": g("SELECT COUNT(*) n FROM applications WHERE status='interview'"),
        "offers": g("SELECT COUNT(*) n FROM applications WHERE status='offer'"),
        "intros": g("SELECT COUNT(*) n FROM interview_items WHERE kind='self_intro'"),
        "knowledge": g("SELECT COUNT(*) n FROM interview_items WHERE kind='knowledge'"),
        "logs": g("SELECT COUNT(*) n FROM work_logs"),
        "sources": g("SELECT COUNT(*) n FROM sources"),
        "tracks": g("SELECT COUNT(*) n FROM job_tracks"),
        "assets": g("SELECT COUNT(*) n FROM assets"),
    }
    by_status = rows(conn.execute("SELECT status,COUNT(*) n FROM applications GROUP BY status").fetchall())
    by_industry = rows(conn.execute(
        "SELECT industry,COUNT(*) n FROM applications WHERE industry IS NOT NULL AND industry<>'' GROUP BY industry ORDER BY n DESC LIMIT 8").fetchall())
    conn.close()
    data["by_status"] = by_status
    data["by_industry"] = by_industry
    return data


def ops_brief():
    """求职操作系统首页：从现有资产自动推导下一步行动和缺口。"""
    conn = get_db()
    exps = rows(conn.execute("SELECT * FROM experiences ORDER BY start_date DESC").fetchall())
    projects = rows(conn.execute("""
        SELECT p.*, e.company AS experience_company, e.role AS experience_role
        FROM projects p
        LEFT JOIN experiences e ON e.id=p.experience_id
        ORDER BY p.updated_at DESC, p.id DESC
    """).fetchall())
    apps = rows(conn.execute("SELECT * FROM applications ORDER BY applied_date DESC, id DESC").fetchall())
    items = rows(conn.execute("SELECT * FROM interview_items ORDER BY updated_at DESC").fetchall())
    logs = rows(conn.execute("SELECT * FROM work_logs ORDER BY log_date DESC, id DESC LIMIT 20").fetchall())
    sources = rows(conn.execute("SELECT * FROM sources ORDER BY updated_at DESC, id DESC LIMIT 20").fetchall())
    tracks = rows(conn.execute("SELECT * FROM job_tracks ORDER BY updated_at DESC, id DESC").fetchall())
    conn.close()

    pitches = {x.get("project_id") for x in items if x.get("kind") == "project_pitch" and x.get("project_id")}
    intros = [x for x in items if x.get("kind") == "self_intro"]
    knowledge = [x for x in items if x.get("kind") == "knowledge"]
    active_apps = [a for a in apps if a.get("status") not in ("offer", "rejected")]
    apps_missing_jd = [a for a in active_apps if not (a.get("job_description") or "").strip()]
    apps_need_analysis = [a for a in active_apps if (a.get("job_description") or "").strip() and not (a.get("gap_analysis") or "").strip()]
    thin_projects = [p for p in projects if len((p.get("document") or "").strip()) < 350]
    projects_without_pitch = [p for p in projects if p.get("id") not in pitches]

    def action(kind, title, why, cta, view, priority=2, entity_id=None, intent=None):
        return {
            "kind": kind, "title": title, "why": why, "cta": cta,
            "view": view, "priority": priority, "entity_id": entity_id, "intent": intent,
        }

    actions = []
    if not sources:
        actions.append(action("sources", "先把资料丢进资料库", "Caddie 需要先拥有简历、JD、面试反馈和历史打磨稿，后续分析才有完整上下文。", "去上传", "sources", 1))
    if not exps:
        actions.append(action("foundation", "先录入第一段经历", "没有经历资产，后续简历、JD 匹配和面试准备都缺少事实来源。", "去录经历", "projects", 1))
    if thin_projects:
        p = thin_projects[0]
        actions.append(action("project_doc", f"补全项目文档：{p.get('name')}", "这份项目材料偏薄，建议补背景、你的动作、数据结果和可追问细节。", "打开项目", "projects", 1, p.get("id"), "project"))
    if apps_missing_jd:
        a = apps_missing_jd[0]
        actions.append(action("jd", f"补 JD：{a.get('company')} · {a.get('role')}", "有投递但没有岗位描述，无法做差距分析和面试准备。", "粘贴 JD", "board", 1, a.get("id"), "jd"))
    if apps_need_analysis:
        a = apps_need_analysis[0]
        actions.append(action("jd_analysis", f"分析 JD 差距：{a.get('company')} · {a.get('role')}", "这条投递已经有 JD，但还没生成匹配点、缺口和准备建议。", "做对比", "board", 1, a.get("id"), "jd"))
    if projects_without_pitch:
        p = projects_without_pitch[0]
        actions.append(action("pitch", f"生成项目话术：{p.get('name')}", "项目已经沉淀，但还没有 30 秒版、2 分钟版和高频追问答法。", "去生成", "interview", 2, p.get("id"), "pitch"))
    if not intros:
        actions.append(action("intro", "生成第一版自我介绍", "面试前需要一版可复用的定位陈述，后续可按岗位分版本。", "去生成", "interview", 2, None, "intro"))
    if not logs:
        actions.append(action("worklog", "开始记录在职日记", "日记是后续提炼成果、补简历项目故事的原料。", "记一条", "worklog", 3))
    if not knowledge:
        actions.append(action("knowledge", "建立第一张面试知识卡", "把常考知识点沉淀成卡片，模拟面试前可以快速复习。", "去建卡", "knowledge", 3))

    actions = sorted(actions, key=lambda x: x["priority"])[:8]

    total_projects = max(len(projects), 1)
    total_active = max(len(active_apps), 1)
    gaps = [
        {
            "key": "project_docs",
            "label": "项目故事完整度",
            "ready": len(projects) - len(thin_projects),
            "total": len(projects),
            "hint": "文档超过 350 字的项目，通常更适合被改写成简历 bullet 和面试故事。",
            "view": "projects",
        },
        {
            "key": "project_pitch",
            "label": "项目话术覆盖",
            "ready": len([p for p in projects if p.get("id") in pitches]),
            "total": len(projects),
            "hint": "每个重点项目都应该有 30 秒版、2 分钟版和追问答法。",
            "view": "interview",
        },
        {
            "key": "jd",
            "label": "活跃投递 JD 覆盖",
            "ready": len(active_apps) - len(apps_missing_jd),
            "total": len(active_apps),
            "hint": "没有 JD，就无法判断简历和面试应该往哪里定制。",
            "view": "board",
        },
        {
            "key": "jd_analysis",
            "label": "JD 差距分析覆盖",
            "ready": len([a for a in active_apps if (a.get("gap_analysis") or "").strip()]),
            "total": len(active_apps),
            "hint": "差距分析会把岗位要求转成简历修改点、知识准备和面试策略。",
            "view": "board",
        },
    ]
    for g in gaps:
        total = max(g["total"], 1)
        g["ratio"] = round(g["ready"] / total, 2)
        g["missing"] = max(g["total"] - g["ready"], 0)

    focus = "先补齐求职资产底座"
    if actions:
        focus = actions[0]["title"]
    elif active_apps:
        focus = "当前资产比较完整，可以进入面试复训和投递跟进"
    elif apps:
        focus = "复盘已结束投递，准备下一轮定向投递"

    return {
        "focus": focus,
        "actions": actions,
        "gaps": gaps,
        "active_applications": active_apps[:5],
        "recent_logs": logs[:5],
        "stats": {
            "experiences": len(exps),
            "projects": len(projects),
            "applications": len(apps),
            "active_applications": len(active_apps),
            "self_intros": len(intros),
            "knowledge_cards": len(knowledge),
            "work_logs": len(logs),
            "sources": len(sources),
            "tracks": len(tracks),
            "thin_projects": len(thin_projects),
            "apps_missing_jd": len(apps_missing_jd),
            "apps_need_analysis": len(apps_need_analysis),
            "projects_without_pitch": len(projects_without_pitch),
        },
    }


# ─── 给 AI 的数据快照（让它能引用正确的 id 来提改动）────────────────────────

def snapshot_for_ai() -> str:
    exps = get_experiences()
    apps = get_applications()
    s = "【当前经历与项目（含 id）】\n"
    if not exps:
        s += "（空）\n"
    for e in exps:
        s += f"经历 id={e['id']}：{e['company']} · {e['role']}\n"
        for p in e.get("projects", []):
            s += f"    项目 id={p['id']}：{p['name']}（{p.get('one_liner') or ''}）\n"
    s += "\n【当前投递（含 id）】\n"
    if not apps:
        s += "（空）\n"
    for a in apps:
        s += f"投递 id={a['id']}：{a['company']} · {a['role']} | 行业 {a.get('industry') or '?'} | 状态 {a['status']}\n"
    return s


# ─── 检索 ─────────────────────────────────────────────────────────────────────

def search(query, limit=20):
    conn = get_db()
    q = query.strip()
    if not q:
        conn.close()
        return []
    found = []
    # If the user names a document inside a longer sentence, exact title
    # evidence must outrank semantically similar notes. SQLite unicode61 does
    # not segment continuous Chinese reliably, and titles often carry numeric
    # prefixes such as "1." that the user naturally omits.
    def normalized_title(value):
        value = re.sub(r"^\s*(?:[（(]?\d+[）).、．]\s*)+", "", str(value or ""))
        return re.sub(r"[\s·•_—\-:：，。！？；、（）()【】\[\]《》]+", "", value).lower()

    normalized_query = normalized_title(q)
    exact_title_rows = []
    for item in rows(conn.execute(
        """SELECT entity_type,entity_id,title,content snippet
           FROM search_index WHERE length(trim(title)) >= 4 LIMIT 3000"""
    ).fetchall()):
        title_key = normalized_title(item.get("title"))
        if len(title_key) >= 4 and title_key in normalized_query:
            item["exact_title_match"] = True
            exact_title_rows.append(item)
    exact_title_rows.sort(key=lambda item: len(normalized_title(item.get("title"))), reverse=True)
    try:
        terms = " OR ".join(f'"{t}"*' for t in q.split() if t)
        found = rows(conn.execute("""
            SELECT entity_type, entity_id, title,
                snippet(search_index,3,'《','》','…',18) snippet
            FROM search_index WHERE search_index MATCH ? ORDER BY rank LIMIT ?""",
            (terms or q + "*", limit)).fetchall())
    except sqlite3.Error:
        found = []
    # unicode61 does not provide reliable substring matching for continuous
    # CJK text. A bounded LIKE pass also covers two-character company names and
    # metadata phrases while preserving FTS ordering for normal queries.
    if len(found) < limit:
        like = f"%{q}%"
        fallback = rows(conn.execute(
            """SELECT entity_type,entity_id,title,content snippet
               FROM search_index
               WHERE title LIKE ? OR content LIKE ?
               LIMIT ?""",
            (like, like, limit),
        ).fetchall())
        seen = {(str(item["entity_type"]), str(item["entity_id"])) for item in found}
        for item in fallback:
            key = (str(item["entity_type"]), str(item["entity_id"]))
            if key not in seen:
                found.append(item)
                seen.add(key)
            if len(found) >= limit:
                break
    merged = []
    seen = set()
    for item in exact_title_rows + found:
        key = (str(item["entity_type"]), str(item["entity_id"]))
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)
        if len(merged) >= limit:
            break
    conn.close()
    return merged


def search_index_health():
    conn = get_db()
    fts_count = conn.execute("SELECT COUNT(*) n FROM search_index").fetchone()["n"]
    semantic_count = conn.execute("SELECT COUNT(*) n FROM semantic_chunks").fetchone()["n"]
    orphan_projects = conn.execute(
        """SELECT COUNT(*) n FROM search_index s
           WHERE s.entity_type='project'
             AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=CAST(s.entity_id AS INTEGER))"""
    ).fetchone()["n"]
    conn.close()
    return {
        "fts_documents": int(fts_count),
        "semantic_chunks": int(semantic_count),
        "orphan_projects": int(orphan_projects),
        "semantic_ready": int(semantic_count) > 0,
    }


def replace_search_document(entity_type, entity_id, title, content):
    conn = get_db()
    conn.execute("DELETE FROM search_index WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
    conn.execute("INSERT INTO search_index(entity_type,entity_id,title,content) VALUES(?,?,?,?)",
                 (entity_type, entity_id, title or "", content or ""))
    conn.commit(); conn.close()


def delete_stale_search_documents(active_entities):
    """清理全量重建后已不存在的受管实体，避免 FTS 命中幽灵数据。"""
    managed_types = ("job_track", "knowledge_item", "project", "source", "asset")
    active = {(str(entity_type), str(entity_id)) for entity_type, entity_id in active_entities}
    conn = get_db()
    stale = conn.execute(
        "SELECT entity_type,entity_id FROM search_index "
        "WHERE entity_type IN (?,?,?,?,?)",
        managed_types,
    ).fetchall()
    for item in stale:
        key = (str(item["entity_type"]), str(item["entity_id"]))
        if key not in active:
            conn.execute(
                "DELETE FROM search_index WHERE entity_type=? AND entity_id=?",
                (item["entity_type"], item["entity_id"]),
            )
    conn.commit()
    conn.close()


def get_semantic_chunks():
    conn = get_db()
    result = rows(conn.execute("SELECT * FROM semantic_chunks").fetchall())
    conn.close()
    return result


def upsert_semantic_chunk(item):
    conn = get_db()
    conn.execute("""INSERT INTO semantic_chunks
        (chunk_key,entity_type,entity_id,title,content,content_hash,vector_json,metadata_json,updated_at)
        VALUES(?,?,?,?,?,?,?,?,datetime('now','localtime'))
        ON CONFLICT(chunk_key) DO UPDATE SET title=excluded.title,content=excluded.content,
        content_hash=excluded.content_hash,vector_json=excluded.vector_json,
        metadata_json=excluded.metadata_json,updated_at=datetime('now','localtime')""",
        (item.get("chunk_key"), item.get("entity_type"), item.get("entity_id"), item.get("title"),
         item.get("content"), item.get("content_hash"), item.get("vector_json"), item.get("metadata_json")))
    conn.commit(); conn.close()


def delete_stale_semantic_chunks(active_keys):
    conn = get_db()
    if active_keys:
        marks = ",".join("?" for _ in active_keys)
        conn.execute(f"DELETE FROM semantic_chunks WHERE chunk_key NOT IN ({marks})", tuple(active_keys))
    else:
        conn.execute("DELETE FROM semantic_chunks")
    conn.commit(); conn.close()


# ─── 应用 AI 提议的结构化改动 ────────────────────────────────────────────────

def apply_changes(changes: list):
    """执行一组改动，返回结果、投递 id 和可导航的实际目标。"""
    done = []
    app_ids = []  # 新建的 application id，供调用方存 JD 用
    targets = []
    for ch in changes:
        t = ch.get("type")
        try:
            if t == "create_experience":
                eid = create_experience(ch)
                done.append(f"新增经历：{ch.get('company')} · {ch.get('role')}")
                targets.append({
                    "type": "experience", "id": eid,
                    "label": f"{ch.get('company')} · {ch.get('role')}",
                })
                for p in ch.get("projects", []) or []:
                    pid = create_project(eid, p)
                    done.append(f"  └ 新增项目：{p.get('name')}")
                    targets.append({
                        "type": "project", "id": pid, "parent_id": eid,
                        "label": p.get("name") or "未命名项目",
                    })
            elif t == "create_project":
                project_data, source_ids = _source_backed_project_change(ch)
                pid = create_project(ch.get("experience_id"), project_data)
                _link_project_sources(pid, source_ids)
                done.append(f"新增项目：{ch.get('name')}")
                targets.append({
                    "type": "project", "id": pid,
                    "parent_id": ch.get("experience_id"),
                    "label": ch.get("name") or "未命名项目",
                })
            elif t == "update_project":
                cur = get_project(ch.get("project_id")) or {}
                merged = {
                    "name": ch.get("name") or cur.get("name"),
                    "one_liner": ch.get("one_liner") or cur.get("one_liner"),
                    "document": ch.get("document") or cur.get("document"),
                    "technologies": ch.get("technologies") or cur.get("technologies"),
                    "keywords": ch.get("keywords") or cur.get("keywords"),
                }
                merged, source_ids = _source_backed_project_change(ch, merged)
                update_project(ch.get("project_id"), merged)
                _link_project_sources(ch.get("project_id"), source_ids)
                done.append(f"更新项目文档：{merged['name']}")
                targets.append({
                    "type": "project", "id": ch.get("project_id"),
                    "label": merged.get("name") or "未命名项目",
                })
            elif t == "create_followup":
                create_followup(ch.get("project_id"), ch)
                done.append(f"新增追问：{(ch.get('question') or '')[:36]}")
            elif t == "create_application":
                aid = create_application(ch)
                if ch.get("job_description"):
                    set_application_jd(aid, ch.get("job_description"))
                app_ids.append(aid)
                done.append(f"新增投递：{ch.get('company')} · {ch.get('role')}")
            elif t == "update_application":
                aid = ch.get("application_id")
                current = get_application(aid)
                if not current:
                    raise ValueError(f"投递 id={aid} 不存在")
                if ch.get("job_description"):
                    set_application_jd(aid, ch.get("job_description"))
                    current = get_application(aid)
                merged = dict(current)
                merged.update({k: v for k, v in ch.items()
                               if k not in {"type", "application_id", "job_description", "allow_status_correction"}
                               and v is not None})
                update_application(
                    aid, merged,
                    allow_status_correction=bool(ch.get("allow_status_correction")),
                )
                app_ids.append(aid)
                done.append(f"更新投递：{merged.get('company')} · {merged.get('role')}")
            elif t == "merge_application":
                source_id = ch.get("source_application_id")
                target_id = ch.get("target_application_id")
                updates = {
                    k: v for k, v in ch.items()
                    if k not in {"type", "source_application_id", "target_application_id"}
                    and v is not None
                }
                aid = merge_applications(source_id, target_id, updates)
                merged = get_application(aid) or {}
                app_ids.append(aid)
                done.append(
                    f"合并重复投递：{merged.get('company')} · {merged.get('role')}"
                )
            elif t == "set_application_status":
                set_application_status(ch.get("application_id"), ch.get("status"))
                done.append(f"投递状态 → {STATUS_LABEL.get(ch.get('status'), ch.get('status'))}")
            elif t == "add_interview":
                add_interview(ch.get("application_id"), ch)
                done.append(f"记录面试反馈（投递 id={ch.get('application_id')}）")
            else:
                done.append(f"（跳过未知改动：{t}）")
        except Exception as e:
            done.append(f"（失败：{t} — {str(e)[:80]}）")
    return done, app_ids, targets


def _source_backed_project_change(change, project_data=None):
    """Keep full source Markdown out of model JSON and resolve it at write time."""
    data = dict(project_data or change)
    source_ids = []
    try:
        primary_id = int(change.get("primary_source_id"))
    except (TypeError, ValueError):
        primary_id = None
    raw_supporting = change.get("supporting_source_ids") or change.get("source_ids") or []
    if not isinstance(raw_supporting, (list, tuple, set)):
        raw_supporting = [raw_supporting]
    for value in ([primary_id] if primary_id else []) + list(raw_supporting):
        try:
            sid = int(value)
        except (TypeError, ValueError):
            continue
        if sid not in source_ids:
            source_ids.append(sid)
    if primary_id:
        source = get_source(primary_id)
        if not source or not (source.get("content") or "").strip():
            if get_knowledge_item(primary_id):
                raise ValueError(
                    f"#{primary_id} 是知识文档，不是原始资料；请重新生成候选并为各项目生成独立正文"
                )
            raise ValueError(f"主资料 #{primary_id} 不存在或正文为空")
        data["document"] = source["content"]
    return data, source_ids


def _link_project_sources(project_id, source_ids):
    for index, source_id in enumerate(source_ids):
        if not get_source(source_id):
            continue
        create_source_link(
            source_id, "project", project_id,
            relation="primary_source" if index == 0 else "evidence_for",
            reason="整理入库时自动关联",
        )


# ─── 首启示例数据 ─────────────────────────────────────────────────────────────

def _seed_if_empty():
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) n FROM experiences").fetchone()["n"]
    conn.close()
    if n:
        return
    eid = create_experience({
        "company": "某互联网公司", "role": "数据分析实习生",
        "start_date": "2025-06", "end_date": "2025-09", "location": "上海"})
    create_project(eid, {
        "name": "用户留存提升项目",
        "one_liner": "通过分层运营把次留提升 6 个百分点",
        "technologies": "SQL, Python, Tableau, A/B Test",
        "keywords": "用户留存, 分层运营, AB实验, 漏斗分析",
        "document": (
            "# 用户留存提升项目\n\n"
            "> 一句话：通过用户分层 + 定向召回，把新用户次日留存从 32% 提升到 38%。\n\n"
            "## 背景\n新用户次日留存长期偏低，团队希望找到可干预的抓手。\n\n"
            "## 我做了什么\n- 拉新用户行为数据，做漏斗与分层（活跃/沉默/流失）\n"
            "- 设计 3 组召回策略并跑 A/B 实验\n- 输出留存看板供团队周会复盘\n\n"
            "## 结果\n- 次日留存 32% → 38%（+6pp）\n- 召回策略 B 显著优于对照组（p<0.05）\n\n"
            "## 待补充 / 面试可能追问\n- 留存的量化口径？（待和 AI 探讨后补全）\n")})
    create_application({
        "company": "字节跳动", "role": "数据分析师", "industry": "互联网",
        "status": "interview", "source": "Boss直聘", "applied_date": "2026-06-01"})
    create_application({
        "company": "招商银行", "role": "数据分析（金融科技）", "industry": "金融",
        "status": "screening", "source": "官网", "applied_date": "2026-06-05"})
    create_application({
        "company": "拼多多", "role": "商业分析", "industry": "互联网",
        "status": "applied", "source": "内推", "applied_date": "2026-06-10"})
