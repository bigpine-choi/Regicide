extends SceneTree
## Native controls, role payloads and independent clock revision ordering.
const CaptureApp = preload("res://tests/clock_adjust_capture_scene.gd")
var app: Variant
var checks: int = 0
var failures: int = 0


func _initialize() -> void:
	_run.call_deferred()


func verify(ok: bool, message: String) -> void:
	checks += 1
	if not ok:
		failures += 1
		printerr("FAIL: ", message)


func fixture(revision: int = 1, edit_revision: int = 0, sample: int = 1000) -> Dictionary:
	return {"game_id":"clock-edit-ui", "revision":revision,
		"fen":"4k3/8/8/8/8/8/4P3/4K3 w - - 0 1", "board":{"e1":"K", "e2":"P", "e8":"k"},
		"human_color":"white", "human_to_move":true, "engine_to_move":false, "turn":"white",
		"legal_moves":["e2e4"], "moves":[], "last_move":null, "result":"*", "in_check":false,
		"can_undo":false, "error":null, "status":"UI fixture", "engine_thinking":false,
		"engine":{"name":"Regicide fixture", "sparring_mode":true, "movetime_ms":1000, "settings_revision":0, "extra_budget_ms":0},
		"clock":{"enabled":true, "editable":true, "edit_revision":edit_revision, "control_id":"10+5", "label":"10+5 래피드",
			"white_ms":600000, "black_ms":590000, "running_color":"white", "status":"running",
			"turn_elapsed_ms":0, "sampled_monotonic_ms":sample}, "decision_support":null}


func contains_editor(node: Node) -> bool:
	if node is LineEdit or node is TextEdit or node is SpinBox:
		return true
	for child in node.get_children():
		if contains_editor(child):
			return true
	return false


func _run() -> void:
	root.size = Vector2i(1240, 860)
	app = CaptureApp.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	await process_frame
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state(fixture())
	verify(app.clock_adjust_toggle.visible and not app.clock_adjust_panel.visible, "compact control starts closed in live sparring")
	app.clock_adjust_toggle.pressed.emit()
	verify(app.clock_adjust_panel.visible, "single click opens both clocks")
	verify(not contains_editor(app.clock_adjust_panel), "clock editing requires no typing or numeric fields")
	verify(app.clock_adjust_labels.human.text == "내 시간 · 10:00" and app.clock_adjust_labels.engine.text == "Regicide · 9:50", "role clocks show current remaining time")
	verify(not app.time_option.visible and app.auto_time_label.visible, "automatic AI timing remains the sparring default")
	app._on_flip_pressed()
	verify(app.clock_adjust_labels.human.text == "내 시간 · 10:00", "flipping board does not swap human clock controls")
	app._on_flip_pressed()
	app.board_model.selected_square = "e2"
	var board_state: Dictionary = app.board.state
	var edit_revision: int = 0
	var captions: Array[String] = ["−1분", "−10초", "+10초", "+1분"]
	for side in ["human", "engine"]:
		for index in range(4):
			var button: Button = app.clock_adjust_buttons[side][index]
			verify(button.text == captions[index], "compact adjustment caption")
			var response: Dictionary = app.state.duplicate(true)
			response.clock.edit_revision = edit_revision + 1
			response.clock.sampled_monotonic_ms += 1
			response.clock["white_ms" if side == "human" else "black_ms"] += app.CLOCK_ADJUST_DELTAS[index]
			app.next_response = {"ok":true, "data":response}
			button.pressed.emit()
			await process_frame
			await process_frame
			var sent: Dictionary = app.captured_requests[-1]
			verify(sent.path == "/api/clock-adjust" and sent.clock_client and not sent.main_client, "clock adjustment uses independent HTTP channel")
			verify(sent.payload == {"game_id":"clock-edit-ui", "clock_revision":edit_revision, "side":side, "delta_ms":app.CLOCK_ADJUST_DELTAS[index]}, "button posts exact role, delta and clock version")
			edit_revision += 1
			verify(app.state.clock.edit_revision == edit_revision and app.state.revision == 1, "manual time edit updates only clock revision")
	verify(app.board_model.selected_square == "e2" and is_same(app.board.state, board_state), "clock edits preserve selection and board object")
	verify(app.clock_adjust_panel.visible and not app.time_option.visible, "panel stays open and automatic timing stays active")
	app.request_in_flight = true
	app.engine_thinking = true
	app.hold_response = true
	var during_search: Dictionary = app.state.duplicate(true)
	during_search.clock.edit_revision += 1
	during_search.clock.black_ms += 10000
	during_search.clock.sampled_monotonic_ms += 1
	during_search.engine.pacing = {"status":"waiting", "target_total_ms":3000, "clock_edit_revision":during_search.clock.edit_revision}
	app.next_response = {"ok":true, "data":during_search}
	app._render_clock_adjustments()
	verify(not app.clock_adjust_buttons.engine[2].disabled, "AI search does not disable clock editing")
	app.clock_adjust_buttons.engine[2].pressed.emit()
	verify(app.clock_adjust_in_flight and app.clock_adjust_buttons.engine[2].disabled and app.clock_adjust_buttons.human[0].disabled, "pending adjustment prevents duplicate requests on either side")
	var request_count: int = app.captured_requests.size()
	app._on_clock_adjust_pressed("human", 10000)
	verify(app.captured_requests.size() == request_count, "repeated click cannot reuse in-flight clock revision")
	verify(app.request_in_flight and app.engine_thinking, "editing does not cancel the active AI move")
	app.hold_response = false
	await process_frame
	await process_frame
	verify(not app.clock_adjust_in_flight and not app.clock_adjust_buttons.engine[2].disabled, "clock controls recover before AI finishes")
	verify(app.engine_thinking and app.request_in_flight, "AI request survives successful clock adjustment")
	verify(app.state.engine.pacing == during_search.engine.pacing, "accepted edit also refreshes current AI pacing metadata")
	app.request_in_flight = false
	app.engine_thinking = false
	var current_clock: Dictionary = app.state.clock.duplicate(true)
	var stale_poll: Dictionary = app.state.duplicate(true)
	stale_poll.clock.edit_revision -= 1
	stale_poll.clock.sampled_monotonic_ms += 100000
	stale_poll.clock.black_ms = 123
	stale_poll.engine.pacing = {"status":"waiting", "target_total_ms":9000, "clock_edit_revision":0}
	app.accept_clock_refresh(stale_poll)
	verify(app.state.clock == current_clock, "older edit revision loses even with a later sample timestamp")
	verify(app.state.engine.pacing == during_search.engine.pacing, "stale clock poll cannot revert recomputed AI pacing")
	app.accept_state(stale_poll)
	verify(app.state.clock == current_clock, "delayed ordinary response cannot rewind clock edit")
	verify(app.state.engine.pacing == during_search.engine.pacing, "stale ordinary response also preserves recomputed AI pacing")
	var same_sample_edit: Dictionary = app.state.duplicate(true)
	same_sample_edit.clock.edit_revision += 1
	same_sample_edit.clock.white_ms += 60000
	app.accept_clock_refresh(same_sample_edit)
	verify(app.state.clock.white_ms == same_sample_edit.clock.white_ms, "new edit wins when sampled timestamp is equal")
	var old_board: Dictionary = fixture(0, 200, 1000000)
	app.accept_clock_refresh(old_board)
	verify(app.state.revision == 1 and app.state.clock.edit_revision == same_sample_edit.clock.edit_revision, "older board response never installs a clock from another turn")
	var server_conflict: Dictionary = app.state.duplicate(true)
	server_conflict.clock.edit_revision += 1
	server_conflict.clock.black_ms += 60000
	server_conflict.clock.sampled_monotonic_ms += 100
	app.next_response = {"ok":false, "error":"다른 화면에서 시간이 변경됐습니다. 다시 눌러 주세요.", "state":server_conflict}
	app.clock_adjust_buttons.human[2].pressed.emit()
	await process_frame
	await process_frame
	verify(app.state.clock.edit_revision == server_conflict.clock.edit_revision, "conflict response refreshes authoritative clocks")
	verify(app.clock_adjust_note.text.contains("다른 화면") and not app.clock_adjust_buttons.human[2].disabled, "conflict is visible and manual retry available")
	app.next_response = {"ok":false, "error":"연결이 끊겼습니다."}
	app.clock_adjust_buttons.engine[0].pressed.emit()
	await process_frame
	await process_frame
	verify(app.clock_adjust_note.text.contains("연결이 끊겼습니다") and not app.clock_adjust_in_flight, "connection failure releases controls without fabricated clock")
	app.request_in_flight = true
	app._render_clock_adjustments()
	verify(app.clock_adjust_buttons.human[2].disabled, "ordinary game mutation disables clock edits")
	app.request_in_flight = false
	var black_human: Dictionary = fixture(2, 20, 3000)
	black_human.human_color = "black"
	app.accept_state(black_human)
	verify(app.clock_adjust_labels.human.text == "내 시간 · 9:50" and app.clock_adjust_labels.engine.text == "Regicide · 10:00", "role controls follow human playing black")
	for hidden_case in ["normal", "untimed", "not_editable", "handicap", "ended"]:
		var hidden: Dictionary = fixture(int(app.state.revision) + 1, 21, 4000)
		match hidden_case:
			"normal": hidden.engine.sparring_mode = false
			"untimed": hidden.clock.enabled = false
			"not_editable": hidden.clock.editable = false
			"handicap": hidden.handicap_mode = true
			"ended": hidden.result = "1-0"
		app.accept_state(hidden)
		verify(not app.clock_adjust_toggle.visible and not app.clock_adjust_panel.visible and app.clock_adjust_buttons.human[2].disabled, "clock edits unavailable: " + hidden_case)
	app.accept_state(fixture(10, 30, 10000))
	app.review_mode = true
	app._render_clock_adjustments()
	verify(not app.clock_adjust_toggle.visible and app.clock_adjust_buttons.engine[2].disabled, "replay cannot edit live clocks")
	app.review_mode = false
	app._render_clock_adjustments()
	if not app.clock_adjust_panel.visible:
		app.clock_adjust_toggle.pressed.emit()
	root.size = Vector2i(900, 700)
	await process_frame
	await process_frame
	verify(app.sidebar_scroll.horizontal_scroll_mode == ScrollContainer.SCROLL_MODE_DISABLED, "compact layout never needs horizontal scrolling")
	for side in ["human", "engine"]:
		var last_right: float = -INF
		for button: Button in app.clock_adjust_buttons[side]:
			var rect: Rect2 = button.get_global_rect()
			verify(rect.position.x >= last_right and rect.end.x <= app.clock_adjust_panel.get_global_rect().end.x + 1, "adjustment buttons fit narrow sidebar without overlap")
			verify(button.size.x >= button.get_minimum_size().x, "clock button caption fits at small window size")
			last_right = rect.end.x
	app.hold_response = true
	var delayed_edit: Dictionary = fixture(10, 31, 11000)
	app.next_response = {"ok":true, "data":delayed_edit}
	app.clock_adjust_buttons.human[2].pressed.emit()
	var moved: Dictionary = fixture(11, 31, 12000)
	moved.fen = "4k3/8/8/8/4P3/8/8/4K3 b - - 0 1"
	moved.board = {"e1":"K", "e4":"P", "e8":"k"}
	moved.moves = [{"ply":1, "san":"e4", "uci":"e2e4", "color":"white", "actor":"human"}]
	moved.clock.white_ms = 640000
	moved.clock.running_color = "black"
	moved.engine.pacing = {"status":"idle"}
	app.accept_state(moved)
	app.hold_response = false
	await process_frame
	await process_frame
	verify(app.state.revision == 11 and app.state.fen == moved.fen and app.state.moves == moved.moves, "delayed adjustment callback cannot roll back an intervening engine move")
	verify(app.state.clock.white_ms == moved.clock.white_ms and app.state.engine.pacing == moved.engine.pacing, "old-turn edit response cannot restore old clocks or pacing")
	app.hold_response = true
	app.next_response = {"ok":true, "data":fixture(11, 32, 13000)}
	app.clock_adjust_buttons.human[2].pressed.emit()
	var new_game: Dictionary = fixture(12, 0, 14000)
	new_game.game_id = "next-clock-edit-game"
	app.accept_state(new_game)
	app.hold_response = false
	await process_frame
	await process_frame
	verify(app.state.game_id == "next-clock-edit-game" and app.state.clock.edit_revision == 0, "late edit response cannot replace new game")
	verify(not app.clock_adjust_panel.visible and not app.clock_adjust_in_flight, "new game starts with compact controls and no pending request")
	app.queue_free()
	await process_frame
	print("Godot click clock adjustment checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
