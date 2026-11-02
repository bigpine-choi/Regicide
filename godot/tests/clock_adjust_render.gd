extends SceneTree
## Synthetic offline screenshots of the shipped clock controls; no game host.
## --output-dir ABSOLUTE_DIRECTORY
const CaptureApp = preload("res://tests/clock_adjust_capture_scene.gd")


func _initialize() -> void:
	_run.call_deferred()


func _run() -> void:
	var output: String = ""
	var args: PackedStringArray = OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--output-dir":
			output = args[index + 1]
	if not output.is_absolute_path() or DirAccess.make_dir_recursive_absolute(output) != OK:
		push_error("An accessible absolute --output-dir is required")
		quit(1)
		return
	var viewport := SubViewport.new()
	viewport.size = Vector2i(1240, 860)
	viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	root.add_child(viewport)
	var app := CaptureApp.new()
	app.offline_mode = true
	viewport.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	app.backend_url = "http://127.0.0.1:1"
	var pieces: Dictionary = {}
	var back: String = "rnbqkbnr"
	for index in range(8):
		var file: String = "abcdefgh"[index]
		pieces[file + "1"] = back[index].to_upper()
		pieces[file + "2"] = "P"
		pieces[file + "7"] = "p"
		pieces[file + "8"] = back[index]
	app.accept_state({"game_id":"synthetic-clock-adjust", "revision":1,
		"fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "board":pieces,
		"human_color":"white", "human_to_move":true, "engine_to_move":false, "turn":"white",
		"legal_moves":["e2e4", "d2d4"], "moves":[], "last_move":null, "result":"*", "in_check":false,
		"can_undo":false, "error":null, "status":"당신 차례입니다.", "engine_thinking":false,
		"engine":{"name":"Regicide", "sparring_mode":true, "movetime_ms":1000, "settings_revision":0,
			"sparring":{"phase":"balance"}, "extra_budget_ms":0},
		"clock":{"enabled":true, "editable":true, "edit_revision":0, "control_id":"10+5", "label":"10+5 래피드",
			"white_ms":600000, "black_ms":590000, "running_color":"white", "status":"running",
			"turn_elapsed_ms":0, "sampled_monotonic_ms":1000}, "decision_support":null})
	app.boss_mode_option.select(app.boss_mode_option.get_item_index(2))
	app._on_game_mode_selected(2)
	app.clock_adjust_toggle.pressed.emit()
	var captures: Array[Dictionary] = []
	for size in [Vector2i(1240, 860), Vector2i(900, 680)]:
		viewport.size = size
		for frame in range(6):
			await process_frame
		app.sidebar_scroll.scroll_vertical = 0
		RenderingServer.force_draw(false)
		var image: Image = viewport.get_texture().get_image()
		var target: String = output.path_join("godot-click-clock-synthetic-%d.png" % size.x)
		if FileAccess.file_exists(target) or image == null or image.is_empty() or image.save_png(target) != OK:
			push_error("Cannot save new screenshot: " + target)
			quit(1)
			return
		captures.append({"file":target, "width":size.x, "height":size.y, "synthetic":true,
			"panel_rect":str(app.clock_adjust_panel.get_global_rect()), "manual_play":false})
	var manifest := FileAccess.open(output.path_join("manifest.json"), FileAccess.WRITE)
	manifest.store_string(JSON.stringify({"render_mode":"synthetic offline; no HTTP or engine", "captures":captures}, "  "))
	manifest.close()
	app.queue_free()
	viewport.queue_free()
	await process_frame
	print("Clock adjustment screenshots: ", output)
	quit(0)
