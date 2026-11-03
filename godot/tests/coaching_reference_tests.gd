extends SceneTree

class QuietPanel extends "res://scripts/coaching_mode.gd":
	func _setup_voice() -> void:
		_speech_enabled = false

var checks: int = 0
var failures: Array = []

func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures.append(label)
		push_error(label)

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var args: PackedStringArray = OS.get_cmdline_user_args()
	var fixture_path: String = args[args.find("--fixture") + 1]
	var fixture: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(fixture_path))
	var panel := QuietPanel.new()
	panel.offline_mode = true
	root.add_child(panel)
	panel.show()
	panel.report = fixture.report.duplicate(true)
	panel.selected_ply = 2
	panel.show_after = false
	panel._render()
	var native_report: String = JSON.stringify(panel.report)
	var context: Dictionary = panel._reference_position(panel._shown_frame())
	check(context.history_uci == ["f2f3"], "black position includes its full history")
	check(context.fen == panel.report.frames[1].fen, "black frame FEN matches")
	panel._reference = {"source": "stockfish_coaching_reference", "request_id": panel._reference_request_id,
		"status": "ready", "fen": context.fen, "cp": -321, "mate": null, "depth": 18, "bound": "exact"}
	panel._render()
	check(panel.evaluation_bar.snapshot.cp == -321, "Stockfish replaces only gauge score")
	check(panel.evaluation_label.text.begins_with("Stockfish"), "gauge names Stockfish")
	check(panel.evaluation_bar.tooltip_text.contains("Stockfish 참고"), "tooltip does not claim native score")
	check(panel.evaluation_bar.white_share < 0.5, "black advantage has correct white share")
	panel.model.view_from_white = false
	panel._render()
	check(panel.evaluation_bar.white_share < 0.5, "flipping preserves white score")
	check(JSON.stringify(panel.report) == native_report, "native report and classification untouched")
	panel._reference.request_id = "old-ticket"
	check(panel._evaluation_for_frame(panel._shown_frame()).get("cp") != -321, "stale request rejected")
	panel._reference.request_id = panel._reference_request_id
	panel._reference.fen = panel.report.frames[0].fen
	check(panel._evaluation_for_frame(panel._shown_frame()).get("cp") != -321, "stale FEN rejected")
	panel.selected_ply = 1
	panel._render()
	check(panel._reference.is_empty(), "position switch clears previous score")
	check(panel._reference_context.history_uci.is_empty(), "white initial context has empty history")
	panel.selected_ply = 3
	panel.line_kind = "played"
	panel.line_index = 1
	panel._render()
	check(panel._reference_context.history_uci == ["f2f3", "e7e5", "g2g4"], "PV later frame includes actual path plus PV prefix")
	check(not panel.evaluation_bar.has_score, "later PV has no invented native root score")
	panel._reference = {"source": "stockfish_coaching_reference", "request_id": panel._reference_request_id,
		"status": "ready", "fen": panel._shown_frame().fen, "cp": null, "mate": -1, "winner": "black", "depth": 20, "bound": "exact"}
	panel._render()
	check(panel.evaluation_bar.white_share == 0.0, "black mate reference fills black")
	panel._reference.mate = 2
	panel._reference.winner = "white"
	panel._render()
	check(panel.evaluation_bar.white_share == 1.0, "white mate reference fills white")
	panel.retry_active = true
	panel._retry_frame = panel.report.frames[2]
	panel._render()
	check(panel._reference.is_empty() and panel._reference_request_id.is_empty(), "hidden question invalidates reference")
	check(not panel.evaluation_bar.has_score and panel.evaluation_label.text.contains("가렸"), "retry never reveals score")
	check(panel._reference_position(panel._shown_frame()).is_empty(), "hidden question sends no position")
	panel.retry_active = false
	panel.line_kind = ""
	panel.show_after = false
	panel._render()
	check(not panel._reference_request_id.is_empty(), "revealed position gets fresh request id")
	panel.hide()
	panel._sync_reference_context(panel._shown_frame())
	check(panel._reference.is_empty() and panel._reference_key.is_empty(), "leaving panel invalidates pending result")
	if args.has("--backend-file"):
		var config: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(args[args.find("--backend-file") + 1]))
		panel.backend_url = config.url
		panel.backend_token = config.token
		panel.offline_mode = false
		panel.selected_ply = 3
		panel.line_kind = "played"
		panel.line_index = 1
		panel.show()
		panel._render()
		var deadline: int = Time.get_ticks_msec() + 7000
		while panel._reference.get("status") != "ready" and Time.get_ticks_msec() < deadline:
			await create_timer(0.1).timeout
		check(panel._reference.get("status") == "ready", "real HTTP reference reaches ready")
		check(panel._reference.get("mate") == -1, "actual Stockfish finds black mate in one at PV child")
		check(panel.evaluation_bar.snapshot.get("source") == "stockfish_coaching_reference", "actual response displayed as Stockfish")
		check(panel.evaluation_label.text.contains("Stockfish"), "actual source visible")
		panel.retry_active = true
		panel._retry_frame = panel.report.frames[2]
		panel._render()
		await create_timer(0.8).timeout
		check(not panel.evaluation_bar.has_score, "real reference remains hidden during retry")
		panel.hide()
		await create_timer(0.8).timeout
	print("COACHING_REFERENCE_RESULT " + JSON.stringify({"checks": checks, "failures": failures}))
	panel.queue_free()
	await process_frame
	quit(0 if failures.is_empty() else 1)
