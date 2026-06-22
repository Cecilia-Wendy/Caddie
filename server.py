"""
Caddie - FastAPI 后端
"""
import uuid
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import db
import ai
import vcs
import context as caddie_context

app = FastAPI(title="Caddie")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup():
    db.init_db()
    vcs.ensure_repo()   # 初始化版本仓库并回填一条初始记录
    linked = db.ensure_application_tracks()
    if linked:
        _commit(f"自动建立并关联 {linked} 条岗位求职线")


def _commit(msg: str):
    """每次改动后自动提交一条版本记录。"""
    vcs.sync_and_commit(msg)


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ─── Schemas ──────────────────────────────────────────────────────────────────

class ProviderIn(BaseModel):
    id: Optional[str] = None
    name: str
    type: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""


class ChatIn(BaseModel):
    message: str
    session_id: Optional[str] = None


class ProposeIn(BaseModel):
    session_id: str


class ProjChatIn(BaseModel):
    message: str
    mode: str = "organize"   # organize（直接整理，少问）| ask（追问深挖）


class ApplyIn(BaseModel):
    changes: list


class ExperienceIn(BaseModel):
    company: str
    role: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    location: Optional[str] = None


class ProjectIn(BaseModel):
    name: str
    one_liner: Optional[str] = None
    document: Optional[str] = None
    technologies: Optional[str] = None
    keywords: Optional[str] = None


class PortfolioIn(BaseModel):
    name: str
    one_liner: Optional[str] = None
    document: Optional[str] = None
    technologies: Optional[str] = None
    keywords: Optional[str] = None
    repo_url: Optional[str] = None
    demo_url: Optional[str] = None
    cover_source_id: Optional[int] = None
    build_status: Optional[str] = None


class PortfolioMetaIn(BaseModel):
    repo_url: Optional[str] = None
    demo_url: Optional[str] = None
    cover_source_id: Optional[int] = None
    build_status: Optional[str] = None


class PortfolioDraftIn(BaseModel):
    github_url: Optional[str] = None
    doc_text: Optional[str] = None


class FocusIngestIn(BaseModel):
    transcript: str
    scope: Optional[str] = "portfolio"


class FocusApplyIn(BaseModel):
    items: list
    scope: Optional[str] = "portfolio"


class ApplicationIn(BaseModel):
    company: str
    role: str
    industry: Optional[str] = None
    applied_date: Optional[str] = None
    status: Optional[str] = "applied"
    source: Optional[str] = None
    notes: Optional[str] = None


class InterviewScheduleIn(BaseModel):
    round_number: Optional[int] = None
    round_type: Optional[str] = None
    interview_date: str
    duration_minutes: Optional[int] = 60
    location: Optional[str] = None
    meeting_link: Optional[str] = None
    notes: Optional[str] = None
    feedback: Optional[str] = None
    outcome: Optional[str] = None


class SourceIn(BaseModel):
    title: Optional[str] = None
    source_type: str = "other"
    content: str


class JobTrackIn(BaseModel):
    company: Optional[str] = None
    role: Optional[str] = None
    track_group: Optional[str] = None
    target: Optional[str] = None
    jd: Optional[str] = None
    status: str = "active"
    notes: Optional[str] = None
    persona: Optional[str] = None
    priority: Optional[str] = "normal"


class TrackGapIn(BaseModel):
    track_id: Optional[int] = None
    dimension: Optional[str] = None
    requirement: Optional[str] = None
    my_status: Optional[str] = "missing"
    severity: Optional[str] = "fixable"
    plan_type: Optional[str] = "none"
    plan_ref_type: Optional[str] = None
    plan_ref_id: Optional[int] = None
    status: Optional[str] = "todo"
    note: Optional[str] = None


class AssetIn(BaseModel):
    asset_type: str = "other"
    track_id: Optional[int] = None
    project_id: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None
    status: str = "draft"


# ─── AI 供应商设置（本 demo 的主角）──────────────────────────────────────────

@app.get("/api/providers")
def providers():
    return ai.list_providers_masked()


@app.post("/api/providers")
def save_provider(body: ProviderIn):
    return ai.upsert_provider(body.dict())


@app.delete("/api/providers/{pid}")
def remove_provider(pid: str):
    ai.delete_provider(pid)
    return {"ok": True}


@app.post("/api/providers/{pid}/activate")
def activate_provider(pid: str):
    ai.set_active(pid)
    return {"ok": True}


@app.post("/api/providers/test")
def test_provider(body: ProviderIn):
    """用表单当前内容直接测连通（未保存也能测）。若没填 key 则用已存的。"""
    p = body.dict()
    if not p.get("api_key") and p.get("id"):
        stored = next((x for x in ai.load_config().get("providers", []) if x["id"] == p["id"]), None)
        if stored:
            p["api_key"] = stored.get("api_key", "")
    return ai.test_connection(p)


# ─── 经历 / 项目 ──────────────────────────────────────────────────────────────

@app.get("/api/experiences")
def list_experiences():
    return db.get_experiences()


@app.post("/api/experiences")
def add_experience(body: ExperienceIn):
    eid = db.create_experience(body.dict())
    _commit(f"新增经历：{body.company} · {body.role}")
    return {"id": eid}


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


@app.delete("/api/experiences/{eid}")
def del_experience(eid: int):
    db.delete_experience(eid)
    _commit("删除一段经历及其项目")
    return {"ok": True}


@app.post("/api/experiences/{eid}/projects")
def add_project(eid: int, body: ProjectIn):
    pid = db.create_project(eid, body.dict())
    _commit(f"新增项目：{body.name}")
    return {"id": pid}


@app.put("/api/projects/{pid}")
def edit_project(pid: int, body: ProjectIn):
    db.update_project(pid, body.dict())
    _commit(f"编辑项目文档：{body.name}")
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def del_project(pid: int):
    p = db.get_project(pid) or {}
    db.delete_project(pid)
    _commit(f"删除项目：{p.get('name','')}")
    return {"ok": True}


# ─── 作品集（个人项目） ───────────────────────────────────────────────────────

@app.get("/api/portfolio")
def list_portfolio():
    return {"items": db.list_portfolio(), "build_status": db.PORTFOLIO_STATUS}


@app.post("/api/portfolio")
def add_portfolio(body: PortfolioIn):
    pid = db.create_portfolio_project(body.dict())
    _commit(f"新增作品：{body.name}")
    return {"id": pid}


@app.put("/api/portfolio/{pid}/meta")
def edit_portfolio_meta(pid: int, body: PortfolioMetaIn):
    db.update_portfolio_meta(pid, body.dict())
    p = db.get_project(pid) or {}
    _commit(f"更新作品信息：{p.get('name','')}")
    return {"ok": True}


def _fetch_github_context(url: str):
    """抓取公开 GitHub 仓库的简介/语言/README，拼成 AI 可读的材料。"""
    import base64

    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)", url or "")
    if not m:
        return None
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    H = {"User-Agent": "Caddie", "Accept": "application/vnd.github+json"}
    info, langs, readme = {}, {}, ""
    try:
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}", headers=H, timeout=15)
        if r.status_code == 200:
            info = r.json()
    except Exception:
        pass
    try:
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/languages", headers=H, timeout=10)
        if r.status_code == 200:
            langs = r.json()
    except Exception:
        pass
    # GitHub 的 README API 会自动识别文件名和默认分支，比逐个请求 raw/HEAD 稳定且更快。
    try:
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/readme", headers=H, timeout=15)
        if r.status_code == 200:
            payload = r.json()
            encoded = payload.get("content") or ""
            if payload.get("encoding") == "base64" and encoded:
                readme = base64.b64decode(encoded).decode("utf-8", errors="ignore")[:8000]
    except Exception:
        pass
    if not info and not readme:
        return None
    parts = [f"GitHub 仓库：{owner}/{repo}"]
    if info.get("description"):
        parts.append("仓库简介：" + info["description"])
    if info.get("homepage"):
        parts.append("项目主页：" + info["homepage"])
    if langs:
        parts.append("语言构成：" + ", ".join(langs.keys()))
    if info.get("topics"):
        parts.append("标签：" + ", ".join(info["topics"]))
    if readme:
        parts.append("README 内容：\n" + readme)
    return {"text": "\n".join(parts),
            "repo_url": info.get("html_url") or url,
            "demo_url": info.get("homepage") or ""}


PORTFOLIO_DRAFT_PROMPT = """你是作品集助手。根据用户提供的项目材料（GitHub 信息或需求/技术文档），提炼成一个【个人作品】条目。
材料可能是已完成的项目，也可能是还没动手、只有需求/设计文档的项目——都照样提炼，没做的就按规划来写，并把状态标成在做。
返回严格 JSON，不要加 markdown 代码块：
{
 "name": "作品名称（简洁）",
 "one_liner": "一句话亮点，口语化、说人话、别堆术语，突出它解决什么问题或有什么意思",
 "technologies": "用到的技术，逗号分隔，写通俗常见的名字（如 React, Python, Claude API）",
 "document": "结构化介绍，用这几个二级标题：## 这是什么\\n## 做了什么（或：计划做什么）\\n## 技术亮点\\n## 现状。通俗中文，到面试能讲出来的程度",
 "build_status": "building 在做 / live 已上线 / paused 搁置，从材料判断，拿不准就 building"
}"""


@app.post("/api/portfolio/draft")
def portfolio_draft(body: PortfolioDraftIn):
    repo_url = (body.github_url or "").strip()
    demo_url = ""
    if repo_url:
        ctx = _fetch_github_context(repo_url)
        if not ctx:
            raise HTTPException(400, "GitHub 链接解析失败，确认是 https://github.com/用户/仓库 这种格式，且仓库是公开的")
        material, repo_url, demo_url = ctx["text"], ctx["repo_url"], ctx["demo_url"]
    elif (body.doc_text or "").strip():
        material = body.doc_text.strip()[:8000]
    else:
        raise HTTPException(400, "给个 GitHub 链接，或粘贴一段项目文档")
    raw = ai.chat([{"role": "user", "content": material}], system=PORTFOLIO_DRAFT_PROMPT, max_tokens=1500)
    data = ai.extract_json(raw)
    if not isinstance(data, dict):
        raise HTTPException(500, "AI 解析失败，请重试")
    data.setdefault("build_status", "building")
    data["repo_url"] = repo_url
    data["demo_url"] = data.get("demo_url") or demo_url
    return data


# ─── 面试关注点库（自迭代 prompt 内容层） ─────────────────────────────────────

FOCUS_INGEST_PROMPT = """你在维护一个【面试关注点库】：记录面试官评价"个人作品/项目"时真正在意什么，用来指导以后怎么把作品写得能扛住面试。

给你两样东西：① 现有关注点库；② 一份新的面试逐字稿。
任务：从逐字稿里提炼面试官的关注点（尤其是反复追问、犀利质疑的点），与现有库合并去重，输出更新后的【完整】关注点库。

规则：
1. 关注点写成"指导写作的指令"，例如"主动说明这个作品为什么必须单独做、跟现成工具的区别"，而不是干巴巴的标签。
2. 同一关注点在多处反复出现 → strength 设 'hard'（铁律级），否则 'soft'。
3. 既有库里已有的别重复；若新逐字稿强化了它，可升级 strength 并补充 evidence。
4. 每条带一句原话佐证（evidence），方便用户核对。
5. 控制在 10 条以内，最重要的在前。

只返回 JSON，不要 markdown：
{"focus_points":[
  {"dimension":"必要性/差异化","directive":"主动说明为什么要单独做这个作品、跟现成工具(ChatGPT/Notion等)的区别","strength":"hard","evidence":"随便找个大模型APP都能讨论，你优势在哪"}
]}"""


@app.get("/api/focus-points")
def get_focus_points(scope: str = "portfolio"):
    return {"items": db.list_focus_points(scope)}


@app.post("/api/focus-points/ingest")
def ingest_focus_points(body: FocusIngestIn):
    scope = body.scope or "portfolio"
    existing = db.list_focus_points(scope)
    ex_text = "\n".join(
        f"- [{n.get('category') or ''}|{n.get('strength') or 'soft'}] {n.get('directive')}"
        for n in existing) or "（库为空）"
    user_msg = f"现有关注点库：\n{ex_text}\n\n新的面试逐字稿：\n{(body.transcript or '')[:16000]}"
    raw = ai.chat([{"role": "user", "content": user_msg}], system=FOCUS_INGEST_PROMPT, max_tokens=1800)
    data = ai.extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("focus_points"), list):
        raise HTTPException(500, "AI 提炼失败，请重试")
    return {"items": data["focus_points"], "previous_count": len(existing)}


@app.post("/api/focus-points/apply")
def apply_focus_points(body: FocusApplyIn):
    db.replace_focus_points(body.items, body.scope or "portfolio")
    _commit("更新面试关注点库")
    return {"ok": True, "count": len(body.items)}


# ─── 投递 ─────────────────────────────────────────────────────────────────────

@app.get("/api/applications")
def list_applications():
    return {"items": db.get_applications(), "labels": db.STATUS_LABEL, "flow": db.STATUS_FLOW}


@app.post("/api/applications")
def add_application(body: ApplicationIn):
    aid = db.create_application(body.dict())
    _commit(f"新增投递：{body.company} · {body.role}")
    return {"id": aid}


@app.put("/api/applications/{aid}")
def edit_application(aid: int, body: ApplicationIn):
    db.update_application(aid, body.dict())
    _commit(f"编辑投递：{body.company}")
    return {"ok": True}


@app.post("/api/applications/{aid}/status")
def move_application(aid: int, body: dict):
    db.set_application_status(aid, body.get("status"))
    _commit(f"投递状态更新 → {db.STATUS_LABEL.get(body.get('status'), body.get('status'))}")
    return {"ok": True}


@app.delete("/api/applications/{aid}")
def del_application(aid: int):
    db.delete_application(aid)
    _commit("删除一条投递")
    return {"ok": True}


@app.post("/api/applications/{aid}/interviews")
def add_interview_schedule(aid: int, body: InterviewScheduleIn):
    app_ = db.get_application(aid)
    if not app_:
        raise HTTPException(404, "投递不存在")
    iid = db.add_interview(aid, body.dict())
    if app_.get("status") in ("applied", "screening", "written"):
        db.set_application_status(aid, "interview")
    _commit(f"安排面试：{app_.get('company','')} · {app_.get('role','')}")
    return {"id": iid}


@app.put("/api/interviews/{iid}")
def edit_interview_schedule(iid: int, body: InterviewScheduleIn):
    db.update_interview(iid, body.dict())
    _commit("更新面试安排")
    return {"ok": True}


@app.delete("/api/interviews/{iid}")
def delete_interview_schedule(iid: int):
    db.delete_interview(iid)
    _commit("删除面试安排")
    return {"ok": True}


@app.get("/api/calendar")
def calendar(date_from: Optional[str] = None, date_to: Optional[str] = None):
    return {"items": db.list_interview_schedule(date_from, date_to)}


@app.get("/api/dashboard")
def dashboard():
    return db.dashboard_stats()


# ─── 检索 ─────────────────────────────────────────────────────────────────────

@app.get("/api/search")
def search(q: str = ""):
    if not q.strip():
        return []
    return db.search(q)


# ─── 会话 ─────────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
def get_sessions():
    return db.list_sessions()


@app.post("/api/sessions")
def new_session():
    sid = uuid.uuid4().hex
    db.create_session(sid)
    return {"id": sid}


@app.get("/api/sessions/{sid}/messages")
def session_messages(sid: str):
    return db.get_chat_history(sid, limit=200)


@app.delete("/api/sessions/{sid}")
def remove_session(sid: str):
    db.delete_session(sid)
    return {"ok": True}


# ─── 对话 ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是 Caddie，用户的本地求职管家。你的职责：
1. 帮用户深挖、记录项目经历，把模糊的描述追问成可写进简历的细节（背景-行动-量化结果）。
2. 基于用户已有的经历和项目回答问题，帮他准备面试。
3. 分析面试反馈，指出答得薄弱的地方，给出更好的回答思路。

重要规则：你【不能】直接修改用户的数据库（项目文档、投递看板都不归你直接改）。
所以【绝对不要】声称"已更新看板/已更新文档/已保存"这类话——那是假的。
当对话里出现可以入库的信息（新项目细节、新投递、面试反馈、状态变化）时，
你只需自然地接着聊，并在合适处提醒一句："这些我可以帮你整理入库，点下方『整理入库』我来生成更新建议，你确认后才会真正写入。"
用中文回答，专业、直接、不啰嗦。"""


def _experience_context() -> str:
    exps = db.get_experiences()
    if not exps:
        return "\n\n（用户还没有录入任何经历。）"
    s = "\n\n【用户已有经历概览——这是你的跨对话记忆，任何会话都基于它】\n"
    for e in exps:
        s += f"- {e['company']} | {e['role']}（{e.get('start_date','')}~{e.get('end_date','至今')}）\n"
        for p in e.get("projects", []):
            s += f"    · 项目：{p['name']} — {p.get('one_liner') or ''}\n"
    return s


@app.post("/api/chat")
def chat(body: ChatIn):
    session_id = body.session_id or uuid.uuid4().hex
    db.create_session(session_id)
    history = db.get_chat_history(session_id, limit=16)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": body.message})
    system = SYSTEM_PROMPT + _experience_context()

    try:
        reply = ai.chat(messages, system=system, max_tokens=2048)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"调用 AI 失败：{str(e)[:300]}")

    db.save_message(session_id, "user", body.message)
    db.save_message(session_id, "assistant", reply)
    db.touch_session(session_id, title=body.message)
    return {"reply": reply, "session_id": session_id}


# ─── 对话 → 真落库（核心：AI 提议结构化改动，用户确认后写入）────────────────

PROPOSE_PROMPT = """你是 Caddie 的「入库整理器」。根据下面这段对话和当前数据状态，
提取出应该写入数据库的结构化改动。只针对对话里【明确出现的新信息】，不要编造。

可用的改动类型（type）：
- create_experience: 字段 company, role, start_date, end_date；可选 projects:[{name,one_liner,document,technologies,keywords}]
- create_project: 字段 experience_id（必须引用下面快照里的真实 id）, name, one_liner, document(完整 Markdown), technologies, keywords
- update_project: 字段 project_id（真实 id）, 以及要更新的字段；document 请给【完整的新版 Markdown】（在原文基础上补充，不要删掉已有内容）
- create_application: 字段 company, role, industry, status(applied/screening/written/interview/offer/rejected), source
- set_application_status: 字段 application_id（真实 id）, status
- add_interview: 字段 application_id（真实 id）, round_type, feedback, outcome

%s

对话内容：
%s

只返回如下 JSON，不要任何多余文字：
{
  "summary": "一句话说明这次要改什么；如果没有任何需要入库的信息就写：无",
  "changes": [ { "type": "...", ... } ]
}
若没有需要入库的内容，changes 返回空数组 []。"""


@app.post("/api/chat/propose")
def propose(body: ProposeIn):
    history = db.get_chat_history(body.session_id, limit=40)
    if not history:
        return {"summary": "无", "changes": []}
    convo = "\n".join(f"{'用户' if m['role']=='user' else 'Caddie'}：{m['content']}" for m in history)
    prompt = PROPOSE_PROMPT % (db.snapshot_for_ai(), convo[:8000])
    try:
        raw = ai.chat([{"role": "user", "content": prompt}], max_tokens=3000)
        data = ai.extract_json(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"整理失败（模型没返回合规 JSON）：{str(e)[:200]}")
    data.setdefault("summary", "")
    data.setdefault("changes", [])
    return data


@app.post("/api/changes/apply")
def apply_changes(body: ApplyIn):
    results, app_ids = db.apply_changes(body.changes)
    _commit("整理入库：" + "；".join(results)[:80] if results else "整理入库")
    return {"ok": True, "results": results, "ids": app_ids}


# ─── 版本管理（本地 Git，自动提交）──────────────────────────────────────────

@app.get("/api/vcs/log")
def vcs_log():
    return {"status": vcs.status_summary(), "log": vcs.log()}


@app.get("/api/vcs/show/{h}")
def vcs_show(h: str):
    return vcs.show(h)


@app.post("/api/vcs/push")
def vcs_push():
    vcs.push()
    return {"ok": True, "status": vcs.status_summary()}


@app.get("/api/projects/{pid}/history")
def project_history(pid: int):
    return vcs.project_history(pid)


@app.post("/api/projects/{pid}/restore")
def project_restore(pid: int, body: dict):
    cur = db.get_project(pid)
    if not cur:
        raise HTTPException(404, "项目不存在")
    old = vcs.project_doc_at(pid, body.get("hash", ""))
    if not old:
        raise HTTPException(400, "找不到该历史版本")
    db.update_project(pid, {
        "name": cur.get("name"),
        "one_liner": old.get("one_liner") or cur.get("one_liner"),
        "document": old.get("document"),
        "technologies": old.get("technologies") or cur.get("technologies"),
        "keywords": old.get("keywords") or cur.get("keywords"),
    })
    _commit(f"回滚项目「{cur.get('name','')}」到历史版本 {body.get('hash','')[:7]}")
    return {"ok": True}


# ─── 个人网页：按经历改，只做文本替换，不动风格 ──────────────────────────────

SITE_HTML = ai.CONFIG_DIR / "site.html"
SITE_META = ai.CONFIG_DIR / "site_meta.json"


class SiteLoadIn(BaseModel):
    source_type: str   # path | url | inline
    value: str


class SiteInstructIn(BaseModel):
    instruction: str = ""


class SiteApplyIn(BaseModel):
    edits: list


def _site_html() -> str:
    return SITE_HTML.read_text(encoding="utf-8") if SITE_HTML.exists() else ""


def _site_meta() -> dict:
    if SITE_META.exists():
        try:
            return json.loads(SITE_META.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


@app.get("/api/site")
def site_get():
    return {"html": _site_html(), "meta": _site_meta()}


@app.post("/api/site/load")
def site_load(body: SiteLoadIn):
    html = ""
    meta = {}
    if body.source_type == "path":
        raw = body.value.strip().strip('"').strip("'")
        p = Path(raw.replace("~", str(Path.home()), 1) if raw.startswith("~") else raw)
        if not p.exists():
            raise HTTPException(400, f"找不到文件：{p}")
        html = p.read_text(encoding="utf-8", errors="ignore")
        meta = {"source": str(p), "kind": "本地文件"}
    elif body.source_type == "url":
        try:
            r = requests.get(body.value.strip(), timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            html = r.text
        except Exception as e:
            raise HTTPException(400, f"抓取失败：{str(e)[:160]}")
        meta = {"source": body.value.strip(), "kind": "在线网址"}
    else:
        html = body.value
        meta = {"source": "上传的文件", "kind": "上传"}
    if len(html) < 20:
        raise HTTPException(400, "内容太短，可能没读到网页正文。")
    ai.CONFIG_DIR.mkdir(exist_ok=True)
    SITE_HTML.write_text(html, encoding="utf-8")
    meta["loaded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta["chars"] = len(html)
    SITE_META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "chars": len(html), "meta": meta}


SITE_PROMPT = """你在帮用户改他的【个人作品集网页】。给你三样东西：当前网页完整 HTML、用户在 Caddie 里的经历/项目、用户这次的指令。
你要产出对网页的【精确文本替换】清单。铁律：
1. 绝对不要改 <style> 里的 CSS、配色、字体、布局结构。只改可见文本内容（标题、描述文字、项目条目、数字等）。
2. 每条替换的 find 必须是 HTML 里【一字不差、能唯一定位】的片段（连同标签），replace 是改后的片段。片段尽量短小精准。
3. 改动要基于用户在 Caddie 里的真实经历/项目（让网页和最新经历一致、更有说服力）。
4. 若用户指令确实需要动到风格/配色/布局，不要擅自改——在 summary 里说明，并把 style_changed 设为 true。
5. 【重要】不要因为 Caddie 里暂时没有某段经历，就删除网页上已有的真实内容。网页可能比 Caddie 更全。优先做补充和润色；replace 设为空（删除）要非常谨慎，且必须在 reason 里以「【删除】」开头说明理由。

当前网页 HTML：
%s

用户在 Caddie 的经历/项目：
%s

用户这次的指令（可能为空，为空则你根据经历找出最该更新的文本）：%s

只返回如下 JSON，不要多余文字：
{"summary":"这次改了什么（一句话）","style_changed":false,
 "edits":[{"find":"原文片段","replace":"改后片段","reason":"为什么这么改"}]}
若无需改动，edits 返回 []。"""


@app.post("/api/site/propose")
def site_propose(body: SiteInstructIn):
    html = _site_html()
    if not html:
        raise HTTPException(400, "还没载入网页，先在上方载入你的 HTML。")
    prompt = SITE_PROMPT % (html[:48000], db.snapshot_for_ai(), body.instruction or "（无具体指令）")
    try:
        raw = ai.chat([{"role": "user", "content": prompt}], max_tokens=4000)
        data = ai.extract_json(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"生成建议失败（模型返回不合规）：{str(e)[:200]}")
    data.setdefault("edits", [])
    data.setdefault("summary", "")
    for e in data["edits"]:
        e["found"] = bool(e.get("find")) and (e["find"] in html)
    return data


def _inject_base(html: str, base_href: str) -> str:
    tag = f'<base href="{base_href}">'
    low = html.lower()
    i = low.find("<head")
    if i != -1:
        j = html.find(">", i)
        if j != -1:
            return html[:j + 1] + tag + html[j + 1:]
    return tag + html


@app.get("/api/site/raw", response_class=HTMLResponse)
def site_raw():
    """把载入的网页用真实地址提供出来，预览才能正确加载图片/字体/脚本。"""
    html = _site_html()
    if not html:
        return HTMLResponse("<p style='font-family:sans-serif;color:#888;padding:40px'>还没载入网页</p>")
    meta = _site_meta()
    kind = meta.get("kind")
    if kind == "本地文件":
        html = _inject_base(html, "/site-assets/")
    elif kind == "在线网址":
        html = _inject_base(html, meta.get("source", ""))
    return HTMLResponse(html)


@app.get("/site-assets/{path:path}")
def site_asset(path: str):
    """提供本地网页同目录下的资源（图片等）。"""
    meta = _site_meta()
    src = meta.get("source", "")
    if not src or meta.get("kind") != "本地文件":
        raise HTTPException(404)
    base = Path(src).parent.resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)) or not target.exists() or target.is_dir():
        raise HTTPException(404)
    return FileResponse(str(target))


@app.post("/api/site/apply")
def site_apply(body: SiteApplyIn):
    html = _site_html()
    if not html:
        raise HTTPException(400, "还没载入网页")
    applied, missed = [], []
    for e in body.edits:
        f, r = e.get("find"), e.get("replace", "")
        if f and f in html:
            html = html.replace(f, r, 1)
            applied.append(e.get("reason") or "(改动)")
        else:
            missed.append(e.get("reason") or (f or "")[:30])
    SITE_HTML.write_text(html, encoding="utf-8")
    meta = _site_meta()
    meta["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    SITE_META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    _commit(f"更新个人网页（{len(applied)} 处）")
    return {"ok": True, "applied": applied, "missed": missed}


# ─── 项目级对话：在文档旁边和 AI 一起完善这个项目 ────────────────────────────

PROJECT_CHAT_BASE = """你在帮用户打磨【这一个具体项目】，目标：把它写成一段有说服力、能写进简历、经得起面试追问的项目经历。
铁律：你【不能】直接改文档。每次回复结尾，如果这轮对话里出现了可以写进文档的信息，
用一行明确说明你打算更新哪些地方（例如「✏️ 可更新：背景段 + 量化结果 + 新增一条面试追问」），
然后提示用户点下方『🗂 更新文档』确认。绝不假装已经改好了。用中文，简洁不啰嗦。"""

PROJECT_MODE = {
    "organize": "\n本轮采用【直接整理】模式：以帮用户组织和写为主，尽量少反问。用户给出信息后，"
                "直接把它组织成项目文档该有的语言，主动补全表达。只有在缺了关键信息、不问就没法写时，才问一个最关键的问题。",
    "ask": "\n本轮采用【追问深挖】模式：像资深面试官一样主动追问薄弱处——背景/你具体做了什么/量化结果/技术/难点，"
           "帮用户把模糊的地方问清楚。一次只问一两个，别一口气问太多。",
}


PORTFOLIO_CHAT_BASE = """你在帮用户打磨【这一个个人作品】（自己做的 app / side project / demo），目标：写成面试讲得清、能扛住追问的作品介绍。
铁律：你【不能】直接改文档。每轮结尾若出现可写入文档的信息，用一行说明你打算更新哪些（例如「✏️ 可更新：为什么做 + 技术实现」），然后提示用户点下方『🗂 整理进文档』确认。绝不假装已经改好了。用中文，简洁不啰嗦。
特别注意：面试官最爱追问个人作品的"为什么要单独做、跟现成工具(ChatGPT/Notion 等)的区别、到底解决什么问题、现在做到哪一步"——主动帮用户把这些讲清楚。"""


def _focus_block(scope: str = "portfolio") -> str:
    """把当前关注点库拼成可注入 prompt 的文本（自迭代内容层）。"""
    fps = db.list_focus_points(scope)
    if not fps:
        return ""
    lines = []
    for n in fps:
        tag = "【必答】" if (n.get("strength") or "").lower() == "hard" else "-"
        lines.append(f"{tag} {n.get('directive')}")
    return "【面试关注点（写这个作品时务必主动覆盖，尤其标【必答】的）】\n" + "\n".join(lines) + "\n"


def _project_context(p: dict) -> str:
    exp = p.get("experience") or {}
    return f"""【正在完善的项目】
公司：{exp.get('company','')}　角色：{exp.get('role','')}
项目名：{p.get('name','')}
当前文档（Markdown）：
---
{p.get('document') or '（还是空的）'}
---"""


@app.get("/api/projects/{pid}/messages")
def project_messages(pid: int):
    return db.get_chat_history(f"proj-{pid}", limit=200)


@app.post("/api/projects/{pid}/chat")
def project_chat(pid: int, body: ProjChatIn):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    sid = f"proj-{pid}"
    history = db.get_chat_history(sid, limit=16)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": body.message})
    if p.get("kind") == "personal":
        base = PORTFOLIO_CHAT_BASE + PROJECT_MODE.get(body.mode, PROJECT_MODE["organize"])
        fb = _focus_block()
        system = base + ("\n\n" + fb if fb else "") + "\n\n" + _project_context(p)
    else:
        system = PROJECT_CHAT_BASE + PROJECT_MODE.get(body.mode, PROJECT_MODE["organize"]) + "\n\n" + _project_context(p)
    try:
        reply = ai.chat(messages, system=system, max_tokens=1800)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"调用 AI 失败：{str(e)[:300]}")
    db.save_message(sid, "user", body.message)
    db.save_message(sid, "assistant", reply)
    return {"reply": reply}


PROJECT_PROPOSE_PROMPT = """你在帮用户完善一个项目。请只根据【对话中新增的信息】产出增量补丁，不要重写整篇文档。

硬规则：
1. 大段正文绝对不要放进 JSON。按下面的分隔符格式返回。
2. 项目文档只保留：标题、一句话亮点、背景、我的动作、结果。不要写“可能被追问/我的答法”小节。
3. 面试追问必须单独放到 FOLLOWUPS 区，后端会写入追问题库。
4. 如果某区没有新增信息，保留该区为空。
5. 叙述段可以自然成段；适合枚举的地方再用要点，不要强行全分点。

当前文档：
%s

对话：
%s

严格按此格式返回，不要 markdown 代码围栏，不要额外解释：
SUMMARY: 这次补充了什么（一句话）
ONE_LINER: 更新后的一句话亮点（没有就留空）
TECHNOLOGIES: 逗号分隔（没有就留空）
KEYWORDS: 逗号分隔（没有就留空）
===BACKGROUND===
只写新增/修正的背景段落
===ACTIONS===
只写新增/修正的“我的动作”
===RESULTS===
只写新增/修正的结果和量化影响
===FOLLOWUPS===
Q: 面试官可能追问的问题
A: 建议答法（没有就留空）
STATUS: todo/weak/stable
CATEGORY: 项目贡献/数据方法/业务理解/技术实现/结果量化/风险不足/其他
---
Q: 第二个问题
A:
STATUS: todo
CATEGORY: 其他
===END==="""


def _field_line(raw: str, name: str) -> str:
    m = re.search(rf"(?m)^{re.escape(name)}:\s*(.*)$", raw or "")
    return (m.group(1).strip() if m else "")


def _block(raw: str, name: str) -> str:
    pat = rf"(?s)==={re.escape(name)}===\s*(.*?)(?=\n===[A-Z_]+===|\Z)"
    m = re.search(pat, raw or "")
    return (m.group(1).strip() if m else "")


def _strip_followup_section(doc: str) -> str:
    if not doc:
        return ""
    headings = [
        r"可能被追问(?:的问题)?",
        r"高频追问(?:\s*&\s*我的答法)?",
        r"我的答法",
        r"面试追问",
        r"追问题库",
    ]
    pat = r"(?ms)^#{2,3}\s*(?:" + "|".join(headings) + r").*?(?=^#{2,3}\s+|\Z)"
    return re.sub(pat, "", doc).strip()


def _old_followup_section(doc: str) -> str:
    if not doc:
        return ""
    headings = [
        r"可能被追问(?:的问题)?",
        r"高频追问(?:\s*&\s*我的答法)?",
        r"我的答法",
        r"面试追问",
        r"追问题库",
    ]
    pat = r"(?ms)^#{2,3}\s*(?:" + "|".join(headings) + r").*?(?=^#{2,3}\s+|\Z)"
    m = re.search(pat, doc)
    return (m.group(0).strip() if m else "")


def _extract_followups_from_doc_text(section: str):
    if not section:
        return []
    text = re.sub(r"(?m)^#{2,3}\s+.*$", "", section).strip()
    chunks = re.split(r"\n(?=(?:[-*]\s+)?(?:Q[:：]|问[:：]|追问\s*\d*[:：]|\*\*追问|\d+[.、]\s*))", text)
    out = []
    for chunk in chunks:
        c = chunk.strip(" \n-*\t")
        if not c:
            continue
        c = re.sub(r"^\d+[.、]\s*", "", c).strip()
        q = ""
        a = ""
        m = re.search(r"(?:Q|问|追问\s*\d*)[:：]\s*(.+?)(?:\n|$)", c)
        if m:
            q = m.group(1).strip(" *")
            rest = c[m.end():].strip()
        else:
            lines = [x.strip(" -*") for x in c.splitlines() if x.strip()]
            q = lines[0] if lines else ""
            rest = "\n".join(lines[1:])
        am = re.search(r"(?:A|答|答法|我的答法)[:：]\s*(.*)", rest, flags=re.S)
        a = (am.group(1).strip() if am else rest).strip()
        q = re.sub(r"^\*\*|\*\*$", "", q).strip()
        if len(q) >= 4:
            out.append({"question": q[:300], "answer": a[:2000], "status": "todo", "category": None, "origin": "project_doc"})
    return out[:30]


def _section_body(doc: str, title: str) -> str:
    m = re.search(rf"(?ms)^##\s*{re.escape(title)}\s*\n(.*?)(?=^##\s+|\Z)", doc or "")
    return (m.group(1).strip() if m else "")


def _append_section(old: str, inc: str) -> str:
    old = (old or "").strip()
    inc = (inc or "").strip()
    if not inc:
        return old
    if inc in old:
        return old
    return (old + "\n\n" + inc).strip() if old else inc


def _merge_project_doc(p: dict, patch: dict) -> str:
    cur = _strip_followup_section(p.get("document") or "")
    title = p.get("name") or "项目"
    one_liner = patch.get("one_liner") or p.get("one_liner") or ""
    bg = _append_section(_section_body(cur, "背景"), patch.get("background"))
    act = _append_section(_section_body(cur, "我的动作") or _section_body(cur, "我做了什么"), patch.get("actions"))
    res = _append_section(_section_body(cur, "结果"), patch.get("results"))
    parts = [f"# {title}"]
    if one_liner:
        parts.append(f"> {one_liner}")
    parts.extend([
        "## 背景\n" + (bg or "（待补充）"),
        "## 我的动作\n" + (act or "（待补充）"),
        "## 结果\n" + (res or "（待补充）"),
    ])
    return "\n\n".join(parts).strip()


# 作品视角：每个视角=一套分段骨架（label, 区块KEY, 写作提示）
PORTFOLIO_LENSES = {
    "full": {"name": "全面展示", "desc": "完整呈现，适合个人网页/正式介绍", "sections": [
        ("这是什么", "WHAT", "解决什么问题、给谁用"),
        ("解决的问题", "WHY", "必要性、跟现成方案(ChatGPT/Notion等)的区别"),
        ("核心功能", "FEATURES", "它能做什么"),
        ("技术实现", "TECH", "架构、关键技术选型、难点"),
        ("亮点难点", "HIGHLIGHTS", "最值得说的、踩过的坑"),
        ("现状与规划", "STATUS", "做到哪一步了、下一步"),
    ]},
    "necessity": {"name": "产品必要性", "desc": "主打“为什么值得做”，扛必要性拷问", "sections": [
        ("这是什么", "WHAT", "解决什么问题、给谁用"),
        ("为什么做", "WHY", "必要性、跟现成方案的区别（面试官最爱问）"),
        ("怎么用", "HOW", "输入 · 处理 · 输出"),
        ("现状与规划", "STATUS", "做到哪一步了、下一步"),
    ]},
    "tech": {"name": "技术深度", "desc": "突出工程能力，重点写实现", "sections": [
        ("这是什么", "WHAT", "解决什么问题"),
        ("核心功能", "FEATURES", "它能做什么"),
        ("技术实现", "TECH", "架构、关键技术选型、难点（重点展开）"),
        ("现状与规划", "STATUS", "做到哪一步了、下一步"),
    ]},
}


def _portfolio_lens(lens: str):
    return PORTFOLIO_LENSES.get(lens) or PORTFOLIO_LENSES["full"]


def _portfolio_prompt(lens: str, focus_block: str, doc: str, convo: str) -> str:
    secs = _portfolio_lens(lens)["sections"]
    sec_fmt = "\n".join(f"==={key}===\n{label}：{hint}" for label, key, hint in secs)
    sec_names = "、".join(label for label, _, _ in secs)
    return f"""你在帮用户完善一个【个人作品】（自己做的 app / side project / demo）。只根据【对话中新增的信息】产出增量补丁，不要重写整篇。

{focus_block}
硬规则：
1. 大段正文不要塞进字段行，按下面分隔符格式返回。
2. 作品文档结构固定为：标题、一句话亮点、{sec_names}。
3. 务必主动覆盖上面【面试关注点】里的点（尤其标【必答】的），比如"为什么要单独做、跟现成工具的区别"。哪怕用户没主动说，也引导补全；实在没信息就在该段写「（待补：……）」。
4. 面试追问单独放到 FOLLOWUPS 区，后端会写入追问题库。
5. 某区没有新增信息就留空。

当前文档：
{doc}

对话：
{convo}

严格按此格式返回，不要 markdown 代码围栏，不要额外解释：
SUMMARY: 这次补充了什么（一句话）
ONE_LINER: 更新后的一句话亮点（没有就留空）
TECHNOLOGIES: 逗号分隔（没有就留空）
KEYWORDS: 逗号分隔（没有就留空）
{sec_fmt}
===FOLLOWUPS===
Q: 面试官可能追问的问题
A: 建议答法（没有就留空）
STATUS: todo/weak/stable
CATEGORY: 必要性/技术实现/产品判断/现状规划/其他
===END==="""


def _parse_portfolio_patch(raw: str, lens: str):
    secs = _portfolio_lens(lens)["sections"]
    data = {
        "summary": _field_line(raw, "SUMMARY"),
        "one_liner": _field_line(raw, "ONE_LINER"),
        "technologies": _field_line(raw, "TECHNOLOGIES"),
        "keywords": _field_line(raw, "KEYWORDS"),
        "followups": _parse_followups(raw),
        "_sections": [(label, _block(raw, key)) for label, key, _ in secs],
    }
    return data


def _strip_lead_heading(text: str) -> str:
    """剥掉区块内容自带的首行标题（避免和拼接的 ## 标题重复）。"""
    return re.sub(r"^\s*#{1,6}\s+\S.*\n+", "", (text or "").lstrip(), count=1).strip()


def _merge_portfolio_doc(p: dict, patch: dict) -> str:
    cur = _strip_followup_section(p.get("document") or "")
    title = p.get("name") or "作品"
    one_liner = patch.get("one_liner") or p.get("one_liner") or ""
    parts = [f"# {title}"]
    if one_liner:
        parts.append(f"> {one_liner}")
    lens_labels = [label for label, _ in patch.get("_sections", [])]
    for label, inc in patch.get("_sections", []):
        body = _append_section(_section_body(cur, label), _strip_lead_heading(inc))
        parts.append(f"## {label}\n" + (body or "（待补充）"))
    # 保留当前文档里不属于本视角的旧段落，绝不丢用户内容
    for m in re.finditer(r"(?ms)^##\s*(.+?)\s*\n(.*?)(?=^##\s+|\Z)", cur or ""):
        lbl, body = m.group(1).strip(), m.group(2).strip()
        if lbl not in lens_labels and body and body != "（待补充）":
            parts.append(f"## {lbl}\n" + body)
    return "\n\n".join(parts).strip()


def _parse_followups(raw: str):
    block = _block(raw, "FOLLOWUPS")
    if not block:
        return []
    out = []
    for chunk in re.split(r"\n---+\n", block):
        q = _field_line(chunk, "Q")
        if not q:
            continue
        status = (_field_line(chunk, "STATUS") or "todo").lower()
        if status not in ("stable", "weak", "todo"):
            status = "todo"
        out.append({
            "question": q,
            "answer": _field_line(chunk, "A"),
            "status": status,
            "category": _field_line(chunk, "CATEGORY") or None,
            "origin": "project_conversation",
        })
    return out


def _parse_project_patch(raw: str):
    return {
        "summary": _field_line(raw, "SUMMARY"),
        "one_liner": _field_line(raw, "ONE_LINER"),
        "technologies": _field_line(raw, "TECHNOLOGIES"),
        "keywords": _field_line(raw, "KEYWORDS"),
        "background": _block(raw, "BACKGROUND"),
        "actions": _block(raw, "ACTIONS"),
        "results": _block(raw, "RESULTS"),
        "followups": _parse_followups(raw),
    }


@app.get("/api/portfolio/lenses")
def portfolio_lenses():
    return {"items": [{"key": k, "name": v["name"], "desc": v["desc"],
                       "sections": [lbl for lbl, _, _ in v["sections"]]}
                      for k, v in PORTFOLIO_LENSES.items()]}


@app.post("/api/projects/{pid}/propose")
def project_propose(pid: int, lens: str = "full"):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    history = db.get_chat_history(f"proj-{pid}", limit=40)
    if not history:
        return {"summary": "还没聊过，先在右边和 Caddie 聊聊这个项目", "changes": []}
    convo = "\n".join(f"{'用户' if m['role']=='user' else 'Caddie'}：{m['content']}" for m in history)
    is_personal = p.get("kind") == "personal"
    if is_personal:
        prompt = _portfolio_prompt(lens, _focus_block(), p.get("document") or "（空）", convo[:8000])
    else:
        prompt = PROJECT_PROPOSE_PROMPT % (p.get("document") or "（空）", convo[:8000])
    try:
        raw = ai.chat([{"role": "user", "content": prompt}], max_tokens=3000)
        data = _parse_portfolio_patch(raw, lens) if is_personal else _parse_project_patch(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"整理失败（模型返回无法解析）：{str(e)[:200]}")
    if is_personal:
        has_doc_patch = any((data.get(k) or "").strip() for k in ("one_liner", "technologies", "keywords")) \
            or any((inc or "").strip() for _, inc in data.get("_sections", []))
    else:
        has_doc_patch = any((data.get(k) or "").strip() for k in ("one_liner", "technologies", "keywords", "background", "actions", "results"))
    changes = []
    if has_doc_patch:
        changes.append({
            "type": "update_project", "project_id": pid, "name": p.get("name"),
            "one_liner": data.get("one_liner") or p.get("one_liner"),
            "technologies": data.get("technologies") or p.get("technologies"),
            "keywords": data.get("keywords") or p.get("keywords"),
            "document": _merge_portfolio_doc(p, data) if is_personal else _merge_project_doc(p, data),
            "reason": data.get("summary"),
        })
    for f in data.get("followups") or []:
        f.update({"type": "create_followup", "project_id": pid})
        changes.append(f)
    if not changes:
        return {"summary": data.get("summary") or "没有可更新的内容", "changes": []}
    return {"summary": data.get("summary", ""), "changes": changes}


@app.post("/api/projects/{pid}/extract-followups")
def extract_project_followups(pid: int):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    doc = p.get("document") or ""
    section = _old_followup_section(doc)
    items = _extract_followups_from_doc_text(section)
    if not section or not items:
        return {"summary": "没有在文档里识别到可拆出的追问小节", "changes": []}
    new_doc = _strip_followup_section(doc)
    changes = [{
        "type": "update_project",
        "project_id": pid,
        "name": p.get("name"),
        "one_liner": p.get("one_liner"),
        "technologies": p.get("technologies"),
        "keywords": p.get("keywords"),
        "document": new_doc,
        "reason": "移除文档中的追问小节，追问单独进入题库",
    }]
    for item in items:
        item.update({"type": "create_followup", "project_id": pid})
        changes.append(item)
    return {"summary": f"识别到 {len(items)} 条追问，可拆入追问题库", "changes": changes}


# ─── 文件文字提取（PDF / 纯文本通用）─────────────────────────────────────────

def _extract_text(raw: bytes, filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    if name.endswith(".docx"):
        import io
        import re
        import zipfile
        from xml.etree import ElementTree as ET
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        parts = []
        for node in root.iter():
            if node.tag == "{%s}t" % ns["w"] and node.text:
                parts.append(node.text)
            elif node.tag == "{%s}p" % ns["w"]:
                parts.append("\n")
        return re.sub(r"\n{3,}", "\n\n", "".join(parts))
    return raw.decode("utf-8", errors="ignore")


# ─── 资料资产中心：原始资料 / 求职线 / AI 识别建议 ───────────────────────────

SOURCE_TYPES = {
    "resume": "简历",
    "jd": "JD",
    "project": "项目素材",
    "feedback": "面试反馈",
    "worklog": "日记周报",
    "knowledge": "知识资料",
    "other": "其他",
}


@app.get("/api/sources")
def list_sources(source_type: Optional[str] = None):
    return {"items": db.list_sources(source_type), "types": SOURCE_TYPES}


@app.get("/api/sources/{sid}")
def get_source(sid: int):
    src = db.get_source(sid)
    if not src:
        raise HTTPException(404, "资料不存在")
    if src.get("analysis_json"):
        try:
            src["analysis"] = json.loads(src["analysis_json"])
        except Exception:
            src["analysis"] = None
    return src


@app.post("/api/sources")
def add_source(body: SourceIn):
    text = (body.content or "").strip()
    if len(text) < 5:
        raise HTTPException(400, "资料内容太短")
    sid = db.create_source({
        "source_type": body.source_type or "other",
        "title": body.title or text[:30],
        "content": text,
        "status": "raw",
    })
    _commit(f"新增资料：{body.title or text[:20]}")
    return {"id": sid}


@app.post("/api/sources/upload")
async def upload_source(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        text = _extract_text(raw, file.filename).strip()
    except Exception as e:
        raise HTTPException(400, f"文件解析失败：{str(e)[:200]}")
    if len(text) < 10:
        raise HTTPException(400, "没从文件里读到足够文字（可能是扫描版/图片型 PDF）。")
    store_dir = ai.CONFIG_DIR / "sources"
    store_dir.mkdir(exist_ok=True)
    safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{(file.filename or 'source').replace('/', '_')}"
    path = store_dir / safe_name
    path.write_bytes(raw)
    sid = db.create_source({
        "source_type": "other",
        "title": file.filename or "上传资料",
        "content": text,
        "file_name": file.filename,
        "file_path": str(path),
        "status": "raw",
    })
    _commit(f"上传资料：{file.filename or sid}")
    return {"id": sid, "chars": len(text)}


SOURCE_ANALYZE_PROMPT = """你是 Caddie 的资料资产整理器。请识别这份求职资料，并给出结构化入库建议。

可选 source_type 只能是：
- resume：简历
- jd：岗位 JD
- project：项目素材/周报/项目文档
- feedback：面试反馈/复盘
- worklog：日记/周报/工作记录
- knowledge：面经/课程笔记/知识资料
- other：其他

请只基于资料原文，不要编造。返回严格 JSON：
{
  "source_type": "jd",
  "title": "简短标题",
  "summary": "3-5 句话摘要",
  "tags": ["AI产品", "Agent"],
  "key_points": ["可复用事实/要求/反馈点"],
  "suggested_track": {"company": "可为空", "role": "可为空", "target": "可为空"},
  "suggested_links": [
    {"entity_type": "project/application/experience/interview_item/job_track", "name": "可能关联对象名称", "reason": "为什么相关"}
  ],
  "suggested_actions": [
    {"type": "create_job_track/update_project/create_knowledge_card/save_feedback/analyze_jd", "label": "用户能看懂的动作", "reason": "为什么建议做"}
  ],
  "risks": ["需要人工确认或可能不可靠的点"]
}

资料标题：%s
资料原文：
%s"""


@app.post("/api/sources/{sid}/analyze")
def analyze_source(sid: int):
    src = db.get_source(sid)
    if not src:
        raise HTTPException(404, "资料不存在")
    text = (src.get("content") or "").strip()
    if len(text) < 10:
        raise HTTPException(400, "资料内容太短")
    try:
        out = ai.chat([{"role": "user", "content": SOURCE_ANALYZE_PROMPT % (src.get("title") or "", text[:12000])}], max_tokens=2200)
        data = ai.extract_json(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"资料分析失败（模型返回不合规）：{str(e)[:200]}")
    data.setdefault("source_type", "other")
    data.setdefault("title", src.get("title") or "未命名资料")
    data.setdefault("summary", "")
    db.update_source_analysis(sid, {
        "source_type": data.get("source_type"),
        "title": data.get("title"),
        "summary": data.get("summary"),
        "analysis_json": json.dumps(data, ensure_ascii=False),
        "status": "analyzed",
    })
    _commit(f"分析资料：{data.get('title')}")
    return data


@app.delete("/api/sources/{sid}")
def del_source(sid: int):
    db.delete_source(sid)
    _commit("删除资料")
    return {"ok": True}


@app.get("/api/job-tracks")
def list_job_tracks():
    return db.list_job_tracks()


@app.get("/api/applications/{aid}/track")
def get_application_track(aid: int):
    db.ensure_application_tracks()
    app_ = db.get_application(aid)
    if not app_:
        raise HTTPException(404, "投递不存在")
    if not app_.get("track_id"):
        raise HTTPException(500, "这条投递还没有生成求职线")
    return {"track_id": app_["track_id"]}


@app.post("/api/job-tracks")
def add_job_track(body: JobTrackIn):
    jid = db.create_job_track(body.dict())
    _commit(f"新增求职线：{body.company or ''} · {body.role or ''}")
    return {"id": jid}


@app.get("/api/job-tracks/{tid}")
def get_job_track_detail(tid: int):
    t = db.get_job_track(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    gaps = db.list_track_gaps(tid)
    apps = [a for a in db.get_applications() if a.get("track_id") == tid]
    assets = db.list_assets(track_id=tid, limit=50)
    done = sum(1 for g in gaps if g.get("status") == "done")
    return {"track": t, "gaps": gaps, "applications": apps, "assets": assets,
            "gap_done": done, "gap_total": len(gaps)}


@app.put("/api/job-tracks/{tid}")
def edit_job_track(tid: int, body: JobTrackIn):
    db.update_job_track(tid, body.dict())
    _commit(f"编辑求职线：{body.company or ''} · {body.role or ''}")
    return {"ok": True}


@app.delete("/api/job-tracks/{tid}")
def del_job_track(tid: int):
    t = db.get_job_track(tid) or {}
    db.delete_job_track(tid)
    _commit(f"删除求职线：{t.get('company','')} · {t.get('role','')}")
    return {"ok": True}


# ─── 求职线：差距清单 CRUD ────────────────────────────────────────────────────

@app.get("/api/job-tracks/{tid}/gaps")
def list_gaps(tid: int):
    return {"items": db.list_track_gaps(tid)}


@app.post("/api/job-tracks/{tid}/gaps")
def add_gap(tid: int, body: TrackGapIn):
    d = body.dict(); d["track_id"] = tid
    gid = db.create_track_gap(d)
    _commit("新增差距项")
    return {"id": gid}


@app.put("/api/gaps/{gid}")
def edit_gap(gid: int, body: TrackGapIn):
    db.update_track_gap(gid, {k: v for k, v in body.dict().items() if v is not None})
    return {"ok": True}


@app.delete("/api/gaps/{gid}")
def del_gap(gid: int):
    db.delete_track_gap(gid)
    return {"ok": True}


# ─── 求职线：AI 岗位画像 + 差距诊断 ───────────────────────────────────────────

PERSONA_PROMPT = """你是资深招聘官。读这段 JD，提炼这个岗位真正想要什么样的人（岗位画像）。
输出 4-6 条，每条一句话，聚焦：核心职责、必须的硬能力、看重的软素质/特质、隐含的偏好。
直接给要点，每行一条，不要标题、不要 markdown 符号。"""

GAP_DIAGNOSE_PROMPT = """你是求职教练。对比【岗位要求】和【候选人现有背景】，诊断差距，输出差距清单。

【岗位要求 / JD】
%s

【候选人现有背景（经历/项目/技能）】
%s

为每个关键要求判断候选人的匹配情况，输出 JSON（不要 markdown 围栏）：
{"gaps":[
  {"dimension":"硬技能/项目经历/领域知识/软素质","requirement":"JD里的具体要求","my_status":"have/partial/missing","severity":"blocker/fixable/bypass","plan_type":"knowledge/portfolio/pitch/none","note":"一句话建议怎么补或怎么讲"}
]}
- have=已具备 partial=部分 missing=缺；blocker=硬伤 fixable=可补 bypass=可绕过
- plan_type：knowledge=补知识 portfolio=补作品 pitch=补话术(把现有经历往JD靠) none=无需
- 控制在 8 条以内，最关键的在前。"""


@app.post("/api/job-tracks/{tid}/persona")
def gen_persona(tid: int):
    t = db.get_job_track(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    if not (t.get("jd") or "").strip():
        raise HTTPException(400, "先填写这个目标的 JD")
    out = ai.chat([{"role": "user", "content": t["jd"][:6000]}], system=PERSONA_PROMPT, max_tokens=600)
    db.update_job_track(tid, {**t, "persona": out.strip()})
    _commit("生成岗位画像")
    return {"persona": out.strip()}


@app.post("/api/job-tracks/{tid}/diagnose")
def diagnose_gaps(tid: int):
    t = db.get_job_track(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    if not (t.get("jd") or "").strip():
        raise HTTPException(400, "先填写这个目标的 JD")
    background = _full_career_context(6000)
    prompt = GAP_DIAGNOSE_PROMPT % (t["jd"][:5000], background)
    raw = ai.chat([{"role": "user", "content": prompt}], max_tokens=2000)
    data = ai.extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("gaps"), list):
        raise HTTPException(500, "AI 诊断失败，请重试")
    return {"gaps": data["gaps"]}


@app.post("/api/job-tracks/{tid}/gaps/bulk")
def add_gaps_bulk(tid: int, body: dict):
    items = body.get("items") or []
    n = 0
    for g in items:
        g["track_id"] = tid
        db.create_track_gap(g)
        n += 1
    _commit(f"采纳 {n} 条差距诊断")
    return {"ok": True, "count": n}


ASSET_TYPE_LABEL = {
    "project_pitch": "项目话术", "self_intro": "自我介绍", "knowledge": "知识卡",
    "resume": "定制简历", "gap_analysis": "JD 对比", "review": "复盘",
    "worklog_digest": "日记提炼", "other": "其他",
}
ASSET_STATUS_LABEL = {"draft": "草稿", "final": "定稿", "stale": "待更新"}


def _enrich_asset(a: dict) -> dict:
    """给资产补上可读的类型标签、关联项目/求职线名字。"""
    a["type_label"] = ASSET_TYPE_LABEL.get(a.get("asset_type"), a.get("asset_type"))
    a["status_label"] = ASSET_STATUS_LABEL.get(a.get("status"), a.get("status"))
    a["project_name"] = None
    a["track_name"] = None
    if a.get("project_id"):
        p = db.get_project(a["project_id"])
        if p:
            a["project_name"] = p.get("name")
    if a.get("track_id"):
        t = db.get_job_track(a["track_id"])
        if t:
            a["track_name"] = f"{t.get('company') or ''} {t.get('role') or t.get('target') or ''}".strip()
    return a


@app.get("/api/assets")
def list_assets(asset_type: Optional[str] = None, track_id: Optional[int] = None, project_id: Optional[int] = None):
    items = [_enrich_asset(a) for a in db.list_assets(asset_type=asset_type, track_id=track_id, project_id=project_id)]
    return {"items": items, "types": ASSET_TYPE_LABEL}


@app.get("/api/assets/{aid}")
def get_asset(aid: int):
    asset = db.get_asset(aid)
    if not asset:
        raise HTTPException(404, "资产不存在")
    if asset.get("provenance_json"):
        try:
            asset["provenance"] = json.loads(asset["provenance_json"])
        except Exception:
            asset["provenance"] = None
    if asset.get("derived_from_json"):
        try:
            asset["derived_from"] = json.loads(asset["derived_from_json"])
        except Exception:
            asset["derived_from"] = None
    return _enrich_asset(asset)


@app.post("/api/assets")
def add_asset(body: AssetIn):
    aid = db.create_asset(body.dict())
    _commit(f"新增资产：{body.title or body.asset_type}")
    return {"id": aid}


class AssetUpdateIn(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None


class FollowupIn(BaseModel):
    question: str
    answer: Optional[str] = None
    status: Optional[str] = "todo"
    category: Optional[str] = None
    asked_count: Optional[int] = 0
    origin: Optional[str] = "manual"
    source_track_id: Optional[int] = None
    kind: Optional[str] = None


class FollowupUpdateIn(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    status: Optional[str] = None
    category: Optional[str] = None
    asked_count: Optional[int] = None
    origin: Optional[str] = None
    source_track_id: Optional[int] = None
    kind: Optional[str] = None


@app.put("/api/assets/{aid}")
def edit_asset(aid: int, body: AssetUpdateIn):
    cur = db.get_asset(aid)
    if not cur:
        raise HTTPException(404, "资产不存在")
    merged = {
        "title": body.title if body.title is not None else cur.get("title"),
        "body": body.body if body.body is not None else cur.get("body"),
        "status": body.status if body.status is not None else cur.get("status"),
    }
    db.update_asset(aid, merged)
    _commit(f"更新资产：{merged['title'] or aid} → {ASSET_STATUS_LABEL.get(merged['status'], merged['status'])}")
    return {"ok": True}


@app.delete("/api/assets/{aid}")
def remove_asset(aid: int):
    db.delete_asset(aid)
    _commit("删除资产")
    return {"ok": True}


@app.get("/api/projects/{pid}/followups")
def list_project_followups(pid: int, status: Optional[str] = None):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return {"items": db.list_followups(project_id=pid, status=status)}


@app.post("/api/projects/{pid}/followups")
def add_project_followup(pid: int, body: FollowupIn):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not (body.question or "").strip():
        raise HTTPException(400, "追问不能为空")
    data = body.dict()
    if (data.get("category") or "").strip():
        data["category_source"] = "user"
    fid = db.create_followup(pid, data)
    _commit(f"新增项目追问：{p.get('name')}")
    return {"id": fid}


@app.put("/api/followups/{fid}")
def edit_followup(fid: int, body: FollowupUpdateIn):
    cur = db.get_followup(fid)
    if not cur:
        raise HTTPException(404, "追问不存在")
    patch = body.dict(exclude_unset=True)
    old_cat = cur.get("category")
    if "category" in patch and patch.get("category") != old_cat:
        patch["category_source"] = "user"
        patch["previous_category"] = old_cat
        q = cur.get("question") or ""
        new_cat = patch.get("category") or "未分类"
        db.create_feedback_note({
            "scope": "global",
            "category": "taxonomy",
            "polarity": "do",
            "strength": "soft",
            "directive": f"把形如「{q[:60]}」的追问归类到「{new_cat}」",
            "content": f"追问分类学习：{old_cat or '未分类'} → {new_cat}",
            "original_text": q,
        })
    db.update_followup(fid, patch)
    _commit("更新项目追问")
    return {
        "ok": True,
        "learned": "category" in patch and patch.get("category") != old_cat,
        "previous_category": old_cat,
        "category": patch.get("category", old_cat),
    }


@app.delete("/api/followups/{fid}")
def remove_followup(fid: int):
    cur = db.get_followup(fid)
    if not cur:
        raise HTTPException(404, "追问不存在")
    db.delete_followup(fid)
    _commit("删除项目追问")
    return {"ok": True}


FOLLOWUP_CATEGORIES = [
    "项目贡献", "数据方法", "业务理解", "技术实现",
    "结果量化", "反事实追问", "风险不足", "协作推进", "数据缺口", "其他",
]


def _taxonomy_feedback():
    taxonomy = db.list_feedback_notes(scope="global", status="active")
    return [x for x in taxonomy if x.get("category") == "taxonomy"]


def _classify_followup_items(items):
    if not items:
        return {}
    taxonomy = _taxonomy_feedback()
    prompt = f"""请给下面这些项目面试追问分类。分类名要短、稳定、可复用，例如：项目贡献 / 数据方法 / 业务理解 / 技术实现 / 结果量化 / 反事实追问 / 风险不足。

用户已有分类偏好：
{chr(10).join('- '+(x.get('directive') or x.get('content') or '') for x in taxonomy[:20]) or '（无）'}

优先从这些稳定分类中选；确实不合适时才创建新的短分类：
{' / '.join(FOLLOWUP_CATEGORIES)}

追问列表：
{json.dumps([{'id': x['id'], 'question': x['question']} for x in items], ensure_ascii=False)}

严格返回 JSON：
{{"items":[{{"id":1,"category":"项目贡献"}}]}}"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=800)
        data = ai.extract_json(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"归类失败：{str(e)[:160]}")
    idset = {x["id"] for x in items}
    result = {}
    for x in data.get("items", []) if isinstance(data, dict) else []:
        category = str(x.get("category") or "").strip()[:24]
        if x.get("id") in idset and category:
            result[x["id"]] = category
    return result


def _category_needs_classification(value):
    value = str(value or "").strip()
    return not value or len(value) > 24 or bool(re.search(r"[,，/|｜、]", value))


@app.post("/api/followups/classify")
def classify_followups(body: dict):
    pid = body.get("project_id")
    if not pid or not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    include_all = bool(body.get("include_all"))
    project_items = db.list_followups(project_id=pid)
    items = project_items if include_all else [
        x for x in project_items if _category_needs_classification(x.get("category"))
    ]
    if not items:
        return {"updated": 0, "remaining": 0, "categories": FOLLOWUP_CATEGORIES}
    classified = _classify_followup_items(items)
    updated = 0
    for item in items:
        category = classified.get(item["id"])
        if category and category != item.get("category"):
            db.update_followup(item["id"], {
                "category": category,
                "category_source": "ai",
                "previous_category": item.get("category"),
            })
            updated += 1
    if updated:
        _commit(f"AI 归类项目追问：{updated} 条")
    remaining = len([
        x for x in db.list_followups(project_id=pid)
        if _category_needs_classification(x.get("category"))
    ])
    return {
        "updated": updated,
        "remaining": remaining,
        "categories": FOLLOWUP_CATEGORIES,
        "learned_rules": len(_taxonomy_feedback()),
    }


_REFLECT_HINTS = ("不足", "缺点", "改进", "优化", "怎么改", "如果", "换成", "还能",
                  "反思", "局限", "更好", "为什么不", "假设", "扩展", "下一步", "trade")


def _guess_followup_kind(question: str) -> str:
    return "reflective" if any(k in (question or "") for k in _REFLECT_HINTS) else "factual"


@app.post("/api/followups/{fid}/polish")
def polish_followup(fid: int, body: dict):
    """类型感知的『和 AI 一起打磨答法』。读全项目背景；深挖型问你要真相、反思延伸型 AI 主动复盘。"""
    fol = db.get_followup(fid)
    if not fol:
        raise HTTPException(404, "追问不存在")
    if not db.get_project(fol.get("project_id")):
        raise HTTPException(404, "项目不存在")
    kind = (body.get("kind") or fol.get("kind") or _guess_followup_kind(fol.get("question"))).strip()
    action = (body.get("action") or "").strip()
    instruction = (body.get("instruction") or "").strip()

    ctx = caddie_context.build_context(project_id=fol.get("project_id"), intent="followup_answer")
    siblings = [x for x in db.list_followups(project_id=fol.get("project_id")) if x["id"] != fid]
    sib_text = "\n".join(
        f"- 问：{s.get('question')}\n  答：{(s.get('answer') or '（未答）')[:200]}" for s in siblings[:12]
    ) or "（无）"

    payload = f"""【这个项目的完整背景】
{ctx['text'][:8000]}

【同项目的其它追问及答法（保持一致、别矛盾或重复）】
{sib_text}

【这条追问】
{fol.get('question')}

【我目前的答法（可能为空或粗糙）】
{fol.get('answer') or '（空）'}
"""
    if instruction:
        payload += f"\n【我的补充/指令】\n{instruction}\n"
    if action:
        payload += f"\n【这次要做的】{action}\n"

    if kind == "reflective":
        system = """你是资深面试教练，帮求职者回答【反思/延伸类】追问（如"不足在哪""你会怎么改""如果换个场景"）。
这类问题用户往往没有现成答案，需要你主动复盘、产出高质量分析。要求：
- 必须站在【真实项目背景】上推理，结合他实际用的方法、数据规模、业务场景，不能空谈套话。
- 给出有洞察的内容：真实的不足、可行的改进方向、关键 trade-off。
- 推测性判断标成「（假设）」，由用户拍板，不要冒充事实。
- 结构清晰、可口语化背诵，用中文 Markdown。
直接输出打磨后的答法本身，不要解释你做了什么。"""
    else:
        system = """你是资深面试教练，帮求职者回答【深挖类】追问（如"具体怎么做的""你贡献多少"）。
答案在用户脑子里，你的任务是把他的真话组织成有力回答，绝不替他编造事实或数字。要求：
- 只用【背景】和【我的答法/补充】里出现的事实来组织，按 STAR 讲清楚。
- 缺关键具体细节（某个数字、某个做法）时，在该处插入醒目占位「（这里需要你补：……）」让用户填，不要瞎编。
- 结构清晰、可口语化背诵，用中文 Markdown。
直接输出打磨后的答法本身，不要解释你做了什么。"""

    try:
        out = ai.chat([{"role": "user", "content": payload}], system=system, max_tokens=1600)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"打磨失败：{str(e)[:200]}")
    if not fol.get("kind"):
        db.update_followup(fid, {"kind": kind})
    return {"answer": out, "kind": kind}


@app.get("/api/projects/{pid}/sources")
def project_sources(pid: int):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    out = []
    for link in db.list_source_links(entity_type="project", entity_id=pid):
        src = db.get_source(link.get("source_id"))
        if src:
            out.append({"link": link, "source": src})
    return {"items": out}


@app.get("/api/feedback")
def feedback_notes(scope: Optional[str] = None, scope_id: Optional[int] = None, status: Optional[str] = "active"):
    return {"items": db.list_feedback_notes(scope=scope, scope_id=scope_id, status=status)}


# ─── 项目资料上传：解析文件并并入项目文档 ────────────────────────────────────

PROJECT_FILE_MERGE_PROMPT = """用户为【某个项目】上传了一份资料文件，请把其中和这个项目相关的有用信息
整理成可并入项目的增量补丁，不要重写整篇文档。

硬规则：
1. 大段正文绝对不要放进 JSON。按分隔符格式返回。
2. 项目文档只保留：标题、一句话亮点、背景、我的动作、结果。
3. 资料里能预判到的面试追问，必须放到 FOLLOWUPS 区，不要写进文档。
4. 如果某区没有新增信息，留空。

当前项目文档：
%s

上传的资料文件「%s」内容：
%s

严格按此格式返回，不要 markdown 代码围栏，不要额外解释：
SUMMARY: 从这份资料里并入了什么（一句话）
ONE_LINER: 更新后的一句话亮点（没有就留空）
TECHNOLOGIES: 逗号分隔（没有就留空）
KEYWORDS: 逗号分隔（没有就留空）
===BACKGROUND===
新增/修正的背景段落
===ACTIONS===
新增/修正的“我的动作”
===RESULTS===
新增/修正的结果和量化影响
===FOLLOWUPS===
Q: 面试官可能追问的问题
A: 建议答法（没有就留空）
STATUS: todo/weak/stable
CATEGORY: 项目贡献/数据方法/业务理解/技术实现/结果量化/风险不足/其他
===END==="""


@app.post("/api/projects/{pid}/import")
async def project_import(pid: int, file: UploadFile = File(...)):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    raw = await file.read()
    try:
        text = _extract_text(raw, file.filename).strip()
    except Exception as e:
        raise HTTPException(400, f"文件解析失败：{str(e)[:150]}")
    if len(text) < 10:
        raise HTTPException(400, "没从文件里读到足够文字（可能是扫描版/图片型 PDF）。")
    prompt = PROJECT_FILE_MERGE_PROMPT % (p.get("document") or "（空）", file.filename or "资料", text[:12000])
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=3500)
        data = _parse_project_patch(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"合并失败（模型返回无法解析）：{str(e)[:200]}")
    has_doc_patch = any((data.get(k) or "").strip() for k in ("one_liner", "technologies", "keywords", "background", "actions", "results"))
    changes = []
    if has_doc_patch:
        changes.append({
            "type": "update_project", "project_id": pid, "name": p.get("name"),
            "one_liner": data.get("one_liner") or p.get("one_liner"),
            "technologies": data.get("technologies") or p.get("technologies"),
            "keywords": data.get("keywords") or p.get("keywords"),
            "document": _merge_project_doc(p, data),
            "reason": data.get("summary"),
        })
    for f in data.get("followups") or []:
        f.update({"type": "create_followup", "project_id": pid, "origin": "project_file"})
        changes.append(f)
    if not changes:
        return {"summary": "没从资料里提取到可并入的内容", "changes": [], "chars": len(text)}
    return {"summary": data.get("summary", ""), "changes": changes, "chars": len(text)}


# ─── 简历 PDF 导入 ────────────────────────────────────────────────────────────

RESUME_PROMPT = """下面是用户上传的简历纯文本。请把它拆解成结构化的经历和项目，
返回可入库的改动建议。每段工作/实习 = 一个 create_experience，其下的每个项目放进它的 projects 数组。
项目的 document 用 Markdown 写好（含 标题、一句话亮点、背景、做了什么、结果），technologies 和 keywords 用逗号分隔。

简历文本：
%s

只返回如下 JSON，不要多余文字：
{
  "summary": "识别到 N 段经历、M 个项目",
  "changes": [
    {"type":"create_experience","company":"...","role":"...","start_date":"...","end_date":"...",
     "projects":[{"name":"...","one_liner":"...","document":"# ...","technologies":"...","keywords":"..."}]}
  ]
}"""


@app.post("/api/import/resume")
async def import_resume(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        text = _extract_text(raw, file.filename).strip()
    except Exception as e:
        raise HTTPException(400, f"文件解析失败：{str(e)[:200]}")

    if len(text) < 20:
        raise HTTPException(400, "没从文件里读到足够的文字（可能是扫描版/图片型 PDF）。")

    try:
        out = ai.chat([{"role": "user", "content": RESUME_PROMPT % text[:12000]}], max_tokens=4000)
        data = ai.extract_json(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"简历解析失败（模型返回不合规）：{str(e)[:200]}")
    data.setdefault("summary", "")
    data.setdefault("changes", [])
    data["chars"] = len(text)
    return data


# ─── 面试板块：自我介绍（版本管理）+ 模拟面试 ────────────────────────────────

class InterviewItemIn(BaseModel):
    kind: str = "self_intro"
    title: Optional[str] = None
    target: Optional[str] = None
    content: Optional[str] = None
    project_id: Optional[int] = None


class GenIntroIn(BaseModel):
    target: str = ""
    length: str = "60秒"


class MockIn(BaseModel):
    message: str = ""
    session_id: Optional[str] = None
    target: str = ""


class DebriefIn(BaseModel):
    title: Optional[str] = None
    feedback: str
    project_id: Optional[int] = None
    track_id: Optional[int] = None
    hard_directive: Optional[str] = None


def _full_career_context(limit_chars: int = 6000) -> str:
    exps = db.get_experiences()
    if not exps:
        return "（用户还没有录入任何经历）"
    s = ""
    for e in exps:
        s += f"\n## {e['company']} · {e['role']}（{e.get('start_date','')} ~ {e.get('end_date','至今')}）\n"
        for p in e.get("projects", []):
            proj = db.get_project(p["id"]) or {}
            s += f"### 项目：{p['name']}\n{(proj.get('document') or '')[:800]}\n"
    return s[:limit_chars]


@app.get("/api/interview/items")
def interview_items(kind: Optional[str] = None):
    return db.list_interview_items(kind)


@app.post("/api/interview/items")
def add_interview_item(body: InterviewItemIn):
    iid = db.create_interview_item(body.dict())
    asset_id = None
    if body.kind in ("self_intro", "knowledge"):
        asset_id = db.create_asset({
            "asset_type": body.kind,
            "project_id": body.project_id,
            "title": body.title or ("自我介绍" if body.kind == "self_intro" else "知识卡"),
            "body": body.content or "",
            "status": "draft",
            "provenance_json": json.dumps({
                "intent": "save_interview_item",
                "interview_item_id": iid,
                "target": body.target,
            }, ensure_ascii=False),
        })
    _commit(f"新增面试制品：{body.title or body.kind}")
    return {"id": iid, "asset_id": asset_id}


@app.put("/api/interview/items/{iid}")
def edit_interview_item(iid: int, body: InterviewItemIn):
    db.update_interview_item(iid, body.dict())
    _commit(f"编辑面试制品：{body.title or ''}")
    return {"ok": True}


@app.delete("/api/interview/items/{iid}")
def del_interview_item(iid: int):
    db.delete_interview_item(iid)
    _commit("删除面试制品")
    return {"ok": True}


@app.post("/api/interview/self-intro")
def gen_self_intro(body: GenIntroIn):
    prompt = f"""根据下面这个人的【真实经历】，写一段面试用的【自我介绍】口语稿。
目标岗位/行业：{body.target or '通用'}
时长：{body.length}（据此控制字数，口语化、自然、有重点；突出和目标最相关的经历与量化成果；不浮夸、不编造）。
结构：一句话定位 → 1~2 段最相关经历&成果 → 为什么适合/对它感兴趣。

【他的经历】{_full_career_context()}

直接输出自我介绍正文，不要任何解释或标题。"""
    try:
        text = ai.chat([{"role": "user", "content": prompt}], max_tokens=1200)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"生成失败：{str(e)[:200]}")
    return {"content": text}


MOCK_SYSTEM = """你是一位资深、专业但友善的面试官，正在面试这位求职者。规则：
- 一次只问一个问题，问完就停，等他回答，绝不自问自答。
- 紧扣他的真实经历深入追问：背景 / 你具体做了什么 / 量化结果 / 技术细节 / 权衡取舍 / 难点怎么解决。
- 他回答后，先用一两句简短点评（亮点或不足），再追问下一个问题。
- 适时穿插行为面试题或岗位相关开放题，保持真实面试节奏，有适度压迫感但不刁难。
- 全程中文。第一次发言先简短欢迎，然后直接问第一个问题。"""


@app.post("/api/interview/mock")
def mock_interview(body: MockIn):
    sid = body.session_id or ("mock-" + uuid.uuid4().hex)
    history = db.get_chat_history(sid, limit=20)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": body.message or "（开始面试，请提第一个问题）"})
    system = MOCK_SYSTEM + f"\n本次目标岗位：{body.target or '通用'}\n\n【求职者的经历】" + _full_career_context()
    try:
        reply = ai.chat(messages, system=system, max_tokens=1200)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"调用 AI 失败：{str(e)[:200]}")
    db.save_message(sid, "user", body.message or "（开始面试）")
    db.save_message(sid, "assistant", reply)
    return {"reply": reply, "session_id": sid}


DEBRIEF_PROMPT = """你是面试复盘助手。根据用户提供的真实面试反馈，提取可以回流到求职知识库的内容。

规则：
1. 只提取反馈里真实出现或明确暴露的问题，不编造面试官没问过的问题。
2. followups 是之后要继续准备的项目追问；status 用 weak（答得不稳）或 todo（还没答）。
3. constraints 是以后生成内容时应遵守的软约束，例如表达偏好、强调重点、需要避免的说法。
4. 事实纠正和主导权问题不要擅自升级为 hard；用户会在单独字段明确确认。
5. 返回内容较短，严格使用 JSON。

返回：
{
  "summary": "本轮复盘的一句话结论",
  "followups": [
    {"question":"问题","answer":"反馈中已有的答法，没有则为空","status":"weak","category":"项目贡献"}
  ],
  "constraints": [
    {"directive":"以后生成时应遵守的指令","category":"tone/emphasis/avoidance/interviewer_probe"}
  ]
}

面试反馈：
%s"""


@app.post("/api/interview/debrief")
def save_interview_debrief(body: DebriefIn):
    feedback = (body.feedback or "").strip()
    hard = (body.hard_directive or "").strip()
    if len(feedback) < 10:
        raise HTTPException(400, "复盘内容太短")
    if body.project_id and not db.get_project(body.project_id):
        raise HTTPException(404, "关联项目不存在")
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "关联求职线不存在")
    try:
        raw = ai.chat(
            [{"role": "user", "content": DEBRIEF_PROMPT % feedback[:8000]}],
            max_tokens=1500,
        )
        data = ai.extract_json(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"复盘提取失败：{str(e)[:180]}")

    title = (body.title or "").strip() or "面试复盘"
    aid = db.create_asset({
        "asset_type": "review",
        "project_id": body.project_id,
        "track_id": body.track_id,
        "title": title,
        "body": feedback,
        "status": "final",
        "provenance_json": json.dumps({
            "intent": "interview_debrief",
            "project_id": body.project_id,
            "track_id": body.track_id,
        }, ensure_ascii=False),
    })

    followup_ids = []
    if body.project_id:
        for item in (data.get("followups") or [])[:20]:
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            status = item.get("status") if item.get("status") in ("weak", "todo") else "weak"
            followup_ids.append(db.create_followup(body.project_id, {
                "question": question,
                "answer": str(item.get("answer") or "").strip(),
                "status": status,
                "category": str(item.get("category") or "").strip() or None,
                "origin": title,
                "source_track_id": body.track_id,
                "category_source": "ai" if item.get("category") else None,
            }))

    feedback_ids = []
    constraint_scope = "track" if body.track_id else ("project" if body.project_id else "global")
    constraint_scope_id = body.track_id or body.project_id
    for item in (data.get("constraints") or [])[:12]:
        directive = str(item.get("directive") or "").strip()
        if not directive:
            continue
        feedback_ids.append(db.create_feedback_note({
            "scope": constraint_scope,
            "scope_id": constraint_scope_id,
            "category": str(item.get("category") or "interviewer_probe"),
            "polarity": "do",
            "strength": "soft",
            "directive": directive,
            "content": directive,
            "original_text": feedback,
        }))

    hard_id = None
    if hard:
        hard_id = db.create_feedback_note({
            "scope": "global",
            "category": "fact_correction",
            "polarity": "avoid",
            "strength": "hard",
            "directive": hard,
            "content": hard,
            "original_text": hard,
        })
        stale = db.mark_assets_stale(global_scope=True, exclude_asset_id=aid)
    else:
        stale = db.mark_assets_stale(
            project_id=body.project_id,
            track_id=body.track_id,
            exclude_asset_id=aid,
        )

    _commit(f"面试复盘回流：{title}")
    return {
        "ok": True,
        "summary": data.get("summary") or "复盘已回流",
        "asset_id": aid,
        "followup_ids": followup_ids,
        "feedback_ids": feedback_ids,
        "hard_feedback_id": hard_id,
        "stale_assets": stale,
    }


# ─── 总览 Dashboard ───────────────────────────────────────────────────────────

@app.get("/api/overview")
def overview():
    data = db.overview_stats()
    data["recent"] = vcs.log(8)
    active = ai.get_active_provider()
    data["ai"] = active.get("name") if active else None
    return data


@app.get("/api/ops/brief")
def ops_brief():
    data = db.ops_brief()
    active = ai.get_active_provider()
    data["ai"] = active.get("name") if active else None
    return data


# ─── 在职日记 ─────────────────────────────────────────────────────────────────

class WorkLogIn(BaseModel):
    log_date: Optional[str] = None
    experience_id: Optional[int] = None
    content: str


@app.get("/api/worklogs")
def worklogs(experience_id: Optional[int] = None):
    return db.list_work_logs(experience_id)


@app.post("/api/worklogs")
def add_worklog(body: WorkLogIn):
    wid = db.create_work_log(body.dict())
    _commit("新增在职日记")
    return {"id": wid}


@app.delete("/api/worklogs/{wid}")
def del_worklog(wid: int):
    db.delete_work_log(wid)
    _commit("删除一条在职日记")
    return {"ok": True}


@app.post("/api/worklogs/summary")
def worklog_summary(body: dict):
    eid = body.get("experience_id")
    logs = db.list_work_logs(eid)
    if not logs:
        raise HTTPException(400, "这段经历还没有日记，先记几条。")
    text = "\n".join(f"[{l['log_date']}] {l['content']}" for l in reversed(logs))
    prompt = f"""下面是某人一段实习/工作期间，按天记录的工作日记。请帮他：
1. 提炼这段经历真正的【价值与亮点】（量化成果、能力体现、对业务的影响）。
2. 指出可以包装进简历/面试的 2-3 个【项目级故事】（每个给一句话概括 + 关键数据）。
3. 给出还值得补充记录的方向（哪些没写清、面试可能会追问）。

日记：
{text[:8000]}

用中文，条理清晰，直接给结论。"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=1500)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"summary": out}


WORKLOG_EXTRACT_PROMPT = """你是求职项目梳理助手。请从下面同一段经历的工作日记中识别 1-3 个真正能独立讲清楚的项目候选。

拆分原则：
1. 同一个业务目标、同一套连续动作应合并成一个项目，不要按每天或每个小任务拆碎。
2. 只使用日记里的事实，不编造职责、技术、数字或成果。
3. 项目文档只包含背景、我的动作、结果；面试追问不写进文档。
4. 没有量化证据时，在 MISSING_METRICS 提出具体待补数据，不要替用户编数字。
5. COVERED_LOG_IDS 只能填写输入中真实存在的日志 id。
6. 大段正文不要放进 JSON，严格使用下面的分隔符格式。

每个候选严格按此格式输出，可重复 1-3 次：
===DIGEST===
TITLE: 项目名
ONE_LINER: 一句话说明做了什么和价值；没有可靠结果时不要伪造
TECHNOLOGIES: 工具或方法，逗号分隔
KEYWORDS: 业务关键词，逗号分隔
COVERED_LOG_IDS: 1,2,3
MISSING_METRICS: 待补数据1 | 待补数据2
===BACKGROUND===
用自然段写清业务背景、问题和目标。
===ACTIONS===
写清用户本人做了什么。适合叙述的用自然段，步骤或方法可以用要点。
===RESULTS===
只写有依据的结果；若日记尚无结果，明确写“结果待补充”。
===END_DIGEST===

工作日记：
%s"""


def _parse_int_list(value: str):
    return [int(x) for x in re.findall(r"\d+", value or "")]


def _parse_worklog_digests(raw: str, allowed_log_ids):
    allowed = {int(x) for x in allowed_log_ids}
    chunks = re.findall(r"(?s)===DIGEST===\s*(.*?)\s*===END_DIGEST===", raw or "")
    digests = []
    for chunk in chunks:
        title = _field_line(chunk, "TITLE")
        if not title:
            continue
        one_liner = _field_line(chunk, "ONE_LINER")
        background = _block(chunk, "BACKGROUND")
        actions = _block(chunk, "ACTIONS")
        results = _block(chunk, "RESULTS")
        covered = [x for x in _parse_int_list(_field_line(chunk, "COVERED_LOG_IDS")) if x in allowed]
        missing = [
            x.strip() for x in re.split(r"[|｜]", _field_line(chunk, "MISSING_METRICS"))
            if x.strip() and x.strip().lower() not in ("无", "none", "暂无")
        ]
        document = "\n\n".join([
            f"# {title}",
            f"> {one_liner}" if one_liner else "",
            "## 背景\n" + (background or "（待补充）"),
            "## 我的动作\n" + (actions or "（待补充）"),
            "## 结果\n" + (results or "（待补充）"),
        ]).replace("\n\n\n", "\n\n").strip()
        digests.append({
            "title": title,
            "one_liner": one_liner,
            "technologies": _field_line(chunk, "TECHNOLOGIES"),
            "keywords": _field_line(chunk, "KEYWORDS"),
            "document": document,
            "background": background,
            "actions": actions,
            "results": results,
            "covered_log_ids": covered,
            "missing_metrics": missing,
        })
    return digests


def _worklog_digest_asset_body(digests):
    parts = ["# 在职日记提炼"]
    for item in digests:
        parts.extend([
            f"## {item['title']}",
            item.get("one_liner") or "",
            f"覆盖日记：{', '.join('#' + str(x) for x in item.get('covered_log_ids') or []) or '未标注'}",
            "待补数据：" + ("、".join(item.get("missing_metrics") or []) or "暂无"),
        ])
    return "\n\n".join(x for x in parts if x)


@app.post("/api/worklogs/extract")
def extract_worklogs(body: dict):
    eid = body.get("experience_id")
    if not eid:
        raise HTTPException(400, "请先选择一段经历，再提炼项目。")
    experience = next((x for x in db.get_experiences() if x["id"] == int(eid)), None)
    if not experience:
        raise HTTPException(404, "经历不存在")
    logs = db.list_work_logs(int(eid))
    if not logs:
        raise HTTPException(400, "这段经历还没有日记，先记几条。")
    text = "\n".join(
        f"[log_id={item['id']} | {item.get('log_date') or '日期未知'}]\n{item.get('content') or ''}"
        for item in reversed(logs)
    )
    try:
        raw = ai.chat(
            [{"role": "user", "content": WORKLOG_EXTRACT_PROMPT % text[:12000]}],
            max_tokens=3200,
        )
        digests = _parse_worklog_digests(raw, [x["id"] for x in logs])
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"提炼失败（模型返回无法解析）：{str(e)[:200]}")
    if not digests:
        raise HTTPException(500, "提炼失败：模型没有返回可识别的项目候选，请重试。")
    all_log_ids = sorted({wid for item in digests for wid in item["covered_log_ids"]})
    asset_id = db.create_asset({
        "asset_type": "worklog_digest",
        "title": f"{experience['company']} · {experience['role']} 日记提炼",
        "body": _worklog_digest_asset_body(digests),
        "status": "draft",
        "provenance_json": json.dumps({
            "intent": "worklog_extract",
            "experience_id": int(eid),
            "work_log_ids": all_log_ids,
        }, ensure_ascii=False),
        "derived_from_json": json.dumps(
            [{"type": "work_log", "id": wid} for wid in all_log_ids],
            ensure_ascii=False,
        ),
    })
    _commit(f"提炼在职日记：识别 {len(digests)} 个项目候选")
    return {"digests": digests, "asset_id": asset_id}


@app.post("/api/worklogs/adopt")
def adopt_worklog_digest(body: dict):
    eid = body.get("experience_id")
    digest = body.get("digest") or {}
    mode = body.get("mode") or "create"
    project_id = body.get("project_id")
    if not eid or not digest.get("title"):
        raise HTTPException(400, "缺少经历或项目候选")
    if mode not in ("create", "merge"):
        raise HTTPException(400, "不支持的采纳方式")
    if mode == "merge":
        project = db.get_project(project_id)
        if not project or project.get("experience_id") != int(eid):
            raise HTTPException(400, "请选择当前经历下的项目")
        digest = dict(digest)
        digest["document"] = _merge_project_doc(project, {
            "one_liner": digest.get("one_liner"),
            "background": digest.get("background"),
            "actions": digest.get("actions"),
            "results": digest.get("results"),
        })
        digest["technologies"] = ", ".join(filter(None, [
            project.get("technologies"),
            digest.get("technologies"),
        ]))
    try:
        result = db.adopt_worklog_digest(
            int(eid), digest, mode=mode,
            project_id=int(project_id) if project_id else None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    label = digest.get("title") or "项目候选"
    _commit(("并入项目：" if mode == "merge" else "从日记新建项目：") + label)
    return {"ok": True, **result}


# ─── 知识库（面试用专业知识卡）────────────────────────────────────────────────

class KnowledgeIn(BaseModel):
    topic: str
    target: Optional[str] = ""


@app.post("/api/interview/knowledge")
def gen_knowledge(body: KnowledgeIn):
    prompt = f"""为面试准备，写一张关于「{body.topic}」的【知识卡】{('，结合岗位：'+body.target) if body.target else ''}。
要求：面向面试问答，讲清楚——是什么 / 为什么/什么场景用 / 关键点和坑 / 面试常见追问及简洁答法。
用中文 Markdown，结构清晰、精炼，能直接背。直接输出卡片内容。"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=1600)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"content": out}


# ─── 项目话术（每个项目的面试讲法）────────────────────────────────────────────

@app.post("/api/projects/{pid}/pitch")
def gen_project_pitch(pid: int):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    ctx = caddie_context.build_context(project_id=pid, intent="project_pitch")
    prompt = f"""请基于下面的【统一上下文】写一份【项目面试讲法话术】。

{ctx["text"][:9000]}

请产出：
## 30 秒电梯版（口语，突出成果和你的角色）
## 2 分钟详述版（背景-任务-行动-结果，STAR，带数字）
## 高频追问 & 我的答法（列 4-6 个面试官最可能追问的点，每个给简洁有力的答法）

要求：
- 用中文 Markdown，口语化、可直接背。
- 只基于上下文中的事实，不编造数字、职责或成果。
- 严格遵守【生效反馈约束】，尤其是 hard/铁律类约束。
"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=2200)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    aid = db.create_asset({
        "asset_type": "project_pitch",
        "project_id": pid,
        "title": (p.get("name") or "项目") + " · 面试话术",
        "body": out,
        "status": "draft",
        "provenance_json": caddie_context.provenance_payload(ctx),
    })
    _commit(f"生成项目话术草稿：{p.get('name') or pid}")
    return {"content": out, "title": p.get("name"), "project_id": pid, "asset_id": aid}


# ─── JD 对比（岗位描述 vs 你的画像，找差距）──────────────────────────────────

class JDIn(BaseModel):
    job_description: str
    analyze: bool = True  # False = 只存 JD，不跑 AI 分析


@app.post("/api/applications/{aid}/jd-analyze")
def jd_analyze(aid: int, body: JDIn):
    app_data = db.get_application(aid)
    if not app_data:
        raise HTTPException(404, "投递不存在")
    jd = body.job_description.strip()
    if len(jd) < 10:
        raise HTTPException(400, "JD 内容太短")
    if not body.analyze:
        db.set_application_jd(aid, jd)
        _commit(f"存储 JD：{app_data.get('company')}")
        return {"analysis": ""}
    prompt = f"""把这份岗位 JD 和求职者的真实画像做对比分析。
岗位：{app_data.get('company')} · {app_data.get('role')}
JD：
{jd[:4000]}

求职者画像（经历/项目）：
{_full_career_context()}

请输出（中文 Markdown）：
## ✅ 匹配点（他已经具备、可重点突出的）
## ⚠️ 差距 / 缺口（JD 要求但他画像里弱或没有的）
## 📚 该补的知识/准备（针对缺口，具体到知识点或要准备的故事）
## 🎯 投递/面试建议（一句话该怎么扬长避短）
基于事实，别编造他没有的经历。"""
    try:
        analysis = ai.chat([{"role": "user", "content": prompt}], max_tokens=1800)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    db.set_application_jd(aid, jd, analysis)
    _commit(f"JD 对比分析：{app_data.get('company')}")
    return {"analysis": analysis}


@app.post("/api/applications/{aid}/jd-actions")
def jd_actions(aid: int):
    app_data = db.get_application(aid)
    if not app_data:
        raise HTTPException(404, "投递不存在")
    jd = (app_data.get("job_description") or "").strip()
    analysis = (app_data.get("gap_analysis") or "").strip()
    if not jd:
        raise HTTPException(400, "这条投递还没有 JD，先粘贴 JD。")
    if not analysis:
        raise HTTPException(400, "还没有差距分析，先点「对比分析」。")
    prompt = f"""你是求职操作系统的任务规划器。请把下面的 JD 差距分析转成一份可执行的行动清单。

岗位：{app_data.get('company')} · {app_data.get('role')}
JD 摘要：
{jd[:1800]}

已有差距分析：
{analysis[:5000]}

输出中文 Markdown，必须包含这 4 个部分：
## 简历要改
- 3-5 条，写清楚改哪段经历/项目、补什么证据或数据
## 面试要练
- 5-8 个高频追问，按优先级排序
## 知识要补
- 3-6 个知识卡主题，每个说明为什么要补
## 今天就做
- 3 个 30 分钟内能完成的小动作

要求：具体、可执行，不要泛泛而谈，不要编造用户没有的经历。"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=1800)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"actions": out}


# ─── 双视角自查（求职者视角 ↔ 面试官视角）─────────────────────────────────────

class ReviewIn(BaseModel):
    text: str
    context: Optional[str] = ""


@app.post("/api/review")
def dual_review(body: ReviewIn):
    if len(body.text.strip()) < 10:
        raise HTTPException(400, "内容太短")
    prompt = f"""下面是求职者的一段材料{('（'+body.context+'）') if body.context else ''}。请从两个视角做"自查"，帮他打磨：

材料：
{body.text[:5000]}

输出（中文 Markdown）：
## 🙋 求职者视角（怎么讲更打动人、更清楚）
- 哪里可以更突出成果/数据、更有说服力；表达上怎么改更好（给具体改法）。
## 🧐 面试官视角（会怎么质疑、追问、挑漏洞）
- 面试官看到这段会追问哪些问题？哪里听起来可疑或站不住脚？他需要提前准备什么？
## ✍️ 一句话总结：最该改的一点
直接、具体、可操作。"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=1600)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"review": out}


# ─── 邮箱接入（网易 163/126 IMAP，投递邮件按时间整理）────────────────────────

EMAIL_CFG = ai.CONFIG_DIR / "email.json"


class EmailCfgIn(BaseModel):
    host: str = "imap.163.com"
    user: str
    authcode: str = ""
    clear: bool = False


def _email_cfg():
    if EMAIL_CFG.exists():
        try:
            return json.loads(EMAIL_CFG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


@app.get("/api/email/config")
def email_config():
    c = _email_cfg()
    return {"host": c.get("host", "imap.163.com"), "user": c.get("user", ""), "has_pass": bool(c.get("authcode"))}


@app.post("/api/email/config")
def save_email_config(body: EmailCfgIn):
    c = _email_cfg()
    # clear=断开（清空授权码）；否则授权码留空＝保留原值（方便只改邮箱）
    authcode = "" if body.clear else (body.authcode or c.get("authcode", ""))
    d = {"host": body.host.strip(), "user": body.user.strip(), "authcode": authcode}
    ai.CONFIG_DIR.mkdir(exist_ok=True)
    EMAIL_CFG.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


def _imap_connect():
    """连接 163/126 IMAP，发 ID 命令，选中 INBOX，返回 (M, imaplib)。调用方负责 M.logout()。"""
    import imaplib
    c = _email_cfg()
    if not c.get("user") or not c.get("authcode"):
        raise HTTPException(400, "还没配置邮箱（去设置填邮箱和 IMAP 授权码）。")
    M = imaplib.IMAP4_SSL(c.get("host", "imap.163.com"), 993)
    M.login(c["user"], c["authcode"])
    try:
        tag = M._new_tag()
        M.send(tag + b' ID ("name" "Foxmail" "version" "7.2" "vendor" "NetEase")\r\n')
        for _ in range(5):
            if M.readline().startswith(tag):
                break
    except Exception:
        pass
    typ, _ = M.select("INBOX")
    if typ != "OK":
        raise HTTPException(400, "SELECT INBOX 失败，请在 163 邮箱设置 → POP3/IMAP 里确认 IMAP 服务已开启")
    return M, imaplib


def _imap_decode(s):
    from email.header import decode_header
    if not s:
        return ""
    out = ""
    for part, enc in decode_header(s):
        out += part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part
    return out


def _imap_body_text(M, num):
    """提取单封邮件的纯文本正文（最多 800 字）。"""
    import email as emaillib
    import re
    try:
        typ, bd = M.fetch(num, "(RFC822)")
        if typ != "OK" or not bd or not bd[0] or not isinstance(bd[0], tuple):
            return ""
        msg = emaillib.message_from_bytes(bd[0][1])
        text = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        text = part.get_payload(decode=True).decode(charset, errors="ignore")
                        break
                    except Exception:
                        pass
        else:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = msg.get_payload(decode=True).decode(charset, errors="ignore")
            except Exception:
                pass
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:800]
    except Exception:
        return ""


@app.get("/api/email/fetch")
def email_fetch(days: int = 30, q: str = ""):
    import email as emaillib
    from email.utils import parsedate_to_datetime
    from datetime import timedelta

    kws = ["简历", "面试", "笔试", "投递", "录用", "offer", "Offer", "感谢", "邀请", "测评", "申请", "通知", "录取"]
    try:
        M, imaplib = _imap_connect()
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        ids = data[0].split()[-120:]
        items = []
        for num in reversed(ids):
            typ, d = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not d or not d[0]:
                continue
            msg = emaillib.message_from_bytes(d[0][1])
            subj = _imap_decode(msg.get("Subject"))
            frm = _imap_decode(msg.get("From"))
            try:
                dt = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt = ""
            blob = subj + " " + frm
            if q and q not in blob:
                continue
            job = any(k in blob for k in kws)
            items.append({"date": dt, "from": frm[:60], "subject": subj, "job": job})
        M.logout()
        items.sort(key=lambda x: x["date"], reverse=True)
        return {"items": items, "count": len(items)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"拉取失败：{str(e)[:160]}")


@app.post("/api/email/analyze")
def email_analyze(days: int = 30):
    """拉取近期邮件正文，AI 识别投递阶段，返回 changes 供用户确认后写入看板。"""
    import email as emaillib
    from email.utils import parsedate_to_datetime
    from datetime import timedelta

    kws = ["简历", "面试", "笔试", "投递", "录用", "offer", "Offer", "感谢", "邀请", "测评", "申请", "通知", "录取", "拒绝", "遗憾"]
    try:
        M, imaplib = _imap_connect()
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        ids = data[0].split()[-150:]

        # 第一步：拉所有邮件头，过滤出求职相关的（快）
        job_emails = []
        for num in reversed(ids):
            typ, d = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not d or not d[0]:
                continue
            msg = emaillib.message_from_bytes(d[0][1])
            subj = _imap_decode(msg.get("Subject"))
            frm = _imap_decode(msg.get("From"))
            try:
                dt = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt = ""
            if any(k in (subj + frm) for k in kws):
                job_emails.append({"_num": num, "date": dt, "from": frm[:80], "subject": subj})

        # 第二步：为求职邮件拉正文（最多 30 封）
        for em in job_emails[:30]:
            em["body"] = _imap_body_text(M, em["_num"])

        M.logout()

        # 清掉内部字段
        for em in job_emails:
            em.pop("_num", None)

        if not job_emails:
            return {"items": [], "summary": f"近 {days} 天没有发现与求职相关的邮件", "email_count": 0}

        # 第三步：AI 识别——每封邮件返回一个 item（含 email_index）
        apps = db.get_applications()
        apps_ctx = [{"id": a["id"], "company": a["company"], "role": a["role"],
                     "status": a["status"], "applied_date": a.get("applied_date", "")}
                    for a in apps]

        system = f"""你是求职管家的邮件智能识别模块。
分析邮件列表，每封求职相关邮件输出一个 item，用于用户逐条确认后写入投递看板。

【状态值（只能用以下值）】
- applied   = 已投递（收到投递确认、感谢申请）
- screening = 筛选中（简历审阅中、已查看简历）
- written   = 笔试（收到笔试/在线测评邀请）
- interview = 面试中（收到面试邀请）
- offer     = Offer（录用通知）
- rejected  = 未通过（拒信、遗憾通知）

【已有投递记录】（匹配公司名时参考）
{json.dumps(apps_ctx, ensure_ascii=False, indent=2)}

【规则】
1. 每封求职邮件对应一个 item，email_index 填该邮件的编号（如【3】→ 3）
2. 公司名与已有记录相近 → type = "set_application_status"，填 application_id
3. 全新投递 → type = "create_application"
4. 非求职邮件（广告/系统通知/行程/购物等）→ 直接忽略，不生成 item
5. 同一公司多封邮件只生成最新状态的一个 item

【返回格式】严格 JSON，不加 markdown：
{{
  "summary": "一句话总结",
  "items": [
    {{"email_index": 1, "type": "create_application", "company": "字节跳动", "role": "数据产品经理", "status": "applied", "applied_date": "2026-05-21"}},
    {{"email_index": 3, "type": "set_application_status", "application_id": 2, "company": "美团", "role": "数据分析师", "status": "interview"}}
  ]
}}"""

        user_msg = f"近 {days} 天的求职相关邮件共 {len(job_emails)} 封：\n\n"
        for i, em in enumerate(job_emails, 1):
            body_preview = em.get("body", "")[:400]
            user_msg += f"【{i}】{em['date']}\n发件人：{em['from']}\n主题：{em['subject']}\n正文：{body_preview}\n\n"

        resp = ai.chat([{"role": "user", "content": user_msg}], system=system, max_tokens=2000)
        result = ai.extract_json(resp)
        if not isinstance(result, dict):
            result = {"items": [], "summary": "AI 解析失败，请重试"}

        # 把对应邮件的元数据嵌入每个 item，方便前端展示
        items = result.get("items", [])
        for item in items:
            idx = item.get("email_index", 0)
            if 1 <= idx <= len(job_emails):
                em = job_emails[idx - 1]
                item["email"] = {"date": em["date"], "from": em["from"], "subject": em["subject"]}

        return {
            "items": items,
            "summary": result.get("summary", ""),
            "email_count": len(job_emails)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"分析失败：{str(e)[:200]}")
