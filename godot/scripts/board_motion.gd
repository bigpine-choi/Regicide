extends RefCounted
## Recognizes an already accepted position change; never decides legal moves.

static func snapshot(value: Dictionary) -> Dictionary:
	var result := {"board": value.get("board", {}).duplicate(), "turn": value.get("turn", ""),
		"last_move": value.get("last_move", ""), "game_id": value.get("game_id", ""),
		"ply": value.get("ply", null)}
	return result


static func _square(value: String) -> bool:
	return value.length() == 2 and "abcdefgh".contains(value[0]) and "12345678".contains(value[1])


static func _white(piece: String) -> bool:
	return piece == piece.to_upper()


static func _forward(before: Dictionary, after: Dictionary) -> Dictionary:
	var move: String = str(after.get("last_move", "")) if after.get("last_move") != null else ""
	if move.length() not in [4, 5] or not _square(move.substr(0, 2)) or not _square(move.substr(2, 2)):
		return {}
	var origin := move.substr(0, 2)
	var target := move.substr(2, 2)
	var pieces: Dictionary = before.get("board", {})
	var expected: Dictionary = pieces.duplicate()
	var piece: String = str(pieces.get(origin, ""))
	if piece.is_empty() or origin == target or before.get("turn") != ("white" if _white(piece) else "black"):
		return {}
	if after.get("turn") != ("black" if _white(piece) else "white"):
		return {}
	var captured: String = str(pieces.get(target, ""))
	if not captured.is_empty() and _white(captured) == _white(piece):
		return {}
	var capture := {"square": target, "piece": captured} if not captured.is_empty() else {}
	var arrival := piece
	if move.length() == 5:
		if piece.to_lower() != "p" or not "qrbn".contains(move[4]) or target[1] != ("8" if _white(piece) else "1"):
			return {}
		arrival = move[4].to_upper() if _white(piece) else move[4]
	expected.erase(origin)
	expected[target] = arrival
	var movements: Array = [{"from": origin, "to": target, "piece": piece, "arrival": arrival}]
	# En passant is the only accepted move removing a third, off-target piece.
	if piece.to_lower() == "p" and origin[0] != target[0] and captured.is_empty():
		var captured_square := target[0] + origin[1]
		var opponent := "p" if _white(piece) else "P"
		if pieces.get(captured_square, "") != opponent:
			return {}
		capture = {"square": captured_square, "piece": opponent}
		expected.erase(captured_square)
	# Standard castling moves both sprites in one visual transaction.
	if piece.to_lower() == "k" and origin[0] == "e" and target[0] in ["g", "c"] and origin[1] == target[1]:
		var rank := "1" if _white(piece) else "8"
		if origin[1] != rank:
			return {}
		var rook_from := ("h" if target[0] == "g" else "a") + rank
		var rook_to := ("f" if target[0] == "g" else "d") + rank
		var rook := "R" if _white(piece) else "r"
		if pieces.get(rook_from, "") != rook or pieces.has(rook_to):
			return {}
		expected.erase(rook_from)
		expected[rook_to] = rook
		movements.append({"from": rook_from, "to": rook_to, "piece": rook, "arrival": rook})
	if expected != after.get("board", {}):
		return {}
	return {"movements": movements, "capture": capture, "reverse": false, "move": move}


static func between(before: Dictionary, after: Dictionary) -> Dictionary:
	if before.get("game_id", "") != after.get("game_id", "") or before.get("board", {}) == after.get("board", {}):
		return {}
	# Archive/coaching frames carry explicit ply numbers. A jump is not a move.
	if before.get("ply") != null and after.get("ply") != null and absi(int(before.ply) - int(after.ply)) != 1:
		return {}
	var forward := _forward(before, after)
	if not forward.is_empty():
		return forward
	var reverse := _forward(after, before)
	if reverse.is_empty():
		return {}
	for movement in reverse.movements:
		var square: String = movement.from
		movement.from = movement.to
		movement.to = square
		var piece: String = movement.piece
		movement.piece = movement.arrival
		movement.arrival = piece
	reverse.reverse = true
	return reverse
