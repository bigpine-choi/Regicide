# Real helper budget and cancellation check

Run on 2026-11-02 KST with the preserved baseline engine from commit
`e3bb33cb91e235b6e78f0252b0907be34d346304`, using the changed Python live-evaluation
pipeline. Binary SHA-256 is recorded in `real-helper-result.json`.

Reproduce from the repository root:

```powershell
.venv-tests/Scripts/python.exe build/evaluation-refinement/real_helper_check.py
```

The script uses an isolated temporary game directory, forbids opponent searches,
and launches only its own two baseline evaluation helpers. It starts no HTTP
server or GUI. Polling has a 45-second deadline; this run completed in about six
seconds. The helper processes exited and temporary records were removed.

Observed results:

- The first observation after HumanGame construction was at 1,244ms: the same
  initial FEN was still `searching`, with completed depth 7 and +31cp for White.
- The evaluation completed at 3,112ms with depth 8 and 0cp. Its captured budget
  remained 3,000ms. The opponent and ranked-hint settings remained 1,000ms.
- During a second live helper search, submitting `e2e4` returned in 32.7ms.
  The cancelled helper process had exited by 53.0ms after submission.
- Twenty polls after cancellation observed the new FEN with no score and no old
  `source_fen`; no stale result appeared. No opponent search was run.

`first_score_elapsed_ms` in the JSON means the first score *observed after game
construction*, not latency to the engine's first output. Earlier shallow results
were not sampled during construction. Fake-stream regression tests separately
verify immediate publication of complete score packets and later refinements.

This checks backend integration and budget/cancellation behavior only. It is not
a strength comparison, an assertion that every position reaches an extra depth,
or a GUI verification. Raw stdout is in `real-helper-run.txt`; numeric snapshots,
binary/source hashes, timestamps and platform are in `real-helper-result.json`.
