extends SceneTree
## Private offline layout and approved-symbol captures. No user game or speech.

var app: Variant
var panel: Variant
var folder: String
var captures: Array = []


func _initialize() -> void:
	_run.call_deferred()


func capture(name: String) -> void:
	for _frame in range(16):
		await process_frame
	panel.board.finish_animation()
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(folder.path_join(name + ".png"))
	var notation: Rect2 = panel.move_list.get_global_rect()
	var badge: Rect2 = panel.board.move_badge_rect()
	captures.append({"image": name + ".png", "viewport": [root.size.x, root.size.y],
		"visible_move_rows": int(notation.size.y / panel.move_list.get_item_rect(0).size.y),
		"move_list": [notation.position.x, notation.position.y, notation.size.x, notation.size.y],
		"badge": [badge.position.x, badge.position.y, badge.size.x, badge.size.y],
		"symbol": panel.board.move_annotation.get("symbol", ""),
		"primary_font_has_thumb_glyph": panel.board.label_font.has_char(0x1f44d),
		"key_plies": panel._key_plies(), "selected_ply": panel.selected_ply})


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() < 2 or DisplayServer.get_name() == "headless":
		printerr("Pass fixture JSON and output folder on a rendered display.")
		quit(1)
		return
	var fixture: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[0]))
	folder = args[1]
	DirAccess.make_dir_recursive_absolute(folder)
	root.title = "Regicide Coaching Navigation · Private Offline Fixture"
	app = load("res://main.tscn").instantiate()
	app.offline_mode = true
	root.add_child(app)
	app.main_contents.hide()
	panel = app.coaching_overlay
	panel._speech_enabled = false
	panel.offline_mode = true
	panel.open_game(fixture.pgn, "white", "", "")
	for dimensions in [Vector2i(1760, 960), Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = dimensions
		root.content_scale_size = dimensions
		panel._accept_snapshot(fixture.navigation_report, true)
		panel._select_ply(1)
		panel.find_child("CoachingSidebarScroll", true, false).scroll_vertical = 0
		var suffix: String = "-%dx%d" % [dimensions.x, dimensions.y]
		await capture("native-key-scenes-thumbs-up" + suffix)
		panel.key_next.pressed.emit()
		await capture("native-key-scene-jump" + suffix)
		while not panel.key_next.disabled:
			panel.key_next.pressed.emit()
		await capture("native-last-key-scene" + suffix)
	var file := FileAccess.open(folder.path_join("native-navigation-render.json"), FileAccess.WRITE)
	file.store_string(JSON.stringify({"mode": "private offline synthetic legal positions; no engine-strength claim", "captures": captures}, "  "))
	file.close()
	print("Rendered ", captures.size(), " coaching navigation states. No user game or speech used.")
	app.queue_free()
	await process_frame
	quit()
