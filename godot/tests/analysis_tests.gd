extends SceneTree
## Synthetic UI input plus optional real host; manual validation is separate.

var app: Variant
var panel: Variant
var checks: int = 0
var failures: int = 0
var evidence_dir: String = ""


func _initialize() -> void:
	_run.call_deferred()


func verify(value: bool, message: String) -> void:
	checks += 1
	if not value:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, message: String, seconds: float = 12.0) -> bool:
	var deadline: int = Time.get_ticks_msec() + int(seconds * 1000)
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + message + " · " + str(panel._error))
			return false
		await create_timer(0.01).timeout
	verify(true, message)
	return true


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(evidence_dir.path_join(name + ".png")) == OK, "saved " + name)


func square_center(square: String) -> Vector2:
	var coordinate: Vector2i = panel.model.square_to_display(square)
	var area: Rect2 = panel.board.get_board_rect()
	return area.position + (Vector2(coordinate) + Vector2.ONE * 0.5) * area.size.x / 8.0


func board_button(square: String, pressed: bool) -> void:
	var event := InputEventMouseButton.new()
	event.position = square_center(square)
	event.button_index = MOUSE_BUTTON_LEFT
	event.pressed = pressed
	panel.board._gui_input(event)


func _editor_drag_tests() -> void:
	panel._load_preset(panel.START_FEN)
	panel._choose_piece("Q", "백 퀸")
	var before: String = panel.editor_fen()
	board_button("e2", true)
	verify(panel.editor_fen() == before and panel.editor_pieces.e2 == "P", "edit mouse press never paints over drag source")
	board_button("e2", false)
	verify(panel.editor_pieces.e2 == "Q", "edit click paints only on mouse release")
	panel._load_preset(panel.START_FEN)
	board_button("e2", true)
	var data: Dictionary = panel.board._get_drag_data(square_center("e2"))
	verify(panel.editor_fen() == panel.START_FEN, "begin board drag preserves source and metadata")
	verify(panel.board._can_drop_data(square_center("e4"), data), "board accepts valid edit drag on a logical square")
	panel.board._drop_data(square_center("e4"), data)
	verify(not panel.editor_pieces.has("e2") and panel.editor_pieces.get("e4") == "P", "board drop moves source exactly once")
	verify(panel.editor_fen().ends_with("w KQkq - 0 1"), "unrelated drag preserves rights while resetting EP and halfmove clock")
	data = panel.begin_editor_drag("board", "P", "e4")
	panel.board._drop_data(square_center("d7"), data)
	verify(not panel.editor_pieces.has("e4") and panel.editor_pieces.d7 == "P", "edit drag replaces occupied destination")
	panel._set_editor_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 7 9")
	before = panel.editor_fen()
	data = panel.begin_editor_drag("board", "R", "a1")
	panel.board._drop_data(square_center("a1"), data)
	verify(panel.editor_fen() == before, "same-square drop is exact no-op including rights and clocks")
	data = panel.begin_editor_drag("palette", "R")
	panel.board._drop_data(square_center("a1"), data)
	verify(panel.editor_fen() == before, "identical palette piece drop preserves exact FEN metadata")
	var outside_board: Vector2 = panel.return_button.get_global_rect().get_center()
	data = panel.begin_editor_drag("board", "R", "a1")
	var escape := InputEventKey.new()
	escape.keycode = KEY_ESCAPE
	escape.pressed = true
	panel._input(escape)
	panel.finish_editor_drag(false, outside_board)
	verify(panel.editor_fen() == before and panel.board.pressed_square.is_empty(), "Esc cancels source and metadata without a stale press")
	data = panel.begin_editor_drag("board", "R", "a1")
	panel.finish_editor_drag(false, Vector2(-100, -100))
	verify(panel.editor_fen() == before, "release outside app preserves exact draft")
	data = panel.palette_buttons["n"]._get_drag_data(Vector2.ZERO)
	verify(panel.board._can_drop_data(square_center("c6"), data), "palette payload accepted on board")
	panel.board._drop_data(square_center("c6"), data)
	verify(panel.editor_pieces.c6 == "n" and panel.editor_pieces.a1 == "R", "palette creates copy without removing board source")
	before = panel.editor_fen()
	data = panel.begin_editor_drag("palette", "Q")
	panel.finish_editor_drag(false, outside_board)
	verify(panel.editor_fen() == before, "palette outside board cancels without deleting a piece")
	data = panel.begin_editor_drag("board", "n", "c6")
	verify(panel.trash_area._can_drop_data(Vector2.ZERO, data), "visible trash accepts board piece")
	panel.trash_area._drop_data(Vector2.ZERO, data)
	verify(not panel.editor_pieces.has("c6"), "trash deletes committed board source")
	data = panel.begin_editor_drag("board", "R", "a1")
	panel.finish_editor_drag(false, outside_board)
	verify(not panel.editor_pieces.has("a1"), "completed in-view drop outside board deletes source")
	panel.model.view_from_white = false
	data = panel.begin_editor_drag("palette", "N")
	panel.board._drop_data(square_center("b3"), data)
	verify(panel.editor_pieces.get("b3") == "N" and panel.board.square_at_point(square_center("b3")) == "b3", "flipped view keeps correct drag destination")
	data = panel.begin_editor_drag("board", "N", "b3")
	panel.flip_view()
	verify(not panel.can_editor_drop(data), "view rotation cancels in-progress editor drag")
	var preview: Control = panel.editor_drag_preview("N")
	verify(preview.get_child(0).texture == panel.board.piece_textures.get("N"), "drag preview uses selected piece texture")
	preview.free()
	data = panel.begin_editor_drag("board", "N", "b3")
	panel._load_preset(panel.START_FEN)
	verify(not panel.can_editor_drop(data), "preset invalidates old drag payload")
	data = panel.begin_editor_drag("board", "P", "e2")
	panel._set_editor_fen(panel.START_FEN)
	verify(not panel.can_editor_drop(data), "FEN import invalidates old drag payload")
	data = panel.begin_editor_drag("board", "P", "e2")
	panel._choose_piece("q", "흑 퀸")
	verify(not panel.can_editor_drop(data), "brush change cancels drag payload")
	data = panel.begin_editor_drag("board", "P", "e2")
	panel.busy = true
	panel._render()
	verify(not panel.can_editor_drop(data), "busy mutation invalidates pending edit drag")
	panel.busy = false
	data = panel.begin_editor_drag("board", "P", "e2")
	panel.editing = false
	panel._render()
	verify(not panel.can_editor_drop(data) and panel.begin_editor_drag("palette", "Q").is_empty(), "analysis mode cannot accept stale editor drag")
	panel.editing = true
	panel.model.view_from_white = true
	panel._load_source()


func _evaluation_tests(position: Dictionary) -> void:
	var current: Dictionary = position.duplicate(true)
	var value: Dictionary = {"game_id": current.game_id, "revision": current.revision,
		"fen": current.fen, "turn": current.turn, "status": "ready", "cp": 250,
		"mate": null, "depth": 4, "bound": "exact", "white_share": null,
		"winner": null, "result": "*", "source": "stockfish_gauge", "source_fen": current.fen}
	current.reference_evaluation = value.duplicate(true)
	panel.accept_state(current)
	verify(panel.evaluation_bar.has_score and panel.evaluation_summary.text.contains("백 우세") and panel.evaluation_summary.text.contains("+2.50"), "positive analysis score is always white advantage")
	verify(panel.evaluation_detail.text.contains("Stockfish") and panel.evaluation_detail.text.contains("승률이 아닙니다"), "reference identifies Stockfish source and estimate scale")
	var prior_text: String = panel.evaluation_summary.text
	panel.model.view_from_white = true
	panel._render()
	verify(is_equal_approx(panel.evaluation_bar.white_rect().end.y, panel.evaluation_bar.bar_rect().end.y), "white view anchors score fill at bottom")
	panel.flip_view()
	verify(panel.evaluation_summary.text == prior_text and is_equal_approx(panel.evaluation_bar.white_rect().position.y, panel.evaluation_bar.bar_rect().position.y), "analysis flip changes fill orientation without changing score sign")
	current.reference_evaluation.status = "ready"
	current.reference_evaluation.cp = -175
	panel.accept_state(current)
	verify(panel.evaluation_summary.text.contains("흑 우세") and panel.evaluation_summary.text.contains("-1.75") and panel.evaluation_bar.white_share < 0.5, "negative completed analysis score is black advantage")
	current.reference_evaluation.cp = 0
	panel.accept_state(current)
	verify(panel.evaluation_bar.has_score and panel.evaluation_summary.text.contains("0.00"), "real zero is balanced rather than missing")
	current.reference_evaluation.status = "searching"
	current.reference_evaluation.cp = null
	panel.accept_state(current)
	verify(not panel.evaluation_bar.has_score and panel.evaluation_summary.text.contains("계산 중"), "search before first depth never invents zero")
	current.reference_evaluation.status = "ready"
	for mate in [3, -4]:
		current.reference_evaluation.mate = mate
		panel.accept_state(current)
		verify(panel.evaluation_summary.text.contains("M%d" % absi(mate)) and panel.evaluation_bar.white_share == (1.0 if mate > 0 else 0.0), "signed mate prediction keeps white perspective")
	current.reference_evaluation.mate = 0
	current.reference_evaluation.winner = "black"
	panel.accept_state(current)
	verify(panel.evaluation_summary.text == "Stockfish · 흑 체크메이트", "mate zero is a decisive score rather than draw")
	current.reference_evaluation.mate = null
	current.reference_evaluation.status = "terminal"
	current.reference_evaluation.result = "1/2-1/2"
	current.reference_evaluation.source = "chess_rules"
	panel.accept_state(current)
	verify(panel.evaluation_summary.text == "무승부" and is_equal_approx(panel.evaluation_bar.white_share, 0.5), "terminal draw has explicit balanced result")
	for field in ["game_id", "revision", "fen", "turn"]:
		current.reference_evaluation = value.duplicate(true)
		current.reference_evaluation[field] = -1 if field == "revision" else "stale"
		panel.accept_state(current)
		verify(not panel.evaluation_bar.has_score, "analysis rejects stale evaluation " + field)
	current.reference_evaluation = value.duplicate(true)
	current.reference_evaluation.bound = "lower"
	panel.accept_state(current)
	verify(not panel.evaluation_bar.has_score and panel.evaluation_summary.text.contains("계산 중"), "bounded reference never substitutes for exact Stockfish score")
	panel.editing = true
	panel._render()
	verify(not panel.evaluation_bar.has_score, "editor clears old evaluation immediately")
	panel.editing = false
	panel.busy = true
	panel._render()
	verify(not panel.evaluation_bar.has_score, "move request clears old evaluation immediately")
	panel.busy = false
	panel._error = "연결 오류"
	panel._render()
	verify(not panel.evaluation_bar.has_score and panel.evaluation_detail.text.contains("연결 오류"), "connection failure clears score with a reason")
	panel._error = ""
	current.active = false
	panel.accept_state(current)
	verify(not panel.evaluation_bar.has_score, "stopped analysis cannot retain a score")
	panel.model.view_from_white = true
	panel.accept_state(position)


func _castling_editor_tests() -> void:
	panel._set_editor_fen(panel.START_FEN)
	panel._choose_piece("", "지우개")
	panel._on_square("a2")
	panel._choose_piece("P", "백 폰")
	panel._on_square("a2")
	verify(panel.editor_fields[1] == "KQkq", "pawn edit and restoration preserve all existing castling rights")
	panel._choose_piece("", "지우개")
	panel._on_square("b1")
	panel._on_square("f1")
	verify(panel.editor_fields[1] == "KQkq", "clearing knight and bishop does not discard castling rights")
	var moved: Dictionary = panel.begin_editor_drag("board", "R", "h1")
	panel.drop_editor_piece(moved, "h3")
	verify(panel.editor_fields[1] == "Qkq", "moving kingside home rook prunes only its own right")
	moved = panel.begin_editor_drag("board", "R", "h3")
	panel.drop_editor_piece(moved, "h1")
	verify(panel.editor_fields[1] == "Qkq", "returning rook never restores lost right")
	moved = panel.begin_editor_drag("board", "K", "e1")
	panel.drop_editor_piece(moved, "e3")
	verify(panel.editor_fields[1] == "kq", "moving king prunes current-color rights without changing opponent")
	moved = panel.begin_editor_drag("board", "K", "e3")
	panel.drop_editor_piece(moved, "e1")
	verify(panel.editor_fields[1] == "kq", "returning king never restores lost rights")
	panel._set_editor_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 7 9")
	panel._render()
	verify(panel.castling_checks.size() == 4, "four explicit castling rights available")
	for right in "KQkq":
		verify(panel.castling_checks[right].button_pressed and not panel.castling_checks[right].disabled, "FEN round trip syncs castling right " + right)
	panel._change_castling_right(false, "K")
	verify(panel.editor_fields[1] == "Qkq", "unchecking one right preserves the other three")
	panel._change_castling_right(true, "K")
	verify(panel.editor_fields[1] == "KQkq", "manual rights keep canonical FEN ordering")
	panel._set_editor_fen("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
	panel._render()
	verify(panel.castling_checks["K"].disabled and panel.castling_checks["Q"].disabled and panel.castling_checks["k"].disabled and panel.castling_checks["q"].disabled, "missing rooks disable all rights")
	panel._choose_piece("R", "백 룩")
	panel._on_square("h1")
	verify(not panel.castling_checks["K"].disabled and not panel.castling_checks["K"].button_pressed and panel.editor_fields[1] == "-", "placing home rook enables explicit setting without inferring a right")
	panel._change_castling_right(true, "K")
	verify(panel.editor_fields[1] == "K" and panel.editor_fen().contains(" w K - "), "new custom setup can grant chosen castling right")
	panel._choose_piece("", "지우개")
	panel._on_square("h1")
	verify(panel.editor_fields[1] == "-" and panel.castling_checks["K"].disabled, "piece editing resets castling right and disables missing rook")
	var invalid_fen := "4k3/8/8/8/8/8/8/4K3 w K - 0 1"
	panel._set_editor_fen(invalid_fen)
	panel._render()
	verify(panel.editor_fen() == invalid_fen and panel.castling_checks["K"].button_pressed and panel.castling_checks["K"].disabled, "rendering preserves invalid imported rights for semantic backend validation")
	panel._change_castling_right(false, "K")
	verify(panel.editor_fen() == invalid_fen, "missing rook blocks checkbox mutation rather than silently repairing FEN")
	panel._load_source()


func _synthetic() -> void:
	app.offline_mode = true
	root.add_child(app)
	await process_frame
	app.backend_url = "http://127.0.0.1:1"
	var initial: Dictionary = {"fen": panel.START_FEN} if panel != null else {"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"}
	app.accept_state(initial)
	app._on_analysis_pressed()
	panel = app.analysis_overlay
	await process_frame
	await process_frame
	verify(panel.visible and not app.main_contents.visible, "analysis opens as separate view")
	verify(panel.editor_fen() == initial.fen, "entry copies live FEN")
	verify(panel.palette_buttons.size() == 13, "12 pieces and eraser available")
	panel._load_preset("8/8/8/8/8/8/8/8 w - - 0 1")
	panel._choose_piece("K", "백 왕")
	panel._on_square("e1")
	panel._choose_piece("k", "흑 왕")
	panel._on_square("e8")
	panel._choose_piece("Q", "백 퀸")
	panel._on_square("d4")
	verify(panel.editor_pieces == {"e1": "K", "e8": "k", "d4": "Q"}, "palette places and overwrites without chess rules")
	panel._choose_piece("", "지우개")
	panel._on_square("d4")
	verify(not panel.editor_pieces.has("d4"), "eraser removes chosen square")
	panel._change_turn(1)
	verify(panel.editor_fen() == "4k3/8/8/8/8/8/8/4K3 b - - 0 1", "side to move is encoded")
	var prior: String = panel.editor_fen()
	verify(not panel._set_editor_fen("bad FEN") and panel.editor_fen() == prior, "malformed FEN import is atomic")
	panel._error = ""
	verify(panel._set_editor_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 3 10"), "FEN import accepts metadata")
	verify(panel.editor_fen().ends_with("w KQkq - 3 10"), "castling and clocks round trip")
	panel._choose_piece("Q", "백 퀸")
	panel._on_square("a2")
	verify(panel.editor_fen().ends_with("w KQkq - 0 10"), "unrelated manual placement preserves rights and resets EP clock")
	panel._load_source()
	verify(panel.editor_fen() == initial.fen, "restore source position")
	_editor_drag_tests()
	_castling_editor_tests()
	var current: Dictionary = {"game_id": "analysis-fixture", "revision": 2, "active": true,
		"fen": "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1", "board": {"e1": "K", "e8": "k", "e2": "P"},
		"turn": "white", "legal_moves": ["e2e3", "e2e4"], "can_undo": false, "moves": [], "message": "분석 중"}
	current.hint = {"game_id": current.game_id, "revision": current.revision, "fen": current.fen,
		"status": "ready", "move": "e2e4", "san": "e4", "message": ""}
	panel.editing = false
	panel.accept_state(current)
	verify(panel.board.hint_move == "e2e4" and panel.recommendation.text.contains("e4"), "matching recommendation arrow and text")
	var points: PackedVector2Array = panel.board.hint_centers()
	verify(points.size() == 2 and points[0].y > points[1].y, "white recommendation geometry")
	panel.flip_view()
	points = panel.board.hint_centers()
	verify(points.size() == 2 and points[0].y < points[1].y, "flipped analysis keeps logical coordinates")
	panel._on_square("e2")
	verify(panel.model.selected_square == "e2", "manual legal source selectable")
	current.revision = 3
	panel.accept_state(current)
	verify(panel.board.hint_move.is_empty() and panel.apply_button.disabled, "stale hint cannot be applied")
	current.hint.revision = 3
	panel.accept_state(current)
	verify(panel.board.hint_move == "e2e4", "new revision restores matching hint")
	_evaluation_tests(current)
	var castle_position: Dictionary = current.duplicate(true)
	castle_position.status = "active"
	castle_position.legal_moves = ["e1g1", "e1c1"]
	panel.accept_state(castle_position)
	verify(not panel.castling_buttons.king.disabled and not panel.castling_buttons.queen.disabled, "castling action buttons use supplied current-side legal moves")
	panel.model.pending_promotions.clear()
	panel.model.pending_promotions.append("a7a8q")
	panel._render()
	verify(panel.castling_buttons.king.disabled and panel.castling_buttons.queen.disabled, "pending promotion disables castle actions")
	panel.model.pending_promotions.clear()
	panel.accept_state(current)
	panel.model.selected_square = ""
	panel._poll_in_flight = true
	panel._on_square("e2")
	verify(panel.model.selected_square == "e2" and not panel.busy, "in-flight snapshot never blocks a source click")
	panel.accept_state(current)
	verify(panel.model.selected_square == "e2", "same-position snapshot preserves selection")
	panel._poll_in_flight = false
	await capture("analysis-synthetic")
	verify(app.state == initial, "analysis never changes live state")
	await panel._return_to_game()
	verify(not panel.visible and app.main_contents.visible and app.state == initial, "return restores live view and data")
	app._on_analysis_pressed()
	panel.offline_mode = false
	await panel._return_to_game()
	verify(not panel.visible and app.main_contents.visible and not app.error_message.is_empty(), "disconnected analysis can return with a visible connection error")


func _live_castling_tests() -> void:
	panel.poll_timer.stop()
	await wait_for(func(): return not panel._poll_in_flight, "snapshot idle before castling cases")
	for color in ["white", "black"]:
		for side in ["king", "queen"]:
			if not panel.editing:
				await panel._edit_position()
			var turn_code: String = "w" if color == "white" else "b"
			panel._load_preset("r3k2r/8/8/8/8/8/8/R3K2R " + turn_code + " - - 0 1")
			for right in "KQkq":
				panel._change_castling_right(true, right)
			var initial_fen: String = panel.editor_fen()
			verify(panel.editor_fields[1] == "KQkq", "custom setup explicitly grants all four rights")
			panel.time_option.select(0)
			await panel._start_analysis()
			verify(not panel.editing and not panel.castling_buttons.king.disabled and not panel.castling_buttons.queen.disabled, color + " both castle buttons reflect legal moves")
			var revision: int = int(panel.analysis_state.revision)
			panel._castle(side)
			await wait_for(func(): return not panel.busy and int(panel.analysis_state.revision) > revision, color + " " + side + " castle applied")
			var rank_number: String = "1" if color == "white" else "8"
			var king_piece: String = "K" if color == "white" else "k"
			var rook_piece: String = "R" if color == "white" else "r"
			var king_target: String = ("g" if side == "king" else "c") + rank_number
			var rook_target: String = ("f" if side == "king" else "d") + rank_number
			var rook_source: String = ("h" if side == "king" else "a") + rank_number
			verify(panel.analysis_state.board.get(king_target) == king_piece and panel.analysis_state.board.get(rook_target) == rook_piece and not panel.analysis_state.board.has("e" + rank_number) and not panel.analysis_state.board.has(rook_source), "castling moves king and correct rook together")
			verify(str(panel.analysis_state.fen).get_slice(" ", 2) == ("kq" if color == "white" else "KQ"), "castling consumes both current-color rights only")
			await panel._mutate("undo", {})
			verify(panel.analysis_state.fen == initial_fen and not panel.castling_buttons.king.disabled and not panel.castling_buttons.queen.disabled, "undo restores exact custom FEN, king, rook and rights")
			if color == "white" and side == "king":
				revision = int(panel.analysis_state.revision)
				board_button("e1", true)
				board_button("g1", false)
				await wait_for(func(): return not panel.busy and int(panel.analysis_state.revision) > revision, "king two-square drag still castles")
				verify(panel.analysis_state.board.get("g1") == "K" and panel.analysis_state.board.get("f1") == "R", "legal king drag moves castle rook automatically")
				await panel._mutate("undo", {})
				verify(panel.analysis_state.fen == initial_fen, "drag castle undo restores full FEN")
	var forbidden: Array = [
		["check", "1k2r3/8/8/8/8/8/8/R3K2R w KQ - 0 1", true, true],
		["blocked", "1k6/8/8/8/8/8/8/R3KB1R w KQ - 0 1", true, false],
		["queenside blocked", "1k6/8/8/8/8/8/8/R2BK2R w KQ - 0 1", false, true],
		["attacked transit", "1k3r2/8/8/8/8/8/8/R3K2R w KQ - 0 1", true, false],
		["queenside attacked transit", "1k1r4/8/8/8/8/8/8/R3K2R w KQ - 0 1", false, true],
		["black check", "r3k2r/8/8/8/8/8/8/1K2R3 b kq - 0 1", true, true],
		["black attacked transit", "r3k2r/8/8/8/8/8/8/1K3R2 b kq - 0 1", true, false],
		["black queenside attacked transit", "r3k2r/8/8/8/8/8/8/1K1R4 b kq - 0 1", false, true]]
	for item in forbidden:
		await panel._edit_position()
		panel._load_preset(str(item[1]))
		await panel._start_analysis()
		verify(not panel.editing and panel.castling_buttons.king.disabled == item[2] and panel.castling_buttons.queen.disabled == item[3], str(item[0]) + " castle buttons reject only forbidden sides")


func _live() -> void:
	root.add_child(app)
	panel = app.analysis_overlay
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "live connected"):
		return
	var live_before: Dictionary = app.state.duplicate(true)
	app._on_analysis_pressed()
	if not await wait_for(func(): return panel.visible and panel._state_ready and not panel._poll_in_flight, "analysis GET revision obtained"):
		return
	panel._load_preset("8/8/8/8/8/8/8/8 w - - 0 1")
	await panel._start_analysis()
	verify(panel.editing and not panel._error.is_empty(), "empty-board start rejected visibly")
	panel._load_preset("6k1/5ppp/8/8/8/8/5PPP/3Q2K1 w - - 0 1")
	panel.time_option.select(0)
	await panel._start_analysis()
	verify(not panel.editing and panel.analysis_state.turn == "white", "custom setup starts real white analysis")
	if not await wait_for(func(): return not panel.board.hint_move.is_empty() and not panel.busy, "real Regicide recommendation"):
		return
	verify(panel.analysis_state.legal_moves.has(panel.board.hint_move), "real recommendation legal")
	verify(panel.evaluation_bar.has_score and panel._evaluation_matches(panel.analysis_state.get("evaluation", {})), "same real recommendation search supplies matching evaluation")
	await capture("analysis-live-recommendation")
	await wait_for(func(): return not panel.busy, "poll idle before move")
	var suggested: String = panel.board.hint_move
	var move_revision: int = int(panel.analysis_state.revision)
	board_button(suggested.substr(0, 2), true)
	board_button(suggested.substr(2, 2), false)
	await wait_for(func(): return not panel.busy and int(panel.analysis_state.revision) > move_revision, "inherited legal board drag completes during analysis")
	verify(panel.analysis_state.turn == "black" and panel.analysis_state.can_undo, "recommendation applies one ply only, no automatic opponent")
	if panel.analysis_state.status == "active":
		await wait_for(func(): return not panel.board.hint_move.is_empty() and not panel.busy, "black-side recommendation")
	await wait_for(func(): return not panel.busy, "idle before undo")
	await panel._mutate("undo", {})
	verify(panel.analysis_state.turn == "white" and not panel.analysis_state.can_undo, "undo returns custom initial position")
	await wait_for(func(): return not panel.busy, "idle before edit")
	await panel._edit_position()
	verify(panel.editing and not panel.analysis_state.active, "editing stops obsolete analysis")
	panel._load_preset("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
	await panel._start_analysis()
	panel.poll_timer.stop()
	await wait_for(func(): return not panel._poll_in_flight, "previous snapshot finished before controlled delayed poll")
	panel._refresh()
	verify(panel._poll_in_flight and not panel.busy, "delayed snapshot has independent transport")
	panel._on_square("a7")
	verify(panel.model.selected_square == "a7", "source click works while delayed snapshot is pending")
	await wait_for(func(): return not panel._poll_in_flight, "delayed source-selection snapshot completes")
	verify(panel.model.selected_square == "a7", "snapshot preserves selected promotion source")
	panel._refresh()
	panel._on_square("a8")
	verify(panel.model.pending_promotions.size() == 4 and panel.promotion_row.visible, "analysis promotion chooser offers all four pieces")
	verify(panel._poll_in_flight, "promotion target click works while delayed snapshot is pending")
	await wait_for(func(): return not panel._poll_in_flight, "delayed promotion snapshot completes")
	verify(panel.model.pending_promotions.size() == 4 and panel.promotion_row.visible, "snapshot preserves promotion choice")
	panel._refresh()
	await create_timer(0.08).timeout
	var prior_revision: int = int(panel.analysis_state.revision)
	panel._promote("n")
	verify(panel.busy and panel._poll_in_flight, "promotion mutation starts while old poll is still in flight")
	await wait_for(func(): return not panel.busy, "underpromotion completes independently of old snapshot")
	verify(panel.analysis_state.board.get("a8") == "N", "underpromotion retained")
	verify(panel.evaluation_summary.text == "무승부", "actual insufficient-material position displays terminal draw")
	await wait_for(func(): return not panel._poll_in_flight, "old pre-promotion snapshot finally arrives")
	verify(int(panel.analysis_state.revision) > prior_revision and panel.analysis_state.board.get("a8") == "N" and panel.model.pending_promotions.is_empty(), "late poll cannot roll back mutation or resurrect promotion chooser")
	await panel._edit_position()
	panel._load_preset(panel.START_FEN)
	panel.time_option.select(1)
	panel.poll_timer.start()
	await panel._start_analysis()
	await wait_for(func(): return panel.evaluation_bar.has_score and panel.analysis_state.get("evaluation", {}).get("status") == "searching", "real evaluation arrives during ongoing recommendation search")
	await wait_for(func(): return panel.evaluation_bar.has_score and panel.analysis_state.get("evaluation", {}).get("status") == "ready", "final real evaluation changes from searching to ready")
	await _live_castling_tests()
	await wait_for(func(): return not panel.busy, "idle before exit")
	await panel._return_to_game()
	verify(not panel.visible and app.state == live_before, "live game and archive state preserved after analysis")
	app._on_analysis_pressed()
	await wait_for(func(): return panel.visible and panel._state_ready and not panel._poll_in_flight, "analysis can reopen")
	verify(panel.editing, "reopened analysis begins in editor")
	await panel._return_to_game()


func _run() -> void:
	var live := false
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--backend-file":
			live = true
		if args[index] == "--evidence-dir" and index + 1 < args.size():
			evidence_dir = args[index + 1]
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	if live:
		await _live()
	else:
		await _synthetic()
	app.queue_free()
	await process_frame
	print("Godot analysis %s checks: %d, failures: %d" % ["live" if live else "synthetic", checks, failures])
	quit(0 if failures == 0 else 1)
