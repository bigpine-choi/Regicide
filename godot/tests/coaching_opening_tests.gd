extends SceneTree
## Private native UI fixtures. Grades/text are synthetic; no backend or real voice.

const Prior = preload("res://tests/coaching_continuation_tests.gd")
const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
const F3_FEN = "rnbqkbnr/pppppppp/8/8/8/5P2/PPPPP1PP/RNBQKBNR b KQkq - 0 1"
const MATE_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"

class VoicePanel extends "res://scripts/coaching_mode.gd":
	var spoken: Array[String] = []
	var speaking: bool = false
	var stopped: int = 0

	func _setup_voice() -> void:
		_english_voice = "mock-English"
		_render_speech_controls()

	func _voice_speak(text: String, _voice: String) -> void:
		spoken.append(text)
		speaking = true

	func _voice_stop() -> void:
		stopped += 1
		speaking = false

	func _voice_is_speaking() -> bool:
		return speaking

	func flush_speech() -> void:
		if not _speech_pending_key.is_empty():
			speech_timer.stop()
			_speak_coach()

var panel: VoicePanel
var checks: int = 0
var failures: int = 0


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func settle() -> void:
	for _index in range(8):
		await process_frame


func frame(fen: String, ply: int, last_move: String = "") -> Dictionary:
	var parts: PackedStringArray = fen.split(" ")
	var pieces: Dictionary = {}
	var ranks: PackedStringArray = parts[0].split("/")
	for rank_index in range(8):
		var file_index: int = 0
		for symbol in ranks[rank_index]:
			if symbol.is_valid_int():
				file_index += int(symbol)
			else:
				pieces["abcdefgh"[file_index] + str(8 - rank_index)] = symbol
				file_index += 1
	return {"fen": fen, "board": pieces, "ply": ply, "turn": "white" if parts[1] == "w" else "black", "last_move": last_move,
		"in_check": fen == MATE_FEN, "result": "0-1" if fen == MATE_FEN else "*", "legal_moves": [], "human_to_move": false, "engine_to_move": false}


func fixture() -> Dictionary:
	var actual_fens := [START_FEN, F3_FEN, Prior.FENS[0], Prior.ACTUAL_FEN, MATE_FEN]
	var actual_moves := ["f2f3", "e7e5", "g2g4", "d8h4"]
	var actual_sans := ["f3", "e5", "g4", "Qh4#"]
	var frames: Array = []
	var rows: Array = []
	for index in range(actual_fens.size()):
		frames.append(frame(actual_fens[index], index, actual_moves[index - 1] if index > 0 else ""))
	for index in range(actual_moves.size()):
		rows.append({"ply": index + 1, "uci": actual_moves[index], "san": actual_sans[index], "color": frames[index].turn,
			"fen_before": actual_fens[index], "fen_after": actual_fens[index + 1], "classification": "blunder" if index == 2 else "good",
			"label": "큰 실수" if index == 2 else "좋은 수", "coach": {"dialogue": "Existing move feedback."}})
	var pv_frames: Array = []
	var steps: Array = []
	for index in range(Prior.FENS.size()):
		pv_frames.append(frame(Prior.FENS[index], index + 2, Prior.UCIS[index - 1] if index > 0 else "e7e5"))
	for index in range(Prior.UCIS.size()):
		steps.append({"ply_index": index + 1, "uci": Prior.UCIS[index], "san": Prior.SANS[index], "color": pv_frames[index].turn,
			"text": "Step %d: %s is played in this line." % [index + 1, Prior.SANS[index]], "squares": [str(Prior.UCIS[index]).substr(2, 2)]})
	rows[2].best = {"uci": "e2e4", "san": "e4", "pv_uci": Prior.UCIS.duplicate(), "pv_san": Prior.SANS.duplicate(), "pv_frames": pv_frames,
		"bound": "exact", "score_cp_white": 0, "mate_white": null, "depth": 7}
	rows[2].played = {"uci": "g2g4", "san": "g4", "pv_uci": ["g2g4", "d8h4"], "pv_san": ["g4", "Qh4#"],
		"pv_frames": [frames[2].duplicate(true), frames[3].duplicate(true), frames[4].duplicate(true)], "bound": "exact", "score_cp_white": null, "mate_white": -1, "winner": "black", "depth": 7}
	rows[2].retry_legal_moves = ["e2e4", "g2g4"]
	rows[2].coach.hint = "Keep the king's diagonal covered."
	rows[2].coach.continuation = {"line": "best", "target_plies": 6, "available_plies": 6, "complete": true, "terminal": false,
		"intro": "Follow e4 and the replies in this hypothetical line.", "summary": "Both sides developed pieces in this line.", "steps": steps}
	rows[2].coach.insight = {"theme": "development", "title": "Develop with the center covered", "summary": "Try e4 to open the bishop while keeping the g-pawn at home.",
		"sections": [
			{"id": "idea", "title": "The idea", "text": "e4 opens the bishop's diagonal.", "line": "best", "ply_index": 1, "squares": ["e4", "f1"]},
			{"id": "plan", "title": "The follow-up", "text": "The bishop develops to c4 in this line.", "line": "best", "ply_index": 5, "squares": ["c4"]},
			{"id": "counterplay", "title": "The reply", "text": "Black develops a knight to c6.", "line": "best", "ply_index": 2, "squares": ["c6"]}
		]}
	rows[2].coach.opening = {"title": "The center and king safety", "summary": "White has advanced the kingside pawns; Black has claimed space in the center. Compare their plans with the actual board.",
		"white": {"name": "Barnes Opening"}, "black": {"name": "Central ...e5 setup"}, "recognition": {},
		"ply_index": 3, "fen": Prior.ACTUAL_FEN, "focus_squares": ["e1", "f2", "g2", "e5"], "sources": [], "assessment": {"status": "unavailable"},
		"sections": [
			{"id": "white", "title": "White's plan", "text": "White needs to connect the opening moves to king safety. The f-pawn and g-pawn have both moved, leaving a diagonal toward the king. Before pushing another pawn, look for Black's forcing checks and decide how to keep the king covered. An opening name describes the move order; it does not justify every later move."},
			{"id": "black", "title": "Black's reply", "text": "Black has placed a pawn in the center with e5. That pawn also opens the queen's diagonal. Here the kingside pawn moves give Black a concrete checking idea, so the next decision should start with checks rather than another routine developing move. The actual position matters more than choosing a defense by its name."},
			{"id": "balance", "title": "Who got the better start?", "text": "The opening labels do not decide which side has the better position. On this board the immediate king threat matters more than general development advice. Compare the forcing reply with White's alternatives before drawing a conclusion. This explanation is tied to the displayed position, not a claim that one named opening always beats another."}
		]}
	return {"job_id": "opening-fixture", "revision": 2, "status": "completed", "moves": rows, "frames": frames,
		"game": {"initial_fen": START_FEN, "human_color": "white", "result": "0-1"},
		"critical_plies": [3], "coach": {"lesson_plies": [3], "summary": "The game turned on king safety."}}


func navigation_fixture() -> Dictionary:
	# Legal 30-ply Ruy Lopez history; grades and opening prose are UI-only fixtures.
	var ucis: Array = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6", "e1g1", "f8e7", "f1e1", "b7b5", "a4b3", "d7d6", "c2c3", "e8g8", "h2h3", "c6b8", "d2d4", "b8d7", "c3c4", "c7c6", "b1c3", "c8b7", "c1g5", "b5b4", "c3a4", "h7h6", "g5h4", "f8e8"]
	var sans: Array = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7", "Re1", "b5", "Bb3", "d6", "c3", "O-O", "h3", "Nb8", "d4", "Nbd7", "c4", "c6", "Nc3", "Bb7", "Bg5", "b4", "Na4", "h6", "Bh4", "Re8"]
	var fens: Array = [
		"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
		"rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
		"rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
		"rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
		"r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
		"r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
		"r1bqkbnr/1ppp1ppp/p1n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4",
		"r1bqkbnr/1ppp1ppp/p1n5/4p3/B3P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 4",
		"r1bqkb1r/1ppp1ppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 2 5",
		"r1bqkb1r/1ppp1ppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQ1RK1 b kq - 3 5",
		"r1bqk2r/1pppbppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 4 6",
		"r1bqk2r/1pppbppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQR1K1 b kq - 5 6",
		"r1bqk2r/2ppbppp/p1n2n2/1p2p3/B3P3/5N2/PPPP1PPP/RNBQR1K1 w kq - 0 7",
		"r1bqk2r/2ppbppp/p1n2n2/1p2p3/4P3/1B3N2/PPPP1PPP/RNBQR1K1 b kq - 1 7",
		"r1bqk2r/2p1bppp/p1np1n2/1p2p3/4P3/1B3N2/PPPP1PPP/RNBQR1K1 w kq - 0 8",
		"r1bqk2r/2p1bppp/p1np1n2/1p2p3/4P3/1BP2N2/PP1P1PPP/RNBQR1K1 b kq - 0 8",
		"r1bq1rk1/2p1bppp/p1np1n2/1p2p3/4P3/1BP2N2/PP1P1PPP/RNBQR1K1 w - - 1 9",
		"r1bq1rk1/2p1bppp/p1np1n2/1p2p3/4P3/1BP2N1P/PP1P1PP1/RNBQR1K1 b - - 0 9",
		"rnbq1rk1/2p1bppp/p2p1n2/1p2p3/4P3/1BP2N1P/PP1P1PP1/RNBQR1K1 w - - 1 10",
		"rnbq1rk1/2p1bppp/p2p1n2/1p2p3/3PP3/1BP2N1P/PP3PP1/RNBQR1K1 b - - 0 10",
		"r1bq1rk1/2pnbppp/p2p1n2/1p2p3/3PP3/1BP2N1P/PP3PP1/RNBQR1K1 w - - 1 11",
		"r1bq1rk1/2pnbppp/p2p1n2/1p2p3/2PPP3/1B3N1P/PP3PP1/RNBQR1K1 b - - 0 11",
		"r1bq1rk1/3nbppp/p1pp1n2/1p2p3/2PPP3/1B3N1P/PP3PP1/RNBQR1K1 w - - 0 12",
		"r1bq1rk1/3nbppp/p1pp1n2/1p2p3/2PPP3/1BN2N1P/PP3PP1/R1BQR1K1 b - - 1 12",
		"r2q1rk1/1b1nbppp/p1pp1n2/1p2p3/2PPP3/1BN2N1P/PP3PP1/R1BQR1K1 w - - 2 13",
		"r2q1rk1/1b1nbppp/p1pp1n2/1p2p1B1/2PPP3/1BN2N1P/PP3PP1/R2QR1K1 b - - 3 13",
		"r2q1rk1/1b1nbppp/p1pp1n2/4p1B1/1pPPP3/1BN2N1P/PP3PP1/R2QR1K1 w - - 0 14",
		"r2q1rk1/1b1nbppp/p1pp1n2/4p1B1/NpPPP3/1B3N1P/PP3PP1/R2QR1K1 b - - 1 14",
		"r2q1rk1/1b1nbpp1/p1pp1n1p/4p1B1/NpPPP3/1B3N1P/PP3PP1/R2QR1K1 w - - 0 15",
		"r2q1rk1/1b1nbpp1/p1pp1n1p/4p3/NpPPP2B/1B3N1P/PP3PP1/R2QR1K1 b - - 1 15",
		"r2qr1k1/1b1nbpp1/p1pp1n1p/4p3/NpPPP2B/1B3N1P/PP3PP1/R2QR1K1 w - - 2 16"
	]
	var result: Dictionary = fixture()
	var packet: Dictionary = result.moves[2].coach.opening.duplicate(true)
	packet.title = "Opening navigation fixture"
	packet.summary = "Compare both sides at the selected actual move."
	packet.white.name = "Ruy Lopez"
	packet.black.name = "Ruy Lopez defense"
	packet.focus_squares = []
	packet.sections[0].text = "White's plan at this actual position."
	packet.sections[1].text = "Black's reply at this actual position."
	packet.sections[2].text = "Compare the current position before judging the opening."
	result.job_id = "opening-navigation"
	result.game.result = "*"
	result.critical_plies = [30]
	result.coach.lesson_plies = [30]
	result.frames = []
	result.moves = []
	for index in range(fens.size()):
		result.frames.append(frame(fens[index], index, ucis[index - 1] if index > 0 else ""))
	for index in range(ucis.size()):
		var row: Dictionary = {"ply": index + 1, "uci": ucis[index], "san": sans[index], "color": result.frames[index].turn,
			"fen_before": fens[index], "fen_after": fens[index + 1], "classification": "blunder" if index == 29 else "good",
			"label": "UI fixture", "coach": {"dialogue": "Later actual move feedback."}}
		if index + 1 in [12, 24]:
			row.coach.opening = packet.duplicate(true)
			row.coach.opening.ply_index = index + 1
			row.coach.opening.fen = fens[index + 1]
		result.moves.append(row)
	return result


func load_report(value: Dictionary) -> void:
	panel._accept_snapshot(value, true, true)
	panel.speech_timer.stop()
	panel.continuation_timer.stop()


func choose(section: String) -> void:
	panel.opening_buttons[section].pressed.emit()
	panel.speech_timer.stop()


func _render_captures(args: PackedStringArray) -> void:
	if DisplayServer.get_name() == "headless":
		printerr("Opening screenshots need a rendered display with a private offline report.")
		quit(1)
		return
	var output: String = args[args.find("--screenshots") + 1]
	var source: Dictionary = fixture()
	if args.has("--report"):
		var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(args[args.find("--report") + 1]))
		if not parsed is Dictionary:
			quit(1)
			return
		source = parsed
	DirAccess.make_dir_recursive_absolute(output)
	root.title = "Regicide Opening · Private Offline Fixture"
	panel = VoicePanel.new()
	panel.offline_mode = true
	root.add_child(panel)
	panel._speech_enabled = false
	panel.poll_timer.stop()
	panel.human_color = str(source.get("game", {}).get("human_color", "white"))
	panel.side_option.select(1 if panel.human_color == "black" else 0)
	panel.show()
	var captures: Array = []
	for dimensions in [Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = dimensions
		root.content_scale_size = dimensions
		load_report(source)
		if args.has("--ply"):
			panel._select_ply(int(args[args.find("--ply") + 1]))
		await settle()
		var original: Rect2 = panel.board.get_global_rect()
		verify(panel.opening_card.visible, "report offers actual-position opening feedback")
		for scene in ["base", "white", "black", "balance"]:
			if scene == "base":
				panel._leave_opening()
			else:
				verify(not panel.opening_buttons[scene].disabled, scene + " opening topic is available")
				choose(scene)
				verify(panel._opening_section == scene, scene + " explanation is selected")
			panel.board.finish_animation()
			await settle()
			var rect: Rect2 = panel.board.get_global_rect()
			verify(rect.is_equal_approx(original), "%dpx board stays fixed for %s" % [dimensions.x, scene])
			verify(panel.report == source and not panel._speech_owned, "opening presentation preserves report and stays silent")
			await RenderingServer.frame_post_draw
			var name: String = "native-opening-%dx%d-%s.png" % [dimensions.x, dimensions.y, scene]
			verify(root.get_texture().get_image().save_png(output.path_join(name)) == OK, "saved native opening screenshot")
			captures.append({"image": name, "viewport": [dimensions.x, dimensions.y], "scene": scene, "ply": panel.selected_ply,
				"fen": panel.board.state.get("fen", ""), "board_rect": [rect.position.x, rect.position.y, rect.size.x, rect.size.y]})
	var record := FileAccess.open(output.path_join("native-opening-render.json"), FileAccess.WRITE)
	record.store_string(JSON.stringify({"mode": "isolated offline UI render of supplied analysis; no analysis or speech performed",
		"synthetic_grade_fixture": not args.has("--report"), "checks": checks, "failures": failures, "captures": captures}, "  "))
	record.close()
	panel.queue_free()
	await settle()
	print("Coaching opening renders: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)


func _run() -> void:
	var args: PackedStringArray = OS.get_cmdline_user_args()
	if args.has("--screenshots"):
		await _render_captures(args)
		return
	root.size = Vector2i(1240, 860)
	root.content_scale_size = root.size
	panel = VoicePanel.new()
	panel.offline_mode = true
	root.add_child(panel)
	panel.show()
	panel.poll_timer.stop()
	var source: Dictionary = fixture()
	var row: Dictionary = source.moves[2]
	var opening: Dictionary = row.coach.opening
	load_report(source)
	await settle()
	verify(panel.selected_ply == 3 and panel.opening_card.visible, "known early actual move exposes the opening card")
	verify(panel.coach_dialogue.text == row.coach.insight.summary, "default spoken feedback remains tactical or strategic")
	verify(panel.opening_names.text.contains("백 · " + opening.white.name) and panel.opening_names.text.contains("흑 · " + opening.black.name), "both sides' opening choices are visible")
	verify(panel.opening_summary.text == opening.summary, "opening overview appears separately without being spoken by default")
	panel.flush_speech()
	verify(panel.spoken[-1] == row.coach.insight.summary, "opening overview does not replace automatic move feedback")
	var rect: Rect2 = panel.board.get_global_rect()
	for index in range(3):
		var topic: String = str(opening.sections[index].id)
		choose(topic)
		panel.flush_speech()
		verify(panel._opening_section == topic and panel.coach_dialogue.text == opening.sections[index].text, topic + " selection reveals its own explanation")
		verify(panel.spoken[-1] == opening.sections[index].text, topic + " explanation is automatically spoken")
		verify(panel.line_kind.is_empty() and panel.show_after and panel.board.state.fen == row.fen_after, topic + " stays on the same actual after-move position")
		verify(panel.board.coach_squares == opening.focus_squares and panel.opening_scene.text.contains("실제 기보"), topic + " highlights the actual board without claiming a hypothetical line")
		verify(panel.opening_buttons[topic].button_pressed, topic + " is visibly selected")
		await settle()
		verify(panel.board.get_global_rect().is_equal_approx(rect), topic + " does not change board size")
	verify(panel.stopped >= 2 and panel.report == source, "switching topics cancels old speech without changing the report")
	panel._leave_opening()
	verify(panel._opening_section.is_empty() and panel.coach_dialogue.text == row.coach.insight.summary and panel.board.state.fen == row.fen_after, "basic feedback returns to the selected actual move")

	# Packet integrity is tied to a standard start and the actual after-position.
	for fault in ["missing", "custom_root", "custom_game_root", "wrong_before", "wrong_after", "wrong_packet_fen", "wrong_last_move", "wrong_ply", "fractional_ply", "string_ply", "missing_side", "empty_side", "empty_title", "empty_summary", "wrong_sections", "reordered_sections", "duplicate_section", "empty_text", "empty_section_title", "bad_focus"]:
		var invalid: Dictionary = source.duplicate(true)
		match fault:
			"missing": invalid.moves[2].coach.erase("opening")
			"custom_root": invalid.frames[0].fen = F3_FEN
			"custom_game_root": invalid.game.initial_fen = F3_FEN
			"wrong_before": invalid.moves[2].fen_before = START_FEN
			"wrong_after": invalid.moves[2].fen_after = START_FEN
			"wrong_packet_fen": invalid.moves[2].coach.opening.fen = START_FEN
			"wrong_last_move": invalid.frames[3].last_move = "e2e4"
			"wrong_ply": invalid.moves[2].coach.opening.ply_index = 2
			"fractional_ply": invalid.moves[2].coach.opening.ply_index = 3.5
			"string_ply": invalid.moves[2].coach.opening.ply_index = "3"
			"missing_side": invalid.moves[2].coach.opening.erase("black")
			"empty_side": invalid.moves[2].coach.opening.white.name = " "
			"empty_title": invalid.moves[2].coach.opening.title = ""
			"empty_summary": invalid.moves[2].coach.opening.summary = " "
			"wrong_sections": invalid.moves[2].coach.opening.sections.pop_back()
			"reordered_sections": invalid.moves[2].coach.opening.sections.reverse()
			"duplicate_section": invalid.moves[2].coach.opening.sections[1].id = "white"
			"empty_text": invalid.moves[2].coach.opening.sections[1].text = ""
			"empty_section_title": invalid.moves[2].coach.opening.sections[1].title = ""
			"bad_focus": invalid.moves[2].coach.opening.focus_squares = ["z9"]
		load_report(invalid)
		verify(not panel.opening_card.visible, fault + " opening packet is hidden")
		var feedback: String = panel.coach_dialogue.text
		panel._show_opening("white", panel._opening_key())
		verify(panel._opening_section.is_empty() and panel.coach_dialogue.text == feedback, fault + " cannot take over move feedback")
	var unknown: Dictionary = source.duplicate(true)
	unknown.game.erase("initial_fen")
	unknown.moves[2].coach.opening.recognition = null
	unknown.moves[2].coach.opening.focus_squares = []
	load_report(unknown)
	verify(panel.opening_card.visible, "known standard root allows missing optional recognition metadata")
	choose("white")
	verify(panel._opening_section == "white" and panel.board.coach_squares.is_empty(), "optional focus squares can be empty")
	# Even a spoofed structurally matching late-row packet must respect the horizon.
	var late: Dictionary = source.duplicate(true)
	while late.moves.size() < 25:
		var late_row: Dictionary = row.duplicate(true)
		late_row.ply = late.moves.size() + 1
		late_row.coach.opening.ply_index = late_row.ply
		late.moves.append(late_row)
	while late.frames.size() < 26:
		late.frames.append(source.frames[3].duplicate(true))
	late.frames[24] = source.frames[2].duplicate(true)
	load_report(late)
	panel._select_ply(25)
	verify(not panel.opening_card.visible and panel._opening_packet().is_empty(), "opening feedback is unavailable after the first 24 actual plies")

	# Higher-priority learning modes and stale callbacks cannot leak or race speech.
	load_report(source)
	choose("white")
	var old_ticket: String = panel._opening_key()
	panel._show_insight("plan", panel._insight_key())
	verify(panel._opening_section.is_empty() and panel._insight_section == "plan", "strategy selection replaces opening narration")
	choose("black")
	verify(panel._insight_section.is_empty() and panel._opening_section == "black" and panel.board.state.fen == row.fen_after, "opening selection returns from a strategic future to the actual move")
	panel._begin_continuation()
	panel.continuation_timer.stop()
	verify(panel._opening_section.is_empty() and panel._continuation_active, "six-ply replay cancels opening selection")
	choose("balance")
	verify(not panel._continuation_active and panel.continuation_timer.is_stopped() and panel._opening_section == "balance", "opening selection cancels six-ply playback")
	panel._advance_continuation()
	verify(panel.board.state.fen == row.fen_after, "old playback tick cannot move the actual opening position")
	panel._begin_retry()
	panel.flush_speech()
	verify(panel.retry_active and panel._opening_section.is_empty() and not panel.opening_card.visible, "retry hides the opening answer panel")
	verify(not panel.spoken[-1].contains("e5") and panel.spoken[-1] != opening.sections[2].text, "retry speaks only its own prompt")
	panel._show_opening("white", panel._opening_key())
	verify(panel._opening_section.is_empty(), "hidden retry rejects opening callbacks")
	panel._reveal_retry()
	verify(panel.opening_card.visible and panel.coach_dialogue.text == row.coach.insight.summary, "revealing review restores normal feedback, not an old opening selection")
	choose("white")
	panel._toggle_coach_overview()
	verify(panel._opening_section.is_empty() and not panel.opening_card.visible and panel.coach_dialogue.text == source.coach.summary, "whole-game overview clears opening feedback")
	panel._show_opening("black", panel._opening_key())
	verify(panel._opening_section.is_empty(), "overview rejects stale opening callbacks")
	panel._toggle_coach_overview()
	choose("white")
	panel._show_evidence({"line": "best", "ply_index": 2, "squares": ["c6"]}, 3, source.job_id, row.fen_before)
	verify(panel._opening_section.is_empty() and panel.line_index == 2, "evidence navigation cancels opening narration")
	choose("white")
	panel._change_side(1)
	verify(panel._opening_section.is_empty() and panel.opening_names.text.contains("백 · " + opening.white.name), "human-side change clears speech while keeping White and Black identities")
	panel._show_opening("black", old_ticket)
	verify(panel._opening_section.is_empty(), "old side callback is rejected")
	panel._change_side(0)
	choose("white")
	var spoken_count: int = panel.spoken.size()
	panel.flush_speech()
	spoken_count = panel.spoken.size()
	panel._accept_snapshot(source)
	verify(panel._opening_section == "white" and panel.spoken.size() == spoken_count and panel._speech_pending_key.is_empty(), "identical snapshot preserves current explanation without repeating it")
	var updated: Dictionary = source.duplicate(true)
	updated.moves[2].coach.opening.sections[0].text = "Updated explanation from a changed packet."
	panel._accept_snapshot(updated)
	verify(panel._opening_section.is_empty(), "changed source opening packet invalidates the selection")
	panel._show_opening("white", old_ticket)
	verify(panel._opening_section.is_empty(), "old packet callback cannot speak changed data")
	load_report(source)
	choose("white")
	panel._navigate_critical(4)
	verify(panel._opening_section.is_empty() and panel._walkthrough_active and not panel.opening_card.visible, "actual-game context replay cancels opening speech")
	panel._cancel_walkthrough()
	load_report(source)
	choose("white")
	panel._select_ply(1)
	verify(panel._opening_section.is_empty() and not panel.opening_card.visible, "selecting another actual move clears its old opening card")
	load_report(source)
	choose("white")
	var replaced: Dictionary = source.duplicate(true)
	replaced.job_id = "replacement-opening-game"
	panel._accept_snapshot(replaced, true)
	verify(panel._opening_section.is_empty(), "job replacement clears opening selection")
	load_report(source)
	choose("white")
	panel.hide()
	verify(panel._opening_section.is_empty() and not panel._speech_owned and panel._speech_pending_key.is_empty(), "hiding stops opening narration")
	panel.show()
	load_report(source)
	var long_game: Dictionary = navigation_fixture()
	load_report(long_game)
	verify(panel.selected_ply == 30 and not panel.opening_card.visible and panel.opening_review_button.visible, "opening review is discoverable from a later selected key lesson")
	verify(panel._opening_review_ply() == 24, "entry chooses the latest valid opening within 24 plies")
	var navigation_key: String = panel._opening_review_key()
	panel.opening_review_button.pressed.emit()
	panel.speech_timer.stop()
	panel.flush_speech()
	verify(panel.selected_ply == 24 and panel._opening_section == "balance", "entry navigates to the opening balance explanation")
	verify(panel.spoken[-1] == long_game.moves[23].coach.opening.sections[2].text, "entry automatically speaks the selected opening topic")
	verify(panel.board.state.fen == long_game.frames[24].fen and panel.line_kind.is_empty(), "entry displays the actual after-move opening board")
	verify(panel.report == long_game, "opening jump does not mutate the recorded game")
	panel._leave_opening()
	verify(panel.selected_ply == 24 and panel.coach_dialogue.text == long_game.moves[23].coach.dialogue, "basic feedback stays on the selected actual opening move")
	panel._select_ply(30)
	verify(panel._opening_section.is_empty() and panel.board.state.fen == long_game.frames[30].fen, "ordinary move navigation returns to the later position")
	panel._toggle_coach_overview()
	panel.opening_review_button.pressed.emit()
	verify(not panel._coach_overview and panel.selected_ply == 24 and panel._opening_section == "balance", "entry can leave whole-game overview for an actual opening scene")
	var early_game: Dictionary = long_game.duplicate(true)
	early_game.moves[23].coach.opening.assessment.status = "too_early"
	load_report(early_game)
	panel.opening_review_button.pressed.emit()
	verify(panel.selected_ply == 24 and panel._opening_section == "white", "too-early assessment opens White's plan instead of a premature verdict")
	var pending_game: Dictionary = long_game.duplicate(true)
	pending_game.moves[23].classification = "pending"
	load_report(pending_game)
	verify(panel._opening_review_ply() == 12, "pending later opening is skipped for the latest completed one")
	panel.opening_review_button.pressed.emit()
	verify(panel.selected_ply == 12 and panel._opening_section == "balance", "fallback opening still navigates and explains the actual position")
	var malformed_game: Dictionary = long_game.duplicate(true)
	malformed_game.moves[23].coach.opening.fen = "invalid"
	load_report(malformed_game)
	verify(panel._opening_review_ply() == 12, "malformed latest opening cannot displace a valid earlier one")
	malformed_game.moves[11].coach.opening = null
	load_report(malformed_game)
	verify(not panel.opening_review_button.visible and panel._opening_review_key().is_empty(), "no valid early packet hides the entry")
	panel._open_opening_review(navigation_key)
	verify(panel.selected_ply == 30 and panel._opening_section.is_empty(), "old entry callback cannot navigate a report with no valid opening")
	load_report(long_game)
	panel.busy = true
	panel._render()
	panel._open_opening_review(panel._opening_review_key())
	verify(panel.opening_review_button.disabled and panel.selected_ply == 30, "busy report mutation blocks the opening entry")
	panel.busy = false
	panel._walkthrough_active = true
	panel._render()
	panel._open_opening_review(panel._opening_review_key())
	verify(panel.opening_review_button.disabled and panel.selected_ply == 30, "actual-game animation blocks stale opening entry callbacks")
	panel._walkthrough_active = false
	panel._change_side(1)
	panel._open_opening_review(navigation_key)
	verify(panel.selected_ply == 30 and panel._opening_section.is_empty(), "old-side entry callback is rejected")
	panel._change_side(0)
	var next_game: Dictionary = long_game.duplicate(true)
	next_game.job_id = "opening-navigation-replacement"
	load_report(next_game)
	panel._open_opening_review(navigation_key)
	verify(panel.selected_ply == 30 and panel._opening_section.is_empty(), "old-job entry callback is rejected")
	load_report(source)
	panel._begin_retry()
	verify(not panel.opening_review_button.visible, "retry hides the opening shortcut to preserve the question")
	panel._open_opening_review(panel._opening_review_key())
	verify(panel.retry_active and panel._opening_section.is_empty(), "direct opening-entry callback cannot reveal a retry answer")
	panel._reveal_retry()
	for dimensions in [Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = dimensions
		root.content_scale_size = dimensions
		panel._leave_opening()
		await settle()
		var fixed: Rect2 = panel.board.get_global_rect()
		for topic in ["white", "black", "balance"]:
			choose(topic)
			await settle()
			verify(panel.board.get_global_rect().is_equal_approx(fixed), "%dpx board stays fixed for %s opening paragraph" % [dimensions.x, topic])
	var long_names: Dictionary = source.duplicate(true)
	long_names.moves[2].coach.opening.white.name = "English Opening: Anglo-Indian Defense, King's Indian Formation"
	long_names.moves[2].coach.opening.black.name = "Sicilian Defense: Najdorf Variation, English Attack"
	load_report(long_names)
	await settle()
	var narrow_board: Rect2 = panel.board.get_global_rect()
	choose("white")
	await settle()
	verify(panel.board.get_global_rect().is_equal_approx(narrow_board), "long opening names and full paragraphs preserve the narrow board")
	verify(panel.opening_names.get_global_rect().end.x <= panel.opening_card.get_global_rect().end.x and panel.opening_names.size.y > 40, "long names wrap inside the 900px card")
	load_report(source)
	choose("white")
	panel._return()
	verify(not panel.visible and panel._opening_section.is_empty() and panel._speech_pending_key.is_empty(), "leaving coaching stops opening feedback")
	panel.queue_free()
	await settle()
	print("Coaching opening: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)
