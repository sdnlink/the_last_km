---
name: browser-acceptance
description: Specify and review real browser checks for user-facing behavior in Auto Monitor.
---

# Verify the user-visible operation

- Use the actual entry point and the normal interaction sequence. Specify the starting state, action, and observable assertion. Prefer stable role or label selectors and wait for the expected state.
- For responsive behavior, exercise relevant viewport widths on both sides of the breakpoint. Separate what the user sees during a gesture from what is saved after release or restored after reload.
- Use the target project's configured browser runner through approved checks. Record the operation that ran, the observed result, and the current Watcher receipt. If the required browser is unavailable, report that as a verification blocker.

This workflow was informed by [Anthropic webapp-testing](https://github.com/anthropics/skills/tree/main/skills/webapp-testing) and [OpenAI Playwright](https://github.com/openai/skills/tree/main/skills/.curated/playwright).
