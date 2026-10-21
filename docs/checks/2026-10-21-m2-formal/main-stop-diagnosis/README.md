# Main 004 resource stop diagnosis

Resources only: no game result files were read, no guard settings/source were
changed, and no process was controlled. The source log's SHA before/after and
the frozen guard/analysis script hashes are in `resource-diagnosis-final.json`.

All 1,258 samples reproduce the recorded system/owned/nonworker CPU arithmetic
exactly. No observed live owned identity disappeared without a final timer;
retirement final CPU records also match. In the last 12 seconds there are no
first-seen owned processes whose creation predates the previous sample and
whose cumulative CPU was therefore left unsubtracted. Clipping underflow is
zero in both examined windows. No external known-engine rows appear there.

| Window before the first failure | Busy CPU seconds | Owned CPU seconds | Residual CPU seconds | Residual / total capacity |
| --- | ---: | ---: | ---: | ---: |
| Preceding 10 seconds, -12 to -2 | 48.23090 | 37.67033 | 10.56057 | 6.6023% |
| Final 2 seconds, -2 to 0 | 11.67108 | 4.99948 | 6.67159 | **20.8299%** |

The first failure is raw line **1439**, `timer` sample at
`2026-10-21T09:27:01.005753+00:00`, failure recorded at `.031205`.
Its 2-second CPU capacity is 32.02895 CPU seconds; the residual exceeds the
declared 20% limit by 0.26580 CPU seconds. The later `closed` sample at line
1442 raises the summary maximum to **21.4382%**; that maximum is not the initial
trigger window. The final triggering 0.41139-second interval alone records
3.93750 busy CPU seconds, 0.59375 owned, and 3.34375 residual.

The captured records support an actual excess of the **measured aggregate
residual**, rather than a recorded startup-baseline omission, final-timer loss,
or per-interval clipping artifact. They cannot identify the responsible task:
CPU timers for nonworker host processes were not captured, and asynchronous
timer sampling/quantization and unobserved transient work remain attribution
limits. The failed load check remains failed under its original policy.

Reproduce from the repository root, using a fresh output filename:

```powershell
.venv-tests\Scripts\python.exe -B docs/checks/2026-10-21-m2-formal/main-stop-diagnosis/diagnose.py --directory build-m2-formal/attempt-004 --output docs/checks/2026-10-21-m2-formal/main-stop-diagnosis/resource-diagnosis-final.json
```
