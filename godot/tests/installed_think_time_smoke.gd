extends SceneTree
## Private real-client smoke; no rule implementation or real game library writes.

var app: Variant
var checks: int = 0
var failures: int = 0
var expected_binary: String = ""
var expected_model: String = ""
var output: String = ""
var evidence: String = ""


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, label: String, seconds: float = 9.0) -> bool:
	var deadline: int = Time.get_ticks_msec() + int(seconds * 1000)
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + label + "; " + str(app.error_message))
			return false
		await create_timer(0.01).timeout
	verify(true, label)
	return true


func select_time(milliseconds: int) -> void:
	var index: int = app.time_option.get_item_index(milliseconds)
	app.time_option.select(index)
	app.time_option.item_selected.emit(index)


func capture(name: String) -> void:
	if evidence.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(evidence.path_join(name)) == OK, "rendered scene captured")


func finish() -> void:
	var file := FileAccess.open(output, FileAccess.WRITE)
	if file != null:
		file.store_string(JSON.stringify({"checks": checks, "failures": failures,
			"binary_sha256": expected_binary, "model_sha256": expected_model}) + "\n")
	print("Installed Godot model/evaluation/hint/time checks: %d, failures: %d" % [checks, failures])
	if app != null:
		app.queue_free()
	await process_frame
	quit(0 if failures == 0 else 1)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		match args[index]:
			"--expected-binary-sha": expected_binary = args[index + 1]
			"--expected-model-sha": expected_model = args[index + 1]
			"--smoke-result": output = args[index + 1]
			"--evidence-dir": evidence = args[index + 1]
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "authenticated installed client"):
		await finish()
		return
	verify(app.state.engine.binary_sha256 == expected_binary, "installed binary fingerprint reaches UI")
	verify(app.state.engine.model_sha256 == expected_model, "explicit fixed session model fingerprint reaches UI")
	var game_id: String = str(app.state.game_id)
	app._on_hint_pressed()
	if not await wait_for(func(): return not app.hint_control_in_flight and app.hint.get("status") == "ready" and not app.board.hint_move.is_empty(), "actual fixed-model legal recommendation"):
		await finish()
		return
	verify(app.state.legal_moves.has(app.board.hint_move) and app.state.moves.is_empty(), "recommendation is legal and leaves board unchanged")
	select_time(3000)
	if not await wait_for(func(): return not app.think_time_request_in_flight, "save current-game three-second setting"):
		await finish()
		return
	app._submit_selected_move("e2e4")
	if not await wait_for(func(): return app.engine_thinking and app.evaluation.get("status") == "searching" and int(app.evaluation.get("depth", 0)) > 0, "live streamed evaluation while opponent search runs"):
		await finish()
		return
	select_time(5000)
	if not await wait_for(func(): return not app.think_time_request_in_flight, "save next five-second setting during streamed search"):
		await finish()
		return
	verify(app.engine_thinking and int(app.state.engine.active_movetime_ms) == 3000 and int(app.state.engine.movetime_ms) == 5000, "active three-second snapshot and next five-second setting remain separate")
	verify(not app.evaluation.is_empty() and app.evaluation.get("status") == "searching", "time change preserves streamed evaluation")
	verify(app.think_time_help.text.contains("이번 수 3") and app.think_time_help.text.contains("다음 수부터 5"), "current and next budgets are visible")
	await capture("installed-thinking-current3-next5.png")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 2, "same game receives actual engine reply"):
		await finish()
		return
	if not await wait_for(func(): return app.evaluation.get("source") == "previous_search", "asynchronous evaluation poll reaches the completed position", 2.0):
		await finish()
		return
	verify(app.state.game_id == game_id and app.evaluation.get("source") == "previous_search", "completed search evaluation is carried to unchanged game")
	if not await wait_for(func(): return app.hint.get("status") == "ready" and not app.board.hint_move.is_empty(), "recommendation resumes from new position and next setting"):
		await finish()
		return
	verify(app.state.legal_moves.has(app.board.hint_move), "new-position recommendation remains legal")
	verify(app.state.engine.model_sha256 == expected_model and int(app.state.engine.movetime_ms) == 5000, "same fixed model and saved time survive reply and hint")
	await capture("installed-live-evaluation-and-hint.png")
	app._on_hint_pressed()
	await wait_for(func(): return not app.hint_control_in_flight, "stop advice before private smoke cleanup")
	verify(app.error_message.is_empty(), "installed client has no request error")
	await finish()
