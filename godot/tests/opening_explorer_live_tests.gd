extends SceneTree
## Real local HTTP, shipped native scenes and Regicide analysis. The harness
## replaces only the remote statistics transport; these are fixture statistics.

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


func wait_for(predicate: Callable, message: String, seconds: float = 15.0) -> bool:
	var deadline: int = Time.get_ticks_msec() + int(seconds * 1000)
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + message)
			return false
		await create_timer(0.02).timeout
	verify(true, message)
	return true


func identity(state: Dictionary) -> Dictionary:
	return {"game_id": state.get("game_id"), "revision": state.get("revision"),
		"fen": state.get("fen"), "moves": state.get("moves", []).duplicate(true)}


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	DirAccess.make_dir_recursive_absolute(evidence_dir)
	await process_frame
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(evidence_dir.path_join(name + ".png")) == OK,
		"native HTTP screenshot saved (fixture statistics)")


func finish() -> void:
	if is_instance_valid(app):
		app.opening_explorer.suspend_queries()
		app.analysis_overlay.opening_explorer.suspend_queries()
		app.queue_free()
	await process_frame
	print("Godot opening-explorer HTTP checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--evidence-dir":
			evidence_dir = args[index + 1]
	root.size = Vector2i(1760, 960)
	root.content_scale_size = Vector2i(1760, 960)
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight,
		"native scene connected to authenticated local host"):
		await finish()
		return
	var original := identity(app.state)
	verify(not app.own_backend, "validation attaches without owning or stopping host")
	if not await wait_for(func(): return app.opening_explorer._connection_configured,
		"configured local connection is restored after launch"):
		await finish()
		return
	verify(app.opening_explorer.contents.visible, "opening statistics are visible without an enable action")
	if not await wait_for(func(): return app.opening_explorer.snapshot.get("status", "") == "ready",
		"real host worker returns validated fixture statistics"):
		await finish()
		return
	var explorer: Variant = app.opening_explorer
	verify(explorer.snapshot.fen == app.state.fen and explorer.rows.get_child_count() > 0,
		"HTTP statistics render for displayed game position")
	verify(explorer.games.get_child_count() > 1, "masters reference links survive native URL validation")
	explorer.source_option.select(1)
	explorer._filter_changed(1)
	if not await wait_for(func(): return explorer.snapshot.get("status", "") == "ready" and explorer.snapshot.get("source", "") == "lichess",
		"real JSON general-game rating and speed filters match native controls"):
		await finish()
		return
	verify(explorer.rows.get_child_count() > 0 and explorer._matches(explorer.snapshot),
		"general-game statistics render after HTTP JSON number decoding")
	var target: String = str(explorer.snapshot.moves[0].fen_after)
	var fixture_note := Label.new()
	fixture_note.text = "검증 화면 · 통계 수치는 테스트 응답이며 실제 리체스 통계가 아닙니다."
	fixture_note.position = Vector2(24, 82)
	fixture_note.add_theme_font_size_override("font_size", 12)
	fixture_note.add_theme_color_override("font_color", Color("ffd578"))
	root.add_child(fixture_note)
	await capture("native-http-current-position-fixture-statistics")
	explorer.rows.get_child(0).pressed.emit()
	if not await wait_for(func(): return app.analysis_overlay.visible and not app.analysis_overlay.editing and not app.analysis_overlay.busy and app.analysis_overlay.analysis_state.get("fen", "") == target,
		"candidate button starts separate analysis through actual HTTP"):
		await finish()
		return
	var analysis: Variant = app.analysis_overlay
	verify(analysis.opening_explorer.preferences().enabled and analysis.opening_explorer.source_option.selected == explorer.source_option.selected,
		"analysis inherits enabled source and filters")
	if not await wait_for(func(): return analysis._hint_ready(analysis.analysis_state.get("hint", {})),
		"actual Regicide engine supplies a legal analysis recommendation"):
		await finish()
		return
	verify(analysis.analysis_state.legal_moves.has(analysis.analysis_state.hint.move),
		"Regicide recommendation belongs to selected position's legal moves")
	verify(identity(app.state) == original, "analysis transition preserves native live-game state")
	var server_state: Dictionary = await app._send_request("/api/state")
	verify(server_state.get("ok", false) and identity(server_state.get("data", {})) == original,
		"root HTTP state confirms original game position and history unchanged")
	if not await wait_for(func(): return analysis.opening_explorer.snapshot.get("status", "") == "ready" and analysis.opening_explorer.snapshot.get("fen", "") == target,
		"analysis statistics follow the selected position through HTTP"):
		await finish()
		return
	var analysis_scroll: ScrollContainer = analysis.find_child("AnalysisSidebarScroll", true, false)
	analysis_scroll.scroll_vertical = int(analysis.opening_explorer.position.y)
	await capture("native-http-analysis-fixture-statistics")
	await analysis._return_to_game()
	verify(not analysis.visible and identity(app.state) == original,
		"returning from analysis restores original game")
	await finish()
