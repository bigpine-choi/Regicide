# Option correction

The initial JSON files directly in this folder used the native UCI default
`UsePieceActivity=false`. The production HumanGame explicitly sets it to
`true`. The parent agent was notified before using the initial numbers as
production-equivalent evidence. Those initial files are retained only as
controls and must not support claims about production candidate discovery.

Corrected production-equivalent results are exclusively under
`activity-enabled/`. Their scripts explicitly enable `UsePieceActivity` and
retain the full recorded move history. The files with `-default-control.py`
names preserve source snapshots from before the correction; they are archival
snapshots, not the entry points for current production probes.
