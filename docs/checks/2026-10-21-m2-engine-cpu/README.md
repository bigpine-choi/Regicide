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

The new raw attempt directory is `build-m2-engine-cpu/attempt-001`. A complete
24-game preflight must pass product and independent audits before 768 main
games start. Each phase retains its own PGN, per-search CPU stream, metadata,
raw Job/coordinator/system CPU samples, warning counts, final owned Job state
and audits. The old partial games and the new preflight/main are never pooled.

This is a local external baseline with a previously exposed fixed authored
opening bank. CPU time is consumed process CPU, not an exact measurement of
scheduler waiting time. Sampled process inventories cannot prove the absence
of all future descendants; total Job CPU includes launchers and short-lived
owned work. No complete OS/thermal isolation, causal evaluator improvement,
universal Elo, world ranking or automatic model promotion is claimed.
