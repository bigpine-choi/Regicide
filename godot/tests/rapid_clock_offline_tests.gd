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
	verify(app._clock_text(-500) == "0:00", "negative clock clamped")
	verify(app._clock_text(null) == "—", "missing value not fabricated")
	app.queue_free()
	await process_frame
	print("Godot rapid clock checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
