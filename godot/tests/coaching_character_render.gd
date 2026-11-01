extends SceneTree
## Private offline fixture captures. No backend, saved games, preferences or speech.

var app: Variant
var panel: Variant
var folder: String
var captures: Array = []


func _initialize() -> void:
	_run.call_deferred()


func capture(name: String) -> void:
	for _frame in range(12):
		await process_frame
	panel.board.finish_animation()
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(folder.path_join(name + ".png"))
	var board: Rect2 = panel.board.get_board_rect()
	var sidebar: Rect2 = panel.find_child("CoachingSidebar", true, false).get_global_rect()
	captures.append({"image": name + ".png", "viewport": [root.size.x, root.size.y],
		"board": [board.position.x, board.position.y, board.size.x, board.size.y],
		"sidebar": [sidebar.position.x, sidebar.position.y, sidebar.size.x, sidebar.size.y],
		"retry": panel.retry_active, "speech_started": panel._speech_owned})


func _run() -> void:
	var args: PackedStringArray = OS.get_cmdline_user_args()
	if args.size() < 2 or DisplayServer.get_name() == "headless":
		printerr("Use a rendered display and pass fixture JSON and output folder.")
		quit(1)
		return
	var fixture: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[0]))
	folder = args[1]
	DirAccess.make_dir_recursive_absolute(folder)
	root.title = "Regicide Character Coaching · Private Render Fixture"
	app = load("res://main.tscn").instantiate()
	app.offline_mode = true
	root.add_child(app)
	app.main_contents.hide()
	panel = app.coaching_overlay
	panel.offline_mode = true
	panel.open_game(fixture.pgn, "white", "", "")
	for dimensions in [Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = dimensions
		root.content_scale_size = dimensions
		var suffix: String = "-%dx%d" % [dimensions.x, dimensions.y]
		panel._accept_snapshot(fixture.report, true)
		panel._select_ply(3)
		var scroll: ScrollContainer = panel.find_child("CoachingSidebarScroll", true, false)
		scroll.scroll_vertical = 0
		await capture("native-story" + suffix)
		panel._toggle_coach_overview()
		await capture("native-overview" + suffix)
		panel._toggle_coach_overview()
		panel._show_evidence(fixture.report.moves[2].coach.evidence[0], 3, fixture.report.job_id, fixture.report.moves[2].fen_before)
		await capture("native-evidence" + suffix)
		panel._begin_retry()
		await capture("native-retry" + suffix)
		panel._accept_snapshot(fixture.promotion_report, true)
		panel._select_ply(1)
		panel._begin_retry()
		panel._on_retry_square("a7")
		panel._on_retry_square("a8")
		await capture("native-promotion" + suffix)
	var voices: Array = []
	if DisplayServer.has_feature(DisplayServer.FEATURE_TEXT_TO_SPEECH):
		for voice in DisplayServer.tts_get_voices():
			if str(voice.get("language", "")).begins_with("ko"):
				voices.append({"name": voice.get("name"), "language": voice.get("language")})
	var file := FileAccess.open(folder.path_join("native-character-render.json"), FileAccess.WRITE)
	file.store_string(JSON.stringify({"mode": "offline rendered fixture; no speech played", "captures": captures,
		"korean_voices": voices, "korean_voice_available": not panel._korean_voice.is_empty()}, "  "))
	file.close()
	print("Captured ", captures.size(), " native character screens. Korean voice available: ", not panel._korean_voice.is_empty(), "; no speech played.")
	app.queue_free()
	await process_frame
	quit()
