"""Build worker and reviewer prompts from the current assignment and evidence."""

from __future__ import annotations

from pathlib import Path
import json

from auto_moniter.workspace import workspace_diff, workspace_snapshot
from auto_moniter.verdict import Verdict, verdict_instructions
from auto_moniter.runlog import clipped
from auto_moniter.redaction import safe
from auto_moniter import function_tasks
from auto_moniter.approved_history import ApprovedHistory
from auto_moniter import deployment, documentation_tasks
from auto_moniter.skills import browser_guidance, guidance
from auto_moniter.supervisor_models import _accepted


class AgentPrompts:
    """Build worker and reviewer prompts from the current assignment and evidence."""

    def _worker_prompt(self, index: int, last_worker: Verdict | None, last_review: Verdict | None, *, tree='') -> str:
        if not self.config.function_dispatch and self.config.task_type in ('deployment', 'documentation'):
            return '\n'.join([
                ('You are the deployment execution agent. Carry out the original authorized operational task.'
                 if self.config.task_type == 'deployment' else 'You are the document analysis and editing agent.'),
                (deployment.instructions('worker', self.config, self.run_dir) if self.config.task_type == 'deployment'
                 else documentation_tasks.instructions('worker')),
                'Original request:', self.config.requirement,
                'Human acceptance criteria:', self.config.acceptance,
                'Reviewed requirement:', json.dumps(self.requirements_review, ensure_ascii=False),
                'Module ledger:', json.dumps(self.modules.export(), ensure_ascii=False),
                'Open audit issues:', json.dumps(self.audit.prompt(), ensure_ascii=False),
                'Previous worker conclusion:', json.dumps(last_worker.to_dict() if last_worker else {}, ensure_ascii=False),
                'Independent review:', json.dumps(last_review.to_dict() if last_review else {}, ensure_ascii=False),
                'Fixed supplemental checks:', json.dumps(self.config.checks, ensure_ascii=False),
                verdict_instructions(),
            ])
        if self.config.function_dispatch:
            task = self.function_tasks.current
            repair = self._worker_handoff(task, tree)
            return '\n'.join([
                *([deployment.instructions('worker', self.config, self.run_dir)] if self.config.task_type == 'deployment' else []),
                *([documentation_tasks.instructions('worker')] if self.config.task_type == 'documentation' else []),
                'Verify the current assignment without editing the workspace.' if self._verification_only()
                else 'Implement the current reviewer-approved assignment in the working directory.',
                json.dumps(task, ensure_ascii=False),
                function_tasks.verdict_contract('worker', task, module_ids=self.modules.items),
                *([self._function_test_scope(task)] if self._function_gate else []),
                'Watcher acceptance catalog currently in effect (context, not additional implementation work):',
                json.dumps(safe(self._function_check_catalog(task)), ensure_ascii=False) if self._function_gate else '(legacy checks)',
                'Watcher executes these checks on your submitted candidate before independent review. Do not repeat an old configuration blocker if its '
                'invalid command is absent from this current catalog. Report actual remaining implementation '
                'defects; if code and focused regressions are ready, submit task_status=done for independent review. '
                'Do not edit code solely to resolve a past watcher configuration error.',
                *(['Watcher 已核对返工交接。只处理该派单范围内的步骤，按对应检查和预期结果验证。',
                   json.dumps(safe(repair), ensure_ascii=False)] if repair else []),
                'Latest review of this assignment (findings to verify within its scope):',
                json.dumps({k: getattr(last_review, k) for k in ('summary','gaps','next_actions','blockers')},
                           ensure_ascii=False) if last_review and last_review.task_id == task['id']
                           and (not last_review.repair_steps or repair) else '(none)',
                'Preserve earlier accepted functions and test assertions. Do not execute prose as watcher checks.',
            ])
        snapshot = workspace_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        if (index == 1 or self.requirement_cycles) and last_worker is None and last_review is None:
            parts = [
                "You are the implementation agent in an unattended development loop.",
                "A person stated the requirement below. Implement it in the working directory.",
                "Work through the original requirement module by module. A completed module uses scope=module; "
                "only completion of ALL original modules and their integration uses scope=requirement.",
                "Choose the earliest pending module whose prerequisites can be accepted. Hand off that bounded "
                "module before implementing later ones; do not bundle independent unfinished modules into one repair. "
                "Check the whole entry/caller/failure-path chain of the chosen change before claiming it complete.",
                "Within this turn's declared scope, check missing behavior, tests, edge cases, and incomplete wiring.",
                "When a module is implemented and verified, return status=done, scope=module and its module_ids. "
                "Other pending modules and waiting for independent review are not gaps in that completed module. "
                "The watcher checks the candidate, obtains independent review and commits it before continuing the original requirement.",
                "Keep capability inventory bounded to what the next implementation needs; use real data dependencies, "
                "not a complete audit of the whole repository as a prerequisite for every user-visible change.",
                "",
                "Requirement:",
                self.config.requirement.strip(),
            ]
            if self.requirement_cycles:
                parts.extend([
                    "",
                    "Committed requirement cycles (preserve this accepted work; do not reopen it as a new requirement):",
                    json.dumps(self.requirement_cycles, ensure_ascii=False),
                    "",
                    "The reviewer elaborated the next requirement after the previous local commit. "
                    "Implement that elaboration. A repair of an already accepted defect is not a new requirement cycle.",
                ])
            if self.config.acceptance.strip():
                parts.extend(["", "Acceptance checks:", self.config.acceptance.strip()])
            if self.config.checks:
                parts.extend(["", "Monitor acceptance commands (must pass before final delivery):", json.dumps(self.config.checks, ensure_ascii=False)])
            if self._recovery_note:
                parts.extend(['', 'Recovery guidance:', self._recovery_note])
            parts.extend(["", "Monitor module ledger (pending modules must not be omitted):", json.dumps(self.modules.export(), ensure_ascii=False)])
            parts.extend(["", "Open audit issues (worker fixes; only reviewer closes IDs):", json.dumps(self.audit.prompt(), ensure_ascii=False)])
            parts.extend(["", "Workspace snapshot:", snapshot, "", verdict_instructions()])
            return "\n".join(parts)

        review_block = "No independent review yet."
        if last_review:
            review_block = json.dumps(last_review.to_dict(), ensure_ascii=False, indent=2)
        own_block = json.dumps(last_worker.to_dict(), ensure_ascii=False, indent=2) if last_worker else "{}"
        return "\n".join(
            [
                "Continue the same task from the saved checkpoint and independent review.",
                "If the previous module passed review, preserve it and implement the next unfinished module "
                "from the original requirement. Recheck the whole original requirement for remaining modules; "
                "a module milestone is not completion of the entire requirement.",
                "This is a fresh session if you do not remember earlier edits. Read the workspace before changing it.",
                "Repair one bounded module at a time, following the saved dependency order. "
                "Use the current module's open AUD findings as a complete repair checklist; inspect their shared "
                "entry points, callers, cleanup and failure paths together. Keep unrelated findings in the ledger "
                "for their own modules; do not expand this implementation to close every project-wide gap at once.",
                "Address the named blocking findings as a bounded repair. Preserve verified behavior and unchanged "
                "evidence; do not rewrite it simply because another round started. Run focused checks for the changed "
                "behavior, then one relevant regression suite. Do not run equivalent full discovery commands twice "
                "without a changed input or observed failure. The watcher executes the fixed delivery checks itself. "
                "Keep evidence concise: real command, exit status, source version, remaining limitations. "
                "A reviewer transport or conclusion-format failure requires reviewer repair, not implementation edits.",
                "If this module's repairs and focused checks are complete, submit status=done, scope=module "
                "and the repaired module_ids now. Pending unrelated modules or reviewer approval are not "
                "implementation gaps in this module. Do not expand to other modules while this handoff is ready.",
                "Make concrete progress or verify the deliverable; avoid unnecessary edits to already completed work.",
                "A pending module with historical acceptance needs revalidation, not automatic reimplementation. "
                "Prioritize unfinished implementation over alternating already-verified modules. Do not create new "
                "verification documents solely to resubmit unchanged code: that changes the candidate and can "
                "invalidate other modules. Cite existing current source and logs, confirm necessary dependencies "
                "jointly in module_ids, and keep the final integration review responsible for the whole request.",
                "If the review is wrong, fix the evidence and say why in summary. If it is right, do the work.",
                "",
                "Original requirement:",
                self.config.requirement.strip(),
                "",
                "Accepted module milestones (preserve this work):",
                self._module_progress(),
                "Monitor module ledger (pending modules must not be omitted):",
                json.dumps(self.modules.export(), ensure_ascii=False),
                "",
                "Acceptance checks:",
                self.config.acceptance.strip() or "(none beyond the requirement)",
                "Monitor acceptance commands:",
                json.dumps(self.config.checks, ensure_ascii=False),
                "Recovery guidance:",
                self._recovery_note or "(none)",
                "",
                "Your previous conclusion:",
                own_block,
                "",
                "Last worker response (historical context):",
                self._last_worker_text(last_worker),
                "",
                "Independent review:",
                review_block,
                "",
                "Last completed business audit (retained across reviewer failures):",
                self._audit_context(last_review),
                "",
                "Open audit issues (worker fixes; only reviewer closes IDs):",
                json.dumps(self.audit.prompt(), ensure_ascii=False),
                "",
                "Workspace snapshot:",
                snapshot,
                "",
                verdict_instructions(),
            ]
        )

    def _audit_context(self, latest: Verdict | None) -> str:
        if self._audit_feedback is None:
            return "(no completed business audit)"
        audit = self._audit_feedback.to_dict()
        if latest is not None and audit == latest.to_dict():
            return "(same as the previous independent review above)"
        return json.dumps(audit, ensure_ascii=False, indent=2)

    def _last_worker_text(self, last_worker: Verdict | None) -> str:
        if last_worker and last_worker.raw:
            return last_worker.raw[-8000:]
        path = self.run_dir / f"{self._next_round - 2:03d}-worker-text.md"
        from auto_moniter.run_storage import open_log
        try:
            with open_log(path) as (stream, _, _):
                return stream.read().decode("utf-8", errors="replace")[-8000:]
        except FileNotFoundError:
            return "(no earlier response)"

    def _module_progress(self) -> str:
        role = "worker" if self.config.no_review else "reviewer"
        milestones = [
            {"round": record.index, "summary": record.verdict.summary, "evidence": record.verdict.evidence}
            for record in self._records
            if record.role == role and record.verdict.scope == "module"
            and any(item.startswith("Module accepted: ") for item in record.verdict.done)
        ]
        return clipped(json.dumps(milestones, ensure_ascii=False, indent=2), 12000)

    def _review_prompt(self, index: int, worker: Verdict, output_path: Path, tree: str,
                       last_review: Verdict | None = None) -> str:
        candidate = next((record for record in reversed(self._records)
                          if record.role=='worker' and record.index==index),None)
        receipt = candidate.watcher_result.get('candidate_checks') if candidate else None
        task = self.function_tasks.current
        git_tree_options = self._git_tree_check_options(candidate)
        browser = browser_guidance(json.dumps(task, ensure_ascii=False)) if receipt and task else ''
        focused = bool(task and task.get('review_test') and task['delivery_scope'] == 'function')
        audit_scope = None
        if focused:
            audit_scope = {task['module_id'], *(key.split(':', 1)[0] for key in task['criterion_ids'])}
            if last_review and last_review.task_id != task['id']:
                last_review = None
        return "\n".join(
            [
                *([deployment.instructions(
                    'reviewer', self.config, self.run_dir)] if self.config.task_type == 'deployment' else []),
                *([documentation_tasks.instructions('reviewer')] if self.config.task_type == 'documentation' else []),
                *(['This read-only assignment records independent verification evidence. '
                   'Assess only its stated assertions and exact Watcher receipt. A later assignment handles module acceptance. '
                   'Keep unrelated audits open without treating them as blockers for this evidence-only step. '
                   'A protected test already adopted from the operator’s committed business baseline may be inspected and run. '
                   'Adoption does not authorize any further test edits.']
                  if task and task.get('kind') == 'verification' and task['delivery_scope'] == 'function' else []),
                *(['Fixed checks executed BEFORE this review for the exact submitted candidate:',
                   json.dumps(safe(self._candidate_receipt_summary(receipt)), ensure_ascii=False),
                   'Match check_ids and log for each report. git_trees describes the inputs of recognized Git-bound checks. '
                   'publication_tree identifies the proposed publication separately. Verify the execution root in the log.',
                   'Each log starts with the executed command. Inspect failed or relevant logs as needed, '
                   'and use the worker round in state.json for the complete receipt. '
                   'Assess the requirement, implementation and observed failures in this review. '
                   'Do not request another identical worker submission or another semantic review of unchanged work. '
                   'For a concrete defect within this assignment, return changes_required with the minimal repair boundary. '
                   'For failures outside this assignment, identify their scope explicitly; do not invent permission to edit '
                   'protected tests or other files. Acceptance still requires all mandatory checks to pass. '
                   'You own the delivery decision. Approve only after the required checks pass. '
                   'The watcher publishes the approved candidate without repeating the acceptance assessment.'] if receipt else []),
                *(['实现派单需要返工时，请在 repair_steps 按执行顺序填写 criterion_id、path、observed、expected、'
                   'action、check_id 和 log。path 须在当前可编辑范围，check_id 须属于派单。'
                   '引用 Watcher 日志时填写对应编号和日志文件名，依据源码推断时 log 留空，未指定固定检查时 check_id 留空。'
                   '接受、安排只读补验、现场交接或下发新修复派单时填 repair_steps=[]。'
                   '确需等待外部执行时同时填写 field_handoffs，列出执行方和应返回的证据。']
                   if self._function_gate else []),
                *([guidance('review-evidence')] if receipt and task and self.config.task_type == 'code' else []),
                *(['If an approved existing test needs an independent execution record, request it in supplemental_checks '
                   'with path, criterion_id, audit_id and git_tree. Use the exact recorded publication_tree in git_tree '
                   'when the evidence must come from the publication candidate, or use an empty string for workspace checks. '
                   'Use task_status=changes_required, next_task=null and describe '
                   'the missing evidence in gaps or blockers. Watcher derives the execution command. Do not request '
                   'modified tests, arbitrary commands or a check already present in the current receipt. '
                   'A resolved audit issue in this module may be cited to rerun its approved test on the current candidate. '
                   'After the new receipt arrives, assess its result and continue. Use supplemental_checks=[] otherwise.']
                  if task and self._verification_only() else []),
                *(['需要在指定 Git 候选上补齐正式回执时，使用 git_tree_checks。每项填写 check_id、git_tree 和 base。'
                   '本地测试的 base 填空字符串，差异检查从 bases 选择业务基线。'
                   '使用 task_status=changes_required、next_task=null，并说明补验针对的原验收条件。'
                   'Watcher 将按批准的参数执行，完成后仍由你独立复核原派单。已有同树回执时直接核对对应日志。'
                   '没有补验请求时 git_tree_checks 填空数组。',
                   json.dumps(git_tree_options, ensure_ascii=False)] if git_tree_options else []),
                *([browser] if browser else []),
                *(["Dispatched assignment: " + json.dumps(self.function_tasks.current, ensure_ascii=False),
                   function_tasks.verdict_contract('reviewer', task, module_ids=self.modules.items,
                                                   full_plan=self.config.full_function_plan),
                   "Previously accepted function IDs: " + json.dumps([x['task']['id'] for x in self.function_tasks.accepted])]
                  if self.config.function_dispatch else []),
                *([self._function_planning_contract()] if self._function_gate else []),
                *([self._function_test_scope(self.function_tasks.current)]
                  if self._function_gate and self.function_tasks.current else []),
                *(['In module review reports, list exact source and test paths that the accepted behavior depends on. '
                   'Use directory-wide patterns only when the entire directory is demonstrably part of that behavior. '
                   'For page workflows, compare earlier checked inputs with this candidate. Require current-candidate evidence '
                   'for affected roles, page entries and user operations, including shared filter and table behavior. '
                   'Reuse earlier evidence only with a reason the operation is unaffected. List each missing required receipt '
                   'as pending and do not approve the affected delivery.']
                  if task and task['delivery_scope'] != 'function' else []),
                "You are an independent reviewer in an unattended development loop.",
                "You are read-only. Do not edit files, commit, push, or change configuration.",
                ("This is verification-only work: neither model may edit files. Return a bounded implementation assignment for concrete defects. "
                 if self._verification_only() else "The worker alone implements fixes and runs builds or tests that write files. ")+
                "Inspect code and existing evidence; the watcher runs approved acceptance checks. "
                "Do not run commands that generate files or alter the workspace.",
                "Start from the assigned criterion, observable interface behavior and actual check results. "
                "Inspect implementation only as needed to validate wiring, edge cases or a reproducible failure.",
                ("Recheck audit gaps that the submitted change affects and inspect failed monitor checks. "
                 if focused else "Recheck previously reported audit gaps and failed monitor checks against the current code and evidence. ") +
                "Do not drop an unresolved issue because the worker restarted or claimed it was fixed. "
                "The previous conclusion is context, not proof; judge it independently. "
                "Previously accepted module milestones do not imply completion of pending modules.",
                "Provide conservative files impact scopes in modules, including implementation, regression assets, "
                "shared configuration and dependencies you inspected. A missing scope requires broad revalidation. "
                "needs_review audit issues retain an earlier closure: check affected behavior and reuse unchanged "
                "evidence, do not demand recoding unless a concrete regression is found. Reconfirm their AUD IDs "
                "with current evidence. Distinguish environment-unavailable checks from implementation defects; "
                "put actual unavailable tools/platforms in blockers, and continue independent work.",
                "Accept the work only when the requirement is implemented and you cannot name a concrete missing behavior, broken path, or missing check.",
                "Cite files, commands, and observations that support your conclusion.",
                "Style nits alone are not gaps. A missing requirement, failing check, or unwired feature is a gap.",
                "If you find gaps, status must be continue and next_actions must tell the worker exactly what to change.",
                "For each blocking gap, give its AUD ID (if assigned), requirement/criterion, exact file or symbol, "
                "reproducible trigger, expected versus observed behavior, minimal fix boundary, and a precise verification "
                "command or assertion. Distinguish a reproduced failure from a code inference. "
                "Prefix each new scoped finding with its actual known module ID and /, e.g. M2/summary. "
                "Keep existing AUD IDs. Findings for future modules remain pending under those modules; "
                "do not assign all findings to the current worker module or block it on unrelated unfinished work.",
                ("Continue the recorded audit. A fresh reviewer session or task resume is not a reason to repeat "
                 "a full repository review. Inspect the current assignment, changed inputs, affected dependencies "
                 "and unresolved findings; reuse unchanged verified results. "
                 if self._audit_feedback or self.config.function_dispatch else
                 "In the first audit, inspect the execution entry points and caller/submission path relevant to this requirement. ")+
                "Return all currently known blockers together, not one new condition on each round. "
                "Do not invent acceptance criteria beyond the original request and human amendments.",
                "Reuse unchanged verified findings. Recheck the new delta, affected dependencies, open AUD fixes "
                "and integration. A wording/log-format preference is not a blocking product defect unless it "
                "contradicts acceptance or makes evidence materially false. Do not demand both unittest discover "
                "variants when they collect the same suite. Use the recorded fixed check results in your decision.",
                "Use blocked only when an external secret, credential, or unavailable service stops all progress.",
                "If the worker claims blocked but you can continue without that dependency, status must be continue.",
                "Monitoring continues through questions and blockers. Do not ask the person for permission to continue.",
                ("Use the candidate and the pre-review check results to make the delivery decision."
                 if self.config.workflow == 'review-first' else
                 "Only return done when the worker explicitly reported done and the deliverable passes your independent checks."),
                "A module completion can be accepted with scope=module and status=done. "
                "The supervisor then continues the original requirement. "
                "Use scope=requirement only after independently checking ALL original modules, integration, "
                "and acceptance checks. Do not treat one deliverable module as completion of the entire requirement.",
                "When the saved request asks for another requirement after delivery, accept only the current elaborated requirement. "
                "The supervisor starts the next read-only elaboration after the local commit. "
                "Do not invent that next requirement, and do not treat a repair of the same defect as a new requirement.",
                "",
                f"Review round: {index}",
                ("Checkpoint: assess the assigned targets and the declared delivery scope."
                 if self.config.function_dispatch else "Checkpoint: completion candidate" if _accepted(worker)
                 else "Checkpoint: assess the current work and identify any remaining repairs"),
                f"Workspace fingerprint: {tree}",
                "",
                "Requirement:",
                self.config.requirement.strip(),
                "",
                "Acceptance checks:",
                self.config.acceptance.strip() or "(none beyond the requirement)",
                ("Monitor acceptance commands (inspect coverage and the saved results above):" if receipt else
                 "Monitor acceptance commands (inspect coverage; the monitor executes them for delivery):"),
                json.dumps(self.config.checks, ensure_ascii=False),
                "",
                "Worker conclusion:",
                json.dumps(worker.to_dict(), ensure_ascii=False, indent=2),
                "",
                "Previous independent review and monitor acceptance feedback:",
                json.dumps(last_review.to_dict(), ensure_ascii=False, indent=2) if last_review else "(no previous review)",
                "",
                "Last completed business audit (retained across reviewer failures):",
                '(See the current assignment and its audit issues below.)' if focused else self._audit_context(last_review),
                "",
                "Open audit issues (must not disappear by omission):",
                json.dumps(self.audit.prompt(audit_scope), ensure_ascii=False),
                "AUD IDs belong only to THIS run's ledger above. IDs quoted in predecessor tasks, documents, "
                "requirements or transcripts are historical references, not current issue identifiers. "
                "Report newly found or inherited defects as plain findings prefixed by their current module ID. "
                "Only IDs present in this ledger may be used in issue_resolutions; never invent or transplant closures.",
                "For each fixed or invalid finding, return issue_resolutions with its exact AUD ID, an explanation and current file evidence. "
                "Cite a project file path, optionally with a line number, or a passing check as check:<log filename> "
                "using the saved results above. Free-form observations alone cannot close an AUD. "
                "Do not mark an issue resolved based only on a worker claim. Unresolved issues remain open across turns and resume.",
                "A finding about completion report formatting or waiting for automatic checks does not require source edits. "
                "If current code and evidence satisfy the assignment, close that invalid finding in this same review, "
                "accept the function so Watcher can dispatch the next task. Do not require the worker to send the same candidate again.",
                "When an existing issue remains open, prefix its gaps entry with its exact AUD ID (for example 'AUD-1: assertion still fails'). "
                "This preserves its original description and scope instead of creating a duplicate under new wording.",
                "For a new concrete defect, use MODULE/[CRITERION | PATH#SYMBOL | TRIGGER | OBSERVED] summary. "
                "Keep these fields stable when rephrasing the summary. Use different triggers or observed behavior for separate defects. "
                "Use the existing AUD ID for a known issue and confirm uncertain matches from the current evidence.",
                "",
                "Accepted module milestones:",
                self._module_progress(),
                ("Monitor module ledger for the assigned criteria:" if focused else
                 "Monitor module ledger (check every pending module and its acceptance criteria):"),
                json.dumps([item for item in self.modules.export() if audit_scope is None or item['id'] in audit_scope],
                           ensure_ascii=False),
                "",
                "Workspace snapshot:",
                workspace_snapshot(self.config.cwd, ignore_dir=self.run_dir),
                "",
                "Workspace diff:",
                ('(Use the immutable review delta below; request the cumulative diff only for affected integration.)'
                 if isinstance(self.git_history, ApprovedHistory) else workspace_diff(self.config.cwd, ignore_dir=self.run_dir)),
                self.git_history.review_context() if self.git_history else '',
                "",
                f"Worker transcript: {output_path}",
                "",
                verdict_instructions(reviewer_decides=self.config.workflow == 'review-first'),
            ]
        )
