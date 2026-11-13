# Auto Moniter

**English** | [简体中文](README.zh-CN.md)

Auto Moniter runs coding tasks through a worker, an independent reviewer, and the acceptance checks you configure. Use it from the command line or the local web console.

## Worker, reviewer, and watcher

| Role | Responsibility |
| --- | --- |
| Worker | Implements the assigned change, runs builds and focused tests, and reports completed work, gaps, and evidence. |
| Reviewer | Reads the requirements, code, and existing results; identifies gaps and verifies repairs without editing the project. |
| Watcher / Supervisor | Dispatches work, tracks findings, runs approved acceptance commands, and accepts delivery after the required checks pass. |

New tasks with a reviewer use `review-first`: the reviewer clarifies the requirement, then the worker implements it. Review findings go back to the worker until resolved. A `needs_input` requirement stays undispatched until the missing decision or prerequisite is supplied.

Completion candidates and reported external blockers trigger review. Unfinished work is reviewed periodically according to `--review-every` and the quality policy. Each task finishes when its original requirement has been accepted; continuing with another requirement must be requested explicitly. Reviewers use fresh sessions by default.

## Requirements

| Component | When it is needed |
| --- | --- |
| Linux and Python 3.11+ | Required to run Auto Moniter. |
| Git | Required for local code commits and parallel task worktrees. Configure a commit author before starting. |
| Agent CLIs | Install and authenticate the worker and reviewer you plan to use. Both must be able to run independently. |
| Node.js 22 | Required for JavaScript and inline HTML function checks. Agent installers may require a different Node.js version. |
| Bubblewrap (`bwrap`) | Required for `strict` isolation, which the web console enables for reviewers by default. |
| Project toolchain | Install the target project's build tools, dependencies, and test runners in the task environment. |

The base Python package uses the standard library. The web console needs no frontend build. Additional function parsers are available through the `languages` extra.

## Install

```sh
git clone https://github.com/sdnlink/the_last_km.git
cd the_last_km
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

For function checks in TypeScript/TSX, Go, Rust, Java, C/C++, C#, Kotlin, Swift, Ruby, PHP, Lua, or Bash, install the optional parsers in the same environment:

```sh
python3 -m pip install -e '.[languages]'
```

Activate `.venv` again in each new terminal. Check the installation and available CLIs:

```sh
auto-moniter --version
auto-moniter providers
auto-moniter doctor --cwd /path/to/project
auto-moniter selftest
```

`providers` checks CLI discovery. `doctor` checks local prerequisites. Neither verifies authentication or model access. `selftest` uses local fixtures and makes no model calls.

Configure a Git author in the target repository if one is not already set:

```sh
git -C /path/to/project config user.name "Your Name"
git -C /path/to/project config user.email "you@example.com"
```

## Supported workers and reviewers

The table describes the adapters included in this project. Use the identifier in the first column with `--provider` for the worker or `--reviewer` for the reviewer.

| Agent identifier | CLI executable | Default worker mode | Reviewer support |
| --- | --- | --- | --- |
| `codex` — Codex | `codex` | `full-access` | Yes, `read-only`. |
| `claude` — Claude Code | `claude` | `bypassPermissions` | Yes, `plan`. |
| `grok` — Grok | `grok` | `bypassPermissions` | Yes, `plan`. |
| `antigravity` — Antigravity CLI | `agy` | `bypass` | Yes, `plan`. |
| `gemini` — Gemini CLI | `gemini` | `yolo` | Yes, `plan`. |
| `cursor` — Cursor Agent | `cursor-agent` | `force` | Yes, `plan`. |
| `codebuddy` — CodeBuddy | `codebuddy` | `bypassPermissions` | Yes, `plan`. |
| `copilot` — GitHub Copilot CLI | `copilot` | Automatic execution | Worker only. |
| `hermes` — Hermes | `hermes` | `yolo` | Worker only. |
| `kimi` — Kimi Code CLI | `kimi` | `auto` | Worker only. |
| `glm` — GLM | `claude`, with a separate GLM profile | `bypassPermissions` | Yes, `plan`. |
| `deepseek` — DeepSeek | `claude`, with a separate DeepSeek profile | `bypassPermissions` | Yes, `plan`. |
| `paseo` — Paseo daemon | `paseo` | Depends on the selected backend | Claude backend in `plan` mode only. |
| `command` — Custom command | Your `--exec` template | Defined by the template | Worker only; CLI configuration. |

Copilot, Hermes, Kimi, and CodeBuddy start a new CLI session for each turn; the watcher supplies the current assignment and previous feedback. Other adapters can resume worker sessions where supported. Native reviewers enforce their supported review modes; `strict` isolation adds an OS-level read-only boundary using Bubblewrap.

`auto-moniter providers` lists discovery results on your machine. Install and authenticate each role you select; a discovered executable alone does not confirm that it can reach a model.

## Configure agents

Open **Agent 管理** in the web console and click an agent's name to configure its model, endpoint, authentication, and executable path. You can use an existing CLI installation or install one from that page. Installation progress appears above the agent list.

Complete any required CLI login in a terminal under the same user account that runs Auto Moniter. The reviewer must support a read-only or planning mode. See [agent configuration](console/AGENT_SETUP.md) for the available roles and settings.

Console settings are saved in `<data-dir>/agents.json`. To use those settings from the CLI, set the file's absolute path:

```sh
export AUTO_MONITER_AGENT_SETTINGS=/path/to/console-data/agents.json
```

When this variable is unset, CLI tasks use the agents' existing local configuration. API keys are configured through the agents or console settings.

In the agent configuration dialog, enable the agent and choose its default model. Leave the executable path empty for automatic discovery, or provide an absolute path. Leave an existing saved key empty to retain it; use the clear-key option to remove it. GLM and DeepSeek require their own API-key profiles. Kimi API-key configuration also requires a model ID and the appropriate interface type.

The console launches tasks with these profiles automatically. When running it as a service, authenticate and export any required environment variables for the service user before startup. Installation and CLI detection do not test account quota or model access.

## Use the web console

Start from the repository root:

```sh
./start.sh
```

The web console supports English and Simplified Chinese. It initially follows your browser's language; use the language selector in the top bar to switch. Your choice is remembered across pages and visits. Interface labels, dialogs, status messages, pagination, and times follow the selected language. Task names, requirements, AI output, code, and raw logs stay in their original language.

Open `http://127.0.0.1:8765`. You can also run `python3 -m console`, or `auto-moniter-console` after installation. To choose a port, default project, and external data directory:

```sh
./start.sh --port 9000 --cwd /path/to/project \
  --data-dir ~/.local/state/auto-moniter-console
```

Run `./restart.sh` to restart the console in the background, or start it if it is stopped. For a custom instance, pass the same `--cwd` / `--data-dir`. Existing port and extra scan directories are retained unless overridden. Running tasks continue and task records and queue order are preserved. The script prints the service URL and log path; refresh the browser after it finishes. See the [console guide](console/README.md).

1. Configure the worker and reviewer in **Agent 管理**.
2. Create a task with the project path, requirements, and completion criteria.
3. Scan the project's checks, select the relevant ones, and check their prerequisites. Add custom acceptance commands when needed.
4. Set models, runtime and call budgets, review frequency, and isolation settings, then start the task.
5. Follow execution in **实时协作**. Open task details for requirements, conclusions, Git records, and logs.

The default flow reviews requirements before implementation. Select executable acceptance to run checks, semantic review for review without test execution, or planning to prepare an acceptance plan as a separate task. Function dispatch requires a runnable test plan, which the reviewer can supply without user-written commands.

Use **补充要求 / 调整方向** to update a task. For a resumable stopped task, sending the amendment also attempts to resume execution. Previously saved amendments can be sent with an empty input field. If a budget is exhausted, use the recovery window to increase it explicitly.

Use **追加排队需求** in task details to queue follow-up requirements within the same run. Insert at the tail, head, or before another waiting requirement. The next requirement is reviewed and executed only after the current one passes independent review and watcher acceptance. Pending requests do not enter the current worker prompt. Pause controls, cumulative budgets and recovery remain in effect. Global pages live in the sidebar; task sections use one local tab bar. See the [console guide](console/README.md).

The console listens on loopback. To access a remote instance, open an SSH tunnel:

```sh
ssh -L 8765:127.0.0.1:8765 user@server
```

## Run a CLI task

Replace the project path, agents, and check command with those for your environment:

```sh
auto-moniter run \
  --cwd /path/to/project \
  --provider claude \
  --reviewer codex \
  --workflow review-first \
  --guard \
  --max-runtime 2h \
  --max-agent-calls 100 \
  --check 'python3 -m unittest discover -s tests -q' \
  'Add a health endpoint, including tests for success and failure cases.'
```

The reviewer breaks new review-first tasks into function assignments by default. Use `--no-function-dispatch` for broader assignments. Repeat `--check` to configure multiple acceptance commands. Approved code is committed locally to the task's business branch.

| Option | Use |
| --- | --- |
| `--model`, `--reviewer-model` | Choose each role's model. |
| `--review-every 5` | Review unfinished work after five worker turns. The current turn must finish with a valid conclusion before review starts. |
| `--review-every 0` | Disable turn-based periodic reviews; completion, blockers, and quality checkpoints still trigger review. |
| `--max-rounds N` | Limit worker turns; `0` leaves the turn count unlimited. This is separate from review frequency. |
| `--max-runtime 2h`, `--max-agent-calls 100` | Set cumulative runtime and agent-call budgets. |
| `--turn-timeout 30m`, `--idle-timeout 15m` | Limit a turn's duration and time without CLI output. |
| `--review-isolation strict` | Enforce read-only review with Bubblewrap and a supported native reviewer. The CLI default is `provider`. |
| `--worker-isolation strict --protect tests` | Protect validation files with Bubblewrap; use a run directory outside the target project. |
| `--guard` | Restart the supervisor after an unexpected exit, within the configured restart limit. |

Run `auto-moniter run --help` for all options. From an uninstalled source checkout, use `python3 -m auto_moniter` in place of `auto-moniter`.

### Supply requirements and select models

Read a requirement from a UTF-8 file:

```sh
auto-moniter run --cwd /path/to/project \
  --provider claude --reviewer codex \
  --file requirements.md \
  --check 'python3 -m unittest discover -s tests -q'
```

You can also pipe text into `auto-moniter run` when the requirement argument and `--file` are omitted. Use `--model worker-model-id` and `--reviewer-model reviewer-model-id` to choose models available to each account. An agent identifier selects the adapter; a model ID selects the model within that agent.

### Native CLI modes

Set worker permissions with `--mode` and reviewer permissions with `--reviewer-mode`. For example, run Grok with Claude reviewing:

```sh
auto-moniter run --cwd /path/to/project \
  --provider grok --mode bypass \
  --reviewer claude --reviewer-mode plan \
  --check 'python3 -m unittest discover -s tests -q' \
  'Implement account lockout and test its thresholds and reset behavior.'
```

| Adapter | Worker modes accepted by the adapter |
| --- | --- |
| Codex | `full-access`, `auto`, `read-only`. |
| Claude / Grok | `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`. |
| Gemini | `default`, `auto_edit`, `yolo`, `plan`. |
| Cursor | `default`, `force`, `plan`. |
| Antigravity | `default`, `bypass`, `plan`. |

Common aliases such as `bypass` and `full-access` are translated for the selected backend. Reviewer modes are restricted to native Codex `read-only` or the supported adapter's `plan` mode. Copilot, Hermes, and Kimi use their adapter's worker settings and accept no `--mode` override. See the support table for CodeBuddy and the Claude-based GLM/DeepSeek profiles.

Pass extra CLI arguments with repeated `--extra-arg=VALUE` or `--reviewer-extra-arg=VALUE`. Reviewer overrides that change permissions, configuration, working directory, or session ownership are rejected.

### Run agents through Paseo

Prepare the Paseo daemon and authenticate its backends first. Select the backend explicitly. For a Paseo Claude worker with a native Codex reviewer:

```sh
auto-moniter run --cwd /path/to/project \
  --provider paseo --paseo-backend claude --mode bypass \
  --reviewer codex \
  --check 'python3 -m unittest discover -s tests -q' \
  'Implement the requested API behavior and its regression tests.'
```

To run both roles through Paseo, use Claude for the reviewer:

```sh
auto-moniter run --cwd /path/to/project \
  --provider paseo --paseo-backend codex --mode full-access \
  --reviewer paseo --reviewer-paseo-backend claude \
  --reviewer-mode plan --review-isolation provider \
  --check 'python3 -m unittest discover -s tests -q' \
  'Implement the requested API behavior and its regression tests.'
```

Paseo Claude workers default to `bypass`; Paseo Codex workers default to `full-access`. Paseo Grok workers use their backend's default permissions: omit `--mode` and `--paseo-mode`. Paseo Codex and Grok are available as workers; use a supported native reviewer or Paseo Claude for review. Paseo review requires a fresh session and `provider` isolation. Strict worker isolation also requires a native CLI.

`--paseo-mode` and `--reviewer-paseo-mode` remain available as backend-specific mode options. Use one mode option per role.

### Use a custom worker command

Use a wrapper you provide to translate the task and schema for another agent:

```sh
auto-moniter run --cwd /path/to/project \
  --provider command \
  --exec 'python3 /path/to/agent_wrapper.py --prompt-file {prompt_file} --schema {schema}' \
  --reviewer codex \
  --check 'python3 -m unittest discover -s tests -q' \
  'Implement the requested behavior and report the verification results.'
```

The wrapper must return a structured conclusion matching the supplied schema. Templates support `{prompt}`, `{prompt_file}`, `{cwd}`, `{schema}`, `{session_id}`, and `{model}`; substituted values are shell-quoted. Set `--resume-exec` when the wrapper supports session continuation. Custom commands are available for the worker only.

Use `--workflow legacy --no-review` only when you deliberately want to rely on the worker's conclusion. That mode still checks evidence and configured acceptance commands, and omits independent review.

## Acceptance checks, modules, and quality policy

`--accept` adds written completion criteria. `--check` supplies an executable shell command for the watcher to run in the target project. Add multiple checks separately:

```sh
auto-moniter run --cwd /path/to/project \
  --provider claude --reviewer codex \
  --accept 'Valid requests succeed; invalid requests are rejected with a clear error.' \
  --check 'python3 -m unittest discover -s tests -q' \
  --check 'git diff --check' \
  --check-timeout 10m \
  'Add the requested API endpoint.'
```

Supply the actual behavioral checks needed by the task. Environment descriptions, server addresses, and written instructions belong in requirements or configuration; enter runnable commands in the check fields. A formatting check alone does not establish that a feature works.

The web console can scan existing test entry points and check their prerequisites. Scanning does not run the tests. To add checks to an existing task, stop it and use **恢复执行 → 追加验收命令**. Existing commands and check IDs are retained.

### Fixed modules

Save a module list as `modules.json`. Use stable IDs and add dependencies when one module requires another:

```json
[
  {
    "id": "API",
    "title": "API behavior",
    "acceptance": ["Valid requests succeed and invalid requests are rejected"],
    "files": ["app/api.py", "tests/test_api.py"],
    "checks": ["python3 -m unittest tests.test_api -q"]
  },
  {
    "id": "FINAL",
    "title": "Integration acceptance",
    "acceptance": ["End-to-end requests satisfy the original requirement"],
    "depends_on": ["API"]
  }
]
```

Load it with `--modules modules.json`. Module definitions remain fixed during the run, and delivery requires acceptance of every applicable module and its dependencies.

### Function dispatch and quality settings

With function dispatch enabled, the reviewer selects a named function, its allowed files, acceptance criterion IDs, approved check IDs, and any new focused test files. The watcher checks scope and runs the selected regressions before advancing. Use `--no-function-dispatch` when the task needs broader assignments. Additional parser requirements are listed under installation.

New web tasks generate a complete function plan before serial dispatch. The queue page paginates all assignments and filters current, waiting and accepted work; each entry exposes its full scope, dependencies and verification requirements. CLI users can enable this with `--function-dispatch --full-function-plan`. The plan lives in `state.json.function_plan`; resuming preserves accepted and current work. Existing tasks must stop and resume with the updated supervisor to complete their remaining plan at a safe review boundary. A plan is not acceptance evidence.

The reviewer designs positive, negative and boundary assertions; the worker implements them; the watcher derives local commands from declared `test_files`. Supported runners are Python unittest, Node 22 and Cargo integration tests with locked offline dependencies. `auto:N` refers to the Nth declared test file. Supported declared regressions run even when the reviewer selects only existing check IDs, and accepted regressions run again at final delivery and after resume. Empty or entirely skipped suites fail acceptance. Deployment, real devices, credentials and other environments still need explicitly configured entry points. See [acceptance responsibilities](ACCEPTANCE_GUIDE.md#reviewerworker-和-watcher-的验收职责).

Save optional settings in `quality-policy.json`:

```json
{
  "protect_tests": true,
  "verified_progress": true,
  "no_progress_limit": 12,
  "review_interval_s": 600,
  "review_changed_files": 20,
  "blocked_review_limit": 3,
  "requirements": [],
  "validation_paths": [],
  "review_tools": "provider",
  "max_tokens": 0,
  "max_cost_usd": 0
}
```

Load it with `--quality-policy quality-policy.json`. This example protects existing validation assets, requires verified progress, and adds review checkpoints. In the console, enter the same object in the advanced quality settings. Use `requirements` to map fixed modules to evidence, `validation_paths` to protect additional files, and approved `integration_scopes` when functions in multiple files must change together. Field details and examples are in [Quality policy](QUALITY_POLICY.md).

Token and cost limits require usage reported by the selected backend. Time values use seconds or a single `s`, `m`, `h`, or `d` suffix, such as `30m`; byte limits use integers.

## Develop one project in parallel

In the console's task form, enable **同工程并行开发** and list the files or directories each task owns, including new tests. End directory names with `/`. Split overlapping ownership or declare an earlier task's full ID as a prerequisite.

Each task starts from committed code in a separate Git worktree on an `auto-moniter-task/<task-id>` branch. Prepare the dependencies that worktree needs and use project-relative acceptance commands.

The watcher observes shared inputs and peer changes, usually every three seconds. When inputs change, it invalidates stale conclusions and asks the reviewer to refresh the worker's assignment. Declared prerequisites are merged after final watcher acceptance. Merge conflicts preserve work and go back for review. Each task delivers to its own branch; merge it into your main branch separately.

## Inspect, pause, and resume

Each run prints its record directory. Use that path to inspect or control it:

```sh
auto-moniter status /path/to/run
auto-moniter watch /path/to/run --once
auto-moniter pause /path/to/run
auto-moniter continue /path/to/run
auto-moniter stop /path/to/run
```

Pause takes effect at a turn boundary. To resume a stopped or interrupted run, use the original project path and role configuration, including any model, backend, mode, or isolation options:

```sh
auto-moniter run --resume \
  --run-dir /path/to/run \
  --cwd /path/to/project \
  --provider claude --reviewer codex
```

Resume retains requirements, checks, saved work, and cumulative usage. Resolve the reported blocker or increase the applicable budget before resuming. For a parallel task, use its worktree path as `--cwd`.

An approved requirement and pending function assignment are reused on resume. If requirement review stopped on a blocker or invalid plan, the reviewer continues from its saved analysis and checks the outstanding issue, new instructions, and changed inputs. Starting a fresh reviewer session does not restart a full repository review. Cached analysis supplies context; a revised plan still passes the dispatch checks before the worker runs.

After a worker submits a candidate, the watcher runs the assignment checks and saves their results before independent review. The reviewer receives the exact candidate, check results, and log paths. If the reviewer accepts and mandatory checks passed, the watcher verifies the version and check receipt, commits that assignment locally, then dispatches the next item. It does not run another semantic review or repeat successful checks after approval. A function commit does not mark the whole module or requirement complete. Only the reviewer can close audit findings.

If checks reveal an implementation defect, the reviewer returns a concrete, bounded repair to the worker. If the reviewer accepts despite a mandatory check failure, the run stops with `action_required` rather than repeating implementation or skipping the failed gate. On resume, valid successful receipts and approvals for the same candidate, assignment, configuration, and instructions are reused; an interrupted commit can finish without tests or model calls. Repaired checks that produce new results require review of those results, but not another worker submission. Earlier attempts and logs are archived before rechecking. Older checkpoints without pre-review receipts keep the worker candidate and obtain check results followed by a fresh review.

Module delivery advances only the modules jointly accepted by the worker and reviewer. A module acceptance uses the module gate even when the worker claimed full completion. If approval lacks required evidence citations, it is invalidated; resuming requests a corrected review while preserving successful checks and the submitted candidate.

In the console, stop the task before using **临时调整角色** to change a supported native worker, reviewer, or model, then resume it. The original requirement and cumulative usage remain attached to the task. Paseo, custom commands, and configurations with incompatible extra arguments require a separate follow-up task.

For authentication errors, repair the selected CLI login or profile before resuming. For missing build or test dependencies, prepare them in the task environment. A `needs_input` requirement needs the reported decision or evidence before implementation can start. Check `report.json`, role logs, and acceptance results for the specific reason.

## Data locations and maintenance

| Setting | Default or purpose |
| --- | --- |
| CLI state directory | `$XDG_STATE_HOME/auto-moniter/`, or `~/.local/state/auto-moniter/` when unset. |
| `AUTO_MONITER_STATE_DIR` | Override the CLI state and managed worktree root. |
| `--run-dir` | Choose a CLI task's record directory. |
| Console `--data-dir` | Defaults to `<cwd>/.auto-moniter/console`; contains agent settings, task definitions, and console-launched runs. |
| Console `--runs-dir` | Add a location to scan for existing runs; repeat as needed. |

Each run stores its original requirement in `requirement.md`, its checkpoint in `state.json`, its final status in `report.json`, and role output in `NNN-worker.log` / `NNN-reviewer.log`. Executed acceptance checks also have logs and result records. Keep these files when diagnosing or resuming a run.

Inspect storage and preview cleanup:

```sh
auto-moniter runs --cwd /path/to/project
auto-moniter cleanup --cwd /path/to/project --older-than 7d --keep-runs 5
auto-moniter history list --cwd /path/to/project
```

Add `--apply` to cleanup after reviewing the selection. `--keep-rounds` controls retained round details; `--max-run-bytes`, `--max-history-bytes`, and `--max-agent-state-bytes` set storage limits. Configuring a zero limit disables the corresponding budget where supported.

Review-first tasks publish approved code locally to the checked-out business branch and keep audit records separately. The watcher does not push to a remote. Parallel tasks publish to their task branches. Existing worktree locks prevent two tasks from writing to the same checkout.

See [operations](OPERATIONS.md) for deployment and recovery, the [CLI reference](docs/cli-reference.md) for the full option list, the [acceptance guide](ACCEPTANCE_GUIDE.md) for check selection, and the [console guide](console/README.md) for UI controls.
