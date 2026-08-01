"""Small local CLI for Agent clients that do not speak MCP.

It deliberately delegates to the MCP tool functions, so CLI and MCP retain the
same approval boundary instead of growing two incompatible write paths.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import caddie_mcp
import db


def _json(value: str | None, default: Any = None) -> Any:
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 参数格式错误：{exc.msg}") from exc


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _workflow_guide() -> dict:
    return {
        "purpose": "让不支持 MCP 的 Agent 通过 CLI 使用与 MCP 相同的读取、候选写入和审批规则。",
        "golden_path": [
            {"step": 1, "action": "doctor", "result": "验证连接、读取权限与稳定 ID"},
            {"step": 2, "action": "context / search / read", "result": "只读取完成任务所需的上下文"},
            {"step": 3, "action": "start-run", "result": "在 Caddie 登记本次外部 Agent 工作"},
            {"step": 4, "action": "save-document / propose-job / propose-interview", "result": "保存草稿或提交待确认候选"},
            {"step": 5, "action": "complete", "result": "结束运行；候选进入 Caddie 的 Agent 更新区"},
            {"step": 6, "action": "package", "result": "查看用户是否确认、编辑或拒绝"},
            {"step": 7, "action": "changes", "result": "续接前读取用户后续修改，避免覆盖新版"},
        ],
        "write_rules": {
            "draft": "低风险工作稿可直接保存，但不是确认事实。",
            "career_document": "指定岗位目录时必须写到对应目录候选，不能用通用草稿代替。",
            "facts_and_feedback": "事实、数字、贡献边界和反馈约束必须由用户确认。",
            "job_and_calendar": "岗位状态与面试排期只能提交候选，确认后才生效。",
        },
        "examples": [
            "caddie-agent doctor --agent-key external_agent --company 字节跳动",
            "caddie-agent context --track-id 11 --intent 面试准备",
            "caddie-agent start-run --track-id 11 --title '准备三面' --instruction '整理业务问题'",
            "caddie-agent save-document --track-id 11 --destination job.interview --title '三面准备' --body '...'",
            "caddie-agent propose-interview --track-id 11 --operation schedule --interview-date '2026-08-01 10:00' --reason '用户要求登记面试'",
            "caddie-agent package 任务ID",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="caddie-agent", description="Caddie 外部 Agent 本地接口")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("guide", help="查看完整使用流程与审批边界")

    p = sub.add_parser("verify", help="验证 CLI/MCP 连接并刷新接入回执")
    p.add_argument("--agent-key", default="external_agent")

    p = sub.add_parser("doctor", help="一次检查连接、权限和可用对象")
    p.add_argument("--agent-key", default="external_agent"); p.add_argument("--task-hint", default="")
    p.add_argument("--track-id", type=int); p.add_argument("--project-id", type=int); p.add_argument("--company")

    p = sub.add_parser("jobs", help="列出岗位并解析稳定 track_id")
    p.add_argument("--status"); p.add_argument("--company"); p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("job-context", help="读取一个岗位的完整装配上下文")
    p.add_argument("track_id", type=int); p.add_argument("--query")

    p = sub.add_parser("search", help="检索 Caddie 中的项目、资料、知识和生成结果")
    p.add_argument("query"); p.add_argument("--track-id", type=int); p.add_argument("--limit", type=int, default=8)

    p = sub.add_parser("read", help="按稳定 ID 读取文档")
    p.add_argument("document_type", choices=("knowledge_item", "project", "source", "resume_version"))
    p.add_argument("document_id", type=int)

    p = sub.add_parser("bootstrap", help="读取工作区协议和当前变更游标")
    p.add_argument("--agent-key", default="external_agent"); p.add_argument("--task-hint", default="")
    p.add_argument("--track-id", type=int); p.add_argument("--project-id", type=int)

    p = sub.add_parser("changes", help="读取增量变更")
    p.add_argument("--after", type=int, default=0); p.add_argument("--scope-type"); p.add_argument("--scope-id", type=int)
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("context", help="读取带版本指纹的最小上下文包")
    p.add_argument("--track-id", type=int); p.add_argument("--project-id", type=int)
    p.add_argument("--document-type"); p.add_argument("--document-id", type=int)
    p.add_argument("--intent", default="external_agent"); p.add_argument("--query")
    p.add_argument("--detail", choices=("brief", "full"), default="brief")

    p = sub.add_parser("map", help="查看工作区对象、稳定 ID 与可写位置")
    p.add_argument("--track-id", type=int); p.add_argument("--company")
    p.add_argument("--without-documents", action="store_true"); p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("start-run", help="登记一次外部 Agent 运行")
    p.add_argument("--title", required=True); p.add_argument("--instruction", required=True); p.add_argument("--agent-key", default="external_agent")
    p.add_argument("--track-id", type=int); p.add_argument("--object-type"); p.add_argument("--object-id", type=int); p.add_argument("--context")

    p = sub.add_parser("event", help="写入外部运行进度")
    p.add_argument("task_id", type=int); p.add_argument("--label", required=True); p.add_argument("--detail", default="")
    p.add_argument("--status", default="done"); p.add_argument("--payload"); p.add_argument("--agent-key", default="external_agent")

    p = sub.add_parser("complete", help="结束外部运行")
    p.add_argument("task_id", type=int); p.add_argument("--summary", default="")

    p = sub.add_parser("draft", help="保存低风险草稿资产")
    p.add_argument("--title", required=True); p.add_argument("--body", required=True); p.add_argument("--asset-type", default="other")
    p.add_argument("--track-id", type=int); p.add_argument("--project-id", type=int); p.add_argument("--task-id", type=int)
    p.add_argument("--agent-key", default="external_agent"); p.add_argument("--provenance")

    p = sub.add_parser("propose-fact", help="提交待确认事实")
    p.add_argument("task_id", type=int); p.add_argument("--subject-type", required=True); p.add_argument("--predicate", required=True)
    p.add_argument("--value", required=True); p.add_argument("--subject-id", type=int); p.add_argument("--scope-type", default="global")
    p.add_argument("--scope-id", type=int); p.add_argument("--confidence", type=float); p.add_argument("--evidence"); p.add_argument("--reason", default="")
    p.add_argument("--agent-key", default="external_agent")

    p = sub.add_parser("propose-feedback", help="提交待确认反馈约束")
    p.add_argument("task_id", type=int); p.add_argument("--text", required=True); p.add_argument("--scope", default="global"); p.add_argument("--scope-id", type=int)
    p.add_argument("--category"); p.add_argument("--polarity"); p.add_argument("--strength"); p.add_argument("--directive"); p.add_argument("--reason", default="")
    p.add_argument("--agent-key", default="external_agent")

    p = sub.add_parser("propose-knowledge", help="提议创建岗位准备文档（确认后才写入）")
    p.add_argument("task_id", type=int); p.add_argument("--track-id", required=True, type=int)
    p.add_argument("--title", required=True); p.add_argument("--body", required=True)
    p.add_argument("--folder", default="interview", choices=("company", "role", "professional", "interview", "review"))
    p.add_argument("--topic", default=""); p.add_argument("--mastery", default="learning", choices=("new", "learning", "familiar", "mastered"))
    p.add_argument("--reason", default=""); p.add_argument("--evidence"); p.add_argument("--agent-key", default="external_agent")

    p = sub.add_parser("save-document", help="按明确目标保存草稿或提交待确认文档")
    p.add_argument("--title", required=True); p.add_argument("--body", required=True)
    p.add_argument("--destination", required=True, choices=(
        "job.company", "job.role", "job.professional", "job.interview",
        "job.review", "draft", "existing_document",
    ))
    p.add_argument("--track-id", type=int); p.add_argument("--project-id", type=int)
    p.add_argument("--document-type", choices=("knowledge_item", "project")); p.add_argument("--document-id", type=int)
    p.add_argument("--reason", default=""); p.add_argument("--task-id", type=int)
    p.add_argument("--agent-key", default="external_agent"); p.add_argument("--asset-type", default="other")
    p.add_argument("--topic", default=""); p.add_argument("--mastery", default="learning", choices=("new", "learning", "familiar", "mastered"))

    p = sub.add_parser("propose-job", help="提交岗位/投递字段修改候选（确认后生效）")
    p.add_argument("--track-id", required=True, type=int); p.add_argument("--changes", required=True)
    p.add_argument("--reason", required=True); p.add_argument("--task-id", type=int)
    p.add_argument("--agent-key", default="external_agent")

    p = sub.add_parser("propose-interview", help="提交新增、修改或删除面试排期候选（确认后生效）")
    p.add_argument("--track-id", required=True, type=int)
    p.add_argument("--operation", required=True, choices=("schedule", "update", "delete"))
    p.add_argument("--interview-date"); p.add_argument("--round-type"); p.add_argument("--interview-id", type=int)
    p.add_argument("--duration-minutes", type=int, default=60); p.add_argument("--location"); p.add_argument("--meeting-link")
    p.add_argument("--notes"); p.add_argument("--reason", default=""); p.add_argument("--task-id", type=int)
    p.add_argument("--agent-key", default="external_agent")

    p = sub.add_parser("package", help="读取一次运行的更新包")
    p.add_argument("task_id", type=int)
    p = sub.add_parser("task", help="读取任务详情")
    p.add_argument("task_id", type=int)
    return parser


def main() -> None:
    os.environ["CADDIE_EXTERNAL_TRANSPORT"] = "cli"
    args = build_parser().parse_args()
    values = vars(args)
    command = values.pop("command")
    if command == "guide":
        result = _workflow_guide()
        _emit(result)
        return

    db.init_db()
    if command == "verify":
        result = caddie_mcp.verify_connection(values["agent_key"])
    elif command == "doctor":
        receipt = caddie_mcp.verify_connection(values["agent_key"])
        bootstrap = caddie_mcp.workspace_bootstrap(
            values["agent_key"], values["task_hint"], values["track_id"], values["project_id"],
        )
        workspace = caddie_mcp.get_workspace_map(values["track_id"], values["company"], False, 100)
        if values["track_id"]:
            workspace_summary = {
                "scope": workspace.get("scope"), "job": workspace.get("job"),
                "folders": [{key: item.get(key) for key in ("key", "name", "document_count")}
                            for item in workspace.get("folders") or []],
                "write_destinations": workspace.get("write_destinations") or [],
                "counts": {
                    "assets": len(workspace.get("assets") or []),
                    "resume_versions": len(workspace.get("resume_versions") or []),
                    "applications": len(workspace.get("applications") or []),
                },
            }
        else:
            workspace_summary = {
                "scope": workspace.get("scope"), "jobs": workspace.get("jobs") or [],
                "project_count": len(workspace.get("projects") or []),
            }
        result = {
            "ok": True, "connection": receipt,
            "access": {
                "write_policy": bootstrap["agent"]["write_policy"],
                "read": bootstrap["capabilities"]["read"],
                "preferred_write": bootstrap["capabilities"]["preferred_write"],
                "requires_confirmation": bootstrap["capabilities"]["requires_confirmation"],
            },
            "workspace": workspace_summary,
            "next_step": (
                f"Run context --track-id {values['track_id']} --intent '<your task>'"
                if values["track_id"] else "Choose a track_id from workspace.jobs, then run doctor --track-id <id>."
            ),
        }
    elif command == "jobs":
        result = caddie_mcp.list_jobs(values["status"], values["company"], values["limit"])
    elif command == "job-context":
        result = caddie_mcp.read_job_context(values["track_id"], values["query"])
    elif command == "search":
        result = caddie_mcp.search_caddie(values["query"], values["track_id"], values["limit"])
    elif command == "read":
        result = caddie_mcp.read_document(values["document_type"], values["document_id"])
    elif command == "bootstrap":
        result = caddie_mcp.workspace_bootstrap(values["agent_key"], values["task_hint"], values["track_id"], values["project_id"])
    elif command == "changes":
        result = caddie_mcp.get_changes_since(values["after"], values["scope_type"], values["scope_id"], values["limit"])
    elif command == "context":
        result = caddie_mcp.read_context_package(
            values["track_id"], values["project_id"], values["document_type"], values["document_id"],
            values["intent"], values["query"], values["detail"],
        )
    elif command == "map":
        result = caddie_mcp.get_workspace_map(
            values["track_id"], values["company"], not values["without_documents"], values["limit"],
        )
    elif command == "start-run":
        result = caddie_mcp.start_external_run(values["title"], values["instruction"], values["agent_key"], values["track_id"], values["object_type"], values["object_id"], _json(values["context"]))
    elif command == "event":
        result = caddie_mcp.append_external_run_event(values["task_id"], values["label"], values["detail"], values["status"], _json(values["payload"]), values["agent_key"])
    elif command == "complete":
        result = caddie_mcp.complete_external_run(values["task_id"], values["summary"])
    elif command == "draft":
        result = caddie_mcp.create_draft_asset(values["title"], values["body"], values["asset_type"], values["track_id"], values["project_id"], values["task_id"], values["agent_key"], _json(values["provenance"]))
    elif command == "propose-fact":
        result = caddie_mcp.propose_fact(values["task_id"], values["subject_type"], values["predicate"], values["value"], values["subject_id"], values["scope_type"], values["scope_id"], values["confidence"], _json(values["evidence"], []), values["reason"], values["agent_key"])
    elif command == "propose-feedback":
        result = caddie_mcp.propose_feedback(values["task_id"], values["text"], values["scope"], values["scope_id"], values["category"], values["polarity"], values["strength"], values["directive"], values["reason"], values["agent_key"])
    elif command == "propose-knowledge":
        result = caddie_mcp.propose_track_knowledge_document(
            values["task_id"], values["track_id"], values["title"], values["body"], values["folder"],
            values["topic"], values["mastery"], values["reason"], _json(values["evidence"], []), values["agent_key"],
        )
    elif command == "save-document":
        result = caddie_mcp.save_career_document(
            values["title"], values["body"], values["destination"], values["track_id"],
            values["project_id"], values["document_type"], values["document_id"],
            values["reason"], values["task_id"], values["agent_key"], values["asset_type"],
            values["topic"], values["mastery"],
        )
    elif command == "propose-job":
        result = caddie_mcp.propose_job_record_update(
            values["track_id"], _json(values["changes"]), values["reason"],
            values["task_id"], values["agent_key"],
        )
    elif command == "propose-interview":
        result = caddie_mcp.propose_interview_change(
            values["track_id"], values["operation"], values["interview_date"],
            values["round_type"], values["interview_id"], values["duration_minutes"],
            values["location"], values["meeting_link"], values["notes"], values["reason"],
            values["task_id"], values["agent_key"],
        )
    elif command == "package":
        result = caddie_mcp.read_update_package(values["task_id"])
    else:
        result = caddie_mcp.get_agent_task(values["task_id"])
    _emit(result)


if __name__ == "__main__":
    main()
