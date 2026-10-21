# Restart 005 preflight resource diagnosis

Only resource receipts were read. No game results were read, no engine/cohort
was launched, and no guard, runner or plan settings were changed.

All **378** raw samples reproduce system/owned/nonworker CPU arithmetic exactly.
No observed live owned identity disappears without a final timer, and retirement
CPU records match. The last 12 seconds contain no first-seen owned process with
an old creation time whose CPU had to be left unsubtracted, no clipping
underflow, and no external known-engine rows.

| Window before first failure | Busy CPU seconds | Owned CPU seconds | Residual CPU seconds | Residual fraction |
| --- | ---: | ---: | ---: | ---: |
| Preceding 10 seconds (-12 to -2) | 29.58969 | 19.92272 | 9.66697 | 6.0433% |
| Triggering 2 seconds (-2 to 0) | 11.42375 | 3.98625 | 7.43750 | **23.1156%** |

The first trigger is raw line **393**, timer sample
`2026-10-21T09:44:29.765080+00:00` (failure recorded `.775321`). Its 2-second
capacity is 32.17527 CPU seconds; residual CPU exceeds the unchanged 20% limit
by 1.00245 CPU seconds. The later closed sample at line **396**, `.775598`,
raises the summary maximum to **23.6769%**. That maximum is not the first trigger.

The captured evidence supports excess in the measured aggregate residual,
without a recorded startup-baseline omission, final-timer loss or clipping
explanation. It cannot identify a responsible process: nonworker per-process
CPU was not captured, and asynchronous sampling, quantization and transient
work remain attribution limits. The load check remains failed under its
original policy; this diagnosis does not authorize or start another attempt.

`restart-005-resource-diagnosis.json` contains raw-log before/after hashes,
source hashes, interval witnesses and the first-trigger/closed distinction.
The existing `main-stop-diagnosis/diagnose.py` was used unchanged via `runpy`:
its parser namespace received a directory adapter mapping `/ main` to
`attempt-005/preflight`, while `/ harness` still resolves to attempt 005's
frozen harness. The JSON records this adapter and confirms current/frozen guard
and runner hashes match. Only this JSON and this note were created for 005.
