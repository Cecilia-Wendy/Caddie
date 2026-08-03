"""CLI parity checks for the external Agent protocol."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import caddie_agent_cli


def _run(argv: list[str]) -> dict:
    output = StringIO()
    with patch.object(sys, "argv", ["caddie-agent", *argv]), redirect_stdout(output):
        caddie_agent_cli.main()
    return json.loads(output.getvalue())


def main() -> None:
    # Offline help must not touch the database.
    with patch.object(caddie_agent_cli.db, "init_db") as init_db:
        guide = _run(["guide"])
        assert guide["golden_path"][0]["action"] == "doctor"
        init_db.assert_not_called()

    with (
        patch.object(caddie_agent_cli.db, "init_db"),
        patch.object(caddie_agent_cli.caddie_mcp, "verify_connection", return_value={"ok": True}),
        patch.object(caddie_agent_cli.caddie_mcp, "workspace_bootstrap", return_value={
            "agent": {"write_policy": "draft_or_propose_only"},
            "capabilities": {"read": [], "preferred_write": [], "requires_confirmation": []},
        }),
        patch.object(caddie_agent_cli.caddie_mcp, "get_workspace_map", return_value={"jobs": [{"id": 11}]}),
    ):
        doctor = _run(["doctor", "--agent-key", "codex"])
        assert doctor["ok"] is True
        assert doctor["workspace"]["jobs"][0]["id"] == 11

    with (
        patch.object(caddie_agent_cli.db, "init_db"),
        patch.object(caddie_agent_cli.caddie_mcp, "propose_job_record_update", return_value={"status": "pending"}) as propose,
    ):
        result = _run([
            "propose-job", "--track-id", "11", "--changes", '{"priority":"high"}',
            "--reason", "用户要求提高优先级", "--agent-key", "codex",
        ])
        assert result["status"] == "pending"
        propose.assert_called_once_with(11, {"priority": "high"}, "用户要求提高优先级", None, "codex")

    with (
        patch.object(caddie_agent_cli.db, "init_db"),
        patch.object(caddie_agent_cli.caddie_mcp, "propose_interview_change", return_value={"status": "pending"}) as propose,
    ):
        result = _run([
            "propose-interview", "--track-id", "11", "--operation", "schedule",
            "--interview-date", "2026-08-08 10:00", "--round-type", "业务面",
            "--reason", "用户要求登记", "--agent-key", "codex",
        ])
        assert result["status"] == "pending"
        assert propose.call_args.args[:5] == (11, "schedule", "2026-08-08 10:00", "业务面", None)
        assert propose.call_args.args[-1] == "codex"

    print("CLI_OK guide doctor propose-job propose-interview")


if __name__ == "__main__":
    main()
