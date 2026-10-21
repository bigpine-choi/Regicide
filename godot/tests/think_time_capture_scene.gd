extends "res://scripts/main.gd"
## Exercise the actual controller while replacing only HTTP transport.

var captured_requests: Array[Dictionary] = []
var next_response: Dictionary = {}
var hold_response: bool = false


func _send_request(path: String, payload: Variant = null, client: HTTPRequest = null) -> Dictionary:
	captured_requests.append({"path": path, "payload": payload.duplicate(true) if payload is Dictionary else payload,
		"settings_client": client == think_time_http})
	await get_tree().process_frame
	while hold_response:
		await get_tree().process_frame
	return next_response.duplicate(true)
