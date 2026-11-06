import signal
import subprocess
import sys
import unittest
from unittest.mock import call, patch

from auto_moniter.processes import reap_background


class ProcessPollingTests(unittest.TestCase):
    def test_absent_group_does_not_scan_all_host_processes(self):
        with patch("auto_moniter.processes.os.killpg", side_effect=ProcessLookupError) as probe, patch("auto_moniter.processes._members") as members:
            self.assertFalse(reap_background(12345))
        probe.assert_called_once_with(12345, 0)
        members.assert_not_called()

    def test_existing_group_still_checks_and_terminates_background_processes(self):
        with patch("auto_moniter.processes.os.killpg") as signal_group, patch("auto_moniter.processes._members", side_effect=[[12346], [], [], []]) as members:
            self.assertTrue(reap_background(12345))
        self.assertEqual(signal_group.call_args_list, [call(12345, 0), call(12345, signal.SIGTERM)])
        self.assertEqual(members.call_count, 4)

    def test_uncertain_probe_does_not_skip_verification(self):
        with patch("auto_moniter.processes.os.killpg", side_effect=PermissionError), patch("auto_moniter.processes._members", return_value=[]) as members:
            self.assertFalse(reap_background(12345))
        members.assert_called_once_with(12345)

    def test_invalid_group_is_rejected_before_any_signal_or_scan(self):
        for group in (0, 1, -1, True, "12345"):
            with self.subTest(group=group), patch("auto_moniter.processes.os.killpg") as probe, patch("auto_moniter.processes._members") as members:
                with self.assertRaises(ValueError):
                    reap_background(group)
                probe.assert_not_called()
                members.assert_not_called()

    def test_real_reaped_subprocess_needs_no_proc_scan(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        child.wait(timeout=3)
        with patch("auto_moniter.processes._members", side_effect=AssertionError("unexpected full /proc scan")):
            self.assertFalse(reap_background(child.pid))


if __name__ == "__main__":
    unittest.main()
