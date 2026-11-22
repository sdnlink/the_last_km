---
name: review-evidence
description: Evaluate the current Auto Monitor candidate using Watcher check receipts and report precise evidence or remaining gaps.
---

# Review evidence from the current candidate

1. Match the Watcher receipt to the current candidate, round, and assigned behavior. Cite a passing receipt entry as `check:<log filename>`, using the filename from this receipt even when Watcher reused an earlier check. For an AUD closure, cite the passing check that exercises the reported behavior.
2. Map each material claim to what the selected check actually observed. For browser behavior, distinguish source inspection from an executed interaction. An assertion about the final layout after release does not establish what the user saw while dragging. When a check omits the relevant operation, describe the missing observation and its expected result.
3. Return a focused gap with the entry point, input or action, and observed or inferred result. Use the result recorded by Watcher for approval decisions. When only an evidence reference needs correction, retain the substantive conclusion and correct that reference.

This workflow was informed by [Superpowers verification-before-completion](https://github.com/obra/superpowers/tree/main/skills/verification-before-completion) and [Anthropic webapp-testing](https://github.com/anthropics/skills/tree/main/skills/webapp-testing).
