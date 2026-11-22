---
name: task-decomposition
description: Decompose an approved software requirement into an ordered Auto Monitor function plan with independently testable behaviors and scoped review milestones.
---

# Decompose by observable behavior

1. Trace each acceptance criterion from the public entry point through the relevant implementation and existing tests. Use the smallest independently testable behavior as one assignment. Separate behaviors that can fail, be repaired, and be accepted independently, even when they share a function.
2. Record the entry point in `boundaries`, the input or triggering operation in `inputs`, and the observable result in `outputs`. Put at least one concrete assertion in `verification`. Prefer `Given setup | When operation | Then expected observation`. Attach an approved check or a focused reviewer-authored test that exercises that behavior. Name the precise target and list supporting files together only when the change needs them to deliver this behavior.
3. Compare the remaining assignments for duplicate assertions on the same target. Consolidate equivalent work and keep distinct assertions for distinct behaviors. Give every new test file one owning assignment and reference it as a regression check downstream. Explain atomic changes spanning multiple targets within the approved integration scope.
4. Order implementation of shared code before the module verification that depends on it. When a prerequisite milestone must come earlier, plan another targeted module verification after the change that affects it. Derive impact from actual entry points, dependencies, and tests. Use exact paths when reporting a review scope and include shared dependencies supported by the code.
5. Preserve approved criteria, accepted assignments, and the identity of the active assignment. During plan repair, adjust the affected entries and any references or order that depend on them.

For a responsive canvas, switching a narrow-screen preset to a custom layout and dragging a resize handle belong in separate assignments. Compare card rectangles after switching and reloading in the first test. Measure the width while the pointer is still down in the second. Run the affected layout regression checks after both changes.
