class_name RegicideChessBoard
extends Control
## Draws the server position. Rotating the board changes only square mapping.

signal square_pressed(square: String)
signal drag_requested(from_square: String, to_square: String)

const ViewModel = preload("res://scripts/board_view_model.gd")
var model: ViewModel = ViewModel.new()
var state: Dictionary = {}
var piece_textures: Dictionary = {}
var label_font: Font
var pressed_square: String = ""
var hint_move: String = ""


func _ready() -> void:
	mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	custom_minimum_size = Vector2(400, 400)
	size_flags_horizontal = Control.SIZE_EXPAND_FILL
	size_flags_vertical = Control.SIZE_EXPAND_FILL
	label_font = get_theme_default_font()
	_make_piece_textures()
	resized.connect(queue_redraw)


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
			var piece: String = str(pieces.get(square, ""))
			if state.get("in_check", false) and piece.to_lower() == "k":
				var color := "white" if piece == piece.to_upper() else "black"
				if color == state.get("turn", ""):
					draw_rect(bounds, Color(0.9, 0.2, 0.19, 0.53))
			if not piece.is_empty() and piece_textures.has(piece):
				var inset: float = cell * 0.08
				draw_texture_rect(piece_textures[piece], bounds.grow(-inset), false)
			if destinations.has(square):
				if piece.is_empty():
					draw_circle(bounds.get_center(), cell * 0.11, Color(0.08, 0.23, 0.25, 0.45))
				else:
					draw_arc(bounds.get_center(), cell * 0.43, 0, TAU, 48, Color(0.06, 0.25, 0.27, 0.7), cell * 0.055, true)
	_draw_hint(area, cell)
	var font_size: int = clampi(int(cell * 0.20), 12, 18)
	for index in range(8):
		var x: float = area.position.x + (index + 0.5) * cell
		var y: float = area.position.y + (index + 0.5) * cell
		_center_text(model.file_label(index), Vector2(x, area.end.y + 22.0), font_size)
		_center_text(model.file_label(index), Vector2(x, area.position.y - 12.0), font_size)
		_center_text(model.rank_label(index), Vector2(area.position.x - 19.0, y + font_size * 0.35), font_size)
		_center_text(model.rank_label(index), Vector2(area.end.x + 19.0, y + font_size * 0.35), font_size)


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
