"""Protocol smoke test for the local Caddie MCP stdio server."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


async def main():
    with TemporaryDirectory(prefix="caddie-mcp-smoke-") as temp:
        command = os.environ.get("CADDIE_MCP_TEST_COMMAND") or str(ROOT / ".venv" / "bin" / "python")
        args = json.loads(os.environ.get("CADDIE_MCP_TEST_ARGS") or json.dumps([str(ROOT / "caddie_mcp.py")]))
        params = StdioServerParameters(
            command=command,
            args=args,
            env={**os.environ, "HOME": temp},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                expected = {
                    "get_workspace_map", "list_jobs", "read_job_context", "read_company_context", "search_caddie",
                    "read_document", "read_context_package", "create_agent_task", "append_task_event",
                    "propose_document_change", "get_agent_task",
                    "workspace_bootstrap", "get_changes_since", "start_external_run",
                    "append_external_run_event", "complete_external_run", "create_draft_asset",
                    "propose_fact", "propose_feedback", "propose_track_knowledge_document",
                    "save_career_document", "read_update_package", "verify_connection",
                    "propose_job_record_update", "propose_interview_change",
                }
                assert expected <= names, sorted(names)
                templates = await session.list_resource_templates()
                resource_uris = {str(item.uriTemplate) for item in templates.resourceTemplates}
                assert "caddie://jobs/{track_id}" in resource_uris, sorted(resource_uris)
                assert "caddie://documents/{document_type}/{document_id}" in resource_uris, sorted(resource_uris)
                result = await session.call_tool("list_jobs", {"limit": 2})
                assert not result.isError, result
                bootstrap = await session.call_tool("workspace_bootstrap", {"agent_key": "smoke-test"})
                assert not bootstrap.isError, bootstrap
                workspace_map = await session.call_tool("get_workspace_map", {"limit": 2})
                assert not workspace_map.isError, workspace_map
                print(
                    f"MCP_OK tools={len(names)} resources={len(resource_uris)} "
                    f"list_jobs_content={len(result.content)}"
                )


if __name__ == "__main__":
    asyncio.run(main())
