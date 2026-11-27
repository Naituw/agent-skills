#!/usr/bin/env python3
"""Run one turn in a persistent Codex thread and return its result as JSON."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_review import DEFAULT_TIMEOUT_SECS, ReviewError, codex_home, find_codex, parse_events, tail, terminate


SCHEMA = Path(__file__).resolve().parent.parent / "references" / "turn-schema.json"
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
TURN_INSTRUCTION = (
    "Use needs_input when you need information or a decision to continue, and say what you need. "
    "Otherwise use completed and summarize the result."
)


def settings_path(thread_id):
    key = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return codex_home() / "worker-settings" / f"{key}.json"


def thread_settings(thread_id):
    try:
        return json.loads(settings_path(thread_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_thread_settings(thread_id, model, effort):
    path = settings_path(thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temp = Path(output.name)
        try:
            json.dump({"model": model, "effort": effort}, output)
            output.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def run_turn(args, prompt, result, directory):
    events = directory / "events.jsonl"
    errors = directory / "stderr.log"
    final = directory / "result.json"
    sandbox = args.sandbox or ("workspace-write" if args.write else "read-only")
    argv = [find_codex(), "exec", "-s", sandbox]
    for extra_dir in args.add_dirs:
        argv += ["--add-dir", str(extra_dir.expanduser().resolve())]
    if args.thread_id:
        argv += ["resume", args.thread_id]
    if result["model"]:
        argv += ["-m", result["model"]]
    if result["effort"]:
        argv += ["-c", f'model_reasoning_effort={json.dumps(result["effort"])}']
    argv += ["--json", "--output-schema", str(SCHEMA), "-o", str(final), "--skip-git-repo-check", "-"]

    failure = None
    with events.open("wb") as stdout, errors.open("wb") as stderr:
        proc = subprocess.Popen(
            argv, cwd=result["cwd"], stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
        try:
            proc.communicate(f"{prompt}\n\n{TURN_INSTRUCTION}".encode("utf-8"), timeout=args.timeout or None)
        except subprocess.TimeoutExpired:
            terminate(proc)
            failure = f"Codex timed out after {args.timeout} seconds."
        except KeyboardInterrupt:
            terminate(proc)
            failure = "Codex was interrupted."

    summary = parse_events(events)
    result["thread_id"] = summary.thread_id or args.thread_id
    if failure:
        raise ValueError(failure)
    if proc.returncode or summary.turn_failed:
        detail = "\n".join(summary.errors[-3:]) or tail(errors, 5)
        raise ValueError(detail or "Codex did not complete the turn.")
    if not result["thread_id"]:
        raise ValueError("Codex did not report a thread ID.")

    try:
        reply = json.loads(final.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError("Codex did not return a structured result.") from exc
    if not (
        isinstance(reply, dict)
        and reply.get("status") in ("completed", "needs_input")
        and isinstance(reply.get("message"), str)
        and reply["message"].strip()
    ):
        raise ValueError("Codex returned an invalid result.")
    result.update(status=reply["status"], message=reply["message"])


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "reply"):
        action = commands.add_parser(command)
        action.add_argument("--cd", type=Path, default=Path.cwd(), help="working directory")
        sandbox_group = action.add_mutually_exclusive_group()
        sandbox_group.add_argument("--write", action="store_true", help="allow workspace edits for this turn")
        sandbox_group.add_argument(
            "--sandbox", choices=SANDBOX_MODES, default=None,
            help="Codex sandbox policy for this turn (default: read-only)",
        )
        action.add_argument(
            "--add-dir", dest="add_dirs", action="append", type=Path, default=[],
            help="additional writable directory alongside --write (repeatable)",
        )
        action.add_argument("--model", help="Codex model ID")
        action.add_argument("--effort", help="Codex reasoning effort")
        action.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECS, help="seconds to wait (0 = no limit)")
        if command == "reply":
            action.add_argument("thread_id", help="thread ID returned by start or reply")
        else:
            action.set_defaults(thread_id=None)
        action.add_argument("prompt", help="task or follow-up (use - to read stdin)")
    args = parser.parse_args()
    if args.timeout < 0:
        parser.error("--timeout must be non-negative")
    sandbox = args.sandbox or ("workspace-write" if args.write else "read-only")
    if args.add_dirs and sandbox != "workspace-write":
        parser.error("--add-dir requires workspace-write (--write or --sandbox workspace-write)")
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    result = {
        "status": "failed", "thread_id": args.thread_id,
        "cwd": str(args.cd.expanduser().resolve()), "message": "", "model": args.model, "effort": args.effort,
    }
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        if not prompt.strip():
            raise ValueError("Provide a task or follow-up.")
        if args.thread_id:
            saved = thread_settings(args.thread_id)
            result.update(model=args.model or saved.get("model"), effort=args.effort or saved.get("effort"))
        with tempfile.TemporaryDirectory(prefix="codex-worker-") as directory:
            run_turn(args, prompt, result, Path(directory))
        if args.model or args.effort:
            save_thread_settings(result["thread_id"], result["model"], result["effort"])
    except (OSError, ReviewError, ValueError) as exc:
        result.update(status="failed", message=str(exc))
    except KeyboardInterrupt:
        result.update(status="failed", message="Codex was interrupted.")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(result["status"] == "failed")


if __name__ == "__main__":
    sys.exit(main())
