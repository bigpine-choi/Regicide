Temporary archive fixtures: 11 checks passed. No actual attempt was archived and no engine was launched.

Reproduce from the repository root:

```powershell
.venv-tests\Scripts\python.exe -B docs/checks/2026-10-21-m2-job-accounting/archive-unit/fixture-tests.py
```

`report.json` records the tested helper SHA256 and scope. Fixtures cover deterministic gzip, the strict 100000-byte boundary, original/preserved checksums and sizes, frozen-source byte preservation, compressed phase summaries, executable/model exclusion, destination/collision refusal, input mutation and frozen-source tampering.

After the experiment and its writers have stopped, archive using:

```powershell
.venv-tests\Scripts\python.exe -B scripts/archive_m2_job_attempt.py --attempt attempt-001
```

The helper refuses any existing destination. Interrupted archives retain `archive-incomplete.json`; they are not automatically overwritten or removed. Engine/model payloads are excluded while their SHA256/size and frozen profile metadata remain in the manifest. Frozen harness copies come from the attempt directory, with no substitution from current workspace files.
