extends "res://scripts/opening_explorer.gd"

var captured: Array = []
var next_response: Dictionary = {}
var hold: bool = false


func _request(payload: Variant = null) -> Dictionary:
	captured.append(payload.duplicate(true) if payload is Dictionary else null)
	var response := next_response.duplicate(true)
	while hold:
		await get_tree().process_frame
	return response
