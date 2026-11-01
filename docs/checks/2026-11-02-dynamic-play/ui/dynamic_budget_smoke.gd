extends SceneTree
const Main = preload("res://scripts/main.gd")
var failed: bool = false
func check(value: bool, message: String) -> void:
    if not value:
        failed = true
        push_error(message)
func position(revision: int, settings: int, ms: int, ply: int) -> Dictionary:
    var moves: Array = []
    moves.resize(ply)
    return {"game_id": "test", "revision": revision, "moves": moves, "engine": {
        "movetime_ms": ms, "settings_revision": settings, "dynamic_play_enabled": true,
        "extra_budget_ms": mini(ms, 2000) if ply < 16 else mini(ms / 2, 750),
        "extra_budget_schedule": [{"until_ply": 16, "budget_ms": mini(ms, 2000)},
                                  {"until_ply": 80, "budget_ms": mini(ms / 2, 750)}],
        "active_extra_budget_ms": null}}
func _initialize() -> void:
    var current: Dictionary = position(6, 0, 1000, 16)
    var late: Dictionary = position(5, 1, 3000, 15)
    late.engine.active_extra_budget_ms = 1000
    var merged: Dictionary = Main.merge_state_versions(current, late)
    check(merged.moves.size() == 16, "late settings rewound the board")
    check(Main.planned_extra_budget(merged) == 750, "late opening settings used the wrong phase")
    check(merged.engine.active_extra_budget_ms == null, "stale active budget replaced completed search")
    merged = Main.merge_state_versions(late, current)
    check(merged.moves.size() == 16 and merged.engine.settings_revision == 1, "late board lost newer settings")
    check(Main.planned_extra_budget(merged) == 750, "late board lost newer budget schedule")
    check(Main.planned_extra_budget(position(80, 0, 1000, 80)) == 0, "late phase must have zero extra budget")
    check(Main.planned_extra_budget({"engine": {"extra_budget_ms": 1000}}) == 1000, "legacy budget changed")
    if not failed:
        print("PASS Godot dynamic budget: 7 checks; independent board/settings versions and legacy fallback")
    quit(1 if failed else 0)
