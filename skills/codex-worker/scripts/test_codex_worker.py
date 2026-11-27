#!/usr/bin/env python3
"""Tests for codex_worker.py."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_worker as cw  # noqa: E402


FAKE_CODEX = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
record = Path(os.environ["FAKE_CODEX_RECORD"])
record.write_text(json.dumps({"argv": args, "cwd": os.getcwd()}), encoding="utf-8")
out_idx = args.index("-o") + 1
Path(args[out_idx]).write_text(
    json.dumps({"status": "completed", "message": "ok"}),
    encoding="utf-8",
)
print(json.dumps({"type": "thread.started", "thread_id": "thread-123"}))
print(json.dumps({"type": "turn.completed"}))
"""


class CodexWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-worker-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.work = self.tmp / "work"
        self.extra = self.tmp / "extra"
        self.work.mkdir()
        self.extra.mkdir()
        self.record = self.tmp / "recorded.json"
        self.fake = self.tmp / "fake-codex"
        self.fake.write_text(FAKE_CODEX, encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.env = {
            "CODEX_REVIEW_CODEX_BIN": str(self.fake),
            "CODEX_HOME": str(self.tmp / "codex-home"),
            "FAKE_CODEX_RECORD": str(self.record),
        }

    def run_cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "argv", ["codex_worker.py", *argv]):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = cw.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 2
        return code, stdout.getvalue(), stderr.getvalue()

    def recorded_argv(self):
        return json.loads(self.record.read_text(encoding="utf-8"))["argv"]

    def test_start_defaults_to_read_only(self):
        code, out, _ = self.run_cli("start", "--cd", str(self.work), "hello")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "completed")
        argv = self.recorded_argv()
        self.assertEqual(argv[:3], ["exec", "-s", "read-only"])
        self.assertNotIn("--add-dir", argv)

    def test_write_and_add_dir_before_resume(self):
        extra2 = self.tmp / "extra2"
        extra2.mkdir()
        code, _, _ = self.run_cli(
            "reply", "--cd", str(self.work), "--write",
            "--add-dir", str(self.extra), "--add-dir", str(extra2),
            "thread-123", "follow up",
        )
        self.assertEqual(code, 0)
        argv = self.recorded_argv()
        self.assertEqual(
            argv[:9],
            [
                "exec", "-s", "workspace-write",
                "--add-dir", str(self.extra.resolve()),
                "--add-dir", str(extra2.resolve()),
                "resume", "thread-123",
            ],
        )

    def test_sandbox_danger_full_access_not_persisted(self):
        code, _, _ = self.run_cli(
            "start", "--cd", str(self.work), "--sandbox", "danger-full-access",
            "--model", "gpt-6.2-astra", "--effort", "xhigh", "build",
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.recorded_argv()[:3], ["exec", "-s", "danger-full-access"])

        code, _, _ = self.run_cli("reply", "--cd", str(self.work), "thread-123", "next")
        self.assertEqual(code, 0)
        argv = self.recorded_argv()
        self.assertEqual(argv[:3], ["exec", "-s", "read-only"])
        self.assertIn("gpt-6.2-astra", argv)

    def test_mutually_exclusive_write_and_sandbox(self):
        code, _, err = self.run_cli("start", "--write", "--sandbox", "read-only", "hi")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", err)

    def test_add_dir_requires_workspace_write(self):
        code, _, err = self.run_cli("start", "--add-dir", str(self.extra), "hi")
        self.assertEqual(code, 2)
        self.assertIn("--add-dir requires workspace-write", err)

        code, _, err = self.run_cli(
            "start", "--sandbox", "danger-full-access", "--add-dir", str(self.extra), "hi"
        )
        self.assertEqual(code, 2)
        self.assertIn("--add-dir requires workspace-write", err)


if __name__ == "__main__":
    unittest.main(verbosity=1)
