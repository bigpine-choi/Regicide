extends SceneTree
## Render the actual client in an offscreen viewport with real catalog data.


func _initialize() -> void:
	_run.call_deferred()


func draw_image(viewport: SubViewport, destination: String) -> bool:
	await process_frame
	await process_frame
	RenderingServer.force_draw(false)
	var image: Image = viewport.get_texture().get_image()
	if image == null or image.is_empty():
		printerr("Opening capture has no rendered image.")
		return false
	return image.save_png(destination) == OK


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	var state_file: String = ""
	var output: String = ""
	for index in range(args.size() - 1):
		if args[index] == "--state-file":
			state_file = args[index + 1]
		elif args[index] == "--output":
			output = args[index + 1]
	var shown: Variant = JSON.parse_string(FileAccess.get_file_as_string(state_file))
	if not shown is Dictionary or output.is_empty():
		printerr("Opening capture requires a real catalog state and output folder.")
		quit(1)
		return
	var viewport := SubViewport.new()
	viewport.size = Vector2i(1280, 900)
	viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	root.add_child(viewport)
	var app: Variant = load("res://main.tscn").instantiate()
	app.offline_mode = true
	viewport.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	app.accept_state(shown)
	for frame in range(4):
		await process_frame
	var ok: bool = await draw_image(viewport, output.path_join("opening-compact.png"))
	app._on_opening_toggle_pressed()
	var selected: int = 0
	for index in range(app._opening_rows.size()):
		if int(app._opening_rows[index].get("distance_plies", 1)) > 1:
			selected = index
			break
	app._on_opening_line_selected(selected)
	ok = await draw_image(viewport, output.path_join("opening-expanded.png")) and ok
	var details := {"rendering": "hidden Windows graphics context, offscreen SubViewport, actual application controls",
		"manual_play": false, "catalog_state_file": state_file, "size": [1280, 900],
		"opening_summary": app.opening_summary.text, "opening_status": app.opening_status.text,
		"selected_name": app.opening_preview_title.text, "remaining_san": app.opening_preview.get_parsed_text(),
		"live_position_unchanged": JSON.stringify(app.state) == JSON.stringify(shown), "success": ok}
	var file := FileAccess.open(output.path_join("capture-result.json"), FileAccess.WRITE)
	if file != null:
		file.store_string(JSON.stringify(details, "  ") + "\n")
	app.queue_free()
	viewport.queue_free()
	await process_frame
	print("Opening guide offscreen rendered capture: ", "PASS" if ok else "FAIL")
	quit(0 if ok else 1)
