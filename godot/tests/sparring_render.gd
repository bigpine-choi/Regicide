extends SceneTree
## Offline capture of supplied {state, evaluation}; no HTTP or engine launch.
## --fixture ABSOLUTE_JSON --output-dir ABSOLUTE_DIRECTORY
## --synthetic marks a constructed input; --synthetic-samples adds labelled variants.

var output: String = ""
var captures: Array[Dictionary] = []


func _initialize() -> void:
	_run.call_deferred()


func _capture(app: Variant, viewport: SubViewport, width: int, height: int,
		name: String, synthetic: bool, overrides: Array = []) -> bool:
	viewport.size = Vector2i(width, height)
	for _frame in range(6):
		await process_frame
	app.sidebar_scroll.ensure_control_visible(app.new_button)
	await process_frame
	RenderingServer.force_draw(false)
	var image: Image = viewport.get_texture().get_image()
	var target: String = output.path_join(name + ".png")
	if FileAccess.file_exists(target):
		push_error("Capture already exists; choose a new output directory: " + target)
		return false
	if image == null or image.is_empty() or image.save_png(target) != OK:
		push_error("Could not save rendered capture: " + target)
		return false
	captures.append({"file": target, "width": width, "height": height,
		"synthetic": synthetic, "overrides": overrides,
		"selected_mode": app.boss_mode_option.get_item_text(app.boss_mode_option.selected),
		"mode_help": app.game_mode_help.text, "header": app.boss_phase_label.text,
		"sparring_state": app.state.get("engine", {}).get("sparring"),
		"evaluation_summary": app.evaluation_summary.text,
		"evaluation_detail": app.evaluation_detail.text,
		"board_rect": str(app.board.get_global_rect()),
		"mode_rect": str(app.boss_mode_option.get_global_rect()),
		"new_button_rect": str(app.new_button.get_global_rect())})
	return true


func _run() -> void:
	var args: PackedStringArray = OS.get_cmdline_user_args()
	var fixture_path: String = ""
	for index in range(args.size() - 1):
		if args[index] == "--fixture":
			fixture_path = args[index + 1]
		elif args[index] == "--output-dir":
			output = args[index + 1]
	if not fixture_path.is_absolute_path() or not output.is_absolute_path():
		push_error("--fixture and --output-dir must be absolute paths")
		quit(1)
		return
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(fixture_path))
	if not parsed is Dictionary or not parsed.get("state") is Dictionary or not parsed.get("evaluation") is Dictionary:
		push_error("Fixture must contain state and evaluation objects")
		quit(1)
		return
	if DirAccess.make_dir_recursive_absolute(output) != OK or FileAccess.file_exists(output.path_join("manifest.json")):
		push_error("Use an accessible output directory without a previous manifest")
		quit(1)
		return
	var original: Dictionary = parsed.state.duplicate(true)
	var estimate: Dictionary = parsed.evaluation.duplicate(true)
	var synthetic_input: bool = args.has("--synthetic")
	var viewport := SubViewport.new()
	viewport.size = Vector2i(1240, 860)
	viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	root.add_child(viewport)
	var app: Variant = load("res://main.tscn").instantiate()
	app.offline_mode = true
	viewport.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	app.accept_state(original)
	app.boss_mode_option.select(app.boss_mode_option.get_item_index(2))
	app._render()
	if not app.accept_evaluation(estimate):
		push_error("Evaluation identity does not match the supplied current state")
		quit(1)
		return
	var prefix: String = "synthetic-input" if synthetic_input else "actual"
	for size in [Vector2i(1240, 860), Vector2i(900, 680)]:
		if not await _capture(app, viewport, size.x, size.y,
				"godot-sparring-" + prefix + "-%d" % size.x, synthetic_input):
			quit(1)
			return
	if args.has("--synthetic-samples"):
		for sample in [["ramp", "volatile"], ["finish", "stable"]]:
			var changed: Dictionary = original.duplicate(true)
			changed.revision = int(app.state.revision) + 1
			changed.engine.boss_mode = false
			changed.engine.sparring_mode = true
			if not changed.engine.get("sparring") is Dictionary:
				changed.engine.sparring = {}
			changed.engine.sparring.phase = sample[0]
			var analysis: Dictionary = estimate.duplicate(true)
			analysis.revision = changed.revision
			analysis.reliability = sample[1]
			app.accept_state(changed)
			if not app.accept_evaluation(analysis):
				push_error("Synthetic sample identity mismatch")
				quit(1)
				return
			if not await _capture(app, viewport, 900, 680, "godot-sparring-synthetic-" + sample[0], true,
					["engine.boss_mode", "engine.sparring_mode", "engine.sparring.phase", "evaluation.reliability", "revision"]):
				quit(1)
				return
	var manifest := {"fixture": fixture_path, "fixture_synthetic": synthetic_input,
		"render_mode": "offline; no HTTP or engine launch", "manual_play": false,
		"source_state_modified": false, "captures": captures}
	var file := FileAccess.open(output.path_join("manifest.json"), FileAccess.WRITE)
	if file == null:
		push_error("Could not save capture manifest")
		quit(1)
		return
	file.store_string(JSON.stringify(manifest, "  "))
	file.close()
	app.queue_free()
	viewport.queue_free()
	await process_frame
	print("Sparring offline rendered capture: ", captures.size(), " images; ", output)
	quit(0)
