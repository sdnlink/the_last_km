"""Validate and normalize a saved CLI launch before resuming an imported run."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from auto_moniter.cli import build_parser
from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor


def resume_arguments(manifest: dict, run: Path, state: dict) -> list[str]:
    """Replay only the monitor CLI, checking its workspace and provider identity."""
    argv = manifest.get('argv')
    if not isinstance(argv, list) or not argv or len(argv) > 1000 or any(
            not isinstance(value, str) or '\x00' in value or len(value) > 128 * 1024 for value in argv):
        raise ValueError('原始启动参数格式不正确')
    if len(argv) >= 4 and argv[1:4] == ['-m', 'auto_moniter', 'run']:
        argv = argv[3:]
    if argv[0] != 'run' or any(value in {'--help', '-h', '--version'} for value in argv):
        raise ValueError('原始启动配置必须是 auto_moniter run 参数')
    parser = build_parser()

    def invalid(message):
        raise ValueError('原始启动参数无效：' + message)

    parser.error = invalid
    # Subparsers handle their own validation errors; keep those inside the API too.
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                child.error = invalid
    args = parser.parse_args(argv)
    if args.run_dir is None or not args.run_dir.is_absolute() or args.run_dir.resolve() != run.resolve():
        raise ValueError('原始启动配置与任务运行目录不一致')
    if not args.cwd.is_absolute() or args.cwd.resolve() != Path(state.get('cwd', '')).resolve():
        raise ValueError('原始启动配置与任务项目目录不一致')
    if args.provider == 'command' or args.reviewer == 'command':
        raise ValueError('自定义命令任务请使用原 CLI 配置恢复')
    worker = resolve_provider(args.provider, paseo_backend=args.paseo_backend,
                              paseo_mode=args.paseo_mode, mode=args.mode)
    reviewer = None
    if not args.no_review:
        if not args.reviewer:
            raise ValueError('原始启动配置缺少独立审查器')
        reviewer = resolve_provider(args.reviewer, paseo_backend=args.reviewer_paseo_backend or args.paseo_backend,
                                    paseo_mode=args.reviewer_paseo_mode, mode=args.reviewer_mode, role='reviewer')
        reviewer.validate_review(args.reviewer_extra_arg)
    config = RunConfig('', args.cwd.resolve(), worker, reviewer=reviewer,
                       no_review=args.no_review, extra_args=args.extra_arg,
                       reviewer_extra_args=args.reviewer_extra_arg,
                       review_isolation=args.review_isolation, worker_isolation=args.worker_isolation,
                       protect_paths=args.protect)
    expected = state.get('provider_config')
    if not isinstance(expected, dict):
        raise ValueError('检查点缺少原始执行器配置')
    expected = dict(expected)
    expected.setdefault('review_isolation', 'provider')
    expected.setdefault('worker_isolation', 'provider')
    expected.setdefault('protect_paths', [])
    if Supervisor._provider_config(SimpleNamespace(config=config)) != expected:
        raise ValueError('原始启动配置与检查点的执行器、审核或隔离设置不一致')
    if args.check and args.check != state.get('checks', []):
        raise ValueError('原始验收命令与检查点不一致')
    if args.accept and args.accept != state.get('acceptance', ''):
        raise ValueError('原始验收标准与检查点不一致')
    # Use checkpoint requirement/modules/checks, so source input files need not still exist.
    result = ['run', '--resume', '--cwd', str(args.cwd.resolve()), '--run-dir', str(run.resolve()),
              '--provider', args.provider, '--worker-isolation', args.worker_isolation,
              '--review-isolation', args.review_isolation]
    strings = {'model': '--model', 'mode': '--mode', 'paseo_backend': '--paseo-backend',
               'paseo_mode': '--paseo-mode', 'reviewer': '--reviewer', 'reviewer_model': '--reviewer-model',
               'reviewer_mode': '--reviewer-mode', 'reviewer_paseo_backend': '--reviewer-paseo-backend',
               'reviewer_paseo_mode': '--reviewer-paseo-mode'}
    settings = ('max_rounds', 'max_runtime', 'max_agent_calls', 'stall_limit', 'failure_limit',
                'restart_after_stall', 'turn_timeout', 'idle_timeout', 'retry_delay', 'check_timeout',
                'review_every', 'keep_rounds', 'max_output_bytes', 'memory_bytes', 'file_bytes', 'max_run_bytes')
    for key, flag in strings.items():
        if getattr(args, key):
            result.append(flag + '=' + getattr(args, key))
    for key in settings:
        value = getattr(args, key)
        if value is not None:
            result.append('--' + key.replace('_', '-') + '=' + str(value))
    for key, flag in [('protect', '--protect'), ('extra_arg', '--extra-arg'), ('reviewer_extra_arg', '--reviewer-extra-arg')]:
        result.extend(flag + '=' + value for value in getattr(args, key))
    if args.no_review:
        result.append('--no-review')
    if not args.fresh_review:
        result.append('--no-fresh-review')
    if args.guard:
        result.extend(['--guard', '--guard-restarts=' + str(args.guard_restarts),
                       '--guard-delay=' + args.guard_delay, '--guard-stop-timeout=' + args.guard_stop_timeout])
    return result
