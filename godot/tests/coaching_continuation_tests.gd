extends SceneTree
## Synthetic native UI checks; no backend, archive, or real speech is used.

class TestPanel extends "res://scripts/coaching_mode.gd":
	var clock_ms: int = 1000
	var speaking: bool = false
	var spoken: Array[String] = []

	func _setup_voice() -> void:
		_english_voice = "mock-English"
		_render_speech_controls()

	func _voice_speak(text: String, _voice: String) -> void:
		spoken.append(text)
		speaking = true

	func _voice_stop() -> void:
		speaking = false

	func _voice_is_speaking() -> bool:
		return speaking

	func _continuation_now() -> int:
		return clock_ms

	func flush_speech() -> void:
		if not _speech_pending_key.is_empty():
			speech_timer.stop()
			_speak_coach()

	func finish_speech() -> void:
		flush_speech()
		speaking = false
		_poll_speech()

const FENS = [
	"rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq e6 0 2",
	"rnbqkbnr/pppp1ppp/8/4p3/4P3/5P2/PPPP2PP/RNBQKBNR b KQkq e3 0 2",
	"r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5P2/PPPP2PP/RNBQKBNR w KQkq - 1 3",
	"r1bqkbnr/pppp1ppp/2n5/4p3/4P3/2N2P2/PPPP2PP/R1BQKBNR b KQkq - 2 3",
	"r1bqkb1r/pppp1ppp/2n2n2/4p3/4P3/2N2P2/PPPP2PP/R1BQKBNR w KQkq - 3 4",
	"r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/2N2P2/PPPP2PP/R1BQK1NR b KQkq - 4 4",
	"r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2P2/PPPP2PP/R1BQK1NR w KQkq - 5 5",
]
const ACTUAL_FEN = "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq g3 0 2"
const UCIS = ["e2e4", "b8c6", "b1c3", "g8f6", "f1c4", "f8c5"]
const SANS = ["e4", "Nc6", "Nc3", "Nf6", "Bc4", "Bc5"]
var checks: int = 0
var failures: int = 0
var panel: TestPanel


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
		"in_check": false, "result": "*", "legal_moves": [], "human_to_move": false, "engine_to_move": false}


func fixture() -> Dictionary:
	var frames: Array = []
	var steps: Array = []
	for index in range(FENS.size()):
		frames.append(frame(FENS[index], index, UCIS[index - 1] if index > 0 else "e7e5"))
	for index in range(UCIS.size()):
		steps.append({"ply_index": index + 1, "uci": UCIS[index], "san": SANS[index], "color": frames[index].turn,
			"text": "Step %d: %s develops the position in this example line." % [index + 1, SANS[index]],
			"squares": [str(UCIS[index]).substr(0, 2), str(UCIS[index]).substr(2, 2)]})
	var packet := {"line": "best", "target_plies": 6, "available_plies": 6, "complete": true, "terminal": false,
		"intro": "Try e4. Let's follow the reply and compare how the pieces develop.",
		"summary": "In this line, both bishops are developed. Other replies can change the outcome.", "steps": steps}
	var best := {"uci": UCIS[0], "san": SANS[0], "pv_uci": UCIS.duplicate(), "pv_san": SANS.duplicate(), "pv_frames": frames,
		"score_cp_white": 0, "mate_white": null, "bound": "exact", "depth": 7}
	var actual: Dictionary = frame(ACTUAL_FEN, 1, "g2g4")
	var row := {"ply": 1, "color": "white", "uci": "g2g4", "san": "g4", "fen_before": FENS[0], "fen_after": ACTUAL_FEN,
		"classification": "blunder", "label": "큰 실수", "loss_cp": 300, "best": best, "played": null,
		"retry_legal_moves": ["e2e4", "g2g4"], "coach": {"dialogue": "Try e4 instead of weakening your king.", "continuation": packet}}
	return {"job_id": "continuation-fixture", "revision": 2, "status": "completed", "moves": [row],
		"frames": [frames[0].duplicate(true), actual], "critical_plies": [1], "coach": {"lesson_plies": [1], "summary": "Review the king's safety."}}


func load_report(value: Dictionary) -> void:
	panel._accept_snapshot(value, true, true)
	panel.continuation_timer.stop()
	panel.speech_timer.stop()


func advance() -> void:
	panel.finish_speech()
	panel.clock_ms += 30000
	panel._advance_continuation()
	panel.continuation_timer.stop()
	panel.speech_timer.stop()


func terminal_fixture() -> Dictionary:
	var value: Dictionary = fixture()
	var row: Dictionary = value.moves[0]
	var finish := frame("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3", 1, "d8h4")
	finish.result = "0-1"
	finish.in_check = true
	var before: Dictionary = frame(ACTUAL_FEN, 0, "g2g4")
	var actual := frame("r1bqkbnr/pppp1ppp/2n5/4p3/6P1/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3", 1, "b8c6")
	row.merge({"color": "black", "uci": "b8c6", "san": "Nc6", "fen_before": ACTUAL_FEN, "fen_after": actual.fen, "classification": "missed_mate"}, true)
	row.best.merge({"uci": "d8h4", "san": "Qh4#", "pv_uci": ["d8h4"], "pv_san": ["Qh4#"], "pv_frames": [before, finish]}, true)
	row.coach.continuation.merge({"available_plies": 1, "complete": true, "terminal": true,
		"intro": "Qh4 is checkmate immediately.", "summary": "Check for mate before choosing a developing move.",
		"steps": [{"ply_index": 1, "uci": "d8h4", "san": "Qh4#", "color": "black", "text": "Qh4 is checkmate.", "squares": ["h4", "e1"]}]}, true)
	value.frames = [before, actual]
	return value


func _render_captures(args: PackedStringArray) -> void:
	var output: String = args[args.find("--screenshots") + 1]
	var source: Dictionary = fixture()
	if args.has("--report"):
		source = JSON.parse_string(FileAccess.get_file_as_string(args[args.find("--report") + 1]))
	DirAccess.make_dir_recursive_absolute(output)
	panel = TestPanel.new()
	panel.offline_mode = true
	root.add_child(panel)
	panel._speech_enabled = false
	panel.poll_timer.stop()
	panel.show()
	for dimensions in [Vector2i(1240, 860), Vector2i(900, 680)]:
		root.size = dimensions
		root.content_scale_size = dimensions
		load_report(source)
		await settle()
		var original: Rect2 = panel.board.get_global_rect()
		panel._begin_continuation()
		panel.continuation_timer.stop()
		for index in [0, 3, 6]:
			if index != panel.line_index:
				panel._line_step(index - panel.line_index)
			panel.board.finish_animation()
			await settle()
			verify(panel.board.get_global_rect().is_equal_approx(original), "%dpx width board stays fixed at step %d" % [dimensions.x, index])
			await RenderingServer.frame_post_draw
			var path: String = output.path_join("native-%dx%d-step%d.png" % [dimensions.x, dimensions.y, index])
			verify(root.get_texture().get_image().save_png(path) == OK, "saved rendered native view")
	panel.queue_free()
	await settle()
	print("Coaching continuation renders: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)


func _run() -> void:
	var args: PackedStringArray = OS.get_cmdline_user_args()
	if args.has("--screenshots"):
		await _render_captures(args)
		return
	root.size = Vector2i(1280, 900)
	root.content_scale_size = root.size
	panel = TestPanel.new()
	panel.offline_mode = true
	root.add_child(panel)
	panel.show()
	panel.poll_timer.stop()
	var source: Dictionary = fixture()
	load_report(source)
	await settle()
	var initial_rect: Rect2 = panel.board.get_global_rect()
	verify(panel.continuation_button.visible and panel.continuation_button.text.contains("6수"), "critical alternate exposes the six-ply guide")
	panel._begin_continuation()
	panel.continuation_timer.stop()
	panel.speech_timer.stop()
	verify(panel._continuation_active and panel.line_index == 0 and panel.line_kind == "best", "guide starts before recommended move")
	verify(panel.board.state.fen == FENS[0] and panel.coach_dialogue.text == source.moves[0].coach.continuation.intro, "root frame and introduction agree")
	verify(panel.board.move_annotation.is_empty(), "hypothetical line does not carry actual blunder badge")
	panel.clock_ms += 30000
	panel._advance_continuation()
	verify(panel.line_index == 0, "pending voice blocks automatic movement")
	panel.flush_speech()
	panel.clock_ms += 30000
	panel._advance_continuation()
	verify(panel.line_index == 0 and panel.spoken.size() == 1, "playing voice finishes before advancing")
	advance()
	verify(panel.line_index == 1 and panel.board.state.fen == FENS[1], "first automatic step shows the matching hypothetical position")
	verify(panel.coach_dialogue.text.begins_with("Step 1:") and panel.continuation_moves.text.contains("【e4】"), "step explanation and SAN highlight stay synchronized")
	verify(panel.board.coach_squares == ["e2", "e4"] and panel.board.is_animating(), "step highlights its squares and uses existing piece motion")
	verify(not panel.evaluation_bar.has_score, "root evaluation is not reused for a later hypothetical position")
	await settle()
	verify(panel.board.get_global_rect().is_equal_approx(initial_rect), "guide controls and text do not resize the board")
	panel.flush_speech()
	verify(panel._speech_owned and panel.speaking, "current step is speaking before pause")
	panel._pause_continuation()
	verify(not panel._speech_owned and not panel.speaking and panel._speech_pending_key.is_empty(), "pause interrupts the current narration")
	panel._render()
	verify(panel._speech_pending_key.is_empty(), "paused render cannot restart stopped narration")
	advance()
	verify(panel.line_index == 1 and panel._continuation_paused, "pause holds the position")
	panel._line_step(1)
	verify(panel.line_index == 2 and panel._continuation_paused, "manual next pauses autoplay")
	panel.flush_speech()
	verify(panel.spoken[-1].begins_with("Step 2:"), "manual next reads only the newly selected step")
	panel._line_step(-1)
	verify(panel.line_index == 1 and panel.coach_dialogue.text.begins_with("Step 1:"), "manual previous restores its matching explanation")
	panel.flush_speech()
	verify(panel.spoken[-1].begins_with("Step 1:"), "manual previous replaces the other step's narration")
	panel.finish_speech()
	var spoken_before_resume: int = panel.spoken.size()
	panel._pause_continuation()
	verify(not panel._speech_pending_key.is_empty(), "resume schedules the current sentence again")
	panel.clock_ms += 30000
	panel._advance_continuation()
	verify(panel.line_index == 1, "resume cannot skip past queued narration")
	panel.flush_speech()
	verify(panel.spoken.size() == spoken_before_resume + 1 and panel.spoken[-1].begins_with("Step 1:"), "resume rereads the current explanation before moving")
	for expected in range(2, 7):
		advance()
		verify(panel.line_index == expected and panel.board.state.fen == FENS[expected], "automatic exact frame %d" % expected)
	verify(panel.coach_dialogue.text.contains(source.moves[0].coach.continuation.summary), "final position explains the resulting picture")
	verify(panel.continuation_next.disabled and panel.board.hint_move.is_empty(), "six-ply horizon hides an unshown next move")
	panel.flush_speech()
	verify(panel._speech_owned and not panel._continuation_finished and panel.continuation_pause.text == "일시정지", "final narration retains a pause button until the summary finishes")
	panel._pause_continuation()
	verify(panel.line_index == 6 and panel._continuation_paused and not panel._speech_owned and panel.continuation_pause.text == "계속 재생", "pausing final narration holds its position and offers resume")
	panel._pause_continuation()
	verify(panel.line_index == 6 and not panel._continuation_paused and not panel._speech_pending_key.is_empty(), "resuming final narration stays on the sixth move")
	panel.clock_ms += 30000
	panel._advance_continuation()
	verify(not panel._continuation_finished and panel.line_index == 6, "queued final narration cannot be treated as complete")
	panel.flush_speech()
	verify(panel.spoken[-1].contains(source.moves[0].coach.continuation.summary), "resumed final voice reads its result again")
	advance()
	verify(panel.line_index == 6 and panel._continuation_finished and panel._continuation_paused and panel.continuation_timer.is_stopped() and panel.continuation_pause.text == "처음부터", "replay is offered only after final narration and reading time finish")
	panel._line_step(-1)
	verify(panel.line_index == 5 and not panel._continuation_finished, "previous step clears completed status")
	panel._line_step(1)
	panel._pause_continuation()
	advance()
	verify(panel._continuation_finished, "last step can finish again after manual navigation")
	panel._pause_continuation()
	verify(panel.line_index == 0 and panel._continuation_active and not panel._continuation_finished, "completed guide replay restarts at the original position")
	verify(panel.report == source, "review and original game data are unchanged")
	panel._leave_line()
	verify(not panel._continuation_active and panel.board.state.fen == ACTUAL_FEN, "return restores the actual move")
	panel._advance_continuation()
	verify(panel.board.state.fen == ACTUAL_FEN, "late timer callback cannot leave actual game")

	# A shorter verified PV is clearly limited, never padded with invented moves.
	var shorter: Dictionary = source.duplicate(true)
	shorter.moves[0].coach.continuation.steps.resize(2)
	shorter.moves[0].coach.continuation.available_plies = 2
	shorter.moves[0].coach.continuation.complete = false
	load_report(shorter)
	panel._begin_continuation()
	panel.continuation_timer.stop()
	verify(panel.continuation_progress.text.contains("확인된 수순까지"), "short PV describes its actual coverage")
	panel._line_step(99)
	verify(panel.line_index == 2 and panel.continuation_next.disabled, "manual navigation is capped at available two plies")
	load_report(terminal_fixture())
	panel._begin_continuation()
	panel.continuation_timer.stop()
	verify(panel.continuation_progress.text.contains("여기서 종료"), "early terminal line has a distinct ending label")

	# All changes of scene invalidate playback; same-job refreshes preserve it.
	load_report(source)
	panel._begin_continuation()
	panel.continuation_timer.stop()
	panel._accept_snapshot(source)
	verify(panel._continuation_active, "identical progress snapshot does not restart a guide")
	var changed: Dictionary = source.duplicate(true)
	changed.moves[0].coach.continuation.steps[0].text = "A changed explanation."
	panel._accept_snapshot(changed)
	verify(not panel._continuation_active and panel._speech_pending_key.is_empty() == false, "changed evidence invalidates guide and schedules ordinary feedback")
	for action in ["_show_line", "_show_phase", "_toggle_coach_overview", "_begin_retry", "_select_ply", "_invalidate_replaced_job", "hide"]:
		load_report(source)
		panel._begin_continuation()
		panel.continuation_timer.stop()
		match action:
			"_show_line": panel._show_line("best")
			"_show_phase": panel._show_phase(false)
			"_select_ply": panel._select_ply(1)
			_: panel.call(action)
		verify(not panel._continuation_active and panel.continuation_timer.is_stopped(), action + " cancels guided autoplay")
		var held: String = str(panel.board.state.fen)
		panel._advance_continuation()
		verify(panel.board.state.fen == held, action + " rejects stale timer")
		panel.show()

	load_report(source)
	panel._begin_continuation()
	panel.continuation_timer.stop()
	panel._show_evidence({"line": "best", "ply_index": 2, "squares": ["c6"]}, 1, source.job_id, FENS[0])
	verify(not panel._continuation_active and panel.line_index == 2 and panel.board.state.fen == FENS[2], "evidence scene cancels guide before switching")
	var new_job: Dictionary = source.duplicate(true)
	new_job.job_id = "replacement-job"
	panel._begin_continuation()
	panel._accept_snapshot(new_job, true)
	verify(not panel._continuation_active, "replacement job invalidates old guide")

	# Bad/old reports cannot open a guide, and perspective wording follows the human side.
	for failure in ["missing", "wrong_uci", "wrong_root", "wrong_color", "pending", "bound"]:
		var invalid: Dictionary = source.duplicate(true)
		match failure:
			"missing": invalid.moves[0].coach.erase("continuation")
			"wrong_uci": invalid.moves[0].coach.continuation.steps[0].uci = "d2d4"
			"wrong_root": invalid.moves[0].best.pv_frames[0].fen = ACTUAL_FEN
			"wrong_color": invalid.moves[0].coach.continuation.steps[0].color = "black"
			"pending": invalid.moves[0].classification = "pending"
			"bound": invalid.moves[0].best.bound = "lower"
		load_report(invalid)
		verify(not panel.continuation_button.visible, failure + " report cannot offer a narrated alternative")
		panel._begin_continuation()
		verify(not panel._continuation_active, failure + " report cannot start through direct callback")
	var personal: Dictionary = source.duplicate(true)
	var black: Dictionary = personal.moves[0].coach.continuation.duplicate(true)
	black.intro = "White could play e4. Watch your reply as Black."
	personal.moves[0].coach.perspectives = {"black": {"continuation": black}}
	load_report(personal)
	panel._change_side(1)
	panel._begin_continuation()
	panel.continuation_timer.stop()
	verify(panel.coach_dialogue.text == black.intro, "opponent perspective uses its own continuation wording")
	panel._change_side(0)
	verify(not panel._continuation_active, "side change cancels previous speaker perspective")
	panel._speech_enabled = false
	panel._begin_continuation()
	panel.continuation_timer.stop()
	panel.clock_ms += 1000
	panel._advance_continuation()
	verify(panel.line_index == 0, "silent fallback leaves reading time")
	panel.clock_ms += 10000
	panel._advance_continuation()
	verify(panel.line_index == 1, "silent fallback eventually advances without a voice")
	panel._return()
	verify(not panel._continuation_active and not panel.visible, "leaving review cancels playback")
	panel.queue_free()
	await settle()
	print("Coaching continuation: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)
