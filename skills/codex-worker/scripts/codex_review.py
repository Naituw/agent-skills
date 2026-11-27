#!/usr/bin/env python3
"""Run OpenAI Codex as a read-only reviewer and keep the artifacts of the run.

Subcommands:
  run (default)   review code changes (diff / focus / adversarial) or documents (--doc)
  models          list the models Codex currently offers
  config          show or change the saved review default model and aliases

The model is never asked for interactively. Resolution order, per field (model / effort):
  explicit --model / --effort  >  alias or saved default (config.json)  >  Codex's own config.

Environment overrides (mainly for tests):
  CODEX_HOME               Codex home directory (default ~/.codex)
  CODEX_REVIEW_CODEX_BIN   codex executable to use instead of auto-detection
  CODEX_REVIEW_CONFIG      path of the config file (default <skill dir>/config.json)
  CODEX_REVIEW_HOME        directory that receives run artifacts (default <tmp>/codex-review)
  CODEX_REVIEW_HEARTBEAT   seconds between "running" lines (default 30)

Exit codes: 0 ok, 1 codex failed or produced no review, 2 bad usage / unknown model,
3 codex missing or not logged in, 4 nothing to review / not a git repo / bad ref,
124 timeout, 130 interrupted.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - older interpreters only lose the "inherited model" display
    tomllib = None  # type: ignore[assignment]

SKILL_DIR = Path(__file__).resolve().parent.parent
REFERENCES_DIR = SKILL_DIR / "references"

KNOWN_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_SYNONYMS = {"extrahigh": "xhigh", "extra-high": "xhigh", "x-high": "xhigh", "x_high": "xhigh"}
DEFAULT_TIMEOUT_SECS = 1800
FALLBACK_CODEX_BINS = (
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "~/.local/bin/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
)

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_SETUP, EXIT_TARGET = 0, 1, 2, 3, 4
EXIT_TIMEOUT, EXIT_INTERRUPTED = 124, 130


class ReviewError(Exception):
    """Base class: carries the process exit code."""

    exit_code = EXIT_FAILED


class UsageError(ReviewError):
    exit_code = EXIT_USAGE


class SetupError(ReviewError):
    exit_code = EXIT_SETUP


class TargetError(ReviewError):
    exit_code = EXIT_TARGET


# --------------------------------------------------------------------------------------
# Paths and small helpers
# --------------------------------------------------------------------------------------


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def config_path() -> Path:
    return Path(os.environ.get("CODEX_REVIEW_CONFIG") or SKILL_DIR / "config.json")


def artifacts_root() -> Path:
    return Path(os.environ.get("CODEX_REVIEW_HOME") or Path(tempfile.gettempdir()) / "codex-review")


def log(message: str) -> None:
    print(f"[codex-review] {message}", flush=True)


def normalize_effort(value: str) -> str:
    key = value.strip().lower()
    return EFFORT_SYNONYMS.get(key, key)


# --------------------------------------------------------------------------------------
# Config (saved default + aliases)
# --------------------------------------------------------------------------------------


def load_config() -> Dict[str, object]:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except ValueError as exc:
        raise UsageError(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise UsageError(f"{path} must contain a JSON object")
    data.setdefault("default", None)
    data.setdefault("aliases", {})
    if not isinstance(data["aliases"], dict):
        raise UsageError(f"{path}: 'aliases' must be an object")
    return data


def save_config(data: Dict[str, object]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_bundle(spec: str) -> Tuple[str, Optional[str]]:
    """Split "model[:effort]"; slugs never contain ':'."""
    spec = spec.strip()
    if not spec:
        raise UsageError("empty model spec")
    model, sep, effort = spec.rpartition(":")
    if not sep:
        return spec, None
    if not model or not effort:
        raise UsageError(f"bad model spec '{spec}', expected MODEL or MODEL:EFFORT")
    return model, normalize_effort(effort)


def format_bundle(model: str, effort: Optional[str]) -> str:
    return f"{model}:{effort}" if effort else model


# --------------------------------------------------------------------------------------
# Model catalogue and resolution
# --------------------------------------------------------------------------------------


@dataclasses.dataclass
class ModelInfo:
    slug: str
    display_name: str
    visible: bool
    priority: int
    efforts: List[str]
    default_effort: Optional[str]


def load_catalogue(path: Optional[Path] = None) -> List[ModelInfo]:
    """Read Codex's own model cache; empty list when unavailable (resolution then passes names through)."""
    path = path or codex_home() / "models_cache.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models: List[ModelInfo] = []
    for entry in raw.get("models", []) if isinstance(raw, dict) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            continue
        levels = [
            lv["effort"]
            for lv in entry.get("supported_reasoning_levels") or []
            if isinstance(lv, dict) and isinstance(lv.get("effort"), str)
        ]
        priority = entry.get("priority")
        models.append(
            ModelInfo(
                slug=entry["slug"],
                display_name=str(entry.get("display_name") or entry["slug"]),
                visible=entry.get("visibility") == "list",
                priority=priority if isinstance(priority, int) else 10**6,
                efforts=levels,
                default_effort=entry.get("default_reasoning_level"),
            )
        )
    return models


def read_codex_defaults() -> Dict[str, str]:
    """Top-level model settings from the user-level Codex config (display and validation only)."""
    if tomllib is None:
        return {}
    try:
        data = tomllib.loads((codex_home() / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in ("model", "model_reasoning_effort", "review_model") and isinstance(v, str)}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def visible_summary(catalogue: Sequence[ModelInfo]) -> str:
    rows = [f"  {m.slug}  (efforts: {', '.join(m.efforts) or '?'})" for m in sorted(catalogue, key=lambda m: m.priority) if m.visible]
    return "\n".join(rows) if rows else "  (no model catalogue found)"


def match_model(name: str, catalogue: Sequence[ModelInfo]) -> Tuple[ModelInfo, Optional[str]]:
    """Resolve a user-typed model name to a catalogue entry; returns (model, note-about-ambiguity)."""
    query = name.strip()
    for m in catalogue:
        if m.slug.lower() == query.lower():
            return m, None
    nq = _norm(query)
    tokens = [_norm(t) for t in re.split(r"[\s_/\-]+", query) if _norm(t)]

    def hit(m: ModelInfo) -> bool:
        slug, display = _norm(m.slug), _norm(m.display_name)
        return bool(nq) and (nq in slug or nq in display or (len(tokens) > 1 and all(t in slug for t in tokens)))

    candidates = sorted((m for m in catalogue if m.visible and hit(m)), key=lambda m: (m.priority, len(m.slug), m.slug))
    if not candidates:
        raise UsageError(
            f"unknown model '{name}'. Visible models:\n{visible_summary(catalogue)}\n"
            "If the model is newer than Codex's model cache, rerun with --no-validate to hand the name to Codex unchecked."
        )
    note = None
    if len(candidates) > 1:
        others = ", ".join(m.slug for m in candidates[1:])
        note = f"'{name}' matched {candidates[0].slug} (best priority); other candidates: {others}"
    return candidates[0], note


@dataclasses.dataclass
class Resolved:
    model: Optional[str]
    effort: Optional[str]
    model_source: str  # explicit | alias:<name> | config-default | codex-config
    effort_source: str
    notes: List[str] = dataclasses.field(default_factory=list)

    def describe(self, codex_defaults: Dict[str, str]) -> str:
        if self.model:
            model = f"{self.model} ({self.model_source})"
        else:
            inherited = codex_defaults.get("review_model") or codex_defaults.get("model")
            model = f"{inherited} (inherited from Codex config)" if inherited else "Codex default (inherited from Codex config)"
        if self.effort:
            effort = f"{self.effort} ({self.effort_source})"
        else:
            inherited_effort = codex_defaults.get("model_reasoning_effort")
            effort = f"{inherited_effort} (inherited from Codex config)" if inherited_effort else "Codex default (inherited from Codex config)"
        return f"model {model}, effort {effort}"


def resolve_model(
    cfg: Dict[str, object],
    catalogue: Sequence[ModelInfo],
    model_arg: Optional[str],
    effort_arg: Optional[str],
    codex_defaults: Optional[Dict[str, str]] = None,
    validate: bool = True,
) -> Resolved:
    """Pick model and effort without ever asking: explicit > alias/saved default > Codex config."""
    codex_defaults = codex_defaults or {}
    aliases = {str(k).lower(): str(v) for k, v in dict(cfg.get("aliases") or {}).items()}  # type: ignore[arg-type]
    notes: List[str] = []
    bundle: Optional[Tuple[str, Optional[str]]] = None
    bundle_source = ""
    model: Optional[str] = None
    model_source = "codex-config"

    if model_arg and model_arg.strip():
        key = model_arg.strip().lower()
        if key in aliases:
            bundle, bundle_source = parse_bundle(aliases[key]), f"alias:{key}"
        elif catalogue and validate:
            info, note = match_model(model_arg, catalogue)
            model, model_source = info.slug, "explicit"
            if note:
                notes.append(note)
            elif info.slug.lower() != model_arg.strip().lower():
                notes.append(f"'{model_arg}' matched {info.slug}")
        else:
            model, model_source = model_arg.strip(), "explicit"
            if validate:
                notes.append("no model catalogue found; model name passed to Codex unchecked")
    elif cfg.get("default"):
        bundle, bundle_source = parse_bundle(str(cfg["default"])), "config-default"

    effort: Optional[str] = None
    effort_source = "codex-config"
    if bundle:
        model, model_source = bundle[0], bundle_source
        if validate and catalogue and not any(m.slug == model for m in catalogue):
            raise UsageError(
                f"{bundle_source} points at unknown model '{model}'. Visible models:\n{visible_summary(catalogue)}\n"
                "Fix it with `config set-default` / `config set-alias`."
            )
        if bundle[1]:
            effort, effort_source = bundle[1], bundle_source
    if effort_arg and effort_arg.strip():
        effort, effort_source = normalize_effort(effort_arg), "explicit"

    if effort and validate:
        effective_model = model or codex_defaults.get("review_model") or codex_defaults.get("model")
        info = next((m for m in catalogue if m.slug == effective_model), None)
        if info and info.efforts:
            if effort not in info.efforts:
                raise UsageError(f"effort '{effort}' is not supported by {info.slug}; supported: {', '.join(info.efforts)}")
        elif effort not in KNOWN_EFFORTS:
            raise UsageError(f"unknown effort '{effort}'; known efforts: {', '.join(KNOWN_EFFORTS)}")

    return Resolved(model=model, effort=effort, model_source=model_source, effort_source=effort_source, notes=notes)


# --------------------------------------------------------------------------------------
# Git target detection
# --------------------------------------------------------------------------------------


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def repo_root(cwd: Path) -> Optional[Path]:
    try:
        result = git(cwd, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        raise SetupError("git is not installed or not on PATH")
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


@dataclasses.dataclass
class Target:
    kind: str  # uncommitted | base | commit
    ref: Optional[str] = None

    def label(self) -> str:
        if self.kind == "uncommitted":
            return "uncommitted changes"
        if self.kind == "base":
            return f"current branch against {self.ref}"
        return f"commit {(self.ref or '')[:12]}"

    def slug(self) -> str:
        if self.kind == "uncommitted":
            return "uncommitted"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", (self.ref or "")[:40]).strip("-")
        return f"{self.kind}-{safe}"

    def cli_flags(self) -> List[str]:
        if self.kind == "uncommitted":
            return ["--uncommitted"]
        return ["--base" if self.kind == "base" else "--commit", self.ref or ""]

    def scope(self) -> str:
        """Sentence fragment for custom prompts, where CLI target flags cannot be combined with a prompt."""
        if self.kind == "uncommitted":
            return (
                "the uncommitted changes in this repository: staged and unstaged edits plus untracked files "
                "(inspect them with `git status --porcelain` and `git diff HEAD`, and read untracked files directly)"
            )
        if self.kind == "base":
            return (
                f"the changes on the current branch compared with `{self.ref}` "
                f"(inspect them with `git diff {self.ref}...HEAD` and `git log {self.ref}..HEAD`)"
            )
        return f"the changes introduced by commit {self.ref} (inspect them with `git show {self.ref}`)"


def _verify_ref(root: Path, ref: str) -> str:
    result = git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result.returncode != 0:
        raise TargetError(f"'{ref}' is not a commit or branch in this repository")
    return result.stdout.strip()


def _default_branch(root: Path) -> Optional[str]:
    head = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    remote_ref = head.stdout.strip() if head.returncode == 0 else ""
    candidates = []
    if remote_ref:
        candidates.append(remote_ref.split("/", 1)[-1])
    candidates += ["main", "master"]
    for name in candidates:
        if git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
            return name
    return remote_ref or None


def detect_target(root: Path, uncommitted: bool = False, base: Optional[str] = None, commit: Optional[str] = None) -> Target:
    dirty = bool(git(root, "status", "--porcelain").stdout.strip())
    if uncommitted:
        if not dirty:
            raise TargetError("--uncommitted given but the working tree has no changes")
        return Target("uncommitted")
    if base:
        _verify_ref(root, base)
        return Target("base", base)
    if commit:
        return Target("commit", _verify_ref(root, commit))
    if dirty:
        return Target("uncommitted")
    has_head = git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
    if not has_head:
        raise TargetError("nothing to review: the repository has no commits and no changes")
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    default = _default_branch(root)
    if default and branch not in (default, "HEAD"):
        ahead = git(root, "rev-list", "--count", f"{default}..HEAD")
        if ahead.returncode == 0 and ahead.stdout.strip().isdigit() and int(ahead.stdout.strip()) > 0:
            return Target("base", default)
    return Target("commit", _verify_ref(root, "HEAD"))


# --------------------------------------------------------------------------------------
# Prompts and command assembly
# --------------------------------------------------------------------------------------


def _fill(template: str, values: Dict[str, str]) -> str:
    """Single-pass substitution so user text containing "{scope}" is never expanded twice."""
    return re.sub(r"\{(scope|focus|focus_block|docs)\}", lambda m: values[m.group(1)], template)


def _focus_block(focus: Optional[str]) -> str:
    return f"\nFocus especially on: {focus.strip()}\n" if focus and focus.strip() else ""


def build_prompt(mode: str, target: Optional[Target], focus: Optional[str], docs: Sequence[str]) -> Optional[str]:
    if mode == "diff":
        return None
    template = (REFERENCES_DIR / f"{mode}.prompt.md").read_text(encoding="utf-8")  # focus | adversarial | doc
    values = {
        "scope": target.scope() if target else "",
        "focus": (focus or "").strip(),
        "focus_block": _focus_block(focus),
        "docs": "\n".join(f"- {d}" for d in docs),
    }
    return _fill(template, values)


def build_command(
    codex: str,
    mode: str,
    target: Optional[Target],
    resolved: Resolved,
    review_path: Path,
    ephemeral: bool,
    in_git_repo: bool,
) -> List[str]:
    """argv for codex. Prompts are fed through stdin ("-"), so quoting and ARG_MAX never matter."""
    overrides = ["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"']
    if resolved.effort:
        overrides += ["-c", f'model_reasoning_effort="{resolved.effort}"']
    if resolved.model and mode != "doc":
        # `exec review` runs the review on `review_model` (else the session model); exec never sets it,
        # so pin it next to -m. Without a model both stay untouched and Codex's own config decides.
        overrides += ["-c", f'review_model="{resolved.model}"']
    common = ["--json", "-o", str(review_path)]
    if ephemeral:
        common.append("--ephemeral")
    model_args = ["-m", resolved.model] if resolved.model else []
    if mode == "diff":
        assert target is not None
        return [codex, "exec", "review", *target.cli_flags(), *model_args, *overrides, *common]
    if mode in ("focus", "adversarial"):
        return [codex, "exec", "review", *model_args, *overrides, *common, "-"]
    skip_check = [] if in_git_repo else ["--skip-git-repo-check"]
    return [codex, "exec", *model_args, *overrides, *common, *skip_check, "-"]


# --------------------------------------------------------------------------------------
# Codex binary and process handling
# --------------------------------------------------------------------------------------


def find_codex() -> str:
    override = os.environ.get("CODEX_REVIEW_CODEX_BIN")
    if override:
        return override
    found = shutil.which("codex")
    if found:
        return found
    for candidate in FALLBACK_CODEX_BINS:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise SetupError("`codex` was not found. Install it (https://github.com/openai/codex) and run `codex login`.")


def codex_version(codex: str) -> Optional[str]:
    try:
        out = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def ensure_logged_in(codex: str) -> None:
    try:
        out = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return  # cannot tell; let the real run report the problem
    if out.returncode != 0:
        detail = (out.stdout + out.stderr).strip().splitlines()
        raise SetupError("Codex is not logged in; run `codex login`." + (f" ({detail[-1]})" if detail else ""))


@dataclasses.dataclass
class EventSummary:
    thread_id: Optional[str] = None
    last_agent_message: Optional[str] = None
    errors: List[str] = dataclasses.field(default_factory=list)
    usage: Optional[dict] = None
    turn_failed: bool = False


def parse_events(path: Path) -> EventSummary:
    summary = EventSummary()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return summary
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            summary.thread_id = event.get("thread_id") or summary.thread_id
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                summary.last_agent_message = item["text"]
            elif item.get("type") == "error" and item.get("message"):
                summary.errors.append(str(item["message"]))
        elif kind == "turn.completed":
            summary.usage = event.get("usage")
        elif kind == "turn.failed":
            summary.turn_failed = True
            message = (event.get("error") or {}).get("message")
            if message:
                summary.errors.append(str(message))
        elif kind == "error" and event.get("message"):
            summary.errors.append(str(event["message"]))
    return summary


def observed_models(thread_id: str, since: float) -> List[Dict[str, Optional[str]]]:
    """(model, effort) pairs Codex actually used, read from rollout `turn_context` records.

    `exec review` runs the review in a child thread with its own rollout whose `session_meta` carries
    `parent_thread_id`; the thread printed by `thread.started` has no `turn_context` at all. So look at
    the rollouts written since `since` that belong to the thread or to a child of it.
    """
    sessions = codex_home() / "sessions"
    days = sorted({dt.datetime.fromtimestamp(t).strftime("%Y/%m/%d") for t in (since, time.time())})
    seen: List[Dict[str, Optional[str]]] = []
    for day in days:
        for path in sorted((sessions / day).glob("rollout-*.jsonl")):
            try:
                if path.stat().st_mtime < since - 5:
                    continue
                with path.open(encoding="utf-8", errors="replace") as handle:
                    first = json.loads(handle.readline() or "{}")
                    meta = first.get("payload") if isinstance(first, dict) else None
                    if not isinstance(meta, dict) or thread_id not in (meta.get("id"), meta.get("parent_thread_id")):
                        continue
                    for line in handle:
                        if '"turn_context"' not in line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        payload = record.get("payload") if isinstance(record, dict) else None
                        if record.get("type") != "turn_context" or not isinstance(payload, dict):
                            continue
                        pair = {"model": payload.get("model"), "effort": payload.get("effort")}
                        if pair not in seen:
                            seen.append(pair)
            except (OSError, ValueError):
                continue
    return seen


def terminate(proc: subprocess.Popen) -> None:
    """Stop codex and everything it spawned (it runs in its own session)."""
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:  # pragma: no cover
            proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover
            proc.kill()
        proc.wait()
    except ProcessLookupError:
        pass


def _raise_interrupt(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def tail(path: Path, count: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:])
    except OSError:
        return ""


# --------------------------------------------------------------------------------------
# `run`
# --------------------------------------------------------------------------------------


def derive_mode(args: argparse.Namespace) -> str:
    if args.focus is not None and not args.focus.strip():
        raise UsageError("--focus must not be empty")
    if args.doc:
        if args.uncommitted or args.base or args.commit:
            raise UsageError("--doc reviews documents; it cannot be combined with --uncommitted/--base/--commit")
        if args.adversarial:
            raise UsageError("--adversarial challenges code changes; use --focus to steer a --doc review")
        return "doc"
    if args.adversarial:
        return "adversarial"
    if args.focus:
        return "focus"
    return "diff"


def resolve_docs(paths: Sequence[str], cwd: Path) -> List[str]:
    docs = []
    for raw in paths:
        path = Path(raw).expanduser()
        path = path if path.is_absolute() else cwd / path
        if not path.is_file():
            raise UsageError(f"--doc file not found: {raw}")
        resolved = path.resolve()
        try:
            docs.append(str(resolved.relative_to(cwd.resolve())))
        except ValueError:
            docs.append(str(resolved))
    return docs


def unique_dir(parent: Path, name: str) -> Path:
    candidate, counter = parent / name, 2
    while candidate.exists():
        candidate = parent / f"{name}-{counter}"
        counter += 1
    return candidate


def cmd_run(args: argparse.Namespace) -> int:
    cwd = Path(args.cd).expanduser().resolve() if args.cd else Path.cwd()
    if not cwd.is_dir():
        raise UsageError(f"--cd is not a directory: {cwd}")
    mode = derive_mode(args)
    cfg = load_config()
    catalogue = load_catalogue()
    codex_defaults = read_codex_defaults()
    resolved = resolve_model(cfg, catalogue, args.model, args.effort, codex_defaults, validate=not args.no_validate)

    root = repo_root(cwd)
    target: Optional[Target] = None
    docs: List[str] = []
    if mode == "doc":
        docs = resolve_docs(args.doc, cwd)
    else:
        if root is None:
            raise TargetError(f"{cwd} is not inside a git repository; use --doc to review documents instead")
        target = detect_target(root, args.uncommitted, args.base, args.commit)
        cwd = root

    codex = find_codex()
    started_at = dt.datetime.now()
    topic = target.slug() if target else Path(docs[0]).stem
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else unique_dir(
        artifacts_root(), f"{started_at.strftime('%Y%m%d-%H%M%S')}-{mode}-{topic}"
    )
    review_path = out_dir / "review.md"
    argv = build_command(codex, mode, target, resolved, review_path, args.ephemeral, root is not None)
    prompt = build_prompt(mode, target, args.focus, docs)

    scope_label = target.label() if target else "documents: " + ", ".join(docs)
    header = f"mode={mode} · {scope_label} · {resolved.describe(codex_defaults)}"

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "target": dataclasses.asdict(target) if target else None,
                    "docs": docs,
                    "model": resolved.model,
                    "effort": resolved.effort,
                    "model_source": resolved.model_source,
                    "effort_source": resolved.effort_source,
                    "notes": resolved.notes,
                    "summary": header,
                    "cwd": str(cwd),
                    "argv": argv,
                    "stdin": prompt,
                    "artifacts_dir": str(out_dir),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return EXIT_OK

    ensure_logged_in(codex)
    out_dir.mkdir(parents=True, exist_ok=True)
    log(header)
    for note in resolved.notes:
        log(f"note: {note}")
    log(f"artifacts: {out_dir}")

    meta: Dict[str, object] = {
        "state": "running",
        "mode": mode,
        "target": dataclasses.asdict(target) if target else None,
        "docs": docs,
        "requested": {
            "model": resolved.model,
            "effort": resolved.effort,
            "model_source": resolved.model_source,
            "effort_source": resolved.effort_source,
        },
        "codex": {"binary": codex, "version": codex_version(codex)},
        "argv": argv,
        "cwd": str(cwd),
        "ephemeral": bool(args.ephemeral),
        "started_at": started_at.isoformat(timespec="seconds"),
    }
    write_json(out_dir / "meta.json", meta)
    return run_codex(argv, cwd, prompt, out_dir, args.timeout, meta, resolved)


def run_codex(
    argv: List[str],
    cwd: Path,
    stdin_text: Optional[str],
    out_dir: Path,
    timeout: int,
    meta: Dict[str, object],
    resolved: Resolved,
) -> int:
    events_path, stderr_path, review_path = out_dir / "events.jsonl", out_dir / "stderr.log", out_dir / "review.md"
    beat = float(os.environ.get("CODEX_REVIEW_HEARTBEAT") or 30)
    started = time.time()
    stop_beat = threading.Event()
    timed_out = False
    previous_handlers = {sig: signal.signal(sig, _raise_interrupt) for sig in (signal.SIGTERM, signal.SIGHUP)}

    def heartbeat() -> None:
        while not stop_beat.wait(beat):
            log(f"running… {int(time.time() - started)}s")

    proc: Optional[subprocess.Popen] = None
    try:
        with events_path.open("wb") as events, stderr_path.open("wb") as errors:
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                    stdout=events,
                    stderr=errors,
                    start_new_session=True,
                )
            except OSError as exc:
                raise SetupError(f"cannot execute {argv[0]}: {exc}")
            threading.Thread(target=heartbeat, daemon=True).start()
            if stdin_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin_text.encode("utf-8"))
                    proc.stdin.close()
                except BrokenPipeError:
                    pass  # codex died early; its exit code and stderr tell the story
            deadline = started + timeout if timeout else None
            while True:
                try:
                    proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    if deadline and time.time() > deadline:
                        timed_out = True
                        terminate(proc)
                        break
    except KeyboardInterrupt:
        if proc is not None:
            terminate(proc)
        meta.update(state="interrupted", duration_s=round(time.time() - started, 1))
        write_json(out_dir / "meta.json", meta)
        log("interrupted; codex was stopped")
        return EXIT_INTERRUPTED
    finally:
        stop_beat.set()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)

    duration = round(time.time() - started, 1)
    summary = parse_events(events_path)
    review_text = review_path.read_text(encoding="utf-8").strip() if review_path.exists() else ""
    if not review_text and summary.last_agent_message and summary.last_agent_message.strip():
        review_text = summary.last_agent_message.strip()
        review_path.write_text(review_text + "\n", encoding="utf-8")
    observed = observed_models(summary.thread_id, started) if summary.thread_id and not meta.get("ephemeral") else []
    # `exec review` does its work in a child thread, so the parent's usage block is all zeros: don't report it.
    usage = summary.usage if summary.usage and any(isinstance(v, (int, float)) and v for v in summary.usage.values()) else None
    meta.update(thread_id=summary.thread_id, duration_s=duration, exit_code=proc.returncode if proc else None, usage=usage)
    meta["observed"] = observed

    mismatch = None
    if resolved.model and observed and any(p.get("model") != resolved.model for p in observed):
        mismatch = f"requested model {resolved.model} but Codex ran {', '.join(str(p.get('model')) for p in observed)}"
        meta["warning"] = mismatch

    if timed_out:
        meta["state"] = "timeout"
        write_json(out_dir / "meta.json", meta)
        log(f"timed out after {timeout}s; codex was stopped (partial output in {out_dir})")
        return EXIT_TIMEOUT
    if proc is None or proc.returncode != 0 or not review_text:
        meta["state"] = "failed"
        write_json(out_dir / "meta.json", meta)
        if proc is not None and proc.returncode != 0:
            reason = f"codex exited with status {proc.returncode}"
        else:
            reason = "codex finished without producing review text"
        log(f"FAILED: {reason}")
        for message in summary.errors[-3:]:
            log(f"codex error: {message}")
        stderr_tail = tail(stderr_path)
        if stderr_tail:
            log("stderr (last lines):\n" + stderr_tail)
        log(f"details: {out_dir}")
        return EXIT_FAILED

    meta["state"] = "done"
    write_json(out_dir / "meta.json", meta)
    ran = ", ".join(f"{p.get('model')}/{p.get('effort')}" for p in observed) or "not recorded (ephemeral or rollout not found)"
    log(f"done in {int(duration)}s · thread {summary.thread_id or '?'} · Codex ran: {ran}")
    if mismatch:
        log(f"WARNING: {mismatch}")
    log(f"review: {review_path}")
    print("\n" + review_text + "\n", flush=True)
    return EXIT_OK


# --------------------------------------------------------------------------------------
# `models` and `config`
# --------------------------------------------------------------------------------------


def cmd_models(args: argparse.Namespace) -> int:
    if args.refresh:
        try:
            subprocess.run(
                [find_codex(), "debug", "models"], stdin=subprocess.DEVNULL,
                capture_output=True, timeout=15, check=True,
            )
        except (OSError, subprocess.SubprocessError, ReviewError) as exc:
            print(f"Could not refresh the Codex model catalogue ({exc}). Checking the local cache.", file=sys.stderr)
    catalogue = load_catalogue()
    defaults = read_codex_defaults()
    if not catalogue:
        print(f"No model catalogue at {codex_home() / 'models_cache.json'}. Try `models --refresh`.")
        return EXIT_OK
    inherited = defaults.get("review_model") or defaults.get("model")
    for m in sorted(catalogue, key=lambda m: m.priority):
        if not m.visible and not args.all:
            continue
        marks = []
        if m.slug == inherited:
            marks.append("Codex default")
        if not m.visible:
            marks.append("hidden")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"{m.slug}  efforts: {', '.join(m.efforts) or '?'}  (model default: {m.default_effort or '?'}){suffix}")
    if defaults.get("model_reasoning_effort"):
        print(f"Codex default effort: {defaults['model_reasoning_effort']}")
    return EXIT_OK


def _validated_bundle(spec: str) -> str:
    model, effort = parse_bundle(spec)
    catalogue = load_catalogue()
    if catalogue:
        info, _ = match_model(model, catalogue)
        model = info.slug
    resolve_model({"aliases": {}, "default": None}, catalogue, model, effort, validate=bool(catalogue))
    return format_bundle(model, effort)


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    action = args.action
    if action == "show":
        defaults = read_codex_defaults()
        print(json.dumps({k: cfg[k] for k in ("default", "aliases")}, indent=2, ensure_ascii=False))
        inherited = defaults.get("review_model") or defaults.get("model")
        print(f"(config file: {config_path()})")
        print(f"Without a saved default, Codex's own config decides: model {inherited or '?'}, effort {defaults.get('model_reasoning_effort') or '?'}")
        return EXIT_OK
    if action == "set-default":
        if len(args.values) != 1:
            raise UsageError("usage: config set-default MODEL[:EFFORT]")
        cfg["default"] = _validated_bundle(args.values[0])
        save_config(cfg)
        print(f"default review model: {cfg['default']}")
    elif action == "clear-default":
        cfg["default"] = None
        save_config(cfg)
        print("default review model cleared; Codex's own config decides again")
    elif action == "set-alias":
        if len(args.values) != 2:
            raise UsageError("usage: config set-alias NAME MODEL[:EFFORT]")
        name = args.values[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise UsageError("alias names may contain letters, digits, '.', '_' and '-'")
        cfg["aliases"][name.lower()] = _validated_bundle(args.values[1])  # type: ignore[index]
        save_config(cfg)
        print(f"alias {name.lower()} -> {cfg['aliases'][name.lower()]}")  # type: ignore[index]
    elif action == "rm-alias":
        if len(args.values) != 1:
            raise UsageError("usage: config rm-alias NAME")
        if cfg["aliases"].pop(args.values[0].strip().lower(), None) is None:  # type: ignore[union-attr]
            raise UsageError(f"no such alias: {args.values[0]}")
        save_config(cfg)
        print(f"alias {args.values[0].strip().lower()} removed")
    return EXIT_OK


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex_review.py", description="Run Codex as a read-only reviewer.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="review code changes or documents (default command)")
    target = run.add_mutually_exclusive_group()
    target.add_argument("--uncommitted", action="store_true", help="review staged, unstaged and untracked changes")
    target.add_argument("--base", metavar="BRANCH", help="review the current branch against BRANCH")
    target.add_argument("--commit", metavar="SHA", help="review the changes introduced by a commit")
    run.add_argument("--focus", help="steer the review toward a concern (switches to a custom review prompt)")
    run.add_argument("--adversarial", action="store_true", help="challenge the approach and assumptions, not just the code")
    run.add_argument("--doc", action="append", default=[], metavar="PATH", help="review a document (repeatable)")
    run.add_argument("--model", help="model slug, fuzzy name (e.g. 'sol'), or alias")
    run.add_argument("--effort", help="reasoning effort (low, medium, high, xhigh, max, ...)")
    run.add_argument("--ephemeral", action="store_true", help="do not persist the Codex session")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECS, help="seconds before codex is stopped (0 = none)")
    run.add_argument("--out-dir", help="directory for review.md, meta.json and logs")
    run.add_argument("--cd", help="run in this directory instead of the current one")
    run.add_argument("--dry-run", action="store_true", help="print the resolved command and prompt without running codex")
    run.add_argument("--no-validate", action="store_true", help="pass --model/--effort to Codex without checking them")
    run.set_defaults(func=cmd_run)

    models = sub.add_parser("models", help="list the models Codex offers")
    models.add_argument("--all", action="store_true", help="include hidden models")
    models.add_argument("--refresh", action="store_true", help="refresh the catalogue before listing")
    models.set_defaults(func=cmd_models)

    config = sub.add_parser("config", help="show or change the saved default model and aliases")
    config.add_argument("action", choices=["show", "set-default", "clear-default", "set-alias", "rm-alias"])
    config.add_argument("values", nargs="*", metavar="VALUE")
    config.set_defaults(func=cmd_config)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or (args_list[0] not in ("run", "models", "config", "-h", "--help")):
        args_list.insert(0, "run")
    args = build_parser().parse_args(args_list)
    try:
        return args.func(args)
    except ReviewError as exc:
        print(f"[codex-review] error: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
