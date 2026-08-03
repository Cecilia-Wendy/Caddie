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
            paths = agent_launchers.ensure_packaged_launchers(home, executable, platform_name="posix")
            mcp = paths["mcp"].read_text(encoding="utf-8")
            cli = paths["cli"].read_text(encoding="utf-8")
            self.assertEqual(paths["mcp"], home / ".caddie" / "bin" / "caddie-mcp")
            self.assertTrue(os.access(paths["mcp"], os.X_OK))
            self.assertIn("/Applications/Caddie.app/Contents/MacOS/Caddie", mcp)
            self.assertIn("--mcp", mcp)
            self.assertIn("--agent-cli", cli)
            self.assertNotIn("python3", mcp + cli)
            self.assertNotIn(".venv", mcp + cli)

    def test_launcher_refreshes_fallback_after_app_moves(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first = agent_launchers.ensure_packaged_launchers(home, Path("/tmp/Old Caddie.app/Caddie"), platform_name="posix")
            before = first["mcp"].read_text(encoding="utf-8")
            second = agent_launchers.ensure_packaged_launchers(home, Path("/tmp/New Caddie.app/Caddie"), platform_name="posix")
            after = second["mcp"].read_text(encoding="utf-8")
            self.assertIn("Old Caddie.app", before)
            self.assertNotIn("Old Caddie.app", after)
            self.assertIn("New Caddie.app", after)

    def test_windows_launchers_use_cmd_and_forward_cli_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            executable = home / "Caddie Windows" / "Caddie.exe"
            paths = agent_launchers.ensure_packaged_launchers(
                home, executable, platform_name="nt",
            )
            mcp = paths["mcp"].read_text(encoding="utf-8")
            cli = paths["cli"].read_text(encoding="utf-8")
            self.assertEqual(paths["mcp"], home / ".caddie" / "bin" / "caddie-mcp.cmd")
            self.assertIn("%LOCALAPPDATA%\\Programs\\Caddie\\Caddie.exe", mcp)
            self.assertIn('Caddie.exe" --mcp', mcp)
            self.assertIn("--agent-cli %*", cli)
            self.assertNotIn("#!/bin/zsh", mcp + cli)
            self.assertNotIn("python", (mcp + cli).lower())


if __name__ == "__main__":
    unittest.main()
