extends SceneTree
## Exercises the shipped scene and HTTP callbacks, with synthesized GUI input.

var app: Variant
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


func click_at(point: Vector2) -> void:
	var down := InputEventMouseButton.new()
	down.position = point
	down.global_position = point
	down.button_index = MOUSE_BUTTON_LEFT
	down.pressed = true
	root.push_input(down, true)
	await process_frame
	var up := InputEventMouseButton.new()
	up.position = point
	up.global_position = point
	up.button_index = MOUSE_BUTTON_LEFT
	up.pressed = false
	root.push_input(up, true)
	await process_frame


func click_square(square: String) -> void:
	var rect: Rect2 = app.board.get_board_rect()
	var display: Vector2i = app.board_model.square_to_display(square)
	await click_at(app.board.global_position + rect.position + (Vector2(display) + Vector2.ONE * 0.5) * rect.size.x / 8.0)


func flip_without_mutation(message: String) -> void:
	var before: String = JSON.stringify(app.state)
	var view: bool = app.board_model.view_from_white
	var selected: String = app.board_model.selected_square
	var busy: bool = app.request_in_flight
	verify(not app.flip_button.disabled, "flip enabled: " + message)
	await click_at(app.flip_button.get_global_rect().get_center())
	verify(app.board_model.view_from_white != view, "flip changes view: " + message)
	verify(JSON.stringify(app.state) == before, "flip preserves complete backend state: " + message)
	verify(app.board_model.selected_square == selected, "flip preserves logical selection: " + message)
	verify(app.request_in_flight == busy, "flip preserves in-flight request: " + message)


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	var image: Image = root.get_texture().get_image()
	verify(not image.is_empty(), "rendered image is available")
	verify(image.save_png(evidence_dir.path_join(name + ".png")) == OK, "rendered screenshot saved")


func finish() -> void:
	print("Godot live-scene checks: %d, failures: %d" % [checks, failures])
	if app != null:
		app.queue_free()
	await process_frame
	quit(0 if failures == 0 else 1)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--evidence-dir" and index + 1 < args.size():
			evidence_dir = args[index + 1]
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "initial authenticated callback"):
		await finish()
		return
	verify(app.state.human_color == "white", "real game starts as white")
	verify(app.state.engine.movetime_ms == 1000, "real engine uses 1000 ms")
	verify(app.state.moves.is_empty(), "real initial board is unchanged")
	verify(app.state.legal_moves.has("e2e4"), "real backend provides legal e2e4")
	var initial_revision: int = int(app.state.revision)
	await click_square("e2")
	verify(app.board_model.selected_square == "e2", "real white piece selected by viewport input")
	await flip_without_mutation("white player selected e2")
	verify(not app.board_model.view_from_white, "white player can watch from black")
	await capture("white-player-black-view")
	await click_square("e4")
	if not await wait_for(func(): return app.engine_thinking, "real engine callback is pending after white move"):
		await finish()
		return
	verify(int(app.state.revision) == initial_revision + 1, "exactly one human move applied")
	verify(app.state.moves.size() == 1 and app.state.moves[0].uci == "e2e4", "flipped input submitted exact e2e4")
	await flip_without_mutation("1000 ms engine request")
	await flip_without_mutation("same pending engine request")
	verify(app.engine_thinking, "GUI flips complete before engine callback")
	await capture("engine-thinking-flipped")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 2, "real engine reply updates the board"):
		await finish()
		return
	verify(int(app.state.revision) == initial_revision + 2, "flips did not advance server revision")
	verify(app.state.human_to_move, "turn returns to white after real engine callback")
	verify(not app.board_model.view_from_white, "engine callback preserves chosen black perspective")
	verify(app.error_message.is_empty(), "white real-engine game has no UI error")
	await click_at(app.undo_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.is_empty(), "undo callback restores starting board"):
		await finish()
		return
	verify(not app.board_model.view_from_white, "undo preserves chosen perspective")
	verify(int(app.state.revision) == initial_revision + 3, "undo is the only revision change")
	app.color_option.select(1)
	app.time_option.select(1)
	await click_at(app.new_button.get_global_rect().get_center())
	if not await wait_for(func(): return app.engine_thinking and app.state.human_color == "black", "black new-game opening request"):
		await finish()
		return
	verify(not app.board_model.view_from_white, "new game retains chosen perspective")
	await flip_without_mutation("black player waiting for white opening")
	await flip_without_mutation("black player waiting for same opening")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 1, "black game receives real white engine opening"):
		await finish()
		return
	verify(app.state.human_color == "black" and app.state.human_to_move, "human black receives turn")
	verify(app.state.moves[0].actor == "engine", "white opening was selected by Regicide")
	var black_move: String = str(app.state.legal_moves[0])
	await click_square(black_move.substr(0, 2))
	verify(app.board_model.selected_square == black_move.substr(0, 2), "real black piece selected")
	await flip_without_mutation("black player selecting a piece")
	verify(app.board_model.view_from_white, "black player can watch from white")
	await capture("black-player-white-view")
	await click_square(black_move.substr(2, 2))
	if not await wait_for(func(): return app.engine_thinking, "engine request after black move"):
		await finish()
		return
	verify(app.state.moves.size() == 2 and app.state.moves[1].uci == black_move, "flipped black input submitted exact legal move")
	await flip_without_mutation("real reply after black move")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 3, "second real engine reply"):
		await finish()
		return
	verify(app.state.human_to_move, "turn returns to black")
	verify(not app.board_model.view_from_white, "second callback preserves perspective")
	await click_at(app.resign_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and app.state.result != "*", "resignation callback ends black game"):
		await finish()
		return
	verify(app.state.result == "1-0" and app.state.termination == "resignation", "black resignation has correct result")
	await flip_without_mutation("finished real game")
	verify(app.state.moves.size() == 3, "final flip creates no additional moves")
	verify(app.error_message.is_empty(), "black real-engine game has no UI error")
	await finish()
