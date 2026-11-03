extends SceneTree
## Offline rendering of saved own-engine reports and independent legal special moves.

var panel: Variant
var folder: String
var records: Array = []


func _initialize() -> void:
	_run.call_deferred()


func capture(name: String, finish_motion: bool = true) -> void:
	for _frame in range(12):
		await process_frame
	if finish_motion:
		panel.board.finish_animation()
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(folder.path_join(name + ".png"))
	var grid: Rect2 = panel.board.get_board_rect()
	var badge: Rect2 = panel.board.move_badge_rect()
	records.append({"image": name + ".png", "viewport": [root.size.x, root.size.y],
		"grid": [grid.position.x, grid.position.y, grid.size.x, grid.size.y],
		"badge": [badge.position.x, badge.position.y, badge.size.x, badge.size.y],
		"annotation": panel.board.move_annotation, "fen": panel.board.state.get("fen"),
		"animating": panel.board.is_animating(), "retry": panel.retry_active})


func show_report(report: Dictionary, ply: int) -> void:
	panel._accept_snapshot(report, true)
	panel._select_ply(ply)
	panel.model.view_from_white = true
	panel._render()
	panel.find_child("CoachingSidebarScroll", true, false).scroll_vertical = 0


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() < 4 or DisplayServer.get_name() == "headless":
		printerr("Pass native fixture, actual Fool's Mate report, actual great-candidate report and output folder on a rendered display.")
		quit(1)
		return
	for path in args.slice(0, 3):
		if not FileAccess.file_exists(path):
			printerr("Fixture missing; pass an absolute path: ", path)
			quit(1)
			return
	var fixture: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[0]))
	var actual: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[1]))
	var great: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[2]))
	folder = args[3]
	DirAccess.make_dir_recursive_absolute(folder)
	root.title = "Regicide Move Badges · Private Render Fixture"
	var app = load("res://main.tscn").instantiate()
	app.offline_mode = true
	root.add_child(app)
	app.main_contents.hide()
	panel = app.coaching_overlay
	panel._speech_enabled = false
	panel.offline_mode = true
	panel.open_game(fixture.pgn, "white", "", "")
	for dimensions in [Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = dimensions
		root.content_scale_size = dimensions
		var suffix: String = "-%dx%d" % [dimensions.x, dimensions.y]
		show_report(actual, 3)
		await capture("native-actual-blunder" + suffix)
		show_report(actual, 4)
		await capture("native-actual-best" + suffix)
		show_report(great, 1)
		await capture("native-actual-great" + suffix)
		panel.flip_view()
		await capture("native-great-flipped" + suffix)
		panel._show_phase(false)
		await capture("native-before-no-badge" + suffix)
		panel._begin_retry()
		await capture("native-retry-no-badge" + suffix)
		show_report(fixture.badge_cases[0].report, 1)
		await capture("native-castling" + suffix)
		show_report(fixture.badge_cases[-1].report, 1)
		await capture("native-promotion-edge" + suffix)
		panel.flip_view()
		await capture("native-promotion-edge-flipped" + suffix)
		show_report(actual, 3)
		panel.board.state = actual.frames[2]
		panel.board.finish_animation()
		panel._select_ply(3)
		panel.board.set_process(false)
		panel.board._motion_elapsed = 0.1
		panel.board.queue_redraw()
		await capture("native-moving-no-badge" + suffix, false)
		panel.board.finish_animation()
	var file := FileAccess.open(folder.path_join("native-badge-render.json"), FileAccess.WRITE)
	file.store_string(JSON.stringify({"mode": "private offline renderer; actual own-engine reports for !, star and blunder",
		"great_source": args[2], "actual_source": args[1], "captures": records}, "  "))
	file.close()
	print("Rendered ", records.size(), " badge states. No live game or preferences modified.")
	app.queue_free()
	await process_frame
	quit()
