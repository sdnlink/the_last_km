"""Pending cycle selection is deterministic, bounded and read-only when planned."""
from contextlib import contextmanager
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from auto_moniter.evolution import Evolution, git
from auto_moniter.storage import write_json
from auto_moniter.workspace import LockError, file_lock
from tests import test_evolution_phase_execution as phase_fixtures


class PendingEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.phases=phase_fixtures.EvolutionPhaseExecutionTests()
        self.addCleanup(self.phases.doCleanups)
        self.fixture=self.phases.fixture()

    def engine(self):
        return Evolution(self.fixture.config,evidence_roots=[self.fixture.evidence],
                         directory=self.fixture.engine.directory,log=lambda _:None)

    def cycle(self, ident='001', phase='implementing', issue=None):
        engine=self.engine()
        directory=engine.directory/ident
        directory.mkdir(parents=True)
        workspace=directory/'workspace'
        git(engine.source,'worktree','add','-b','evolution/'+ident,str(workspace),self.fixture.base)
        engine.path=directory/'evolution.json'
        engine.record={'version':1,'source':str(engine.source),'base':engine._identity(),
            'issue':issue or {'id':ident,'kind':'fixture','summary':'pending','evidence':[]},
            'workspace':str(workspace)}
        engine._save('implementing',monitor_run=str(directory/'monitor'))
        if phase!='implementing':
            engine._save(phase)
        return engine.path

    def snapshot(self):
        return {str(p.relative_to(self.fixture.root)):(p.stat().st_mtime_ns,p.read_bytes() if p.is_file() else None)
                for p in self.fixture.root.rglob('*')}

    def test_pending_implementation_is_planned_before_new_issue_without_writes(self):
        path=self.cycle()
        before=self.snapshot()
        with patch('pathlib.Path.mkdir',side_effect=AssertionError('read-only plan created directory')), \
             patch('auto_moniter.evolution.Supervisor',side_effect=AssertionError('plan called model')):
            plan=self.engine().run(plan_only=True)
        self.assertEqual(plan['phase'],'planned')
        self.assertEqual(plan['action'],'recover')
        self.assertEqual(plan['recovery_directory'],str(path.parent))
        self.assertEqual(plan['issue']['id'],'001')
        self.assertEqual(self.snapshot(),before)

    def test_real_interrupted_supervisor_resumes_instead_of_creating_second_cycle(self):
        monitor,_,_=self.phases.running_child_with_heartbeat(self.fixture,0)
        engine=self.engine()
        result=engine.run()
        self.assertEqual(result['phase'],'promoted',result.get('summary'))
        self.assertEqual(engine.path,self.fixture.engine.path)
        self.assertEqual(result['monitor_run'],str(monitor))
        self.assertEqual(list(engine.directory.glob('*/evolution.json')),[engine.path])
        self.assertEqual((len(self.fixture.worker.calls),len(self.fixture.reviewer.calls)),(1,2))
        self.assertEqual(git(self.fixture.source,'rev-list','--count','HEAD'),'2')

    def test_oldest_eligible_cycle_wins_deterministically(self):
        first=self.cycle('001')
        self.cycle('003')
        self.cycle('002',phase='rejected')
        engine=self.engine()
        with patch.object(engine,'recover',return_value={'phase':'resumed'}) as recover, \
             patch.object(engine,'_run',side_effect=AssertionError('second cycle created')):
            self.assertEqual(engine.run(),{'phase':'resumed'})
        recover.assert_called_once_with(first.parent,allow_implementation=True)
        self.assertEqual(len(list(engine.directory.glob('*/evolution.json'))),3)

    def test_oldest_pending_cycle_is_selected_even_if_created_last(self):
        self.cycle('003')
        self.cycle('002',phase='rejected')
        first=self.cycle('001')
        plan=self.engine().run(plan_only=True)
        self.assertEqual(plan['action'],'recover')
        self.assertEqual(plan['recovery_directory'],str(first.parent))

    def test_failed_issue_is_deduplicated_without_resuming_it(self):
        issue=self.engine().run(plan_only=True)['issue']
        self.cycle(phase='rejected',issue=issue)
        plan=self.engine().run(plan_only=True)
        self.assertEqual(plan['phase'],'planned')
        self.assertEqual(plan['issue']['kind'],'proactive_review')
        self.assertNotEqual(plan['issue']['id'],issue['id'])
        self.assertNotIn('recovery_directory',plan)

    def test_explicit_stop_pause_and_nonrunning_supervisor_are_not_resumed(self):
        path=self.cycle()
        original=json.loads(path.read_text())
        for action in ('stop','pause'):
            with self.subTest(action=action):
                control=path.parent/'control.json'
                write_json(control,{'action':action})
                before=path.read_bytes()
                engine=self.engine()
                with patch.object(engine,'recover',side_effect=AssertionError('manual control resumed')), \
                     patch.object(engine,'_run',side_effect=AssertionError('manual control started work')):
                    self.assertIn(engine.run()['phase'],{'stopped','deferred'})
                self.assertEqual(path.read_bytes(),before)
                control.unlink()
        stopped={**original,'phase':'stopped','stop_reason':'SIGTERM'}
        write_json(path,stopped)
        self.assertEqual(self.engine().run(plan_only=True)['phase'],'stopped')
        write_json(path,original)
        monitor=path.parent/'monitor';monitor.mkdir()
        write_json(monitor/'state.json',{'status':'action_required'})
        self.assertNotIn('recovery_directory',self.engine().run(plan_only=True))

    def test_corrupt_history_blocks_dispatch_and_is_preserved(self):
        path=self.cycle()
        path.write_text('{truncated')
        before=path.read_bytes()
        engine=self.engine()
        with patch.object(engine,'recover',side_effect=AssertionError('corrupt history resumed')), \
             patch.object(engine,'_run',side_effect=AssertionError('corrupt history bypassed')):
            self.assertEqual(engine.run()['phase'],'deferred')
            self.assertEqual(engine.run(plan_only=True)['phase'],'deferred')
        self.assertEqual(path.read_bytes(),before)

    def test_lock_competition_does_not_start_a_second_cycle(self):
        path=self.cycle()
        engine=self.engine()
        before=path.read_bytes()
        with file_lock(engine.directory/'evolution.lock'):
            with self.assertRaises(LockError):
                engine.run()
        monitor=path.parent/'monitor';monitor.mkdir()
        with file_lock(monitor/'supervisor.lock'), \
             patch.object(engine,'recover',side_effect=AssertionError('live monitor resumed')):
            self.assertEqual(engine.run()['phase'],'deferred')
        self.assertEqual(path.read_bytes(),before)

    def test_empty_plan_does_not_create_evolution_state(self):
        engine=self.engine()
        self.assertFalse(engine.directory.exists())
        before=self.snapshot()
        with patch('pathlib.Path.mkdir',side_effect=AssertionError('plan created state')):
            self.assertEqual(engine.run(plan_only=True)['phase'],'planned')
        self.assertFalse(engine.directory.exists())
        self.assertEqual(self.snapshot(),before)

    def assert_refused(self, phase, reason):
        engine=self.engine()
        before=self.snapshot()
        records=list(engine.directory.glob('*/evolution.json'))
        calls=(len(self.fixture.worker.calls),len(self.fixture.reviewer.calls))
        with patch('auto_moniter.evolution.Supervisor',side_effect=AssertionError('refused cycle dispatched')), \
             patch.object(engine,'_run',side_effect=AssertionError('refused history started execution')):
            plan=engine.run(plan_only=True)
            self.assertEqual(self.snapshot(),before)
            result=engine.run()
        for outcome in (plan,result):
            self.assertEqual(outcome['phase'],phase,outcome)
            self.assertIn(reason,outcome['summary'].lower())
        self.assertCountEqual(engine.directory.glob('*/evolution.json'),records)
        self.assertEqual(git(self.fixture.source,'rev-parse','HEAD'),self.fixture.base)
        self.assertEqual((len(self.fixture.worker.calls),len(self.fixture.reviewer.calls)),calls)
        for name,(_,contents) in before.items():
            if contents is not None and not name.endswith('/evolution.lock'):
                self.assertEqual((self.fixture.root/name).read_bytes(),contents,name)

    def test_history_order_cannot_bypass_stop_pause_or_damage(self):
        first=self.cycle('001')
        second=self.cycle('002')
        original=first.read_bytes()
        for refused, eligible in ((first,second),(second,first)):
            self.assertNotEqual(refused,eligible)
            control=refused.parent/'control.json'
            for data,phase,reason in (
                    ('{"action":"stop"}','stopped','stop'),
                    ('{"action":"pause"}','deferred','pause'),
                    ('{"action":[]}','deferred','control'),
                    ('{"action":"stop","action":"continue"}','deferred','control')):
                with self.subTest(refused=refused,control=data):
                    control.write_text(data)
                    self.assert_refused(phase,reason)
            control.unlink()
        first.write_text('{"phase": []}')
        self.assert_refused('deferred','checkpoint')
        saved=json.loads(original)
        write_json(first,{**saved,'phase':'stopped','stop_reason':'SIGTERM'})
        self.assert_refused('stopped','stop')

    def test_shared_budget_at_limit_cannot_be_skipped(self):
        first=self.cycle('001')
        self.cycle('002')
        original=json.loads(first.read_text())
        for counter,limit in (('elapsed_s','max_runtime_s'),('agent_calls','max_agent_calls')):
            with self.subTest(counter=counter):
                spent=original['config_identity']['limits'][limit]
                write_json(first,{**original,counter:spent,'usage':{**original['usage'],counter:spent}})
                self.assert_refused('stopped','budget')

    def test_missing_checkpoint_with_worktree_cannot_start_replacement_cycle(self):
        path=self.cycle('001')
        path.unlink()
        self.assert_refused('deferred','checkpoint')

    def test_failed_cycle_with_exhausted_budget_cannot_receive_replacement_budget(self):
        first=self.cycle('001',phase='failed')
        self.cycle('002')
        checkpoint=json.loads(first.read_text())
        limit=checkpoint['config_identity']['limits']['max_agent_calls']
        write_json(first,{**checkpoint,'agent_calls':limit,'usage':{**checkpoint['usage'],'agent_calls':limit}})
        self.assert_refused('stopped','budget')

    def test_child_checkpoint_and_heartbeat_exhaustion_refuse_automatic_recovery(self):
        monitor,original,recorded=self.phases.running_child_with_heartbeat(self.fixture,0)
        path=monitor/'state.json'
        usage=original['usage']
        write_json(path,{**original,'status':'budget_exceeded'})
        self.assert_refused('stopped','budget')
        write_json(path,{**original,'usage':{**usage,'agent_calls':usage['max_agent_calls']}})
        self.assert_refused('stopped','budget')
        write_json(path,original)
        write_json(monitor/'heartbeat',{'at':(recorded+timedelta(seconds=usage['max_runtime_s'])).isoformat()})
        self.assert_refused('stopped','budget')

    def test_budget_report_written_before_state_cannot_be_treated_as_interruption(self):
        monitor,_,_=self.phases.running_child_with_heartbeat(self.fixture,0)
        write_json(monitor/'report.json',{'status':'budget_exceeded','rounds':1,
                   'summary':'Runtime exhausted','stop_reason':'budget_exceeded'})
        self.assert_refused('stopped','budget')

    def test_malformed_child_checkpoint_is_refused_in_read_only_plan_and_execution(self):
        monitor,original,_=self.phases.running_child_with_heartbeat(self.fixture,0)
        write_json(monitor/'state.json',{**original,'provider_config':{}})
        self.assert_refused('deferred','supervisor configuration')

    def test_guardian_on_a_later_cycle_blocks_earlier_candidate(self):
        self.cycle('001')
        second=self.cycle('002')
        with file_lock(second.parent/'monitor/guardian.lock'):
            self.assert_refused('deferred','active')

    def test_control_and_budget_are_rechecked_at_recovery_lock_handoff(self):
        monitor,original,_=self.phases.running_child_with_heartbeat(self.fixture,0)
        for name in ('stop','pause','invalid control','budget'):
            with self.subTest(change=name):
                engine=self.engine()
                cycle_lock=engine.directory/'evolution.lock'
                acquisitions=[]
                control=monitor/'control.json'
                state=monitor/'state.json'
                before=engine.directory.glob('*/evolution.json')
                records={p:p.read_bytes() for p in before}

                @contextmanager
                def changed_at_lock(path):
                    if path==cycle_lock:
                        acquisitions.append(path)
                        if len(acquisitions)==2:
                            if name=='budget':
                                write_json(state,{**original,'status':'budget_exceeded'})
                            else:
                                write_json(control,{'action':[] if name=='invalid control' else name})
                    with file_lock(path):
                        yield

                with patch('auto_moniter.evolution.file_lock',side_effect=changed_at_lock), \
                     patch.object(engine,'_recovery_quiesce',side_effect=AssertionError('refused cycle reached cleanup')), \
                     patch('auto_moniter.evolution.Supervisor',side_effect=AssertionError('refused cycle dispatched')):
                    result=engine.run()
                self.assertIn(result['phase'],{'stopped','deferred'})
                self.assertEqual({p:p.read_bytes() for p in engine.directory.glob('*/evolution.json')},records)
                self.assertEqual(git(self.fixture.source,'rev-parse','HEAD'),self.fixture.base)
                control.unlink(missing_ok=True)
                write_json(state,original)

    def test_stop_arriving_while_acquiring_child_lock_prevents_cleanup(self):
        monitor,_,_=self.phases.running_child_with_heartbeat(self.fixture,0)
        engine=self.engine()
        record=monitor.parent/'evolution.json'
        before=record.read_bytes()

        @contextmanager
        def stop_at_child_lock(path):
            with file_lock(path):
                if path==monitor/'supervisor.lock':
                    write_json(monitor/'control.json',{'action':'stop'})
                yield

        with patch('auto_moniter.evolution.file_lock',side_effect=stop_at_child_lock), \
             patch.object(engine,'_recovery_quiesce',side_effect=AssertionError('stopped cycle reached cleanup')), \
             patch('auto_moniter.evolution.Supervisor',side_effect=AssertionError('stopped cycle dispatched')):
            result=engine.run()
        self.assertEqual(result['phase'],'stopped')
        self.assertEqual(record.read_bytes(),before)
        self.assertEqual(list(engine.directory.glob('*/evolution.json')),[record])
        self.assertEqual((len(self.fixture.worker.calls),len(self.fixture.reviewer.calls)),(1,1))
        self.assertEqual(git(self.fixture.source,'rev-parse','HEAD'),self.fixture.base)


if __name__=='__main__':
    unittest.main()
