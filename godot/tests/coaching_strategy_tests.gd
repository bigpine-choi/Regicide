extends SceneTree
## Native strategy/plan/defense UI checks with legal fixture frames and mock voice.

const ContinuationFixture = preload("res://tests/coaching_continuation_tests.gd")
const FENS = ContinuationFixture.FENS
const UCIS = ContinuationFixture.UCIS
const SANS = ContinuationFixture.SANS
const ACTUAL_FEN = ContinuationFixture.ACTUAL_FEN
const MATE_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"

class VoicePanel extends "res://scripts/coaching_mode.gd":
	var speaking: bool = false
	var spoken: Array[String] = []
	var stopped: int = 0

	func _setup_voice() -> void:
		_english_voice = "mock-English"
		_render_speech_controls()

	func _voice_speak(text: String, _voice: String) -> void:
		spoken.append(text)
		speaking = true

	func _voice_stop() -> void:
		speaking = false
		stopped += 1

	func _voice_is_speaking() -> bool:
		return speaking

	func flush_speech() -> void:
		if not _speech_pending_key.is_empty():
			speech_timer.stop()
			_speak_coach()

var checks: int = 0
var failures: int = 0
var panel: VoicePanel


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


func frame(fen: String, ply: int, last_move: String) -> Dictionary:
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
	var frames: Array = []
	var steps: Array = []
	for index in range(FENS.size()):
		frames.append(frame(FENS[index], index, UCIS[index - 1] if index > 0 else "e7e5"))
	for index in range(UCIS.size()):
		steps.append({"ply_index": index + 1, "uci": UCIS[index], "san": SANS[index], "color": frames[index].turn,
			"text": "Step %d: %s is the move in this line." % [index + 1, SANS[index]],
			"squares": [str(UCIS[index]).substr(0, 2), str(UCIS[index]).substr(2, 2)]})
	var actual: Dictionary = frame(ACTUAL_FEN, 1, "g2g4")
	var best := {"uci": UCIS[0], "san": SANS[0], "pv_uci": UCIS.duplicate(), "pv_san": SANS.duplicate(), "pv_frames": frames,
		"score_cp_white": 0, "mate_white": null, "bound": "exact", "depth": 7}
	var played := {"uci": "g2g4", "san": "g4", "pv_uci": ["g2g4", "d8h4"], "pv_san": ["g4", "Qh4#"],
		"pv_frames": [frames[0].duplicate(true), actual, frame(MATE_FEN, 2, "d8h4")], "score_cp_white": null, "mate_white": -1, "winner": "black", "bound": "exact", "depth": 7}
	var insight := {"theme": "center_and_king_safety", "title": "Keep the king covered; develop through the center",
		"summary": "Play e4 to open the bishop while keeping the king's diagonal covered.",
		"sections": [
			{"id": "idea", "title": "The idea", "text": "e4 opens the bishop's diagonal without moving the g-pawn away from the king.", "line": "best", "ply_index": 1, "squares": ["e4", "f1"]},
			{"id": "plan", "title": "The plan", "text": "Develop the knight, then bring the bishop to c4. In this line both pieces join the center.", "line": "best", "ply_index": 5, "squares": ["c3", "c4"]},
			{"id": "counterplay", "title": "The opponent's reply", "text": "Black develops the knight to c6, adding pressure to d4. Other replies can change the plan.", "line": "best", "ply_index": 2, "squares": ["c6", "d4"]}
		], "step_notes": []}
	var continuation := {"line": "best", "target_plies": 6, "available_plies": 6, "complete": true, "terminal": false,
		"intro": "Try e4 and follow one possible line.", "summary": "Both sides have developed pieces in this line.", "steps": steps}
	var row := {"ply": 1, "color": "white", "uci": "g2g4", "san": "g4", "fen_before": FENS[0], "fen_after": ACTUAL_FEN,
		"classification": "blunder", "label": "큰 실수", "loss_cp": 300, "best": best, "played": played,
		"retry_legal_moves": ["e2e4", "g2g4"], "coach": {"dialogue": "Legacy feedback stays available.", "hint": "Find a move that keeps the king covered.", "insight": insight, "continuation": continuation}}
	return {"job_id": "strategy-fixture", "revision": 2, "status": "completed", "moves": [row],
		"frames": [frames[0].duplicate(true), actual], "critical_plies": [1], "coach": {"lesson_plies": [1], "summary": "The game was decided by king safety."}}


func load_report(value: Dictionary) -> void:
	panel._accept_snapshot(value, true, true)
	panel.speech_timer.stop()
	panel.continuation_timer.stop()


func choose(section: String) -> void:
	panel.insight_buttons[section].pressed.emit()
	panel.speech_timer.stop()


func _render_captures(args: PackedStringArray) -> void:
	if DisplayServer.get_name() == "headless":
		printerr("Strategy screenshots need a rendered display, using a private offline report.")
		quit(1)
		return
	var output: String = args[args.find("--screenshots") + 1]
	var source: Dictionary = fixture()
	if args.has("--report"):
		var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(args[args.find("--report") + 1]))
		if not parsed is Dictionary:
			printerr("The strategy render report is not a JSON object.")
			quit(1)
			return
		source = parsed
	DirAccess.make_dir_recursive_absolute(output)
	root.title = "Regicide Strategy · Private Offline Fixture"
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
		await settle()
		var original: Rect2 = panel.board.get_global_rect()
		verify(panel.insight_controls.visible, "report exposes strategic summary and topics")
		for scene in ["base", "idea", "plan", "counterplay"]:
			if scene == "base":
				panel._leave_insight()
			else:
				verify(not panel.insight_buttons[scene].disabled, scene + " report evidence has a valid frame")
				choose(scene)
				verify(panel._insight_section == scene, scene + " packet enters its selected explanation")
			panel.board.finish_animation()
			await settle()
			var rect: Rect2 = panel.board.get_global_rect()
			verify(rect.is_equal_approx(original), "%dpx board stays fixed for %s" % [dimensions.x, scene])
			verify(panel.report == source and not panel._speech_owned, "offline presentation preserves the supplied report and stays silent")
			await RenderingServer.frame_post_draw
			var name: String = "native-strategy-%dx%d-%s.png" % [dimensions.x, dimensions.y, scene]
			verify(root.get_texture().get_image().save_png(output.path_join(name)) == OK, "saved native strategy screenshot")
			captures.append({"image": name, "viewport": [dimensions.x, dimensions.y], "scene": scene,
				"selected_ply": panel.selected_ply, "line": panel.line_kind, "line_index": panel.line_index,
				"fen": panel.board.state.get("fen", ""), "board_rect": [rect.position.x, rect.position.y, rect.size.x, rect.size.y]})
	var record := FileAccess.open(output.path_join("native-strategy-render.json"), FileAccess.WRITE)
	record.store_string(JSON.stringify({"mode": "isolated offline UI render of supplied analysis; no analysis or speech performed",
		"synthetic_grade_fixture": not args.has("--report"),
		"checks": checks, "failures": failures, "captures": captures}, "  "))
	record.close()
	panel.queue_free()
	await settle()
	print("Coaching strategy renders: %d checks, %d failures" % [checks, failures])
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
	var insight: Dictionary = source.moves[0].coach.insight
	load_report(source)
	await settle()
	verify(panel.coach_dialogue.text == insight.summary, "normal Rook guidance uses the strategic summary")
	verify(panel.insight_controls.visible and panel.insight_title.text == insight.title, "strategy topic is visible in the coach card")
	verify(panel.insight_buttons.values().all(func(button: Button): return not button.disabled), "all three verified topics are enabled")
	var before: Rect2 = panel.board.get_global_rect()
	choose("idea")
	verify(panel._insight_section == "idea" and panel.line_kind == "best" and panel.line_index == 1, "idea selection enters its verified scene")
	verify(panel.board.state.fen == FENS[1] and panel.board.coach_squares == ["e4", "f1"], "idea shows its exact frame and relevant squares")
	verify(panel.coach_dialogue.text == insight.sections[0].text and panel.insight_scene.text.contains("예상 전개"), "explanation and hypothetical scene label agree")
	verify(panel.board.move_annotation.is_empty() and not panel.evaluation_bar.has_score, "hypothetical strategy scene hides actual badge and root evaluation")
	panel.flush_speech()
	verify(panel.spoken[-1] == insight.sections[0].text, "idea is automatically narrated")
	var spoken_count: int = panel.spoken.size()
	panel._accept_snapshot(source)
	panel._render()
	verify(panel._insight_section == "idea" and panel._speech_pending_key.is_empty() and panel.spoken.size() == spoken_count, "identical snapshots preserve the selection without repeating voice")
	choose("plan")
	verify(panel.stopped > 0 and not panel.speaking, "switching topics cancels previous narration")
	panel.flush_speech()
	verify(panel.coach_dialogue.text == insight.sections[1].text and panel.spoken[-1] == insight.sections[1].text and panel.board.state.fen == FENS[5], "plan text, speech, and future position are synchronized")
	choose("counterplay")
	panel.flush_speech()
	verify(panel.line_kind == "best" and panel.line_index == 2 and panel.board.state.fen == FENS[2], "opponent response follows the same recommended branch")
	verify(panel.coach_dialogue.text == insight.sections[2].text and panel.insight_scene.text.contains("추천 수 이후 예상"), "response text distinguishes predicted replies from actual game moves")
	verify(panel.insight_buttons.counterplay.button_pressed and not panel.insight_buttons.idea.button_pressed, "selected strategy topic is highlighted")
	await settle()
	verify(panel.board.get_global_rect().is_equal_approx(before), "switching strategy chunks does not resize the board")
	panel._leave_insight()
	verify(panel._insight_section.is_empty() and panel.line_kind.is_empty() and panel.board.state.fen == ACTUAL_FEN, "basic explanation returns to the actual played position")
	verify(panel.coach_dialogue.text == insight.summary and not panel.insight_return.visible, "basic explanation restores the summary")
	verify(panel.report == source, "strategy exploration does not mutate the report or game")

	# Old and malformed packets fail locally without breaking ordinary coaching.
	var legacy: Dictionary = source.duplicate(true)
	legacy.moves[0].coach.erase("insight")
	load_report(legacy)
	verify(not panel.insight_controls.visible and panel.coach_dialogue.text == legacy.moves[0].coach.dialogue, "reports without insight preserve previous coaching")
	for malformed in ["missing_frames", "wrong_root", "wrong_current_root", "bad_index", "zero_index", "fractional_index", "string_index", "wrong_move", "duplicate", "wrong_order", "missing_section", "unknown_line", "mixed_branch", "wrong_text", "missing_section_title", "wrong_turn", "upper_bound", "lower_bound", "played_bound", "same_actual", "empty_theme", "empty_title", "empty_summary", "bad_focus", "empty_focus", "string_focus", "extra_frames", "wrong_san"]:
		var invalid: Dictionary = source.duplicate(true)
		match malformed:
			"missing_frames": invalid.moves[0].best.erase("pv_frames")
			"wrong_root": invalid.moves[0].best.pv_frames[0].fen = ACTUAL_FEN
			"wrong_current_root": invalid.frames[0].fen = ACTUAL_FEN
			"bad_index": invalid.moves[0].coach.insight.sections[0].ply_index = 99
			"zero_index": invalid.moves[0].coach.insight.sections[0].ply_index = 0
			"fractional_index": invalid.moves[0].coach.insight.sections[0].ply_index = 1.5
			"string_index": invalid.moves[0].coach.insight.sections[0].ply_index = "1"
			"wrong_move": invalid.moves[0].best.pv_frames[1].last_move = "d2d4"
			"duplicate": invalid.moves[0].coach.insight.sections.append(invalid.moves[0].coach.insight.sections[0].duplicate(true))
			"wrong_order": invalid.moves[0].coach.insight.sections.reverse()
			"missing_section": invalid.moves[0].coach.insight.sections.pop_back()
			"unknown_line": invalid.moves[0].coach.insight.sections[0].line = "unknown"
			"mixed_branch": invalid.moves[0].coach.insight.sections[2].line = "played"
			"wrong_text": invalid.moves[0].coach.insight.sections[0].text = ""
			"missing_section_title": invalid.moves[0].coach.insight.sections[1].erase("title")
			"wrong_turn": invalid.moves[0].best.pv_frames[0].turn = "black"
			"upper_bound": invalid.moves[0].best.bound = "upper"
			"lower_bound": invalid.moves[0].best.bound = "lower"
			"played_bound": invalid.moves[0].played.bound = "upper"
			"same_actual": invalid.moves[0].best = invalid.moves[0].played.duplicate(true)
			"empty_theme": invalid.moves[0].coach.insight.theme = " "
			"empty_title": invalid.moves[0].coach.insight.title = ""
			"empty_summary": invalid.moves[0].coach.insight.summary = " "
			"bad_focus": invalid.moves[0].coach.insight.sections[1].squares = ["z9"]
			"empty_focus": invalid.moves[0].coach.insight.sections[1].squares = []
			"string_focus": invalid.moves[0].coach.insight.sections[1].squares = "c4"
			"extra_frames": invalid.moves[0].best.pv_frames.append(invalid.moves[0].best.pv_frames[-1].duplicate(true))
			"wrong_san": invalid.moves[0].best.san = "Qh4#"
		load_report(invalid)
		verify(panel.insight_buttons.idea.disabled and not panel.insight_controls.visible, malformed + " rejects the entire unsupported packet")
		verify(panel.coach_dialogue.text == invalid.moves[0].coach.dialogue, malformed + " cannot override the ordinary summary")
		panel._show_insight("idea", panel._insight_key())
		verify(panel._insight_section.is_empty(), malformed + " cannot enter through a direct callback")

	# Positive feedback can explain a good actual move rather than invent an alternative.
	var positive: Dictionary = source.duplicate(true)
	var row: Dictionary = positive.moves[0]
	row.merge({"classification": "good", "uci": "e2e4", "san": "e4", "fen_after": FENS[1]}, true)
	row.played = row.best.duplicate(true)
	positive.frames[1] = row.played.pv_frames[1].duplicate(true)
	for section in row.coach.insight.sections:
		section.line = "played"
		section.ply_index = 1
	load_report(positive)
	choose("idea")
	verify(panel.line_kind == "played" and panel.board.state.fen == FENS[1], "good moves can explain the verified actual branch")
	verify(not panel.continuation_button.visible, "positive feedback does not invent a better alternative guide")
	var wrong_positive: Dictionary = positive.duplicate(true)
	for section in wrong_positive.moves[0].coach.insight.sections:
		section.line = "best"
	load_report(wrong_positive)
	verify(not panel.insight_controls.visible and panel.coach_dialogue.text == wrong_positive.moves[0].coach.dialogue, "positive feedback cannot use a stale alternative branch")

	# Scene replacement invalidates saved callbacks and currently selected concepts.
	load_report(source)
	var old_ticket: String = panel._insight_key()
	choose("plan")
	var changed: Dictionary = source.duplicate(true)
	changed.moves[0].coach.insight.sections[1].text = "A refreshed plan from new evidence."
	panel._accept_snapshot(changed)
	verify(panel._insight_section.is_empty() and panel.line_kind.is_empty(), "changed strategic evidence invalidates the old scene")
	panel._show_insight("idea", old_ticket)
	verify(panel._insight_section.is_empty(), "stale section callback cannot target a changed report")
	load_report(source)
	choose("plan")
	var bound_update: Dictionary = source.duplicate(true)
	bound_update.moves[0].best.bound = "upper"
	panel._accept_snapshot(bound_update)
	verify(panel._insight_section.is_empty() and panel.coach_dialogue.text == source.moves[0].coach.dialogue, "new upper-bound evidence cancels selected insight and restores ordinary guidance")
	var next_job: Dictionary = source.duplicate(true)
	next_job.job_id = "another-job"
	load_report(source)
	choose("idea")
	panel._accept_snapshot(next_job, true)
	verify(panel._insight_section.is_empty(), "replaced job clears the topic")
	panel._show_insight("plan", old_ticket)
	verify(panel._insight_section.is_empty(), "old job's callback cannot select a new game's topic")

	# Hidden-answer and higher-priority modes never expose strategy solutions.
	load_report(source)
	choose("idea")
	panel.flush_speech()
	panel._begin_retry()
	panel.flush_speech()
	verify(panel.retry_active and panel._insight_section.is_empty() and not panel.insight_controls.visible, "retry clears strategic selection and hides solution controls")
	verify(panel.spoken[-1] != insight.summary and not panel.spoken[-1].contains("e4"), "retry voice does not leak the strategic solution")
	panel._show_insight("plan", panel._insight_key())
	verify(panel._insight_section.is_empty(), "hidden-answer mode rejects solution callbacks")
	panel._reveal_retry()
	verify(panel.insight_controls.visible and panel.coach_dialogue.text == insight.summary, "revealing feedback restores the strategic summary")
	panel._retry_reply = {"message": "This is the current retry result.", "line": source.moves[0].played.duplicate(true)}
	panel._show_retry_line("retry_candidate")
	verify(not panel.insight_controls.visible and panel.coach_dialogue.text == "This is the current retry result.", "retry line explanation outranks stored strategy")
	load_report(source)
	choose("plan")
	panel._begin_continuation()
	panel.continuation_timer.stop()
	verify(panel._insight_section.is_empty() and panel._continuation_active and not panel.insight_controls.visible, "six-ply guide takes over from selected strategy")
	verify(panel.coach_dialogue.text == source.moves[0].coach.continuation.intro, "six-ply introduction outranks strategy summary")
	panel._show_insight("idea", panel._insight_key())
	verify(panel._insight_section.is_empty(), "guide cannot be interrupted by stale concept callbacks")
	panel._leave_line()
	verify(panel.insight_controls.visible and panel.coach_dialogue.text == insight.summary, "returning from six-ply replay restores strategy overview")
	choose("idea")
	panel._toggle_coach_overview()
	verify(panel._insight_section.is_empty() and not panel.insight_controls.visible and panel.coach_dialogue.text == source.coach.summary, "whole-game overview takes priority")
	panel._toggle_coach_overview()
	verify(panel.coach_dialogue.text == insight.summary, "leaving whole-game overview restores position strategy")
	choose("plan")
	panel._show_evidence({"line": "best", "ply_index": 2, "squares": ["c6"]}, 1, source.job_id, FENS[0])
	verify(panel._insight_section.is_empty() and panel.line_index == 2 and panel.board.state.fen == FENS[2], "ordinary evidence replaces selected strategy scene")
	choose("idea")
	panel._line_step(1)
	verify(panel._insight_section.is_empty() and panel.line_index == 2 and panel.coach_dialogue.text == insight.summary, "manual PV stepping leaves concept narration and keeps the current line")
	choose("idea")
	panel._select_ply(1)
	verify(panel._insight_section.is_empty() and panel.board.state.fen == ACTUAL_FEN, "actual-move navigation cancels strategy scene")

	# Side-specific wording and panel lifecycle remain local presentation state.
	var personal: Dictionary = source.duplicate(true)
	var black: Dictionary = insight.duplicate(true)
	black.summary = "White should keep the king covered; watch for your queen's reply."
	black.sections[0].text = "White could play e4 while keeping the g-pawn near the king."
	personal.moves[0].coach.perspectives = {"black": {"insight": black}}
	load_report(personal)
	choose("plan")
	panel._change_side(1)
	verify(panel._insight_section.is_empty() and panel.coach_dialogue.text == black.summary, "changing human side clears selection and switches perspective")
	choose("idea")
	panel.flush_speech()
	verify(panel.spoken[-1] == black.sections[0].text, "opponent perspective has matching selected narration")
	panel.hide()
	verify(panel._insight_section.is_empty() and not panel._speech_owned and panel._speech_pending_key.is_empty(), "hiding cancels selected strategy and its speech")
	panel.show()
	panel._change_side(0)
	load_report(source)
	for size in [Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = size
		root.content_scale_size = size
		panel._leave_insight()
		await settle()
		var rect: Rect2 = panel.board.get_global_rect()
		for section in ["idea", "plan", "counterplay"]:
			choose(section)
			await settle()
			verify(panel.board.get_global_rect().is_equal_approx(rect), "%dpx layout preserves board for %s" % [size.x, section])
	choose("idea")
	panel._return()
	verify(not panel.visible and panel._insight_section.is_empty() and panel._speech_pending_key.is_empty(), "leaving coaching cancels strategic narration")
	panel.queue_free()
	await settle()
	print("Coaching strategy: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)
