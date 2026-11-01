extends SceneTree
## Render real animation frames at known times, without desktop mouse input.

func _initialize() -> void:
	_run.call_deferred()

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	var case_path := ""
	var output := ""
	for index in range(args.size() - 1):
		if args[index] == "--cases-file":
			case_path = args[index + 1]
		if args[index] == "--output":
			output = args[index + 1]
	root.size = Vector2i(760, 760)
	root.content_scale_size = Vector2i(760, 760)
	root.title = "Regicide Animation Render QA"
	var background := ColorRect.new()
	background.color = Color("0c1422")
	background.size = Vector2(760, 760)
	root.add_child(background)
	var board = load("res://scripts/chess_board.gd").new()
	board.position = Vector2(25, 25)
	board.size = Vector2(710, 710)
	root.add_child(board)
	await process_frame
	var cases: Array = JSON.parse_string(FileAccess.get_file_as_string(case_path))
	DirAccess.make_dir_recursive_absolute(output)
	for case_index in [1, 4, 12]:
		var fixture: Dictionary = cases[case_index]
		board.state = fixture.before
		board.finish_animation()
		board.state = fixture.after
		board.set_process(false)
		var duration: float = board._motion_duration
		for index in range(9):
			board._motion_elapsed = float(index) / 8.0 * (duration + board.LANDING_SECONDS - 0.001)
			board.queue_redraw()
			await process_frame
			await RenderingServer.frame_post_draw
			var bitmap: Image = root.get_texture().get_image()
			var result: int = bitmap.save_png(output.path_join("case-%02d-frame-%02d.png" % [case_index, index]))
			if result != OK:
				printerr("Unable to save animation frame")
				quit(1)
				return
	print("Rendered knight, castling and capture-promotion: 27 real animation frames")
	quit()
