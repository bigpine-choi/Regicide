extends Control
## Original code-drawn rook companion. No external artwork or chess evaluation.

var mood: String = "encourage":
	set(value):
		mood = value
		queue_redraw()


func _ready() -> void:
	custom_minimum_size = Vector2(78, 100)
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	resized.connect(queue_redraw)


func _rounded(rect: Rect2, color: Color, radius: int, border: Color = Color.TRANSPARENT) -> void:
	var style := StyleBoxFlat.new()
	style.bg_color = color
	style.set_corner_radius_all(radius)
	if border.a > 0.0:
		style.border_color = border
		style.set_border_width_all(2)
	draw_style_box(style, rect)


func _draw() -> void:
	var scale: float = minf(size.x / 78.0, size.y / 100.0)
	draw_set_transform(Vector2((size.x - 78.0 * scale) / 2.0, 0), 0.0, Vector2.ONE * scale)
	var ink := Color("173b46")
	var cream := Color("f4e9c9")
	var teal := Color("77c8b5")
	var gold := Color("e5b95e")
	draw_circle(Vector2(39, 49), 35, Color("203e4a"))
	draw_ellipse_shadow()
	# A small chess rook, a scarf and an open notebook form its silhouette.
	draw_colored_polygon(PackedVector2Array([Vector2(25, 55), Vector2(17, 83), Vector2(55, 83), Vector2(51, 55)]), Color("488f87"))
	_rounded(Rect2(20, 31, 39, 38), cream, 9, ink)
	_rounded(Rect2(17, 17, 45, 23), cream, 4, ink)
	for x in [26, 43]:
		draw_rect(Rect2(x, 16, 9, 10), Color("203e4a"))
	draw_line(Vector2(20, 38), Vector2(59, 38), gold, 3.0, true)
	if mood == "celebrate":
		draw_arc(Vector2(30, 47), 4, PI, TAU, 12, ink, 2.4, true)
		draw_arc(Vector2(48, 47), 4, PI, TAU, 12, ink, 2.4, true)
	else:
		draw_circle(Vector2(30, 46), 2.8, ink)
		draw_circle(Vector2(48, 46), 2.8, ink)
		if mood == "warning":
			draw_line(Vector2(26, 40), Vector2(33, 42), ink, 2.0, true)
			draw_line(Vector2(45, 42), Vector2(52, 40), ink, 2.0, true)
		elif mood == "think":
			draw_line(Vector2(44, 39), Vector2(52, 39), ink, 2.0, true)
	draw_circle(Vector2(24, 53), 3.0, Color("e1a98b"))
	draw_circle(Vector2(54, 53), 3.0, Color("e1a98b"))
	if mood == "think":
		draw_line(Vector2(36, 56), Vector2(42, 56), ink, 1.8, true)
	else:
		draw_arc(Vector2(39, 53), 6, 0.2, PI - 0.2, 16, ink, 2.0, true)
	_rounded(Rect2(23, 63, 33, 8), teal, 3, ink)
	draw_colored_polygon(PackedVector2Array([Vector2(48, 67), Vector2(57, 68), Vector2(60, 81), Vector2(51, 78)]), teal)
	_rounded(Rect2(15, 79, 49, 9), cream, 3, ink)
	# The notebook makes this rook a coach rather than a playable board piece.
	draw_colored_polygon(PackedVector2Array([Vector2(30, 72), Vector2(42, 76), Vector2(55, 72), Vector2(55, 87), Vector2(42, 91), Vector2(30, 87)]), Color("fcf7e9"))
	draw_polyline(PackedVector2Array([Vector2(30, 72), Vector2(42, 76), Vector2(55, 72), Vector2(55, 87), Vector2(42, 91), Vector2(30, 87), Vector2(30, 72)]), ink, 1.8, true)
	draw_line(Vector2(42, 76), Vector2(42, 90), Color("8dada7"), 1.4, true)
	if mood == "celebrate":
		_star(Vector2(66, 16), 7, gold)
		_star(Vector2(10, 38), 4, gold)
	elif mood == "think":
		for index in range(3):
			draw_circle(Vector2(62 + index * 5, 11 - index * 3), 1.8, teal)
	elif mood == "warning":
		draw_circle(Vector2(66, 19), 7.5, gold)
		draw_line(Vector2(66, 14), Vector2(66, 20), ink, 2.0, true)
		draw_circle(Vector2(66, 23), 1.2, ink)


func draw_ellipse_shadow() -> void:
	draw_set_transform_matrix(get_transform_for_shadow())
	draw_circle(Vector2.ZERO, 1.0, Color(0.01, 0.04, 0.06, 0.35))
	var scale: float = minf(size.x / 78.0, size.y / 100.0)
	draw_set_transform(Vector2((size.x - 78.0 * scale) / 2.0, 0), 0.0, Vector2.ONE * scale)


func get_transform_for_shadow() -> Transform2D:
	var scale: float = minf(size.x / 78.0, size.y / 100.0)
	return Transform2D(Vector2(27 * scale, 0), Vector2(0, 5 * scale), Vector2((size.x - 78.0 * scale) / 2.0 + 39 * scale, 94 * scale))


func _star(center: Vector2, radius: float, color: Color) -> void:
	var points := PackedVector2Array()
	for index in range(8):
		var angle: float = float(index) * PI / 4.0 - PI / 2.0
		points.append(center + Vector2(cos(angle), sin(angle)) * (radius if index % 2 == 0 else radius * 0.35))
	draw_colored_polygon(points, color)
