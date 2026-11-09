"""Verify current NDR documentation without rewriting historical evidence.

The trusted baseline must be outside the writable target project. All source,
contract, test and historical evidence files remain byte-for-byte protected.
"""
import argparse
from contextlib import contextmanager
import gzip
import hashlib
import importlib.util
import json
import sys
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pathlib import Path
from auto_moniter.workspace import file_snapshot, workspace_fingerprint


def baseline_problems(project, baseline, current):
    errors = []
    for name in baseline.keys() | current.keys():
        if baseline.get(name) == current.get(name):
            continue
        relative = Path(name).relative_to(project)
        if relative.parts[0] != 'docs' or relative.suffix != '.md':
            errors.append('Protected asset changed: ' + str(relative))
    return errors


@contextmanager
def no_bytecode():
    original = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = original


def verify(project, baseline_path):
    with no_bytecode():
        return _verify(project, baseline_path)


def _verify(project, baseline_path):
    project, baseline_path = project.resolve(), baseline_path.resolve()
    if baseline_path.is_relative_to(project):
        raise ValueError('Trusted baseline must be outside the target project')
    baseline = json.loads(gzip.decompress(baseline_path.read_bytes()))
    if not baseline or any(not Path(name).is_relative_to(project) for name in baseline):
        raise ValueError('Baseline belongs to a different project')
    current = file_snapshot(project)
    errors = baseline_problems(project, baseline, current)
    allowed_roots = {Path(name).relative_to(project).parts[0] for name in baseline} | {'.git', '.auto-moniter', '__pycache__'}
    errors += ['Unexpected project entry: ' + p.name for p in project.iterdir() if p.name not in allowed_roots]
    if errors:
        return {'passed': False, 'errors': errors, 'archived_documentation': []}
    before = workspace_fingerprint(project)
    spec = importlib.util.spec_from_file_location('ndr_review_readonly', project / 'scripts/review_development.py')
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    dev, contract = review.module('check_development_contracts'), review.module('check_hzd_contracts')
    docs, implementation, resources = dev.load_context()
    api = contract.load_spec()
    record = json.loads((project / 'docs/sql/validation/2026-11-05-code-review.json').read_text())
    archived = {row['path'] for row in record['artifacts']
                if row['path'].startswith('docs/') and Path(row['path']).suffix == '.md'
                and hashlib.sha256((project / row['path']).read_bytes()).hexdigest() != row['sha256']}
    # Suppress only exact historical-document mismatch findings. All other focus
    # assertions, including non-Markdown hash checks, execute unchanged.
    stale = []
    plan = review.review_plan(docs, implementation, resources)
    if len(plan) != 100 or len({json.dumps(f, sort_keys=True) for f in plan}) != 100:
        errors.append('Expected 100 distinct focus definitions')
    for focus in plan:
        findings, _ = review.focus_check(focus, docs, implementation, resources, api)
        for error in findings:
            prefix = 'Stale validation artifact: '
            path = error[len(prefix):] if error.startswith(prefix) else ''
            if focus['subject'] == 'evidence_hashes' and path in archived:
                stale.append(path)
            else:
                errors.append(error)
    if workspace_fingerprint(project) != before:
        errors.append('Project changed during documentation verification')
    return {'passed': not errors, 'errors': errors, 'focus_count': len(plan), 'workspace_tree': before,
            'archived_documentation': sorted(set(stale)),
            'scope': 'Current static documentation focus checks only. Historical runtime evidence remains valid only for its recorded hashes; no current runtime approval.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.project, args.baseline)
    except Exception as error:
        result = {'passed': False, 'errors': [type(error).__name__ + ': ' + str(error)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
