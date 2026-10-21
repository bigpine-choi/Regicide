extends SceneTree
## Synthesized scene and real local HTTP inputs; no manual-play claim.

var app: Variant
var checks: int = 0
var failures: int = 0
var evidence_dir: String = ""


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
		"legal_moves": ["e2e3", "e2e4"] if color == "white" else ["e5e4"],
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


func _run_synthetic() -> void:
	app.offline_mode = true
	root.add_child(app)
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
	verify(app.hint_summary.text == "Regicide 추천 · e4" and app.hint_detail.text.contains("e2 → e4"), "readable SAN and coordinates shown")
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
	if not await wait_for(func(): return not app.request_in_flight, "new black-human game receives first engine move"):
		return
	verify(app.hint_enabled and app.state.human_color == "black" and app.state.moves.size() == 1, "new game preserves ON preference")
	if not await wait_for(func(): return not app.board.hint_move.is_empty(), "black-human real engine hint"):
		return
	verify(app.state.legal_moves.has(app.board.hint_move), "real black recommendation is legal")
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
