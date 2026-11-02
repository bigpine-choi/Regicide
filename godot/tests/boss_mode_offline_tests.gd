extends SceneTree
## Actual UI and controller with HTTP substituted; no engine or game is started.

const CaptureApp = preload("res://tests/think_time_capture_scene.gd")
var checks: int = 0
var failures: int = 0
var app: Variant


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func fixture(revision: int, phase: String = "") -> Dictionary:
	var result: Dictionary = {"game_id": "boss-ui-game", "revision": revision,
		"fen": "7k/8/8/8/8/8/4P3/4K3 w - - 0 1",
		"board": {"e2": "P", "e1": "K", "h8": "k"}, "turn": "white",
		"human_color": "white", "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["e2e3", "e2e4"], "moves": [], "last_move": null,
		"in_check": false, "result": "*", "can_undo": false,
		"status": "offline UI fixture", "termination": null, "error": null,
		"engine_thinking": false,
		"engine": {"name": "Regicide fixture", "movetime_ms": 1000,
			"settings_revision": 0, "active_movetime_ms": null,
			"extra_budget_ms": 0, "active_extra_budget_ms": null}}
	if not phase.is_empty():
		result.engine.boss_mode = true
		result.engine.boss_phase = phase
	return result


func _run() -> void:
	root.size = Vector2i(1240, 860)
	app = CaptureApp.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	await process_frame
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state(fixture(1))
	verify(app.boss_mode_option.get_selected_id() == 0, "basic mode is the default")
	verify(not app.boss_phase_label.text.contains("보스 모드"), "legacy state has no invented boss status")
	app.next_response = {"ok": true, "data": fixture(2)}
	app._on_new_game_pressed()
	await process_frame
	await process_frame
	verify(app.captured_requests[-1].payload.boss_mode == false, "default new game sends false")
	app.boss_mode_option.select(1)
	app.accept_state(fixture(3))
	verify(app.boss_mode_option.get_selected_id() == 1, "draft mode survives rendering")
	verify(not app.boss_phase_label.text.contains("보스 모드"), "draft mode does not modify the live game")
	app.next_response = {"ok": true, "data": fixture(4, "opening")}
	app._on_new_game_pressed()
	await process_frame
	await process_frame
	verify(app.captured_requests[-1].path == "/api/new" and app.captured_requests[-1].payload.boss_mode == true, "boss selection goes only to new-game request")
	verify(app.boss_phase_label.text == "보스 모드 · 변칙 오프닝", "opening label is displayed")
	await process_frame
	var normal_board: Rect2 = app.board.get_global_rect()
	app.accept_state(fixture(7, "full_strength"))
	await process_frame
	verify(app.boss_phase_label.text == "보스 모드 · 전력 대국", "full-strength label is displayed")
	verify(app.board.get_global_rect().is_equal_approx(normal_board), "phase change keeps board geometry")
	app.accept_state(fixture(8, "trap_hunt"))
	verify(app.boss_phase_label.text == "보스 모드 · 전술 사냥", "tactical lure phase label is displayed")
	var late_setting: Dictionary = fixture(5, "opening")
	late_setting.engine.movetime_ms = 3000
	late_setting.engine.settings_revision = 1
	app.accept_state(late_setting)
	verify(app.boss_phase_label.text == "보스 모드 · 전술 사냥", "late settings cannot rewind tactical lure phase")
	verify(app.time_option.get_selected_id() == 3000, "newer time setting still applies")
	app.boss_mode_option.select(0)
	app._render()
	verify(app.boss_phase_label.text == "보스 모드 · 전술 사냥", "normal-mode draft leaves active boss phase intact")
	app.accept_state(fixture(9, "opening"))
	verify(app.boss_phase_label.text == "보스 모드 · 변칙 오프닝", "new board revision can rewind phase after undo")
	app.review_mode = true
	app.review_frames = [fixture(1, "full_strength")]
	app.review_moves = []
	app.review_summary = {"human_color": "white"}
	app.review_ply = 0
	app._render()
	verify(app.boss_phase_label.text == "보스 모드 · 전력 대국", "replay uses saved frame phase, not current game phase")
	verify(app.boss_mode_option.disabled and not app.live_settings.visible, "replay cannot alter new-game controls")
	app.review_mode = false
	app.state.handicap_mode = true
	app._render()
	verify(app.boss_mode_option.disabled, "handicap preview disables mode control")
	app.state.handicap_mode = false
	app.next_response = {"ok": true, "data": fixture(10)}
	app._on_new_game_pressed()
	await process_frame
	await process_frame
	verify(app.captured_requests[-1].payload.boss_mode == false, "switch back to normal is explicit")
	verify(not app.boss_phase_label.text.contains("보스 모드"), "normal new game clears phase display")
	app.accept_state(fixture(9, "full_strength"))
	verify(not app.boss_phase_label.text.contains("보스 모드"), "stale board cannot resurrect previous boss display")
	app.queue_free()
	await process_frame
	print("Godot boss-mode offline checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
