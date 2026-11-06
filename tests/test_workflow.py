import unittest

from console.workflow import work_details


class WorkDetailsTests(unittest.TestCase):
    def test_rejected_closure_is_presented_as_rejected_without_rewriting_original(self):
        record = {'role': 'reviewer', 'index': 6, 'summary': 'AUD-1、AUD-2已独立复核关闭',
                  'status': 'continue', 'issue_resolutions': [{'id': 'AUD-1'}, {'id': 'AUD-2'}],
                  'gaps': ['Audit issue update rejected: invalid file evidence'],
                  'git_checkpoint': {'commit': 'a' * 40}}
        for state in ({'rounds': [record]}, {'rounds': [], 'last_review': record}):
            with self.subTest(pruned=not state['rounds']):
                work = work_details(state, {'phase': 'finished'}, {}, 'stopped', {}, {}, 'codex', 'codex')
                result = work['roles']['reviewer']['last_conclusion']
                self.assertIn('未获 watcher 接受', result['summary'])
                self.assertEqual(result['reported_summary'], record['summary'])
                self.assertEqual(result['watcher_result']['status'], 'rejected')
                self.assertEqual(result['git_checkpoint']['commit'], 'a' * 40)
                self.assertEqual(work['conclusions'][0], result)
        self.assertEqual(record['summary'], 'AUD-1、AUD-2已独立复核关闭')
        self.assertNotIn('watcher_result', record)

    def test_proposals_alone_never_imply_watcher_accepted_closure(self):
        from console.conclusions import present_conclusion

        record = {'role': 'reviewer', 'summary': 'AUD-1已复核', 'issue_resolutions': [{'id': 'AUD-1'}]}
        self.assertEqual(present_conclusion(record)['watcher_result'], {})
        outcome = {'status': 'applied', 'closed': ['AUD-1'], 'issues': {'AUD-1': 'resolved'}}
        record['watcher_result'] = outcome
        state = {'rounds': [record], 'audit_issues': {'items': [{'id': 'AUD-1', 'status': 'open'}]}}
        work = work_details(state, {'phase': 'finished'}, {}, 'stopped', {}, {}, 'codex', 'codex')
        # A later reopening must not rewrite a historical accepted decision.
        self.assertEqual(work['conclusions'][0]['watcher_result'], outcome)
        self.assertNotIn('reported_summary', work['conclusions'][0])

    def fixture(self, status='running', phase='working'):
        state = {'rounds': [
            {'role': 'worker', 'index': 1, 'status': 'continue', 'summary': '已实现接口', 'gaps': ['补测试'],
             'evidence': ['app.py'], 'git_checkpoint': {'commit': 'a' * 40}},
            {'role': 'reviewer', 'index': 1, 'status': 'continue', 'summary': '边界条件待修复', 'gaps': ['补异常处理']},
        ], 'session_id': 'worker-session', 'provider_config': {}}
        activity = {'role': 'worker', 'round': 2, 'focus': '正在补充失败分支测试', 'events': [
            {'role': 'worker', 'round': 2, 'kind': 'command', 'text': 'python3 -m unittest', 'output': 'Ran 2 tests'},
            {'role': 'worker', 'round': 1, 'kind': 'message', 'text': '{"status":"continue","summary":"已实现接口"}'},
            {'role': 'reviewer', 'round': 1, 'kind': 'command', 'text': 'git diff base worker'},
            {'role': 'worker', 'round': 2, 'kind': 'phase', 'text': '开始执行'},
        ]}
        health = {'phase': phase, 'activity': 'round 2: worker running', 'health': 'active',
                  'rounds': 1, 'heartbeat_age_s': 2, 'usage': {'agent_calls': 3}}
        return work_details(state, health, activity, status, {'pid': 10, 'child_pid': 11},
                            {'guardian_pid': 9}, 'codex', 'claude')

    def test_only_current_role_is_executing_and_watcher_uses_real_process_details(self):
        work = self.fixture()
        self.assertEqual(work['roles']['worker']['status'], 'running')
        self.assertEqual(work['roles']['worker']['current_work'], '正在补充失败分支测试')
        self.assertEqual(work['roles']['reviewer']['status'], 'waiting')
        self.assertEqual(work['roles']['reviewer']['last_conclusion']['summary'], '边界条件待修复')
        self.assertEqual(work['roles']['watcher']['pid'], 10)
        self.assertEqual(work['roles']['watcher']['guardian_pid'], 9)
        self.assertEqual(work['roles']['watcher']['usage']['agent_calls'], 3)
        review = self.fixture(phase='reviewing')
        self.assertEqual(review['roles']['worker']['status'], 'waiting')
        self.assertEqual(review['roles']['reviewer']['status'], 'running')

    def test_conclusions_contain_only_worker_and_reviewer_results(self):
        work = self.fixture()
        self.assertEqual([c['summary'] for c in work['conclusions']], ['已实现接口', '边界条件待修复'])
        self.assertFalse(any('output' in c or 'text' in c for c in work['conclusions']))
        self.assertEqual(work['conclusions'][0]['git_checkpoint']['commit'], 'a' * 40)
        self.assertFalse(any(e['text'].startswith('{"status"') for e in work['workflow']))
        self.assertTrue(any(e.get('output') == 'Ran 2 tests' for e in work['workflow']))

    def test_paused_stopped_and_finished_roles_never_show_running(self):
        for status in ('paused', 'stopped', 'done', 'queued', 'interrupted'):
            with self.subTest(status=status):
                work = self.fixture(status=status)
                self.assertFalse(any(role['status'] == 'running' for role in work['roles'].values()))
                self.assertEqual(len(work['conclusions']), 2)

    def test_finished_report_and_pruned_runs_preserve_last_role_conclusions(self):
        state = {'rounds': 3, 'records': [{'role': 'worker', 'index': 3, 'status': 'done', 'summary': '任务完成'}],
                 'last_review': {'status': 'done', 'summary': '审核通过'}}
        work = work_details(state, {'phase': 'finished'}, {}, 'done', {}, {}, 'codex', 'claude')
        self.assertEqual(work['roles']['worker']['last_conclusion']['index'], 3)
        self.assertEqual(work['roles']['reviewer']['last_conclusion']['summary'], '审核通过')
        self.assertEqual({c['role'] for c in work['conclusions']}, {'worker', 'reviewer'})

    def test_validation_runs_in_watcher_without_marking_agents_as_executing(self):
        work = self.fixture(phase='validating')
        self.assertEqual(work['roles']['worker']['status'], 'waiting')
        self.assertEqual(work['roles']['reviewer']['status'], 'waiting')
        self.assertEqual(work['roles']['watcher']['status'], 'monitoring')

    def test_starting_worker_does_not_reuse_previous_reviewer_focus(self):
        activity = {'role': 'reviewer', 'round': 1, 'focus': '上一轮审核结论',
                    'events': [{'role': 'worker', 'round': 1, 'kind': 'message', 'text': '上一轮实现已完成'}]}
        work = work_details({}, {'phase': 'working', 'activity': 'round 2: starting worker'},
                            activity, 'running', {}, {}, 'codex', 'claude')
        self.assertEqual(work['roles']['worker']['current_work'], '正在实现任务')
        self.assertEqual(work['roles']['worker']['round'], 2)
        self.assertEqual(work['roles']['reviewer']['current_work'], '')

    def test_overdue_review_explains_failed_worker_rounds_and_running_turn(self):
        rounds = [{'role': 'reviewer', 'index': 2, 'summary': '发现两项缺口', 'status': 'continue'},
                  {'role': 'worker', 'index': 3, 'timed_out': True, 'exit_code': -15},
                  {'role': 'worker', 'index': 4, 'interruption': 'stop command', 'exit_code': -15},
                  {'role': 'worker', 'index': 5, 'interruption': 'stop command', 'exit_code': -15}]
        health = {'phase': 'working', 'activity': 'round 6: worker running',
                  'review_schedule': {'enabled': True, 'every_worker_rounds': 2, 'rounds_since_review': 3}}
        work = work_details({'rounds': rounds}, health, {}, 'running', {}, {}, 'codex', 'codex')
        reviewer = work['roles']['reviewer']
        self.assertEqual(reviewer['status'], 'waiting')
        self.assertEqual(reviewer['last_review_round'], 2)
        self.assertEqual(reviewer['failed_worker_rounds'], [3, 4, 5])
        self.assertTrue(reviewer['due'])
        self.assertIn('3/2', reviewer['wait_reason'])
        self.assertIn('正常结束', reviewer['wait_reason'])
        self.assertIn('未继续进入审核', reviewer['wait_reason'])

    def test_review_waiting_explains_disabled_periodic_review_and_terminal_states(self):
        health = {'phase': 'working', 'review_schedule': {'enabled': True, 'every_worker_rounds': 0,
                                                       'rounds_since_review': 10}}
        work = work_details({}, health, {}, 'running', {}, {}, 'codex', 'codex')
        self.assertIn('周期审核已关闭', work['roles']['reviewer']['wait_reason'])
        self.assertFalse(work['roles']['reviewer']['due'])
        work = work_details({}, health, {}, 'paused', {}, {}, 'codex', 'codex')
        self.assertIn('已暂停', work['roles']['reviewer']['wait_reason'])
        work = work_details({}, health, {}, 'stopped', {}, {}, 'codex', 'codex')
        self.assertIn('尚无审核结论', work['roles']['reviewer']['wait_reason'])
        work = work_details({'provider_config': {'no_review': True}}, health, {}, 'running', {}, {}, 'codex', 'codex')
        self.assertEqual(work['roles']['reviewer']['status'], 'disabled')
        self.assertIn('未启用独立审核', work['roles']['reviewer']['wait_reason'])
