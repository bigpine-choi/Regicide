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
	verify(app.captured_requests[-1].payload.sparring_mode == false, "default new game also disables sparring")
	app.boss_mode_option.select(1)
	app.accept_state(fixture(3))
	verify(app.boss_mode_option.get_selected_id() == 1, "draft mode survives rendering")
	verify(not app.boss_phase_label.text.contains("보스 모드"), "draft mode does not modify the live game")
	app.next_response = {"ok": true, "data": fixture(4, "opening")}
	app._on_new_game_pressed()
	await process_frame
	await process_frame
	verify(app.captured_requests[-1].path == "/api/new" and app.captured_requests[-1].payload.boss_mode == true, "boss selection goes only to new-game request")
	verify(app.captured_requests[-1].payload.sparring_mode == false, "boss selection excludes sparring")
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
	await _sparring_checks()
	await _rating_checks()
	app.queue_free()
	await process_frame
	print("Godot boss-mode offline checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)


func sparring_fixture(revision: int, phase: String, observations: Variant = 3, status: String = "adapting") -> Dictionary:
	var value: Dictionary = fixture(revision)
	value.game_id = "sparring-ui-game"
	value.engine.sparring_mode = true
	value.engine.extra_budget_ms = 250
	value.engine.sparring = {"phase": phase, "status": status, "observations": observations,
		"level": 0.65, "confidence": "high", "target_loss_cp": 72}
	return value


func _sparring_checks() -> void:
	var normal_fast: Dictionary = fixture(10)
	normal_fast.engine.movetime_ms = 100
	normal_fast.engine.settings_revision = 2
	app.accept_state(normal_fast)
	var requests_before: int = app.captured_requests.size()
	app.boss_mode_option.select(2)
	app.boss_mode_option.item_selected.emit(2)
	verify(app.game_mode_help.text.contains("30수까지 ±1점 범위를 목표로 형세를 조절하고, 이후 서서히 강도를 높여 70번째 수부터 전력으로 둡니다.") and app.boss_mode_option.tooltip_text.contains("각각 30번"), "sparring help describes a one-point target over thirty full moves, not a guaranteed result")
	verify(app.game_mode_help.text.contains("자동으로 조절"), "sparring draft explains automatic time management")
	verify(not app.time_option.visible and app.auto_time_label.visible and app.time_option.disabled, "sparring draft immediately replaces the manual selector")
	app._on_think_time_selected(app.time_option.get_item_index(5000))
	verify(app.captured_requests.size() == requests_before, "hidden manual control cannot update the current normal game")
	verify(app.time_option.get_selected_id() == 100 and app.captured_requests.size() == requests_before, "draft mode selection never changes time or sends a time mutation")
	verify(not app.think_time_help.text.contains("맞춤 조절을 쉬고"), "draft sparring does not mislabel an active normal game")
	var minimum_eligible: int = 5000
	for index in range(app.time_option.item_count):
		var ms: int = app.time_option.get_item_id(index)
		if ms >= 600:
			minimum_eligible = mini(minimum_eligible, ms)
	verify(minimum_eligible == 1000, "one second is the shortest selectable eligible time")
	verify(not app.boss_phase_label.text.contains("스파링"), "sparring draft does not alter current normal game")
	app.next_response = {"ok": true, "data": sparring_fixture(11, "calibration", 0, "calibrating")}
	app._on_new_game_pressed()
	await process_frame
	await process_frame
	verify(app.captured_requests[-1].payload.sparring_mode == true and app.captured_requests[-1].payload.boss_mode == false, "sparring payload is mutually exclusive with boss")
	verify(app.boss_phase_label.text == "스파링 모드" and app.state.engine.sparring.observations == 0, "calibration observations remain in state without live diagnostics")
	verify(app.think_time_help.text.contains("사람 권장시간") and app.think_time_help.text.contains("4분 이하"), "automatic sparring explains recommendation and human time pressure")
	verify(not app.captured_requests[-1].payload.has("movetime_ms"), "new sparring lets the host choose automatic search budget")
	var geometry: Rect2 = app.board.get_global_rect()
	for row in [[12, "sparring", "맞춤 스파링"], [13, "ramp", "압박 높이는 중"], [14, "finish", "전력 마무리"]]:
		app.accept_state(sparring_fixture(row[0], row[1], 8))
		await process_frame
		verify(app.boss_phase_label.text == "스파링 모드" and app.state.engine.sparring.phase == row[1] and app.state.engine.sparring.observations == 8, "sparring phase is preserved without announcing adjustment: " + row[1])
	verify(app.board.get_global_rect().is_equal_approx(geometry), "phase updates do not resize the board")
	verify(not app.boss_phase_label.text.contains("0.65") and not app.boss_phase_label.text.contains("72") and not app.boss_phase_label.text.contains("%"), "internal strength and confidence never become a displayed rating or probability")
	var late: Dictionary = sparring_fixture(12, "calibration", 1)
	late.engine.settings_revision = 1
	late.engine.movetime_ms = 3000
	app.accept_state(late)
	verify(app.state.engine.sparring.phase == "finish" and app.state.engine.sparring.observations == 8, "late settings cannot replace a newer sparring profile")
	verify(app.time_option.get_selected_id() == 3000, "independent newer time setting still applies")
	app.accept_state(sparring_fixture(15, "calibration", 1, "holding"))
	verify(app.state.engine.sparring.phase == "calibration" and app.state.engine.sparring.observations == 1, "undo may decrease observations and reset phase at a newer board revision")
	verify(app.boss_phase_label.text == "스파링 모드" and app.boss_phase_label.tooltip_text.is_empty(), "holding and observations remain absent from live labels and tooltips")
	app.accept_state(sparring_fixture(16, "", null, "holding"))
	verify(app.boss_phase_label.text == "스파링 모드", "missing phase and observations keep the compatible mode label")
	var legacy: Dictionary = sparring_fixture(17, "")
	legacy.engine.sparring = null
	app.accept_state(legacy)
	verify(app.boss_phase_label.text == "스파링 모드", "missing profile is null-safe")
	app.boss_mode_option.select(1)
	app._render()
	verify(app.game_mode_help.text.contains("함정") and app.boss_phase_label.text == "스파링 모드", "prospective boss help does not change active sparring state")
	app.review_mode = true
	app.review_frames = [sparring_fixture(1, "ramp", 4)]
	app.review_moves = []
	app.review_summary = {"human_color": "white"}
	app.review_ply = 0
	app._render()
	verify(app.boss_phase_label.text == "스파링 모드" and app._display_state().engine.sparring.phase == "ramp" and app._display_state().engine.sparring.observations == 4, "archive retains its own profile without announcing adjustment")
	verify(not app.game_mode_help.visible and app.boss_mode_option.disabled, "replay hides prospective mode help and disables selection")
	app.review_mode = false
	var fast: Dictionary = sparring_fixture(18, "sparring")
	fast.engine.movetime_ms = 100
	fast.engine.settings_revision = 2
	fast.engine.extra_budget_ms = 0
	app.accept_state(fast)
	verify(app.think_time_help.text.contains("사람 권장시간") and not app.time_option.visible, "active sparring never exposes a manual short-time option")
	app.boss_mode_option.select(0)
	app._render()
	verify(app.time_option.get_selected_id() == 100 and not app.time_option.visible and app.think_time_help.text.contains("현재 스파링 대국"), "normal draft cannot change automatic management of the active sparring game")
	requests_before = app.captured_requests.size()
	app._on_think_time_selected(app.time_option.get_item_index(5000))
	verify(app.captured_requests.size() == requests_before, "active sparring blocks manual settings even with normal draft selected")
	var eligible: Dictionary = sparring_fixture(19, "sparring")
	eligible.engine.settings_revision = 3
	app.accept_state(eligible)
	verify(not app.think_time_help.text.contains("맞춤 조절을 쉬고"), "eligible current time removes short-time notice")
	app.next_response = {"ok": true, "data": fixture(20)}
	app._on_new_game_pressed()
	await process_frame
	await process_frame
	verify(not app.captured_requests[-1].payload.boss_mode and not app.captured_requests[-1].payload.sparring_mode, "return to normal clears both mode flags")
	app.accept_state(sparring_fixture(19, "finish", 20))
	verify(not app.boss_phase_label.text.contains("스파링"), "old game response cannot revive sparring state")
	verify(app.time_option.visible and not app.auto_time_label.visible and not app.time_option.disabled, "new normal game restores manual time control")


func rating_fixture(revision: int, estimate: Variant = null, delta: Variant = null, saved: Variant = null) -> Dictionary:
	var value: Dictionary = sparring_fixture(revision, "sparring")
	value.engine.sparring_rating = {"estimate": estimate, "delta": delta, "label": saved,
		"status": "awaiting_observations" if estimate == null else "provisional_uncalibrated",
		"target": "chess_com_rapid", "observations": 0 if estimate == null else 2,
		"calibration_games": 0, "source": "heuristic_prior", "updated_human_ply": null if estimate == null else 1}
	return value


func _rating_checks() -> void:
	app.accept_state(rating_fixture(21))
	verify(app.sparring_rating_label.text.contains("표본 부족") and app.sparring_rating_label.visible, "no observation shows no invented estimate")
	verify(not app.rating_label_panel.visible, "live game cannot submit a real rating")
	app.engine_thinking = true
	app._render()
	verify(app.sparring_rating_label.text.contains("분석 중"), "first observation displays analysis pending")
	app.engine_thinking = false
	app.accept_state(rating_fixture(22, 1250.0, 20.0))
	verify(app.sparring_rating_label.text == "추정 래피드 ~1250 (+20) · 실험", "verified numeric JSON estimate and positive delta display exactly")
	verify(app.sparring_rating_label.tooltip_text.contains("공식 레이팅이 아닙니다"), "rating remains explicitly experimental")
	var calibrated_v2: Dictionary = rating_fixture(22, 1450, -8)
	calibrated_v2.engine.sparring_rating.merge({"method": "regicide-sparring-rating-v2", "observations": 9, "calibration_games": 2, "evidence_level": "low"}, true)
	app.accept_state(calibrated_v2)
	verify(app.sparring_rating_label.text == "추정 래피드 ~1450 (-8) · 표본 적음", "v2 sparse evidence remains visible beside the live number")
	verify(app.sparring_rating_label.tooltip_text.contains("분석한 수 9개 · 실제 점수 보정 2판"), "v2 shows observed moves and local calibration games")
	app.accept_state(rating_fixture(22, 1250.0, 20.0))
	var late: Dictionary = rating_fixture(21)
	late.engine.settings_revision = 1
	late.engine.movetime_ms = 3000
	app.accept_state(late)
	verify(app.sparring_rating_label.text.contains("~1250 (+20)"), "late settings cannot erase verified estimate")
	app.accept_state(rating_fixture(23))
	verify(app.sparring_rating_label.text.contains("표본 부족"), "undo at a newer revision can remove the observation")
	app.accept_state(rating_fixture(24, 1210, -40))
	verify(app.sparring_rating_label.text.contains("~1210 (-40)"), "negative verified delta is preserved")
	app.review_mode = true
	app.review_frames = [rating_fixture(1, 1180, -10)]
	app.review_moves = []
	app.review_ply = 0
	app._render()
	verify(app.sparring_rating_label.text.contains("~1180 (-10)") and not app.rating_label_panel.visible, "archive displays its saved estimate but cannot label the live game")
	app.review_mode = false
	var terminal: Dictionary = rating_fixture(25, 1210, -40)
	terminal.result = "0-1"
	terminal.human_to_move = false
	app.accept_state(terminal)
	verify(app.rating_label_panel.visible and not app.rating_label_save.disabled, "completed current sparring game exposes rating input")
	var requests_before: int = app.captured_requests.size()
	app.rating_label_input.text = "99"
	app._on_rating_label_save()
	verify(app.captured_requests.size() == requests_before and app.error_label.text.contains("100~4000"), "invalid label never sends a request")
	app.next_response = {"ok": false, "error": "저장에 실패했습니다.", "state": terminal}
	app.rating_label_input.text = "1200"
	app._on_rating_label_save()
	await process_frame
	await process_frame
	verify(not app.rating_label_note.text.contains("저장됨") and app.error_label.text.contains("저장에 실패"), "failed label request does not fake saved status")
	var saved: Dictionary = terminal.duplicate(true)
	saved.revision = 26
	saved.engine.sparring_rating.label = {"rating": 1420, "source": "user_reported", "recorded_at": "synthetic"}
	app.next_response = {"ok": true, "data": saved}
	app.rating_label_input.text = "1420"
	app._on_rating_label_save()
	verify(app.rating_label_save.disabled, "saving prevents duplicate clicks")
	await process_frame
	await process_frame
	verify(app.captured_requests[-1].path == "/api/sparring/rating-label" and app.captured_requests[-1].payload == {"game_id": "sparring-ui-game", "rating": 1420, "revision": 25}, "label request carries exact game revision and integer rating")
	verify(app.rating_label_note.text.contains("1420 저장됨") and app.rating_label_note.text.contains("다음 대국"), "confirmed server label explains future calibration")
	verify(app.sparring_rating_label.text.contains("~1210 (-40)") and app.rating_label_save.text == "레이팅 수정", "label does not rewrite current estimates and remains editable")
	var correction: Dictionary = saved.duplicate(true)
	correction.revision = 27
	correction.engine.sparring_rating.label.rating = 1500
	correction.engine.sparring_rating.label.eligible_for_calibration = false
	app.next_response = {"ok": true, "data": correction}
	app.rating_label_input.text = "1500"
	app._on_rating_label_save()
	await process_frame
	await process_frame
	verify(app.rating_label_input.text == "1500" and app.rating_label_note.text.contains("1500 저장됨"), "same-game correction displays the new saved label")
	verify(app.rating_label_note.text.contains("이번 대국은 추정 보정에 쓰지 않습니다"), "ineligible labels do not promise future calibration")
	terminal.engine.settings_revision = 2
	app.accept_state(terminal)
	verify(app.rating_label_note.text.contains("1500 저장됨"), "late older terminal reply cannot erase saved correction")
	app.accept_state(fixture(28))
	verify(not app.sparring_rating_label.visible and not app.rating_label_panel.visible and app.rating_label_input.text.is_empty(), "normal new game clears rating and label draft")
