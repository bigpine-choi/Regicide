extends SceneTree
## Automated real-scene controls and synthesized board input, not manual play.

var app: Control
var checks: int = 0
var failures: int = 0
var evidence_dir: String = ""
var expected: Dictionary = {}


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, message: String) -> bool:
	var deadline: int = Time.get_ticks_msec() + 12000
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + message + "; " + str(app.error_message))
			return false
		await create_timer(0.01).timeout
	verify(true, message)
	return true


func click_at(point: Vector2) -> void:
	var motion := InputEventMouseMotion.new()
	motion.position = point
	motion.global_position = point
	root.push_input(motion, true)
	await process_frame
	var event := InputEventMouseButton.new()
	event.position = point
	event.global_position = point
	event.button_index = MOUSE_BUTTON_LEFT
	event.pressed = true
	root.push_input(event, true)
	await process_frame
	var released := InputEventMouseButton.new()
	released.position = point
	released.global_position = point
	released.button_index = MOUSE_BUTTON_LEFT
	released.pressed = false
	root.push_input(released, true)
	await process_frame


func click_square(square: String) -> void:
	var rect: Rect2 = app.board.get_board_rect()
	var coordinate: Vector2i = app.board_model.square_to_display(square)
	await click_at(app.board.global_position + rect.position + (Vector2(coordinate) + Vector2.ONE * 0.5) * rect.size.x / 8.0)


func press(button: Button) -> void:
	verify(not button.disabled, "enabled control: " + button.text)
	button.pressed.emit()
	await process_frame


func verify_frame(ply: int) -> void:
	var shown: Dictionary = app._display_state()
	var wanted: Dictionary = expected.frames[ply]
	verify(app.review_ply == ply, "selected replay ply %d" % ply)
	verify(shown.board == wanted.board, "independent board matches replay ply %d" % ply)
	verify(shown.turn == wanted.turn, "independent turn matches replay ply %d" % ply)
	verify(shown.get("last_move") == wanted.get("last_move"), "last move matches replay ply %d" % ply)
	verify(shown.moves.size() == ply, "visible history has exact replay prefix %d" % ply)
	verify(not shown.get("human_to_move", false) and not shown.get("engine_to_move", false), "replay frame never enables either player")


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	var screenshot: Image = root.get_texture().get_image()
	verify(not screenshot.is_empty(), "native screenshot exists")
	verify(screenshot.save_png(evidence_dir.path_join(name + ".png")) == OK, "native screenshot saved")


func finish() -> void:
	print("Godot archive live-scene checks: %d, failures: %d" % [checks, failures])
	if app != null:
		app.queue_free()
	await process_frame
	quit(0 if failures == 0 else 1)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--evidence-dir" and index + 1 < args.size():
			evidence_dir = args[index + 1]
		if args[index] == "--expected-file" and index + 1 < args.size():
			expected = JSON.parse_string(FileAccess.get_file_as_string(args[index + 1]))
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "authenticated live game loaded"):
		await finish()
		return
	await click_square("e2")
	verify(app.board_model.selected_square == "e2", "live selection exists before archive review")
	var live: Dictionary = app.state.duplicate(true)
	await press(app.library_button)
	if not await wait_for(func(): return not app.request_in_flight and app.library_list.item_count >= 2, "persistent game library loaded"):
		await finish()
		return
	verify(app.library_dialog.visible, "game library dialog is visible")
	await capture("game-library")
	var index: int = -1
	for candidate in range(app._library_games.size()):
		if str(app._library_games[candidate].game_id) == str(expected.game_id):
			index = candidate
	verify(index >= 0, "archived handicap game is listed after backend restart")
	if index < 0:
		await finish()
		return
	app.library_list.select(index)
	await press(app.library_open_button)
	if not await wait_for(func(): return app.review_mode and not app.request_in_flight, "selected archived game opened"):
		await finish()
		return
	verify(app.review_controls.visible, "replay controls are visible")
	verify(str(app.review_summary.game_id) == str(expected.game_id), "correct selected game is replayed")
	verify(app.review_summary.get("handicap_moves", 0) == 1, "handicap count is retained in archive")
	verify(app.review_summary.get("movetime_ms") == null, "legacy missing move time arrives as HTTP null")
	verify(app._display_state().engine.movetime_ms == 1000, "legacy null move time uses valid display fallback")
	verify(app.state == live, "opening replay preserves authoritative live game")
	verify(app.undo_button.disabled and app.new_button.disabled and app.resign_button.disabled and app.handicap_button.disabled, "replay disables live mutations")
	verify(not app.new_button.is_visible_in_tree() and not app.undo_button.is_visible_in_tree() and not app.resign_button.is_visible_in_tree() and not app.handicap_button.is_visible_in_tree(), "replay hides current-game controls")
	verify(app.save_button.is_visible_in_tree() and Rect2(Vector2.ZERO, Vector2(root.size)).encloses(app.save_button.get_global_rect()), "selected-game PGN control fits inside replay window")
	verify_frame(0)
	verify(app.review_previous_button.disabled, "previous disabled at initial board")
	await press(app.review_next_button)
	verify_frame(1)
	await click_at(app.flip_button.get_global_rect().get_center())
	verify(not app.board_model.view_from_white, "archive can flip to black view")
	verify_frame(1)
	await press(app.review_next_button)
	verify_frame(2)
	verify(app.review_moves[1].get("forced", false), "replay retains user-forced attribution")
	await capture("archive-black-view")
	var selection: String = app.board_model.selected_square
	await click_square("e2")
	await click_square("e4")
	verify(app.board_model.selected_square == selection, "replay board ignores move input")
	verify(app.state == live, "replay input cannot mutate current board")
	await press(app.review_previous_button)
	verify_frame(1)
	await click_at(app.flip_button.get_global_rect().get_center())
	verify(app.board_model.view_from_white, "archive can flip back to white view")
	await press(app.review_last_button)
	verify_frame(expected.frames.size() - 1)
	verify(app.review_next_button.disabled, "next disabled at final archived position")
	verify(app.move_history.text.contains("핸디캡"), "replayed history visibly marks handicap")
	await capture("archive-white-view")
	# Use the scene's file chooser for automation. A native OS chooser is outside
	# this scene test's input surface and is not claimed to be manually tested.
	app.save_dialog.use_native_dialog = false
	app._on_save_pressed()
	await process_frame
	verify(app._pending_pgn == app.review_pgn and not app.review_pgn.is_empty(), "PGN export uses selected archive, not live game")
	verify(app.save_dialog.visible, "selected-game export opens file dialog")
	app.save_dialog.hide()
	app._save_pgn_file(evidence_dir.path_join("selected-game.pgn"))
	verify(FileAccess.file_exists(evidence_dir.path_join("selected-game.pgn")), "selected-game PGN export is saved")
	await press(app.review_return_button)
	verify(not app.review_mode, "return control leaves replay")
	verify(app.state == live and app._display_state() == live, "return restores exact current game")
	verify(app.new_button.is_visible_in_tree() and app.undo_button.is_visible_in_tree() and app.resign_button.is_visible_in_tree() and app.handicap_button.is_visible_in_tree(), "return restores current-game controls")
	verify(app.board_model.selected_square == "e2", "return restores previous live selection")
	verify(app.board_model.view_from_white, "return preserves chosen viewpoint")
	await click_square("e4")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 2, "live game continues with actual engine after reviewing archive"):
		await finish()
		return
	verify(app.state.moves[0].uci == "e2e4", "continuing live game submits intended human move")
	verify(int(app.state.revision) == int(live.revision) + 2, "only resumed human move and engine reply advance revision")
	verify(app.error_message.is_empty(), "archive and resumed live flow have no error")
	await finish()
