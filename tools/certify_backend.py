"""Opt-in real-provider smoke with pinned version checks; may incur model cost."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", choices=["claude", "codex", "grok"], required=True)
    parser.add_argument("--reviewer", choices=["claude", "codex", "grok"], required=True)
    parser.add_argument("--expected-worker-version", required=True)
    parser.add_argument("--expected-reviewer-version", required=True)
    parser.add_argument('--model', default='', help='Explicit worker model for reproducible certification')
    parser.add_argument('--reviewer-model', default='', help='Explicit independent reviewer model')
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Explicitly authorize paid agent calls for this smoke")
    args = parser.parse_args()
    versions = {}
    for role, name, expected in [("worker", args.worker, args.expected_worker_version), ("reviewer", args.reviewer, args.expected_reviewer_version)]:
        result = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=10)
        actual = result.stdout.strip()
        if result.returncode or not (expected == actual or expected in actual.split()):
            parser.error(f"{role} CLI version does not match expected version")
        versions[role] = {"name": name, "version": actual}
    report = {"versions": versions, "models": {'worker': args.model or '(provider default)', 'reviewer': args.reviewer_model or args.model or '(provider default)'},
              "status": "not_executed", "scope": "small real-provider functional smoke only"}
    if args.execute:
        with tempfile.TemporaryDirectory(prefix="auto-moniter-certification-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            tests = project / "tests"
            tests.mkdir()
            (tests / "test_add.py").write_text("import unittest\nfrom calculator import add\nclass TestAdd(unittest.TestCase):\n def test_add(self):\n  self.assertEqual(add(2,3),5)\n  self.assertEqual(add(-2,3),1)\n")
            (project / ".gitignore").write_text("__pycache__/\n")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # Persist evidence rather than deleting it with the temporary project.
            run = args.output.resolve().with_suffix(".run")
            command = [sys.executable, "-m", "auto_moniter", "run", "Implement calculator.py with add(a,b), preserve tests, verify positive and negative inputs, and deliver the entire requirement.",
                       "--cwd", str(project), "--run-dir", str(run), "--provider", args.worker, "--reviewer", args.reviewer,
                       "--worker-isolation", "strict", "--review-isolation", "strict", "--protect", "tests",
                       "--check", __import__('shlex').join([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q']),
                       "--max-runtime", "10m", "--max-agent-calls", "8", "--max-rounds", "4"]
            if args.model:
                command.append('--model=' + args.model)
            if args.reviewer_model:
                command.append('--reviewer-model=' + args.reviewer_model)
            result = subprocess.run(command, timeout=720)
            report.update(status="passed" if result.returncode == 0 else "failed", exit=result.returncode, run_dir=str(run))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] in {"passed", "not_executed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
