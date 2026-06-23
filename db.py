"""
Caddie - 本地数据库
数据存储在 ~/.caddie/caddie.db（SQLite）

双层雏形：
- 展示层：projects.document（Markdown，给人看、可编辑）
- 底层检索：search_index（FTS5 关键词；后续接入向量 embedding 做语义检索）
"""
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".caddie" / "caddie.db"


def get_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 允许读写并发，减少 database is locked
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

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
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
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
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            entity_type, entity_id UNINDEXED, title, content,
            tokenize='unicode61'
        )
    """)

    # 迁移：给老库补字段（已存在则忽略）
    for col in ("job_description TEXT", "gap_analysis TEXT"):
        try:
            c.execute(f"ALTER TABLE applications ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

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
    ):
        try:
            c.execute(f"ALTER TABLE job_tracks ADD COLUMN {col}")
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

    conn.commit()
    conn.close()
    _seed_if_empty()


def set_application_jd(aid, jd, analysis=None):
    conn = get_db()
    if analysis is None:
        conn.execute("UPDATE applications SET job_description=? WHERE id=?", (jd, aid))
    else:
        conn.execute("UPDATE applications SET job_description=?, gap_analysis=? WHERE id=?", (jd, analysis, aid))
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


def create_experience(d):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO experiences (company,role,start_date,end_date,location) VALUES (?,?,?,?,?)",
        (d.get("company"), d.get("role"), d.get("start_date"), d.get("end_date"), d.get("location")))
    eid = cur.lastrowid
    conn.commit(); conn.close()
    return eid


def delete_experience(eid):
    conn = get_db()
    pids = [r["id"] for r in conn.execute("SELECT id FROM projects WHERE experience_id=?", (eid,)).fetchall()]
    for pid in pids:
        conn.execute("DELETE FROM search_index WHERE entity_type='project' AND entity_id=?", (pid,))
    conn.execute("DELETE FROM projects WHERE experience_id=?", (eid,))
    conn.execute("DELETE FROM experiences WHERE id=?", (eid,))
    conn.commit(); conn.close()


# ─── 项目（文档） ─────────────────────────────────────────────────────────────

def get_project(pid):
    conn = get_db()
    p = one(conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())
    if p:
        exp = one(conn.execute("SELECT company,role FROM experiences WHERE id=?", (p["experience_id"],)).fetchone())
        p["experience"] = exp
    conn.close()
    return p


def create_project(exp_id, d):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO projects (experience_id,name,one_liner,document,technologies,keywords) VALUES (?,?,?,?,?,?)",
        (exp_id, d.get("name"), d.get("one_liner"), d.get("document"),
         d.get("technologies"), d.get("keywords")))
    pid = cur.lastrowid
    _reindex_project(conn, pid, d)
    conn.commit(); conn.close()
    return pid


def update_project(pid, d):
    conn = get_db()
    conn.execute("""
        UPDATE projects SET name=?, one_liner=?, document=?, technologies=?, keywords=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("name"), d.get("one_liner"), d.get("document"),
         d.get("technologies"), d.get("keywords"), pid))
    conn.execute("DELETE FROM search_index WHERE entity_type='project' AND entity_id=?", (pid,))
    _reindex_project(conn, pid, d)
    conn.commit(); conn.close()


def delete_project(pid):
    conn = get_db()
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    conn.execute("DELETE FROM search_index WHERE entity_type='project' AND entity_id=?", (pid,))
    conn.commit(); conn.close()


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
    content = " ".join(filter(None, [
        d.get("one_liner", ""), d.get("document", ""),
        d.get("technologies", ""), d.get("keywords", "")]))
    conn.execute(
        "INSERT INTO search_index (entity_type,entity_id,title,content) VALUES (?,?,?,?)",
        ("project", pid, d.get("name", ""), content))


# ─── 投递 ─────────────────────────────────────────────────────────────────────

STATUS_FLOW = ["applied", "screening", "written", "interview", "offer", "rejected"]
STATUS_LABEL = {
    "applied": "已投递", "screening": "筛选中", "written": "笔试",
    "interview": "面试中", "offer": "Offer", "rejected": "未通过",
}

# 作品集状态
PORTFOLIO_STATUS = {"building": "在做", "live": "已上线", "paused": "搁置"}


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
    conn = get_db()
    existing = conn.execute(
        """SELECT id FROM job_tracks
        WHERE trim(COALESCE(company,''))=trim(COALESCE(?, ''))
          AND trim(COALESCE(role,''))=trim(COALESCE(?, ''))
        ORDER BY id LIMIT 1""", (d.get("company"), d.get("role"))).fetchone()
    if existing:
        track_id = existing["id"]
    else:
        group = _infer_track_group(d)
        tcur = conn.execute(
            """INSERT INTO job_tracks
            (company,role,target,jd,status,notes,track_group,priority)
            VALUES (?,?,?,?,?,?,?,?)""",
            (d.get("company"), d.get("role"), d.get("industry"),
             d.get("job_description"), "active", "由投递自动建立",
             group, "normal"))
        track_id = tcur.lastrowid
    cur = conn.execute(
        """INSERT INTO applications
        (company,role,industry,applied_date,status,source,notes,track_id)
        VALUES (?,?,?,?,?,?,?,?)""",
        (d.get("company"), d.get("role"), d.get("industry"),
         d.get("applied_date", datetime.now().strftime("%Y-%m-%d")),
         d.get("status", "applied"), d.get("source"), d.get("notes"), track_id))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    return aid


def update_application(aid, d):
    conn = get_db()
    current = one(conn.execute(
        "SELECT track_id,job_description FROM applications WHERE id=?", (aid,)).fetchone()) or {}
    conn.execute("""UPDATE applications SET company=?,role=?,industry=?,applied_date=?,
        status=?,source=?,notes=?,updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("company"), d.get("role"), d.get("industry"), d.get("applied_date"),
         d.get("status"), d.get("source"), d.get("notes"), aid))
    if current.get("track_id"):
        conn.execute(
            """UPDATE job_tracks SET company=?,role=?,target=?,
            track_group=COALESCE(track_group,?),
            jd=COALESCE(NULLIF(?,''),jd),updated_at=datetime('now','localtime')
            WHERE id=?""",
            (d.get("company"), d.get("role"), d.get("industry"),
             _infer_track_group(d), current.get("job_description") or "",
             current["track_id"]))
    conn.commit(); conn.close()


def _infer_track_group(d):
    """给具体岗位一个可编辑的初始板块，不替用户做过细分类。"""
    text = " ".join(str(d.get(k) or "") for k in
                    ("role", "industry", "target", "job_description"))
    if any(x.lower() in text.lower() for x in
           ("ai", "人工智能", "大模型", "算法", "智能体", "agent")):
        return "AI 相关"
    if any(x in text for x in ("数据", "商业分析", "经营分析")):
        return "数据与分析"
    if any(x in text for x in ("金融", "证券", "银行", "保险")):
        return "金融相关"
    return "其他岗位"


def ensure_application_tracks():
    """让每条具体投递天然拥有一条求职线，并兼容已有未关联数据。"""
    conn = get_db()
    apps = rows(conn.execute(
        """SELECT id,company,role,industry,job_description,track_id
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
            cur = conn.execute(
                """INSERT INTO job_tracks
                (company,role,target,jd,status,notes,track_group,priority)
                VALUES (?,?,?,?,?,?,?,?)""",
                (app.get("company"), app.get("role"), app.get("industry"),
                 app.get("job_description"), "active", "由投递自动建立",
                 _infer_track_group(app), "normal"))
            tid = cur.lastrowid
        conn.execute(
            "UPDATE applications SET track_id=?,updated_at=datetime('now','localtime') WHERE id=?",
            (tid, app["id"]))
        changed += 1
    if changed:
        conn.commit()
    conn.close()
    return changed


def set_application_status(aid, status):
    conn = get_db()
    conn.execute("UPDATE applications SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                 (status, aid))
    conn.commit(); conn.close()


def set_application_track(aid, track_id):
    """把一条投递关联到某个求职目标（track_id=None 解除关联）。"""
    conn = get_db()
    conn.execute("UPDATE applications SET track_id=?, updated_at=datetime('now','localtime') WHERE id=?",
                 (track_id, aid))
    conn.commit(); conn.close()


def delete_application(aid):
    conn = get_db()
    conn.execute("DELETE FROM applications WHERE id=?", (aid,))
    conn.commit(); conn.close()


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
        SELECT s.id, s.title, s.updated_at,
            (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS n
        FROM sessions s ORDER BY s.updated_at DESC""").fetchall()
    conn.close()
    return rows(r)


def create_session(sid, title="新对话"):
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO sessions (id,title) VALUES (?,?)", (sid, title))
    conn.commit(); conn.close()


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


def delete_session(sid):
    conn = get_db()
    conn.execute("DELETE FROM chat_messages WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit(); conn.close()


def save_message(session_id, role, content):
    conn = get_db()
    conn.execute("INSERT INTO chat_messages (session_id,role,content) VALUES (?,?,?)",
                 (session_id, role, content))
    conn.commit(); conn.close()


def get_chat_history(session_id, limit=30):
    conn = get_db()
    r = conn.execute(
        "SELECT role,content FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit)).fetchall()
    conn.close()
    return list(reversed(rows(r)))


def clear_chat(session_id):
    conn = get_db()
    conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
    conn.commit(); conn.close()


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


def update_interview(iid, d):
    conn = get_db()
    conn.execute("""UPDATE interviews SET round_number=?,round_type=?,interview_date=?,
        duration_minutes=?,location=?,meeting_link=?,notes=?,feedback=?,outcome=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("round_number"), d.get("round_type"), d.get("interview_date"),
         d.get("duration_minutes") or 60, d.get("location"),
         d.get("meeting_link"), d.get("notes"), d.get("feedback"),
         d.get("outcome"), iid))
    conn.commit(); conn.close()


def delete_interview(iid):
    conn = get_db()
    conn.execute("DELETE FROM interviews WHERE id=?", (iid,))
    conn.commit(); conn.close()


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
    return result


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
    cur = conn.execute(
        "INSERT INTO interview_items (kind,title,target,content,project_id) VALUES (?,?,?,?,?)",
        (d.get("kind"), d.get("title"), d.get("target"), d.get("content"), d.get("project_id")))
    iid = cur.lastrowid
    conn.commit(); conn.close()
    return iid


def update_interview_item(iid, d):
    conn = get_db()
    conn.execute("""UPDATE interview_items SET title=?,target=?,content=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("title"), d.get("target"), d.get("content"), iid))
    conn.commit(); conn.close()


def delete_interview_item(iid):
    conn = get_db()
    conn.execute("DELETE FROM interview_items WHERE id=?", (iid,))
    conn.commit(); conn.close()


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
            conn.execute("""
                UPDATE projects SET name=?,one_liner=?,document=?,technologies=?,keywords=?,
                    updated_at=datetime('now','localtime') WHERE id=?""",
                (merged["name"], merged["one_liner"], merged["document"],
                 merged["technologies"], merged["keywords"], project_id))
            conn.execute("DELETE FROM search_index WHERE entity_type='project' AND entity_id=?", (project_id,))
            _reindex_project(conn, project_id, merged)
            pid = project_id
        else:
            cur = conn.execute(
                "INSERT INTO projects (experience_id,name,one_liner,document,technologies,keywords) VALUES (?,?,?,?,?,?)",
                (experience_id, digest.get("title") or "日记提炼项目",
                 digest.get("one_liner"), digest.get("document"),
                 digest.get("technologies"), digest.get("keywords")),
            )
            pid = cur.lastrowid
            _reindex_project(conn, pid, {
                "name": digest.get("title") or "日记提炼项目",
                "one_liner": digest.get("one_liner"),
                "document": digest.get("document"),
                "technologies": digest.get("technologies"),
                "keywords": digest.get("keywords"),
            })

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
        r = conn.execute("""SELECT id,source_type,title,file_name,summary,status,created_at,updated_at
            FROM sources WHERE source_type=? ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (source_type, limit)).fetchall()
    else:
        r = conn.execute("""SELECT id,source_type,title,file_name,summary,status,created_at,updated_at
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
        (source_type,title,content,file_name,file_path,summary,analysis_json,status)
        VALUES (?,?,?,?,?,?,?,?)""",
        (d.get("source_type") or "other", d.get("title"), d.get("content"),
         d.get("file_name"), d.get("file_path"), d.get("summary"),
         d.get("analysis_json"), d.get("status") or "raw"))
    sid = cur.lastrowid
    conn.commit(); conn.close()
    return sid


def update_source_analysis(sid, d):
    conn = get_db()
    conn.execute("""UPDATE sources SET source_type=?,title=?,summary=?,analysis_json=?,
        status=?, updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("source_type") or "other", d.get("title"), d.get("summary"),
         d.get("analysis_json"), d.get("status") or "analyzed", sid))
    conn.commit(); conn.close()


def delete_source(sid):
    conn = get_db()
    conn.execute("DELETE FROM sources WHERE id=?", (sid,))
    conn.commit(); conn.close()


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


def list_job_tracks():
    ensure_application_tracks()
    conn = get_db()
    r = conn.execute("""
        SELECT jt.*,a.id AS application_id,a.status AS application_status,
               a.applied_date,a.source AS application_source,
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
    cur = conn.execute("""INSERT INTO job_tracks
        (company,role,target,jd,status,notes,track_group,priority)
        VALUES (?,?,?,?,?,?,?,?)""",
        (d.get("company"), d.get("role"), d.get("target"), d.get("jd"),
         d.get("status") or "active", d.get("notes"), d.get("track_group"),
         d.get("priority") or "normal"))
    jid = cur.lastrowid
    conn.commit(); conn.close()
    return jid


def update_job_track(tid, d):
    conn = get_db()
    conn.execute("""UPDATE job_tracks SET company=?, role=?, target=?, jd=?, status=?,
        notes=?, persona=?, priority=?, track_group=?,
        updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("company"), d.get("role"), d.get("target"), d.get("jd"),
         d.get("status") or "active", d.get("notes"), d.get("persona"),
         d.get("priority") or "normal", d.get("track_group"), tid))
    conn.commit(); conn.close()


def delete_job_track(tid):
    conn = get_db()
    conn.execute("DELETE FROM track_gaps WHERE track_id=?", (tid,))
    conn.execute("UPDATE applications SET track_id=NULL WHERE track_id=?", (tid,))
    conn.execute("DELETE FROM job_tracks WHERE id=?", (tid,))
    conn.commit(); conn.close()


# ─── 求职目标：差距清单 ───────────────────────────────────────────────────────

def list_track_gaps(track_id):
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM track_gaps WHERE track_id=? ORDER BY "
        "CASE severity WHEN 'blocker' THEN 0 WHEN 'fixable' THEN 1 ELSE 2 END, id", (track_id,)).fetchall()
    conn.close()
    return rows(r)


def create_track_gap(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO track_gaps
        (track_id,dimension,requirement,my_status,severity,plan_type,plan_ref_type,plan_ref_id,status,note)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (d.get("track_id"), d.get("dimension"), d.get("requirement"),
         d.get("my_status") or "missing", d.get("severity") or "fixable",
         d.get("plan_type") or "none", d.get("plan_ref_type"), d.get("plan_ref_id"),
         d.get("status") or "todo", d.get("note")))
    gid = cur.lastrowid
    conn.commit(); conn.close()
    _recompute_readiness(d.get("track_id"))
    return gid


def update_track_gap(gid, d):
    conn = get_db()
    row = one(conn.execute("SELECT track_id FROM track_gaps WHERE id=?", (gid,)).fetchone())
    fields = ["dimension", "requirement", "my_status", "severity", "plan_type",
              "plan_ref_type", "plan_ref_id", "status", "note"]
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
    rows_ = conn.execute("SELECT status FROM track_gaps WHERE track_id=?", (track_id,)).fetchall()
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


def delete_asset(aid):
    conn = get_db()
    conn.execute("DELETE FROM assets WHERE id=?", (aid,))
    conn.commit(); conn.close()


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


def create_feedback_note(d):
    conn = get_db()
    cur = conn.execute("""INSERT INTO feedback_notes
        (scope,scope_id,note_type,content,category,polarity,strength,directive,original_text,source_id,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("scope") or "global", d.get("scope_id"), d.get("note_type") or "feedback",
         d.get("content"), d.get("category"), d.get("polarity"), d.get("strength"),
         d.get("directive"), d.get("original_text"), d.get("source_id"),
         d.get("status") or "active"))
    fid = cur.lastrowid
    conn.commit(); conn.close()
    return fid


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
    try:
        terms = " OR ".join(f'"{t}"*' for t in q.split() if t)
        r = conn.execute("""
            SELECT entity_type, entity_id, title,
                snippet(search_index,3,'《','》','…',18) snippet
            FROM search_index WHERE search_index MATCH ? ORDER BY rank LIMIT ?""",
            (terms or q + "*", limit)).fetchall()
    except Exception:
        like = f"%{q}%"
        r = conn.execute("""SELECT entity_type,entity_id,title,content snippet
            FROM search_index WHERE title LIKE ? OR content LIKE ? LIMIT ?""",
            (like, like, limit)).fetchall()
    conn.close()
    return rows(r)


# ─── 应用 AI 提议的结构化改动 ────────────────────────────────────────────────

def apply_changes(changes: list):
    """执行一组改动，返回 (results描述列表, created_app_ids列表)。"""
    done = []
    app_ids = []  # 新建的 application id，供调用方存 JD 用
    for ch in changes:
        t = ch.get("type")
        try:
            if t == "create_experience":
                eid = create_experience(ch)
                done.append(f"新增经历：{ch.get('company')} · {ch.get('role')}")
                for p in ch.get("projects", []) or []:
                    create_project(eid, p)
                    done.append(f"  └ 新增项目：{p.get('name')}")
            elif t == "create_project":
                create_project(ch.get("experience_id"), ch)
                done.append(f"新增项目：{ch.get('name')}")
            elif t == "update_project":
                cur = get_project(ch.get("project_id")) or {}
                merged = {
                    "name": ch.get("name") or cur.get("name"),
                    "one_liner": ch.get("one_liner") or cur.get("one_liner"),
                    "document": ch.get("document") or cur.get("document"),
                    "technologies": ch.get("technologies") or cur.get("technologies"),
                    "keywords": ch.get("keywords") or cur.get("keywords"),
                }
                update_project(ch.get("project_id"), merged)
                done.append(f"更新项目文档：{merged['name']}")
            elif t == "create_followup":
                create_followup(ch.get("project_id"), ch)
                done.append(f"新增追问：{(ch.get('question') or '')[:36]}")
            elif t == "create_application":
                aid = create_application(ch)
                app_ids.append(aid)
                done.append(f"新增投递：{ch.get('company')} · {ch.get('role')}")
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
    return done, app_ids


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
