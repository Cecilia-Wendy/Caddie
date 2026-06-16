# Caddie 求职管家

本地优先、对话优先的 AI 求职管家。把你的经历讲给它，它帮你拆解建档、随时检索；面试反馈回流，持续打磨。**所有数据只存在你本地**，可接任意 AI 供应商。

> 球童（Caddie）：全程帮你管装备、在每一杆递上对的那根球杆的人——这就是它想做的事。

## 功能

- **聊天台**：对话/语音听写把经历喂进去，AI 自动整理；多会话、跨会话共享你的职业记忆；「🗂 整理入库」把对话变成结构化更新，确认后才写入。
- **经历 · 项目**：每个项目是一份可读可改的文档；右侧专属对话（追问/直接整理两种模式）一起完善；📎 上传资料文件自动并入；简历 PDF 一键拆解建档。
- **投递看板**：看板 + 表格两种视图，追踪进度、汇总行业分布。
- **个人网页**：载入你的作品集 HTML，对照经历生成「只改文字、不动风格」的修改建议，逐条确认后应用、可下载。
- **🔍 全局检索**：随时检索经历、项目、关键词。
- **🕘 版本历史**：每次改动自动用本地 Git 记录一版，可看历史 diff、回滚。
- **⚙️ 多 AI 供应商**：Claude / OpenAI / DeepSeek / Kimi / 智谱 / 硅基流动 / 本地 Ollama / 自定义，一键切换。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py            # 打开桌面窗口；未装 pywebview 则回退浏览器
```

仅浏览器运行：
```bash
pip install fastapi uvicorn requests python-multipart pypdf
uvicorn server:app --port 8766      # 浏览器打开 http://127.0.0.1:8766
```

首次使用：左侧「设置」→ 添加一个 AI 供应商（选预设、填 API Key、测试连接、保存），即可开始。

## 数据与隐私

- 全部数据存本地 `~/.caddie/`：`caddie.db`（SQLite）、`config.json`（含 API Key，明文存本地，**不会被上传**）、`vault/`（版本库）。
- 版本历史默认**纯本地 Git**。仓库本身（本项目代码）才放 GitHub，你的个人数据不在其中。

## 技术栈

Python · FastAPI · SQLite · pywebview · 本地 Git 版本管理 · 可插拔 AI 供应商（Anthropic / OpenAI 兼容）。

---

🤖 Built with [Claude Code](https://claude.com/claude-code)
