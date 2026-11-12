# Auto Moniter

**English** | [简体中文](README.zh-CN.md)

Give your coding agents a clear path from a task brief to verified delivery.

Auto Moniter is a local supervisor for AI coding agents. It coordinates implementation, independent review, and the acceptance checks you define. When a review finds gaps, it feeds them back for another repair. Delivery is accepted only when the required review, evidence, checks, and version gates pass.

It works with the Agent CLIs you already use, with a Linux CLI and a web console for managing tasks, progress, review findings, logs, and resource use.

- **Current version:** 0.4.1
- **Platform:** Linux, Python 3.11 or later
- **Runtime dependencies:** Python standard library; install and authenticate your chosen Agent CLIs separately
- **Project website:** [Chinese](https://sdnlink.cn/index.html) · [English](https://sdnlink.cn/en/index.html)

## Why use it?

Getting an agent to write code is one part of the job. Keeping the original scope in view, reviewing the change independently, and collecting evidence that it works takes a workflow.

Auto Moniter provides that workflow:

- Keep the original requirements and human acceptance criteria attached to the task.
- Clarify scope and edge cases before implementation with the review-first workflow.
- Assign focused work, including individual named functions where function dispatch is enabled.
- Track review findings under stable issue IDs until they are independently resolved.
- Run your configured acceptance checks and verify that evidence matches the code being reviewed.
- Preserve checkpoints, review context, and accumulated usage when a task pauses or resumes.
- Record approved local Git commits after review and checks pass.

An agent saying “done” is a claim to verify. A formatting check passing does not prove a feature works. Tests and evidence should match the behavior your task requires.

## How the roles work together

| Role | Responsibility |
| --- | --- |
| **Worker** | Implements the assigned change, adds tests, and reports its scope, results, and supporting evidence. |
| **Reviewer** | Inspects code and existing evidence without editing the implementation; reports gaps and verifies repairs. |
| **Watcher / Supervisor** | Coordinates the agents, runs configured checks, maintains task and issue state, and makes the final acceptance decision. |

The default review-first workflow starts with a read-only requirement review. Once the requirement is ready, the worker implements it and the reviewer checks the result. Gaps go back for repair. The watcher accepts delivery only after the relevant gates pass.

Periodic reviews can also check work in progress. `--review-every 5` is the default; `0` disables turn-based periodic reviews while retaining completion, blocker, and applicable quality-policy checkpoints. A review interval schedules a review after a worker turn returns; it does not interrupt a running turn.

If the reviewer returns `needs_input`, the requirement is not ready to dispatch. Missing external decisions, exhausted budgets, authentication failures, or unsafe recovery can stop a run with a non-delivery status and saved diagnostics.

See [workflow details](WORKFLOW.md), [quality policy](QUALITY_POLICY.md), and the [complete Chinese reference](README.zh-CN.md) for the full contracts and recovery behavior.

## Quick start

Install Python 3.11+ on Linux, then clone the repository:

```sh
git clone https://github.com/sdnlink/the_last_km.git
cd the_last_km
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

Before starting model calls, run the local checks:

```sh
auto-moniter doctor --cwd /path/to/your/project
auto-moniter selftest
```

`doctor` checks the environment and CLI availability; it does not verify account authentication or model access. `selftest` uses local fixtures and does not call a model.

### Use the web console

From the repository root:

```sh
./start.sh
# Or: python3 -m console
```

Open `http://127.0.0.1:8765`. Use the setup screen to configure your agents, then create a task with an explicit requirement, acceptance criteria, relevant checks, and a suitable budget.

The console supports task control, live activity, module acceptance, review findings, Git records, and resource monitoring. For a resumable stopped task, sending an amendment saves it and attempts to resume execution. Previously saved amendments can be sent with an empty text field; there is no need to enter them again. If recovery is blocked, the saved content stays intact and the UI reports the reason. Budget increases and new executable checks still require explicit configuration.

See the [console guide](console/README.md) for setup and controls.

### Use the CLI

After installing and authenticating the worker and reviewer CLIs, start a task against your project:

```sh
auto-moniter run \
  --cwd /path/to/your/project \
  --provider claude \
  --reviewer codex \
  --workflow review-first \
  --guard \
  --max-runtime 2h \
  --max-agent-calls 100 \
  --check 'python3 -m unittest discover -s tests -q' \
  'Add a health endpoint and verify success, failure, and boundary cases.'
```

Replace the example check with a real behavioral check for your project. The worker and reviewer must both have the required CLI access. `--guard` monitors the supervisor process; it does not perform the business review.

You can also run directly from the source directory without installing the package:

```sh
python3 -m auto_moniter providers
python3 -m auto_moniter run --help
```

The executable is named `auto-moniter`; the Python module is `auto_moniter`.

## Develop one project in parallel

In the console's task creation form, open **同工程并行开发** and enable a separate Git worktree. Give each task a concrete list of files or directories it owns, including new tests. Directories end with `/`. For example, assign `src/auth/` and `tests/test_auth.py` to one task, and `src/billing/` and `tests/test_billing.py` to another. If ownership overlaps, declare the earlier task as a prerequisite instead of running both against those files at once.

Each task starts from committed code on its own `auto-moniter-task/<task-id>` branch. Your original checkout and its uncommitted work stay intact. Relative check commands run in the task's checkout; configure any dependencies that checkout needs before starting it.

While agents run, the watcher polls shared source and peer work for changes, usually every three seconds. A changed input invalidates the old conclusion. The reviewer assesses its impact and refreshes the worker's assignment before implementation continues. The watcher merges committed source updates and declared prerequisites only after their final watcher delivery is confirmed. Peer drafts are visible as coordination context, and remain unaccepted until reviewed.

Conflicting merges preserve files and the index, with details passed to review. Out-of-scope edits and unresolved integration errors prevent publication. A history rewrite or an unsafe recovery still needs intervention. Work is delivered to each task's branch; integrating those branches into your main branch remains a separate step. Existing same-worktree locks remain in force.

## Agent support

Supported native workers include Claude, Codex, Grok, Antigravity, Cursor, Gemini, Copilot, Hermes, Kimi, and CodeBuddy. GLM and DeepSeek integrations use configured Claude Code backends. Selected agents can also run through Paseo.

Reviewer support is narrower: the backend must provide a supported read-only or planning mode. Worker availability does not imply reviewer compatibility. Consult the [agent setup guide](console/AGENT_SETUP.md) and run `auto-moniter providers` to inspect your environment.

Native Codex review uses a read-only sandbox; other supported native reviewers use their applicable planning modes. Optional strict review isolation uses Bubblewrap and verifies the boundary before starting. Unsupported combinations fail explicitly.

## Pause, resume, and inspect

Every run prints its record directory. By default, records live under `$XDG_STATE_HOME/auto-moniter/` or `~/.local/state/auto-moniter/`, outside the project tree. Explicit and legacy run directories remain supported.

```sh
auto-moniter status /path/to/run
auto-moniter watch /path/to/run --once
auto-moniter run --resume --run-dir /path/to/run \
  --cwd /path/to/your/project --provider claude --reviewer codex
```

Resume retains the original requirement, acceptance criteria, checks, review context, isolation settings, and cumulative usage. Resuming a task does not reset its budget. When more budget is needed, explicitly increase the applicable limit or use the console recovery window.

The supervisor verifies workspace and version consistency. Changes made outside the reviewed candidate may require another review. A stopped or interrupted run is not a completed delivery.

## Deployment and data flow

The supervisor and console run locally. Your chosen agent may send code, prompts, or context to a model service according to its own configuration. Local orchestration is not a guarantee of offline inference.

The console listens on loopback by default. For remote access, use an SSH tunnel:

```sh
ssh -L 8765:127.0.0.1:8765 user@server
```

The [operations guide](OPERATIONS.md) covers deployment and troubleshooting. Templates in [deploy/](deploy/) require adaptation to your users, paths, credentials, isolation, and resource limits.

The standalone company website lives in [webdist/](webdist/README.md). It includes Chinese and English pages, uses relative resource links, and can be served by a static web server without the development console API.

## Documentation

| Guide | Contents |
| --- | --- |
| [完整中文说明](README.zh-CN.md) | Full command reference, workflow contracts, edge cases, and recovery details |
| [Web console](console/README.md) | Task management, controls, configuration, and logs |
| [Agent setup](console/AGENT_SETUP.md) | Agent installation, authentication, models, and supported roles |
| [Workflow](WORKFLOW.md) | Review-first delivery, Git boundaries, and recovery |
| [Acceptance guide](ACCEPTANCE_GUIDE.md) | Selecting behavioral checks and understanding evidence |
| [Quality policy](QUALITY_POLICY.md) | Protected tests, progress gates, integration tasks, and budgets |
| [Operations](OPERATIONS.md) | Deployment, diagnosis, and process recovery |
| [Changelog](CHANGELOG.md) | Release history |

The overview on this page is in English. The detailed reference guides currently remain in Chinese.

## Development checks

```sh
python3 -m unittest discover -q
node --test tests/*.mjs
git diff --check
```

The automated tests use local fixtures and simulated providers rather than paid model calls. Some environment-dependent checks may be skipped when their prerequisites are unavailable.

## Contact

Beijing Changxian Technology Co., Ltd. (北京长线科技有限公司)

- Product support: [support@sdnlink.cn](mailto:support@sdnlink.cn)
