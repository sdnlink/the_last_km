# Browser evidence (CYCLE-1)

Operator preserved original headless Chrome artifacts into the task run directory, then this worker copied them into the project tree so independent reviewers can verify without host `/tmp` access.

## Provenance

- Task id: `cda15a5c834f46059d2b5e579b6db8d0`
- Run source (read-only): `.auto-moniter/console/runs/cda15a5c834f46059d2b5e579b6db8d0/browser-evidence/`
- Original host paths (may be gone; hashes still match):
  - `/tmp/auto-selfhost-web-xb2Z7C/` — create form before-submit and task-created screenshots
  - `/tmp/auto-selfhost-web-caIaRt/` — role-switch and resumed screenshots
- `manifest.json` lists original path, byte size, and SHA-256 for each PNG.
- This directory is a byte-identical copy of the run-side evidence; SHA-256 of each PNG matches the manifest.

## Files

| File | Purpose |
| --- | --- |
| `before-submit.png` | Create form filled before submit |
| `task-created.png` | Task page after browser create |
| `role-switch.png` | Role/model switch dialog in use |
| `resumed.png` | Task after role switch applied / resume controls |
| `auto-selfhost-web-current.json` | Create metadata (title, run_dir, created_via) |
| `auto-selfhost-role-switch.json` | Role-switch evidence pointer |
| `manifest.json` | Operator SHA-256 inventory |

## Verification performed by worker

```text
sha256sum before-submit.png   # a43b1f3225980d5cd4d053152faa8a2da57691781b4f9d9965d18ff92f09213e
sha256sum task-created.png    # 4faf8f1f57d2f0f9fa033b29bcd1fd91ba854214103a8e1f9eb53ce339aaeadb
sha256sum role-switch.png     # 11b5f9d03800fe810d063d6fb0e44025ddcc4e5c70a28e2e4592986c0d922388
sha256sum resumed.png         # ca0f97a90ce8a00c6c04260a64dc55a66e2a1bb16cb8b0868182310d29ee1eb0
```

All four digests match `manifest.json`. Temporary HTTP fixture tests remain separate and do not replace this create evidence.

## Limits

- Screenshots and metadata prove form create and later role-switch UI; full network HAR is not present.
- Host `/tmp` originals may no longer exist; project and RUN copies are the durable sources.
- This is not watcher approval or production publish evidence.
