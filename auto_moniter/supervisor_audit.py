"""Audit evidence and module report handling."""

from __future__ import annotations

from pathlib import Path
import json
from auto_moniter.providers import ConfiguredProvider, ProviderError
from auto_moniter.workspace import acceptance_problems, evidence_paths, workspace_fingerprint
from auto_moniter.verdict import Verdict
from auto_moniter.modules import ModuleLedger
from auto_moniter import candidate_checks, function_tasks
from auto_moniter import documentation_tasks
from auto_moniter.supervisor_models import RoundRecord, RunReport, _accepted, _audit_findings, _failure_reason


class ReviewAudit:
    """Audit evidence and module report handling."""

    def _audit_evidence_guard(self, record, tree):
        if self.config.workflow != 'review-first':
            return lambda evidence: acceptance_problems(
                self.config.cwd, evidence, baseline=self._baseline_files, ignore_dir=self.run_dir,
                git_base=self.git_history.initial if getattr(self.git_history, 'business_commits', False) else '')

        def check_evidence(evidence):
            from auto_moniter import field_handoff

            handoffs = {item for item in evidence if item.startswith('handoff:')}
            paths = [item for item in evidence if not item.startswith(('check:', 'handoff:'))]
            checks = {item.removeprefix('check:') for item in evidence if item.startswith('check:')}
            if handoffs:
                value = field_handoff.read(self.run_dir)
                if (not record or record.watcher_result.get('field_handoff_revision') != value.get('revision')
                        or not field_handoff.matches_checkpoint(value, task=self.function_tasks.current,
                            round_index=record.index, tree=tree, directions=self._dispatch_directions,
                            checks_digest=record.watcher_result.get('candidate_checks', {}).get('digest'))
                        or not handoffs <= field_handoff.citations(value)):
                    return ['现场回执引文必须属于当前派单、本轮候选和已提交的回执。']
            if checks:
                receipt = record.watcher_result.get('candidate_checks') if record and record.tree == tree else None
                context = self._candidate_check_context(record) if receipt else None
                if (not candidate_checks.valid(receipt, self.run_dir, context) or receipt['source_changed']
                        or not checks <= {check['log'] for check in receipt['checks'] if check['status'] == 'passed'}):
                    return ['Cite a passing check from the current candidate as check:<log filename>.']
            if not checks and not handoffs and not evidence_paths(
                    self.config.cwd, paths, ignore_dir=self.run_dir, include_git_ignored=False):
                return ['Cite an existing project file or a passing check from the current candidate.']
            return []

        return check_evidence

    def _record_audit(self, review: Verdict, worker: Verdict, index: int, tree: str, *, record=None) -> dict:
        modules = sorted(set(worker.module_ids) & self.modules.items.keys()) if worker.scope == "module" else []
        corrections = self.audit.reconcile_modules(self.modules.items)
        if corrections:
            self._event('audit_issue_scope_corrected', changes=corrections)
        findings, references = self.audit.local_findings(_audit_findings(review))
        open_before = {item["id"] for item in self.audit.pending()}
        reported, closed = self.audit.update(findings, review.issue_resolutions, index, tree, modules,
                                            self._audit_evidence_guard(record, tree), known_modules=self.modules.items,
                                            check_paths=lambda item, evidence: self._audit_check_paths(record, item, evidence))
        if references:
            for reference in references:
                reference['local_ids'] = [item['id'] for item in self.audit.items if item['summary'] == reference['finding']]
            self._event('audit_external_references_registered', references=references)
        if reported:
            if self.config.function_dispatch:
                self._function_issue_ids = sorted(set(self._function_issue_ids) | set(reported))
            self._event("audit_issues_reported", issues=reported)
        if closed:
            self._event("audit_issues_resolved", issues=closed, tree=tree)
            if open_before.intersection(closed) - {item["id"] for item in self.audit.pending()}:
                self._progress_reset |= any([self.quality.milestone('audit:' + ident) for ident in closed])
        return {'status': 'applied', 'reported': reported, 'closed': closed, 'reasons': [],
                'issues': {item['id']: item['status'] for item in self.audit.items}}

    def _audit_check_paths(self, record, item, evidence):
        from auto_moniter.local_tests import command

        cited_logs = {value.removeprefix('check:') for value in evidence if value.startswith('check:')}
        task = self.function_tasks.current
        if (not cited_logs or not record or not self._function_gate or not task
                or item['module_ids'] != [task['module_id']]
                or not self.modules.impact_files(task['module_id'])):
            return []
        receipt = record.watcher_result['candidate_checks']  # The evidence guard checked the receipt and logs.
        if receipt['context']['task'] != task:
            return []
        try:
            tests = {command(self.config.cwd, name): name for name in task.get('test_files', [])}
        except (OSError, ValueError):
            return []
        sources = {check['log']: tests.get(check['command']) for check in receipt['checks']}
        paths = [sources.get(name) for name in cited_logs]
        if not all(paths):
            return []
        paths = sorted({Path(name).as_posix() for name in [*paths, *function_tasks.writable_files(task)]})
        return paths if all(len(name.encode()) <= 1024 for name in paths) else []

    def _checkpoint(self, record: RoundRecord, raw: str, before: str) -> None:
        data = RunReport('', 0, '', '', [record]).to_dict()['records'][0]
        data['requirement'] = self.config.requirement
        data['acceptance'] = self.config.acceptance
        if record.role == 'reviewer':
            data['audit_issues'] = self.audit.export()
        data['directions'] = [item for item in self.directions.messages()
                              if item['id'] in self._direction_ids[record.role]]
        record.git_checkpoint = self.git_history.record(record.index, record.role, data, before=before, raw=raw)
        self._event('git_checkpoint', role=record.role, round=record.index,
                    commit=record.git_checkpoint['commit'], ref=self.git_history.ref)

    def _merge_modules(self, verdict: Verdict, *, reviewer=False, apply=True) -> ModuleLedger | None:
        import copy
        # Preflight uses the identical rules as acceptance, without changing the
        # live ledger. An invalid scope must not partially register new modules.
        staged = copy.deepcopy(self.modules)
        try:
            proposals = []
            scopes = []
            for item in verdict.modules:
                if reviewer and isinstance(item, dict) and item.get('files'):
                    from auto_moniter.modules import definitions
                    definitions([item])
                    scopes.append((item['id'], item['files']))
                    item = {k:v for k,v in item.items() if k != 'files'}
                if isinstance(item, dict) and item.get('files') and (item.get('id') not in self.modules.items or item['files'] != self.modules.items[item['id']].get('files')):
                    raise ValueError('Module impact files must be supplied by the user, not by an agent')
                if isinstance(item, dict) and item.get("checks") and (item.get("id") not in self.modules.items or item["checks"] != self.modules.items[item["id"]].get("checks")):
                    raise ValueError("Executable module checks must be supplied by the user in --modules, not by an agent")
                proposals.append(item)
            staged.merge(proposals)
            for key, paths in scopes:
                staged.review_scope(key, paths)
        except ValueError as exc:
            verdict.status = "continue"
            verdict.parse_error = "invalid module plan"
            verdict.gaps.append(f"Module plan rejected: {exc}")
        else:
            if apply:
                self.modules = staged
            return staged

    def _module_report_errors(self, verdict):
        errors = [error for error in self.modules.coverage(verdict)
                  if 'requires accepted dependencies:' in error]
        if not self._verification_only():
            errors.extend(acceptance_problems(self.config.cwd, verdict.evidence,
                baseline=self._baseline_files, ignore_dir=self.run_dir,
                git_base=self.git_history.initial if getattr(self.git_history, 'business_commits', False) else ''))
        return errors

    def _repair_module_worker_report(self, record, records, errors):
        """One read-only report correction; acceptance still needs checks and review."""
        import copy
        previous = record.watcher_result.get('module_report')
        retryable = (previous and (previous.get('failure_kind') in ('format', 'transport')
                     or previous.get('status') == 'attempted'
                     or record.failure.startswith('Module report correction failed: invalid ')
                     or record.failure == 'Module report correction failed: timed out'))
        key = (record.index, record.tree)
        if previous and (not self.config.resume or not retryable or key in self._module_report_attempts):
            record.failure = 'Module report correction did not succeed; candidate and report are preserved.'
            return None
        if self._pending_directions() or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != record.tree:
            record.failure = 'Inputs changed before module report correction; fresh assessment required.'
            return None
        provider = copy.copy(self.config.provider)
        delegate = provider
        while isinstance(delegate, ConfiguredProvider):
            delegate.mode = ''
            delegate.delegate = copy.copy(delegate.delegate)
            delegate = delegate.delegate
        delegate.mode = ''
        try:
            provider.validate_review(self.config.extra_args)
        except ProviderError as exc:
            record.failure = 'Module report correction requires a read-only worker: ' + str(exc)
            return None
        self._process_control()
        if self._stop:
            return None
        self._check_budget(new_call=True)
        self._module_report_attempts.add(key)
        saved = json.loads((self.run_dir/'state.json').read_text(encoding='utf-8'))
        report = {'status': 'attempted', 'errors': errors, 'original': record.verdict.to_dict()}
        previous_failure = record.failure
        if previous:
            report['attempt_history'] = [*previous.get('attempt_history', []), {
                'status': previous['status'], 'failure': previous_failure,
                'failure_kind': previous.get('failure_kind', ''),
                'original_output': previous.get('original_output', '')}]
        record.watcher_result['module_report'] = report
        artifact = f'{record.index:03d}-worker-report-original-{self._agent_calls}'
        for suffix in ('.log', '-text.md', '-prompt.md'):
            source = self.run_dir / f'{record.index:03d}-worker{suffix}'
            if source.exists():
                source.replace(self.run_dir/(artifact+suffix))
        record.output_path = str(self.run_dir/(artifact+'.log'))
        report['original_output'] = previous.get('original_output', record.output_path) if previous else record.output_path
        if previous:
            report['attempt_history'][-1]['output'] = record.output_path
        self._write_state(records, 'running', saved.get('stall', 0), saved.get('gap_key', ''), record.tree)
        self._event('module_report_repair', round=record.index, tree=record.tree, reasons=errors)
        prompt = ('Worker report correction only for the SAME saved candidate. Do not implement, edit files, '
                  'commit or rerun builds/tests. Read only the files needed to substantiate the report. '
                  'Correct cited file paths and explicitly list any prerequisite modules you can actually '
                  'revalidate on this candidate. Missing acceptance state is not evidence of a code defect. '
                  'If a prerequisite is incomplete, return continue with the actual gap; do not invent completion. '
                  'Do not expand the original module milestone to unrelated modules or the entire requirement. '
                  'You are the worker: issue_resolutions MUST be [], even for known AUD IDs. '
                  'Only the independent reviewer may propose audit closures; do not create or duplicate audit IDs. '
                  'Independent review and watcher checks awaiting execution are not implementation gaps. '
                  'Return corrected JSON; watcher checks and independent review still follow.\n'
                  + (documentation_tasks.instructions('report_correction') if self.config.task_type == 'documentation' else
                     'Describe known platform limits in summary/evidence, preserving actual unresolved behavior as gaps.\n')
                  + 'Report errors:\n' + json.dumps(errors, ensure_ascii=False)
                  + '\nPrevious report failure (if any):\n' + previous_failure
                  + '\nOriginal report:\n' + json.dumps(report['original'], ensure_ascii=False)
                  + '\nAuthoritative module ledger:\n' + json.dumps(self.modules.export(), ensure_ascii=False))
        signature = self.project.signature if self.project else ''
        turn = self._invoke(provider, prompt, '', role='worker', index=record.index,
            model=self.config.model, extra_args=self.config.extra_args, read_only=True, report_only=True)
        self.worker_session_id = ''
        self._drop_worker_session = True
        verdict = self._verdict_from_turn(turn, role='worker')
        record.elapsed_s += turn.elapsed_s
        self._worker_seconds_since_review += turn.elapsed_s
        if (workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != record.tree
                or self._pending_directions()
                or self.project and (self.project.needs_review or self.project.signature != signature)):
            report['status'] = 'stale'
            record.failure = 'Inputs changed during read-only module report correction; no approval applied.'
            return None
        failure = _failure_reason(turn, verdict) or turn.interruption
        staged = self._merge_modules(verdict, apply=False) if not failure and not verdict.parse_error else None
        problems = self._module_report_errors(verdict) if _accepted(verdict) else []
        if verdict.issue_resolutions:
            problems.append('Worker report correction must leave issue_resolutions=[]; only the reviewer may close audit issues.')
        if _accepted(verdict) and (verdict.scope != 'module'
                or not set(report['original']['module_ids']) <= set(verdict.module_ids)):
            problems.append('Correction must retain the original module milestone and its module IDs.')
        if failure or verdict.parse_error or problems:
            kind = 'transport' if failure else ('format' if verdict.parse_error else 'validation')
            report.update(status='rejected', correction_errors=problems, failure_kind=kind)
            record.failure = 'Module report correction failed: ' + str(failure or verdict.parse_error or '; '.join(problems))
            return None
        self.modules = staged
        report['status'] = 'corrected'
        record.verdict = verdict
        record.output_path = str(turn.output_path)
        record.session_id = turn.session_id
        record.exit_code, record.timed_out = turn.exit_code, turn.timed_out
        record.interruption = turn.interruption
        record.failure = ''
        self._write_state(records, 'running', saved.get('stall', 0), saved.get('gap_key', ''), record.tree)
        return turn
