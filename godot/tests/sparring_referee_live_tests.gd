extends SceneTree
## Actual temporary local game and real Stockfish HTTP; no fixture score data.

var app: Variant
var checks: int = 0
var failures: int = 0
var evidence_dir: String = ""


func _initialize() -> void:
	_run.call_deferred()


func verify(ok: bool, message: String) -> void:
	checks += 1
	if not ok:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, message: String, seconds: float = 20.0) -> bool:
	var deadline: int = Time.get_ticks_msec() + int(seconds * 1000)
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + message)
			return false
		await create_timer(.03).timeout
	verify(true, message)
	return true


func identity(state: Dictionary) -> Dictionary:
	return {"game_id": state.get("game_id"), "revision": state.get("revision"), "fen": state.get("fen"),
		"moves": state.get("moves", []).duplicate(true)}


func finish() -> void:
	if is_instance_valid(app):
		app.queue_free()
	await process_frame
	print("Godot real sparring-referee HTTP: %d checks, failures: %d" % [checks, failures])
	quit(1 if failures else 0)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--evidence-dir":
			evidence_dir = args[index + 1]
	root.size = Vector2i(1280, 900)
	root.content_scale_size = Vector2i(1280, 900)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight,
		"native client connected to temporary authenticated HTTP server"):
		await finish()
		return
	var original := identity(app.state)
	verify(not app.own_backend and app.state.engine.get("sparring_mode", false), "native verification attaches to assisted temporary practice")
	if not await wait_for(func(): return app.evaluation.get("reference_evaluation", {}).get("status", "") == "ready" and app.evaluation_summary.text.contains("Stockfish 심판"),
		"actual Stockfish HTTP result becomes the native gauge"):
		await finish()
		return
	var reference: Dictionary = app.evaluation.reference_evaluation
	verify(app._evaluation_matches(reference, app.state) and reference.turn == app.state.turn and reference.bound == "exact",
		"displayed reference matches current game/revision/FEN/turn and exact bound")
	verify(app.evaluation_bar.snapshot.get("source") == "local_stockfish_referee" and app.evaluation.get("source") != "local_stockfish_referee",
		"native gauge changes source without overwriting Regicide evaluation fields")
	verify(app.evaluation_detail.text.contains("Regicide 자체") and app.evaluation_detail.text.contains("깊이"),
		"actual display retains source, completed depth and own score")
	var summary: String = app.evaluation_summary.text
	app._on_flip_pressed()
	verify(app.evaluation_summary.text == summary, "reference score keeps white perspective when native board flips")
	app._on_flip_pressed()
	var server_state: Dictionary = await app._send_request("/api/state")
	verify(server_state.get("ok", false) and identity(server_state.data) == original,
		"native polling and view changes preserve temporary game position/history")
	if not evidence_dir.is_empty() and DisplayServer.get_name() != "headless":
		DirAccess.make_dir_recursive_absolute(evidence_dir)
		var note := Label.new()
		note.text = "임시 대국 기능 검증 · 실제 Regicide와 로컬 Stockfish 평가"
		note.position = Vector2(24, 82)
		note.add_theme_font_size_override("font_size", 12)
		note.add_theme_color_override("font_color", Color("ffd578"))
		root.add_child(note)
		await RenderingServer.frame_post_draw
		verify(root.get_texture().get_image().save_png(evidence_dir.path_join("native-real-stockfish-reference.png")) == OK,
			"actual native reference screenshot saved")
	await finish()
