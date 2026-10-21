# M2 warning-only residual CPU and actual endpoint accounting

2026-10-21 KST. The user authorized recording residual CPU above 20% as a
warning and assessing each engine's actual CPU time and response latency. The
new [plan](../../experiments/2026-10-21-m2-engine-cpu-plan.md) preserves the
earlier gameplay, resources, opponents, models, opening banks and deadlines.
Historical stopped attempts retain their original validity and evidence.

## Preparation

- `m2_formal_v4.py` freezes 23 harness sources. Earlier v1/v2/v3 sources remain
  unchanged. `m2_warning_load_guard.py` and its independent raw receipt checker
  record warning windows and episodes; native accounting/identity/sampling,
  known external engine CPU, move legality and response errors remain fatal.
- `m2_engine_cpu.py` retains query-only process handles and raw all-thread
  `GetProcessTimes` counters before/after each actual search. Failed calls also
  retain partial receipts. `m2_engine_identity.py` binds the actual compute
  endpoint to frozen image/script/runtime, PID and creation identity.
- `m2_engine_tracking.py` binds receipts to game, role, FEN, move and search
  order, flushes a separate raw stream, closes all held handles even if a log
  write fails, then records final process counters. `m2_engine_receipts.py`
  independently recalculates scalar arithmetic and descriptive distributions.
- The callable and metering envelope retain the requested time * 2 + 0.25 s
  deadline. The older outer comparison time also includes post-response log
  serialization/flush and remains diagnostic. Zero CPU deltas and CPU/wall
  ratios above one are allowed. No CPU, NPS or cross-engine speed floor is used.
- [Native meter evidence](native-meter/README.md) checks counters against
  separately declared Windows calls, short/long workloads and handle cleanup.
  [Native endpoint evidence](native-endpoint/README.md) verifies actual Sunfish,
  Regicide and Stockfish identities, including a legal 100 ms Sunfish response.
  Failed diagnostic probes and their original sources are preserved.
- `run_regression.py` saves offline test results and source hashes. It excludes
  the already executed native tests. `peer-plan-preparation-r3.json` verifies
  unchanged conditions and references; `peer-tool-preparation.json` records
  independent auditor fixture checks. These are preparation evidence, not
  formal games or playing-strength results.
- `preservation-before.json` verifies unchanged native sources/binary and both
  original development checkouts, including their preexisting local changes.

## Execution and interpretation

Attempt 001 passed the actual 24-game preflight, including seven warning
windows and a peak residual of 28.793057%. Both audits passed. Main stopped
at 40/768 games on endpoint setup access error before that pair's first move.
It remains invalid for comparison. [Outcome and diagnostic limits](../../experiments/2026-10-21-m2-engine-cpu-attempt-001-outcome.md)
and the original archive preserve the stopped run.

The v5 correction adds mandatory calling-Job membership and held creation
ordering before treating a numeric parent link as a current engine child.
Global outside-Job matches and positively proven older relations are recorded
as excluded; unknown actual children and incomplete scope still fail. A new
28-source harness retains all 23 earlier source bindings. See the
[restart plan](../../experiments/2026-10-21-m2-engine-cpu-restart-001.md),
[native Job evidence](native-job-scope/README.md) and independent native scope
recalculation. Previous plans, source versions and results remain intact.

Attempt 002 passed another actual 24-game preflight, including 19 warning
windows and a peak residual of 47.758807%. Both audits passed. Its main run
stopped at 194/768 games because the runtime load sampler still followed global
numeric parent links before checking Job ownership. A stale link to a protected
Windows process caused an access error. The recorded process creation times
predate the experiment launcher; this was a process-discovery defect, not proof
of user activity. The incomplete run remains invalid. See the
[attempt 002 outcome](../../experiments/2026-10-21-m2-engine-cpu-attempt-002-outcome.md).

The v6 correction applies explicit owned-Job membership before process queries
in the runtime sampler as well. Global named external engines remain visible
and their CPU use remains fatal. Raw Job lists on both sides of the inventory,
same-handle membership evidence, held exit evidence, excluded parent matches
and bounded birth/exit races are retained and independently recalculated. A
process born or ending between inventories without an observed identity is
not assigned an invented identity; its CPU remains in total Job accounting.
Persistent unobserved members and real access/API failures remain fatal.
The new 33-source harness preserves every earlier source binding. See the
[second restart plan](../../experiments/2026-10-21-m2-engine-cpu-restart-002.md).
Offline v6 regressions passed: provider 19, warning/scope receipts 33, formal
integration 27, and independent auditor fixtures 12. Execution receipts
distinguish direct saved output from reconstructed command context and
post-execution hashes. [Native runtime evidence](native-process-scope/README.md)
checks protected-process exclusion, actual owned-engine membership and an
intentional external-engine CPU failure with complete diagnostic cleanup.
The first positive diagnostic driver's tuple/JSON policy mismatch and its
read-only correction are both preserved; the CPU workload was not rerun.

Attempt 003 completed its fresh 24-game preflight in 595.218 seconds with
product and independent audits PASS. It retained 2,213 search receipts,
2,501 mainline plies, 16,171 PV plies and 2,340 runtime ownership scopes.
Fifteen warning windows in three episodes peaked at 32.593519%; they did not
stop or invalidate the run. Final owned Job active processes were zero.

Before this attempt's main phase started, the user explicitly reduced the
main target from 768 to 96 games to obtain a quick descriptive baseline.
The old 768-game main is not started. The new
[96-game plan](../../experiments/2026-10-21-m2-baseline96-plan.md) fixes the
original bank's first four opening families, retaining all three candidates,
both opponents, both thinking times and paired colors. A small adapter reuses
the unchanged v6 worker and raw validators; its nine synthetic checks passed.
The completed preflight is referenced with exact audit/input/source hashes,
not copied or presented as newly played games. New raw main output goes to
`build-m2-baseline96/run-001`. Old stopped games and preflight scores are
excluded from the 96 main results.

Future change comparisons follow the user's
[short experiment policy](../../EXPERIMENT_POLICY.md): prioritize engine code
and learning, compare one change against its baseline in a short paired match,
and explain cost before expanding the sample.

This is a local external baseline with a previously exposed fixed authored
opening bank. CPU time is consumed process CPU, not an exact measurement of
scheduler waiting time. Sampled process inventories cannot prove the absence
of all future descendants; total Job CPU includes launchers and short-lived
owned work. No complete OS/thermal isolation, causal evaluator improvement,
universal Elo, world ranking or automatic model promotion is claimed.
