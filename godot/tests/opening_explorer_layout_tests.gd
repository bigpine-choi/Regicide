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
	root.size = Vector2i(1760, 960)
	root.content_scale_size = root.size
	app = Main.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state(state_fixture())
	await settled()
	app.opening_explorer.render_snapshot(statistics(100))
	await settled()
	verify(app.sidebar_tabs.tab_count == 2 and app.sidebar_tabs.current_tab == 0, "game and record controls retain their own tabs")
	verify(app.opening_explorer.is_visible_in_tree() and app.opening_explorer.find_child("ExplorerEnabled", true, false) == null, "opening statistics are always visible without an on-off control")
	verify(not app.new_game_settings.visible and not app.opening_details.visible, "optional new-game and opening catalog details start folded")
	var workspace: Variant = app.find_child("OpeningWorkspace", true, false)
	var opening_rect: Rect2 = workspace.opening_panel.get_global_rect()
	verify(not workspace.vertical and opening_rect.position.x >= app.sidebar_tabs.get_global_rect().end.x, "wide window gives openings a dedicated right column")
	verify(opening_rect.size.x >= 430 and opening_rect.end.x <= 1760, "opening column is at least 430 pixels and fits the window")
	verify(app.opening_explorer.rows.get_child(0).size.y >= 76 and app.opening_explorer.moves_scroll.size.y >= 330 and app.opening_explorer.moves_scroll.size.y <= 335, "readable move rows use a bounded scrolling list for a hundred results")
	var board_rect: Rect2 = app.board.get_global_rect()
	app.select_sidebar_tab(1)
	await settled()
	verify(app.opening_explorer.is_visible_in_tree() and app.sidebar_pages[1].visible and app.board.get_global_rect() == board_rect, "record navigation preserves opening visibility and board geometry")
	app.select_sidebar_tab(0)
	await capture("main-wide-opening")
	app.board_model.pending_promotions.assign(["a7a8q", "a7a8r", "a7a8b", "a7a8n"])
	app.select_sidebar_tab(1)
	app._render()
	await settled()
	verify(app.sidebar_tabs.current_tab == 0 and app.promotion_panel.is_visible_in_tree(), "promotion reveals game controls while openings remain separate")
	app.board_model.pending_promotions.clear()
	for viewport_size in [Vector2i(1280, 900), Vector2i(900, 680)]:
		root.size = viewport_size
		root.content_scale_size = viewport_size
		app._render()
		await settled()
		app.opening_explorer.render_snapshot(statistics(100))
		await settled()
		verify(workspace.vertical and workspace.opening_panel.position.y >= workspace.get_child(0).size.y, "small windows place the full opening panel below the board")
		var page_scroll: ScrollContainer = app.find_child("WorkspaceScroll", true, false)
		page_scroll.scroll_vertical = 0
		await settled()
		verify(app.sidebar_tabs.get_global_rect().end.x <= viewport_size.x and app.undo_button.is_visible_in_tree(), "small-window game navigation stays within the viewport")
		page_scroll.scroll_vertical = int(workspace.opening_panel.position.y)
		await settled()
		verify(app.opening_explorer.get_global_rect().end.x <= viewport_size.x, "small-window openings remain readable without horizontal clipping")
		await capture("main-opening-%d" % viewport_size.x)
		page_scroll.scroll_vertical = 0
	app.hide()
	root.size = Vector2i(1760, 960)
	root.content_scale_size = root.size
	analysis = Analysis.new()
	analysis.offline_mode = true
	analysis.theme = app.theme
	root.add_child(analysis)
	await analysis.open_position(START, "http://127.0.0.1:1", "fixture")
	analysis.opening_explorer.render_snapshot(statistics(100))
	await settled()
	var analysis_workspace: Variant = analysis.find_child("OpeningWorkspace", true, false)
	verify(analysis.editor_controls.is_visible_in_tree() and analysis.opening_explorer.is_visible_in_tree(), "analysis editor and statistics can be used together")
	verify(not analysis_workspace.vertical and analysis_workspace.opening_panel.get_global_rect().position.x >= analysis.sidebar_tabs.get_global_rect().end.x, "analysis uses the same wide opening column")
	verify(analysis.start_button.is_visible_in_tree() and analysis.undo_button.is_visible_in_tree(), "analysis common actions stay available beside statistics")
	await capture("analysis-wide-opening")
	root.size = Vector2i(900, 680)
	root.content_scale_size = root.size
	await settled()
	var analysis_scroll: ScrollContainer = analysis.find_child("AnalysisWorkspaceScroll", true, false)
	analysis_scroll.scroll_vertical = int(analysis_workspace.opening_panel.position.y)
	await settled()
	verify(analysis_workspace.vertical and analysis.opening_explorer.get_global_rect().end.x <= 900, "small analysis window scrolls to full-width statistics")
	await capture("analysis-opening-900")
	analysis.free()
	app.free()
	print("Opening explorer layout: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)
