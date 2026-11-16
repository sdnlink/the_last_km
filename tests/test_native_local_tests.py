import contextlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auto_moniter.local_tests import command, catalog
from auto_moniter import native_test_runner


SCHEME = '''<Scheme><TestAction><Testables><TestableReference skipped="NO"><BuildableReference BlueprintName="AppTests" BuildableName="AppTests.xctest" ReferencedContainer="container:App.xcodeproj"/></TestableReference></Testables></TestAction></Scheme>'''
DEVICE='12345678-1234-1234-1234-123456789ABC'


class NativeLocalTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        scheme=self.root/'ios/App.xcodeproj/xcshareddata/xcschemes/App.xcscheme'
        scheme.parent.mkdir(parents=True);scheme.write_text(SCHEME)
        self.test=self.root/'ios/AppTests/GoalTests.swift';self.test.parent.mkdir()
        self.test.write_text('import XCTest\nfinal class GoalTests: XCTestCase { func testActual() {} }\n')

    def args(self):
        return SimpleNamespace(runner='xcode',manifest='ios/App.xcodeproj',executable='xcodebuild',scheme='App',target='AppTests')

    def test_xctest_command_derived_without_operator_ids_and_supports_planned_file(self):
        value=command(self.root,'ios/AppTests/FutureTests.swift')
        self.assertIn('--runner xcode',value)
        self.assertIn('--scheme App',value)
        self.assertIn('--target AppTests',value)
        self.assertIn('auto:1',catalog(self.root,{'test_files':['ios/AppTests/FutureTests.swift']}))
        self.assertIsNone(command(self.root,'ios/UnrelatedTests/GoalTests.swift'))

    def test_swift_package_command_uses_existing_nearest_manifest(self):
        (self.root/'Package.swift').write_text('// swift-tools-version: 5.9\n')
        value=command(self.root,'Tests/GoalsTests/GoalTests.swift')
        self.assertIn('--runner swift',value)
        self.assertIn('--manifest Package.swift',value)

    def test_swift_package_filters_to_declared_classes_and_empty_file_cannot_use_old_tests(self):
        (self.root/'Package.swift').write_text('// swift-tools-version: 5.9\n')
        args=SimpleNamespace(runner='swift',manifest='Package.swift',executable='swift')
        with patch('auto_moniter.native_test_runner.stream',return_value=0) as stream:
            self.assertEqual(native_test_runner.run(args,self.root,self.test),0)
        self.assertEqual(stream.call_args.args[0][-2:],['--filter','GoalTests'])
        self.test.write_text('import XCTest\n')
        with patch('auto_moniter.native_test_runner.stream') as stream,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(native_test_runner.run(args,self.root,self.test),1)
        stream.assert_not_called()

    def test_ambiguous_scheme_and_skipped_target_do_not_invent_selection(self):
        scheme=self.root/'ios/App.xcodeproj/xcshareddata/xcschemes'
        (scheme/'Other.xcscheme').write_text(SCHEME)
        self.assertIsNone(command(self.root,'ios/AppTests/GoalTests.swift'))
        (scheme/'Other.xcscheme').unlink()
        (scheme/'App.xcscheme').write_text(SCHEME.replace('skipped="NO"','skipped="YES"'))
        self.assertIsNone(command(self.root,'ios/AppTests/GoalTests.swift'))

    def test_missing_mac_is_honest_failure_without_executing_xcode(self):
        with patch('auto_moniter.native_test_runner.platform.system',return_value='Linux'), \
                patch('auto_moniter.native_test_runner.subprocess.run') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(native_test_runner.run(self.args(),self.root,self.test),127)
        run.assert_not_called()
        # The actual executable wrapper also reports platform absence, not success.
        if sys.platform=='linux':
            result=subprocess.run(shlex.split(command(self.root,'ios/AppTests/GoalTests.swift')),capture_output=True,text=True)
            self.assertEqual(result.returncode,127,result.stdout+result.stderr)
            self.assertIn('requires macOS',result.stdout)

    def test_xcode_runner_binds_file_classes_and_rejects_failed_or_skipped_summary(self):
        summaries=[({'totalTestCount':2,'passedTests':2,'failedTests':0,'skippedTests':0},0),
                   ({'totalTestCount':2,'passedTests':1,'failedTests':0,'skippedTests':1},1),
                   ({'totalTestCount':2,'passedTests':1,'failedTests':1,'skippedTests':0},1),({},1)]
        for summary,expected in summaries:
            calls=[]
            def fake_run(argv,**kwargs):
                calls.append(argv)
                result=({'devices':{'com.apple.CoreSimulator.SimRuntime.iOS-18-2':[
                    {'udid':DEVICE,'isAvailable':True,'name':'iPhone 16'}]}} if 'simctl' in argv else summary)
                return subprocess.CompletedProcess(argv,0,stdout=json.dumps(result))
            with patch('auto_moniter.native_test_runner.platform.system',return_value='Darwin'), \
                    patch('auto_moniter.native_test_runner.subprocess.run',side_effect=fake_run), \
                    patch('auto_moniter.native_test_runner.stream',return_value=0) as stream, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(native_test_runner.run(self.args(),self.root,self.test),expected)
            argv=stream.call_args.args[0]
            self.assertIn('-only-testing:AppTests/GoalTests',argv)
            self.assertIn('CODE_SIGN_IDENTITY=-',argv)
            self.assertIn('platform=iOS Simulator,id='+DEVICE,argv)

    def test_missing_test_classes_and_failed_test_command_cannot_pass(self):
        self.test.write_text('import Foundation\n')
        with patch('auto_moniter.native_test_runner.platform.system',return_value='Darwin'),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(native_test_runner.run(self.args(),self.root,self.test),1)

    def test_empty_native_output_cannot_count_as_behavioral_success(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(native_test_runner.stream([sys.executable,'-c','print("BUILD SUCCEEDED")']),1)
            self.assertEqual(native_test_runner.stream([sys.executable,'-c','print("Executed 1 test, with 0 failures")']),0)
            self.assertEqual(native_test_runner.stream([sys.executable,'-c','print("Executed 1 test, with 0 failures");raise SystemExit(1)']),1)
