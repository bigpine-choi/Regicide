extends SceneTree
## Actual authenticated HTTP and scene controls; no fabricated UI responses.

var app: Variant
var checks := 0
var failures := 0
var evidence_dir := ""


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, message: String) -> bool:
	var deadline := Time.get_ticks_msec() + 15000
	while not predicate.call():
		if Time.get_ticks_msec() >= deadline:
			verify(false, "timeout: " + message + "; " + str(app.error_message))
			return false
		await create_timer(0.01).timeout
	verify(true, message)
	return true


func stats_total(expected: int) -> bool:
	return app.opening_stats.get("status") == "ready" \
		and app.opening_stats.get("source") == "local" \
		and app.opening_stats.get("fen") == app.analysis_state.get("fen") \
		and int(app.opening_stats.get("total", -1)) == expected


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await process_frame
	await RenderingServer.frame_post_draw
	var destination := evidence_dir.path_join(name + ".png")
	DirAccess.make_dir_recursive_absolute(evidence_dir)
	verify(root.get_texture().get_image().save_png(destination) == OK, "rendered screenshot saved: " + name)


func finish() -> void:
	if app != null:
		app.closing = true
		app.queue_free()
	await process_frame
	print("Godot opening statistics live checks: %d, failures: %d" % [checks, failures])
	quit(1 if failures else 0)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--evidence-dir" and index + 1 < args.size():
			evidence_dir = args[index + 1]
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "initial authenticated live state"):
		await finish()
		return
	var live: Dictionary = app.state.duplicate(true)
	verify(live.moves.is_empty() and not app.opening_stats_panel.visible, "initial live game remains outside the explorer")
	app.analysis_button.pressed.emit()
	if not await wait_for(func(): return app.analysis_mode and not app.request_in_flight, "start independent analysis from the actual UI button"):
		await finish()
		return
	if not await wait_for(func(): return stats_total(3), "initial position has three completed games"):
		await finish()
		return
	verify(app.opening_stats_panel.results.text == "백승 33.3% / 무 33.3% / 흑승 33.3%", "initial White/draw/Black proportions reflect the PGN corpus")
	await capture("opening-stats-initial")
	app._on_square_pressed("e2")
	app._on_square_pressed("e4")
	if not await wait_for(func(): return not app.request_in_flight and app.analysis_state.get("moves", []).size() == 1 and stats_total(2), "White e4 changes the sample to two games"):
		await finish()
		return
	verify(app.analysis_state.turn == "black" and app._can_select_move(), "analysis leaves Black under human control")
	verify(app.opening_stats_panel.results.text == "백승 50.0% / 무 50.0% / 흑승 0.0%", "e4 sample proportions are White-based")
	await capture("opening-stats-e4")
	app._on_drag_requested("e7", "e5")
	if not await wait_for(func(): return not app.request_in_flight and app.analysis_state.get("moves", []).size() == 2 and stats_total(1), "Black e5 via drag changes the sample to one game"):
		await finish()
		return
	var position_after_e5: String = app.analysis_state.fen
	verify(not app.engine_thinking and app.state.fen == live.fen and app.state.revision == live.revision, "free analysis never asks the opponent engine to reply or mutates the live game")
	app.analysis_undo_button.pressed.emit()
	if not await wait_for(func(): return not app.request_in_flight and app.analysis_state.get("moves", []).size() == 1 and stats_total(2), "analysis undo restores the two-game sample"):
		await finish()
		return
	app.analysis_redo_button.pressed.emit()
	if not await wait_for(func(): return not app.request_in_flight and app.analysis_state.get("fen") == position_after_e5 and stats_total(1), "analysis redo restores the one-game sample"):
		await finish()
		return
	var outcome_text: String = app.opening_stats_panel.results.text
	app.flip_button.pressed.emit()
	verify(app.opening_stats_panel.results.text == outcome_text and outcome_text.contains("백승 100.0%"), "flipping perspective preserves White's historical win proportion")
	await capture("opening-stats-flipped")
	app.opening_stats_panel.source_option.select(1)
	app.opening_stats_panel.source_option.item_selected.emit(1)
	if not await wait_for(func(): return app.opening_stats.get("source") == "masters" and app.opening_stats.get("status") == "auth_required", "online source reports missing authentication without an external call"):
		await finish()
		return
	verify(app.opening_stats_panel.results.text.is_empty() and app.opening_stats_panel.summary.text == "Lichess 토큰 설정 필요", "missing credentials do not invent numerical results")
	app.analysis_return_button.pressed.emit()
	verify(not app.analysis_mode and app.board.state.fen == live.fen, "analysis exit restores the real live board")
	verify(app.state.game_id == live.game_id and app.state.revision == live.revision and app.state.moves == live.moves, "live identity, revision and move history are unchanged")
	verify(not app.opening_stats_panel.visible, "statistics are hidden when returning to play")
	await finish()
