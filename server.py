"""
Caddie - FastAPI 后端
"""
import uuid
import json
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

app = FastAPI(title="Caddie")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup():
    db.init_db()
    vcs.ensure_repo()   # 初始化版本仓库并回填一条初始记录


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


class ApplicationIn(BaseModel):
    company: str
    role: str
    industry: Optional[str] = None
    applied_date: Optional[str] = None
    status: Optional[str] = "applied"
    source: Optional[str] = None
    notes: Optional[str] = None


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
    results = db.apply_changes(body.changes)
    _commit("整理入库：" + "；".join(results)[:80] if results else "整理入库")
    return {"ok": True, "results": results}


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


PROJECT_PROPOSE_PROMPT = """你在帮用户完善一个项目的文档。根据【当前文档】和【你们的对话】，产出改进后的【完整项目文档】。
要求：在原文基础上补充对话里出现的新信息，不要删掉已有的有效内容；用 Markdown；
结构包含：标题、一句话亮点(用 > 引用块)、背景、我做了什么、结果(尽量量化)、可能被追问的点。

当前文档：
%s

对话：
%s

只返回如下 JSON，不要任何多余文字：
{
 "summary": "这次补充/改了什么（一句话）",
 "one_liner": "更新后的一句话亮点",
 "technologies": "逗号分隔",
 "keywords": "逗号分隔",
 "document": "改进后的完整 Markdown 文档"
}"""


@app.post("/api/projects/{pid}/propose")
def project_propose(pid: int):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    history = db.get_chat_history(f"proj-{pid}", limit=40)
    if not history:
        return {"summary": "还没聊过，先在右边和 Caddie 聊聊这个项目", "changes": []}
    convo = "\n".join(f"{'用户' if m['role']=='user' else 'Caddie'}：{m['content']}" for m in history)
    prompt = PROJECT_PROPOSE_PROMPT % (p.get("document") or "（空）", convo[:8000])
    try:
        raw = ai.chat([{"role": "user", "content": prompt}], max_tokens=3000)
        data = ai.extract_json(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"整理失败（模型没返回合规 JSON）：{str(e)[:200]}")
    if not data.get("document"):
        return {"summary": "没有可更新的内容", "changes": []}
    changes = [{
        "type": "update_project", "project_id": pid, "name": p.get("name"),
        "one_liner": data.get("one_liner"), "technologies": data.get("technologies"),
        "keywords": data.get("keywords"), "document": data.get("document"),
        "reason": data.get("summary"),
    }]
    return {"summary": data.get("summary", ""), "changes": changes}


# ─── 文件文字提取（PDF / 纯文本通用）─────────────────────────────────────────

def _extract_text(raw: bytes, filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    return raw.decode("utf-8", errors="ignore")


# ─── 项目资料上传：解析文件并并入项目文档 ────────────────────────────────────

PROJECT_FILE_MERGE_PROMPT = """用户为【某个项目】上传了一份资料文件，请把其中和这个项目相关的有用信息
合并进项目文档。要求：在原文档基础上补充，不要删掉已有有效内容；用 Markdown；
保留并维护这些小节：标题、一句话亮点(> 引用)、背景、我做了什么、结果(量化)、
以及【可能被追问 / 我的答法】（把资料里能预判到的面试问题和答法整理进来）。

当前项目文档：
%s

上传的资料文件「%s」内容：
%s

只返回如下 JSON，不要多余文字：
{
 "summary": "从这份资料里并入了什么（一句话）",
 "one_liner": "更新后的一句话亮点",
 "technologies": "逗号分隔",
 "keywords": "逗号分隔",
 "document": "合并后的完整 Markdown 文档"
}"""


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
        data = ai.extract_json(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"合并失败（模型返回不合规）：{str(e)[:200]}")
    if not data.get("document"):
        return {"summary": "没从资料里提取到可并入的内容", "changes": [], "chars": len(text)}
    changes = [{
        "type": "update_project", "project_id": pid, "name": p.get("name"),
        "one_liner": data.get("one_liner"), "technologies": data.get("technologies"),
        "keywords": data.get("keywords"), "document": data.get("document"),
        "reason": data.get("summary"),
    }]
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
