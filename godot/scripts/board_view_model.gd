class_name RegicideBoardViewModel
extends RefCounted
## Presentation state only. All legal moves and adjudication come from the host.

var view_from_white: bool = true
var selected_square: String = ""
var pending_promotions: Array[String] = []


func display_to_square(column: int, row: int) -> String:
	if column < 0 or column > 7 or row < 0 or row > 7:
		return ""
	var file_index: int = column if view_from_white else 7 - column
	var rank_number: int = 8 - row if view_from_white else row + 1
	return "abcdefgh"[file_index] + str(rank_number)


func square_to_display(square: String) -> Vector2i:
	if square.length() != 2:
		return Vector2i(-1, -1)
	var file_index: int = "abcdefgh".find(square[0])
	var rank_number: int = square.substr(1).to_int()
	if file_index < 0 or rank_number < 1 or rank_number > 8:
		return Vector2i(-1, -1)
	return Vector2i(file_index if view_from_white else 7 - file_index,
		8 - rank_number if view_from_white else rank_number - 1)


func file_label(column: int) -> String:
	var square := display_to_square(column, 0)
	return square[0] if not square.is_empty() else ""


func rank_label(row: int) -> String:
	var square := display_to_square(0, row)
	return square[1] if not square.is_empty() else ""


func flip_view() -> void:
	view_from_white = not view_from_white


func apply_state(next: Dictionary) -> void:
	# Never derive perspective from human_color, turn, revision or new-game state.
	if not selected_square.is_empty():
		var still_legal := false
		for move in next.get("legal_moves", []):
			if str(move).begins_with(selected_square):
				still_legal = true
				break
		if not still_legal:
			selected_square = ""
	if not pending_promotions.is_empty():
		var legal: Array = next.get("legal_moves", [])
		for move in pending_promotions:
			if not legal.has(move):
				pending_promotions.clear()
				break
