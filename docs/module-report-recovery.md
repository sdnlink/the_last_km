# Module report recovery and requirement versions

An immutable module-definition error is a report problem. In review-first tasks,
the watcher now validates the proposed module plan inside the existing read-only
reviewer repair loop, before applying either module additions or review scopes.
The failure identifies the module, field and first differing list item, including
bounded excerpts of the saved and proposed values.

The reviewer gets one correction of the same candidate. This call uses the
existing time/call budget and keeps the read-only boundary. The original failed
report is archived, and no new worker implementation is dispatched for that
correction. A second invalid report stops with the candidate preserved. Changed
source or new user instructions prevent repair from accepting stale input.

Existing definitions and approved executable checks remain immutable. A report
can omit existing module definitions or repeat them exactly. New authorized
requirements use additional modules. Preflight does not register proposed new
modules or scopes; they enter the ledger only after successful validation.

For a completed module candidate, missing dependency confirmations or invalid
file citations also get one read-only worker report correction before watcher
checks. The original report and output are retained. The worker may confirm
prerequisites it actually revalidates, or return an incomplete conclusion if
they are not finished. It cannot broaden the milestone into the entire original
requirement, remove the original module IDs, edit source, bypass a failed check
or consume more than the existing budget. The corrected candidate still needs
fresh independent review. A reviewer omitting required dependency confirmations
uses its own bounded report-repair opportunity.

If a worker correction fails because its JSON is invalid or the invocation is
interrupted, an explicit resume can retry that same unchanged candidate once
in the new supervisor process. Previous failed logs and attempt metadata remain
available. A repeated format error stops again; it never starts an automatic
retry loop or resets the original budget. A correction rejected for actual
validation gaps is not retried by a plain resume. Workers must keep
`issue_resolutions=[]`; only independent reviewers can propose audit closures.
Report-only worker calls use a separate JSON schema with zero allowed audit
closures; this leaves the independent reviewer schema unchanged.

## Requirement provenance

The original requirement is version 1. Each chronological user amendment adds a
version with its direction ID, timestamp, text hash, parent hash and resulting
hash. The history is derived from the original requirement and existing
`directions.json`, including legacy messages; reading versions never rewrites
the saved original, amendments, approvals or module definitions.

Task detail exposes `requirement_revision`, and the requirements page shows the
current version and amendment history. Supervisor state stores the current
history. Each `last_invocation` and `last_worker_invocation` also records the
exact input history captured for that call, so a subsequent amendment does not
retroactively change what an earlier agent received.

Prompts identify prior requirement elaborations as reviewed snapshots. A later
explicit user amendment overrides an earlier conflicting product or technology
decision; unrelated requirements and approved checks remain in force. Version
metadata is provenance, not approval and not automatic semantic interpretation.
The reviewer still evaluates implementation against the amended request. In
function dispatch, a worker remains limited to the approved assignment and
direction IDs; pending amendments require reviewer replanning.

## Verification

```sh
.venv/bin/python -m unittest tests.test_module_report_repair tests.test_requirement_revisions tests.test_review_efficiency tests.test_modules tests.test_task_directions tests.test_module_handoff_performance -q
node --check console/static/pages/detail.js
node --check console/static/translations.js
```

The regression cases exercise real supervisor transitions, reviewer-only repair,
atomic module registration, repeat failure, existing call limits, stable legacy
version history, amendments arriving during a call, read-only prerequisite
confirmation, rejected source edits during correction and console persistence.
They use local scripted providers and do not call models.

Existing supervisor processes retain the code loaded when they started. Apply
these changes to a running task by stopping it, confirming its checkpoint, and
resuming with the same task ID and existing budget. Restart the console to load
the new API and page behavior; this alone does not restart task supervisors.
