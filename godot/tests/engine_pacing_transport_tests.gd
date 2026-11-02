extends SceneTree
## Transport-only clock/pacing regression; fake HTTP, no engine or sleeps.
const CaptureApp = preload("res://tests/think_time_capture_scene.gd")
var app: Variant
var checks: int = 0
var failures: int = 0

func _initialize() -> void:
	_run.call_deferred()

func verify(ok: bool, label: String) -> void:
	checks += 1
	if not ok:
		failures += 1
		printerr("FAIL: ", label)

func _run() -> void:
	app = CaptureApp.new()
	app.offline_mode = true
	root.add_child(app)
	await process_frame
	app.backend_url = "http://127.0.0.1:1"
	app.state = {"game_id":"pacing-transport", "revision":17}
	app._set_main_request_timeout("/api/engine")
	verify(app.http.timeout >= 45.0 + 7.0 + 15.0, "engine request allows paced turn plus search and transport margin")
	app._set_main_request_timeout("/api/state")
	verify(app.http.timeout == 15.0, "ordinary request restores short timeout after engine request")
	app._set_main_request_timeout("/api/new")
	verify(app.http.timeout == 15.0, "new game is not assigned the move timeout")
	app.next_response = {"ok":true,"data":{"game_id":"pacing-transport","revision":17}}
	app.own_backend = true
	app.engine_thinking = true
	var original: String = JSON.stringify(app.state)
	await app._release_engine_wait_on_close()
	verify(app.captured_requests.size() == 1, "owned pending move sends one release request")
	verify(app.captured_requests[0].path == "/api/engine/skip-wait", "release uses dedicated host pacing endpoint")
	verify(app.captured_requests[0].payload == {"revision":17}, "release binds the currently pending revision")
	verify(JSON.stringify(app.state) == original and app.engine_thinking, "release does not apply another move or complete original request")
	app.own_backend = false
	await app._release_engine_wait_on_close()
	verify(app.captured_requests.size() == 1, "non-owned backend is not interrupted on close")
	app.own_backend = true
	app.engine_thinking = false
	await app._release_engine_wait_on_close()
	verify(app.captured_requests.size() == 1, "idle game needs no pacing release")
	app.queue_free()
	await process_frame
	print("Godot engine pacing transport checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
