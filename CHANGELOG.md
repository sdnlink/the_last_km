# Unreleased

- Add guided acceptance setup with natural-language completion criteria, read-only project check discovery, explicit selection, environment preflight and a separate verification-planning task mode. Preserve advanced commands and legacy API configuration.
- Persist versioned check snapshots and distinguish missing prerequisites, failed checks, timeouts and interruptions; expose acceptance output logs in task detail.

- Add confirmed task deletion in the list, detail and progress views. Persist per-console removal markers to prevent rediscovery, retain project/run/Git evidence, and refuse active or unrecovered process ownership at deletion time.

- Add hierarchical workspace leases across parent/child directories and separate state roots while keeping linked worktrees independent.
- Expose strict review, protected worker paths, fixed module plans, execution timeouts, verification mode and storage budgets in the console; permit explicit cumulative budget increases on resume.
- Account for shared Git audit objects and known native provider session stores, stopping at configured sampled quotas without deleting evidence.
- Exercise independent review rejection, worker correction and explicit audit closure in the installed offline selftest; pin models in real backend certification.
- Add a read-only NDR documentation gate that distinguishes historical Markdown evidence from current static verification without rewriting historical records.

- Avoid full host process scans when an exited invocation's process group is already absent; retain background-process verification and cleanup for existing or uncertain groups, and reject invalid group IDs.

- Stop scheduling when Paseo agent shutdown fails; keep session IDs and report resumable recovery failure instead of claiming stopped/round-limited completion. Preserve native process recovery records when exit or process-group cleanup is unconfirmed.

- Persist and inherit turn/idle/check timeouts, retry delay and output capture limit on resume; distinguish omitted options from explicit overrides, including explicit default values.

- Make status/watch prefer live checkpoints over stale reports; confirm completion only after matching valid state/report and released monitor/guardian locks. Expose delivery confirmation/report warnings, wait through guardian restart gaps and exit watch when the monitor has died without a complete final record.

- Apply the external workspace lock to guardian startup preflight and interrupted-run cleanup, using the same lock order as normal supervision.

- Refuse file evidence when Git queries fail in a detected repository; recognize both paths of a Git rename.
- Require matching persisted state before the guardian accepts a newly written orphan report after monitor death; reject malformed report fields and successful reports with outstanding process records.

- Accept verified deletion of an initial baseline file as delivery/issue-closure evidence; continue rejecting unknown, outside and replacement paths.
- Prevent failed, interrupted or malformed agent turns from freezing partial module definitions into the immutable ledger.

- Preserve the latest independent review across failed worker turns, history pruning and resume.
- Pass previous audit and monitor check feedback to fresh reviewers for explicit rechecking; document the watcher/worker/reviewer roles and delivery gate.
- Keep the last completed business audit separate from failed/malformed reviewer feedback, across retention and resume.
- Validate worker file evidence independently of reviewer evidence; refuse replacement text acceptance criteria on resume.
- Add periodic in-progress audits (`--review-every`, default 5 worker turns) with persisted scheduling and strict joint completion gates.
- Add a bounded monitor-owned audit issue ledger, explicit reviewer-only closures with file evidence, module/final delivery gates, and revalidation after source changes. Preserve open issues across failed turns, retained history and resume; refuse corrupt checkpoints and stop on capacity exhaustion.
- Use jointly accepted module IDs for module checks and milestones; apply audit closures/new findings within one capacity decision; count verified issue closures as progress across resume; recognize numbered issue followups without changing original scope or creating duplicate issues.

# 0.4.1

- Add per-project `runs` storage inventory and a preview-first `cleanup` command, including explicit legacy/custom run parents.
- Retain recent and recoverable runs; require matching final state/report and unlocked supervisor/guardian before removal. Reject symlinks, mounted trees, unknown metadata and outstanding process records; recheck under locks before deletion.
- Document logical/allocated size estimates and the boundary between monitor retention, backend sessions and system logs.

# 0.4.0

- Add bounded guardian shutdown with SIGKILL escalation.
- Default to private state outside the project; add an external project lock and optional strict native worker mount isolation for state and protected validation paths.
- Add inherited native-process memory/file limits, a periodically checked run storage budget, and systemd cgroup deployment templates.
- Redact common credentials from diagnostic logs, summaries, and reports; retain private executable configuration verbatim.
- Add local alert records, CI verification, an opt-in pinned real-backend smoke, and offline soak tooling.

Migration: default run paths changed. Existing explicit run directories remain supported. Strict worker isolation requires state outside the project and Bubblewrap. Production backend and 24-hour certification remain deployment acceptance tasks; no claim of successful execution is made for them.

# 0.3.0

This release targets Linux with Python 3.11 or newer.

- Keep worker implementation and read-only review separate, without workspace backups.
- Track immutable module requirements, dependencies, evidence, and code fingerprints.
- Run user-specified module and final acceptance commands; reject stale reviews and revalidate modules after changes.
- Recover interrupted local processes and Paseo sessions before starting new work.
- Add optional guardian restart, runtime/call/output budgets, stagnation recovery, and bounded history.
- Add pause/continue/stop, health status, bounded events, and verified Bubblewrap isolation.
- Add offline doctor and an installed-package selftest.

Migration: new conclusions require `scope`, `modules`, and `module_ids`. Existing supported checkpoints remain readable; future state versions are refused. Resume with the original providers, modes, and command templates. Custom shell reviewers and Paseo review backends without a supported read-only mode are refused. Use a native reviewer instead.

Provider CLIs remain external dependencies. Offline tests do not establish compatibility with every vendor CLI version or confirm credentials, API availability, or model quality. See README for isolation, process recovery, and acceptance limitations.

- Add immutable quality policies with requirement-to-module/file coverage, original acceptance test protection, verified progress milestones and resumable no-progress stops.
- Audit partial work at bounded worker time checkpoints or changed-file thresholds; suspend repeated independently confirmed external blockers.
- Retain unaffected module acceptance using user-owned file scopes and dependency propagation, preserving conservative invalidation for unknown inputs and final integration checks.
- Offer strict Claude files-only review with restricted tools, disabled customizations/MCP and reduced deployment credential exposure.
- Persist explicit backend token/cost reports and unknown usage across resume; enforce optional reported-usage budgets without estimating unreported internal requests.

- Default new reviewed CLI/Web tasks to read-only requirement elaboration before implementation and per-turn independent review. Persist the original request and elaboration across resume.
- Create local audit commits only after joint approval and watcher checks in review-first mode; retain rejected iterations in run records and review exact immutable tree snapshots without advancing refs.
- Recognize Grok's actual unsigned-in/login/API-key errors as terminal authentication failures, display actionable Web feedback, and stream public progress while excluding reasoning.
