"""Use a private temporary state root for tests and their subprocesses."""
import atexit
import os
import shutil
import tempfile
_test_state = tempfile.mkdtemp(prefix="auto-moniter-test-state-")
os.environ["AUTO_MONITER_STATE_DIR"] = _test_state
atexit.register(shutil.rmtree, _test_state, ignore_errors=True)
