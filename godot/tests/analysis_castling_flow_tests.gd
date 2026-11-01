extends SceneTree
var app: Variant
var panel: Variant
var last_point := Vector2.ZERO
var failures := 0
var checks := 0
var relative_motion := true

func _initialize() -> void:
	_run.call_deferred()

func verify(value: bool, text: String) -> void:
	checks += 1
	if not value:
		failures += 1
		printerr("FAIL ", text)

func frames() -> void:
	await process_frame
	await process_frame

func motion(point: Vector2, held: bool) -> void:
	var event := InputEventMouseMotion.new()
	event.position = point
	event.global_position = point
	event.relative = point - last_point if relative_motion else Vector2.ZERO
	event.button_mask = MOUSE_BUTTON_MASK_LEFT if held else 0
	last_point = point
	Input.parse_input_event(event)

func button(point: Vector2, pressed: bool) -> void:
	var event := InputEventMouseButton.new()
	event.position = point
	event.global_position = point
	event.button_index = MOUSE_BUTTON_LEFT
	event.button_mask = MOUSE_BUTTON_MASK_LEFT if pressed else 0
	event.pressed = pressed
	Input.parse_input_event(event)

func click(point: Vector2) -> void:
	motion(point, false)
	button(point, true)
	await frames()
	button(point, false)
	await frames()

func center(square: String) -> Vector2:
	var display: Vector2i = panel.model.square_to_display(square)
	var area: Rect2 = panel.board.get_board_rect()
	return panel.board.get_global_transform() * (area.position + (Vector2(display) + Vector2.ONE * 0.5) * area.size.x / 8.0)

func wait_ready(predicate: Callable, text: String) -> void:
	var deadline := Time.get_ticks_msec() + 8000
	while not predicate.call() and Time.get_ticks_msec() < deadline:
		await process_frame
	verify(predicate.call(), text)

func move_clicks(move: String) -> void:
	var revision: int = int(panel.analysis_state.revision)
	await click(center(move.substr(0,2)))
	await click(center(move.substr(2,2)))
	await wait_ready(func(): return not panel.busy and int(panel.analysis_state.revision) > revision, "Viewport clicks " + move)

func opening() -> void:
	for move in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6"]:
		await move_clicks(move)

func drag(from_square: String, to_square: String) -> void:
	var start: Vector2 = center(from_square)
	var target: Vector2 = center(to_square)
	motion(start, false)
	button(start, true)
	await frames()
	motion(start + Vector2(22, 0), true)
	await frames()
	motion(target, true)
	await frames()
	button(target, false)
	await frames()

func castle_and_undo(color: String, method: String, from_square: String, to_square: String) -> void:
	var before: String = panel.analysis_state.fen
	var revision: int = int(panel.analysis_state.revision)
	if method == "button":
		await click(panel.castling_buttons.king.get_global_rect().get_center())
	elif method == "click":
		await click(center(from_square))
		await click(center(to_square))
	else:
		await drag(from_square, to_square)
	await wait_ready(func(): return not panel.busy and int(panel.analysis_state.revision) > revision, color + " " + method + " castle during normal play")
	var rank_number: String = "1" if color == "white" else "8"
	verify(panel.analysis_state.board.get("g" + rank_number) == ("K" if color == "white" else "k") and panel.analysis_state.board.get("f" + rank_number) == ("R" if color == "white" else "r"), color + " castle moved king and rook")
	await panel._mutate("undo", {})
	verify(panel.analysis_state.fen == before, color + " gesture undo restores exact FEN")

func start_standard(edited: bool) -> void:
	if not panel.editing:
		await panel._edit_position()
	panel._load_preset(panel.START_FEN)
	if edited:
		panel._choose_piece("", "지우개")
		panel._on_square("a2")
		panel._choose_piece("P", "백 폰")
		panel._on_square("a2")
		verify(panel.editor_fields[1] == "KQkq", "edited pawn restoration keeps unrelated rights")
	panel.time_option.select(0)
	await panel._start_analysis()
	panel.poll_timer.stop()
	await frames()
	verify(str(panel.analysis_state.fen).get_slice(" ", 2) == "KQkq", "normal analysis starts with all retained rights")

func _run() -> void:
	root.size = Vector2i(1240,860)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	await wait_ready(func(): return not app.state.is_empty() and not app.request_in_flight, "backend connected")
	app._on_analysis_pressed()
	panel = app.analysis_overlay
	await wait_ready(func(): return panel._state_ready and not panel._poll_in_flight, "analysis initialized")
	var before_white := ""
	for edited in [false, true]:
		await start_standard(edited)
		await opening()
		before_white = panel.analysis_state.fen
		verify(panel.analysis_state.legal_moves.has("e1g1") and not panel.castling_buttons.king.disabled, "normal opening offers white castle")
		for white_view in [true, false]:
			panel.model.view_from_white = white_view
			panel._render()
			await frames()
			for delta in [true, false]:
				relative_motion = delta
				await castle_and_undo("white", "drag", "e1", "g1")
		await castle_and_undo("white", "click", "e1", "h1")
		await castle_and_undo("white", "drag", "h1", "e1")
		await castle_and_undo("white", "button", "", "")
		await move_clicks("e1g1")
		await move_clicks("f8c5")
		await move_clicks("d2d3")
		verify(panel.analysis_state.legal_moves.has("e8g8"), "normal opening offers black castle after clearing bishop")
		for white_view in [true, false]:
			panel.model.view_from_white = white_view
			panel._render()
			await frames()
			for delta in [true, false]:
				relative_motion = delta
				await castle_and_undo("black", "drag", "e8", "g8")
		await castle_and_undo("black", "drag", "e8", "h8")
		await castle_and_undo("black", "click", "h8", "e8")
		await castle_and_undo("black", "button", "", "")
	await panel._edit_position()
	var fields: PackedStringArray = before_white.split(" ")
	fields[2] = "-"
	panel._load_preset(" ".join(fields))
	await panel._start_analysis()
	var forbidden_revision: int = int(panel.analysis_state.revision)
	await drag("e1", "g1")
	verify(int(panel.analysis_state.revision) == forbidden_revision and panel.castling_buttons.king.disabled, "king two-square drag cannot grant absent rights")
	await click(center("e1"))
	await click(center("h1"))
	verify(int(panel.analysis_state.revision) == forbidden_revision, "king-rook gesture cannot bypass absent rights")
	verify(not panel.castle_reason.text.is_empty(), "unavailable castle has visible backend reason")
	await panel._return_to_game()
	print("Godot normal-play castling input checks: %d, failures: %d" % [checks, failures])
	app.queue_free()
	await frames()
	quit(0 if failures == 0 else 1)
