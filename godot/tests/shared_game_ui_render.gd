extends SceneTree
## Render actual common game controls in a private offline project; no live host.
const CaptureApp = preload("res://tests/think_time_capture_scene.gd")
var app: Variant
var folder: String
var captures: Array = []
var failures: int = 0

func _initialize() -> void:
	_run.call_deferred()

func verify(ok: bool, message: String) -> void:
	if not ok:
		failures += 1
		printerr("FAIL: ", message)

func capture(name: String) -> void:
	for _frame in range(16):
		await process_frame
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(folder.path_join(name + ".png")) == OK, "capture saved")
	var rect: Rect2 = app.move_history.get_global_rect()
	var rows: int = int(ceil(float(app.state.moves.size()) / 2))
	var row_height: float = float(app.move_history.get_content_height()) / rows
	var visible_rows: int = mini(rows, int(floor((rect.size.y - 8) / row_height)))
	if app.sidebar_tabs.current_tab == 1:
		verify(visible_rows >= 8, "record tab uses remaining height at " + str(root.size))
		verify(app.save_button.get_global_rect().end.y <= app.sidebar_scroll.get_global_rect().end.y + 2, "PGN save remains visible")
		verify(app.sidebar_scroll.get_v_scroll_bar().max_value <= app.sidebar_scroll.size.y + 2, "outer record panel does not need a second vertical scroll")
	captures.append({"image": name + ".png", "viewport": [root.size.x, root.size.y], "tab": app.sidebar_tabs.current_tab,
		"history_rect": [rect.position.x, rect.position.y, rect.size.x, rect.size.y], "visible_fullmove_rows":visible_rows,
		"history_total_rows": rows, "row_height": row_height, "clock_adjust_visible": app.clock_adjust_toggle.visible,
		"rating_input_visible":app.rating_label_panel.visible, "evaluation": app.evaluation_summary.text})

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() < 2 or DisplayServer.get_name() == "headless":
		printerr("Pass fixture and output directory on a rendered display")
		quit(1)
		return
	folder = args[1]
	DirAccess.make_dir_recursive_absolute(folder)
	var state: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[0]))
	app = CaptureApp.new()
	app.offline_mode = true
	root.title = "Regicide · Private Common UI Verification"
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state(state)
	app.accept_evaluation(state.evaluation)
	app.select_sidebar_tab(1)
	for dimensions in [Vector2i(1760, 960), Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = dimensions
		root.content_scale_size = dimensions
		await capture("normal-records-%dx%d" % [dimensions.x, dimensions.y])
	for mode in ["normal", "boss", "sparring"]:
		state.revision += 1
		state.engine.boss_mode = mode == "boss"
		state.engine.sparring_mode = mode == "sparring"
		state.evaluation.revision = state.revision
		state.evaluation.reference_evaluation.revision = state.revision
		app.accept_state(state)
		app.accept_evaluation(state.evaluation)
		app.select_sidebar_tab(0)
		app._clock_adjust_expanded = true
		app._render_clock_adjustments()
		verify(app.clock_adjust_toggle.visible and not app.clock_adjust_buttons.human[2].disabled, mode + " clock edits")
		root.size = Vector2i(1240, 860)
		root.content_scale_size = root.size
		await capture(mode + "-clock-controls")
		state.evaluation.reference_evaluation.status = "loading"
		app.accept_evaluation(state.evaluation)
		verify(not app.evaluation_bar.has_score and is_equal_approx(app.evaluation_bar.white_share, 0.5), mode + " waits neutrally for Stockfish")
		await capture(mode + "-stockfish-pending")
		state.evaluation.reference_evaluation.status = "ready"
		state.revision += 1
		state.result = "1/2-1/2"
		app.accept_state(state)
		verify(app.rating_label_panel.visible and not app.rating_label_save.disabled, mode + " rating input without estimate")
		await capture(mode + "-completed-rating")
		state.result = "*"
	var file := FileAccess.open(folder.path_join("native-shared-ui-render.json"), FileAccess.WRITE)
	file.store_string(JSON.stringify({"scope":"offline replay of recorded move history with synthetic common metadata; no live user game", "captures":captures, "failures":failures}, "  "))
	file.close()
	app.free()
	print("Shared UI render: %d captures, %d failures" % [captures.size(), failures])
	quit(1 if failures else 0)
