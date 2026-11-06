"""Offline acceptance test for an installed package; never calls a model."""

import json
import shlex
import sys
import tempfile
from pathlib import Path

from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor


def selftest() -> dict:
    with tempfile.TemporaryDirectory(prefix="auto-moniter-selftest-") as directory:
        root = Path(directory)
        verdict = {"status": "done", "scope": "requirement", "summary": "Offline fixture ready for delivery",
                   "module_ids": ["fixture"], "modules": [{"id": "fixture", "title": "Offline fixture",
                   "acceptance": ["fixture file contains expected content"], "depends_on": []}],
                   "done": ["fixture created"], "gaps": [], "next_actions": [], "blockers": [], "evidence": ["fixture.txt"]}
        code = f"from pathlib import Path; import json; Path('fixture.txt').write_text('ok'); print(json.dumps({verdict!r}))"
        provider = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
        check = shlex.join([sys.executable, "-c", "from pathlib import Path; assert Path('fixture.txt').read_text() == 'ok'"])
        result = Supervisor(RunConfig("Create an offline fixture", root, provider, no_review=True,
                                     checks=[check], max_rounds=1, max_runtime_s=10, run_dir=root / "run"), log=lambda _: None).run()
        report = json.loads((root / "run/report.json").read_text()) if (root / "run/report.json").exists() else {}
        passed = result.status == "done" and report.get("modules", [{}])[0].get("status") == "accepted"
        return {"passed": passed, "status": result.status, "summary": result.summary,
                "checks": ["worker subprocess", "structured conclusion", "module ledger", "monitor acceptance command", "report persistence"],
                "paid_model_calls": 0}
