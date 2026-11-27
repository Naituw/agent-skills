---
name: codex-worker
description: Delegate coding, investigation, or independent code and design reviews to Codex from another agent. Continue the same Codex thread for follow-up work or discussion.
---

# Codex worker

Run these commands with a signed-in Codex CLI. Resolve script paths from this skill's folder.

- Start a task with `python3 scripts/codex_worker.py start --cd DIR --model MODEL_ID --effort EFFORT "TASK"`. Choose the model and effort using the [model guidance](#model). Include the relevant scope and expected result.
- Continue with `python3 scripts/codex_worker.py reply --cd DIR THREAD_ID "FOLLOW-UP"`. Use the returned directory and thread ID to answer a question or give Codex more work.

Both commands default to read-only. Add `--write` on turns where Codex should edit the workspace, plus repeatable `--add-dir DIR` for extra writable directories outside `--cd`. For turns that require host tools or system services blocked by `workspace-write` (e.g., `xcodebuild` / `CoreSimulator`), you may pass `--sandbox danger-full-access` explicitly on that turn; note that this lowers safety by removing sandbox isolation, so pass it only when strictly necessary and prefer read-only or `--write` (+ `--add-dir`) otherwise. Sandbox flags are per-turn and are not persisted across turns. Each turn returns JSON with `thread_id`, `cwd`, `status` (`completed`, `needs_input`, or `failed`), and `message`.

## Model

Check available models and supported efforts with `python3 scripts/codex_review.py models --refresh`. Follow the user's choice when given. Default to 高档 for review and solution design, and 中档 for executing a detailed plan. Ask the user if the choice is unclear.

Prefer Sol with `xhigh` for 中档 or 中级, Astra with `xhigh` for 高档 or 高级, and Astra with `max` for 最高. Choose a concrete model ID and effort from the list and pass them to `--model` and `--effort` for work or review.

`reply` reuses the model and effort selected for the thread. Pass new options only to change them.

## Defensive code

Codex models like to program defensively. Code written by Codex needs a review afterwards to avoid unnecessary defensive code.

## Review

Start a read-only worker for a review you want to discuss in the same thread. For the dedicated Git diff or document review runner with model options and saved reports, see `python3 scripts/codex_review.py run --help`.

Reviews can take many minutes: when other work can proceed in parallel, run the review in the background and collect the result later instead of waiting on it (unless your environment forbids background tasks).

Check findings against the code or document and use your own judgment. Do not accept a review wholesale: Codex also tends to ask for defensive code in reviews, so identify those findings. Accept well-supported findings and apply fixes within the task's scope.

Discuss with Codex in the same thread when you dispute a finding, when you find defensive-code problems, and to have Codex confirm that your fixes are correct. Explain your reasoning and discuss the evidence. Limit the whole discussion to three rounds; never go beyond three. If you have not reached agreement or confirmation by then, stop, summarize the evidence and both positions, and let the user decide.
