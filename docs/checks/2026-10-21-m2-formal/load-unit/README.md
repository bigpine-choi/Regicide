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

The initial 24 synthetic tests cover four concurrent roots, final timers after exit,
PID reuse, access failures, counter failures, delayed samples, rolling load,
receipt failures, and cleanup of 40 successive cohorts (120 processes). Exited
children were included in one final receipt, then both retained timer handles
were closed in that revision. The corrected handle lifetime is described below.
An attached root retains its final timer until detach. Repeated
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

## Timer follow-up

The actual formal preflight's attempt 002 stopped with zero completed games
after a 2.0156-second sampling gap. The runner's foreground hashing/auditing
had delayed manual polling. Its failure evidence remains separate and invalid.
The declared 2-second maximum gap has not been increased.

`GuardMonitor` now wraps the existing guard with a shared reentrant lock and a
background sampling timer started after the idle preflight. Worker identity,
attach/detach, manual samples, timer samples and assertions are serialized.
The timer runs while the runner hashes or replays evidence. Thread-start,
sampling, lock-acquisition and bounded-shutdown failures invalidate the result.
A stuck native query retains its own handles until it returns, when its thread
does deferred cleanup; another thread does not close a handle being queried.
The summary adds `timer` state and the log adds timer lifecycle events.

One narrow calculation boundary was also fixed: adjacent manual/timer samples
may read the same quantized Windows CPU counters. Their zero-CPU interval now
preserves wall-time coverage in the rolling window. It contributes zero CPU
ticks to the fraction. A counter that stalls for a full sampling interval still
fails closed. The regression shows repeated 10ms zero-delta intervals keep the
full 2-second window ready and reject a 0.30 nonworker fraction.

Wrapper unit attempt 003 passed **32 tests**: the original 24, seven real-thread
or lifecycle tests, and the quantized-counter regression. It includes a runner
that makes no polling calls for 2.3 seconds while the timer continues sampling,
inflight registration serialization, live query/init/start/close failures, and
bounded stop with deferred cleanup. Attempt 002's initial thread test failed
because its test policy allowed exactly 0.5 seconds, leaving no scheduling
overhead for its own 0.5-second idle sleep. That fixture now uses a 1-second
bound; the formal policy remains 2 seconds. Both test attempts are preserved.

An independent review then identified that a foreground manual sample could
itself get stuck in a native query, preventing the runner from reaching its
cleanup even while the timer reported a failure. After timer startup, all
native sample/identify/attach/detach operations now execute in the sampler's
request queue. The caller has the declared bounded response wait. A timeout
returns control to the runner for its owned-Job cleanup and invalid report;
the sampler keeps ownership of any outstanding query's handles. Assertions
also use the serialized queue. The timer still checks its scheduled deadline
between requests, so foreground hashing/auditing does not set its cadence.

Actor unit attempts 004 and 005 passed **33 tests**, including a main-requested
query that hangs before the first timer deadline. The test verifies the query
runs on `M2LoadGuard`, the caller gets a bounded timeout, and final handles are
closed safely after the query is released. Attempt 005 is the final source
(`7e88a1e4...`) and test (`238c6c0a...`) receipt. Failed and intermediate attempts
remain preserved. The initial idle preflight still uses the existing
synchronous implementation before any workers exist; bounded native response
during that pre-worker phase is outside this wrapper's current guarantee.

The earlier native attempts above certify their recorded pre-wrapper source
hash only. The coordinator records the new wrapper's native smoke separately;
the earlier passes are not presented as native validation of later code.

## Retired process lifetime follow-up

Formal preflight attempt 003 reached two completed preflight games, then
stopped on four first-seen `OpenProcess error 5` process entries. The previous
error rows did not retain their names or parent IDs, so this evidence does not
establish whether the processes were stale retired children or short-lived
helpers first encountered during startup. That failed attempt remains invalid;
the guard's error rows now preserve name, parent, creation and CPU fields for
classification. Unproven access-denied processes still invalidate the run.

An accounted final timer now moves from active owned handles to a retired
tombstone. Its exact process handle and matching native timer cache remain
open until the raw inventory omits the PID in two consecutive snapshots.
Only an owned exact held process object proven exited by its handle, with
matching identity/name/parent and unchanged final CPU, can replace a stale
access-denied entry. Live objects, missing ownership proof, inconsistent
metadata, query failures, or conflicting identities still fail closed. Holding
the process object preserves its PID identity until the handles are closed,
as documented by [Microsoft PROCESS_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-process_information).

Accounted tombstones do not appear repeatedly in the complete retained-timer
array. A PID actually present in raw inventory may produce an owned row with
the same final CPU (zero further CPU delta). Compact lifecycle events identify
retirement, proven stale-entry recovery and handle closure, and each sample
records the current retired-handle count. Cleanup depends on the inventory
absence condition; a permanently listed process is not silently discarded to
impose a memory limit.

Unit attempt 007 passed **39 tests**, covering held exited access-denied
recovery before and after retirement, rejection of live/unproved entries,
two-absence cleanup, fresh identity after closure, conflicting identity while
held, matching-cache-only closure, and 80 successive completed tombstones
without retained-array or handle accumulation after absence. The prior 38-test
attempt 006 remains preserved. At that step the source SHA256 was
`f22ef91446b59b74db101bd7ddb96426fe8a50d52b1fdf6f76bdcbadf6873610`;
test SHA256 is `2e3336dcdffefd7c2375a6eba66e8c013d6f9bb3c72b5efd9bf372187f63af9c`.
The historical guard remains unchanged. The coordinator records the actual
Windows cohort/tombstone convergence smoke separately before freezing the
next preflight attempt. Reducing worker Git helper launches is a separate
runner change and is not evidence that this provider change fixed attempt
003's unclassified first-seen entries.

A subsequent full-path review found that a recovered retired process row
would pass through `M2LoadGuard.sample()`'s retirement callback again. Repeated
retirement is now idempotent only after the same held exited object's identity
and unchanged final CPU are reverified; missing proof or a changed final timer
still fails. No absence counter is reset by the duplicate callback.

Final unit attempt 008 passed **41 tests**. Its added integration test uses the
actual `WindowsLoadProvider` and complete guard sample/persist/retire path with
only OS I/O mocked. The retired stale access-denied row reappears three times
with zero additional owned CPU, then two absent inventories close its handles;
the root's final sample, detach and close retain a valid receipt. A separate
regression rejects a changed final timer on repeated retirement. Final source
SHA256 is `39b25242e03faef0dd65708481ea08720eadf62cf163604ba173df26725d5b13`,
and final test SHA256 is
`b3c47295de02fe6d5b8a9c11caeb092f106293459c1b5f7bac54e7b3370330fa`.
