# Native endpoint identity verification

`receipt-final.json` is the completed verification using the formal harness's
default process creation flags. It records source hashes, frozen literal engine
commands, two completed parent inventories, held process creation/image proofs,
separate CPU reads and cleanup. This is a functional accounting check, with zero
formal games and no claim of load isolation or playing strength.

- Sunfish: transport PID 32544 was the frozen venv launcher; the unique direct
  frozen base-Python endpoint was PID 18740. One requested 100 ms search returned
  legal move `f2f4`. Endpoint CPU was 0.078125 s and response elapsed time was
  0.08176120003918186 s; launcher CPU increased by zero ticks over that interval.
  Independent held `GetProcessTimes` queries bracketed the meter's raw counters
  in the expected order. These values are descriptive, with native quantization;
  no minimum CPU ratio or exact scheduler waiting-time assertion is made.
- Regicide: PID 18116 was both transport and frozen native endpoint.
- Stockfish: PID 21212 was both transport and frozen native endpoint.
- Each exact held process was proven exited after graceful UCI quit. A later
  complete Toolhelp inventory found all four PIDs absent. No unrelated process
  was terminated, configured or assigned to a Job.

The earlier `receipt.json` and `receipt-inventory.json` are retained failures.
Their probes added `CREATE_NO_WINDOW`, unlike the formal harness. The observed
Sunfish launcher then had two direct children, so the strict unique-child
resolver correctly rejected it. `receipt-children.json` subsequently identified
those children as a frozen base Python process and the system console host.
`receipt-children-default.json` used the formal default creation flags and
observed just the unique frozen base-Python child. Every probe's original source
and receipt is retained; neither failed run is counted as successful.

The final resolver permits an optional explicitly frozen console helper only
when the caller supplies its path and SHA. The formal default and final native
probe supply no such exception. Helper variants and the child-free Sunfish
direct variant have offline fixtures; the final native Sunfish check exercised
the unique-base-child variant. Unknown children, duplicated endpoints or helpers,
unexpected grandchildren, failed access, changed identity or image mismatches
remain fatal. Query plus synchronization access (0x101000) grants read/wait
rights, with no process mutation rights. Liveness uses the nonsignaled held
process object; running-process exit timestamps are undefined and ignored.

This resolves endpoint selection at launch. Inventories are sampled, not an
atomic guarantee against future descendants. The CPU meter reopens the selected
PID with its exact creation identity and measures all threads of that process;
its descendants are excluded. Launcher and helper CPU remains within the owned
experiment Job's totals. Engine-level CPU measurements do not identify an
individual background application or certify homogeneous CPU speed, thermal
state or per-move scheduling fairness.

The initial strict resolver snapshot is retained at
`source-initial/m2_engine_identity.py`. Its bytes were checked against SHA256
`c8e41e9ae8862a8e52afb2bf0e3e78a2a2fd441744a59cc38fb77a760f449bac`,
exactly matching all four early probe receipts. The final native receipt binds
the later resolver SHA256
`9c80fd44a3a405ede42c5ceb72b3bdb6c7cebc1d1528ed21b0b40409152ebe9d`.
