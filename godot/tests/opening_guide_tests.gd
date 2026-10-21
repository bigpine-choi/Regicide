extends SceneTree
## Opening learning is local presentation; synthetic fixtures are not source claims.

const CaptureApp = preload("res://tests/think_time_capture_scene.gd")
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


func guide() -> Dictionary:
	return {"status": "in_catalog", "history_scope": "standard",
		"current_matches": [{"name": "King's Knight Opening", "eco": "C44", "ply": 4, "match_type": "exact"}],
		"current_matches_total": 1, "current_matches_truncated": false,
		"matched_opening": null,
		"continuations": [
			{"uci": "f1b5", "san": "Bb5", "immediate_openings": [{"name": "Ruy Lopez", "eco": "C60"}],
				"reachable_openings": [
					{"name": "Ruy Lopez", "eco": "C60", "distance_plies": 1, "remaining_uci": ["f1b5"], "remaining_san": "3. Bb5"},
					{"name": "Ruy Lopez: Morphy Defense", "eco": "C78", "distance_plies": 2, "remaining_uci": ["f1b5", "a7a6"], "remaining_san": "3. Bb5 a6"},
					{"name": "Ruy Lopez: Marshall Attack", "eco": "C89", "distance_plies": 12,
						"remaining_uci": ["f1b5", "a7a6", "b5a4", "g8f6", "e1g1", "f8e7", "f1e1", "b7b5", "a4b3", "e8g8", "c2c3", "d7d5"],
						"remaining_san": "3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 O-O 8. c3 d5"}],
				"immediate_total": 1, "immediate_truncated": false, "reachable_total": 3, "reachable_lines_total": 3, "reachable_truncated": false},
			{"uci": "f1c4", "san": "Bc4", "immediate_openings": [{"name": "Italian Game", "eco": "C50"}],
				"reachable_openings": [{"name": "Italian Game: Giuoco Piano", "eco": "C50", "distance_plies": 2,
					"remaining_uci": ["f1c4", "f8c5"], "remaining_san": "3. Bc4 Bc5"}],
				"immediate_total": 1, "immediate_truncated": false, "reachable_total": 2, "reachable_lines_total": 2, "reachable_truncated": false}],
		"continuations_total": 2, "continuations_truncated": false,
		"source": {"repository_url": "https://example.invalid/openings-fixture", "commit": "fixture-only-commit",
			"license": "CC0-1.0", "attribution": "Synthetic opening catalog fixture"},
		"disclaimer": "Names and sequences are learning information; selection never makes a game move.",
		"ordering": "SAN 표기순이며 추천 순위가 아닙니다."}


func fixture(revision: int = 1) -> Dictionary:
	return {"game_id": "opening-game", "revision": revision,
		"fen": "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
		"board": {"a8": "r", "c8": "b", "d8": "q", "e8": "k", "f8": "b", "g8": "n", "h8": "r",
			"a7": "p", "b7": "p", "c7": "p", "d7": "p", "f7": "p", "g7": "p", "h7": "p", "c6": "n", "e5": "p",
			"e4": "P", "f3": "N", "a2": "P", "b2": "P", "c2": "P", "d2": "P", "f2": "P", "g2": "P", "h2": "P",
			"a1": "R", "b1": "N", "c1": "B", "d1": "Q", "e1": "K", "f1": "B", "h1": "R"},
		"turn": "white", "human_color": "white", "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["f1b5", "f1c4", "d2d4"], "moves": [], "last_move": "b8c6",
		"in_check": false, "result": "*", "can_undo": true, "status": "대국 중", "termination": null, "error": null,
		"engine": {"name": "Synthetic opening display fixture", "movetime_ms": 1000}, "opening_guide": guide()}


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(evidence_dir.path_join(name + ".png")) == OK,
		"rendered opening screenshot saved")


func _run() -> void:
	for index in range(OS.get_cmdline_user_args().size() - 1):
		if OS.get_cmdline_user_args()[index] == "--evidence-dir":
			evidence_dir = OS.get_cmdline_user_args()[index + 1]
	root.size = Vector2i(1240, 860)
	app = CaptureApp.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	await process_frame
	await process_frame
	verify(not app.opening_details.visible and app.opening_summary.text.contains("기다"), "missing guide is safe and details begin folded")
	var position := fixture()
	app.accept_state(position)
	verify(app.opening_summary.text.contains("King's Knight Opening") and app.opening_summary.text.contains("C44"), "current opening name and ECO are visible")
	verify(app.opening_status.text.contains("등록된 수순"), "exact path match is identified")
	verify(app._opening_rows.size() == 5, "immediate and future names are preserved without duplicate immediate rows")
	var branch_text: String = app.opening_branch_list.get_parsed_text()
	verify(branch_text.contains("Bb5") and branch_text.contains("Bc4") and branch_text.contains("Marshall Attack"), "first SAN and reachable named lines appear together")
	verify(branch_text.contains("다음 수") and branch_text.contains("12번 착수 뒤"), "immediate and later names are distinguished")
	verify(app.opening_catalog_note.text.contains(guide().ordering), "expanded list explains its actual ordering rather than implying recommendation rank")
	app.board_model.selected_square = "f1"
	var before: String = JSON.stringify(app.state)
	var before_hint: String = JSON.stringify(app.hint)
	app._on_opening_toggle_pressed()
	verify(app.opening_details.visible, "branch list expands locally")
	app._on_opening_line_selected(0)
	verify(app.opening_preview.get_parsed_text() == "3. Bb5", "immediate names retain the catalog's complete numbered SAN")
	app._on_opening_line_selected(2)
	verify(app.opening_preview_title.text.contains("Marshall Attack") and app.opening_preview_title.text.contains("12"), "selected target name and full distance are visible")
	verify(app.opening_preview.get_parsed_text() == guide().continuations[0].reachable_openings[2].remaining_san, "preview shows the complete remaining sequence rather than only the next move")
	verify(JSON.stringify(app.state) == before and JSON.stringify(app.hint) == before_hint and app.board_model.selected_square == "f1", "preview changes neither live position, hints nor selected piece")
	verify(app.captured_requests.is_empty(), "opening selection makes no HTTP or engine request")
	app._render()
	verify(app._opening_selected == 2 and app.opening_preview_title.text.contains("Marshall Attack"), "ordinary renders preserve a selected learning preview")
	app._on_opening_source_pressed()
	verify(app.opening_source_dialog.visible and app.opening_source_text.get_parsed_text().contains("CC0-1.0")
		and app.opening_source_text.get_parsed_text().contains("fixture-only-commit"), "source and fixed version are accessible without network access")
	app.opening_source_dialog.hide()
	await capture("opening-expanded")
	var board_width: float = app.board.size.x
	app._on_opening_toggle_pressed()
	await process_frame
	verify(not app.opening_details.visible and is_equal_approx(app.board.size.x, board_width), "folding the learning panel preserves board width")
	app.request_in_flight = true
	app.engine_thinking = true
	app._render()
	app._on_opening_toggle_pressed()
	app._on_opening_line_selected(1)
	verify(app.opening_details.visible and not app.opening_toggle.disabled and app.opening_preview_title.text.contains("Morphy"), "read-only learning remains available during engine thinking")
	verify(app.request_in_flight and app.engine_thinking and app.captured_requests.is_empty(), "learning does not restart or complete the active search")
	app.request_in_flight = false
	app.engine_thinking = false
	app.state.handicap_mode = true
	app._render()
	app._on_opening_line_selected(2)
	verify(app.opening_preview_title.text.contains("Marshall Attack") and app.state.handicap_mode, "handicap preview still permits read-only learning")
	var undone := fixture(2)
	undone.opening_guide.current_matches[0].name = "Open Game"
	undone.opening_guide.continuations = []
	app.accept_state(undone)
	verify(app.opening_summary.text.contains("Open Game") and app._opening_selected == -1 and app.opening_preview.get_parsed_text().is_empty(), "new or undone position resets a stale line preview")
	app.accept_state(position)
	verify(app.opening_summary.text.contains("Open Game"), "delayed older board response cannot roll opening learning back")
	var changed := fixture(3)
	changed.opening_guide.status = "beyond_catalog"
	changed.opening_guide.matched_opening = changed.opening_guide.current_matches[0].duplicate(true)
	changed.opening_guide.current_matches = []
	changed.opening_guide.continuations = []
	app.accept_state(changed)
	verify(app.opening_summary.text.begins_with("마지막 확인:") and app.opening_status.text.contains("등록 수순 밖"), "last known name is distinct from the current position")
	changed = fixture(4)
	changed.opening_guide.current_matches[0].match_type = "transposition"
	changed.opening_guide.current_matches.append({"name": "Alternative position name", "eco": "C40"})
	app.accept_state(changed)
	verify(app.opening_status.text.contains("다른 수순") and app.opening_summary.tooltip_text.contains("Alternative"), "transposition and alternate registered names are visible")
	changed = fixture(5)
	changed.opening_guide.history_scope = "custom"
	changed.opening_guide.current_matches[0].match_type = "position"
	app.accept_state(changed)
	verify(app.opening_status.text.contains("사용자 시작 위치") and app.opening_status.text.contains("현재 위치"), "custom root uses a clearly labelled position comparison")
	changed = fixture(6)
	changed.opening_guide.status = "unknown"
	changed.opening_guide.current_matches = []
	changed.opening_guide.continuations = []
	app.accept_state(changed)
	verify(app.opening_summary.text == "등록된 오프닝 이름 없음", "unknown is not a stale previously recognized name")
	changed = fixture(7)
	changed.opening_guide = null
	app.accept_state(changed)
	verify(app.opening_source_button.disabled and app.opening_summary.text.contains("기다"), "old backend or absent guide degrades safely")
	var live := fixture(8)
	app.accept_state(live)
	before = JSON.stringify(app.state)
	app.review_mode = true
	var replay_last := fixture()
	replay_last.opening_guide.status = "beyond_catalog"
	replay_last.opening_guide.matched_opening = replay_last.opening_guide.current_matches[0].duplicate(true)
	replay_last.opening_guide.current_matches = []
	app.review_frames = [fixture(), replay_last]
	app.review_moves = [{"san": "e4"}]
	app.review_summary = {}
	app._set_review_ply(0)
	app._on_opening_line_selected(1)
	verify(app.opening_summary.text.contains("King's Knight Opening") and app.opening_preview_title.text.contains("Morphy"), "replay shows its own opening and supports line preview")
	app._set_review_ply(1)
	verify(app.opening_summary.text.begins_with("마지막 확인:"), "replay navigation updates the displayed frame's opening")
	verify(JSON.stringify(app.state) == before, "opening replay leaves the live game unchanged")
	app._exit_review()
	verify(app.opening_summary.text.contains("King's Knight Opening"), "returning from replay restores live opening information")
	root.size = Vector2i(900, 680)
	await process_frame
	await process_frame
	verify(app.opening_summary.get_global_rect().position.x > app.board.get_global_rect().position.x and app.board.size.x > 300,
		"small window retains a usable board and the opening panel in its sidebar")
	await capture("opening-small-window")
	app.sidebar_scroll.scroll_vertical = 10000
	await process_frame
	var promotion := fixture(9)
	promotion.board = {"a7": "P", "e1": "K", "h8": "k"}
	promotion.legal_moves = ["a7a8q", "a7a8r", "a7a8b", "a7a8n"]
	app.accept_state(promotion)
	app.board_model.pending_promotions.assign(promotion.legal_moves)
	app._render()
	await process_frame
	await process_frame
	verify(app.promotion_panel.visible and app.sidebar_scroll.get_global_rect().encloses(app.promotion_buttons["q"].get_global_rect()),
		"required promotion choice becomes reachable even after scrolling expanded learning content")
	verify(app.opening_details.visible and app.board_model.pending_promotions.size() == 4,
		"revealing promotion preserves learning expansion and all legal promotion choices")
	var truncation_flags := ["continuations_truncated", "immediate_truncated", "reachable_truncated"]
	for index in range(truncation_flags.size()):
		var partial := fixture(10 + index)
		var flag: String = truncation_flags[index]
		if flag == "continuations_truncated":
			partial.opening_guide[flag] = true
		else:
			partial.opening_guide.continuations[0][flag] = true
		app.accept_state(partial)
		verify(app.opening_catalog_note.text.contains("일부만 표시"), "actual schema flag discloses omitted content: " + flag)
	var partial_names := fixture(13)
	partial_names.opening_guide.current_matches_truncated = true
	partial_names.opening_guide.current_matches_total = 2
	app.accept_state(partial_names)
	verify(app.opening_status.text.contains("이름은 일부만 표시"), "truncated current aliases are disclosed beside the current opening name")
	# 1. Nf3 d5 2. e3 is an unnamed catalog prefix, not a departure from it.
	var intermediate := fixture(14)
	intermediate.opening_guide.current_matches = []
	intermediate.opening_guide.current_matches_total = 0
	intermediate.opening_guide.matched_opening = {"name": "Zukertort Opening", "eco": "A06", "ply": 2, "match_type": "exact"}
	intermediate.opening_guide.continuations = [{"uci": "c7c5", "san": "c5", "immediate_openings": [],
		"reachable_openings": [{"name": "Zukertort Opening: Pachman Gambit", "eco": "A06", "distance_plies": 4,
			"remaining_uci": ["c7c5", "c2c4", "d5c4", "b2b3"], "remaining_san": "2...c5 3. c4 dxc4 4. b3"}]}]
	intermediate.opening_guide.continuations_total = 1
	app.accept_state(intermediate)
	verify(app.opening_summary.text == "마지막 확인: Zukertort Opening · A06" and app.opening_status.text.contains("중간 위치")
		and not app.opening_status.text.contains("등록 수순 밖"), "unnamed catalog prefix keeps the last name without falsely reporting an out-of-catalog position")
	app._on_opening_line_selected(0)
	verify(app.opening_preview.get_parsed_text() == "2...c5 3. c4 dxc4 4. b3", "an unnamed intermediate catalog position still previews its reachable named line")
	intermediate = intermediate.duplicate(true)
	intermediate.revision = 15
	intermediate.opening_guide.matched_opening = null
	app.accept_state(intermediate)
	verify(app.opening_summary.text == "등록된 오프닝 이름 없음" and app.opening_status.text.contains("중간 위치")
		and not app.opening_status.text.contains("등록 수순 밖"), "catalog prefix before any recognized name is also distinguished from leaving the catalog")
	intermediate = intermediate.duplicate(true)
	intermediate.revision = 16
	intermediate.opening_guide.status = "unknown"
	intermediate.opening_guide.matched_opening = {"name": "Zukertort Opening", "eco": "A06"}
	app.accept_state(intermediate)
	verify(not app.opening_status.text.contains("등록 수순 밖"), "only beyond_catalog may describe the current position as outside registered sequences")
	verify(app.captured_requests.is_empty(), "all synthetic opening interactions remain local")
	app.queue_free()
	await process_frame
	print("Godot opening-guide checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
