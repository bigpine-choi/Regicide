extends SceneTree
## Shipped controller with fake HTTP and controlled clock snapshots; no engine.
const CaptureApp = preload("res://tests/think_time_capture_scene.gd")
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

func fixture(revision: int = 1, sample_ms: int = 1000) -> Dictionary:
	return {"game_id": "rapid-ui", "revision": revision,
		"fen": "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1", "board": {"e1":"K", "e2":"P", "e8":"k"},
		"human_color":"white", "human_to_move":true, "engine_to_move":false, "turn":"white",
		"legal_moves":["e2e4"], "moves":[], "last_move":null, "result":"*", "in_check":false,
		"can_undo":false, "error":null, "status":"UI fixture", "engine_thinking":false,
		"engine":{"name":"Regicide fixture", "movetime_ms":1000, "settings_revision":0, "extra_budget_ms":0},
		"clock":{"enabled":true, "control_id":"10+5", "label":"10+5 래피드", "white_ms":600000, "black_ms":599000,
			"running_color":"white", "status":"running", "turn_elapsed_ms":0, "sampled_monotonic_ms":sample_ms},
		"decision_support":{"status":"ready", "complexity":{"score":67, "label":"높음", "reasons":["기물 접촉이 많습니다."]},
			"recommendation":{"status":"ready", "total_ms":30000, "remaining_ms":30000, "reason":"남은 시간을 보존하면서 위협을 확인하세요.", "urgency":"normal"}}}


func handicap_fixture(color: String, revision: int, control: String, preview: bool = false, replaced: bool = false) -> Dictionary:
	var value: Dictionary = fixture(revision, revision * 1000)
	value.human_color = color
	value.engine.sparring_mode = true
	value.engine.time_management = "automatic"
	value.engine.automatic_think_time = true
	value.handicap_mode = preview
	value.can_handicap = not preview
	value.human_to_move = not preview
	value.engine_to_move = false
	value.clock.control_id = control
	value.clock.editable = not preview
	value.clock.edit_revision = revision
	value.clock.timeout_policy = "engine_only"
	value.clock.soft_timeout_color = color
	value.clock.white_ms = 597000
	value.clock.black_ms = 594000
	value.clock.status = "paused" if preview else "running"
	value.clock.running_color = null if preview else color
	value.clock.pause_reason = "handicap_preview" if preview else null
	value.board = {"e1":"K", "e8":"k", "e2":"P", "e7":"p"}
	value.moves = []
	if color == "white":
		value.board.erase("e2")
		value.board.e4 = "P"
		value.moves.append({"ply":1,"uci":"e2e4","san":"e4","actor":"human","color":"white"})
		if not preview:
			value.board.erase("e7")
			value.board["e6" if replaced else "e5"] = "p"
			value.moves.append({"ply":2,"uci":"e7e6" if replaced else "e7e5","san":"e6" if replaced else "e5","actor":"engine","color":"black","forced":replaced})
		value.fen = "4k3/4p3/8/8/4P3/8/8/4K3 b - - 0 1" if preview else "4k3/8/4p3/8/4P3/8/8/4K3 w - - 0 2" if replaced else "4k3/8/8/4p3/4P3/8/8/4K3 w - - 0 2"
		value.legal_moves = ["e7e6"] if preview else ["e1d1"]
	else:
		if not preview:
			value.board.erase("e2")
			value.board["e3" if replaced else "e4"] = "P"
			value.moves.append({"ply":1,"uci":"e2e3" if replaced else "e2e4","san":"e3" if replaced else "e4","actor":"engine","color":"white","forced":replaced})
		value.fen = "4k3/4p3/8/8/8/8/4P3/4K3 w - - 0 1" if preview else "4k3/4p3/8/8/8/4P3/8/4K3 b - - 0 1" if replaced else "4k3/4p3/8/8/4P3/8/8/4K3 b - - 0 1"
		value.legal_moves = ["e2e3"] if preview else ["e7e5"]
	value.turn = ("black" if color == "white" else "white") if preview else color
	return value


func check_handicap_clock_continuation() -> void:
	var revision: int = 40
	for control in ["10+0", "10+5", "15+10"]:
		for color in ["white", "black"]:
			var label: String = control + " / " + color
			app.accept_state(handicap_fixture(color, revision, control))
			var preview: Dictionary = handicap_fixture(color, revision + 1, control, true)
			app.next_response = {"ok":true,"data":preview}
			app.handicap_button.pressed.emit()
			await process_frame
			await process_frame
			verify(app.captured_requests[-1].path == "/api/handicap", "replacement button enters preview: " + label)
			verify(app.state.handicap_mode and app.top_clock.visible and app.bottom_clock.visible, "preview keeps both timed clocks visible: " + label)
			app._clock_received_ticks = Time.get_ticks_msec() - 60000
			app._render_clock_and_advice()
			verify(app.top_clock.text == "9:54" and app.bottom_clock.text == "9:57", "preview freezes both clock displays: " + label)
			verify(not app.clock_adjust_toggle.visible, "preview disables clock adjustment: " + label)
			var cancelled: Dictionary = handicap_fixture(color, revision + 2, control)
			app.next_response = {"ok":true,"data":cancelled}
			app.handicap_cancel_button.pressed.emit()
			await process_frame
			await process_frame
			verify(app.captured_requests[-1].path == "/api/handicap-cancel" and not app.state.handicap_mode, "cancel resumes original live branch: " + label)
			app._clock_received_ticks = Time.get_ticks_msec() - 2200
			app._render_clock_and_advice()
			verify(app.bottom_clock.text == ("▶ 9:55" if color == "white" else "9:57") and app.top_clock.text == ("9:54" if color == "white" else "▶ 9:52"), "cancel restarts only the human countdown: " + label)
			preview = handicap_fixture(color, revision + 3, control, true)
			app.next_response = {"ok":true,"data":preview}
			app.handicap_button.pressed.emit()
			await process_frame
			await process_frame
			var replacement: Dictionary = handicap_fixture(color, revision + 4, control, false, true)
			app.next_response = {"ok":true,"data":replacement}
			app._submit_selected_move("e7e6" if color == "white" else "e2e3")
			await process_frame
			await process_frame
			verify(app.captured_requests[-1].path == "/api/forced-move", "board replacement uses forced-move endpoint: " + label)
			verify(app.state.clock.enabled and app.state.clock.control_id == control and not app.state.handicap_mode, "replacement preserves chosen time control: " + label)
			verify(app.auto_time_label.visible and not app.time_option.visible and app.state.engine.time_management == "automatic", "replacement keeps automatic sparring time management: " + label)
			verify(app.clock_adjust_toggle.visible and app._can_select_move(), "live human controls return after replacement: " + label)
			app._clock_received_ticks = Time.get_ticks_msec() - 2200
			app._render_clock_and_advice()
			verify(app.bottom_clock.text == ("▶ 9:55" if color == "white" else "9:57") and app.top_clock.text == ("9:54" if color == "white" else "▶ 9:52"), "replacement clock keeps ticking for the next human turn: " + label)
			app.accept_clock_refresh(preview)
			verify(app.state.revision == replacement.revision and app.state.clock.status == "running", "late preview response cannot pause the replacement branch: " + label)
			revision += 5

func _run() -> void:
	root.size = Vector2i(1240, 860)
	app = CaptureApp.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	await process_frame
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state(fixture())
	verify(app.rapid_time_option.item_count == 3, "exactly three rapid controls")
	verify(app.rapid_time_option.selected == 0, "default ten minutes")
	verify(app.bottom_clock.text == "▶ 10:00", "human clock follows white perspective")
	verify(app.top_clock.text == "9:59", "opponent clock is stopped")
	verify(app.complexity_label.text.contains("높음 67/100 · 실험"), "human estimate labeled experimental")
	app.board_model.selected_square = "e2"
	var original_board_state: Dictionary = app.board.state
	var refresh: Dictionary = fixture(1, 1100)
	refresh.clock.white_ms = 599900
	refresh.clock.turn_elapsed_ms = 100
	refresh.decision_support.recommendation.remaining_ms = 29900
	app.accept_clock_refresh(refresh)
	verify(is_same(app.board.state, original_board_state), "same-position refresh does not rebuild board state")
	verify(app.board_model.selected_square == "e2", "timer refresh preserves selection")
	app._clock_received_ticks = Time.get_ticks_msec() - 2200
	app._render_clock_and_advice()
	verify(app.bottom_clock.text == "▶ 9:58", "active clock decreases")
	verify(app.top_clock.text == "9:59", "other clock does not decrease")
	verify(app.recommendation_label.text.contains("총 30초 권장 · 앞으로 28초"), "advice subtracts thought already spent")
	var stale: Dictionary = fixture(1, 999)
	stale.clock.white_ms = 700000
	app.accept_state(stale)
	verify(app.bottom_clock.text == "▶ 9:58", "stale same-revision clock ignored")
	app.accept_state(fixture())
	verify(app.bottom_clock.text == "▶ 9:58", "duplicate packet does not reset countdown")
	app._clock_received_ticks = Time.get_ticks_msec() - 3000
	var delayed: Dictionary = fixture(1, 2100)
	delayed.clock.white_ms = 598900
	delayed.clock.turn_elapsed_ms = 1100
	delayed.decision_support.recommendation.remaining_ms = 28900
	app.accept_clock_refresh(delayed)
	verify(app.bottom_clock.text == "▶ 9:57", "delayed fresher snapshot does not restore time")
	verify(app.recommendation_label.text.contains("앞으로 27초"), "delayed advice keeps elapsed time")
	app._clock_received_ticks = Time.get_ticks_msec() - 1200
	app._on_flip_pressed()
	verify(app.top_clock.text == "▶ 9:58", "clock follows flipped white location")
	verify(app.bottom_clock.text == "9:59", "black follows flipped bottom location")
	app._on_flip_pressed()
	var next: Dictionary = fixture(2, 3300)
	next.clock.white_ms = 602000
	next.clock.running_color = "black"
	next.human_to_move = false
	next.decision_support.status = "inactive"
	app.accept_state(next)
	app._clock_received_ticks = Time.get_ticks_msec() - 2200
	app._render_clock_and_advice()
	verify(app.bottom_clock.text == "10:02", "increment only from server")
	verify(app.top_clock.text == "▶ 9:57", "opponent clock runs")
	verify(app.complexity_label.text.contains("상대 차례"), "no human recommendation in opponent turn")
	var opponent_refresh: Dictionary = next.duplicate(true)
	opponent_refresh.clock.sampled_monotonic_ms = 5600
	opponent_refresh.decision_support = null
	app.accept_clock_refresh(opponent_refresh)
	verify(app.state.decision_support.is_empty() and app.complexity_label.text.contains("상대 차례"), "null opponent advice is safe during lightweight refresh")
	var paused: Dictionary = fixture(3, 6000)
	paused.clock.status = "paused"
	paused.clock.running_color = null
	paused.clock.white_ms = 5000
	paused.decision_support.recommendation = {"status":"inactive", "total_ms":null, "remaining_ms":null, "reason":"시계 일시 정지"}
	app.accept_state(paused)
	app._clock_received_ticks = Time.get_ticks_msec() - 10000
	app._render_clock_and_advice()
	verify(app.bottom_clock.text == "0:05.0", "paused clock stays fixed")
	verify(app.recommendation_label.text == "시계 일시 정지", "paused guidance inactive")
	var low: Dictionary = fixture(4, 12000)
	low.clock.white_ms = 700
	low.decision_support.recommendation.remaining_ms = 400
	app.accept_state(low)
	app._clock_received_ticks = Time.get_ticks_msec() - 1200
	app._render_clock_and_advice()
	verify(app.bottom_clock.text == "▶ 0:00", "countdown clamps at zero")
	verify(app.state.result == "*", "client does not adjudicate flagfall")
	verify(app.recommendation_label.text.contains("이제 결정"), "spent recommendation prompts decision")
	app.review_mode = true
	app.review_frames = [fixture(1, 1000)]
	app.review_moves = []
	app.review_summary = {"human_color":"white"}
	app.review_ply = 0
	app._render()
	verify(app.bottom_clock.text == "10:00", "recorded clock frozen despite old anchor")
	verify(not app.decision_panel.visible, "replay does not show live advice")
	app.review_mode = false
	var legacy: Dictionary = fixture(5, 16000)
	legacy.erase("clock")
	legacy.erase("decision_support")
	app.accept_state(legacy)
	verify(not app.top_clock.visible and app.bottom_clock.text == "새 대국에서 시간 선택", "legacy state shows setup prompt without invented time")
	var disabled: Dictionary = fixture(5, 0)
	disabled.clock = {"enabled":false, "status":"disabled", "sampled_monotonic_ms":null, "turn_elapsed_ms":null, "white_ms":null, "black_ms":null, "running_color":null}
	app.accept_state(disabled)
	app.accept_state(disabled.duplicate(true))
	verify(app.bottom_clock.text == "새 대국에서 시간 선택", "repeated disabled null clock snapshot is safe")
	verify(app._clock_sample(disabled.clock) == 0.0, "nullable sample safely falls back")
	for index in range(3):
		app.rapid_time_option.select(index)
		app._on_rapid_time_selected(index)
		var after: Dictionary = fixture(6 + index, 18000 + index)
		app.next_response = {"ok":true, "data":after}
		app._on_new_game_pressed()
		await process_frame
		await process_frame
		verify(app.captured_requests[-1].payload.time_control == ["10+0", "10+5", "15+10"][index], "selected rapid control in new-game payload")
		verify(app.rapid_time_option.selected == index, "draft rapid control survives render")
	verify(app.rapid_time_help.text.contains("15분") and app.rapid_time_help.text.contains("10초"), "increment explanation")
	for color in ["white", "black"]:
		var revision: int = 20 if color == "white" else 22
		var overtime: Dictionary = fixture(revision, 30000 + revision)
		overtime.human_color = color
		overtime.turn = color
		overtime.clock[color + "_ms"] = 0
		overtime.clock.running_color = color
		overtime.clock.timeout_policy = "engine_only"
		overtime.clock.soft_timeout_color = color
		overtime.clock.overtime_color = color
		overtime.decision_support.recommendation = {"status":"inactive", "total_ms":null, "remaining_ms":null, "reason":"연습 시간이 소진됐지만 계속 둘 수 있습니다."}
		var from_square: String = "e2" if color == "white" else "e7"
		var to_square: String = "e4" if color == "white" else "e5"
		if color == "black":
			overtime.board = {"e1":"K", "e7":"p", "e8":"k"}
			overtime.fen = "4k3/4p3/8/8/8/8/8/4K3 b - - 0 1"
			overtime.legal_moves = ["e7e5"]
		app.accept_state(overtime)
		verify(app.recommendation_label.text == "내 시간 소진 · 계속 진행", "server-declared human overtime stays playable: " + color)
		verify(app.complexity_label.text.contains("높음") and app._can_select_move(), "complexity and legal input stay active at human zero: " + color)
		var incremented: Dictionary = overtime.duplicate(true)
		incremented.revision += 1
		incremented.human_to_move = false
		incremented.decision_support = null
		incremented.clock.running_color = "black" if color == "white" else "white"
		incremented.clock[color + "_ms"] = 5000
		incremented.clock.overtime_color = null
		incremented.clock.sampled_monotonic_ms += 1
		app.next_response = {"ok":true, "data":incremented}
		app._on_square_pressed(from_square)
		app._on_square_pressed(to_square)
		await process_frame
		await process_frame
		verify(app.captured_requests[-1].path == "/api/move" and app.captured_requests[-1].payload.move == from_square + to_square, "human can submit an actual move at zero: " + color)
		verify(app.state.result == "*" and not app.recommendation_label.text.contains("내 시간 소진"), "server increment clears overtime without inventing result: " + color)
	verify(app._clock_text(-500) == "0:00", "negative clock clamped")
	verify(app._clock_text(null) == "—", "missing value not fabricated")
	await check_handicap_clock_continuation()
	app.queue_free()
	await process_frame
	print("Godot rapid clock checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
