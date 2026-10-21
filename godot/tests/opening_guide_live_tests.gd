extends SceneTree
## Real authenticated host, legal prepared history, read-only learning controls.

var app: Variant
var checks: int = 0
var failures: int = 0
var evidence_dir: String = ""


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, message: String, seconds: float = 30.0) -> bool:
	var deadline: int = Time.get_ticks_msec() + int(seconds * 1000)
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + message + "; " + str(app.error_message))
			return false
		await create_timer(0.01).timeout
	verify(true, message)
	return true


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(evidence_dir.path_join(name + ".png")) == OK,
		"actual catalog rendered screenshot saved")


func finish() -> void:
	app.queue_free()
	await process_frame
	print("Godot opening-guide live checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--evidence-dir":
			evidence_dir = args[index + 1]
	root.size = Vector2i(1280, 900)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight,
		"authenticated current position and opening data"):
		await finish()
		return
	var original: String = JSON.stringify(app.state)
	var catalog: Dictionary = app.state.get("opening_guide", {})
	verify(catalog.get("status") == "in_catalog" and not catalog.get("current_matches", []).is_empty(),
		"actual prepared opening is recognized")
	if not catalog.get("current_matches", []).is_empty():
		verify(app.opening_summary.text.contains(str(catalog.current_matches[0].name)),
			"UI displays the actual catalog's current name")
	verify(not app._opening_rows.is_empty(), "actual catalog supplies reachable named lines")
	verify(app.opening_catalog_note.text.contains(str(catalog.get("ordering", "SAN"))),
		"actual catalog ordering is disclosed in the expanded learning section")
	var clipped: bool = bool(catalog.get("continuations_truncated", false))
	for branch in catalog.get("continuations", []):
		clipped = clipped or bool(branch.get("immediate_truncated", false)) or bool(branch.get("reachable_truncated", false))
	verify(not clipped or app.opening_catalog_note.text.contains("일부만 표시"),
		"real capped name lists are visibly labelled as partial")
	app._on_opening_toggle_pressed()
	var selected: int = 0
	for index in range(app._opening_rows.size()):
		if int(app._opening_rows[index].get("distance_plies", 1)) > 1:
			selected = index
			break
	app._on_opening_line_selected(selected)
	if not app._opening_rows.is_empty():
		verify(app.opening_preview.get_parsed_text() == str(app._opening_rows[selected].remaining_san),
			"complete actual remaining SAN sequence is displayed")
	verify(JSON.stringify(app.state) == original, "real-host learning preview changes no position or history")
	app._on_opening_source_pressed()
	verify(app.opening_source_dialog.visible and app.opening_source_text.get_parsed_text().contains(str(catalog.source.commit)),
		"actual data source and pinned commit are accessible")
	app.opening_source_dialog.hide()
	await capture("opening-actual-catalog-expanded")
	app._on_opening_toggle_pressed()
	await capture("opening-actual-catalog-compact")
	app._on_library_pressed()
	if not await wait_for(func(): return not app.request_in_flight and app.library_dialog.visible and not app._library_games.is_empty(),
		"temporary game library loads through authenticated HTTP"):
		await finish()
		return
	app.library_list.select(0)
	app._open_selected_record()
	if not await wait_for(func(): return not app.request_in_flight and app.review_mode,
		"saved game opens in read-only replay"):
		await finish()
		return
	verify(app.review_frames[0].has("opening_guide"), "archive provides opening information for the displayed frame")
	app._last_review_position()
	verify(app.opening_summary.text.contains(str(catalog.current_matches[0].name)),
		"archive's final frame restores its actual opening name")
	app._set_review_ply(0)
	verify(not app._opening_rows.is_empty(), "initial replay frame offers its own possible named lines")
	app._on_opening_line_selected(0)
	verify(JSON.stringify(app.state) == original, "archive opening exploration preserves live game")
	app._exit_review()
	verify(app.opening_summary.text.contains(str(catalog.current_matches[0].name)), "leaving replay restores live opening")
	verify(app.error_message.is_empty(), "learning controls and archive requests finish without client errors")
	await finish()
