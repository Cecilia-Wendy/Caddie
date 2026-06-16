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
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
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

    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            entity_type, entity_id UNINDEXED, title, content,
            tokenize='unicode61'
        )
    """)

    conn.commit()
    conn.close()
    _seed_if_empty()


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


def get_applications():
    conn = get_db()
    apps = rows(conn.execute("SELECT * FROM applications ORDER BY applied_date DESC").fetchall())
    for a in apps:
        a["interviews"] = rows(conn.execute(
            "SELECT * FROM interviews WHERE application_id=? ORDER BY round_number", (a["id"],)).fetchall())
    conn.close()
    return apps


def create_application(d):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO applications (company,role,industry,applied_date,status,source,notes) VALUES (?,?,?,?,?,?,?)",
        (d.get("company"), d.get("role"), d.get("industry"),
         d.get("applied_date", datetime.now().strftime("%Y-%m-%d")),
         d.get("status", "applied"), d.get("source"), d.get("notes")))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    return aid


def update_application(aid, d):
    conn = get_db()
    conn.execute("""UPDATE applications SET company=?,role=?,industry=?,applied_date=?,
        status=?,source=?,notes=?,updated_at=datetime('now','localtime') WHERE id=?""",
        (d.get("company"), d.get("role"), d.get("industry"), d.get("applied_date"),
         d.get("status"), d.get("source"), d.get("notes"), aid))
    conn.commit(); conn.close()


def set_application_status(aid, status):
    conn = get_db()
    conn.execute("UPDATE applications SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                 (status, aid))
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


# ─── 面试记录 ─────────────────────────────────────────────────────────────────

def add_interview(app_id, d):
    conn = get_db()
    last = conn.execute("SELECT MAX(round_number) n FROM interviews WHERE application_id=?",
                        (app_id,)).fetchone()["n"] or 0
    conn.execute("""INSERT INTO interviews (application_id,round_number,round_type,interview_date,feedback,outcome)
        VALUES (?,?,?,?,?,?)""",
        (app_id, last + 1, d.get("round_type"), d.get("interview_date"),
         d.get("feedback"), d.get("outcome")))
    conn.commit(); conn.close()


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

def apply_changes(changes: list) -> list:
    """执行一组改动，返回每条的结果描述。"""
    done = []
    for ch in changes:
        t = ch.get("type")
        try:
            if t == "create_experience":
                eid = create_experience(ch)
                done.append(f"新增经历：{ch.get('company')} · {ch.get('role')}")
                # 允许顺带带项目
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
            elif t == "create_application":
                create_application(ch)
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
    return done


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
