extends SceneTree
## Synthetic native controls: no public requests or private credentials.

const Explorer = preload("res://tests/opening_explorer_fixture.gd")
const Main = preload("res://scripts/main.gd")
const Analysis = preload("res://scripts/analysis_mode.gd")
const AnalysisFixture = preload("res://tests/opening_explorer_analysis_fixture.gd")
const START := "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
const AFTER := "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
var checks: int = 0
var failures: int = 0
var selected_fen: String = ""


func _initialize() -> void:
	_run.call_deferred()


func verify(ok: bool, message: String) -> void:
	checks += 1
	if not ok:
		failures += 1
		printerr("FAIL: ", message)


func fixture(fen: String = START, source: String = "masters") -> Dictionary:
	return {"status": "ready", "request_key": fen + source, "fen": fen, "source": source,
		"ratings": [] if source == "masters" else [1600, 1800, 2000, 2200, 2500],
		"speeds": [] if source == "masters" else ["rapid", "classical"],
		"configured": true, "message": "", "total": 100, "white": 40, "draws": 30, "black": 30,
		"fetched_at": "2026-11-03T00:00:00Z", "cached": false,
		"moves": [{"uci": "e2e4", "san": "e4", "count": 60, "play_pct": 60.0,
			"white": 30, "draws": 18, "black": 12, "white_pct": 50.0, "draw_pct": 30.0,
			"black_pct": 20.0, "average_rating": 2400, "fen_after": AFTER}],
		"games": [{"id": "Abc12345", "white": "White fixture", "black": "Black fixture",
			"white_rating": 2500, "black_rating": 2500, "winner": "white", "year": 2026,
			"url": "https://lichess.org/import/master/Abc12345/white" if source == "masters" else "https://lichess.org/Abc12345"}]}


func _run() -> void:
	root.size = Vector2i(1240, 860)
	root.content_scale_size = Vector2i(1240, 860)
	var panel := Explorer.new()
	panel.custom_minimum_size.x = 360
	panel.position = Vector2(20, 20)
	root.add_child(panel)
	panel.set_context(START, "http://127.0.0.1:1", "local-fixture")
	await process_frame
	verify(not panel.enabled_toggle.button_pressed and not panel.contents.visible and panel.captured.is_empty(), "external lookup is off by default")
	verify(panel.token_input.secret and panel.token_input.text.is_empty(), "credential entry starts empty and masked")
	panel.next_response = fixture()
	panel.enabled_toggle.button_pressed = true
	await process_frame
	verify(panel.captured.size() == 1 and panel.captured[0].fen == START and panel.captured[0].source == "masters", "enabling sends current position through host")
	verify(panel.rows.get_child_count() == 1 and panel.games.get_child_count() == 2, "move statistics and reference game render")
	verify(panel.summary_label.text.contains("100판") and panel.summary_label.text.contains("최선 수의 증명이 아닙니다"), "sample size and statistical meaning are visible")
	var args := OS.get_cmdline_user_args()
	if "--render-output" in args and DisplayServer.get_name() != "headless":
		var output := args[args.find("--render-output") + 1]
		DirAccess.make_dir_recursive_absolute(output.get_base_dir())
		await process_frame
		await RenderingServer.frame_post_draw
		verify(root.get_texture().get_image().save_png(output) == OK, "native fixture image saved")
	verify(panel._matches(fixture()), "masters ignores unused rating and speed filters")
	panel.explore_position.connect(func(fen: String): selected_fen = fen)
	panel.rows.get_child(0).pressed.emit()
	verify(selected_fen == AFTER and panel.current_fen == START, "candidate emits analysis position without changing live context")
	var count: int = panel.captured.size()
	panel.set_context(START, "http://127.0.0.1:1", "local-fixture")
	verify(panel.captured.size() == count, "ordinary same-position updates do not duplicate queries")
	var mismatched := fixture(AFTER)
	panel._accept_response(mismatched)
	verify(panel.snapshot.fen == START and panel.status_label.text.contains("다시 조회"), "other view's position cannot overwrite current statistics")
	panel.next_response = fixture(START, "lichess")
	panel.source_option.select(1)
	panel._filter_changed(1)
	await process_frame
	verify(panel.filter_box.visible and panel.captured[-1].ratings == [1600, 1800, 2000, 2200, 2500], "general games expose rating and time filters")
	verify(panel._matches(fixture(START, "lichess")), "matching filtered result is accepted")
	var wrong_filters := fixture(START, "lichess")
	wrong_filters.speeds = ["blitz"]
	verify(not panel._matches(wrong_filters), "mismatched time control is rejected")
	panel.next_response = fixture(AFTER, "lichess")
	panel.hold = true
	panel.set_context(AFTER, "http://127.0.0.1:1", "local-fixture")
	verify(panel.rows.get_child_count() == 0 and panel.snapshot.is_empty(), "board changes immediately clear prior position")
	panel.enabled_toggle.button_pressed = false
	panel.hold = false
	await process_frame
	await process_frame
	verify(panel.rows.get_child_count() == 0 and panel.snapshot.is_empty(), "late response after disabling stays hidden")
	verify(panel.safe_game_url("https://lichess.org/Abc12345") and not panel.safe_game_url("https://lichess.org.evil.test/Abc12345") and not panel.safe_game_url("javascript:alert(1)"), "reference links restrict host, scheme, and game path")
	verify(panel.safe_game_url("https://lichess.org/import/master/Abc12345/white") and panel.safe_game_url("https://lichess.org/import/master/Abc12345/black") and not panel.safe_game_url("https://lichess.org/import/master/Abc12345/white/evil") and not panel.safe_game_url("https://lichess.org/import/master/../../white"), "masters references use verified import route without arbitrary suffix or traversal")
	panel.render_snapshot({"status": "auth_required"})
	verify(panel.status_label.text.contains("토큰"), "missing authentication has actionable guidance")
	panel.render_snapshot({"status": "rate_limited", "retry_after": 61})
	verify(panel.status_label.text.contains("61초"), "rate-limited state shows retry delay")
	panel.render_snapshot({"status": "ready", "total": 0, "source": "masters", "moves": [], "games": []})
	verify(panel.status_label.text.contains("없습니다") and panel.rows.get_child_count() == 0, "empty database position is explicit")
	panel.next_response = {"configured": true, "message": "저장 실패: 이번 실행에서만 사용합니다."}
	panel.token_input.text = "not-a-real-token-fixture"
	panel._save_token()
	await process_frame
	verify(panel.token_input.text.is_empty() and panel.captured[-1].action == "token", "setting token clears input and uses host endpoint")
	verify(panel.connection_label.text.contains("이번 실행"), "credential persistence warning survives ordinary query status")
	panel.next_response = fixture(AFTER, "lichess")
	panel.enabled_toggle.button_pressed = true
	await process_frame
	panel.select_section(2)
	var requests_before: int = panel.captured.size()
	panel.set_context(START, "http://127.0.0.1:1", "local-fixture")
	panel._poll()
	await process_frame
	verify(panel.captured.size() == requests_before and panel._poll_timer.is_stopped(), "connection settings pause lookup and polling while board changes")
	panel.next_response = fixture(START, "lichess")
	panel.token_response = {"configured": true, "message": "연결 설정 저장"}
	panel.token_input.text = "not-a-real-token-fixture"
	panel._save_token()
	await process_frame
	verify(panel.section_tabs.current_tab == 0 and panel.captured[-1].action == "query" and panel.captured[-1].fen == START, "saving connection returns to statistics and queries latest position")
	panel.free()
	var analysis := Analysis.new()
	analysis.offline_mode = true
	root.add_child(analysis)
	await process_frame
	verify(analysis.opening_explorer != null and not analysis.opening_explorer.enabled_toggle.button_pressed, "analysis view owns a separate opt-in explorer")
	analysis.free()
	var inherited := AnalysisFixture.new()
	root.add_child(inherited)
	await inherited.open_position(START, "http://127.0.0.1:1", "fixture")
	var explorer_fixture: Variant = inherited.opening_explorer
	explorer_fixture.next_response = fixture(START, "lichess")
	explorer_fixture.apply_preferences({"enabled": true, "source": 1, "rating": 0, "speed": 0})
	explorer_fixture.captured.clear()
	explorer_fixture.next_response = fixture(AFTER, "masters")
	await inherited.explore_position(AFTER, "http://127.0.0.1:1", "fixture", {"enabled": true, "source": 0, "rating": 1, "speed": 2})
	verify(explorer_fixture.captured.size() == 1 and explorer_fixture.captured[0].fen == AFTER and explorer_fixture.captured[0].source == "masters", "reopening an enabled view queries only the new inherited position and filters")
	verify(inherited.started_fen == AFTER and explorer_fixture.source_option.selected == 0, "candidate analysis starts with inherited source selection")
	inherited.free()
	var app := Main.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	app.accept_state({"game_id": "explorer-ui-fixture", "revision": 1,
		"fen": START, "board": {"e1": "K", "e2": "P", "e8": "k"}, "turn": "white",
		"human_color": "white", "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["e2e4"], "moves": [], "last_move": null, "result": "*",
		"in_check": false, "can_undo": false, "error": null, "status": "UI fixture",
		"engine_thinking": false, "engine": {"name": "Regicide fixture", "movetime_ms": 1000}})
	await process_frame
	verify(app.opening_explorer != null and app.opening_explorer.current_fen == START and not app.opening_explorer.enabled_toggle.button_pressed, "live and saved-game board render provides context without external lookup")
	var board_rect: Rect2 = app.board.get_global_rect()
	app.select_sidebar_tab(1)
	app.opening_explorer.enabled_toggle.button_pressed = true
	app.opening_explorer.render_snapshot(fixture())
	await process_frame
	verify(app.board.get_global_rect().size.x >= board_rect.size.x - 1, "expanded explorer stays within existing sidebar width")
	if "--render-output" in args and DisplayServer.get_name() != "headless":
		var output := args[args.find("--render-output") + 1]
		app.sidebar_scroll.ensure_control_visible(app.opening_explorer)
		await process_frame
		await RenderingServer.frame_post_draw
		verify(root.get_texture().get_image().save_png(output.get_base_dir().path_join("native-main-fixture.png")) == OK, "integrated main image saved")
	app.free()
	print("Opening explorer native controls: ", checks, " checks, ", failures, " failures")
	quit(1 if failures else 0)
