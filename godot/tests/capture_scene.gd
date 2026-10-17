extends "res://scripts/main.gd"
## Replace only HTTP dispatch, leaving shipped layout/input/promotion code intact.

var captured_actions: Array[Dictionary] = []


func _mutate(path: String, payload: Dictionary = {}) -> void:
	captured_actions.append({"path": path, "payload": payload.duplicate(true)})
