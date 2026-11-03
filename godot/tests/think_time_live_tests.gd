extends SceneTree
## Synthetic controls against a private backend; wrapper records actual budgets.

var app: Variant
var probe: HTTPRequest
var checks: int = 0
var failures: int = 0
var evidence_dir: String = ""


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, message: String, seconds: float = 12.0) -> bool:
	var deadline: int = Time.get_ticks_msec() + int(seconds * 1000.0)
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + message + "; " + str(app.error_message))
			return false
		await create_timer(0.01).timeout
	verify(true, message)
	return true


func select_time(milliseconds: int) -> void:
	var index: int = app.time_option.get_item_index(milliseconds)
	app.time_option.select(index)
	app.time_option.item_selected.emit(index)


func wait_for_backend_search(milliseconds: int) -> bool:
	var deadline: int = Time.get_ticks_msec() + 3000
	while Time.get_ticks_msec() < deadline:
		var response: Dictionary = await app._send_request("/api/state", null, probe)
		if response.get("ok", false) and response.data.get("engine_thinking", false):
			verify(int(response.data.engine.get("active_movetime_ms", 0)) == milliseconds,
				"backend captured actual current search budget %d" % milliseconds)
			return true
		await create_timer(0.01).timeout
	verify(false, "backend search did not start")
	return false


func play_human(preferred: String) -> bool:
	var legal: Array = app.state.get("legal_moves", [])
	if legal.is_empty():
		verify(false, "human has a legal continuation")
		return false
	var move: String = preferred if legal.has(preferred) else str(legal[0])
	app._submit_selected_move(move)
	return await wait_for(func(): return app.engine_thinking, "native controller is waiting for engine reply")


func capture_current_and_next_time() -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	var image: Image = root.get_texture().get_image()
	verify(not image.is_empty(), "rendered current/next time image is available")
	var destination: String = evidence_dir.path_join("thinking-time-next-move.png")
	verify(not FileAccess.file_exists(destination), "preserve an existing rendered evidence file")
	if not FileAccess.file_exists(destination):
		DirAccess.make_dir_recursive_absolute(evidence_dir)
		verify(image.save_png(destination) == OK, "current/next time screenshot saved")


func finish() -> void:
	print("Godot think-time live-scene checks: %d, failures: %d" % [checks, failures])
	if app != null:
		app.queue_free()
	await process_frame
	quit(0 if failures == 0 else 1)


func _run() -> void:
	var arguments := OS.get_cmdline_user_args()
	for index in range(arguments.size()):
		if arguments[index] == "--evidence-dir" and index + 1 < arguments.size():
			evidence_dir = arguments[index + 1]
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	probe = HTTPRequest.new()
	probe.timeout = 3.0
	root.add_child(probe)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "initial authenticated game"):
		await finish()
		return
	var game_id: String = str(app.state.game_id)
	var initial_revision: int = int(app.state.revision)
	verify(app.state.human_color == "white", "test starts a white human game")
	verify(not app.time_option.disabled, "current-game time selector is usable")
	select_time(3000)
	if not await wait_for(func(): return not app.think_time_request_in_flight, "save 3-second opening budget"):
		await finish()
		return
	verify(app.state.game_id == game_id and int(app.state.revision) == initial_revision and app.state.moves.is_empty(), "changing time preserves game, board and empty history")
	verify(app.time_option.get_selected_id() == 3000, "saved initial setting is selected")
	if not await play_human("e2e4") or not await wait_for_backend_search(3000):
		await finish()
		return
	var during_search: Dictionary = app.state.duplicate(true)
	verify(not app.time_option.disabled and app.new_button.disabled, "search permits time changes while board actions stay blocked")
	select_time(5000)
	if not await wait_for(func(): return not app.think_time_request_in_flight, "save next 5-second budget while first search continues"):
		await finish()
		return
	verify(app.engine_thinking and app.request_in_flight, "settings response leaves first search running")
	verify(app.state.game_id == game_id and int(app.state.revision) == int(during_search.revision) and app.state.moves == during_search.moves, "live setting update preserves board revision and move history")
	verify(int(app.state.engine.active_movetime_ms) == 3000 and int(app.state.engine.movetime_ms) == 5000, "current 3-second search is separate from next 5-second setting")
	verify(app.think_time_help.text.contains("이번 수 3") and app.think_time_help.text.contains("다음 수부터 5"), "current and next budgets have explicit user labels")
	await capture_current_and_next_time()
	var view: bool = app.board_model.view_from_white
	app.flip_button.pressed.emit()
	verify(app.board_model.view_from_white != view and app.engine_thinking, "perspective remains usable during search and settings changes")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 2, "first real reply finishes"):
		await finish()
		return
	verify(int(app.state.engine.movetime_ms) == 5000, "first engine response does not revert newer 5-second setting")
	verify(app.state.game_id == game_id and app.state.can_undo, "same game continues with undo available")
	if not await play_human("g1f3") or not await wait_for_backend_search(5000):
		await finish()
		return
	select_time(1000)
	if not await wait_for(func(): return not app.think_time_request_in_flight, "save next 1-second budget during second search"):
		await finish()
		return
	verify(app.engine_thinking and int(app.state.engine.active_movetime_ms) == 5000 and int(app.state.engine.movetime_ms) == 1000, "second search retains 5 seconds while next setting becomes 1 second")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 4, "second real reply finishes"):
		await finish()
		return
	if not await play_human("d2d4") or not await wait_for_backend_search(1000):
		await finish()
		return
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 6, "third reply uses the saved 1-second setting"):
		await finish()
		return
	verify(app.state.game_id == game_id and app.state.human_color == "white" and int(app.state.engine.movetime_ms) == 1000, "all setting changes preserve game and human color")
	app.undo_button.pressed.emit()
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 4, "undo still removes the last human-engine pair"):
		await finish()
		return
	verify(int(app.state.engine.movetime_ms) == 1000, "undo keeps the current thinking-time setting")
	var before_preview: Dictionary = app.state.duplicate(true)
	app.handicap_button.pressed.emit()
	if not await wait_for(func(): return not app.request_in_flight and app.state.get("handicap_mode", false), "enter engine-move replacement preview"):
		await finish()
		return
	verify(app.time_option.disabled, "replacement preview disables time changes")
	select_time(5000)
	await process_frame
	verify(not app.think_time_request_in_flight and int(app.state.engine.movetime_ms) == 1000, "disabled preview callback cannot send or apply a setting change")
	app.handicap_cancel_button.pressed.emit()
	if not await wait_for(func(): return not app.request_in_flight and not app.state.get("handicap_mode", true), "cancel replacement preview"):
		await finish()
		return
	verify(app.state.moves == before_preview.moves and app.state.board == before_preview.board, "preview cancel restores the unchanged game")
	verify(int(app.state.engine.movetime_ms) == 1000 and not app.time_option.disabled, "normal time selection returns after preview")
	verify(app.error_message.is_empty(), "live setting and undo flow has no UI error")
	await finish()
