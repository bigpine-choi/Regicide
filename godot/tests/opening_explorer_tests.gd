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
	root.size = Vector2i(1760, 960)
	root.content_scale_size = Vector2i(1760, 960)
	var restored := Explorer.new()
	restored.hide()
	restored.next_response = {"status": "idle", "configured": true, "connection": {"source": "stored", "remembered": true, "message": "저장된 연결키를 불러왔습니다."}}
	root.add_child(restored)
	restored.set_context(START, "http://127.0.0.1:1", "restored-fixture")
	await process_frame
	verify(restored.captured.size() == 1 and restored.captured[0] == null, "startup reads only local connection metadata while hidden")
	verify(restored.preferences().enabled and not restored.token_form.visible and restored.change_token_button.visible, "stored connection automatically enables statistics and hides token setup")
	verify(restored.connection_label.text.contains("저장된 연결키"), "restored connection status is visible without exposing its token")
	restored.change_token_button.pressed.emit()
	verify(restored.token_form.visible, "token replacement is an explicit action")
	restored.next_response = fixture()
	restored.show()
	await process_frame
	verify(restored.captured.size() == 2 and restored.captured[-1].action == "query", "showing the restored panel queries current statistics")
	restored.set_context(AFTER, "http://127.0.0.1:1", "restored-fixture")
	verify(restored.captured.filter(func(value: Variant): return value == null).size() == 1, "board changes do not repeat connection bootstrap")
	restored.render_snapshot({"status": "auth_required", "configured": true, "message": "저장된 연결키를 리체스가 승인하지 않았습니다."})
	verify(restored.token_form.visible and restored.status_label.text.contains("승인하지 않았습니다"), "invalid stored token preserves authentication reason and exposes replacement")
	restored.free()
	var pending := Explorer.new()
	pending.hide()
	pending.hold = true
	pending.next_response = {"status": "idle", "configured": true}
	root.add_child(pending)
	pending.set_context(START, "http://127.0.0.1:1", "pending-fixture")
	pending.hold = false
	await process_frame
	await process_frame
	verify(pending.preferences().enabled and pending.captured.size() == 1, "late bootstrap keeps statistics available without querying a hidden view")
	pending.free()
	var retry := Explorer.new()
	retry.hide()
	retry.next_response = {"error": "일시적인 로컬 연결 실패"}
	root.add_child(retry)
	retry.set_context(START, "http://127.0.0.1:1", "retry-fixture")
	await process_frame
	retry.set_context(AFTER, "http://127.0.0.1:1", "retry-fixture")
	verify(retry.captured.size() == 1, "failed bootstrap backs off without scheduling a retry loop")
	retry.next_response = {"status": "idle", "configured": true}
	retry._connection_retry_after_ms = 0
	retry.set_context(AFTER, "http://127.0.0.1:1", "retry-fixture")
	await process_frame
	verify(retry.captured.size() == 2 and retry.captured[-1] == null and retry.preferences().enabled, "later context update recovers transient bootstrap failure without querying hidden statistics")
	retry.free()
	var panel := Explorer.new()
	panel.custom_minimum_size.x = 360
	panel.position = Vector2(20, 20)
	root.add_child(panel)
	panel.next_response = fixture()
	panel.set_context(START, "http://127.0.0.1:1", "local-fixture")
	await process_frame
	verify(panel.contents.visible and panel.preferences().enabled and panel.captured.size() == 2 and panel.captured[0].action == "query", "statistics query automatically when a visible board receives its context")
	panel.captured.clear()
	verify(panel.token_input.secret and panel.token_input.text.is_empty(), "credential entry starts empty and masked")
	panel.next_response = fixture()
	panel._refresh()
	await process_frame
	verify(panel.captured.size() == 1 and panel.captured[0].fen == START and panel.captured[0].source == "masters", "refresh sends current position through host")
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
	var parsed_filters: Dictionary = JSON.parse_string(JSON.stringify(fixture(START, "lichess")))
	parsed_filters.speeds.reverse()
	verify(panel._matches(parsed_filters), "JSON-decoded numeric rating filters match native integer selections")
	verify(not panel._same_values([1600.5, 1800.0], [1600, 1800]) and not panel._same_values(["1600", "1800"], [1600, 1800]) and not panel._same_values([true], [1]), "numeric filter matching rejects fractional, string, and boolean impostors")
	var wrong_filters := fixture(START, "lichess")
	wrong_filters.speeds = ["blitz"]
	verify(not panel._matches(wrong_filters), "mismatched time control is rejected")
	panel.next_response = fixture(AFTER, "lichess")
	panel.hold = true
	panel.set_context(AFTER, "http://127.0.0.1:1", "local-fixture")
	verify(panel.rows.get_child_count() == 0 and panel.snapshot.is_empty(), "board changes immediately clear prior position")
	panel.hide()
	panel.hold = false
	await process_frame
	await process_frame
	verify(panel.rows.get_child_count() == 0 and panel.snapshot.is_empty(), "late response after leaving the view stays hidden")
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
	panel.show()
	panel.select_section(0)
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
	verify(analysis.opening_explorer != null and analysis.opening_explorer.preferences().enabled, "analysis view owns a separate always-enabled explorer")
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
	verify(app.opening_explorer != null and app.opening_explorer.current_fen == START and app.opening_explorer.preferences().enabled, "live and saved-game board render provides always-enabled explorer context")
	var board_rect: Rect2 = app.board.get_global_rect()
	app.select_sidebar_tab(1)
	app.opening_explorer.render_snapshot(fixture())
	await process_frame
	verify(app.board.get_global_rect().size.x >= board_rect.size.x - 1, "statistics updates preserve board size")
	if "--render-output" in args and DisplayServer.get_name() != "headless":
		var output := args[args.find("--render-output") + 1]
		await process_frame
		await RenderingServer.frame_post_draw
		verify(root.get_texture().get_image().save_png(output.get_base_dir().path_join("native-main-fixture.png")) == OK, "integrated main image saved")
	app.free()
	print("Opening explorer native controls: ", checks, " checks, ", failures, " failures")
	quit(1 if failures else 0)
