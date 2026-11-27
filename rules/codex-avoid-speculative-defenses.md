---
description: Avoid speculative defensive programming and blocking on speculation, in both code authoring and code review
agents: [codex]
sources:
  - https://github.com/anomalyco/opencode/blob/dev/AGENTS.md
  - https://github.com/forrestchang/andrej-karpathy-skills/blob/main/CLAUDE.md
  - https://github.com/The-PR-Agent/pr-agent/blob/main/pr_agent/settings/pr_reviewer_prompts.toml
  - https://github.com/garrytan/gstack/blob/main/review/SKILL.md
  - https://google.github.io/eng-practices/review/reviewer/standard.html
---

# Avoid Speculative Defenses

- **Validate once at the owning boundary:** Validate unknown values once at the boundary that owns them. Do not defensively revalidate values already guaranteed by a schema, constructor, or internal type.
- **Minimum code, nothing speculative:** Write and request the minimum code that solves the problem—nothing speculative. No error handling for impossible scenarios. Before adding complexity for a speculative or vanishingly unlikely race or security edge case, weigh the concrete failure mode, likelihood, and complexity cost.
- **Ground failures in realistic triggers:** Only guard against or report concrete failure modes grounded in the visible code and triggered by a realistic input or state—not speculative defects.
- **Reserve speculative warnings for catastrophes:** Do not block on speculation unless it is a suspected release-blocking catastrophe (widespread data loss, total outage, or system-wide compromise)—and if so, label it explicitly speculative.
- **Favor overall code health over perfection:** Favor approving or shipping a change once it definitely improves overall code health, even if it isn't perfect.
