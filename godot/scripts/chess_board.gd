class_name RegicideChessBoard
extends Control
## Draws the server position. Rotating the board changes only square mapping.

signal square_pressed(square: String)
signal drag_requested(from_square: String, to_square: String)

const ViewModel = preload("res://scripts/board_view_model.gd")
const BoardMotion = preload("res://scripts/board_motion.gd")
var model: ViewModel = ViewModel.new()
var state: Dictionary:
	get:
		return _state
	set(value):
		_accept_position(value)
var animation_enabled: bool = true:
	set(value):
		animation_enabled = value
		if not value:
			finish_animation()
var piece_textures: Dictionary = {}
var label_font: Font
var pressed_square: String = ""
var hint_move: String = ""
## Presentation-only, supplied by verified coaching evidence for this frame.
var coach_squares: Array = []
## Passive review badge for one verified actual-game position, never a move input.
var move_annotation: Dictionary = {}:
	set(value):
		move_annotation = value.duplicate(true)
		queue_redraw()
var _state: Dictionary = {}
var _position: Dictionary = {}
var _motion: Dictionary = {}
var _motion_elapsed: float = 0.0
var _motion_duration: float = 0.24
var _motion_view: bool = true
const LANDING_SECONDS := 0.12


func _motion_allowed() -> bool:
	return true


func _accept_position(value: Dictionary) -> void:
	var previous: Dictionary = _position
	_state = value
	_position = BoardMotion.snapshot(value)
	if previous == _position:
		queue_redraw()
		return
	finish_animation()
	if animation_enabled and is_node_ready() and is_visible_in_tree() and _motion_allowed():
		_motion = BoardMotion.between(previous, _position)
		if not _motion.is_empty():
			_motion_elapsed = 0.0
			_motion_duration = 0.28 if _motion.movements.size() > 1 else 0.24
			_motion_view = model.view_from_white
			set_process(true)
	queue_redraw()


func finish_animation() -> void:
	_motion = {}
	_motion_elapsed = 0.0
	set_process(false)
	queue_redraw()


func is_animating() -> bool:
	return not _motion.is_empty()


func _process(delta: float) -> void:
	if not is_visible_in_tree() or model.view_from_white != _motion_view or not _motion_allowed():
		finish_animation()
		return
	_motion_elapsed += delta
	if _motion_elapsed >= _motion_duration + LANDING_SECONDS:
		finish_animation()
	else:
		queue_redraw()


func _notification(what: int) -> void:
	if what == NOTIFICATION_VISIBILITY_CHANGED and not is_visible_in_tree():
		finish_animation()


func _on_board_resized() -> void:
	finish_animation()


func _ready() -> void:
	mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	custom_minimum_size = Vector2(400, 400)
	size_flags_horizontal = Control.SIZE_EXPAND_FILL
	size_flags_vertical = Control.SIZE_EXPAND_FILL
	label_font = get_theme_default_font()
	_make_piece_textures()
	resized.connect(_on_board_resized)
	set_process(false)


func get_board_rect() -> Rect2:
	var side: float = maxf(0.0, minf(size.x, size.y) - 54.0)
	return Rect2((size - Vector2.ONE * side) / 2.0, Vector2.ONE * side)


func square_at_point(point: Vector2) -> String:
	var area := get_board_rect()
	if not area.has_point(point) or area.size.x <= 0.0:
		return ""
	var cell: float = area.size.x / 8.0
	return model.display_to_square(int((point.x - area.position.x) / cell),
		int((point.y - area.position.y) / cell))


func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_LEFT:
		var square := square_at_point(event.position)
		if event.pressed:
			pressed_square = square
			if not square.is_empty():
				square_pressed.emit(square)
		elif not square.is_empty() and square != pressed_square and not pressed_square.is_empty():
			drag_requested.emit(pressed_square, square)
			pressed_square = ""
		accept_event()


func _draw() -> void:
	var area := get_board_rect()
	if area.size.x <= 0.0:
		return
	var cell: float = area.size.x / 8.0
	draw_style_box(_frame_style(), area.grow(7.0))
	var pieces: Dictionary = state.get("board", {})
	var moving: bool = is_animating() and model.view_from_white == _motion_view and _motion_allowed()
	var hidden: Dictionary = {}
	if moving:
		for movement in _motion.movements:
			hidden[movement.to] = true
		if _motion.reverse and not _motion.capture.is_empty():
			hidden[_motion.capture.square] = true
	var last: String = state.get("last_move", "") if state.get("last_move") != null else ""
	var destinations: Dictionary = {}
	for move in state.get("legal_moves", []):
		if not model.selected_square.is_empty() and str(move).begins_with(model.selected_square):
			destinations[str(move).substr(2, 2)] = true
	for row in range(8):
		for column in range(8):
			var square := model.display_to_square(column, row)
			var bounds := Rect2(area.position + Vector2(column, row) * cell, Vector2.ONE * cell)
			var file_index: int = "abcdefgh".find(square[0])
			var is_dark: bool = (file_index + int(square[1])) % 2 == 1
			draw_rect(bounds, Color("749598") if is_dark else Color("dbe5de"))
			if not last.is_empty() and (square == last.substr(0, 2) or square == last.substr(2, 2)):
				draw_rect(bounds, Color(0.97, 0.79, 0.25, 0.34))
			if square == model.selected_square:
				draw_rect(bounds, Color(0.98, 0.77, 0.22, 0.55))
				draw_rect(bounds.grow(-2.0), Color("ffd578"), false, 3.0)
			if coach_squares.has(square):
				draw_rect(bounds.grow(-4.0), Color("79d9cd"), false, 3.0)
			var piece: String = str(pieces.get(square, ""))
			if state.get("in_check", false) and piece.to_lower() == "k":
				var color := "white" if piece == piece.to_upper() else "black"
				if color == state.get("turn", ""):
					draw_rect(bounds, Color(0.9, 0.2, 0.19, 0.53))
			if not hidden.has(square) and not piece.is_empty() and piece_textures.has(piece):
				var inset: float = cell * 0.08
				draw_texture_rect(piece_textures[piece], bounds.grow(-inset), false)
			if destinations.has(square):
				if piece.is_empty():
					draw_circle(bounds.get_center(), cell * 0.11, Color(0.08, 0.23, 0.25, 0.45))
				else:
					draw_arc(bounds.get_center(), cell * 0.43, 0, TAU, 48, Color(0.06, 0.25, 0.27, 0.7), cell * 0.055, true)
	if moving:
		_draw_motion(area, cell)
	_draw_hint(area, cell)
	_draw_move_badge()
	var font_size: int = clampi(int(cell * 0.20), 12, 18)
	for index in range(8):
		var x: float = area.position.x + (index + 0.5) * cell
		var y: float = area.position.y + (index + 0.5) * cell
		_center_text(model.file_label(index), Vector2(x, area.end.y + 22.0), font_size)
		_center_text(model.file_label(index), Vector2(x, area.position.y - 12.0), font_size)
		_center_text(model.rank_label(index), Vector2(area.position.x - 19.0, y + font_size * 0.35), font_size)
		_center_text(model.rank_label(index), Vector2(area.end.x + 19.0, y + font_size * 0.35), font_size)


func _piece_center(square: String, area: Rect2, cell: float) -> Vector2:
	return area.position + (Vector2(model.square_to_display(square)) + Vector2.ONE * 0.5) * cell


func move_badge_rect() -> Rect2:
	if move_annotation.is_empty() or is_animating():
		return Rect2()
	var square: String = str(move_annotation.get("square", ""))
	var uci: String = str(move_annotation.get("uci", ""))
	if move_annotation.get("scope") != "actual_move" or str(move_annotation.get("fen_after", "")).is_empty() or move_annotation.get("fen_after") != state.get("fen") or int(move_annotation.get("ply", -1)) != int(state.get("ply", -2)):
		return Rect2()
	if uci.length() not in [4, 5] or square != uci.substr(2, 2) or state.get("last_move") != uci or not state.get("board", {}).has(square):
		return Rect2()
	var coordinate: Vector2i = model.square_to_display(square)
	if coordinate.x < 0 or coordinate.y < 0:
		return Rect2()
	var area := get_board_rect()
	var cell: float = area.size.x / 8.0
	var radius: float = clampf(cell * 0.205, 10.0, 19.0)
	var center: Vector2 = area.position + (Vector2(coordinate) + Vector2(0.80, 0.17)) * cell
	center.x = clampf(center.x, area.position.x + radius + 2, area.end.x - radius - 2)
	center.y = clampf(center.y, area.position.y + radius + 2, area.end.y - radius - 2)
	return Rect2(center - Vector2.ONE * radius, Vector2.ONE * radius * 2.0)


func _draw_move_badge() -> void:
	var bounds := move_badge_rect()
	if bounds.size.x <= 0.0:
		return
	var colors := {"great": "42cbbb", "best": "8ecb6b", "good": "8daf8d", "inaccuracy": "e7bc58", "mistake": "e38c49", "blunder": "e76069", "allows_mate": "e76069", "missed_mate": "e38c49", "uncertain": "a5aebc"}
	var center := bounds.get_center()
	var radius: float = bounds.size.x / 2.0
	draw_circle(center + Vector2(0, 2), radius + 1, Color(0.03, 0.07, 0.11, 0.45))
	draw_circle(center, radius + 1.5, Color("142633"))
	draw_circle(center, radius, Color(colors.get(str(move_annotation.get("kind", "")), "a5aebc")))
	var text: String = str(move_annotation.get("symbol", ""))
	var font_size: int = clampi(int(bounds.size.x * 0.64), 13, 22)
	var extent: Vector2 = label_font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size)
	var baseline: float = (label_font.get_ascent(font_size) - label_font.get_descent(font_size)) * 0.5
	draw_string(label_font, center + Vector2(-extent.x * 0.5, baseline), text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size, Color("112633"))


func _draw_piece(piece: String, center: Vector2, cell: float, scale_factor: float = 1.0, opacity: float = 1.0) -> void:
	if not piece_textures.has(piece) or opacity <= 0.0:
		return
	var extent := Vector2.ONE * cell * 0.84 * scale_factor
	draw_texture_rect(piece_textures[piece], Rect2(center - extent / 2.0, extent), false, Color(1, 1, 1, opacity))


func _draw_motion(area: Rect2, cell: float) -> void:
	var travel := clampf(_motion_elapsed / _motion_duration, 0.0, 1.0)
	var eased := 1.0 - pow(1.0 - travel, 3.0)
	var landing := clampf((_motion_elapsed - _motion_duration) / LANDING_SECONDS, 0.0, 1.0)
	var capture: Dictionary = _motion.capture
	if not capture.is_empty():
		var fade := smoothstep(0.3, 0.95, travel)
		var opacity: float = fade if _motion.reverse else 1.0 - fade
		_draw_piece(str(capture.piece), _piece_center(str(capture.square), area, cell), cell, 0.84 + opacity * 0.16, opacity)
	var moving_pieces: Array = _motion.movements.duplicate()
	if moving_pieces.size() > 1:
		# The rook travels behind the king when their paths cross in castling.
		moving_pieces.reverse()
	for movement in moving_pieces:
		var origin := _piece_center(str(movement.from), area, cell)
		var target := _piece_center(str(movement.to), area, cell)
		var center: Vector2 = origin.lerp(target, eased)
		var lift: float = sin(PI * travel) * cell * (0.09 if str(movement.piece).to_lower() == "n" else 0.035)
		if moving_pieces.size() > 1:
			lift = sin(PI * travel) * cell * (0.16 if str(movement.piece).to_lower() == "k" else -0.07)
		center.y -= lift
		if landing > 0.0:
			draw_circle(target, cell * (0.32 + landing * 0.16), Color(1.0, 0.82, 0.36, (1.0 - landing) * 0.16))
		# A small shadow and settling pulse add weight without shaking the board.
		draw_set_transform(center + Vector2(0, cell * 0.33 + lift), 0.0, Vector2(1.0, 0.22))
		draw_circle(Vector2.ZERO, cell * 0.25, Color(0.06, 0.12, 0.17, (1.0 - landing) * 0.20))
		draw_set_transform(Vector2.ZERO)
		var scale_factor: float = 1.0 + sin(PI * landing) * 0.045
		if movement.piece != movement.arrival:
			var morph := smoothstep(0.62, 1.0, travel)
			_draw_piece(str(movement.piece), center, cell, scale_factor, 1.0 - morph)
			_draw_piece(str(movement.arrival), center, cell, scale_factor, morph)
		else:
			_draw_piece(str(movement.arrival), center, cell, scale_factor)


func hint_centers() -> PackedVector2Array:
	# Use the exact same square mapping as clicks, including the flipped view.
	if hint_move.length() < 4:
		return PackedVector2Array()
	var source := model.square_to_display(hint_move.substr(0, 2))
	var target := model.square_to_display(hint_move.substr(2, 2))
	if source.x < 0 or target.x < 0 or source == target:
		return PackedVector2Array()
	var area := get_board_rect()
	var cell: float = area.size.x / 8.0
	return PackedVector2Array([area.position + (Vector2(source) + Vector2.ONE * 0.5) * cell,
		area.position + (Vector2(target) + Vector2.ONE * 0.5) * cell])


func _draw_hint(_area: Rect2, cell: float) -> void:
	var points := hint_centers()
	if points.size() != 2:
		return
	var source: Vector2 = points[0]
	var target: Vector2 = points[1]
	var direction: Vector2 = (target - source).normalized()
	var cross := Vector2(-direction.y, direction.x)
	var tip: Vector2 = target - direction * cell * 0.08
	var neck: Vector2 = tip - direction * cell * 0.29
	# Painted directly onto this Control: no overlay can consume click or drag.
	var tint := Color(0.18, 0.78, 0.96, 0.87)
	draw_circle(source, cell * 0.22, Color(0.06, 0.22, 0.31, 0.8))
	draw_arc(source, cell * 0.22, 0.0, TAU, 40, tint, cell * 0.055, true)
	draw_line(source, neck, Color(0.04, 0.17, 0.22, 0.9), cell * 0.17, true)
	draw_line(source, neck, tint, cell * 0.11, true)
	draw_colored_polygon(PackedVector2Array([tip, neck + cross * cell * 0.19, neck - cross * cell * 0.19]), tint)


func _center_text(value: String, baseline: Vector2, font_size: int) -> void:
	var width: float = label_font.get_string_size(value, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
	draw_string(label_font, baseline - Vector2(width / 2.0, 0), value,
		HORIZONTAL_ALIGNMENT_LEFT, -1, font_size, Color("9bb0bf"))


func _frame_style() -> StyleBoxFlat:
	var frame := StyleBoxFlat.new()
	frame.bg_color = Color("293f4c")
	frame.set_corner_radius_all(5)
	return frame


func _make_piece_textures() -> void:
	# Original simple vector silhouettes, generated locally; no external chess art.
	var shapes: Dictionary = {
		"p": '<circle cx="50" cy="28" r="13"/><path d="M40 42 L35 65 Q29 69 30 75 H70 Q71 69 65 65 L60 42 Z"/><path d="M28 78 H72 L77 88 H23 Z"/>',
		"r": '<path d="M25 18 H36 V29 H44 V18 H56 V29 H64 V18 H75 V42 L66 48 V73 H34 V48 L25 42 Z"/><path d="M29 74 H71 L76 87 H24 Z"/><path d="M35 47 H65" fill="none"/>',
		"n": '<path d="M28 76 Q23 62 37 50 L27 49 L22 38 L46 19 L49 10 L59 20 Q76 27 73 50 L67 76 Z"/><path d="M28 78 H72 L77 88 H23 Z"/><path d="M39 39 L45 35 M47 23 Q59 35 45 51" fill="none"/>',
		"b": '<path d="M50 13 Q22 34 37 49 L42 55 L33 75 H67 L58 55 L63 49 Q78 34 50 13 Z"/><path d="M29 77 H71 L76 88 H24 Z"/><path d="M44 28 L56 42" fill="none"/><circle cx="50" cy="11" r="4"/>',
		"q": '<path d="M22 31 L32 69 H68 L78 31 L61 47 L50 25 L39 47 Z"/><path d="M32 71 H68 L72 79 H28 Z"/><path d="M26 81 H74 L78 89 H22 Z"/><circle cx="20" cy="26" r="5"/><circle cx="50" cy="19" r="5"/><circle cx="80" cy="26" r="5"/>',
		"k": '<path d="M46 10 H54 V19 H63 V27 H54 V38 H46 V27 H37 V19 H46 Z"/><path d="M49 43 Q24 29 27 51 Q30 60 37 66 L33 76 H67 L63 66 Q70 60 73 51 Q76 29 51 43 Z"/><path d="M28 79 H72 L77 89 H23 Z"/><path d="M38 65 H62" fill="none"/>'
	}
	for kind in shapes:
		for white in [true, false]:
			var fill := "#fff9e9" if white else "#26364b"
			var stroke := "#243444" if white else "#e3ece7"
			var svg := '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 100 100"><g fill="%s" stroke="%s" stroke-width="3" stroke-linejoin="round" stroke-linecap="round">%s</g></svg>' % [fill, stroke, shapes[kind]]
			var bitmap := Image.new()
			if bitmap.load_svg_from_string(svg, 2.0) == OK:
				piece_textures[str(kind).to_upper() if white else str(kind)] = ImageTexture.create_from_image(bitmap)
