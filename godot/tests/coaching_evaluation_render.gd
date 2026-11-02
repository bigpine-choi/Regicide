extends SceneTree
## Render a private saved report without opening a backend or changing a live game.

var panel: Variant
var folder: String
var records: Array = []


func _initialize() -> void:
	_run.call_deferred()


func capture(name: String) -> void:
	for _frame in range(12):
		await process_frame
	panel.board.finish_animation()
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(folder.path_join(name + ".png"))
	records.append({"image": name + ".png", "fen": panel.board.state.get("fen"),
		"evaluation": panel.evaluation_bar.snapshot, "has_score": panel.evaluation_bar.has_score,
		"label": panel.evaluation_label.text, "white_share": panel.evaluation_bar.white_share,
		"view_from_white": panel.model.view_from_white})


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() < 2 or DisplayServer.get_name() == "headless":
		printerr("Pass an actual Fool's Mate coaching report and output directory on a rendered display.")
		quit(1)
		return
	var report: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[0]))
	folder = args[1]
	DirAccess.make_dir_recursive_absolute(folder)
	root.title = "Regicide Evaluation - Private Verification"
	root.size = Vector2i(1240, 860)
	root.content_scale_size = root.size
	var app = load("res://main.tscn").instantiate()
	app.offline_mode = true
	root.add_child(app)
	app.main_contents.hide()
	panel = app.coaching_overlay
	panel._speech_enabled = false
	panel.offline_mode = true
	panel.open_game("1. f3 e5 2. g4 Qh4# 0-1", "white", "", "")
	panel._accept_snapshot(report, true)
	panel._select_ply(1)
	await capture("native-after-f3")
	panel._select_ply(3)
	await capture("native-black-mate-threat")
	panel.flip_view()
	await capture("native-black-mate-threat-flipped")
	panel.flip_view()
	panel._select_ply(4)
	await capture("native-terminal-black-win")
	panel._select_ply(3)
	panel._show_line("best")
	panel._line_step(1)
	await capture("native-unanalysed-pv")
	panel._select_ply(3)
	panel._begin_retry()
	await capture("native-retry-hidden")
	panel._end_retry()
	panel._select_ply(3)
	var file := FileAccess.open(folder.path_join("native-evaluation-render.json"), FileAccess.WRITE)
	file.store_string(JSON.stringify({"source": args[0], "captures": records,
		"mode": "private offline rendering of actual own-engine report"}, "  "))
	file.close()
	print("Rendered ", records.size(), " evaluation states without accessing live game files.")
	if "--hold" not in args:
		app.queue_free()
		await process_frame
		quit()
