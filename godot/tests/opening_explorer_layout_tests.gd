extends SceneTree
## Synthetic display data only. No player files, credentials, or public calls.

const Main = preload("res://scripts/main.gd")
const Analysis = preload("res://scripts/analysis_mode.gd")
const START := "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
var checks: int = 0
var failures: int = 0
var evidence_dir: String = ""
var app: Variant
var analysis: Variant


func _initialize() -> void:
	_run.call_deferred()


func verify(ok: bool, message: String) -> void:
	checks += 1
	if not ok:
		failures += 1
		printerr("FAIL: ", message)


func state_fixture() -> Dictionary:
	var pieces := {}
	var files := "abcdefgh"
	for index in range(8):
		pieces[files[index] + "1"] = "RNBQKBNR"[index]
		pieces[files[index] + "2"] = "P"
		pieces[files[index] + "7"] = "p"
		pieces[files[index] + "8"] = "rnbqkbnr"[index]
	return {"game_id": "layout-fixture", "revision": 1, "fen": START, "board": pieces,
		"turn": "white", "human_color": "white", "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["e2e4"], "moves": [], "last_move": null, "result": "*", "in_check": false,
		"can_undo": true, "can_handicap": false, "engine_thinking": false, "status": "배치 검증 화면",
		"engine": {"name": "Regicide · 화면 검증", "movetime_ms": 1000},
		"clock": {"enabled": false}}


func statistics(count: int) -> Dictionary:
	var moves := []
	for index in range(count):
		moves.append({"uci": "e2e4", "san": "e4" if index % 2 == 0 else "d4", "count": 6000,
			"play_pct": 40.0, "white_pct": 45.0, "draw_pct": 35.0, "black_pct": 20.0,
			"fen_after": START})
	return {"status": "ready", "configured": true, "source": "masters", "fen": START,
		"total": 15000, "fetched_at": "2026-11-03T02:00:00Z", "moves": moves,
		"games": [{"white": "White fixture", "black": "Black fixture", "year": 2026,
			"url": "https://lichess.org/import/master/Abc12345/white"}]}


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	DirAccess.make_dir_recursive_absolute(evidence_dir)
	await process_frame
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(evidence_dir.path_join(name + ".png")) == OK,
		"saved synthetic layout " + name)


func settled() -> void:
	await process_frame
	await process_frame
	await process_frame


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--evidence-dir":
			evidence_dir = args[index + 1]
	root.size = Vector2i(1280, 900)
	root.content_scale_size = Vector2i(1280, 900)
	app = Main.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state(state_fixture())
	await settled()
	verify(app.sidebar_tabs.tab_count == 3 and app.sidebar_tabs.current_tab == 0, "game, opening and record tabs start on game")
	verify(not app.new_game_settings.is_visible_in_tree() and app.new_button.is_visible_in_tree(), "new-game configuration is folded while launch stays accessible")
	verify(app.time_option.is_visible_in_tree() and app.current_time_controls.is_ancestor_of(app.time_option), "current AI think time remains available outside folded new-game settings")
	var board_size: Vector2 = app.board.get_global_rect().size
	await capture("main-game-1280")
	app.select_sidebar_tab(1)
	app.opening_explorer.enabled_toggle.button_pressed = true
	app.opening_explorer.render_snapshot(statistics(1))
	await settled()
	var tabs_rect: Rect2 = app.sidebar_tabs.get_global_rect()
	var list_height: float = app.opening_explorer.moves_scroll.size.y
	app.opening_explorer.render_snapshot(statistics(100))
	await settled()
	verify(app.sidebar_pages[1].is_visible_in_tree() and not app.sidebar_pages[0].is_visible_in_tree() and not app.sidebar_pages[2].is_visible_in_tree(), "only opening content is visible")
	verify(app.undo_button.is_visible_in_tree() and app.hint_button.is_visible_in_tree(), "undo and hints stay available across tabs")
	verify(app.opening_explorer.rows.get_child_count() == 100 and app.opening_explorer.moves_scroll.size.y <= list_height + 1 and list_height <= 250, "one hundred moves stay in a bounded compact list")
	verify(app.sidebar_tabs.get_global_rect() == tabs_rect and app.board.get_global_rect().size == board_size, "statistics do not move navigation or resize board")
	verify(not app.opening_explorer.token_input.is_visible_in_tree(), "connected statistics do not show key instructions or entry")
	app.sidebar_scroll.scroll_vertical = int(app.opening_explorer.position.y)
	await capture("main-opening-1280")
	var preferences: Dictionary = app.opening_explorer.preferences()
	app.select_sidebar_tab(2)
	await settled()
	verify(app.move_history.is_visible_in_tree() and app.library_button.is_visible_in_tree() and not app.opening_explorer.is_visible_in_tree(), "record tools have a separate page")
	verify(app.opening_explorer.preferences() == preferences and app.opening_explorer._poll_timer.is_stopped(), "leaving opening preserves settings and stops polling")
	await capture("main-record-1280")
	app.select_sidebar_tab(0)
	app._toggle_new_game_settings()
	verify(app.new_game_settings.is_visible_in_tree(), "configuration opens on explicit request")
	app._toggle_new_game_settings()
	app.select_sidebar_tab(1)
	app.board_model.pending_promotions.assign(["a7a8q", "a7a8r", "a7a8b", "a7a8n"])
	app._render()
	await settled()
	verify(app.sidebar_tabs.current_tab == 0 and app.promotion_panel.is_visible_in_tree(), "promotion automatically reveals game controls")
	app.board_model.pending_promotions.clear()
	app.state.handicap_mode = true
	app._render()
	app.select_sidebar_tab(2)
	verify(app.handicap_cancel_button.is_visible_in_tree(), "replacement cancel remains outside every page")
	app.state.handicap_mode = false
	var detail := {"frames": [state_fixture()], "moves": [], "summary": {"human_color": "white", "source_kind": "pgn"}, "pgn": "*"}
	verify(app._adopt_review(detail), "review fixture opens")
	app._render()
	app.select_sidebar_tab(1)
	verify(app.sidebar_return_live.is_visible_in_tree(), "return-to-live stays visible while reviewing an opening")
	app._exit_review()
	verify(app.sidebar_tabs.current_tab == 0 and not app.review_mode, "leaving review restores game page")
	for viewport_size in [Vector2i(1280, 900), Vector2i(900, 680)]:
		root.size = viewport_size
		root.content_scale_size = viewport_size
		await settled()
		for selected in range(3):
			app.select_sidebar_tab(selected)
			if selected == 1:
				app.opening_explorer.render_snapshot(statistics(100))
			await settled()
			app.sidebar_scroll.scroll_vertical = 100000
			await settled()
			var tab_area: Rect2 = app.sidebar_tabs.get_global_rect()
			verify(tab_area.position.y >= 0 and tab_area.end.x <= viewport_size.x + 1 and tab_area.end.y < viewport_size.y and app.undo_button.is_visible_in_tree(), "main navigation stays onscreen at %s tab %d" % [viewport_size, selected])
			verify(app.sidebar_scroll.get_global_rect().position.y >= tab_area.end.y, "scrolled body cannot cover tabs")
			if viewport_size.x == 900:
				app.sidebar_scroll.scroll_vertical = int(app.opening_explorer.position.y) if selected == 1 else 0
				await capture("main-%d-900" % selected)
	app.select_sidebar_tab(1)
	app.board_model.pending_promotions.assign(["a7a8q", "a7a8r", "a7a8b", "a7a8n"])
	app._render()
	await settled()
	var promotion_area: Rect2 = app.promotion_panel.get_global_rect()
	var scroll_area: Rect2 = app.sidebar_scroll.get_global_rect()
	verify(app.sidebar_tabs.current_tab == 0 and promotion_area.position.y >= scroll_area.position.y - 1 and promotion_area.end.y <= scroll_area.end.y + 1, "small-window promotion is automatically scrolled fully into view")
	app.board_model.pending_promotions.clear()
	app.hide()
	analysis = Analysis.new()
	analysis.offline_mode = true
	analysis.theme = app.theme
	root.add_child(analysis)
	await analysis.open_position(START, "http://127.0.0.1:1", "fixture")
	analysis.select_sidebar_tab(1)
	analysis.opening_explorer.enabled_toggle.button_pressed = true
	analysis.opening_explorer.render_snapshot(statistics(100))
	await settled()
	verify(analysis.sidebar_tabs.tab_count == 2 and not analysis.editor_controls.is_visible_in_tree(), "analysis has separate controls and opening pages")
	verify(analysis.start_button.is_visible_in_tree() and analysis.undo_button.is_visible_in_tree() and analysis.edit_button.is_visible_in_tree(), "analysis common actions remain above every page")
	var analysis_tabs: Rect2 = analysis.sidebar_tabs.get_global_rect()
	analysis.sidebar_scroll.scroll_vertical = 100000
	await settled()
	verify(analysis.sidebar_tabs.get_global_rect() == analysis_tabs and analysis_tabs.end.x <= 901, "analysis tabs stay fixed at smallest window with hundred moves")
	analysis.sidebar_scroll.scroll_vertical = 0
	await capture("analysis-opening-900")
	analysis.select_sidebar_tab(0)
	await settled()
	verify(analysis.editor_controls.is_visible_in_tree() and not analysis.opening_explorer.is_visible_in_tree(), "analysis controls return without reconnecting")
	await capture("analysis-controls-900")
	analysis.free()
	app.free()
	print("Opening explorer layout: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)
