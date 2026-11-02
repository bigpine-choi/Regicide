extends "res://scripts/main.gd"
## Test the shipped controls with only HTTP transport replaced.

var captured_requests: Array[Dictionary] = []
var next_response: Dictionary = {}
var hold_response: bool = false


func _send_request(path: String, payload: Variant = null, client: HTTPRequest = null) -> Dictionary:
	captured_requests.append({"path": path, "payload": payload.duplicate(true) if payload is Dictionary else payload,
		"clock_client": client == clock_adjust_http, "main_client": client == http})
	await get_tree().process_frame
	while hold_response:
		await get_tree().process_frame
	return next_response.duplicate(true)
