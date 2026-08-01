import os
import tempfile
import unittest
from pathlib import Path

import agent_launchers


class AgentLauncherTests(unittest.TestCase):
    def test_packaged_launchers_use_app_runtime_without_python(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            executable = home / "Moved Caddie.app" / "Contents" / "MacOS" / "Caddie"
            paths = agent_launchers.ensure_packaged_launchers(home, executable)
            mcp = paths["mcp"].read_text(encoding="utf-8")
            cli = paths["cli"].read_text(encoding="utf-8")
            self.assertEqual(
                paths["mcp"],
                home / ".caddie-hosted-alpha" / "bin" / "caddie-hosted-alpha-mcp",
            )
            self.assertTrue(os.access(paths["mcp"], os.X_OK))
            self.assertIn(
                "/Applications/Caddie Hosted Alpha.app/Contents/MacOS/Caddie Hosted Alpha",
                mcp,
            )
            self.assertIn("--mcp", mcp)
            self.assertIn("--agent-cli", cli)
            self.assertNotIn("python3", mcp + cli)
            self.assertNotIn(".venv", mcp + cli)

    def test_launcher_refreshes_fallback_after_app_moves(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first = agent_launchers.ensure_packaged_launchers(home, Path("/tmp/Old Caddie.app/Caddie"))
            before = first["mcp"].read_text(encoding="utf-8")
            second = agent_launchers.ensure_packaged_launchers(home, Path("/tmp/New Caddie.app/Caddie"))
            after = second["mcp"].read_text(encoding="utf-8")
            self.assertIn("Old Caddie.app", before)
            self.assertNotIn("Old Caddie.app", after)
            self.assertIn("New Caddie.app", after)


if __name__ == "__main__":
    unittest.main()
