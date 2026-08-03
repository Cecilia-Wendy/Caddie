# Caddie · AI 求职管家

> 帮「真正有料的人」把做过的事讲清楚——不作弊，不埋没。

本地优先、对话优先的个人求职操作系统。核心不是记录"投了哪里"，而是解决更深的问题：**你明明做过有价值的事，但坐下来就不知道怎么讲**。

---

## 它解决什么问题

市面上的求职工具，大多是记录层面的（看板、提醒、状态追踪）。但大多数求职者真正卡住的地方不是"忘了投哪里"，而是：

- 项目经历散在脑子里，面试前临时拼凑
- 面试官一追问细节（"数据怎么来的""你贡献多少"），就语塞
- 每换一个岗位，重写话术又是一通折腾
- AI 生成的内容没有个人背景，全是套话

Caddie 从根上解决这个问题：把你真实做过的事结构化，让 AI 在你的真实背景里帮你讲，而不是替你编。

---

## 核心功能

### 📁 经历 · 项目库

每个项目是一个**容器**，而不是一段长文档：

- **事实文档**（背景 / 我的动作 / 结果 / 量化），WYSIWYG 富文本，永远不见 `#` `**` 这些符号
- **追问题库**：每个项目配一套面试追问库，AI 打磨答法，状态追踪（稳 / 虚 / 待补）
- **衍生资产**：针对不同岗位生成的定制话术，全局和求职线两级，带出处和版本

### 📔 在职日记引擎

不是"收集器"，是"提炼器"。

随手记工作片段 → AI 识别属于哪个项目 → 生成结构化摘要 → 一键采纳进入项目库。碎片变资产的最短路径。

### ❓ 追问题库 + 两种润色策略

追问分两类，处理策略完全不同：

| 类型 | 例子 | AI 策略 |
|---|---|---|
| **深挖型**（事实在你脑子里） | "数据怎么来的" "你具体做了哪些" | 只用你的真实信息组织答案；缺关键细节就打占位 `（这里需要你补：…）`，绝不编造 |
| **反思型**（AI 比你更客观） | "最大不足是什么" "换个方案你会怎么做" | 基于项目事实主动复盘，给有洞察的分析，推测处标 `（假设）` 供你拍板 |

AI 自动归类，你改了分类它记住，下次照你的来——「会长大的分类法」。

### 🎯 求职线 + 邮件识别

每条求职线配独立的定制话术库；邮箱授权后 AI 自动识别投递邮件，解析阶段写入看板。

### 🎤 面试练习

自我介绍生成（可指定目标岗位和风格），AI 模拟面试 + 复盘回流，面试中拿到的新追问可回流进项目追问库。

---

## AI 架构设计

### context.py — 上下文装配层

所有 AI 生成，无论是话术、追问答案还是复盘，都先经过统一的上下文装配：

```
build_context(project_id, track_id, intent)
  ├── 项目事实 + 完整文档
  ├── 相关原始资料（source_links）
  ├── 同项目已有追问（保持答案一致性）
  ├── 已生成的相关资产引用
  └── 生效的反馈约束（全局 / 求职线 / 项目三级）
```

AI 不是每次独立生成，而是在你完整的个人背景下生成。这是输出质量有保证的根本原因。

### 反馈约束注入（Feedback Loop）

```
用户改了 AI 的分类/措辞
  → 写入 feedback_notes（硬约束 / 偏好）
    → 每次 build_context 自动注入
      → 下次 AI 自动遵守
```

输出越用越像你，不靠 fine-tune，靠结构化记忆。

### Provenance（生成出处追溯）

每份生成资产带 `provenance_json`，记录生成时的 intent、关联的项目/求职线/资料 id。项目文档更新时，关联资产自动标 `stale`，提示需要重生成。

---

## 产品设计原则

| 原则 | 体现 |
|---|---|
| 永不直面 Markdown | WYSIWYG 编辑器（Toast UI）+ 自定义渲染层；用户看不到 `#` `**` |
| 输出即作品 | 三级视觉层次（eyebrow / kv / kv-hot）；生成内容直接是成品排版 |
| 简单是功能 | 导航从 12 个收敛到 6 个分组入口 |
| 本地优先 | 所有数据 `~/.caddie/`；检索与文档处理在本机完成 |
| 会长大 | 你的修正（分类、措辞偏好）AI 记住，越用越像你 |

---

## 技术栈

| 层 | 选型 | 原因 |
|---|---|---|
| 后端 | Python · FastAPI | AI 生态原生，LLM 调用最顺 |
| 数据库 | SQLite + WAL 模式 | 本地优先，WAL 解决并发写锁 |
| 检索 | SQLite FTS5 + FastEmbed BGE 中文向量 | 精确关键词与语义召回合并，向量留在本机 |
| 前端 | 无框架单页 + React 编辑器岛 | 主应用保持轻量，知识编辑器独立构建 |
| 编辑器 | BlockNote（主）+ Toast UI（回退） | 飞书式块编辑，本地 vendored，不走 CDN |
| 版本控制 | 本地 Git（vcs.py） | 每次 AI 写入自动存档，可回溯可回滚 |
| AI 供应商 | 可插拔（Claude / OpenAI 兼容 / Ollama） | 不绑定单一厂商 |

---

## 快速开始

需要 **Python 3.10+**。（版本历史功能需要本机装有 `git`，没装也能正常使用其它功能。）

**macOS / Linux**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py        # 自动打开桌面窗口；未安装 pywebview 则回退浏览器
```

**Windows**（PowerShell）

```powershell
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py        # 桌面窗口需 Windows 10/11 自带的 WebView2，否则自动回退浏览器
```

> Windows 用 CMD 的话，激活命令换成 `.\.venv\Scripts\activate.bat`。
> 如果浏览器没自动打开，手动访问 `http://127.0.0.1:8766`。

首次使用：左侧「⚙️ 设置」→ 添加 AI 供应商（填 API Key、测试连接）→ 开始使用。

首次触发语义检索时会下载约 90MB 的 `BAAI/bge-small-zh-v1.5` 模型到
项目的 `.models/` 目录。下载完成后向量生成与检索均可离线运行；如果模型暂时
不可用，系统会自动降级为 SQLite FTS5 关键词检索，不影响基本聊天功能。

数据存储在用户主目录的 `.caddie/`（macOS/Linux：`~/.caddie/`；Windows：`C:\Users\<你>\.caddie\`），是 SQLite + 本地版本库，**代码仓库不含任何个人数据**。

### 构建知识编辑器

日常运行不需要 Node。只有修改 `frontend/knowledge-editor/` 中的 BlockNote
编辑器源码时，才需要重新构建：

```bash
cd frontend/knowledge-editor
npm install
npm run build
```

构建产物会写入 `static/vendor/knowledge-editor/`，由 FastAPI 本地提供。

### 接入外部 Agent（MCP）

Caddie 可以作为本地 stdio MCP Server，供 Claude Desktop、Codex、WorkBuddy 或其他兼容
MCP 的 Agent 使用。外部 Agent 可以读取岗位与公司档案、检索资料、读取文档，或使用
`read_context_package` 获得带版本指纹、硬/软反馈约束的最小上下文；它们可以创建任务并
提交候选修改，但不能绕过 Caddie 的确认流程直接改写正式内容。

以 Claude Desktop 的 MCP 配置为例：

```json
{
  "mcpServers": {
    "caddie": {
      "command": "/Users/你的用户名/.caddie/bin/caddie-mcp"
    }
  }
}
```

安装版 Caddie 首次启动会自动创建稳定入口，使用 App 内置运行时，不依赖系统 Python、
源码目录或项目 `.venv`。也可以直接运行：

```bash
~/.caddie/bin/caddie-mcp
```

当前还提供三类 Agent 协作能力：

- **启动与同步**：读取工作区协议、创建外部运行、追加进度、通过变更游标读取用户后续修改；
- **低风险草稿**：直接保存带出处的 `draft` 资产，例如一版待审自我介绍或岗位研究；
- **需确认变更**：事实、数字、贡献边界、反馈约束和正式文档只能提交候选，必须在 Caddie
  的 Agent 工作台确认后才会生效。

推荐外部 Agent 先调用 `workspace_bootstrap`，再调用 `get_workspace_map`
定位岗位、项目、文档与稳定写入位置。写回时优先使用
`save_career_document`，并明确 `destination`：

- `job.company` / `job.role` / `job.professional` / `job.interview` / `job.review`
- `draft`
- `existing_document`

岗位目录写入与现有文档更新仍需在 Caddie 确认；`draft` 会保存为带来源的可编辑草稿。
WorkBuddy 可在 Caddie 设置页一键写入用户级 MCP 配置，也可在 WorkBuddy 的
“插件 → MCP 服务器”中查看连接状态。

这样 Codex、Claude Code 或其他 Agent 可以成为主操作台，而 Caddie 仍是本地职业资产、
版本、证据与用户判断的唯一落点。文档候选还会检查原文版本，避免覆盖用户的新修改。

### 不使用 MCP 的终端接入（CLI）

需要让任意本地 Agent 或脚本按同样规则工作时，可以调用：

```bash
# 先查看流程；再一次验证连接、权限并找到稳定 ID
zsh ./scripts/caddie-agent.sh guide
zsh ./scripts/caddie-agent.sh doctor --agent-key codex --company 字节跳动
# 选定岗位后，只读取这次任务需要的上下文
zsh ./scripts/caddie-agent.sh context --track-id 12 --intent "准备业务二面"
# 登记运行，把正式岗位准备内容送入 Caddie 等待确认
zsh ./scripts/caddie-agent.sh start-run --track-id 12 --title "准备业务二面" \
  --instruction "整理项目表达和高频追问"
zsh ./scripts/caddie-agent.sh save-document --track-id 12 --destination job.interview \
  --title "二面高频问题" --body "..." --task-id TASK_ID
zsh ./scripts/caddie-agent.sh complete TASK_ID --summary "已提交面试准备文档候选"
# 用户确认、编辑或拒绝后，Agent 回读最终结果和后续变更
zsh ./scripts/caddie-agent.sh package TASK_ID
zsh ./scripts/caddie-agent.sh changes --after CURSOR
```

CLI 与 MCP 共用同一条实现和审批边界：`draft` 可以直接保存，事实、反馈和正式文档
必须先 `propose-*`，再由用户在 Caddie 的更新包中确认。需要改写原文时，先读取
`context`，再在写回前重新读取并比较目标对象的 `revision`；默认 `context` 只返回最小摘要和
按需读取目录，避免把整库资料与旧话术重复塞给模型；服务端也会使用原文哈希
拦截过期候选，避免覆盖用户的新修改。岗位状态与面试排期可通过 `propose-job` 和
`propose-interview` 提交候选，但不能绕过用户确认直接改库。用 `guide` 看完整流程，
用 `--help` 查看全部命令。

### macOS 登录后自动运行

运行 `./scripts/caddie-open.sh` 会安装并启动 `scripts/com.caddie.server.plist`。
Caddie 会在登录时自动启动，并在意外退出后重启。安装脚本会通过
`~/.caddie/app` 的英文软链接访问项目，避免 macOS 启动项解析中文路径失败。
服务日志位于 `~/.caddie/logs/`。

---

🤖 Built with [Claude Code](https://claude.com/claude-code)
