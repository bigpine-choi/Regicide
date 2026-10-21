# CPU attribution audit — 2026-10-21

The saved residual is **not a measurement of other visible applications**.
The user's statement that no other apps were open is compatible with these
receipts. Replaying arithmetic does not establish which process caused it.
No source, thresholds, frozen plans or earlier evidence were changed; no
engine, cohort or user process was started, stopped or controlled in this audit.

The guard computes, for each sampled interval,

`residual = max(0, system_busy_delta - observed_owned_process_cpu_delta)`.

Its ownership set consists of registered worker roots and proven descendants,
not the whole experiment. Thus it leaves the coordinator/monitor's own CPU,
other processes, OS work, unseen short-lived experiment children and timing
error mixed together. Calling this number “another app's CPU” was unsupported.

| CPU producer | Treatment in current guard |
| --- | --- |
| Proven worker, engine and descendant Python runtime | Subtracted as owned |
| Threads inside those owned processes, including Sunfish's thread pool and the C++ UCI search thread | Included in each process CPU total; a separate thread PID is not required |
| Coordinator main thread, sampler thread, root replay/hash/JSON work | Remains in residual; coordinator is not a registered worker |
| Parent Codex/tooling processes and OS/background services | Remain in residual; visibility of an application window is irrelevant |
| Own child created and terminated entirely between inventories | Can remain in residual because it never gained an identity/held timer |
| Experiment-triggered work in another service | Cannot be distinguished from unrelated service work by these logs |

Microsoft documents `GetSystemTimes` kernel time as including idle and its
values as the sum over all processors on systems with at most 64 processors.
Therefore `total = delta(kernel + user)` and `busy = total - delta(idle)` are
consistent with this 16-logical-CPU host. The denominator is machine CPU
capacity, not one core or the number of active lanes. The two triggering
2-second denominators correspond to 16.0145 and 16.0876 logical CPUs on average;
there is no observed factor-of-16 normalization mistake.
[GetSystemTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes)

Microsoft also documents process kernel/user times as sums over every thread
in that process. Normal Python background threads are therefore not omitted
from a proven worker merely because they do not appear as child processes.
The frozen Sunfish adapter uses `ThreadPoolExecutor(max_workers=1)`; Regicide's
UCI search uses `std::thread`, which belongs to the same measured process.
[GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)

The partition is still an **estimate**, not an atomic accounting identity:

- `WindowsLoadProvider.snapshot()` reads owned processes sequentially before
  reading system counters; the guard tags the window with a later monotonic
  end time. Process/system readings are neither simultaneous nor recorded with
  separate exact read timestamps. Changing read delays and counter quantization
  can create short residuals. For a fixed owned set, ordinary unweighted deltas
  telescope, so timing error is mainly an endpoint issue rather than an error
  that necessarily accumulates once per sample.
- The 2-second window linearly apportions a boundary interval. It assumes CPU
  activity was uniform within that interval; a burst's exact time is unknown.
- Clipping occurs before summing intervals. Algebraically it adds
  `sum(weight * max(0, owned_delta - busy_delta))` to the unclipped residual.
  This is an upward bias when underflow occurs. It is **zero in both actual
  triggering windows**, so it does not explain those saved threshold crossings.
- Held final timers and exact identities protect observed exiting children,
  but cannot recover a process absent from every inventory. No observed live
  owned process disappeared without a final timer in the two prior diagnoses;
  that check does not prove that every experiment child was ever observed.

The existing resource diagnoses remain valid descriptions of the recorded
policy calculation, with the following attribution limits:

| First-trigger window | Residual CPU seconds | Fraction | Weighted snapshot-body wall time | Active roots |
| --- | ---: | ---: | ---: | ---: |
| 004 main | 6.67159 | 20.8299% | 0.11928 s | 4 |
| 005 preflight | 7.43750 | 23.1156% | 0.08800 s | 2 |

Snapshot-body wall time is **not monitor CPU time**, excludes some JSON/retire
and foreground orchestration work, and must not be subtracted as measured CPU.
The old receipts do not log the coordinator's CPU even though its native timer
is queried while collecting process metadata. There is consequently no measured
historical amount with which to separate monitor/orchestration CPU from the
residual. In 005's final 2 seconds there were seven owned identities, no observed
new owned identity, no retirement event and no old-baseline omission. In 004,
the first boundary interval is 99.699% included; in 005 it is 46.545% included
but has zero recorded residual. Neither observation identifies an application.

For a future, separately preregistered diagnostic, record coordinator process
CPU and compare observed subtree totals against the existing owned Job's
cumulative accounting. Microsoft documents Job total CPU as including active
and terminated associated processes; that offers an independent check on
between-inventory child loss. Record system/process read timestamps and assess
residual uncertainty before interpreting a short spike or selecting a load
policy. These are recommendations only and were not implemented or run here.
[Job accounting](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)

Code pointers: `m2_load_guard.py:148` (sampling sequence), `:192` (held timers),
`:374` (window), `:422` (selected ownership), `:491` (owned deltas), `:503`
(window timestamps); `see_clean_guard.py:143` (process CPU), `:164` (inventory);
`m2_formal.py:480` (coordinator), `:529` (replay), `:561` (root verification).
Source SHA256: guard `39b25242e03faef0dd65708481ea08720eadf62cf163604ba173df26725d5b13`,
runner `6b1b575279e9f14a8ca26934c21ea827f2a5839ebfb4cfb2aa1b77319dcfe6fa`.
