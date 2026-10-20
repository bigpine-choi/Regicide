extends SceneTree
## No backend or chess engine is started. Only HTTP transport is substituted.

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


func fixture() -> Dictionary:
	return {"game_id": "offline-game", "revision": 17,
		"fen": "7k/4P3/8/8/8/8/8/4K3 w - - 0 1",
		"board": {"e7": "P", "e1": "K", "h8": "k"}, "turn": "white",
		"human_color": "white", "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["e7e8q", "e7e8r", "e7e8b", "e7e8n", "e1d1"],
		"moves": [{"ply": 1, "uci": "h2h1", "san": "Kh1", "actor": "engine"}],
		"last_move": "h2h1", "in_check": false, "result": "*", "can_undo": true,
		"status": "offline UI fixture", "termination": null, "error": null,
		"engine_thinking": false,
		"engine": {"name": "offline transport", "movetime_ms": 3000,
			"settings_revision": 5, "active_movetime_ms": null}}


func select_time(milliseconds: int) -> void:
	var index: int = app.time_option.get_item_index(milliseconds)
	app.time_option.select(index)
	app.time_option.item_selected.emit(index)


func _run() -> void:
	root.size = Vector2i(1240, 860)
	app = CaptureApp.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	await process_frame
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state(fixture())
	verify(app.time_option.get_selected_id() == 3000, "dropdown reflects saved current-game time")
	verify(not app.time_option.disabled, "current game time can change while idle")
	app.board_model.selected_square = "e7"
	app.board_model.pending_promotions.assign(["e7e8q", "e7e8n"])
	var next: Dictionary = fixture()
	next.engine.movetime_ms = 5000
	next.engine.settings_revision = 6
	app.next_response = {"ok": true, "data": next}
	app.hold_response = true
	select_time(5000)
	verify(app.think_time_request_in_flight, "settings request has separate busy state")
	verify(not app.request_in_flight, "settings request does not consume board/search busy state")
	verify(app.captured_requests.size() == 1, "one dropdown selection sends exactly one request")
	var sent: Dictionary = app.captured_requests[0]
	verify(sent.path == "/api/think-time" and sent.settings_client, "dedicated settings transport and route")
	verify(sent.payload == {"game_id": "offline-game", "settings_revision": 5, "movetime_ms": 5000}, "exact game and settings version payload; no board mutation")
	verify(app.board_model.selected_square == "e7", "pending settings preserve selected square")
	verify(app.board_model.pending_promotions == ["e7e8q", "e7e8n"], "pending settings preserve promotion choices")
	verify(app.time_option.disabled and app.new_button.disabled, "only one settings update and no new-game race")
	app.hold_response = false
	await process_frame
	await process_frame
	verify(not app.think_time_request_in_flight, "settings busy flag clears")
	verify(int(app.state.revision) == 17 and app.state.moves == next.moves, "settings acknowledgement preserves board revision and history")
	verify(app.board_model.selected_square == "e7" and app.promotion_panel.visible, "acknowledgement keeps selection and promotion panel")
	verify(app.time_option.get_selected_id() == 5000, "acknowledged next-search time remains selected")
	verify(app.think_time_help.text.contains("5"), "saved time is visible")
	app.request_in_flight = true
	app.engine_thinking = true
	app.active_think_time_ms = 5000
	app._render()
	verify(not app.time_option.disabled, "dropdown stays usable during engine search")
	var while_searching: Dictionary = app.state.duplicate(true)
	while_searching.engine.movetime_ms = 1000
	while_searching.engine.settings_revision = 7
	while_searching.engine.active_movetime_ms = 5000
	while_searching.engine_thinking = true
	app.next_response = {"ok": true, "data": while_searching}
	select_time(1000)
	await process_frame
	await process_frame
	verify(app.request_in_flight and app.engine_thinking, "changing settings does not finish or restart the active search")
	verify(app.active_think_time_ms == 5000, "active search keeps its start-time budget")
	verify(app.think_time_help.text.contains("이번 수 5") and app.think_time_help.text.contains("다음 수부터 1"), "current and next search times are distinguished")
	var stale_settings: Dictionary = while_searching.duplicate(true)
	stale_settings.revision = 18
	stale_settings.engine.movetime_ms = 3000
	stale_settings.engine.settings_revision = 5
	stale_settings.engine.active_movetime_ms = null
	stale_settings.engine_thinking = false
	app.accept_state(stale_settings)
	verify(int(app.state.revision) == 18, "new board response is accepted")
	verify(int(app.state.engine.movetime_ms) == 1000 and int(app.state.engine.settings_revision) == 7, "old search response cannot roll back newer settings")
	var late_settings: Dictionary = while_searching.duplicate(true)
	late_settings.engine.movetime_ms = 3000
	late_settings.engine.settings_revision = 8
	late_settings.moves = []
	app.accept_state(late_settings)
	verify(int(app.state.revision) == 18 and not app.state.moves.is_empty(), "late settings response cannot roll back board/history")
	verify(int(app.state.engine.movetime_ms) == 3000 and int(app.state.engine.settings_revision) == 8, "late settings acknowledgement retains its independent newer version")
	verify(app.state.engine.active_movetime_ms == null and not app.state.engine_thinking, "late settings snapshot cannot revive a completed search")
	var very_old: Dictionary = fixture()
	very_old.moves = []
	var before_old: String = JSON.stringify(app.state)
	app.accept_state(very_old)
	verify(JSON.stringify(app.state) == before_old, "older board and older settings are rejected together")
	app.request_in_flight = false
	app.engine_thinking = false
	app.active_think_time_ms = 0
	app.state.handicap_mode = true
	app._render()
	var before_blocked: int = app.captured_requests.size()
	select_time(5000)
	verify(app.time_option.disabled and app.captured_requests.size() == before_blocked, "handicap preview blocks setting changes")
	app.state.handicap_mode = false
	app.review_mode = true
	app._render()
	select_time(5000)
	verify(app.time_option.disabled and not app.think_time_help.visible and app.captured_requests.size() == before_blocked, "read-only replay cannot alter live settings")
	app.review_mode = false
	app.request_in_flight = true
	app._render()
	verify(app.time_option.disabled, "non-search board operation briefly blocks a settings race")
	app.request_in_flight = false
	app.next_response = {"ok": false, "error": "stale settings version", "state": app.state.duplicate(true)}
	select_time(5000)
	await process_frame
	await process_frame
	verify(not app.think_time_request_in_flight and app.time_option.get_selected_id() == 3000, "failed request restores acknowledged selection and clears busy state")
	verify(app.error_message == "stale settings version", "failed settings request remains visible")
	var fresh_game: Dictionary = fixture()
	fresh_game.game_id = "new-game"
	fresh_game.revision = 19
	fresh_game.engine.settings_revision = 0
	fresh_game.engine.movetime_ms = 100
	app.accept_state(fresh_game)
	verify(app.state.game_id == "new-game" and int(app.state.engine.movetime_ms) == 100, "new game has independent versions and settings")
	verify(app.time_option.get_selected_id() == 100, "dropdown follows new-game settings")
	app.accept_state(fixture())
	verify(app.state.game_id == "new-game" and int(app.state.revision) == 19 and int(app.state.engine.movetime_ms) == 100, "delayed response from an older game cannot restore its board or settings")
	app.queue_free()
	await process_frame
	print("Godot think-time offline checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
