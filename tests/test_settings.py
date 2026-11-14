"""项目级 .claude/settings.json：JSON 合法，压缩与续接时的 SessionStart hook 能跑、输出中文提醒且按角色写。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SettingsTest(unittest.TestCase):
    def test_session_start_hook(self):
        d = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        (entry,) = d["hooks"]["SessionStart"]
        self.assertEqual(set(entry["matcher"].split("|")), {"compact", "resume"})
        (hook,) = entry["hooks"]
        out = subprocess.run(hook["command"], shell=True, capture_output=True, text=True, check=True, cwd=ROOT).stdout
        self.assertIn("中文", out)
        self.assertIn("worktree", out)       # worktree 里另起的会话也收到：角色要分开写，不能只写“只做总控”

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "不在 git 检出里")
    def test_not_ignored(self):
        """settings.json 要进仓库；worktree 目录要忽略。按 git 自己的判断（git check-ignore），不按 .gitignore 的字面。"""
        ign = lambda p: subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT).returncode == 0  # noqa: E731
        self.assertFalse(ign(".claude/settings.json"))
        self.assertTrue(ign(".claude/worktrees/x"))


if __name__ == "__main__":
    unittest.main()
