extends SceneTree
## Native scene/HTTP verification. Synthetic controls are not manual play.

class VoicePanel extends "res://scripts/coaching_mode.gd":
	var spoken: Array[String] = []
	var stopped: int = 0
	var mock_speaking: bool = false

	func _setup_voice() -> void:
		_english_voice = "mock-local-English"
		_render_speech_controls()

	func _voice_speak(text: String, _voice: String) -> void:
		spoken.append(text)
		mock_speaking = true

	func _voice_stop() -> void:
		stopped += 1
		mock_speaking = false

	func _voice_is_speaking() -> bool:
		return mock_speaking

	func flush_voice() -> void:
		if not _speech_pending_key.is_empty():
			speech_timer.stop()
			speech_timer.timeout.emit()

class MockWindowsSpeech extends "res://scripts/windows_speech.gd":
	var queries: int = 0
	var requests: Array[Dictionary] = []
	var cancellations: int = 0

	func query_voices() -> void:
		queries += 1

	func synthesize(ticket: int, text: String, voice: String) -> void:
		requests.append({"ticket": ticket, "text": text, "voice": voice})

	func cancel_synthesis() -> void:
		cancellations += 1


class DesktopVoicePanel extends "res://scripts/coaching_mode.gd":
	var played: int = 0
	var playing: bool = false

	func _setup_voice() -> void:
		_setup_windows_voice()
		_render_speech_controls()

	func _make_windows_speech() -> WindowsSpeech:
		return MockWindowsSpeech.new()

	func _play_windows_audio(_stream: AudioStreamWAV) -> void:
		played += 1
		playing = true

	func _windows_audio_is_playing() -> bool:
		return playing

	func _voice_stop() -> void:
		playing = false
		super._voice_stop()

	func flush_voice() -> void:
		if not _speech_pending_key.is_empty():
			speech_timer.stop()
			speech_timer.timeout.emit()

var app: Variant
var panel: Variant
var checks: int = 0
var failures: int = 0
var fixture: Dictionary = {}
var output: String = ""
var live: bool = false
var geometries: Array = []
var library_geometries: Array = []


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func settle() -> void:
	for _frame in range(12):
		await process_frame


func wait_for(condition: Callable, message: String, timeout_ms: int = 20000) -> bool:
	var deadline: int = Time.get_ticks_msec() + timeout_ms
	while not condition.call():
		if Time.get_ticks_msec() >= deadline:
			verify(false, "timeout: " + message + "; " + str(panel._error))
			return false
		await create_timer(0.02).timeout
	verify(true, message)
	return true


func geometry() -> Array:
	var rect: Rect2 = panel.board.get_global_rect()
	var grid: Rect2 = panel.board.get_board_rect()
	var bar: Rect2 = panel.evaluation_bar.bar_rect()
	return [rect.position.x, rect.position.y, rect.size.x, rect.size.y,
		grid.position.x, grid.position.y, grid.size.x, grid.size.y,
		bar.position.x, bar.position.y, bar.size.x, bar.size.y]


func _synthetic() -> void:
	app.backend_url = "http://127.0.0.1:1"
	app.state = {"result": "*", "moves": [{"uci": "f2f3"}], "human_color": "white"}
	app._render()
	verify(app.coaching_entry_button.disabled, "live unfinished game cannot start postgame coaching")
	app.state.result = "0-1"
	app._render()
	verify(not app.coaching_entry_button.disabled, "completed current game exposes coaching")
	verify(app._adopt_review(fixture.preview), "archive-shaped replay enters existing review")
	app._render()
	verify(not app.coaching_entry_button.disabled, "archive/PGN replay exposes coaching")
	await app._on_coaching_pressed()
	verify(panel.visible and not app.main_contents.visible, "coaching entry opens overlay")
	verify(panel.source_pgn == fixture.pgn, "selected archived PGN transferred unchanged")
	panel._preview = fixture.preview
	panel._render()
	verify(not panel.start_button.disabled, "validated replay is ready for explicit analysis start")
	var snapshot: Dictionary = fixture.report.duplicate(true)
	panel._accept_snapshot(snapshot, true)
	verify(panel.selected_ply == 3, "completion initially selects first personal critical move")
	verify(panel.show_after and panel.board.state.fen == fixture.report.frames[3].fen, "mistake selection defaults to exact actual after-position")
	verify(panel.board.hint_move.is_empty() and panel.board.move_annotation.get("symbol") == "??", "actual move badge is shown without a misplaced recommendation arrow")
	panel._show_phase(false)
	verify(panel.board.hint_move == "e2e4" and panel.evaluation_bar.has_score, "explicit before view shows recommendation and matching root estimate")
	verify(panel.focus_option.get_selected_id() == 0 and panel.critical_list.item_count == 2, "default important positions include both players")
	verify(panel.critical_list.get_item_text(0).contains("흑 · 상대 수"), "opponent's black mistake is clearly labelled")
	verify(panel.summary_label.text.contains("백 · 내 수") and panel.summary_label.text.contains("흑 · 상대 수"), "separate summaries identify both sides")
	var source_report: Dictionary = panel.report.duplicate(true)
	panel._change_side(1)
	verify(panel.human_color == "black" and panel.summary_label.text.contains("흑 · 내 수") and panel.summary_label.text.contains("백 · 상대 수"), "color change immediately recomputes both-side summaries")
	verify(panel.report == source_report, "side labels never require or change the engine report")
	panel._accept_snapshot(snapshot)
	verify(panel.human_color == "black", "polling does not undo chosen personal color")
	panel._change_side(0)
	panel._show_phase(true)
	verify(panel.board.state.fen == fixture.report.frames[3].fen, "actual move after-position shown")
	verify(panel.board.hint_move.is_empty() and panel.evaluation_bar.has_score and panel.evaluation_bar.snapshot.fen == panel.board.state.fen, "after-position uses its own next-row root evaluation without a recommendation arrow")
	for kind in ["best", "played"]:
		panel._show_line(kind)
		var frames: Array = fixture.report.moves[2][kind].pv_frames
		verify(panel.board.state.fen == frames[0].fen, kind + " line starts at original pre-position")
		verify(panel.evaluation_bar.has_score, kind + " root estimate has correct reference")
		for index in range(1, frames.size()):
			panel._line_step(1)
			verify(panel.board.state.fen == frames[index].fen, kind + " legal PV frame %d" % index)
			verify(not panel.evaluation_bar.has_score, "root evaluation not relabelled as later PV score")
		verify(panel.line_next.disabled and not panel.line_previous.disabled, kind + " PV boundaries")
		panel._leave_line()
		verify(panel.board.state.fen == fixture.report.frames[3].fen and panel.show_after, "line returns to actual selected move's after-position")
	panel._last()
	verify(panel.board.state.fen == fixture.report.frames[4].fen, "end control reaches actual checkmate")
	verify(panel.board.state.in_check, "mate board retains check indicator")
	panel._change_focus(1)
	verify(panel.critical_list.item_count == 1 and panel.critical_list.get_item_metadata(0) == 3, "my filter shows personal mistake")
	panel._change_focus(2)
	verify(panel.critical_list.item_count == 1 and panel.critical_list.get_item_metadata(0) == 2, "opponent filter shows opposing mistake")
	panel._change_focus(0)
	verify(panel.critical_list.item_count == 2, "both filter restores both mistakes")
	panel._select_ply(3)
	panel._critical_step(-1)
	verify(panel.selected_ply == 2, "previous critical control obeys selected filter")
	_evaluation_checks(snapshot)
	await _walkthrough_checks(snapshot)
	await _character_checks(snapshot)
	await _voice_checks(snapshot)
	_windows_voice_checks(snapshot)
	_perspective_voice_checks(snapshot)
	_teaching_voice_checks(snapshot)
	await _badge_checks(snapshot)
	var before_flip: Dictionary = panel.board.state.duplicate(true)
	var old_view: bool = panel.model.view_from_white
	panel.flip_view()
	verify(panel.model.view_from_white != old_view and panel.board.state == before_flip, "flip changes perspective only")
	panel.report.status = "running"
	panel.report.progress.completed = 2
	panel._select_ply(1)
	panel._show_phase(true)
	panel._accept_snapshot(snapshot)
	verify(panel.selected_ply == 1 and panel.show_after, "completed poll does not reset deliberate replay selection")
	var stale: Dictionary = snapshot.duplicate(true)
	stale.revision = int(snapshot.revision) - 1
	stale.status = "running"
	panel._accept_snapshot(stale)
	verify(panel.report.status == "completed", "older revision cannot roll report back")
	stale.revision = int(snapshot.revision) + 1
	stale.job_id = "different-game"
	panel._accept_snapshot(stale)
	verify(panel.report.job_id == snapshot.job_id, "another job cannot replace selected report")
	panel._invalidate_replaced_job()
	verify(panel.report.status == "cancelled" and not panel._error.is_empty(), "foreign job invalidates local active status with explanation")
	panel._render()
	verify(panel.cancel_button.disabled and not panel.start_button.disabled, "foreign job cannot be cancelled from this stale view")
	verify(panel._score({"score_cp_white": 70, "bound": "lower"}) == "≥ +0.70", "lower score bound remains visible")
	verify(panel._score({"mate_white": -3, "bound": "upper"}) == "≤ 흑 M3", "mate score bound remains visible")
	panel._error = ""
	panel._accept_snapshot(snapshot, true)
	panel._select_ply(3)
	panel._show_line("played")
	panel._line_step(1)
	panel._line_step(1)
	var pending: Dictionary = snapshot.duplicate(true)
	pending.status = "running"
	pending.progress.completed = 0
	for row in pending.moves:
		row.best = null
		row.played = null
		row.classification = "pending"
	panel._accept_snapshot(pending, true, true)
	verify(panel.line_kind.is_empty() and panel.line_index == 0 and panel.show_after, "restart resets old PV branch state to actual after-position")
	verify(panel.board.state.fen == snapshot.frames[3].fen and panel.board.hint_move.is_empty() and panel.board.move_annotation.is_empty(), "restart shows selected actual position without pending badges")
	panel._accept_snapshot(snapshot)
	panel._show_line("played")
	panel._line_step(1)
	panel._line_step(1)
	var shortened: Dictionary = snapshot.duplicate(true)
	shortened.moves[2].played.pv_frames = shortened.moves[2].played.pv_frames.slice(0, 2)
	shortened.moves[2].played.pv_uci = shortened.moves[2].played.pv_uci.slice(0, 1)
	panel._accept_snapshot(shortened)
	verify(panel.line_index == 1 and panel.line_next.disabled, "replacement with shorter PV clamps replay index")
	panel._accept_snapshot(snapshot, true)
	app.offline_mode = false
	app.evaluation_in_flight = false
	app.hint_in_flight = false
	app.hint_enabled = true
	app._poll_evaluation()
	app._poll_hint()
	verify(not app.evaluation_in_flight and not app.hint_in_flight, "coaching suspends hidden live hint/evaluation polling")
	app.offline_mode = true
	for viewport in [Vector2i(1240, 860), Vector2i(900, 680), Vector2i(1500, 1000)]:
		root.size = viewport
		root.content_scale_size = viewport
		panel._accept_snapshot(snapshot, true)
		panel._select_ply(1)
		await settle()
		var base: Array = geometry()
		verify(app.size.is_equal_approx(Vector2(viewport)), "main scene fills resized viewport %s" % viewport)
		var maximum: float = 0.0
		for phase in ["long", "mate", "cancelled", "error", "line"]:
			var next: Dictionary = snapshot.duplicate(true)
			next.moves[2].explanation = "기물 손실과 상대의 응수를 살펴보세요. ".repeat(80)
			next.moves[2].coach.dialogue = "기물 손실과 상대의 응수를 살펴보자. ".repeat(80)
			next.summary.message = "긴 요약 ".repeat(80)
			if phase in ["cancelled", "error"]:
				next.status = phase
				next.error = "연결 상태를 확인한 뒤 다시 분석하세요. ".repeat(40)
			panel._accept_snapshot(next, true)
			panel._select_ply(3)
			if phase == "line":
				panel._show_line("played")
				panel._line_step(1)
			elif phase == "mate":
				panel._last()
			await settle()
			var actual: Array = geometry()
			for index in range(base.size()):
				maximum = maxf(maximum, absf(float(base[index]) - float(actual[index])))
			verify(actual == base, "%s geometry stable at %s" % [phase, viewport])
		var sidebar: Control = panel.find_child("CoachingSidebar", true, false)
		verify(sidebar.get_global_rect().end.x <= float(viewport.x) + 1, "sidebar inside viewport %s" % viewport)
		verify(panel.board.get_board_rect().size.x > 250, "board remains usable at %s" % viewport)
		geometries.append({"viewport": [viewport.x, viewport.y], "maximum_delta": maximum, "geometry": base})
	verify(geometries[0].geometry != geometries[1].geometry and geometries[1].geometry != geometries[2].geometry, "explicit window resizing changes available board area")
	panel._accept_snapshot(snapshot, true)
	await panel._return()
	verify(not panel.visible and app.main_contents.visible, "return restores original replay")
	verify(app.review_mode and app.review_pgn == fixture.pgn, "return preserves selected archived game")
	panel.show()
	app.main_contents.hide()
	panel.report.status = "running"
	panel.backend_url = "http://127.0.0.1:1"
	panel.requester.timeout = 0.2
	await panel._return()
	verify(not panel.visible and app.main_contents.visible, "failed cancellation does not trap user in overlay")
	verify(not panel.return_warning.is_empty() and app.error_message == panel.return_warning, "unconfirmed cancellation reported honestly")
	verify(panel._unconfirmed_job == snapshot.job_id, "same job can be recognized on a later reconnect")
	verify(app.find_child("PieceAnimationToggle", true, false) == null, "animation has no user-facing on/off control")
	for target in [app.board, app.analysis_overlay.board, panel.board]:
		verify(target.get("animation_enabled") == true, "all three boards animate by default")
	_library_checks()
	await _library_layout_checks()


func _library_checks() -> void:
	var first := {"game_id": "saved-first", "human_color": "white", "result": "1-0", "ply_count": 30,
		"started_at": "2026-11-01T14:30:00+00:00", "started_at_local": "2026-11-01 23:30:00", "started_at_utc_offset": "+09:00",
		"ended_at_local": "2026-11-02 00:01:00", "ended_at_utc_offset": "+09:00"}
	var second := {"game_id": "saved-second", "human_color": "black", "result": "0-1", "ply_count": 42,
		"started_at": "2026-11-01T15:30:00+00:00", "started_at_local": "2026-11-02 00:30:00", "started_at_utc_offset": "+09:00",
		"ended_at_local": "2026-11-02 01:02:03", "ended_at_utc_offset": "+09:00"}
	verify(app.library_button.text == "대국 보관함" and app.library_dialog.title == "대국 보관함", "game library entry clearly named")
	verify(app._record_description(second).begins_with("2026-11-02 00:30:00"), "archive list uses backend local day rollover exactly")
	verify(app._record_details(second).contains("종료: 2026-11-02 01:02:03 (UTC+09:00)"), "selected detail shows exact end time and zone")
	app._library_games = [first, second]
	app._library_status_note = "자동 보관된 대국 2개 · 최근 대국부터 표시합니다."
	app.library_search.text = ""
	app._filter_library()
	verify(app.library_list.item_count == 2, "all saved games initially listed")
	app.library_search.text = "00:30"
	app._filter_library()
	verify(app.library_list.item_count == 1 and app._selected_library_game().game_id == "saved-second", "time filter retains correct stable game identity")
	verify(app.library_detail.text.contains("시작: 2026-11-02 00:30:00") and app.library_detail.text.contains("내 흑"), "selected details include date/time and personal color")
	app.library_search.text = ""
	app._filter_library()
	verify(app._selected_library_game().game_id == "saved-second" and app.library_list.get_selected_items()[0] == 1, "clearing filter preserves selected game id rather than index")
	app.library_search.text = "없는 날짜"
	app._filter_library()
	verify(app.library_list.item_count == 0 and app.library_open_button.disabled, "empty search cannot open an unrelated game")
	app.library_search.text = "백 승"
	app._filter_library()
	verify(app._selected_library_game().game_id == "saved-first", "result filter finds requested saved game")


func _library_layout_checks() -> void:
	var dialog: AcceptDialog = app.library_dialog
	var footer: Control = dialog.find_child("GameLibraryFooter", true, false)
	var close_button: Control = dialog.find_child("GameLibraryClose", true, false)
	for viewport in [Vector2i(900, 680), Vector2i(1240, 860)]:
		dialog.hide()
		root.size = viewport
		root.content_scale_size = viewport
		await settle()
		for content in ["normal", "long", "empty"]:
			app._library_status_note = "자동 보관된 대국 2개 · 최근 대국부터 표시합니다."
			if content == "long":
				app._library_status_note += "\n날짜와 시간을 확인하고 대국을 선택하세요. ".repeat(60)
			app.library_search.text = "없는 날짜" if content == "empty" else ""
			app._filter_library()
			dialog.popup_centered_ratio(0.78)
			for index in range(18):
				await process_frame
				if index not in [0, 5, 17]:
					continue
				var window_rect := Rect2(Vector2(dialog.position), Vector2(dialog.size))
				var window_local := Rect2(Vector2.ZERO, Vector2(dialog.size))
				var open_rect: Rect2 = app.library_open_button.get_global_rect()
				var close_rect: Rect2 = close_button.get_global_rect()
				var footer_rect: Rect2 = footer.get_global_rect()
				var list_rect: Rect2 = app.library_list.get_global_rect()
				verify(Rect2(Vector2.ZERO, Vector2(viewport)).encloses(window_rect), "library window stays within %s, %s frame%d" % [viewport, content, index])
				verify(window_local.encloses(open_rect) and window_local.encloses(close_rect), "library open/close controls remain inside window, %s frame%d" % [content, index])
				verify(window_local.encloses(footer_rect) and footer_rect.end.y <= open_rect.position.y, "library footer stays visible above actions, %s frame%d" % [content, index])
				verify(list_rect.end.y <= footer_rect.position.y and list_rect.size.y >= 200, "library list fits without covering footer, %s frame%d" % [content, index])
				library_geometries.append({"viewport": [viewport.x, viewport.y], "case": content, "frame": index,
					"window": [window_rect.position.x, window_rect.position.y, window_rect.size.x, window_rect.size.y],
					"open_button_bottom": open_rect.end.y, "close_button_bottom": close_rect.end.y,
					"footer_bottom": footer_rect.end.y, "list_bottom": list_rect.end.y})
			dialog.hide()


func _evaluation_checks(snapshot: Dictionary) -> void:
	var value: Dictionary = snapshot.duplicate(true)
	value.moves[1].best.score_cp_white = 180
	value.moves[2].best.score_cp_white = 45
	value.moves[2].played.score_cp_white = -1300
	value.moves[3].best.score_cp_white = -240
	value.moves[3].best.bound = "upper"
	panel._accept_snapshot(value.duplicate(true), true, true)
	panel._select_ply(3)
	verify(panel.evaluation_bar.has_score and panel.evaluation_bar.snapshot.cp == -240, "after-position uses next-row search, not previous restricted played score")
	verify(panel.evaluation_bar.snapshot.bound == "upper" and panel.evaluation_label.text.contains("≤"), "white-perspective bound survives the after-position lookup")
	panel._show_phase(false)
	verify(panel.evaluation_bar.snapshot.cp == 45, "before-position retains its own root estimate")
	panel._show_phase(true)
	var share: float = panel.evaluation_bar.white_share
	panel.flip_view()
	verify(panel.evaluation_bar.snapshot.cp == -240 and panel.evaluation_bar.white_share == share, "board flip never changes the white-perspective sign")
	panel.flip_view()
	panel._walkthrough_active = true
	panel._walkthrough_cursor = 1
	panel._walkthrough_target = 3
	panel._render()
	verify(panel.evaluation_bar.snapshot.cp == 180 and panel.evaluation_bar.snapshot.fen == value.frames[1].fen, "walkthrough score follows actual cursor rather than selected lesson")
	verify(panel.board.hint_move.is_empty() and panel.evaluation_label.text.contains("+1.80"), "black-to-move replay keeps white score and no answer arrow")
	panel._cancel_walkthrough()
	panel._show_line("played")
	verify(panel.evaluation_bar.snapshot.cp == 45, "played-line root gauge uses unrestricted best instead of constrained played score")
	verify(panel._selected_line().score_cp_white == -1300 and panel.comparison_label.text.contains("-13.00"), "played move's separate evaluation remains in comparison card")
	panel._line_step(1)
	verify(not panel.evaluation_bar.has_score, "PV child does not borrow root or mainline score even at matching FEN")
	panel._leave_line()
	panel._begin_retry()
	verify(not panel.evaluation_bar.has_score and panel.evaluation_label.text == "정답과 평가는 잠시 가렸어요", "retry hides matching position score too")
	panel._retry_reply = {"line": value.moves[2].played.duplicate(true), "best": value.moves[2].best.duplicate(true), "message": "Controlled retry result"}
	panel._retry_reply.best.score_cp_white = 92
	panel._render()
	verify(not panel.evaluation_bar.has_score, "fresh retry feedback stays hidden until explicitly revealed")
	panel._show_retry_line("retry_candidate")
	verify(panel.evaluation_bar.snapshot.cp == 92 and panel._selected_line().score_cp_white == -1300, "revealed retry candidate gauge uses fresh retry best rather than candidate or archived best")
	panel._line_step(1)
	verify(not panel.evaluation_bar.has_score, "revealed retry child never inherits fresh root score")
	panel._line_step(-1)
	panel._retry_reply.best.pv_frames[0].fen = value.frames[1].fen
	panel._render()
	verify(not panel.evaluation_bar.has_score, "mismatched fresh retry best is rejected without falling back to archived score")
	panel._select_ply(3)
	panel.report.moves[3].best = null
	panel._render()
	verify(not panel.evaluation_bar.has_score, "missing next-row analysis never falls back to previous played root")
	panel.report = value.duplicate(true)
	panel.report.moves[3].best.pv_frames[0].fen = value.frames[2].fen
	panel._render()
	verify(not panel.evaluation_bar.has_score, "mismatched search-root FEN is rejected")
	panel.report = value.duplicate(true)
	panel.report.moves[3].fen_before = value.frames[2].fen
	panel._render()
	verify(not panel.evaluation_bar.has_score, "mismatched row FEN is rejected")
	panel.report = value.duplicate(true)
	panel.report.frames[4]["result"] = "0-1"
	panel._last()
	verify(panel.evaluation_bar.has_score and panel.evaluation_label.text == "흑 승리" and panel.evaluation_bar.white_share == 0.0, "terminal frame displays its result without inventing a search score")
	panel.report.frames[4]["result"] = "1/2-1/2"
	panel._render()
	verify(panel.evaluation_label.text == "무승부" and panel.evaluation_bar.white_share == 0.5, "terminal draw uses the recorded frame result")
	panel.report.frames[4]["result"] = "*"
	panel._render()
	verify(not panel.evaluation_bar.has_score, "unsearched final nonterminal position remains unavailable")
	panel.report = value.duplicate(true)
	panel.report.moves[3].best.merge({"score_cp_white": null, "mate_white": -2, "winner": "black"}, true)
	panel._select_ply(3)
	verify(panel.evaluation_bar.has_score and panel.evaluation_bar.white_share == 0.0 and panel.evaluation_label.text.contains("흑 메이트"), "matching root mate keeps black-winning perspective")
	panel._accept_snapshot(snapshot, true, true)


func _walkthrough_checks(snapshot: Dictionary) -> void:
	panel._change_focus(1)
	panel._first()
	var visited: Array = []
	var capture := func(ply: int): visited.append(ply)
	panel.replay_frame_shown.connect(capture)
	panel._critical_step(1)
	verify(panel._walkthrough_active and panel._walkthrough_cursor == 0 and panel._walkthrough_target == 3, "next mistake starts at actual shown before-position")
	verify(panel.critical_next.disabled and panel.critical_previous.disabled, "duplicate critical navigation disabled during replay")
	for expected in [1, 2]:
		panel._advance_walkthrough()
		verify(panel.board.state.fen == snapshot.frames[expected].fen, "every intervening real move shown in order: %d" % expected)
		verify(panel.board.hint_move.is_empty() and panel.evaluation_bar.has_score and panel.evaluation_bar.snapshot.fen == panel.board.state.fen, "intervening replay displays matching position analysis without recommendation arrow")
	verify(visited == [1, 2], "includes prior selected move and opponent reply, without skips")
	verify(panel.walkthrough_label.text.contains("【1… e5】"), "SAN progression marks current actual move")
	panel._advance_walkthrough()
	verify(not panel._walkthrough_active and panel.selected_ply == 3 and panel.show_after, "replay stops after target actual move")
	verify(visited == [1, 2, 3] and panel.board.state.fen == snapshot.frames[3].fen and panel.board.move_annotation.get("symbol") == "??", "target actual move is included and gets its own final badge")
	panel._select_ply(1)
	panel._show_phase(true)
	visited.clear()
	panel._critical_step(1)
	verify(panel._walkthrough_cursor == 1, "after-position starts after already shown move")
	panel._advance_walkthrough()
	panel._advance_walkthrough()
	verify(visited == [2, 3], "after-position skips already completed move and includes target")
	panel._select_ply(1)
	panel._show_line("best")
	panel._line_step(1)
	panel._critical_step(1)
	verify(panel.line_kind.is_empty() and panel.board.state.fen == snapshot.frames[0].fen, "PV exits to real source before actual-game replay")
	panel._pause_walkthrough()
	panel._advance_walkthrough()
	verify(panel._walkthrough_cursor == 0 and panel._walkthrough_paused, "pause retains current real position")
	panel._pause_walkthrough()
	panel._advance_walkthrough()
	verify(panel._walkthrough_cursor == 1 and not panel._walkthrough_paused, "resume continues next real move")
	panel._skip_walkthrough()
	verify(not panel._walkthrough_active and panel.selected_ply == 3 and panel.show_after, "skip goes directly to intended actual after-position")
	panel._select_ply(1)
	panel._critical_step(1)
	panel._advance_walkthrough()
	panel._select_ply(4)
	panel._advance_walkthrough()
	verify(not panel._walkthrough_active and panel.selected_ply == 4, "manual move selection cancels pending replay")
	panel._select_ply(1)
	panel._critical_step(1)
	panel._advance_walkthrough()
	var frame_before_filter: Dictionary = panel.board.state.duplicate(true)
	panel._change_focus(2)
	verify(not panel._walkthrough_active and panel.board.state == frame_before_filter, "changing filter cancels replay without changing shown position")
	panel._change_focus(1)
	panel._select_ply(1)
	panel._critical_step(1)
	panel._accept_snapshot(snapshot, true, true)
	verify(not panel._walkthrough_active, "fresh analysis invalidates former replay target")
	panel._select_ply(1)
	panel._change_focus(0)
	panel._select_critical(1)
	verify(panel._walkthrough_active and panel._walkthrough_target == 3, "forward important-list click also replays intervening moves")
	panel.walkthrough_timer.wait_time = 0.02
	panel.walkthrough_timer.start()
	await wait_for(func(): return not panel._walkthrough_active, "timer advances actual replay and completes", 2000)
	verify(panel.selected_ply == 3 and panel.board.state.fen == snapshot.frames[3].fen, "timed playback finishes at exact target after-position")
	panel.walkthrough_timer.wait_time = 0.65
	panel.replay_frame_shown.disconnect(capture)
	var longer: Dictionary = fixture.walkthrough_report
	panel._accept_snapshot(longer, true)
	panel._select_ply(3)
	panel._navigate_critical(10)
	for _advance in range(5):
		panel._advance_walkthrough()
	verify(panel._walkthrough_cursor == 8 and panel.selected_ply == 3, "long replay separates displayed cursor eight from origin ply three")
	verify(panel.board.state.fen == longer.frames[8].fen, "long replay visibly reaches eighth actual position")
	panel._step(1)
	verify(not panel._walkthrough_active and panel.board.state.fen == longer.frames[9].fen, "manual next advances displayed position eight to nine, not stale origin")
	panel._select_ply(3)
	panel._navigate_critical(10)
	for _advance in range(5):
		panel._advance_walkthrough()
	panel._step(-1)
	verify(not panel._walkthrough_active and panel.board.state.fen == longer.frames[7].fen, "manual previous retreats displayed position eight to seven")
	panel._first()
	panel._navigate_critical(10)
	verify(panel.first_button.disabled and panel.previous_button.disabled, "beginning replay initially disables first and previous")
	panel._advance_walkthrough()
	verify(not panel.first_button.disabled and not panel.previous_button.disabled, "first and previous enable after first displayed replay move")
	panel._step(-1)
	verify(panel.board.state.fen == longer.frames[0].fen, "manual previous from first displayed move returns to initial position")
	panel._accept_snapshot(snapshot, true)
	panel._select_ply(3)


func _drag_retry_piece(from_square: String, to_square: String) -> void:
	var area: Rect2 = panel.board.get_board_rect()
	var origin: Vector2 = panel.board.global_position + area.position + (Vector2(panel.model.square_to_display(from_square)) + Vector2.ONE * 0.5) * area.size.x / 8.0
	var target: Vector2 = panel.board.global_position + area.position + (Vector2(panel.model.square_to_display(to_square)) + Vector2.ONE * 0.5) * area.size.x / 8.0
	var motion := InputEventMouseMotion.new()
	motion.position = origin
	motion.global_position = origin
	root.push_input(motion, true)
	var press := InputEventMouseButton.new()
	press.position = origin
	press.global_position = origin
	press.button_index = MOUSE_BUTTON_LEFT
	press.pressed = true
	root.push_input(press, true)
	await process_frame
	motion = InputEventMouseMotion.new()
	motion.position = target
	motion.global_position = target
	motion.relative = target - origin
	motion.button_mask = MOUSE_BUTTON_MASK_LEFT
	root.push_input(motion, true)
	var release := InputEventMouseButton.new()
	release.position = target
	release.global_position = target
	release.button_index = MOUSE_BUTTON_LEFT
	release.pressed = false
	root.push_input(release, true)
	await process_frame


func _character_checks(snapshot: Dictionary) -> void:
	panel._accept_snapshot(snapshot, true)
	panel._select_ply(3)
	verify(panel.avatar != null and panel.coach_dialogue.text == snapshot.moves[2].coach.dialogue, "original rook companion presents selected grounded dialogue")
	verify(panel._lesson_plies() == [1, 2, 3, 4], "selected lesson course comes from report")
	panel._toggle_coach_overview()
	verify(panel.coach_dialogue.text == snapshot.coach.summary and panel.board.coach_squares.is_empty(), "overview shows whole-game coaching and clears position highlights")
	panel._toggle_coach_overview()
	var evidence: Dictionary = snapshot.moves[2].coach.evidence[0]
	panel._show_evidence(evidence, 3, snapshot.job_id, snapshot.moves[2].fen_before)
	verify(panel.line_kind == "played" and panel.line_index == 2 and panel.board.state.fen == snapshot.moves[2].played.pv_frames[2].fen, "evidence opens its precise legal PV frame")
	verify(panel.board.coach_squares == ["h4", "e1"], "evidence highlights only its referenced squares")
	panel._line_step(-1)
	verify(panel.board.coach_squares.is_empty(), "highlights disappear at a different PV frame")
	panel._line_step(1)
	verify(panel.board.coach_squares == ["h4", "e1"], "returning to exact evidence frame restores its highlights")
	panel._select_ply(2)
	panel._show_evidence(evidence, 3, snapshot.job_id, snapshot.moves[2].fen_before)
	verify(panel.selected_ply == 2 and panel.line_kind.is_empty() and panel.board.coach_squares.is_empty(), "old evidence control cannot jump to another selected lesson")
	panel._select_ply(1)
	panel._step_lesson(1)
	verify(panel._walkthrough_active and panel._walkthrough_target == 2, "next lesson uses actual intervening game replay")
	panel._skip_walkthrough()
	panel._select_ply(3)
	var report_before: Dictionary = panel.report.duplicate(true)
	panel._show_phase(true)
	panel._begin_retry()
	verify(panel.retry_active and panel.board.state.fen == snapshot.frames[2].fen, "retry starts at exact original pre-position")
	verify(not panel.board.is_animating(), "retry does not reveal the played solution through reverse animation")
	verify(panel.retry_button.get_index() < panel.coach_evidence.get_index(), "retry entry appears before longer evidence list")
	verify(panel.board.state.legal_moves == snapshot.moves[2].retry_legal_moves, "retry uses only server-generated legal moves")
	verify(panel.board.hint_move.is_empty() and panel.board.coach_squares.is_empty() and not panel.evaluation_bar.has_score, "retry hides answer arrow, annotation and evaluation")
	verify(not panel.move_list.visible and not panel.critical_list.visible and not panel.summary_label.visible and not panel.coach_evidence.visible, "retry hides notation lists, classification summary and explanatory evidence")
	verify(not panel.position_label.text.contains("g4") and not panel.comparison_label.text.contains("e4"), "retry masks played and recommended SAN in headers")
	panel._on_retry_drag("e2", "e5")
	verify(panel._retry_chosen_uci.is_empty(), "illegal retry drag is not submitted")
	await _drag_retry_piece("e2", "e4")
	verify(panel._retry_chosen_uci == "e2e4", "real viewport drag selects a legal retry move")
	verify(panel.board.state.fen == snapshot.frames[2].fen and panel.report == report_before, "attempt never modifies archived board or report")
	var ticket: Dictionary = panel._retry_ticket("e2e4")
	var reply := {"job_id": ticket.job_id, "revision": ticket.revision, "ply": 3, "uci": "e2e4", "san": "e4",
		"verdict": "recommended", "message": "좋아! 상대의 위협을 잘 살펴봤어.", "line": snapshot.moves[2].best.duplicate(true), "best": snapshot.moves[2].best.duplicate(true), "loss_cp": 0}
	reply.best.score_cp_white = 22
	verify(panel._accept_retry_result(reply, ticket), "matching retry response is accepted")
	verify(panel.retry_active and panel.avatar.mood == "celebrate" and panel.board.hint_move.is_empty(), "positive feedback does not automatically reveal solution")
	panel._reveal_retry()
	verify(not panel.retry_active and panel.line_kind == "retry_best" and panel.board.hint_move == "e2e4", "explicit reveal opens fresh recommendation")
	verify(panel._selected_line().score_cp_white == 22 and panel.report == report_before, "fresh retry result remains separate from original report")
	verify(panel.retry_line_button.visible and panel.retry_reveal_button.visible and panel.coach_dialogue.text.contains(reply.message), "fresh analysis retains both line controls and its own explanation")
	panel.retry_line_button.pressed.emit()
	verify(panel.line_kind == "retry_candidate" and panel.retry_reveal_button.visible, "fresh candidate response remains reachable after reveal")
	panel.retry_reveal_button.pressed.emit()
	verify(panel.line_kind == "retry_best" and panel._selected_line().score_cp_white == 22, "fresh recommendation remains reachable after viewing candidate")
	panel._begin_retry()
	ticket = panel._retry_ticket("e2e4")
	var wrong: Dictionary = reply.duplicate(true)
	wrong.job_id = "another-job"
	verify(not panel._accept_retry_result(wrong, ticket), "different-job retry result rejected")
	wrong = reply.duplicate(true)
	wrong.uci = "d2d4"
	verify(not panel._accept_retry_result(wrong, ticket), "different selected move retry result rejected")
	panel._select_ply(1)
	verify(not panel._accept_retry_result(reply, ticket) and panel._retry_reply.is_empty(), "navigation invalidates a late retry result")
	verify(not panel._accept_retry_error({"error": "old failure"}, ticket), "navigation also ignores stale retry failures")
	panel._select_ply(3)
	panel._begin_retry()
	ticket = panel._retry_ticket("e2e4")
	var newer: Dictionary = panel.report.duplicate(true)
	newer.revision = panel.revision + 1
	newer.status = "cancelled"
	verify(panel._accept_retry_error({"error": "revision changed", "data": {"coaching": newer}}, ticket), "retry revision conflict is handled")
	verify(panel.revision == newer.revision and not panel.retry_active and panel._can_retry(), "same-job revision conflict refreshes report and allows a fresh attempt")
	panel._begin_retry()
	ticket = panel._retry_ticket("e2e4")
	newer = panel.report.duplicate(true)
	newer.revision += 1
	newer.job_id = "foreign-report"
	newer.moves[2].san = "foreign-answer"
	panel._accept_retry_error({"error": "job changed", "data": {"coaching": newer}}, ticket)
	verify(not panel.retry_active and not panel._can_retry() and panel.report.job_id == snapshot.job_id and panel.report.moves[2].san == snapshot.moves[2].san, "foreign retry conflict invalidates exercise without attaching foreign report")
	panel._accept_snapshot(snapshot, true)
	panel._select_ply(4)
	panel._begin_retry()
	verify(panel.retry_active and not panel.position_label.text.contains("Qh4") and not panel.comparison_label.text.contains("Qh4") and not panel.move_list.visible, "positive lesson's actually played best move is hidden too")
	panel._show_retry_hint()
	verify(panel.coach_dialogue.text == snapshot.moves[3].coach.hint, "hint appears only when explicitly requested")
	panel._accept_snapshot(fixture.promotion_report, true)
	panel._select_ply(1)
	for kind in ["q", "r", "b", "n"]:
		panel._begin_retry()
		panel._on_retry_square("a7")
		panel._on_retry_square("a8")
		verify(panel.retry_promotion.visible and panel.model.pending_promotions.size() == 4 and panel._retry_chosen_uci.is_empty(), "promotion asks before submitting, candidate " + kind)
		verify(not panel.retry_promotion_buttons[kind].disabled, "legal underpromotion remains available: " + kind)
		panel._retry_promote(kind)
		verify(panel._retry_chosen_uci == "a7a8" + kind, "exact promotion submitted: " + kind)
	var pending: Dictionary = snapshot.duplicate(true)
	pending.status = "running"
	for row in pending.moves:
		row.coach = null
		row.classification = "pending"
		row.retry_legal_moves = []
	panel._accept_snapshot(pending, true, true)
	verify(not panel.retry_active and panel.retry_button.disabled and not panel.coach_dialogue.text.is_empty(), "nullable pending coaching keeps companion usable and exercise disabled")
	verify(not panel._speech_owned and panel.stop_speech_button.disabled, "test fixtures never use real speech output")
	if DisplayServer.get_name() == "headless":
		verify(panel.speak_button.disabled and panel._english_voice.is_empty(), "missing English voice gracefully leaves text coaching available")
	panel._accept_snapshot(snapshot, true)
	panel._select_ply(3)


func _voice_checks(snapshot: Dictionary) -> void:
	var voice := VoicePanel.new()
	voice.offline_mode = true
	root.add_child(voice)
	voice.report = snapshot.duplicate(true)
	voice.revision = int(snapshot.revision)
	voice.show()
	voice._select_ply(1)
	verify(voice._speech_enabled and voice.speak_button.button_pressed and voice.speak_button.text == "음성 켜짐", "voice defaults on with a local English voice")
	verify(is_equal_approx(voice.speech_timer.wait_time, 0.3) and voice.spoken.is_empty(), "speech is delayed 300ms rather than spoken synchronously")
	var first_pending: String = voice._speech_pending_key
	voice._render()
	verify(voice._speech_pending_key == first_pending, "same render preserves one pending utterance")
	voice._select_ply(2)
	voice._select_ply(3)
	await create_timer(0.34).timeout
	verify(voice.spoken.size() == 1 and voice.spoken[0] == snapshot.moves[2].coach.dialogue, "rapid steps automatically read only the final scene")
	voice.report.progress.completed = 2
	voice._render()
	voice.flip_view()
	verify(voice._speech_pending_key.is_empty() and voice.spoken.size() == 1, "progress renders and board flip do not repeat the explanation")
	voice.report.moves[1].coach.dialogue = voice.report.moves[2].coach.dialogue
	voice._select_ply(2)
	voice.flush_voice()
	verify(voice.spoken.size() == 2 and voice.stopped == 1, "different selected move replaces old speech even if wording is identical")
	voice._toggle_speech()
	voice._select_ply(4)
	voice._render()
	verify(not voice._speech_enabled and voice._speech_pending_key.is_empty() and voice.speak_button.text == "음성 꺼짐", "muting persists across navigation and renders")
	voice._toggle_speech()
	voice.flush_voice()
	verify(voice.spoken.size() == 3 and voice.spoken[-1] == snapshot.moves[3].coach.dialogue, "unmuting reads only the current scene")
	voice._toggle_coach_overview()
	voice.flush_voice()
	verify(voice.spoken[-1] == snapshot.coach.summary, "overview speaks the visible whole-game summary")
	voice._select_ply(3)
	voice._show_line("best")
	voice.flush_voice()
	var count: int = voice.spoken.size()
	voice._show_line("played")
	voice.flush_voice()
	verify(voice.spoken.size() == count + 1, "actual versus recommended line change is narrated")
	voice._line_step(1)
	verify(voice._speech_pending_key.is_empty(), "PV stepping with unchanged explanation does not restart the same speech")
	voice._select_ply(1)
	voice.flush_voice()
	count = voice.spoken.size()
	voice._navigate_critical(4)
	voice.walkthrough_timer.stop()
	verify(voice._walkthrough_active and voice._speech_pending_key.is_empty() and not voice._speech_owned, "context replay immediately cancels speech")
	voice._advance_walkthrough()
	voice._pause_walkthrough()
	voice._render()
	verify(voice._speech_pending_key.is_empty(), "paused intermediate replay stays quiet")
	voice._pause_walkthrough()
	voice.walkthrough_timer.stop()
	voice._advance_walkthrough()
	verify(voice._speech_pending_key.is_empty(), "intermediate actual move does not announce transit text")
	voice._advance_walkthrough()
	voice.flush_voice()
	verify(not voice._walkthrough_active and voice.spoken.size() == count + 1 and voice.spoken[-1] == snapshot.moves[3].coach.dialogue, "target arrival reads its real explanation once")
	voice._begin_retry()
	voice.flush_voice()
	verify(voice.retry_active and not voice.spoken[-1].contains("Qh4") and voice.spoken[-1] == voice.coach_dialogue.text, "retry cancels solution speech and reads only the hidden-answer prompt")
	voice._show_retry_hint()
	voice.flush_voice()
	verify(voice.spoken[-1] == snapshot.moves[3].coach.hint, "explicit hint is read after it becomes visible")
	voice._retry_reply = {"uci": "d8h4", "message": "Your move gives checkmate. The king has no legal escape."}
	voice._render()
	voice.flush_voice()
	verify(voice.spoken[-1] == voice._retry_reply.message, "retry feedback is read without adding a hidden best move")
	voice._select_ply(2)
	voice.hide()
	verify(voice._speech_pending_key.is_empty() and voice.speech_timer.is_stopped(), "hiding invalidates queued speech")
	voice.show()
	voice._render()
	voice.flush_voice()
	voice._stop_current_speech()
	voice._render()
	verify(voice._speech_pending_key.is_empty(), "explicit stop is not undone by the next poll render")
	voice._select_ply(1)
	voice._return()
	verify(not voice.visible and voice._speech_pending_key.is_empty(), "return clears pending speech")
	voice.show()
	voice._select_ply(3)
	voice.flush_voice()
	var stopped: int = voice.stopped
	root.remove_child(voice)
	verify(voice.stopped == stopped + 1 and voice._speech_pending_key.is_empty(), "closing the panel stops its owned speech")
	voice.free()


func _windows_voice_checks(snapshot: Dictionary) -> void:
	var voice := DesktopVoicePanel.new()
	voice.offline_mode = true
	root.add_child(voice)
	voice.report = snapshot.duplicate(true)
	voice.show()
	voice._select_ply(1)
	var backend: MockWindowsSpeech = voice._windows_speech
	verify(backend.queries == 1 and voice._voice_loading and voice.speak_button.text == "음성 준비 중", "Desktop fallback distinguishes asynchronous voice discovery from no installed voice")
	backend.voices_ready.emit([{"id": "Desktop English", "language": "en-US"}], "")
	verify(not voice._voice_loading and voice._english_voice == "Desktop English" and not voice._speech_pending_key.is_empty(), "late Desktop voice discovery automatically schedules the current scene")
	voice.flush_voice()
	var old_ticket: int = int(backend.requests[-1].ticket)
	verify(voice._speech_preparing_audio and voice._speech_owned and voice.played == 0, "WAV generation is asynchronous and cannot claim playback yet")
	voice._poll_speech()
	verify(voice._speech_owned and backend.cancellations == 0, "polling does not cancel a WAV still being synthesized")
	voice._select_ply(3)
	voice.flush_voice()
	var current_ticket: int = int(backend.requests[-1].ticket)
	verify(current_ticket != old_ticket and backend.cancellations > 0, "new scene invalidates and cancels the previous synthesis")
	var wav := AudioStreamWAV.new()
	wav.data = PackedByteArray([1, 0, 2, 0, 3, 0, 4, 0])
	backend.audio_ready.emit(old_ticket, wav)
	backend.failed.emit(old_ticket, "old failure")
	verify(voice.played == 0 and voice._speech_error.is_empty(), "stale WAV and failure cannot affect the newer scene")
	backend.audio_ready.emit(current_ticket, wav)
	verify(voice.played == 1 and voice.playing and not voice._speech_preparing_audio, "only the current WAV reaches the game audio player")
	backend.status_changed.emit("sapi", "기본 Windows 음성으로 전환했습니다.")
	verify(voice.voice_status_label.text.contains("기본 Windows") and voice._speech_error.is_empty(), "normal SAPI fallback is visible without being treated as a playback failure")
	voice._poll_speech()
	verify(voice._speech_owned, "native polling observes current WAV playback")
	voice._select_ply(2)
	voice.flush_voice()
	current_ticket = int(backend.requests[-1].ticket)
	backend.failed.emit(current_ticket, "음성 합성에 실패했습니다.")
	verify(not voice._speech_owned and voice.speak_button.text == "음성 다시 시도" and not voice.speak_button.disabled, "synthesis errors expose retry rather than incorrectly claiming no voice")
	voice._render()
	verify(voice._speech_pending_key.is_empty(), "a failed synthesis does not loop on every render")
	voice._toggle_speech()
	voice.flush_voice()
	verify(voice._speech_error.is_empty() and voice._speech_preparing_audio, "explicit retry resynthesizes the current scene")
	current_ticket = int(backend.requests[-1].ticket)
	voice._toggle_speech()
	backend.audio_ready.emit(current_ticket, wav)
	verify(not voice._speech_enabled and voice.played == 1, "muting drops an in-flight WAV")
	voice._toggle_speech()
	voice.flush_voice()
	current_ticket = int(backend.requests[-1].ticket)
	voice.hide()
	backend.audio_ready.emit(current_ticket, wav)
	verify(voice.played == 1 and not voice._speech_owned, "a hidden panel cannot play a late WAV")
	root.remove_child(voice)
	voice.free()


func _perspective_voice_checks(snapshot: Dictionary) -> void:
	var voice := VoicePanel.new()
	voice.offline_mode = true
	root.add_child(voice)
	voice.report = snapshot.duplicate(true)
	voice.report.moves[2].coach.perspectives = {
		"white": {"dialogue": "Your pawn move leaves your king exposed.", "hint": "Check your king's diagonal."},
		"black": {"dialogue": "White exposes their king. You can give checkmate.", "hint": "Look for checks against White's king."}}
	var unchanged: Dictionary = voice.report.duplicate(true)
	voice.show()
	voice._select_ply(3)
	voice.flush_voice()
	verify(voice.coach_dialogue.text.begins_with("Your pawn") and voice.spoken[-1] == voice.coach_dialogue.text, "white perspective is used for visible and spoken coaching")
	voice._change_side(1)
	voice.flush_voice()
	verify(voice.coach_dialogue.text.begins_with("White exposes") and voice.spoken[-1] == voice.coach_dialogue.text, "switching to black narrates the opponent's move from the correct perspective")
	verify(voice.report == unchanged, "perspective selection never mutates saved coaching metadata")
	voice._begin_retry()
	voice.flush_voice()
	verify(not voice.spoken[-1].contains("checkmate"), "unsolved retry still hides the perspective's solution")
	voice._show_retry_hint()
	voice.flush_voice()
	verify(voice.spoken[-1] == "Look for checks against White's king.", "revealed retry hint uses the selected side")
	voice._select_ply(2)
	voice.flush_voice()
	verify(voice.spoken[-1] == snapshot.moves[1].coach.dialogue, "older reports without perspectives keep their original commentary")
	voice.report.moves[1].coach.perspectives = {"black": null}
	voice._render()
	verify(voice.coach_dialogue.text == snapshot.moves[1].coach.dialogue, "nullable perspective metadata falls back safely")
	voice.hide()
	root.remove_child(voice)
	voice.free()


func _visible_coaching_text(node: Node) -> String:
	if node is Control and not node.is_visible_in_tree():
		return ""
	var parts: Array[String] = []
	if node is Label or node is RichTextLabel or node is Button:
		parts.append(str(node.text))
	if node is ItemList:
		for index in range(node.item_count):
			parts.append(node.get_item_text(index))
	for child in node.get_children():
		parts.append(_visible_coaching_text(child))
	return "\n".join(parts)


func _teaching_voice_checks(snapshot: Dictionary) -> void:
	var voice := VoicePanel.new()
	voice.offline_mode = true
	root.add_child(voice)
	voice.report = snapshot.duplicate(true)
	var principle: String = "Develop pieces before launching a flank attack."
	var reason: String = "opens a diagonal for the bishop"
	var teaching := {"concept": "development", "alternative": {"uci": "e2e4", "san": "e4", "reason": reason},
		"principle": principle, "line": "best", "ply_index": 1, "grounding": "verified_move_effect"}
	var white_words: String = "Try e4: it " + reason + ". " + principle
	var black_words: String = "White could play e4: it " + reason + ". " + principle
	voice.report.moves[2].coach.merge({"dialogue": white_words, "teaching": teaching,
		"hint": "Check your king's safety before choosing a move.",
		"perspectives": {
			"white": {"dialogue": white_words, "hint": "Check your king's safety before choosing a move.", "teaching": teaching.duplicate(true)},
			"black": {"dialogue": black_words, "hint": "Look for checks before choosing a move.", "teaching": teaching.duplicate(true)}}}, true)
	var original: Dictionary = voice.report.duplicate(true)
	voice.show()
	voice._select_ply(3)
	voice.flush_voice()
	verify(voice.coach_dialogue.text == white_words and voice.spoken[-1] == white_words, "normal white coaching displays and automatically reads alternative, reason and chess principle together")
	voice._change_side(1)
	voice.flush_voice()
	verify(voice.coach_dialogue.text == black_words and voice.spoken[-1] == black_words, "normal black perspective reads the same lesson without calling the opponent's move yours")
	voice._change_side(0)
	var count: int = voice.spoken.size()
	voice._begin_retry()
	voice.flush_voice()
	verify(voice.retry_active and voice.spoken.size() == count + 1 and not voice.spoken[-1].contains("e4") and not voice.spoken[-1].contains(principle), "entering retry replaces pending full teaching speech with the hidden-answer prompt")
	for phase in ["prompt", "hint", "feedback"]:
		if phase == "hint":
			voice._show_retry_hint()
		elif phase == "feedback":
			voice._retry_reply = {"uci": "g2g4", "message": "Check your king's safety before trying again.", "best": voice.report.moves[2].best, "teaching": teaching.duplicate(true)}
			voice._render()
		voice.flush_voice()
		var screen: String = _visible_coaching_text(voice)
		verify(not screen.contains("e4") and not screen.contains(reason) and not screen.contains(principle) and not screen.contains("verified_move_effect"), "hidden retry " + phase + " does not expose nested teaching metadata, recommendation or principle on screen")
		verify(not voice.spoken[-1].contains("e4") and not voice.spoken[-1].contains(reason) and not voice.spoken[-1].contains(principle), "hidden retry " + phase + " does not speak the stored solution lesson")
		voice._render()
		verify(voice._speech_pending_key.is_empty(), "poll render during retry " + phase + " cannot reschedule hidden teaching")
	voice._end_retry()
	voice._select_ply(3)
	voice.flush_voice()
	verify(voice.spoken[-1] == white_words and voice.coach_dialogue.text == white_words, "return to normal review restores the complete teaching dialogue")
	verify(voice.report == original, "teaching and both perspective records remain unchanged by retry or speech")
	voice.hide()
	root.remove_child(voice)
	voice.free()


func _badge_checks(snapshot: Dictionary) -> void:
	panel._accept_snapshot(snapshot, true)
	panel._select_ply(3)
	verify(panel.show_after and panel.board.move_annotation.get("symbol") == "??", "selected actual blunder defaults to after-frame badge")
	panel._show_phase(false)
	verify(panel.board.move_annotation.is_empty() and not panel.annotation_label.visible, "before view hides badge instead of grading the previous piece")
	panel._first()
	verify(panel.board.state.fen == snapshot.frames[0].fen and panel.previous_button.disabled and not panel.next_button.disabled, "first control reaches initial ungraded position")
	panel._step(1)
	verify(panel.selected_ply == 1 and panel.show_after and panel.board.state.fen == snapshot.frames[1].fen, "next from initial applies the first actual move")
	panel._step(-1)
	verify(panel.board.state.fen == snapshot.frames[0].fen, "previous after first move returns to initial position")
	var labels := {"best": "★", "good": "✓", "inaccuracy": "?!", "mistake": "?", "blunder": "??", "allows_mate": "??", "missed_mate": "?", "uncertain": "…"}
	for kind in labels:
		var modified: Dictionary = snapshot.duplicate(true)
		modified.moves[2].classification = kind
		modified.moves[2].erase("annotation")
		panel._accept_snapshot(modified, true)
		panel._select_ply(3)
		verify(panel.board.move_annotation.get("symbol") == labels[kind], "safe legacy classification badge: " + kind)
	var great: Dictionary = snapshot.duplicate(true)
	var row: Dictionary = great.moves[3]
	row.classification = "best"
	row.annotation = {"symbol": "!!", "kind": "great", "label": "탁월 후보", "square": "h4", "scope": "actual_move", "provisional": true,
		"ply": 4, "uci": row.uci, "fen_after": row.fen_after, "reason": "제한 시간의 대안 비교에 따른 후보입니다."}
	panel._accept_snapshot(great, true)
	panel._select_ply(4)
	verify(panel.board.move_annotation.get("symbol") == "!!" and panel.annotation_label.text.contains("제한 시간"), "explicit verified great metadata gets candidate label and caveat")
	for field in ["fen_after", "uci", "square", "scope", "ply"]:
		var invalid: Dictionary = great.duplicate(true)
		invalid.moves[3].annotation[field] = 1 if field == "ply" else "wrong"
		panel._accept_snapshot(invalid, true)
		verify(panel.board.move_annotation.is_empty(), "mismatched annotation rejected: " + field)
	var pending: Dictionary = great.duplicate(true)
	pending.moves[3].classification = "pending"
	panel._accept_snapshot(pending, true)
	verify(panel.board.move_annotation.is_empty(), "pending row cannot display stale great metadata")
	for example in fixture.badge_cases:
		var special: Dictionary = example.report
		panel._accept_snapshot(special, true)
		panel._select_ply(1)
		var area: Rect2 = panel.board.get_board_rect()
		verify(panel.board.move_annotation.get("symbol") == "★" and panel.board.move_annotation.get("square") == str(special.moves[0].uci).substr(2, 2), example.name + " badge follows actual arriving piece")
		panel.board.state = special.frames[0]
		panel.board.finish_animation()
		panel._select_ply(1)
		verify(panel.board.is_animating() and panel.board.move_badge_rect().size == Vector2.ZERO, example.name + " badge waits for movement to finish")
		panel.board._process(1.0)
		for white_view in [true, false]:
			panel.model.view_from_white = white_view
			panel._render()
			panel.board.finish_animation()
			var badge: Rect2 = panel.board.move_badge_rect()
			verify(badge.size.x >= 20 and area.encloses(badge), example.name + " badge fits board boundary with view " + str(white_view))
			verify(panel.board.square_at_point(badge.get_center()) == str(special.moves[0].uci).substr(2, 2), example.name + " passive badge shares exact flipped click mapping")
			verify(panel.board.get_board_rect() == area, example.name + " badge does not change board geometry")
		panel.board.animation_enabled = false
		panel.board.state = special.frames[0]
		panel._select_ply(1)
		verify(not panel.board.is_animating() and panel.board.move_badge_rect().size.x > 0, example.name + " instant-motion setting shows badge immediately")
		panel.board.animation_enabled = true
		panel._show_line("played")
		panel._line_step(1)
		verify(panel.board.state.fen == special.frames[1].fen and panel.board.move_annotation.is_empty(), example.name + " even matching played PV frame does not reuse actual badge")
		panel._begin_retry()
		verify(panel.retry_active and panel.board.move_annotation.is_empty() and not panel.annotation_legend.visible, example.name + " exercise hides badges and grading legend")
	panel.model.view_from_white = true
	panel._accept_snapshot(snapshot, true)
	panel._select_ply(3)


func _live() -> void:
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "backend state loads"):
		return
	var original: Dictionary = app.state.duplicate(true)
	verify(original.result == "0-1", "fixture current game is finished")
	verify(not app.coaching_entry_button.disabled, "real completed game enables coaching")
	app.coaching_entry_button.pressed.emit()
	if not await wait_for(func(): return panel.visible and not panel.busy and panel._ready_to_start, "coaching setup validates live PGN"):
		return
	verify(panel._moves().size() == 4 and panel.report.is_empty(), "opening coaching does not auto-start expensive analysis")
	panel.time_option.select(0)
	panel.start_button.pressed.emit()
	if not await wait_for(func(): return panel.report.get("status") == "running" and not panel.busy, "analysis starts asynchronously"):
		return
	panel._select_ply(3)
	panel._show_phase(true)
	verify(panel.board.state.fen == fixture.preview.frames[3].fen, "pending report is already browsable")
	var saw_partial: bool = false
	var deadline: int = Time.get_ticks_msec() + 25000
	while panel.report.get("status") == "running" and Time.get_ticks_msec() < deadline:
		var completed: int = int(panel.report.get("progress", {}).get("completed", 0))
		if completed > 0 and completed < 4:
			saw_partial = true
		await create_timer(0.05).timeout
	verify(panel.report.get("status") == "completed", "own-engine full game analysis completes")
	verify(saw_partial, "partial progress visible before complete")
	verify(panel.selected_ply == 3 and panel.show_after, "poll preserves user replay choice")
	verify(panel.report.moves[2].best is Dictionary, "real best move available")
	verify(panel.board.move_annotation.get("symbol") == "??" and panel.board.move_annotation.get("square") == "g4", "real engine mistake badge is tied to actual g4 position")
	panel._show_line("best")
	var chosen: Dictionary = panel._selected_line()
	verify(panel.board.state.fen == panel.report.moves[2].fen_before, "real PV starts at selected exact preposition")
	for index in range(1, chosen.pv_frames.size()):
		panel._line_step(1)
		verify(panel.board.state.fen == chosen.pv_frames[index].fen, "real engine PV frame %d" % index)
	panel._leave_line()
	panel._show_line("played")
	panel._line_step(1)
	verify(panel.board.state.fen == panel.report.moves[2].fen_after, "played first PV frame matches actual move")
	panel._leave_line()
	panel._begin_retry()
	verify(panel.retry_active, "real analyzed lesson exposes interactive retry")
	var original_report: Dictionary = panel.report.duplicate(true)
	panel._on_retry_drag("g2", "g4")
	if await wait_for(func(): return not panel._retry_in_flight and not panel._retry_reply.is_empty(), "real retry returns own-engine feedback", 15000):
		verify(panel._retry_reply.uci == "g2g4" and panel._retry_reply.line.pv_frames[0].fen == panel.report.moves[2].fen_before, "real retry feedback is tied to exact chosen source position")
		verify(panel.report == original_report and app.state == original, "real retry preserves saved report and live game")
		panel._reveal_retry()
		verify(panel.line_kind == "retry_best" and not panel.board.hint_move.is_empty(), "real retry reveal shows fresh engine variation")
	panel._begin_retry()
	panel._on_retry_drag("e2", "e4")
	panel._select_ply(1)
	await wait_for(func(): return not panel._retry_in_flight, "late retry finishes after navigation", 15000)
	verify(panel.selected_ply == 1 and panel._retry_reply.is_empty() and not panel.retry_active, "late real retry cannot overwrite newly selected lesson")
	verify(app.state == original, "live app state unchanged by coaching")
	await panel._return()
	verify(app.main_contents.visible and not panel.visible, "return from complete restores live scene")
	var detail: Dictionary = await app._send_request("/api/games/" + str(original.game_id))
	verify(detail.get("ok", false) and app._adopt_review(detail.get("data", {})), "saved library game opens")
	app._render()
	app.coaching_entry_button.pressed.emit()
	if not await wait_for(func(): return panel.visible and not panel.busy and panel._ready_to_start, "archived coaching setup"):
		return
	panel.time_option.select(2)
	panel.start_button.pressed.emit()
	if not await wait_for(func(): return panel.report.get("status") == "running" and not panel.busy, "deep analysis starts"):
		return
	panel.cancel_button.pressed.emit()
	await wait_for(func(): return panel.report.get("status") == "cancelled" and not panel.busy, "cancel stops own-engine work", 6000)
	await panel._return()
	verify(app.review_mode and app.main_contents.visible, "cancel/return preserves archived review")
	verify(app.state == original, "archive coaching preserves current live game")


func _run() -> void:
	var args: PackedStringArray = OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--fixture":
			fixture = JSON.parse_string(FileAccess.get_file_as_string(args[index + 1]))
		elif args[index] == "--evidence-dir":
			output = args[index + 1]
	live = args.has("--backend-file")
	app = load("res://main.tscn").instantiate()
	app.offline_mode = not live
	root.add_child(app)
	panel = app.coaching_overlay
	# All real audio stays disabled in fixtures, even if this suite is rendered.
	panel._speech_enabled = false
	await settle()
	if live:
		await _live()
	else:
		await _synthetic()
	if not output.is_empty():
		var report := {"checks": checks, "failures": failures, "mode": "headless HTTP scene" if live else "headless fixture scene", "layouts": geometries, "library_layouts": library_geometries}
		var file := FileAccess.open(output.path_join("coaching-native-" + ("live" if live else "synthetic") + ".json"), FileAccess.WRITE)
		file.store_string(JSON.stringify(report, "  "))
		file.close()
	print("Coaching native ", "live" if live else "synthetic", ": ", checks, " checks, ", failures, " failures")
	app.queue_free()
	await process_frame
	quit(1 if failures else 0)
