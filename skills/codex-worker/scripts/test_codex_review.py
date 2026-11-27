#!/usr/bin/env python3
"""Offline tests for codex_review.py (no Codex calls, no billing): python3 scripts/test_codex_review.py"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_review as cr  # noqa: E402

CATALOGUE = {
    "models": [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list", "priority": 1,
         "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-6-astra", "display_name": "GPT-6 Astra", "visibility": "list", "priority": 2,
         "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-6-sol", "display_name": "GPT-6 Sol", "visibility": "list", "priority": 4,
         "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-reserve", "display_name": "Reserve", "visibility": "hide", "priority": 4,
         "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")]},
        {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list", "priority": 5,
         "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]},
        {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "visibility": "list", "priority": 6,
         "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6 Luna", "visibility": "list", "priority": 9,
         "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]},
    ]
}

# A stand-in for the codex executable. It mimics the parts the wrapper depends on: JSONL events on stdout,
# the `-o` file, and rollout files shaped like real ones (`exec review` runs in a child thread whose
# session_meta has parent_thread_id; the printed thread has no turn_context).
FAKE_CODEX = textwrap.dedent(
    """\
    #!__PYTHON__
    import datetime, json, os, pathlib, sys, time
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print("codex-cli 9.9.9"); sys.exit(0)
    if argv[:2] == ["login", "status"]:
        if os.environ.get("FAKE_CODEX_LOGGED_OUT"):
            print("Not logged in"); sys.exit(1)
        print("Logged in using ChatGPT"); sys.exit(0)
    stdin_text = sys.stdin.read()
    pathlib.Path(os.environ["FAKE_CODEX_RECORD"]).write_text(
        json.dumps({"argv": argv, "stdin": stdin_text, "cwd": os.getcwd()}))
    mode = os.environ.get("FAKE_CODEX_MODE", "ok")
    if mode == "sleep":
        time.sleep(60)
    out = argv[argv.index("-o") + 1] if "-o" in argv else None
    model = argv[argv.index("-m") + 1] if "-m" in argv else "config-default-model"
    effort = None
    for i, a in enumerate(argv):
        if a == "-c" and argv[i + 1].startswith("model_reasoning_effort="):
            effort = argv[i + 1].split("=", 1)[1].strip('"')
    print(json.dumps({"type": "thread.started", "thread_id": "fake-thread-1"}))
    if "--output-schema" in argv:
        if mode == "turn-failed":
            print(json.dumps({"type": "turn.failed", "error": {"message": "turn failed"}}))
            sys.exit(0)
        if mode == "invalid-output":
            pathlib.Path(out).write_text("unstructured result")
            sys.exit(0)
        status = "completed" if "resume" in argv else "needs_input"
        pathlib.Path(out).write_text(json.dumps({"status": status, "message": stdin_text.splitlines()[0]}))
        print(json.dumps({"type": "turn.completed", "usage": {}}))
        sys.exit(0)
    if mode == "fail":
        print(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}))
        sys.stderr.write("fatal: boom\\n"); sys.exit(1)
    text = "## Findings\\n- fake review of " + (stdin_text.splitlines()[0] if stdin_text else "diff")
    print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": text}}))
    tokens = 0 if os.environ.get("FAKE_CODEX_ZERO_USAGE") else 1
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": tokens}}))
    if out and mode != "no-output-file":
        pathlib.Path(out).write_text(text)
    day = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions" / datetime.datetime.now().strftime("%Y/%m/%d")
    day.mkdir(parents=True, exist_ok=True)
    ran = os.environ.get("FAKE_CODEX_RUNS_MODEL", model)
    context = json.dumps({"type": "turn_context", "payload": {"model": ran, "effort": effort}})
    def session_meta(thread, parent=None):
        return json.dumps({"type": "session_meta", "payload": {"id": thread, "parent_thread_id": parent}})
    if argv[:2] == ["exec", "review"]:
        (day / "rollout-a-fake-thread-1.jsonl").write_text(session_meta("fake-thread-1") + "\\n")
        (day / "rollout-b-fake-child-1.jsonl").write_text(
            session_meta("fake-child-1", "fake-thread-1") + "\\n" + context + "\\n")
    else:
        (day / "rollout-a-fake-thread-1.jsonl").write_text(session_meta("fake-thread-1") + "\\n" + context + "\\n")
    other = json.dumps({"type": "turn_context", "payload": {"model": "someone-elses-model", "effort": "low"}})
    (day / "rollout-c-other.jsonl").write_text(session_meta("other-thread") + "\\n" + other + "\\n")
    """
).replace("__PYTHON__", sys.executable)


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


def make_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.codex_home = self.tmp / "codex-home"
        self.codex_home.mkdir()
        (self.codex_home / "models_cache.json").write_text(json.dumps(CATALOGUE))
        (self.codex_home / "config.toml").write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "max"\n\n[mcp_servers.x]\nmodel = "ignored"\n')
        self.config = self.tmp / "config.json"
        self.record = self.tmp / "record.json"
        self.fake = self.tmp / "fake-codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.codex_home),
                "CODEX_REVIEW_CONFIG": str(self.config),
                "CODEX_REVIEW_CODEX_BIN": str(self.fake),
                "CODEX_REVIEW_HOME": str(self.tmp / "artifacts"),
                "CODEX_REVIEW_HEARTBEAT": "0.2",
                "FAKE_CODEX_RECORD": str(self.record),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def run_main(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cr.main(list(args))
        return code, out.getvalue(), err.getvalue()


class WorkerTests(Base):
    def run_worker(self, *args, mode="ok", prompt=None):
        return subprocess.run(
            [sys.executable, str(Path(__file__).with_name("codex_worker.py")), *args],
            cwd=self.tmp, env=dict(os.environ, FAKE_CODEX_MODE=mode),
            input=prompt, text=True, capture_output=True,
        )

    def test_start_and_reply_keep_the_thread_and_workspace(self):
        workspace = self.tmp / "workspace with spaces"
        workspace.mkdir()
        start = self.run_worker("start", "--cd", str(workspace), "-", prompt="Review this change.\nDetails")
        self.assertEqual(start.returncode, 0, start.stderr)
        started = json.loads(start.stdout)
        self.assertEqual(started["status"], "needs_input")
        self.assertEqual(started["message"], "Review this change.")
        self.assertEqual(started["cwd"], str(workspace.resolve()))
        first = json.loads(self.record.read_text())
        self.assertEqual(first["argv"][:3], ["exec", "-s", "read-only"])
        self.assertIn("Details", first["stdin"])

        reply = self.run_worker("reply", "--cd", started["cwd"], started["thread_id"], "Check the old format.")
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.assertEqual(json.loads(reply.stdout), {
            "status": "completed", "thread_id": started["thread_id"],
            "cwd": started["cwd"], "message": "Check the old format.",
            "model": None, "effort": None,
        })
        second = json.loads(self.record.read_text())
        self.assertEqual(second["argv"][:5], ["exec", "-s", "read-only", "resume", started["thread_id"]])
        self.assertEqual(second["cwd"], first["cwd"])

    def test_workspace_edits_are_explicit(self):
        completed = self.run_worker("start", "--write", "Implement the fix.")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(self.record.read_text())["argv"][:3], ["exec", "-s", "workspace-write"])

    def test_reply_reuses_the_selected_model_and_effort(self):
        start = self.run_worker("start", "--model", "selected-model", "--effort", "xhigh", "Review this change.")
        self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
        started = json.loads(start.stdout)
        self.assertEqual((started["model"], started["effort"]), ("selected-model", "xhigh"))
        first = json.loads(self.record.read_text())["argv"]
        self.assertEqual(first[first.index("-m") + 1], "selected-model")
        self.assertIn('model_reasoning_effort="xhigh"', first)

        reply = self.run_worker("reply", started["thread_id"], "Follow up on this finding.")
        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)
        argv = json.loads(self.record.read_text())["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "selected-model")
        self.assertIn('model_reasoning_effort="xhigh"', argv)

        override = self.run_worker("reply", "--effort", "max", started["thread_id"], "Investigate further.")
        self.assertEqual(override.returncode, 0, override.stdout + override.stderr)
        follow_up = self.run_worker("reply", started["thread_id"], "Check this edge case too.")
        self.assertEqual(follow_up.returncode, 0, follow_up.stdout + follow_up.stderr)
        applied = json.loads(follow_up.stdout)
        self.assertEqual((applied["model"], applied["effort"]), ("selected-model", "max"))

    def test_failed_turn_keeps_its_thread_handle(self):
        for mode, error in (
            ("turn-failed", "turn failed"),
            ("invalid-output", "Codex did not return a structured result."),
        ):
            with self.subTest(mode=mode):
                failed = self.run_worker("start", "Try a task.", mode=mode)
                self.assertEqual(failed.returncode, 1)
                result = json.loads(failed.stdout)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["thread_id"], "fake-thread-1")
                self.assertEqual(result["message"], error)


class ResolveModelTests(Base):
    def resolve(self, model=None, effort=None, cfg=None, **kw):
        cfg = cfg or {"default": None, "aliases": {}}
        return cr.resolve_model(cfg, cr.load_catalogue(), model, effort, cr.read_codex_defaults(), **kw)

    def test_nothing_specified_inherits_codex_config(self):
        r = self.resolve()
        self.assertEqual((r.model, r.effort), (None, None))
        self.assertIn("gpt-6-astra (inherited from Codex config)", r.describe(cr.read_codex_defaults()))
        self.assertIn("max (inherited from Codex config)", r.describe(cr.read_codex_defaults()))

    def test_codex_defaults_ignore_nested_tables(self):
        self.assertEqual(cr.read_codex_defaults(), {"model": "gpt-6-astra", "model_reasoning_effort": "max"})

    def test_exact_slug_wins_even_when_other_names_contain_it(self):
        self.assertEqual(self.resolve("gpt-6-sol").model, "gpt-6-sol")

    def test_fuzzy_name_prefers_best_priority_and_reports_alternatives(self):
        r = self.resolve("sol")
        self.assertEqual(r.model, "gpt-6.1-sol")
        self.assertTrue(any("gpt-6-sol" in n and "gpt-5.6-sol" in n for n in r.notes))

    def test_fuzzy_name_with_version_tokens(self):
        self.assertEqual(self.resolve("5.6 luna").model, "gpt-5.6-luna")
        self.assertEqual(self.resolve("luna").model, "gpt-6-luna")

    def test_hidden_models_need_exact_slug(self):
        self.assertEqual(self.resolve("gpt-reserve").model, "gpt-reserve")
        with self.assertRaises(cr.UsageError):
            self.resolve("reserv")

    def test_unknown_model_lists_visible_models(self):
        with self.assertRaises(cr.UsageError) as ctx:
            self.resolve("nonsense")
        message = str(ctx.exception)
        self.assertIn("gpt-6.1-sol", message)
        self.assertNotIn("gpt-reserve", message)

    def test_no_validate_passes_name_through(self):
        r = self.resolve("brand-new-model", validate=False)
        self.assertEqual(r.model, "brand-new-model")

    def test_alias_bundle_and_explicit_effort_override(self):
        cfg = {"default": None, "aliases": {"deep": "gpt-6.1-sol:max"}}
        r = self.resolve("Deep", cfg=cfg)
        self.assertEqual((r.model, r.effort, r.model_source), ("gpt-6.1-sol", "max", "alias:deep"))
        r = self.resolve("deep", "high", cfg=cfg)
        self.assertEqual((r.model, r.effort, r.effort_source), ("gpt-6.1-sol", "high", "explicit"))

    def test_saved_default_applies_without_explicit_model(self):
        cfg = {"default": "gpt-6-luna:high", "aliases": {}}
        r = self.resolve(cfg=cfg)
        self.assertEqual((r.model, r.effort, r.model_source), ("gpt-6-luna", "high", "config-default"))

    def test_explicit_model_does_not_inherit_default_effort(self):
        cfg = {"default": "gpt-6-luna:high", "aliases": {}}
        r = self.resolve("gpt-6-sol", cfg=cfg)
        self.assertEqual((r.model, r.effort), ("gpt-6-sol", None))

    def test_effort_alone_keeps_model_inherited(self):
        r = self.resolve(effort="low")
        self.assertEqual((r.model, r.effort), (None, "low"))

    def test_effort_synonym_and_validation(self):
        self.assertEqual(self.resolve("gpt-6-luna", "x-high").effort, "xhigh")
        with self.assertRaises(cr.UsageError):
            self.resolve("gpt-6-luna", "ultra")
        with self.assertRaises(cr.UsageError):
            self.resolve(effort="turbo")

    def test_effort_validated_against_inherited_model(self):
        (self.codex_home / "config.toml").write_text('model = "gpt-6-luna"\n')
        with self.assertRaises(cr.UsageError):
            self.resolve(effort="ultra")

    def test_stale_saved_default_is_reported(self):
        with self.assertRaises(cr.UsageError) as ctx:
            self.resolve(cfg={"default": "gpt-1-retired", "aliases": {}})
        self.assertIn("config-default", str(ctx.exception))

    def test_missing_catalogue_passes_through(self):
        (self.codex_home / "models_cache.json").unlink()
        r = self.resolve("whatever", "high")
        self.assertEqual((r.model, r.effort), ("whatever", "high"))
        self.assertTrue(r.notes)


class TargetTests(Base):
    def test_dirty_tree_means_uncommitted(self):
        repo = make_repo(self.tmp)
        (repo / "a.txt").write_text("two\n")
        self.assertEqual(cr.detect_target(repo), cr.Target("uncommitted"))

    def test_untracked_file_counts_as_dirty(self):
        repo = make_repo(self.tmp)
        (repo / "new.txt").write_text("x\n")
        self.assertEqual(cr.detect_target(repo).kind, "uncommitted")

    def test_clean_branch_ahead_of_default_means_base(self):
        repo = make_repo(self.tmp)
        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "b.txt").write_text("b\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "feat")
        self.assertEqual(cr.detect_target(repo), cr.Target("base", "main"))

    def test_clean_default_branch_means_head_commit(self):
        repo = make_repo(self.tmp)
        target = cr.detect_target(repo)
        self.assertEqual(target.kind, "commit")
        self.assertEqual(target.ref, git(repo, "rev-parse", "HEAD").strip())

    def test_explicit_targets_and_errors(self):
        repo = make_repo(self.tmp)
        self.assertEqual(cr.detect_target(repo, base="main"), cr.Target("base", "main"))
        self.assertEqual(cr.detect_target(repo, commit="HEAD").ref, git(repo, "rev-parse", "HEAD").strip())
        with self.assertRaises(cr.TargetError):
            cr.detect_target(repo, uncommitted=True)
        with self.assertRaises(cr.TargetError):
            cr.detect_target(repo, base="no-such-branch")

    def test_empty_repository_has_nothing_to_review(self):
        repo = self.tmp / "empty"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        with self.assertRaises(cr.TargetError):
            cr.detect_target(repo)


class DryRunTests(Base):
    def dry(self, *args: str, cwd: Path):
        code, out, err = self.run_main("--dry-run", "--cd", str(cwd), *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_diff_mode_command(self):
        repo = make_repo(self.tmp)
        (repo / "a.txt").write_text("two\n")
        plan = self.dry("--model", "luna", "--effort", "low", cwd=repo)
        argv = plan["argv"]
        self.assertEqual(argv[1:4], ["exec", "review", "--uncommitted"])
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-luna")
        for expected in ('sandbox_mode="read-only"', 'approval_policy="never"', 'model_reasoning_effort="low"', 'review_model="gpt-6-luna"'):
            self.assertIn(expected, argv)
        self.assertIsNone(plan["stdin"])
        self.assertNotIn("-", argv[-1:])
        self.assertFalse(Path(plan["artifacts_dir"]).exists(), "dry-run must not create artifacts")

    def test_inherited_model_passes_no_model_flags(self):
        repo = make_repo(self.tmp)
        plan = self.dry(cwd=repo)
        self.assertNotIn("-m", plan["argv"])
        self.assertFalse(any(a.startswith("review_model=") or a.startswith("model_reasoning_effort=") for a in plan["argv"]))
        self.assertEqual(plan["target"]["kind"], "commit")

    def test_effort_only_pins_effort_but_not_model(self):
        repo = make_repo(self.tmp)
        plan = self.dry("--effort", "high", cwd=repo)
        self.assertNotIn("-m", plan["argv"])
        self.assertIn('model_reasoning_effort="high"', plan["argv"])

    def test_focus_mode_uses_custom_prompt_via_stdin(self):
        repo = make_repo(self.tmp)
        (repo / "a.txt").write_text("two\n")
        plan = self.dry("--focus", "race conditions {scope}", cwd=repo)
        self.assertEqual(plan["argv"][1:3], ["exec", "review"])
        self.assertEqual(plan["argv"][-1], "-")
        self.assertNotIn("--uncommitted", plan["argv"])
        self.assertIn("uncommitted changes", plan["stdin"])
        self.assertEqual(plan["stdin"].count("race conditions {scope}"), 1, "user text must not be re-expanded")

    def test_adversarial_mode_with_branch_scope(self):
        repo = make_repo(self.tmp)
        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "b.txt").write_text("b\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "feat")
        plan = self.dry("--adversarial", "--focus", "caching", cwd=repo)
        self.assertEqual(plan["mode"], "adversarial")
        self.assertIn("git diff main...HEAD", plan["stdin"])
        self.assertIn("Focus especially on: caching", plan["stdin"])
        self.assertIn("adversarial review", plan["stdin"])

    def test_adversarial_without_focus_has_no_focus_line(self):
        repo = make_repo(self.tmp)
        plan = self.dry("--adversarial", cwd=repo)
        self.assertNotIn("Focus especially on", plan["stdin"])

    def test_doc_mode_uses_plain_exec_and_relative_paths(self):
        repo = make_repo(self.tmp)
        (repo / "docs").mkdir()
        (repo / "docs" / "spec.md").write_text("# spec\n")
        plan = self.dry("--doc", "docs/spec.md", "--focus", "migration", cwd=repo)
        argv = plan["argv"]
        self.assertEqual(argv[1], "exec")
        self.assertNotEqual(argv[2], "review")
        self.assertNotIn("--skip-git-repo-check", argv)
        self.assertFalse(any(a.startswith("review_model=") for a in argv))
        self.assertIn("- docs/spec.md", plan["stdin"])
        self.assertIn("Focus especially on: migration", plan["stdin"])
        self.assertIsNone(plan["target"])

    def test_doc_mode_outside_git_repo_skips_repo_check(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "plan.md").write_text("# plan\n")
        plan = self.dry("--doc", "plan.md", cwd=plain)
        self.assertIn("--skip-git-repo-check", plan["argv"])

    def test_diff_modes_outside_git_repo_fail(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        code, _, err = self.run_main("--dry-run", "--cd", str(plain))
        self.assertEqual(code, cr.EXIT_TARGET)
        self.assertIn("not inside a git repository", err)

    def test_usage_errors(self):
        repo = make_repo(self.tmp)
        (repo / "d.md").write_text("x\n")
        for args in (["--doc", "d.md", "--base", "main"], ["--doc", "d.md", "--adversarial"], ["--doc", "missing.md"], ["--focus", "  "]):
            code, _, _ = self.run_main("--dry-run", "--cd", str(repo), *args)
            self.assertEqual(code, cr.EXIT_USAGE, args)

    def test_explicit_target_flags(self):
        repo = make_repo(self.tmp)
        plan = self.dry("--base", "main", cwd=repo)
        self.assertEqual(plan["argv"][2:5], ["review", "--base", "main"])
        plan = self.dry("--commit", "HEAD", cwd=repo)
        self.assertEqual(plan["argv"][3], "--commit")

    def test_subcommand_is_optional(self):
        repo = make_repo(self.tmp)
        code, out, _ = self.run_main("run", "--dry-run", "--cd", str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["mode"], "diff")


class RunTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.tmp)
        (self.repo / "a.txt").write_text("two\n")

    def recorded(self):
        return json.loads(self.record.read_text())

    def test_successful_review_writes_artifacts_and_reports_observed_model(self):
        code, out, err = self.run_main("--cd", str(self.repo), "--model", "luna", "--effort", "low")
        self.assertEqual(code, 0, out + err)
        self.assertIn("done in", out)
        self.assertIn("Codex ran: gpt-6-luna/low", out)
        self.assertIn("## Findings", out)
        artifacts = next((self.tmp / "artifacts").iterdir())
        self.assertEqual({p.name for p in artifacts.iterdir()}, {"review.md", "meta.json", "events.jsonl", "stderr.log"})
        meta = json.loads((artifacts / "meta.json").read_text())
        self.assertEqual(meta["state"], "done")
        self.assertEqual(meta["thread_id"], "fake-thread-1")
        self.assertEqual(meta["observed"], [{"model": "gpt-6-luna", "effort": "low"}])
        self.assertEqual(meta["codex"]["version"], "codex-cli 9.9.9")
        self.assertEqual(self.recorded()["cwd"], str(self.repo.resolve()))

    def test_all_zero_usage_is_not_reported(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_ZERO_USAGE": "1"}):
            code, _, _ = self.run_main("--cd", str(self.repo))
        self.assertEqual(code, 0)
        meta = json.loads((next((self.tmp / "artifacts").iterdir()) / "meta.json").read_text())
        self.assertIsNone(meta["usage"])

    def test_doc_mode_observes_the_main_thread_and_ignores_other_sessions(self):
        (self.repo / "spec.md").write_text("# spec\n")
        code, out, err = self.run_main("--cd", str(self.repo), "--doc", "spec.md", "--model", "luna", "--effort", "low")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Codex ran: gpt-6-luna/low", out)
        self.assertNotIn("someone-elses-model", out)

    def test_observed_models_skips_stale_and_unrelated_rollouts(self):
        import datetime as dt
        import time

        day = self.codex_home / "sessions" / dt.datetime.now().strftime("%Y/%m/%d")
        day.mkdir(parents=True)

        def rollout(name, thread, parent, model, age=0.0):
            meta = {"type": "session_meta", "payload": {"id": thread, "parent_thread_id": parent}}
            ctx = {"type": "turn_context", "payload": {"model": model, "effort": "low"}}
            path = day / name
            path.write_text(json.dumps(meta) + "\n" + json.dumps(ctx) + "\n")
            if age:
                os.utime(path, (time.time() - age, time.time() - age))

        since = time.time() - 1
        rollout("rollout-1.jsonl", "T", None, "parent-model")
        rollout("rollout-2.jsonl", "child", "T", "child-model")
        rollout("rollout-3.jsonl", "other", None, "unrelated-model")
        rollout("rollout-4.jsonl", "child-old", "T", "stale-model", age=3600)
        models = [p["model"] for p in cr.observed_models("T", since)]
        self.assertEqual(models, ["parent-model", "child-model"])

    def test_prompt_reaches_codex_through_stdin(self):
        code, out, err = self.run_main("--cd", str(self.repo), "--focus", "locking")
        self.assertEqual(code, 0, out + err)
        recorded = self.recorded()
        self.assertEqual(recorded["argv"][-1], "-")
        self.assertIn("locking", recorded["stdin"])

    def test_model_mismatch_is_flagged(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_RUNS_MODEL": "gpt-5.6-luna"}):
            code, out, _ = self.run_main("--cd", str(self.repo), "--model", "gpt-6-luna")
        self.assertEqual(code, 0)
        self.assertIn("WARNING: requested model gpt-6-luna but Codex ran gpt-5.6-luna", out)

    def test_ephemeral_skips_rollout_lookup(self):
        code, out, _ = self.run_main("--cd", str(self.repo), "--ephemeral")
        self.assertEqual(code, 0)
        self.assertIn("not recorded", out)
        self.assertIn("--ephemeral", self.recorded()["argv"])

    def test_review_text_falls_back_to_last_agent_message(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "no-output-file"}):
            code, out, _ = self.run_main("--cd", str(self.repo))
        self.assertEqual(code, 0)
        artifacts = next((self.tmp / "artifacts").iterdir())
        self.assertIn("## Findings", (artifacts / "review.md").read_text())

    def test_codex_failure_reports_errors_and_exit_code(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "fail"}):
            code, out, _ = self.run_main("--cd", str(self.repo))
        self.assertEqual(code, cr.EXIT_FAILED)
        self.assertIn("FAILED: codex exited with status 1", out)
        self.assertIn("codex error: boom", out)
        self.assertIn("fatal: boom", out)
        meta = json.loads((next((self.tmp / "artifacts").iterdir()) / "meta.json").read_text())
        self.assertEqual(meta["state"], "failed")

    def test_timeout_stops_codex(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "sleep"}):
            code, out, _ = self.run_main("--cd", str(self.repo), "--timeout", "1")
        self.assertEqual(code, cr.EXIT_TIMEOUT)
        self.assertIn("timed out after 1s", out)
        meta = json.loads((next((self.tmp / "artifacts").iterdir()) / "meta.json").read_text())
        self.assertEqual(meta["state"], "timeout")

    def test_logged_out_codex_is_a_setup_error(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_LOGGED_OUT": "1"}):
            code, _, err = self.run_main("--cd", str(self.repo))
        self.assertEqual(code, cr.EXIT_SETUP)
        self.assertIn("codex login", err)
        self.assertFalse((self.tmp / "artifacts").exists())

    def test_missing_codex_binary_is_a_setup_error(self):
        with mock.patch.dict(os.environ, {"CODEX_REVIEW_CODEX_BIN": str(self.tmp / "nope")}):
            code, _, err = self.run_main("--cd", str(self.repo))
        self.assertEqual(code, cr.EXIT_SETUP)
        self.assertTrue(err)

    def test_heartbeat_lines_appear_for_slow_runs(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "sleep"}):
            _, out, _ = self.run_main("--cd", str(self.repo), "--timeout", "1")
        self.assertIn("running…", out)


class ConfigTests(Base):
    def test_set_default_stores_exact_slug_and_validates(self):
        code, out, _ = self.run_main("config", "set-default", "luna:high")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.config.read_text())["default"], "gpt-6-luna:high")
        code, _, err = self.run_main("config", "set-default", "luna:ultra")
        self.assertEqual(code, cr.EXIT_USAGE)
        self.assertIn("ultra", err)
        self.assertEqual(json.loads(self.config.read_text())["default"], "gpt-6-luna:high", "failed set must not change the file")

    def test_saved_default_drives_dry_run(self):
        self.run_main("config", "set-default", "gpt-6.1-sol:max")
        repo = make_repo(self.tmp)
        code, out, _ = self.run_main("--dry-run", "--cd", str(repo))
        plan = json.loads(out)
        self.assertEqual((plan["model"], plan["effort"], plan["model_source"]), ("gpt-6.1-sol", "max", "config-default"))

    def test_clear_default(self):
        self.run_main("config", "set-default", "luna")
        self.run_main("config", "clear-default")
        self.assertIsNone(json.loads(self.config.read_text())["default"])

    def test_alias_lifecycle(self):
        self.assertEqual(self.run_main("config", "set-alias", "Deep", "gpt-6.1-sol:max")[0], 0)
        self.assertEqual(json.loads(self.config.read_text())["aliases"], {"deep": "gpt-6.1-sol:max"})
        repo = make_repo(self.tmp)
        _, out, _ = self.run_main("--dry-run", "--cd", str(repo), "--model", "deep")
        self.assertEqual(json.loads(out)["model"], "gpt-6.1-sol")
        self.assertEqual(self.run_main("config", "rm-alias", "deep")[0], 0)
        self.assertEqual(self.run_main("config", "rm-alias", "deep")[0], cr.EXIT_USAGE)

    def test_alias_name_validation(self):
        self.assertEqual(self.run_main("config", "set-alias", "bad name", "luna")[0], cr.EXIT_USAGE)

    def test_invalid_config_file_is_reported(self):
        self.config.write_text("{not json")
        code, _, err = self.run_main("config", "show")
        self.assertEqual(code, cr.EXIT_USAGE)
        self.assertIn("not valid JSON", err)

    def test_models_listing_marks_codex_default(self):
        code, out, _ = self.run_main("models")
        self.assertEqual(code, 0)
        self.assertIn("gpt-6-astra", out)
        self.assertIn("[Codex default]", out)
        self.assertNotIn("gpt-reserve", out)
        self.assertIn("gpt-reserve", self.run_main("models", "--all")[1])


if __name__ == "__main__":
    unittest.main(verbosity=1)
