extends SceneTree
## Synthesized scene and real local HTTP inputs; no manual-play claim.

var app: Variant
var checks: int = 0
var failures: int = 0
var evidence_dir: String = ""
var ranked_geometries: Array = []


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, message: String, seconds: float = 12.0) -> bool:
	var deadline: int = Time.get_ticks_msec() + int(seconds * 1000.0)
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + message + "; " + str(app.error_message))
			return false
		await create_timer(0.01).timeout
	verify(true, message)
	return true


func fixture(color: String = "white") -> Dictionary:
	return {"game_id": "hint-game", "revision": 1,
		"fen": "8/8/8/4p3/8/8/4P3/4K2k w - - 0 1",
		"board": {"e2": "P", "e5": "p", "e1": "K", "h1": "k"},
		"turn": color, "human_color": color, "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["e2e3", "e2e4", "e1d1"] if color == "white" else ["e5e4"],
		"moves": [], "last_move": null, "in_check": false, "result": "*", "can_undo": false,
		"status": "대국 중", "termination": null, "error": null,
		"engine": {"name": "Regicide", "movetime_ms": 1000}}


func hint_for(position: Dictionary, move: String = "e2e4", san: String = "e4") -> Dictionary:
	return {"enabled": true, "game_id": position.game_id, "revision": position.revision,
		"fen": position.fen, "status": "ready", "move": move, "san": san,
		"from": move.substr(0, 2), "to": move.substr(2, 2),
		"promotion": move.substr(4) if move.length() == 5 else null,
		"depth": 5, "movetime_ms": 1000, "engine_name": "Regicide", "message": ""}


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(evidence_dir.path_join(name + ".png")) == OK,
		"rendered screenshot saved: " + name)


func ranked_hint(position: Dictionary, moves: Array, sans: Array) -> Dictionary:
	var value: Dictionary = hint_for(position, moves[0], sans[0])
	value.candidates = []
	for index in range(moves.size()):
		var candidate: Dictionary = hint_for(position, moves[index], sans[index])
		candidate.rank = index + 1
		value.candidates.append(candidate)
	return value


func settle() -> void:
	for _frame in range(10):
		await process_frame


func _run_ranked() -> void:
	var position: Dictionary = fixture()
	position.game_id = "ranked-hint-game"
	position.revision = int(app.state.revision) + 1
	app.accept_state(position)
	var value: Dictionary = ranked_hint(position, ["e2e4", "e2e3", "e1d1"], ["e4", "e3", "Kd1"])
	value.candidates.reverse()
	verify(app.accept_hint(value) and app.board.hint_moves.size() == 3, "all three legal candidates are accepted even when transport order differs")
	var layouts: Array = app.board.hint_arrow_layouts()
	for index in range(3):
		verify(layouts[index].rank == index + 1 and layouts[index].color == [Color("ef5350"), Color("ff9800"), Color("fdd835")][index], "rank determines exact shared color " + str(index + 1))
		verify(app.hint_candidate_labels[index].text.begins_with(str(index + 1)) and app.hint_candidate_labels[index].get_theme_color("font_color") == layouts[index].color, "legend and arrow agree for rank " + str(index + 1))
		verify(layouts[index].source.is_equal_approx(layouts[index].centers[0]) and layouts[index].target.is_equal_approx(layouts[index].centers[1]), "ranked arrow starts and ends at exact square centers")
	verify(app.board.hint_move == "e2e4", "legacy single-move API still exposes the highest ranked valid move")
	var original: Dictionary = app.state.duplicate(true)
	app._on_square_pressed("e2")
	verify(app.board_model.selected_square == "e2" and app.state == original, "painted multi-hint markers do not obstruct ordinary piece selection")
	var key := InputEventKey.new()
	key.keycode = KEY_F
	key.pressed = true
	var previous_view: bool = app.board_model.view_from_white
	app._unhandled_key_input(key)
	verify(app.board_model.view_from_white != previous_view and app.board.hint_moves.size() == 3, "keyboard flip continues to work with all three hints")
	for item in app.board.hint_arrow_layouts():
		verify(item.source.is_equal_approx(item.centers[0]) and item.target.is_equal_approx(item.centers[1]) and app.board.square_at_point(item.source) == str(item.move).substr(0, 2) and app.board.square_at_point(item.target) == str(item.move).substr(2, 2), "flipped candidate preserves exact square centers")
	var malformed: Dictionary = value.duplicate(true)
	malformed.candidates = [{"rank": 0, "move": "e2e4"}, {"rank": 1.5, "move": "e2e4"}, {"rank": 1, "move": "e2e4", "san": "e4"},
		{"rank": 2, "move": "e2e4"}, {"rank": 2, "move": "e2e3", "from": "a1"}, {"rank": 3, "move": "e2e5"}, {"rank": 3, "move": "e1d1", "san": "Kd1"}]
	verify(app.accept_hint(malformed) and app.board.hint_moves.size() == 2 and app.board.hint_moves[1].rank == 3, "illegal, duplicate, mismatched and invalid-rank candidates are removed without renumbering survivors")
	verify(app.hint_candidate_labels[1].text.is_empty() and app.hint_candidate_labels[2].text.begins_with("3"), "missing second rank cannot receive another candidate's color")
	malformed = value.duplicate(true)
	malformed.candidates = [{"rank": 1, "move": "e2e5"}, {"rank": 2, "move": "e2e3", "san": "e3"}]
	verify(app.accept_hint(malformed) and app.board.hint_move == "e2e3" and app.board.hint_arrow_layouts()[0].color == Color("ff9800"), "invalid first move does not prevent safe second candidate and retains rank-two color")
	app.accept_hint(value)
	for field in ["revision", "fen", "game_id"]:
		var stale: Dictionary = value.duplicate(true)
		stale[field] = -1 if field == "revision" else "stale"
		verify(not app.accept_hint(stale) and app.board.hint_moves.size() == 3, "stale multi-candidate " + field + " never replaces current arrows")
	for overlay in [app.analysis_overlay, app.coaching_overlay]:
		overlay.show()
		app._render_hint()
		verify(app.board.hint_moves.is_empty() and not app.accept_hint(value), "live ranked hints do not leak into another mode")
		overlay.hide()
		app.accept_hint(value)
	app._on_hint_pressed()
	verify(app.board.hint_moves.is_empty() and not app.hint_legend.visible, "OFF removes every ranked arrow and legend")
	verify(not app.accept_hint(value), "delayed multi-candidate response cannot resurrect an OFF hint")
	app._on_hint_pressed()
	app.accept_hint(value)
	for dimensions in [Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = dimensions
		app.board_model.view_from_white = true
		app._render()
		app.accept_hint(value)
		await settle()
		var before: Rect2 = app.board.get_global_rect()
		await capture("hint-three-" + str(dimensions.x))
		for mode in ["waiting", "one", "two", "long", "searching", "three"]:
			var changed: Dictionary = value.duplicate(true)
			if mode == "one":
				changed.candidates = [value.candidates[2]]
			elif mode == "two":
				changed.candidates = [value.candidates[2], value.candidates[1]]
			elif mode == "long":
				for item in changed.candidates:
					item.san = "very long unexpected notation ".repeat(15)
			elif mode in ["searching", "waiting"]:
				changed.status = mode
			app.accept_hint(changed)
			await settle()
			verify(app.board.get_global_rect().is_equal_approx(before), "board stays fixed at " + str(dimensions.x) + " while hint changes to " + mode)
			ranked_geometries.append({"viewport": [dimensions.x, dimensions.y], "mode": mode, "board": [before.position.x, before.position.y, before.size.x, before.size.y]})
	var promotion: Dictionary = position.duplicate(true)
	promotion.game_id = "ranked-promotion"
	promotion.revision = int(app.state.revision) + 1
	promotion.fen = "8/P7/8/8/8/8/8/4K2k w - - 0 1"
	promotion.board = {"a7": "P", "e1": "K", "h1": "k"}
	promotion.legal_moves = ["a7a8q", "a7a8r", "a7a8b", "a7a8n"]
	app.accept_state(promotion)
	value = ranked_hint(promotion, ["a7a8q", "a7a8n", "a7a8r"], ["a8=Q", "a8=N", "a8=R"])
	app.accept_hint(value)
	for flipped in [false, true]:
		app.board_model.view_from_white = not flipped
		app._render()
		await settle()
		layouts = app.board.hint_arrow_layouts()
		var cell: float = app.board.get_board_rect().size.x / 8.0
		for index in range(3):
			verify(layouts[index].source.is_equal_approx(layouts[index].centers[0]) and layouts[index].target.is_equal_approx(layouts[index].centers[1]) and app.board.square_at_point(layouts[index].source) == "a7" and app.board.square_at_point(layouts[index].target) == "a8", "promotion arrow stays centered on edge squares when flipped=" + str(flipped))
			for other in range(index + 1, 3):
				verify(layouts[index].marker.distance_to(layouts[other].marker) > cell * 0.27, "different promotions have separated numeric rank markers")
		verify(app.hint_candidate_labels[1].text.contains("나이트") and app.hint_candidate_labels[2].text.contains("룩"), "promotion legend identifies the piece for every candidate")
		await capture("hint-three-promotions-" + ("flipped" if flipped else "white"))
	app.board.hint_moves = []
	app.board.hint_move = "a7a8n"
	verify(app.board.hint_centers().size() == 2 and app.board.hint_arrow_layouts().is_empty(), "analysis and coaching retain their original single-arrow API")
	app.accept_hint(value)
	var changed: Dictionary = promotion.duplicate(true)
	changed.revision += 1
	changed.human_to_move = false
	app.accept_state(changed)
	verify(app.board.hint_moves.is_empty() and not app.accept_hint(value), "turn and revision changes clear all candidates")
	if not evidence_dir.is_empty():
		var file := FileAccess.open(evidence_dir.path_join("hint-ranked-layouts.json"), FileAccess.WRITE)
		file.store_string(JSON.stringify(ranked_geometries, "  "))
		file.close()


func _run_opponent_hints() -> void:
	for human_color in ["white", "black"]:
		var opponent_color: String = "black" if human_color == "white" else "white"
		var position: Dictionary = fixture(human_color)
		position.game_id = "opponent-hints-" + opponent_color
		position.revision = int(app.state.revision) + 1
		position.turn = opponent_color
		position.human_to_move = false
		position.engine_to_move = true
		position.fen = "4k3/4p3/8/8/8/8/4P3/4K3 " + ("b" if opponent_color == "black" else "w") + " - - 0 1"
		position.board = {"e2": "P", "e7": "p", "e1": "K", "e8": "k"}
		position.legal_moves = []
		position.hint_legal_moves = ["e7e5", "e7e6", "e8d8"] if opponent_color == "black" else ["e2e4", "e2e3", "e1d1"]
		app.request_in_flight = true
		app.engine_thinking = true
		app.accept_state(position)
		var value: Dictionary = ranked_hint(position, position.hint_legal_moves, ["e5", "e6", "Kd8"] if opponent_color == "black" else ["e4", "e3", "Kd1"])
		value.turn = opponent_color
		value.calculating = true
		verify(app.accept_hint(value) and app.board.hint_moves.size() == 3, "three " + opponent_color + " candidates render during the engine HTTP request")
		verify(app.hint_summary.text == "Regicide 후보 수 · 3개 후보" and app.hint_detail.text.contains("Regicide가 선택") and not app.hint_detail.text.contains("직접 선택"), "opponent label explains automatic move choice")
		verify(app.state.legal_moves.is_empty() and not app._can_select_move(), "displaying opponent candidates does not enable human input")
		app._on_square_pressed(str(value["from"]))
		verify(app.board_model.selected_square.is_empty() and app.state.moves.is_empty(), "opponent candidate arrows do not select or play a piece")
		var wrong_side: Dictionary = value.duplicate(true)
		wrong_side.turn = human_color
		verify(not app.accept_hint(wrong_side), "wrong-turn candidate snapshot is rejected")
		var malformed: Dictionary = value.duplicate(true)
		malformed.candidates = [{"rank": 1, "move": "a1a8", "san": "Ra8"}]
		verify(not app.accept_hint(malformed), "opponent candidate must be in the display-only legal list")
		app.request_in_flight = true
		app.engine_thinking = false
		app._render_hint()
		verify(app.board.hint_moves.is_empty() and not app.accept_hint(value), "unrelated in-flight mutation still hides opponent arrows")
		app.engine_thinking = true
		app.accept_hint(value)
		app._on_hint_pressed()
		verify(not app.hint_enabled and app.board.hint_moves.is_empty() and not app.accept_hint(value), "OFF clears opponent arrows while its search continues")
		app._on_hint_pressed()
		app.accept_hint(value)
		await settle()
		await capture("hint-opponent-" + opponent_color)
		app._on_flip_pressed()
		for item in app.board.hint_arrow_layouts():
			verify(item.source.is_equal_approx(item.centers[0]) and item.target.is_equal_approx(item.centers[1]), "flipped opponent arrow retains centered endpoints")
		var next: Dictionary = fixture(human_color)
		next.game_id = position.game_id
		next.revision = position.revision + 1
		app.engine_thinking = false
		app.request_in_flight = false
		app.accept_state(next)
		verify(app.board.hint_moves.is_empty() and not app.accept_hint(value), "actual reply clears and invalidates the previous opponent candidates")
		verify(app.accept_hint(hint_for(next, next.legal_moves[0], "e3" if human_color == "white" else "e4")) and app.hint_summary.text.begins_with("내 추천 수"), "turn switch returns to the human's recommendation label")


func _run_synthetic() -> void:
	app.offline_mode = true
	root.add_child(app)
	app.coaching_overlay._speech_enabled = false
	await process_frame
	await process_frame
	app.backend_url = "http://127.0.0.1:1"
	var position := fixture()
	app.accept_state(position)
	var value := hint_for(position)
	verify(not app.hint_enabled and app.hint_button.text == "추천 수: 끔", "default is OFF")
	verify(not app.accept_hint(value) and app.board.hint_move.is_empty(), "OFF rejects even matching ready hints")
	var reconnect: Dictionary = position.duplicate(true)
	reconnect.hint_enabled = true
	app.accept_state(reconnect)
	verify(app.hint_enabled, "initial connection restores backend preference")
	app.accept_state(position.merged({"hint_enabled": false}, true))
	verify(not app.hint_enabled, "initial OFF preference remains OFF")
	app._on_hint_pressed()
	verify(app.hint_enabled and app.hint_summary.visible, "ON immediately shows recommendation status")
	verify(app.accept_hint(value) and app.board.hint_move == "e2e4", "matching legal human hint is rendered")
	app._hint_connection_error = "temporary poll failure"
	app._render_hint()
	verify(app.board.hint_move.is_empty(), "poll failure removes displayed hint")
	app.accept_hint(value)
	verify(app._hint_connection_error.is_empty() and app.board.hint_move == "e2e4", "matching successful poll recovers from transient connection failure")
	verify(app.hint_summary.text == "내 추천 수 · e4" and app.hint_detail.text.contains("e2 → e4"), "readable SAN and coordinates shown")
	verify(app.state.moves.is_empty(), "displaying a recommendation never plays a move")
	app.accept_state(position.merged({"hint_enabled": false}, true))
	verify(app.hint_enabled, "state callback cannot override local toggle preference")
	var points: PackedVector2Array = app.board.hint_centers()
	verify(points.size() == 2 and points[0].y > points[1].y, "white-view arrow points from e2 to e4")
	app._on_square_pressed("e2")
	verify(app.board_model.selected_square == "e2", "hint drawing preserves normal piece selection")
	app._on_flip_pressed()
	points = app.board.hint_centers()
	verify(points.size() == 2 and points[0].y < points[1].y, "flipped view maps the same logical hint")
	verify(app.board.square_at_point(points[0]) == "e2" and app.board.square_at_point(points[1]) == "e4", "hint and input use identical square mapping")
	await capture("hint-white-flipped")
	var bad: Dictionary = value.duplicate(true)
	for field in ["revision", "fen", "game_id"]:
		bad = value.duplicate(true)
		bad[field] = 2 if field == "revision" else "wrong"
		verify(not app.accept_hint(bad), "reject stale " + field)
	bad = hint_for(position, "e2e5", "e5")
	verify(not app.accept_hint(bad), "reject an illegal ready move")
	app._on_hint_pressed()
	verify(not app.hint_enabled and app.board.hint_move.is_empty() and not app.hint_summary.visible, "OFF clears arrow and text immediately")
	verify(not app.accept_hint(value), "late callback cannot resurrect OFF recommendation")
	app._on_hint_pressed()
	app.accept_hint(value)
	app.request_in_flight = true
	app.engine_thinking = true
	app._render()
	verify(not app.hint_button.disabled and app.board.hint_move.is_empty(), "toggle remains responsive during main-engine request")
	app._on_hint_pressed()
	verify(not app.hint_enabled and app.request_in_flight and app.engine_thinking, "OFF is independent of main-engine calculation")
	app.request_in_flight = false
	app.engine_thinking = false
	app._on_hint_pressed()
	app.accept_hint(value)
	var changed: Dictionary = position.duplicate(true)
	changed.revision = 2
	app.accept_state(changed)
	verify(app.board.hint_move.is_empty() and not app.accept_hint(value), "move or undo revision clears and rejects old recommendation")
	changed = position.duplicate(true)
	changed.game_id = "new-game"
	changed.revision = 3
	app.accept_state(changed)
	verify(app.state.game_id == "new-game" and int(app.state.revision) == 3,
		"new game advances the monotonic board revision")
	verify(app.hint_enabled and not app.accept_hint(value), "new game keeps preference and rejects previous-game callback")
	app.accept_state(position)
	verify(app.state.game_id == "new-game" and int(app.state.revision) == 3,
		"delayed previous-game response cannot restore its old recommendation")
	position = fixture()
	position.game_id = "review-game"
	position.revision = 4
	value = hint_for(position)
	app.accept_state(position)
	verify(app.accept_hint(value), "fresh live state accepts its matching hint before review")
	app.review_mode = true
	app.review_frames = [position.duplicate(true)]
	app.review_moves = []
	app.review_summary = {}
	app.review_ply = 0
	app._render()
	verify(app.board.hint_move.is_empty() and not app.hint_summary.visible, "live hint never leaks into historical review")
	verify(not app.accept_hint(value) and app.hint_button.disabled, "review rejects live callbacks and disables toggle")
	app._exit_review()
	for flag in ["handicap_mode", "human_to_move", "result", "error"]:
		changed = position.duplicate(true)
		changed.revision = int(app.state.revision) + 1
		changed[flag] = {"handicap_mode": true, "human_to_move": false, "result": "1-0", "error": "engine error"}[flag]
		app.accept_state(changed)
		verify(app.board.hint_move.is_empty() and not app.accept_hint(hint_for(changed)), "no recommendation while " + flag)
	var black := fixture("black")
	black.game_id = "black-game"
	black.revision = int(app.state.revision) + 1
	app.accept_state(black)
	verify(app.accept_hint(hint_for(black, "e5e4", "e4")), "black-human legal recommendation accepted")
	points = app.board.hint_centers()
	verify(points.size() == 2 and points[0].y > points[1].y, "black-view black pawn arrow has correct direction")
	app._on_flip_pressed()
	points = app.board.hint_centers()
	verify(points.size() == 2 and points[0].y < points[1].y, "white-view black pawn arrow has correct direction")
	var promotion := position.duplicate(true)
	promotion.game_id = "promotion-game"
	promotion.revision = int(app.state.revision) + 1
	promotion.fen = "8/P7/8/8/8/8/8/4K2k w - - 0 1"
	promotion.board = {"a7": "P", "e1": "K", "h1": "k"}
	promotion.legal_moves = ["a7a8q", "a7a8r", "a7a8b", "a7a8n"]
	app.accept_state(promotion)
	verify(app.accept_hint(hint_for(promotion, "a7a8n", "a8=N")), "underpromotion recommendation remains legal")
	verify(app.hint_summary.text.contains("a8=N") and app.hint_detail.text.contains("나이트 승격"), "promotion piece is visible in readable hint")
	await capture("hint-promotion")
	root.size = Vector2i(900, 680)
	await process_frame
	await process_frame
	verify(app.hint_button.get_global_rect().position.x > app.board.get_global_rect().position.x, "small-window hint stays in sidebar")
	await capture("hint-small-window")
	await _run_opponent_hints()
	await _run_ranked()
	app.closing = true
	verify(not app.accept_hint(hint_for(promotion, "a7a8q", "a8=Q")), "shutdown ignores late recommendation")


func _run_live() -> void:
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "initial authenticated state"):
		return
	verify(not app.hint_enabled and app.board.hint_move.is_empty(), "live initial OFF")
	var before: String = JSON.stringify(app.state)
	app._on_hint_pressed()
	if not await wait_for(func(): return not app.board.hint_move.is_empty(), "white-human real engine hint"):
		return
	verify(app.state.legal_moves.has(app.board.hint_move), "real white recommendation is legal")
	verify(app.board.hint_moves.size() == 3 and app.board.hint_moves.all(func(item: Dictionary): return app.state.legal_moves.has(item.move)), "real white helper delivers three distinct legal ranked moves")
	verify(JSON.stringify(app.state) == before, "helper does not mutate the game")
	await capture("hint-live-white")
	app._on_flip_pressed()
	var points: PackedVector2Array = app.board.hint_centers()
	verify(app.board.square_at_point(points[0]) == app.board.hint_move.substr(0, 2), "live arrow matches flipped input")
	app._poll_hint()
	await wait_for(func(): return app.hint_in_flight, "delayed ready poll is in flight")
	await create_timer(0.05).timeout
	app._on_hint_pressed()
	verify(app.board.hint_move.is_empty(), "live OFF clears immediately")
	await wait_for(func(): return not app.hint_control_in_flight, "OFF acknowledged independently")
	await wait_for(func(): return not app.hint_in_flight, "old ready callback arrives after OFF")
	verify(not app.hint_enabled and app.board.hint_move.is_empty(), "actual delayed ready callback cannot resurrect OFF")
	app._on_hint_pressed()
	await create_timer(0.05).timeout
	app._on_hint_pressed()
	await wait_for(func(): return not app.hint_control_in_flight, "rapid ON/OFF serialized")
	await create_timer(1.2).timeout
	verify(not app.hint_enabled and app.board.hint_move.is_empty(), "cancelled helper never resurrects arrow")
	app._on_square_pressed("e2")
	app._on_square_pressed("e4")
	if not await wait_for(func(): return app.engine_thinking, "main engine actually thinking"):
		return
	verify(not app.hint_button.disabled, "live hint button available while main engine is busy")
	app._on_hint_pressed()
	verify(app.hint_enabled and app.request_in_flight, "toggle ON dispatches while main request remains active")
	if not await wait_for(func(): return app.engine_thinking and app.request_in_flight and app.board.hint_moves.size() == 3, "black opponent's three candidates arrive before its move"):
		return
	verify(app.state.legal_moves.is_empty() and app.board.hint_moves.all(func(item: Dictionary): return app.state.hint_legal_moves.has(item.move)), "black opponent arrows are display-only legal candidates")
	verify(app.hint_summary.text.begins_with("Regicide 후보 수"), "live opponent candidates have their own label")
	await capture("hint-live-engine-black")
	app._on_hint_pressed()
	verify(not app.hint_enabled and app.request_in_flight and app.board.hint_move.is_empty(), "toggle OFF works while main request remains active")
	await wait_for(func(): return not app.request_in_flight, "main engine completes independently")
	verify(app.state.moves.size() == 2, "helper added no extra moves")
	app._on_hint_pressed()
	await wait_for(func(): return not app.board.hint_move.is_empty(), "fresh hint after actual move")
	var old: Dictionary = app.hint.duplicate(true)
	app._on_undo_pressed()
	verify(app.board.hint_move.is_empty(), "undo clears immediately before HTTP response")
	await wait_for(func(): return not app.request_in_flight, "undo completes")
	verify(not app.accept_hint(old), "old pre-undo callback rejected")
	app.color_option.select(1)
	app._on_new_game_pressed()
	verify(app.board.hint_move.is_empty(), "new game clears immediately")
	if not await wait_for(func(): return app.engine_thinking and app.request_in_flight and app.state.human_color == "black" and app.board.hint_moves.size() == 3, "white opponent's three candidates arrive before its opening move"):
		return
	verify(app.state.legal_moves.is_empty() and app.board.hint_moves.all(func(item: Dictionary): return app.state.hint_legal_moves.has(item.move)), "white opponent arrows are display-only legal candidates")
	await capture("hint-live-engine-white")
	if not await wait_for(func(): return not app.request_in_flight, "new black-human game receives first engine move"):
		return
	verify(app.hint_enabled and app.state.human_color == "black" and app.state.moves.size() == 1, "new game preserves ON preference")
	if not await wait_for(func(): return not app.board.hint_move.is_empty(), "black-human real engine hint"):
		return
	verify(app.state.legal_moves.has(app.board.hint_move), "real black recommendation is legal")
	verify(app.board.hint_moves.size() == 3 and app.board.hint_moves.all(func(item: Dictionary): return app.state.legal_moves.has(item.move)), "real black helper delivers three distinct legal ranked moves")
	await capture("hint-live-black")
	old = app.hint.duplicate(true)
	app._on_handicap_pressed()
	await wait_for(func(): return not app.request_in_flight, "handicap mode entered")
	verify(app.state.handicap_mode and app.board.hint_move.is_empty() and not app.accept_hint(old), "no human recommendation during engine replacement")
	app._on_handicap_cancel_pressed()
	await wait_for(func(): return not app.request_in_flight, "handicap cancelled")
	await wait_for(func(): return not app.board.hint_move.is_empty(), "hint resumes after cancel")
	app._on_resign_pressed()
	await wait_for(func(): return not app.request_in_flight, "resignation completes")
	verify(app.board.hint_move.is_empty() and app.state.result != "*", "terminal game has no arrow")
	app._on_hint_pressed()
	await wait_for(func(): return not app.hint_control_in_flight, "final OFF acknowledged")


func _run() -> void:
	var live: bool = false
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--backend-file":
			live = true
		if args[index] == "--evidence-dir" and index + 1 < args.size():
			evidence_dir = args[index + 1]
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	if live:
		await _run_live()
	else:
		await _run_synthetic()
	app.queue_free()
	await process_frame
	print("Godot hint %s checks: %d, failures: %d" % ["live" if live else "synthetic", checks, failures])
	quit(0 if failures == 0 else 1)
