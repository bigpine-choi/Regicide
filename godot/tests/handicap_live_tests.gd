extends SceneTree
## Exercise the shipped controls and callbacks with synthesized viewport input.

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


func click_at(point: Vector2) -> void:
	var down := InputEventMouseButton.new()
	down.position = point
	down.global_position = point
	down.button_index = MOUSE_BUTTON_LEFT
	down.pressed = true
	root.push_input(down, true)
	await process_frame
	var up := InputEventMouseButton.new()
	up.position = point
	up.global_position = point
	up.button_index = MOUSE_BUTTON_LEFT
	up.pressed = false
	root.push_input(up, true)
	await process_frame


func click_square(square: String) -> void:
	var rect: Rect2 = app.board.get_board_rect()
	var display: Vector2i = app.board_model.square_to_display(square)
	await click_at(app.board.global_position + rect.position + (Vector2(display) + Vector2.ONE * 0.5) * rect.size.x / 8.0)


func flip(message: String) -> void:
	var before: String = JSON.stringify(app.state)
	var view: bool = app.board_model.view_from_white
	var selected: String = app.board_model.selected_square
	await click_at(app.flip_button.get_global_rect().get_center())
	verify(app.board_model.view_from_white != view, "flip changes view: " + message)
	verify(JSON.stringify(app.state) == before, "flip preserves backend state: " + message)
	verify(app.board_model.selected_square == selected, "flip preserves selection: " + message)


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	var screenshot: Image = root.get_texture().get_image()
	verify(not screenshot.is_empty(), "native capture exists")
	verify(screenshot.save_png(evidence_dir.path_join(name + ".png")) == OK, "native capture saved")


func finish() -> void:
	print("Godot handicap live-scene checks: %d, failures: %d" % [checks, failures])
	if app != null:
		app.queue_free()
	await process_frame
	quit(0 if failures == 0 else 1)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--evidence-dir" and index + 1 < args.size():
			evidence_dir = args[index + 1]
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "initial authenticated state"):
		await finish()
		return
	verify(app.undo_button.text == "무르기", "undo has requested visible name")
	verify(app.handicap_button.disabled, "no engine move exists to replace initially")
	await click_square("e2")
	await click_square("e4")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 2, "actual first engine reply"):
		await finish()
		return
	verify(not app.handicap_button.disabled, "engine move can be changed")
	verify(app.handicap_option.get_selected_id() == 2, "latest engine ply selected by default")
	await flip("before replacing black's move")
	await click_at(app.handicap_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and app.state.get("handicap_mode", false), "enter replacement mode"):
		await finish()
		return
	verify(app.state.moves.size() == 1, "replacement rewinds to before chosen engine ply")
	verify(not app.state.human_to_move and not app.state.engine_to_move, "replacement stalls normal players")
	verify(app.undo_button.disabled and app.new_button.disabled and app.resign_button.disabled and app.save_button.disabled, "preview prevents conflicting actions")
	verify(app.handicap_cancel_button.visible and not app.handicap_cancel_button.disabled, "cancel control is visible and usable")
	await capture("handicap-black-view")
	await click_square("f7")
	verify(app.board_model.selected_square == "f7", "black engine pawn selected from black view")
	await flip("selected forced pawn")
	await click_square("f6")
	if not await wait_for(func(): return not app.request_in_flight and not app.state.get("handicap_mode", true), "commit forced f7f6"):
		await finish()
		return
	verify(app.state.moves.size() == 2 and app.state.moves[1].uci == "f7f6", "flipped input submits exact forced move")
	verify(app.state.moves[1].get("forced", false) and app.state.moves[1].actor == "engine", "forced engine move visibly annotated")
	verify(app.state.human_to_move and not app.engine_thinking, "forced reply returns turn to human without extra search")
	verify(app.move_history.text.contains("핸디캡"), "move history marks user-imposed handicap")
	await click_square("d2")
	await click_square("d4")
	if not await wait_for(func(): return app.engine_thinking, "Regicide starts new search after next human move"):
		await finish()
		return
	await flip("search from forced history")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 4, "Regicide continuation from forced position"):
		await finish()
		return
	verify(app.state.moves[1].uci == "f7f6" and app.state.moves[2].uci == "d2d4", "new search retains forced prefix")
	await click_at(app.undo_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 2, "undo removes last human-engine pair"):
		await finish()
		return
	verify(app.state.moves[1].get("forced", false), "undo retains surviving handicap annotation")
	verify(not app.board_model.view_from_white, "undo preserves current black viewpoint")
	app.color_option.select(1)
	await click_at(app.new_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and app.state.human_color == "black" and app.state.moves.size() == 1, "black game real white opening"):
		await finish()
		return
	verify(app.handicap_option.get_selected_id() == 1, "white engine opening can be replaced")
	await click_at(app.handicap_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and app.state.get("handicap_mode", false), "black user enters white engine replacement"):
		await finish()
		return
	await click_square("f2")
	await click_square("f3")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 1 and not app.state.get("handicap_mode", true), "force white f2f3 from black perspective"):
		await finish()
		return
	verify(app.state.human_to_move and app.state.turn == "black", "human black receives turn after imposed white move")
	verify(not app.state.can_undo, "normal undo preserves engine opening")
	await click_square("e7")
	await click_square("e5")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 3, "actual Regicide follows white forced opening"):
		await finish()
		return
	var original: Dictionary = app.state.duplicate(true)
	app.handicap_option.select(app.handicap_option.get_item_index(1))
	await click_at(app.handicap_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and app.state.get("handicap_mode", false), "select older white engine move"):
		await finish()
		return
	verify(app.state.moves.is_empty(), "older selection discards all following moves in preview")
	await flip("preview with canceled branch")
	await click_at(app.handicap_cancel_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and not app.state.get("handicap_mode", true), "cancel restores exact prior game"):
		await finish()
		return
	verify(app.state.moves == original.moves and app.state.board == original.board, "cancel restores full board and history")
	verify(int(app.state.revision) == int(original.revision) + 2, "begin and cancel each advance revision exactly once")
	verify(app.board_model.view_from_white, "cancel preserves user's new view")
	app.handicap_option.select(app.handicap_option.get_item_index(1))
	await click_at(app.handicap_button.get_global_rect().get_center())
	if not await wait_for(func(): return not app.request_in_flight and app.state.get("handicap_mode", false), "replace earlier move after cancel"):
		await finish()
		return
	await click_square("g2")
	await click_square("g4")
	if not await wait_for(func(): return not app.request_in_flight and app.state.moves.size() == 1 and not app.state.get("handicap_mode", true), "earlier forced branch committed"):
		await finish()
		return
	verify(app.state.moves[0].uci == "g2g4" and app.state.moves[0].get("forced", false), "earlier replacement removes old forced move and suffix")
	verify(app.state.human_to_move and app.state.human_color == "black", "black user can continue new branch")
	verify(app.error_message.is_empty(), "entire actual-engine handicap flow has no UI error")
	await capture("handicap-replaced-opening")
	await finish()
