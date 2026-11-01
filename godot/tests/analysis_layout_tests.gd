extends SceneTree
## Container geometry checks; headless mode never claims pixel/rendered validation.

var app: Variant
var panel: Variant
var checks: int = 0
var failures: int = 0
var output_path: String = ""
var source_script: String = "res://scripts/analysis_mode.gd"
var expect_drift: bool = false
var backend_file: String = ""
var report: Dictionary = {"windows": [], "live": [], "mode": "headless geometry"}
var baseline_drift: bool = false
var revision: int = 0


func _initialize() -> void:
	_run.call_deferred()


func verify(value: bool, message: String) -> void:
	checks += 1
	if not value:
		failures += 1
		printerr("FAIL: ", message)


func vector_values(value: Vector2) -> Array:
	return [value.x, value.y]


func rect_values(value: Rect2) -> Array:
	return [value.position.x, value.position.y, value.size.x, value.size.y]


func world_rect(control: Control, local: Rect2) -> Rect2:
	var transform: Transform2D = control.get_global_transform()
	return Rect2(transform * local.position, local.size * transform.get_scale())


func geometry() -> Dictionary:
	var grid: Rect2 = world_rect(panel.board, panel.board.get_board_rect())
	var bar: Rect2 = world_rect(panel.evaluation_bar, panel.evaluation_bar.bar_rect())
	var sidebar: Control = panel.find_child("AnalysisSidebarPanel", true, false)
	return {"control": rect_values(panel.board.get_global_rect()), "grid": rect_values(grid),
		"center": vector_values(grid.get_center()), "bar": rect_values(bar),
		"viewport": vector_values(root.get_visible_rect().size), "window_pixels": [root.size.x, root.size.y],
		"content_scale_size": [root.content_scale_size.x, root.content_scale_size.y],
		"sidebar": rect_values(sidebar.get_global_rect()) if sidebar != null else [],
		"evaluation_summary": str(panel.evaluation_summary.text), "evaluation_detail": str(panel.evaluation_detail.text)}


func geometry_delta(first: Dictionary, second: Dictionary) -> float:
	var maximum: float = 0.0
	for field in ["control", "grid", "center", "bar", "sidebar"]:
		for index in range(first[field].size()):
			maximum = maxf(maximum, absf(float(first[field][index]) - float(second[field][index])))
	return maximum


func settle() -> void:
	# Text wrapping changes minimum sizes on deferred Container layout passes.
	for _frame in range(12):
		await process_frame


func base_state() -> Dictionary:
	revision += 1
	var fen: String = "r3k2r/4P3/8/8/8/8/4P3/R3K2R w KQkq - 0 1"
	return {"game_id": "analysis-layout", "revision": revision, "active": true, "fen": fen,
		"turn": "white", "status": "active", "message": "백 차례 · 양쪽 수를 직접 둘 수 있습니다.",
		"board": {"e1": "K", "a1": "R", "h1": "R", "e8": "k", "a8": "r", "h8": "r", "e2": "P", "e7": "P"},
		"legal_moves": ["e2e4", "e1g1", "e1c1", "e7e8q", "e7e8r", "e7e8b", "e7e8n"],
		"can_undo": true, "moves": [], "hint": {"status": "searching", "message": "추천 수 계산 중"},
		"evaluation": {"game_id": "analysis-layout", "revision": revision, "fen": fen,
			"turn": "white", "status": "waiting", "result": "*", "cp": null, "mate": null},
		"castling": {"king": {"move": "e1g1", "available": true, "reason": "가능합니다."},
			"queen": {"move": "e1c1", "available": true, "reason": "가능합니다."}}}


func apply_case(label: String) -> void:
	panel.busy = false
	panel._error = ""
	panel.editing = label.begins_with("editor")
	panel.model.pending_promotions.clear()
	panel.model.view_from_white = label != "flipped"
	var state: Dictionary = base_state()
	match label:
		"searching":
			state.evaluation.status = "searching"
		"positive", "flipped":
			state.evaluation.merge({"status": "ready", "cp": 172, "mate": null, "depth": 12, "bound": "exact", "white_share": 0.65}, true)
		"negative_bound":
			state.evaluation.merge({"status": "searching", "cp": -1472, "depth": 128, "bound": "upper", "white_share": 0.1}, true)
		"mate", "mate_zero":
			state.evaluation.merge({"status": "ready", "mate": 0 if label == "mate_zero" else -25, "winner": "black", "depth": 100}, true)
		"draw":
			state.merge({"status": "draw", "message": "반복 또는 50수 청구를 포함한 무승부 위치입니다.", "legal_moves": []}, true)
			state.evaluation.merge({"status": "terminal", "result": "1/2-1/2", "white_share": 0.5}, true)
		"long_castling":
			state.castling.king.reason = ("왕의 통과 칸 f1과 도착 칸 g1이 공격받아 지금은 캐슬링할 수 없습니다.\n").repeat(8)
			state.castling.queen.reason = ("현재 차례에서는 캐슬링 권리가 없고 b1·c1·d1의 기물이 길을 막습니다. ").repeat(8)
		"long_history":
			for index in range(250):
				state.moves.append({"san": "Qh8+", "ply": index + 1})
			state.hint = {"status": "unavailable", "message": "PV " + "Nf3Bc4O-OQh8+".repeat(150)}
		"promotion":
			panel.model.pending_promotions.assign(["e7e8q", "e7e8r", "e7e8b", "e7e8n"])
		"busy":
			panel.busy = true
		"error", "editor_error":
			panel._error = ("분석 관리기와 연결할 수 없습니다. 현재 기물 배치와 선택한 수를 확인해 주세요.\n").repeat(12)
		"long_status":
			state.message = "백차례현재위치의추천수와캐슬링가능여부를분석하고있습니다".repeat(30)
	panel.accept_state(state)
	panel._render()
	if label == "long_header":
		panel.turn_label.text = "현재 차례를 표시하는 매우 긴 안내 문장 ".repeat(20)
		panel.evaluation_summary.text = "백 우세 · +123456.78 · 자체 엔진 평가의 긴 제목 ".repeat(12)


func sample(label: String, reference: Dictionary) -> Dictionary:
	await settle()
	var maximum: float = 0.0
	for _frame in range(12):
		maximum = maxf(maximum, geometry_delta(reference, geometry()))
		await process_frame
	var measured: Dictionary = geometry()
	var grid: Array = measured.grid
	var bar: Array = measured.bar
	verify(absf(float(grid[1]) - float(bar[1])) < 0.1 and absf(float(grid[3]) - float(bar[3])) < 0.1,
		label + " evaluation bar aligns vertically with actual board squares")
	verify(float(bar[0]) + float(bar[2]) <= float(grid[0]) and absf(float(grid[2]) - float(grid[3])) < 0.1,
		label + " square board and separate evaluation gutter")
	if expect_drift:
		baseline_drift = baseline_drift or maximum > 0.5
	else:
		verify(maximum < 0.1, label + " fixed-window board/control/center/bar geometry unchanged (delta=" + str(maximum) + ")")
		verify(float(grid[0]) >= 0 and float(grid[1]) >= 0 and float(bar[0]) >= 0 and
			float(grid[0]) + float(grid[2]) <= float(measured.viewport[0]) and
			float(grid[1]) + float(grid[3]) <= float(measured.viewport[1]), label + " squares and evaluation gutter remain inside viewport")
		verify(measured.sidebar.size() == 4 and absf(float(measured.sidebar[2]) - 360.0) < 0.1,
			label + " dynamic sidebar keeps its fixed 360px width")
	measured["case"] = label
	measured["max_delta"] = maximum
	return measured


func _offline() -> void:
	var sizes: Array[Vector2i] = [Vector2i(1240, 860), Vector2i(900, 680), Vector2i(1500, 1000)]
	var cases: Array[String] = ["editor", "waiting", "searching", "positive", "negative_bound", "mate", "mate_zero", "draw", "long_castling", "long_history", "promotion", "busy", "error", "long_status", "long_header", "flipped", "editor_error", "waiting"]
	for dimensions in sizes:
		# canvas_items otherwise retains the project's 1240x860 logical canvas.
		# Exercise smaller/larger Container areas, not just physical canvas scaling.
		root.content_scale_size = dimensions
		root.size = dimensions
		apply_case("editor")
		await settle()
		var reference: Dictionary = geometry()
		var window_report: Dictionary = {"window": [dimensions.x, dimensions.y], "reference": reference, "cases": []}
		for label in cases:
			apply_case(label)
			window_report.cases.append(await sample(label, reference))
		report.windows.append(window_report)
	if expect_drift:
		verify(baseline_drift, "original source reproduces variable-text geometry drift")
	else:
		var first: Array = report.windows[0].reference.grid
		var small: Array = report.windows[1].reference.grid
		var large: Array = report.windows[2].reference.grid
		verify(float(small[2]) < float(first[2]) and float(first[2]) < float(large[2]), "board resizes only when the window dimensions change")


func _live() -> void:
	if backend_file.is_empty() or expect_drift:
		return
	var config: Variant = JSON.parse_string(FileAccess.get_file_as_string(backend_file))
	verify(config is Dictionary, "private isolated backend configuration readable")
	if not config is Dictionary:
		return
	root.size = Vector2i(1240, 860)
	root.content_scale_size = Vector2i(1240, 860)
	panel.offline_mode = false
	panel._error = ""
	panel.busy = false
	# Offline fixture revisions belong to no real session.
	panel.analysis_state.clear()
	await panel.open_position(panel.START_FEN, str(config.url), str(config.token))
	panel.time_option.select(0)
	await panel._start_analysis()
	await settle()
	var reference: Dictionary = geometry()
	report.live.append(await sample("live_start", reference))
	for move in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "e1g1"]:
		verify(panel.analysis_state.legal_moves.has(move), "live legal move " + move)
		await panel._mutate("move", {"move": move})
		report.live.append(await sample("live_" + move, reference))
	await panel._return_to_game()


func _run() -> void:
	var args: PackedStringArray = OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--report" and index + 1 < args.size(): output_path = args[index + 1]
		if args[index] == "--source-script" and index + 1 < args.size(): source_script = args[index + 1]
		if args[index] == "--backend-file" and index + 1 < args.size(): backend_file = args[index + 1]
		if args[index] == "--expect-drift": expect_drift = true
	root.size = Vector2i(1240, 860)
	app = load("res://main.tscn").instantiate()
	app.offline_mode = true
	root.add_child(app)
	await process_frame
	app.main_contents.hide()
	panel = load(source_script).new()
	panel.offline_mode = true
	app.add_child(panel)
	panel.open_position(panel.START_FEN, "http://127.0.0.1:1", "offline")
	panel.poll_timer.stop()
	report.mode = "headless geometry" if DisplayServer.get_name() == "headless" else "native rendered geometry"
	report["source_script"] = source_script
	report["expect_drift"] = expect_drift
	if not expect_drift:
		var left: Node = panel.find_child("AnalysisBoardColumn", true, false)
		verify(left != null and left.get_child_count() == 3 and left.get_child(0).name == "AnalysisBoardToolbar"
			and left.get_child(1).name == "AnalysisEvaluationSummary" and left.get_child(2).name == "AnalysisBoardRow",
			"left column contains only the fixed toolbar, fixed score and expanding board")
	await _offline()
	panel.poll_timer.start()
	await _live()
	report["checks"] = checks
	report["failures"] = failures
	report["baseline_drift"] = baseline_drift
	if not output_path.is_empty():
		var file := FileAccess.open(output_path, FileAccess.WRITE)
		if file: file.store_string(JSON.stringify(report, "  ") + "\n")
	app.queue_free()
	await process_frame
	print("Analysis layout checks: %d, failures: %d, mode: %s" % [checks, failures, report.mode])
	quit(0 if failures == 0 else 1)
