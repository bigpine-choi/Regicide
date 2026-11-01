extends "res://scripts/chess_board.gd"
## Free editing has transactional native drags; ordinary move input is inherited.

var editor_host: Variant
var _editor_drag_started: bool = false
var _editor_press_position: Vector2 = Vector2.ZERO


func _motion_allowed() -> bool:
	return editor_host == null or not editor_host.editing


func _gui_input(event: InputEvent) -> void:
	if editor_host == null or not editor_host.editing:
		super(event)
		return
	if event is InputEventMouseMotion and (event.button_mask & MOUSE_BUTTON_MASK_LEFT or Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT)) and not _editor_drag_started and not pressed_square.is_empty() and event.position.distance_to(_editor_press_position) >= 8.0 and not get_viewport().gui_is_dragging():
		# Some pointer integrations report position changes with zero relative
		# motion. Start from the captured press position in that case as well.
		var piece: String = str(editor_host.editor_pieces.get(pressed_square, ""))
		var payload: Dictionary = editor_host.begin_editor_drag("board", piece, pressed_square)
		if not payload.is_empty():
			_editor_drag_started = true
			force_drag(payload, editor_host.editor_drag_preview(piece))
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_LEFT:
		var square := square_at_point(event.position)
		if event.pressed:
			pressed_square = square
			_editor_press_position = event.position
			_editor_drag_started = false
		elif not _editor_drag_started and square == pressed_square and not square.is_empty():
			# Painting on press would replace the source before a drag can begin.
			square_pressed.emit(square)
		if not event.pressed:
			pressed_square = ""
		accept_event()


func _get_drag_data(at_position: Vector2) -> Variant:
	if editor_host == null or not editor_host.editing or editor_host.busy:
		return null
	var square := square_at_point(at_position)
	var piece: String = str(editor_host.editor_pieces.get(square, ""))
	if square.is_empty() or piece.is_empty():
		return null
	var payload: Dictionary = editor_host.begin_editor_drag("board", piece, square)
	if payload.is_empty():
		return null
	_editor_drag_started = true
	if get_viewport().gui_is_dragging():
		set_drag_preview(editor_host.editor_drag_preview(piece))
	return payload


func _can_drop_data(at_position: Vector2, data: Variant) -> bool:
	return editor_host != null and editor_host.can_editor_drop(data) and not square_at_point(at_position).is_empty()


func _drop_data(at_position: Vector2, data: Variant) -> void:
	if _can_drop_data(at_position, data):
		editor_host.drop_editor_piece(data, square_at_point(at_position))


func cancel_editor_press() -> void:
	pressed_square = ""
	# Suppress any release belonging to a cancelled native drag.
	_editor_drag_started = true


class PalettePiece extends Button:
	var editor_host: Variant
	var piece: String = ""
	var _pointer_pressed: bool = false
	var _press_position: Vector2 = Vector2.ZERO

	func _gui_input(event: InputEvent) -> void:
		if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_LEFT:
			_pointer_pressed = event.pressed
			if event.pressed:
				_press_position = event.position
		elif event is InputEventMouseMotion and _pointer_pressed and (event.button_mask & MOUSE_BUTTON_MASK_LEFT or Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT)) and event.position.distance_to(_press_position) >= 8.0 and not get_viewport().gui_is_dragging():
			var payload: Dictionary = editor_host.begin_editor_drag("palette", piece)
			if not payload.is_empty():
				_pointer_pressed = false
				force_drag(payload, editor_host.editor_drag_preview(piece))

	func cancel_editor_press() -> void:
		_pointer_pressed = false

	func _get_drag_data(_at_position: Vector2) -> Variant:
		if editor_host == null:
			return null
		var payload: Dictionary = editor_host.begin_editor_drag("palette", piece)
		if payload.is_empty():
			return null
		if get_viewport().gui_is_dragging():
			set_drag_preview(editor_host.editor_drag_preview(piece))
		return payload


class TrashArea extends PanelContainer:
	var editor_host: Variant

	func _can_drop_data(_at_position: Vector2, data: Variant) -> bool:
		return editor_host != null and editor_host.can_editor_drop(data) and str(data.get("origin", "")) == "board"

	func _drop_data(_at_position: Vector2, data: Variant) -> void:
		if _can_drop_data(Vector2.ZERO, data):
			editor_host.discard_editor_piece(data)
