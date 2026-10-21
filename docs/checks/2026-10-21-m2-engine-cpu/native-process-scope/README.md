# Native process ownership checks, 2026-10-21

Two bounded cases used newly created diagnostic processes. No formal matches
were played, and these checks make no playing-strength claim. Product and
runtime sources were not modified during the diagnostics.

## Owned burst and protected outside PPID metadata

Command: `.venv-tests/Scripts/python.exe -B docs/checks/2026-10-21-m2-engine-cpu/native-process-scope/probe.py`

The actual owned-burst case completed with a healthy guard: 20 snapshots/scopes,
two gated Job workers, eight legal diagnostic moves from short fresh engines,
and 9,062,500 observed whole-Job CPU delta ticks. Synthetic parent PID changes
were applied only to Toolhelp metadata for outside PIDs 4, 1396, 1612 and 34924;
the recorded OpenProcess trace contains no opens for these targets. This is a
controlled metadata injection, not a recreation of the historical failure.

The original diagnostic driver then marked the case `FAIL_RAW_AUDIT`: it passed
an in-memory tuple-valued policy to a validator expecting serialized JSON arrays.
The original driver, failure result and raw files remain unchanged in
`attempt-001/`. Reading the saved JSON summary, as the formal runner does,
produced `PASS` / `valid_for_load_check=true`; this read-only correction is in
`attempt-001/owned-burst/replay-correction.json`. No positive CPU load was rerun.
The eight short engines exited between samples, so this case alone does not
demonstrate a sampled owned engine identity. The next case supplies that check.

## Owned idle engine plus external CPU negative case

Command: `.venv-tests/Scripts/python.exe -B docs/checks/2026-10-21-m2-engine-cpu/native-process-scope/continue_negative.py`

The separate continuation driver runs only the previously unexecuted negative
case. Its own gated worker holds one idle Regicide descendant without requesting
searches. Regicide PID 19012 has four actual positive Job membership reads.
Another newly created Regicide, PID 36932, is outside the Job and newer than the
worker. Its Toolhelp parent metadata is synthetically changed to the worker PID.
It remains external; a single legal 100ms requested search increases its CPU by
937,500 ticks and triggers the expected `external_cpu` hard failure.

`attempt-002/receipt.json` is `PASS_EXPECTED_NEGATIVE`; the case receipt is
`PASS_EXPECTED_EXTERNAL_CPU_FAILURE`. Independent raw replay correctly returns
`INVALID` / `valid_for_load_check=false` for this intentional external-CPU run.
Its arithmetic records 17 scopes, 17 accepted samples and the same 937,500
external CPU ticks. The original outside parent row and each synthetic change
are preserved separately in the case receipt.

Both cases ended with Job active processes zero, unknown owned processes zero
and the Job closed. The outside diagnostic engine quit normally; an independent
held query/synchronize handle returned wait status zero, verified its creation
identity and was closed. The continuation records source hashes before/after
and equality. Final data: `attempt-002/external-negative/{receipt.json,
load-summary.json,load.jsonl}`. An expected failing guard cannot detach identities
as a clean successful run would; the final native Job/held-exit proofs establish
cleanup separately. No unrelated process was terminated or reconfigured.

## Offline unit evidence

`provider-unit19-tool-output.log` preserves the actual previously observed tool
output: 19 tests passed in 0.007 seconds, exit zero, tool chunk `0351f4`.
`provider-unit19-evidence.json` distinguishes the reconstructed command from the
original saved output: the exact command string and original UTC timestamp were
not retained with that tool result. Its source hashes were measured afterward,
not before or during that unit run. No unit test rerun was used to replace the
original observation.

Final post-test hashes:

- `scripts/m2_job_process_scope.py`: `5cc98087d2b630127ed86e5a3b49b4939d6025f6b02b003b2fa4b205cb9f6d1b`
- `tests/m2_job_process_scope_tests.py`: `ad08d1d404838ac394c5d329c274dcac28600202d4e18c6a5017e080a8df9872`

All native diagnostic work is finished. Further formal execution belongs to the
coordinator after freezing its new plan and source set.
