# M2 load guard validation

`scripts/m2_load_guard.py` adds the formal experiment's cohort monitor without
changing the historical `see_clean_guard.py` or the pilot harness. It has no
process termination operation. The runner owns worker containment and cleanup.

The policy is frozen before an experiment: 0.5-second target sampling, a
2-second maximum sample gap/duration, a complete rolling 2-second host CPU
window, and a maximum aggregate nonworker CPU fraction of 0.20. Named external
engine identities must remain unchanged with **zero observed CPU tick increase**.
All native identity/timer errors invalidate the receipt. Host kernel time
includes idle time and is counted once. Worker CPU is subtracted only after a
PID/creation-time ancestry proof; old first-seen process CPU has no guessed
historical subtraction.

The 24 synthetic tests cover four concurrent roots, final timers after exit,
PID reuse, access failures, counter failures, delayed samples, rolling load,
receipt failures, and cleanup of 40 successive cohorts (120 processes). Exited
children are included in one final receipt, then both retained timer handles
are closed. An attached root retains its final timer until detach. Repeated
frames therefore contain current/just-completed processes, not every prior
worker. An active child after a detached root invalidates the run.

Native attempt 001 passed both separately recorded probes:

- Positive: 5.013 seconds idle, then 4 newly created Python roots and their sleep
  children, final-timer samples, detach and close. All own workers exited;
  `valid_for_load_check=true`, remaining owned handles 0, maximum rolling
  nonworker fraction 0.08646, maximum sample gap 0.67494 seconds.
- Negative: a private copy of the existing engine (`edd950fb...`) reached idle
  UCI readiness before the 2.014-second baseline. `go infinite` then increased
  its external CPU counter by 5,156,250 ticks; `assert_clean()` rejected it with
  `external_cpu`. Its receipt is deliberately invalid. Only that new canary
  received stop/quit and belonged to the cleanup Job. The local executable
  copy is ignored; its SHA and raw UCI/CPU records are retained.

Native attempt 002 repeated both probes successfully after adding fallback
cleanup for a newly created probe process if Job assignment itself fails.
The guard and unit-test source hashes did not change. Attempt 001's exact
helper source is retained as `native-attempt-001/probe-source.py`; the current
helper hash matches attempt 002. The fallback can terminate only a `Popen`
object created by this helper, and was not needed in either successful run.

Commands from the repository root (use fresh output directories):

```powershell
.venv-tests\Scripts\python.exe docs/checks/2026-10-21-m2-formal/load-unit/unit_receipt.py --output docs/checks/2026-10-21-m2-formal/load-unit/unit-attempt-001
.venv-tests\Scripts\python.exe docs/checks/2026-10-21-m2-formal/load-unit/native_probe.py --output docs/checks/2026-10-21-m2-formal/load-unit/native-attempt-001
```

These are sampled load checks, not proof of an otherwise idle machine, thermal
stability, or isolation from unknown/transient work between samples. The host
fraction includes this monitor and other nonworker development tools. They do
not establish engine speed or playing strength. The native probes ran before
the formal match, after the preparation training workload had stopped.
