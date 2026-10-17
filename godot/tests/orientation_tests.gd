extends SceneTree
## Automated input through a real Viewport; no rules are reimplemented here.

const ViewModel = preload("res://scripts/board_view_model.gd")
const BoardControl = preload("res://scripts/chess_board.gd")
const CaptureScene = preload("res://tests/capture_scene.gd")
var checks: int = 0
var failures: int = 0
var pressed: Array[String] = []
var app: Variant


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func fixture(color: String = "white") -> Dictionary:
	return {"revision": 17, "fen": "8/8/8/8/8/8/4P3/4K2k w - - 0 1",
		"board": {"e2": "P", "e1": "K", "h1": "k"}, "turn": "white",
		"human_color": color, "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["e2e3", "e2e4", "e1d1"],
		"moves": [{"ply": 1, "uci": "h2h1", "san": "Kh1", "actor": "engine"}],
		"last_move": "h2h1", "in_check": false, "result": "*", "can_undo": true,
		"status": "test fixture", "termination": null, "error": null,
		"engine": {"name": "test fixture", "movetime_ms": 1000}}


func click_at(point: Vector2) -> void:
	var motion := InputEventMouseMotion.new()
	motion.position = point
	motion.global_position = point
	root.push_input(motion, true)
	await process_frame
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


func drag_between(origin: String, destination: String) -> void:
	var rect: Rect2 = app.board.get_board_rect()
	var cell: float = rect.size.x / 8.0
	var from_display: Vector2i = app.board_model.square_to_display(origin)
	var to_display: Vector2i = app.board_model.square_to_display(destination)
	var from_point: Vector2 = app.board.global_position + rect.position + (Vector2(from_display) + Vector2.ONE * 0.5) * cell
	var to_point: Vector2 = app.board.global_position + rect.position + (Vector2(to_display) + Vector2.ONE * 0.5) * cell
	var down := InputEventMouseButton.new()
	down.position = from_point
	down.global_position = from_point
	down.button_index = MOUSE_BUTTON_LEFT
	down.pressed = true
	root.push_input(down, true)
	await process_frame
	var motion := InputEventMouseMotion.new()
	motion.position = to_point
	motion.global_position = to_point
	motion.relative = to_point - from_point
	motion.button_mask = MOUSE_BUTTON_MASK_LEFT
	root.push_input(motion, true)
	await process_frame
	var up := InputEventMouseButton.new()
	up.position = to_point
	up.global_position = to_point
	up.button_index = MOUSE_BUTTON_LEFT
	up.pressed = false
	root.push_input(up, true)
	await process_frame


func verify_scene_flip(message: String) -> void:
	var before: String = JSON.stringify(app.state)
	var white: bool = app.board_model.view_from_white
	var selected: String = app.board_model.selected_square
	var promotions: Array = app.board_model.pending_promotions.duplicate()
	var busy: bool = app.request_in_flight
	verify(not app.flip_button.disabled, "flip button enabled: " + message)
	await click_at(app.flip_button.get_global_rect().get_center())
	verify(app.board_model.view_from_white != white, "real scene button changes perspective: " + message)
	verify(JSON.stringify(app.state) == before, "scene flip preserves all server data: " + message)
	verify(app.board_model.selected_square == selected, "scene flip preserves selection: " + message)
	verify(app.board_model.pending_promotions == promotions, "scene flip preserves promotions: " + message)
	verify(app.request_in_flight == busy, "scene flip sends no HTTP request: " + message)
	verify(app.view_label.text.begins_with("흑" if white else "백"), "scene perspective label follows view")


func _run() -> void:
	var model = ViewModel.new()
	for white in [true, false]:
		model.view_from_white = white
		var files: String = "abcdefgh" if white else "hgfedcba"
		var ranks: String = "87654321" if white else "12345678"
		var seen: Dictionary = {}
		for row in range(8):
			verify(model.rank_label(row) == ranks[row], "rank label %d/%s" % [row, white])
			for column in range(8):
				var square: String = files[column] + ranks[row]
				verify(model.display_to_square(column, row) == square, "display square " + square)
				verify(model.square_to_display(square) == Vector2i(column, row), "inverse square " + square)
				seen[square] = true
			verify(model.file_label(row) == files[row], "file label %d/%s" % [row, white])
		verify(seen.size() == 64, "all squares appear exactly once")
		for outside in [Vector2i(-1, 0), Vector2i(0, -1), Vector2i(8, 0), Vector2i(0, 8)]:
			verify(model.display_to_square(outside.x, outside.y) == "", "outside display rejected")
		for invalid in ["", "a0", "a9", "i1", "a11", "11", "A1"]:
			verify(model.square_to_display(invalid) == Vector2i(-1, -1), "invalid square rejected")
	for human_color in ["white", "black"]:
		for white in [true, false]:
			var state: Dictionary = fixture(human_color)
			var before: String = JSON.stringify(state)
			model.view_from_white = white
			model.selected_square = "e2"
			model.apply_state(state)
			model.flip_view()
			verify(model.view_from_white != white, "flip independent of player color")
			verify(model.selected_square == "e2", "selected logical square survives flip")
			verify(JSON.stringify(state) == before, "flip cannot mutate authoritative state")
			model.flip_view()
			verify(model.view_from_white == white, "double flip is identity")
			for action in ["refresh", "undo", "new game", "ended game", "engine thinking"]:
				state["revision"] = int(state["revision"]) + 1
				state["human_color"] = "black" if human_color == "white" else "white"
				state["human_to_move"] = action != "engine thinking" and action != "ended game"
				state["engine_to_move"] = action == "engine thinking"
				state["result"] = "0-1" if action == "ended game" else "*"
				model.apply_state(state)
				verify(model.view_from_white == white, "perspective survives " + action)
	# A promotion chooser stays associated with logical UCI moves across flips.
	var promotion: Dictionary = fixture()
	promotion["legal_moves"] = ["a7a8q", "a7a8r", "a7a8b", "a7a8n"]
	model.selected_square = "a7"
	model.pending_promotions.assign(promotion["legal_moves"])
	model.apply_state(promotion)
	var pending: Array[String] = model.pending_promotions.duplicate()
	model.flip_view()
	verify(model.pending_promotions == pending, "promotion choices survive flip")
	verify(model.selected_square == "a7", "promotion origin survives flip")
	model.apply_state(promotion.duplicate(true))
	verify(model.pending_promotions == pending, "same-state refresh preserves promotion choices")
	model.apply_state(fixture())
	verify(model.pending_promotions.is_empty(), "changed legal moves invalidate old promotion choices")
	verify(model.selected_square.is_empty(), "changed legal moves invalidate old selection")
	root.size = Vector2i(1280, 900)
	var board = BoardControl.new()
	board.position = Vector2(40, 40)
	board.size = Vector2(700, 700)
	board.model = model
	board.state = fixture()
	board.square_pressed.connect(func(square: String): pressed.append(square))
	root.add_child(board)
	await process_frame
	await process_frame
	for white in [true, false]:
		model.view_from_white = white
		var rect: Rect2 = board.get_board_rect()
		var cell: float = rect.size.x / 8.0
		for row in range(8):
			for column in range(8):
				var point: Vector2 = rect.position + Vector2(column + 0.5, row + 0.5) * cell
				verify(board.square_at_point(point) == model.display_to_square(column, row), "pixel mapping")
		verify(board.square_at_point(rect.position - Vector2.ONE) == "", "outside board rejected")
		# Castling, en-passant and promotion coordinates use the same exact mapping.
		for uci in ["e1g1", "e8c8", "e5d6", "a7a8q", "h2h1n"]:
			for square in [uci.substr(0, 2), uci.substr(2, 2)]:
				var display: Vector2i = model.square_to_display(square)
				var point: Vector2 = board.global_position + rect.position + (Vector2(display) + Vector2.ONE * 0.5) * cell
				var before_count: int = pressed.size()
				await click_at(point)
				verify(pressed.size() == before_count + 1 and pressed.back() == square, "Viewport click " + uci + ":" + square)
	board.queue_free()
	await process_frame
	app = load("res://main.tscn").instantiate()
	app.offline_mode = true
	root.add_child(app)
	await process_frame
	await process_frame
	for human_color in ["white", "black"]:
		app.accept_state(fixture(human_color))
		await process_frame
		await click_square("e2")
		verify(app.board_model.selected_square == "e2", "scene selects logical e2: " + human_color)
		await verify_scene_flip("selected piece, human " + human_color)
		var saved_view: bool = app.board_model.view_from_white
		app.accept_state(fixture("black" if human_color == "white" else "white"))
		verify(app.board_model.view_from_white == saved_view, "scene new human color retains chosen view")
		app.request_in_flight = true
		app.engine_thinking = true
		app._render()
		await verify_scene_flip("engine thinking, human " + human_color)
		verify(app.engine_thinking, "engine flag survives flip")
		app.request_in_flight = false
		app.engine_thinking = false
		var finished: Dictionary = fixture(human_color)
		finished["result"] = "1-0"
		finished["human_to_move"] = false
		finished["legal_moves"] = []
		app.accept_state(finished)
		await verify_scene_flip("finished game, human " + human_color)
		var promote: Dictionary = fixture(human_color)
		var origin: String = "a7" if human_color == "white" else "h2"
		var destination: String = "a8" if human_color == "white" else "h1"
		promote["turn"] = human_color
		promote["board"] = {origin: "P" if human_color == "white" else "p", "e1": "K", "e8": "k"}
		promote["legal_moves"] = [origin + destination + "q", origin + destination + "r", origin + destination + "b", origin + destination + "n"]
		app.accept_state(promote)
		await click_square(origin)
		await click_square(destination)
		verify(app.promotion_panel.visible, "nonmodal promotion choices open: " + human_color)
		verify(app.board_model.pending_promotions.size() == 4, "four exact promotion choices")
		await verify_scene_flip("promotion chooser, human " + human_color)
		verify(app.promotion_panel.visible, "promotion chooser stays visible after flip")
		app._cancel_promotion()
		verify(app.board_model.pending_promotions.is_empty(), "promotion cancel remains usable after flip")
	app.queue_free()
	await process_frame
	# Assert the actual controller submits the same logical move in both views.
	app = CaptureScene.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	await process_frame
	await process_frame
	for white in [true, false]:
		app.board_model.view_from_white = white
		for uci in ["e1g1", "e8c8", "e5d6", "a7a8q", "h2h1n"]:
			var special: Dictionary = fixture("white" if not uci.begins_with("e8") and not uci.begins_with("h2") else "black")
			special["legal_moves"] = [uci]
			app.board_model.selected_square = ""
			app.accept_state(special)
			app.captured_actions.clear()
			await click_square(uci.substr(0, 2))
			await click_square(uci.substr(2, 2))
			if uci.length() == 5:
				verify(app.captured_actions.is_empty(), "promotion waits for explicit choice")
				await verify_scene_flip("exact promotion " + uci)
				verify(not app.promotion_buttons[uci.substr(4)].disabled, "exact promotion button enabled: " + uci)
				await click_at(app.promotion_buttons[uci.substr(4)].get_global_rect().get_center())
			verify(app.captured_actions.size() == 1, "exactly one special move request %s/%s: %s" % [uci, white, app.captured_actions])
			if app.captured_actions.size() == 1:
				verify(app.captured_actions[0].path == "/api/move" and app.captured_actions[0].payload.move == uci, "exact special UCI " + uci)
		for already_selected in [false, true]:
			app.accept_state(fixture())
			app.board_model.selected_square = "e2" if already_selected else ""
			app._render()
			app.captured_actions.clear()
			await drag_between("e2", "e4")
			verify(app.captured_actions.size() == 1, "exactly one drag move request %s/%s: %s" % [white, already_selected, app.captured_actions])
			if app.captured_actions.size() == 1:
				verify(app.captured_actions[0].payload.move == "e2e4", "drag exact logical move, selected=%s" % already_selected)
	app.queue_free()
	await process_frame
	print("Godot orientation/input checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
