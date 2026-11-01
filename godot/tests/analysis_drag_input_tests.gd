extends SceneTree

var app: Variant
var panel: Variant
var last_point: Vector2 = Vector2.ZERO
var relative_motion: bool = true
var checks: int = 0
var failures: int = 0


func verify(value: bool, description: String) -> void:
	checks += 1
	if not value:
		failures += 1
		printerr("FAIL: ", description)


func _initialize() -> void:
	_run.call_deferred()


func motion(point: Vector2, held: bool) -> void:
	var event := InputEventMouseMotion.new()
	event.position = point
	event.global_position = point
	event.relative = point - last_point if relative_motion else Vector2.ZERO
	last_point = point
	event.button_mask = MOUSE_BUTTON_MASK_LEFT if held else 0
	Input.parse_input_event(event)


func button(point: Vector2, pressed: bool) -> void:
	var event := InputEventMouseButton.new()
	event.position = point
	event.global_position = point
	event.button_index = MOUSE_BUTTON_LEFT
	event.button_mask = MOUSE_BUTTON_MASK_LEFT if pressed else 0
	event.pressed = pressed
	Input.parse_input_event(event)


func frames() -> void:
	await process_frame
	await process_frame


func center(square: String) -> Vector2:
	var display: Vector2i = panel.model.square_to_display(square)
	var area: Rect2 = panel.board.get_board_rect()
	return panel.board.get_global_transform() * (area.position + (Vector2(display) + Vector2.ONE * 0.5) * area.size.x / 8.0)


func native_drag(start: Vector2, end: Vector2) -> void:
	var before: String = panel.editor_fen()
	motion(start, false)
	await frames()
	button(start, true)
	await frames()
	verify(panel.editor_fen() == before, "pointer press preserves draft")
	motion(start + Vector2(22, 0), true)
	await frames()
	verify(root.gui_is_dragging() and not panel._editor_drag.is_empty(), "held motion starts native drag with payload")
	motion(end, true)
	await frames()
	verify(panel.editor_fen() == before, "moving preview preserves draft until release")
	button(end, false)
	await frames()
	verify(not root.gui_is_dragging() and panel._editor_drag.is_empty(), "release finishes native drag and clears payload")


func _run() -> void:
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	app.offline_mode = true
	root.add_child(app)
	await frames()
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state({"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"})
	app._on_analysis_pressed()
	panel = app.analysis_overlay
	await frames()
	await native_drag(panel.palette_buttons["Q"].get_global_rect().get_center(), center("e4"))
	var palette_passed: bool = panel.editor_pieces.get("e4") == "Q"
	panel._load_preset(panel.START_FEN)
	await frames()
	await native_drag(center("e2"), center("e4"))
	var board_passed: bool = panel.editor_pieces.get("e4") == "P" and not panel.editor_pieces.has("e2")
	relative_motion = false
	panel._load_preset(panel.START_FEN)
	await frames()
	await native_drag(panel.palette_buttons["Q"].get_global_rect().get_center(), center("e4"))
	var absolute_palette: bool = panel.editor_pieces.get("e4") == "Q"
	panel._load_preset(panel.START_FEN)
	await frames()
	await native_drag(center("e2"), center("e4"))
	var absolute_board: bool = panel.editor_pieces.get("e4") == "P" and not panel.editor_pieces.has("e2")
	verify(palette_passed and board_passed, "normal relative-motion palette and board drags commit correctly")
	verify(absolute_palette and absolute_board, "zero-relative position-motion palette and board drags commit correctly")
	panel._load_preset(panel.START_FEN)
	await frames()
	await native_drag(center("e2"), panel.trash_area.get_global_rect().get_center())
	verify(not panel.editor_pieces.has("e2"), "native trash drop deletes board source")
	panel._load_preset(panel.START_FEN)
	await frames()
	await native_drag(center("e2"), panel.return_button.get_global_rect().get_center())
	verify(not panel.editor_pieces.has("e2") and panel.visible, "native in-view outside-board drop deletes without activating underlying button")
	panel._load_preset(panel.START_FEN)
	await frames()
	await native_drag(panel.palette_buttons["Q"].get_global_rect().get_center(), panel.return_button.get_global_rect().get_center())
	verify(panel.editor_fen() == panel.START_FEN, "native palette outside-board drop preserves exact draft")
	panel.model.view_from_white = false
	await frames()
	await native_drag(center("e2"), center("e4"))
	verify(panel.editor_pieces.get("e4") == "P" and not panel.editor_pieces.has("e2"), "native flipped board drag uses correct squares")
	panel._load_preset(panel.START_FEN)
	await frames()
	await native_drag(center("e2"), Vector2(-100, -100))
	verify(panel.editor_fen() == panel.START_FEN, "native release outside viewport preserves full FEN")
	var start: Vector2 = center("e2")
	motion(start, false)
	button(start, true)
	await frames()
	motion(start + Vector2(22, 0), true)
	await frames()
	var escape := InputEventKey.new()
	escape.keycode = KEY_ESCAPE
	escape.pressed = true
	Input.parse_input_event(escape)
	await frames()
	button(panel.return_button.get_global_rect().get_center(), false)
	await frames()
	verify(panel.editor_fen() == panel.START_FEN and panel._editor_drag.is_empty() and not root.gui_is_dragging(), "native Escape cancels payload and preview without deleting source")
	print("Godot native drag dispatch checks: %d, failures: %d" % [checks, failures])
	app.queue_free()
	await frames()
	quit(0 if failures == 0 else 1)
