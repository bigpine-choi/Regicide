# main-0040 endpoint setup failure

The confirmed defect is using a global numeric parent PID as a current engine
child relation before proving creation identity or membership in the owned Job.
The current system corroborates a stale-parent explanation. The failed first
inventory and offending child PID were not recorded, so the exact historical
child that triggered error 5 cannot be established from the retained receipts.

The failed cell's preserved `games.json.affinity.transports` identifies Regicide
transport PID 1016, creation ticks `134370565422326671`, launched by worker PID
39556 with creation ticks `134370565414190660`. The candidate's creation was
2026-10-21 11:42:22 UTC. Its frozen executable, literal command and affinity are
recorded. The baseline Sunfish endpoint registered normally. No search was
performed in this failed cell; setup stopped on access denial.

Later complete native inventories still report two numeric parent links to
1016: PID 1396 `csrss.exe` and PID 1612 `wininit.exe`. Independent CIM reports
their births as 2026-10-16 01:55:14.681389 UTC and 01:55:18.268282 UTC. Both
predate the new Regicide identity by about five days and therefore cannot be its
actual children. Native process-query, query-plus-wait, READ_CONTROL and
query-plus-READ_CONTROL opens all return Windows error 5 for both system
processes. Their DACL contents were inaccessible; access denial does not reveal
the precise DACL or protection mechanism. PID 1016 was absent at diagnosis time
and every attempted read-only open returned error 87.

The frozen resolver selects all global `parent_pid == transport_pid` entries,
opens each entry, and only then checks its creation time. This ordering reaches
the inaccessible old system process before it could prove that the parent link
does not belong to the new engine. The first setup-error record omits child PID
and inventory, so identifying PID 1396 as the exact historical trigger would be
an inference, despite its being the first matching current PID in sorted order.

Both preserved preflight and main load logs contain no observation of PID 1016,
1396 or 1612 in their selected process/retained/root receipts. The failing engine
was too short lived to be sampled. Old Job receipts record aggregate CPU and
process counts, not `JobObjectBasicProcessIdList` membership. Final main Job
accounting reports 85 total processes and zero active processes after cleanup.
These records cannot retrospectively prove which children belonged to the Job.
The main result remains stopped at 40/768 games and invalid for comparison.

This exact `wininit.exe`/`csrss.exe` parent-PID reuse pattern is documented by
Microsoft, which recommends Job process membership rather than treating the
global parent field as current ownership. [Microsoft explanation](https://devblogs.microsoft.com/oldnewthing/20150403-00/?p=44313).
The process-opening restrictions are separately documented by Microsoft.
[OpenProcess documentation](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess).

The proposed correction must record two complete calling-Job PID inventories,
verify the worker and transport belong to that Job, and prove that Job limits
do not permit breakaway. A global parent match outside that proven Job can then
be recorded as outside the cohort without trying to open unrelated protected
processes. Within the Job, held PID/creation/image checks remain mandatory;
creation inversion can reject stale edges, while access failure for an actual
cohort candidate remains fatal. Unknown current Job membership or an incomplete
native inventory must not be accepted as evidence of absence.

`receipt-r2.json` preserves the read-only native/CIM evidence and original input
hashes. Frozen identity SHA `9c80fd44...` still matched the current source when
captured. No engine was started, no security privilege or DACL was changed, and
no user process was terminated. The first diagnostic's Windows PowerShell JSON
date serialization error is retained separately in
`receipt-diagnostic-error.json` with its original source hash and source file.
