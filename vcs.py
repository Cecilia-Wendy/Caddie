"""
Caddie - 版本管理（本地 Git）

把数据库里的经历/项目/投递/网页镜像成【可读的 Markdown】，放进 ~/.caddie/vault 这个 git 仓库，
每次改动自动 commit 一条记录。于是你能看历史、看 diff、回滚。
（API Key 等敏感配置在 ~/.caddie/config.json，不在 vault 里，永远不会被提交。）

之后要推到 GitHub，再加 remote + push 即可。
"""
import re
import threading
import subprocess
from pathlib import Path

import db

CADDIE_DIR = Path.home() / ".caddie"
VAULT = CADDIE_DIR / "vault"


def _run(args):
    # core.quotePath=false 让中文文件名正常显示，不变成八进制转义
    return subprocess.run(["git", "-c", "core.quotePath=false", *args],
                          cwd=str(VAULT), capture_output=True, text=True)


def _slug(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (s or "").strip())
    return s[:36] or "untitled"


def ensure_repo():
    VAULT.mkdir(parents=True, exist_ok=True)
    if not (VAULT / ".git").exists():
        _run(["init"])
        _run(["config", "user.email", "caddie@local"])
        _run(["config", "user.name", "Caddie"])
        (VAULT / ".gitignore").write_text("", encoding="utf-8")
        _mirror()
        _run(["add", "-A"])
        _run(["commit", "-m", "初始化职业记忆库"])


def _mirror():
    """把当前数据库状态全量写成 Markdown 文件（删除的项目对应文件也会消失）。"""
    pdir = VAULT / "projects"
    pdir.mkdir(exist_ok=True)
    for f in pdir.glob("*.md"):
        f.unlink()

    exps = db.get_experiences()
    overview = ["# 经历总览\n"]
    for e in exps:
        overview.append(f"- **{e['company']}** · {e['role']}"
                        f"（{e.get('start_date','')} ~ {e.get('end_date','至今')}）")
        for p in e.get("projects", []):
            proj = db.get_project(p["id"]) or {}
            fn = f"{p['id']:03d}-{_slug(e['company'])}-{_slug(p['name'])}.md"
            front = (f"---\n项目: {proj.get('name','')}\n公司: {e['company']}\n"
                     f"一句话: {proj.get('one_liner') or ''}\n"
                     f"技术: {proj.get('technologies') or ''}\n---\n\n")
            (pdir / fn).write_text(front + (proj.get("document") or ""), encoding="utf-8")
            overview.append(f"    - {p['name']}  →  projects/{fn}")
    (VAULT / "经历总览.md").write_text("\n".join(overview) + "\n", encoding="utf-8")

    apps = db.get_applications()
    al = ["# 投递记录\n", "| 公司 | 岗位 | 行业 | 渠道 | 投递日 | 状态 |",
          "|---|---|---|---|---|---|"]
    for a in apps:
        al.append(f"| {a['company']} | {a['role']} | {a.get('industry') or ''} | "
                  f"{a.get('source') or ''} | {a.get('applied_date') or ''} | "
                  f"{db.STATUS_LABEL.get(a['status'], a['status'])} |")
    (VAULT / "投递记录.md").write_text("\n".join(al) + "\n", encoding="utf-8")

    site = CADDIE_DIR / "site.html"
    if site.exists():
        (VAULT / "个人网页.html").write_text(
            site.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")


def _has_remote() -> bool:
    return "origin" in _run(["remote"]).stdout.split()


def push():
    """推到 GitHub 远端（有 origin 才推）。失败静默，不影响本地使用。"""
    try:
        if _has_remote():
            _run(["push", "origin", "main"])
    except Exception:
        pass


def sync_and_commit(message: str) -> bool:
    """镜像当前状态并提交一条记录；提交成功后在后台自动推送到远端。无改动则跳过。"""
    try:
        ensure_repo()
        _mirror()
        _run(["add", "-A"])
        r = _run(["commit", "-m", message])
        if r.returncode == 0 and _has_remote():
            threading.Thread(target=push, daemon=True).start()
        return r.returncode == 0
    except Exception:
        return False


def log(limit: int = 80):
    ensure_repo()
    r = _run(["log", f"-{limit}",
              "--pretty=format:%h\x1f%ad\x1f%s",
              "--date=format:%Y-%m-%d %H:%M"])
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            out.append({"hash": parts[0], "date": parts[1], "msg": parts[2]})
    return out


def show(h: str):
    ensure_repo()
    # 安全：只接受短哈希字符
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", h or ""):
        return {"stat": "", "diff": "无效的版本号"}
    stat = _run(["show", h, "--stat", "--pretty=format:%s%n%ad",
                 "--date=format:%Y-%m-%d %H:%M"]).stdout
    diff = _run(["show", h, "--pretty=format:", "--unified=2"]).stdout
    return {"stat": stat, "diff": diff[:30000]}


def status_summary():
    """给前端显示：仓库路径 + 提交数 + 远端同步状态。"""
    ensure_repo()
    n = _run(["rev-list", "--count", "HEAD"]).stdout.strip()
    remote = ""
    ahead = ""
    if _has_remote():
        remote = _run(["remote", "get-url", "origin"]).stdout.strip()
        a = _run(["rev-list", "--count", "origin/main..HEAD"]).stdout.strip()
        ahead = a if a.isdigit() else ""
    return {"vault": str(VAULT), "commits": n or "0", "remote": remote, "ahead": ahead}
