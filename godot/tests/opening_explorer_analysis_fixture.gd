extends "res://scripts/analysis_mode.gd"
## Retain shipped opening/context lifecycle while replacing external transport.

const ExplorerFixture = preload("res://tests/opening_explorer_fixture.gd")
var started_fen: String = ""


func _build_ui() -> void:
	super._build_ui()
	var parent := opening_explorer.get_parent()
	var index := opening_explorer.get_index()
	opening_explorer.free()
	opening_explorer = ExplorerFixture.new()
	opening_explorer.explore_position.connect(_on_explorer_position)
	parent.add_child(opening_explorer)
	parent.move_child(opening_explorer, index)


func _refresh() -> void:
	_state_ready = true


func _start_analysis() -> void:
	started_fen = source_fen
