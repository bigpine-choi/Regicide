extends Control
## Read-only coaching presentation. The host validates/replays every position.

signal returned
signal replay_frame_shown(ply: int)
signal retry_submitted(uci: String)

const ChessBoard = preload("res://scripts/chess_board.gd")
const EvaluationBar = preload("res://scripts/evaluation_bar.gd")
const ViewModel = preload("res://scripts/board_view_model.gd")
const CoachAvatar = preload("res://scripts/coach_avatar.gd")
const WindowsSpeech = preload("res://scripts/windows_speech.gd")

var backend_url: String = ""
var backend_token: String = ""
var offline_mode: bool = false
var source_pgn: String = ""
var human_color: String = "white"
var report: Dictionary = {}
var revision: int = 0
var selected_ply: int = 1
var show_after: bool = true
var line_kind: String = ""
var line_index: int = 0
var busy: bool = false
var _error: String = ""
var _epoch: int = 0
var _mutation_epoch: int = 0
var _polling: bool = false
var _ready_to_start: bool = false
var _preview: Dictionary = {}
var _list_key: String = ""
var _critical_key: String = ""
var _local_selection: bool = false
var _job_replaced: bool = false
var return_warning: String = ""
var _unconfirmed_job: String = ""
var _unconfirmed_pgn: String = ""
var _walkthrough_active: bool = false
var _walkthrough_paused: bool = false
var _walkthrough_cursor: int = 0
var _walkthrough_from: int = 0
var _walkthrough_target: int = 0
var _walkthrough_job: String = ""
var _coach_overview: bool = false
var _coach_evidence_key: String = ""
var _coach_focus_fen: String = ""
var _coach_focus_line: String = ""
var _coach_focus_index: int = -1
var _coach_focus: Array = []
var retry_active: bool = false
var _retry_in_flight: bool = false
var _retry_epoch: int = 0
var _retry_frame: Dictionary = {}
var _retry_reply: Dictionary = {}
var _retry_status: String = ""
var _retry_hint: bool = false
var _retry_chosen_uci: String = ""
var _english_voice: String = ""
var _speech_owned: bool = false
var _speech_text: String = ""
var _speech_enabled: bool = true
var _speech_pending_key: String = ""
var _speech_last_key: String = ""
var speech_timer: Timer
var _speech_backend: String = "native"
var _voice_loading: bool = false
var _speech_error: String = ""
var _speech_generation: int = 0
var _speech_preparing_audio: bool = false
var _windows_speech: WindowsSpeech
var _speech_player: AudioStreamPlayer
var voice_status_label: Label

var model: ViewModel = ViewModel.new()
var board: ChessBoard
var evaluation_bar: EvaluationBar
var position_label: Label
var evaluation_label: Label
var status_label: Label
var game_label: Label
var progress_label: Label
var progress_bar: ProgressBar
var error_label: Label
var summary_label: Label
var estimate_label: Label
var explanation_label: Label
var comparison_label: Label
var line_label: Label
var notice_label: Label
var time_option: OptionButton
var side_option: OptionButton
var focus_option: OptionButton
var start_button: Button
var cancel_button: Button
var return_button: Button
var before_button: Button
var after_button: Button
var best_button: Button
var played_button: Button
var line_previous: Button
var line_next: Button
var line_return: Button
var first_button: Button
var previous_button: Button
var next_button: Button
var last_button: Button
var critical_previous: Button
var critical_next: Button
var move_list: ItemList
var critical_list: ItemList
var walkthrough_label: Label
var walkthrough_pause: Button
var walkthrough_skip: Button
var walkthrough_timer: Timer
var avatar: CoachAvatar
var coach_dialogue: Label
var coach_course: Label
var coach_evidence: VBoxContainer
var coach_overview_button: Button
var lesson_previous: Button
var lesson_next: Button
var retry_button: Button
var retry_hint_button: Button
var retry_reveal_button: Button
var retry_again_button: Button
var retry_feedback: Label
var retry_line_button: Button
var retry_promotion: HBoxContainer
var retry_promotion_buttons: Dictionary = {}
var retry_requester: HTTPRequest
var annotation_label: Label
var annotation_legend: Label
var speak_button: Button
var stop_speech_button: Button
var requester: HTTPRequest
var poll_requester: HTTPRequest
var poll_timer: Timer


func _ready() -> void:
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_build_ui()
	_setup_voice()
	hide()


func _exit_tree() -> void:
	_stop_speech()


func _notification(what: int) -> void:
	if what == NOTIFICATION_VISIBILITY_CHANGED and not is_visible_in_tree():
		_stop_speech()


func _label(text: String, size: int = 14, color: String = "c0cddb") -> Label:
	var label := Label.new()
	label.text = text
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.add_theme_font_size_override("font_size", size)
	label.add_theme_color_override("font_color", Color(color))
	return label


func _button(text: String, action: Callable, node_name: String) -> Button:
	var button := Button.new()
	button.text = text
	button.name = node_name
	button.pressed.connect(action)
	button.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	return button


func _single_line(label: Label) -> void:
	label.autowrap_mode = TextServer.AUTOWRAP_OFF
	label.clip_text = true
	label.max_lines_visible = 1
	label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER


func _build_ui() -> void:
	var background := ColorRect.new()
	background.color = Color("0c1422")
	background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	add_child(background)
	var margin := MarginContainer.new()
	margin.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	for side in ["left", "right", "top", "bottom"]:
		margin.add_theme_constant_override("margin_" + side, 20)
	add_child(margin)
	var page := VBoxContainer.new()
	page.add_theme_constant_override("separation", 10)
	margin.add_child(page)
	var header := HBoxContainer.new()
	page.add_child(header)
	var title := _label("REGICIDE  ·  대국 코칭", 25, "f6f0de")
	_single_line(title)
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(title)
	return_button = _button("돌아가기", _return, "CoachingReturn")
	header.add_child(return_button)
	var body := HBoxContainer.new()
	body.name = "CoachingBody"
	body.size_flags_vertical = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 16)
	page.add_child(body)
	var board_column := VBoxContainer.new()
	board_column.name = "CoachingBoardColumn"
	board_column.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	board_column.add_theme_constant_override("separation", 8)
	body.add_child(board_column)
	var toolbar := HBoxContainer.new()
	toolbar.custom_minimum_size.y = 36
	board_column.add_child(toolbar)
	position_label = _label("기보 준비", 16, "9bd7ca")
	position_label.name = "CoachingPosition"
	_single_line(position_label)
	position_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	toolbar.add_child(position_label)
	toolbar.add_child(_button("시점 뒤집기 ⇅", flip_view, "CoachingFlip"))
	evaluation_label = _label("형세 분석 대기", 17, "f6f0de")
	_single_line(evaluation_label)
	evaluation_label.custom_minimum_size.y = 26
	board_column.add_child(evaluation_label)
	var board_row := HBoxContainer.new()
	board_row.size_flags_vertical = Control.SIZE_EXPAND_FILL
	board_row.add_theme_constant_override("separation", 0)
	board_column.add_child(board_row)
	var gutter := Control.new()
	gutter.custom_minimum_size.x = 40
	board_row.add_child(gutter)
	board = ChessBoard.new()
	board.name = "CoachingBoard"
	board.model = model
	board_row.add_child(board)
	board.mouse_filter = Control.MOUSE_FILTER_IGNORE
	board.square_pressed.connect(_on_retry_square)
	board.drag_requested.connect(_on_retry_drag)
	evaluation_bar = EvaluationBar.new()
	evaluation_bar.board = board
	board.add_child(evaluation_bar)
	evaluation_bar.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	board.resized.connect(evaluation_bar.queue_redraw)
	var navigation := HBoxContainer.new()
	board_column.add_child(navigation)
	first_button = _button("처음", _first, "CoachingFirst")
	previous_button = _button("← 이전 수", _step.bind(-1), "CoachingPrevious")
	next_button = _button("다음 수 →", _step.bind(1), "CoachingNext")
	last_button = _button("끝", _last, "CoachingLast")
	for button in [first_button, previous_button, next_button, last_button]:
		button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		navigation.add_child(button)
	var panel := PanelContainer.new()
	panel.name = "CoachingSidebar"
	panel.custom_minimum_size.x = 380
	panel.size_flags_horizontal = Control.SIZE_SHRINK_END
	body.add_child(panel)
	var inset := MarginContainer.new()
	for side in ["left", "right", "top", "bottom"]:
		inset.add_theme_constant_override("margin_" + side, 12)
	panel.add_child(inset)
	var scroll := ScrollContainer.new()
	scroll.name = "CoachingSidebarScroll"
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_AUTO
	inset.add_child(scroll)
	var sidebar := VBoxContainer.new()
	sidebar.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	sidebar.add_theme_constant_override("separation", 9)
	scroll.add_child(sidebar)
	status_label = _label("한 판을 돌아보며 배우기", 20, "f6f0de")
	sidebar.add_child(status_label)
	game_label = _label("", 13, "9bd7ca")
	sidebar.add_child(game_label)
	error_label = _label("", 13, "ffb4a1")
	error_label.name = "CoachingError"
	sidebar.add_child(error_label)
	annotation_label = _label("", 14, "f4e9c9")
	annotation_label.name = "CoachingMoveAnnotation"
	sidebar.add_child(annotation_label)
	_build_coach(sidebar)
	annotation_legend = _label("기물 위 배지 · ★ 추천  ✓ 좋은 수  ?! 아쉬움  ? 실수  ?? 큰 실수  … 판단 보류\n!! 탁월 후보: 제한 시간 안에서 다른 대안까지 비교한 결과입니다. 배지는 더 깊은 분석에서 달라질 수 있습니다.", 12, "8197aa")
	sidebar.add_child(annotation_legend)
	side_option = OptionButton.new()
	side_option.name = "CoachingHumanColor"
	side_option.add_item("내가 둔 색 · 백", 0)
	side_option.add_item("내가 둔 색 · 흑", 1)
	side_option.item_selected.connect(_change_side)
	sidebar.add_child(side_option)
	time_option = OptionButton.new()
	time_option.name = "CoachingTime"
	time_option.add_item("빠르게 훑기 · 탐색당 0.5초", 500)
	time_option.add_item("자세히 분석 · 탐색당 1.5초", 1500)
	time_option.add_item("깊게 분석 · 탐색당 5초", 5000)
	time_option.select(1)
	time_option.item_selected.connect(func(_index: int): _render())
	sidebar.add_child(time_option)
	estimate_label = _label("", 12, "8197aa")
	sidebar.add_child(estimate_label)
	var actions := HBoxContainer.new()
	sidebar.add_child(actions)
	start_button = _button("코칭 분석 시작", _start, "CoachingStart")
	start_button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	actions.add_child(start_button)
	cancel_button = _button("분석 중단", _cancel, "CoachingCancel")
	actions.add_child(cancel_button)
	progress_bar = ProgressBar.new()
	progress_bar.custom_minimum_size.y = 8
	progress_bar.show_percentage = false
	sidebar.add_child(progress_bar)
	progress_label = _label("", 12, "9bd7ca")
	sidebar.add_child(progress_label)
	summary_label = _label("", 14, "f6f0de")
	sidebar.add_child(summary_label)
	focus_option = OptionButton.new()
	focus_option.name = "CoachingFocus"
	focus_option.add_item("중요 국면 · 양쪽 수 모두", 0)
	focus_option.add_item("중요 국면 · 내 수", 1)
	focus_option.add_item("중요 국면 · 상대 수", 2)
	focus_option.select(0)
	focus_option.item_selected.connect(_change_focus)
	sidebar.add_child(focus_option)
	var critical_navigation := HBoxContainer.new()
	sidebar.add_child(critical_navigation)
	critical_previous = _button("← 이전 실수", _critical_step.bind(-1), "CoachingPreviousCritical")
	critical_next = _button("다음 실수 →", _critical_step.bind(1), "CoachingNextCritical")
	critical_navigation.add_child(critical_previous)
	critical_navigation.add_child(critical_next)
	walkthrough_label = _label("", 13, "9bd7ca")
	walkthrough_label.name = "CoachingWalkthrough"
	sidebar.add_child(walkthrough_label)
	var walkthrough_actions := HBoxContainer.new()
	sidebar.add_child(walkthrough_actions)
	walkthrough_pause = _button("일시정지", _pause_walkthrough, "CoachingWalkthroughPause")
	walkthrough_skip = _button("바로 이동", _skip_walkthrough, "CoachingWalkthroughSkip")
	walkthrough_actions.add_child(walkthrough_pause)
	walkthrough_actions.add_child(walkthrough_skip)
	critical_list = ItemList.new()
	critical_list.name = "CoachingCriticalList"
	critical_list.custom_minimum_size.y = 112
	critical_list.item_selected.connect(_select_critical)
	sidebar.add_child(critical_list)
	comparison_label = _label("분석이 끝난 수부터 비교할 수 있습니다.", 17, "f6f0de")
	comparison_label.name = "CoachingComparison"
	sidebar.add_child(comparison_label)
	explanation_label = _label("", 14, "c0cddb")
	explanation_label.name = "CoachingExplanation"
	sidebar.add_child(explanation_label)
	var phases := HBoxContainer.new()
	sidebar.add_child(phases)
	before_button = _button("두기 전 보기", _show_phase.bind(false), "CoachingBefore")
	after_button = _button("둔 뒤 보기", _show_phase.bind(true), "CoachingAfter")
	phases.add_child(before_button)
	phases.add_child(after_button)
	var lines := HBoxContainer.new()
	sidebar.add_child(lines)
	best_button = _button("최선 수순 보기", _show_line.bind("best"), "CoachingBestLine")
	played_button = _button("실전 수의 응수", _show_line.bind("played"), "CoachingPlayedLine")
	lines.add_child(best_button)
	lines.add_child(played_button)
	line_label = _label("", 13, "9bd7ca")
	line_label.name = "CoachingLine"
	sidebar.add_child(line_label)
	var line_navigation := HBoxContainer.new()
	sidebar.add_child(line_navigation)
	line_previous = _button("수순 ←", _line_step.bind(-1), "CoachingLinePrevious")
	line_next = _button("수순 →", _line_step.bind(1), "CoachingLineNext")
	line_return = _button("실제 기보로", _leave_line, "CoachingLineReturn")
	for button in [line_previous, line_next, line_return]:
		line_navigation.add_child(button)
	sidebar.add_child(_label("전체 기보 · 수를 선택해 비교", 14, "f6f0de"))
	move_list = ItemList.new()
	move_list.name = "CoachingMoveList"
	move_list.custom_minimum_size.y = 190
	move_list.item_selected.connect(func(index: int): _select_ply(index + 1))
	sidebar.add_child(move_list)
	notice_label = _label("Regicide가 선택한 시간 안에 찾은 수를 비교합니다. 더 깊이 분석하면 추천과 실수 분류가 달라질 수 있습니다. +는 백, −는 흑 우세이며 1.00은 폰 한 개에 해당하는 평가 단위입니다.", 12, "8197aa")
	sidebar.add_child(notice_label)
	requester = HTTPRequest.new()
	requester.timeout = 15.0
	add_child(requester)
	retry_requester = HTTPRequest.new()
	retry_requester.timeout = 20.0
	add_child(retry_requester)
	poll_requester = HTTPRequest.new()
	poll_requester.timeout = 8.0
	add_child(poll_requester)
	poll_timer = Timer.new()
	poll_timer.wait_time = 0.5
	poll_timer.timeout.connect(_poll)
	poll_timer.timeout.connect(_poll_speech)
	add_child(poll_timer)
	poll_timer.start()
	walkthrough_timer = Timer.new()
	walkthrough_timer.wait_time = 0.65
	walkthrough_timer.timeout.connect(_advance_walkthrough)
	add_child(walkthrough_timer)
	speech_timer = Timer.new()
	speech_timer.one_shot = true
	speech_timer.wait_time = 0.3
	speech_timer.timeout.connect(_speak_coach)
	add_child(speech_timer)


func _build_coach(sidebar: VBoxContainer) -> void:
	var card := PanelContainer.new()
	card.name = "CoachingCharacterCard"
	var style := StyleBoxFlat.new()
	style.bg_color = Color("19333f")
	style.border_color = Color("3e6c73")
	style.set_border_width_all(1)
	style.set_corner_radius_all(12)
	style.content_margin_left = 10
	style.content_margin_right = 10
	style.content_margin_top = 10
	style.content_margin_bottom = 10
	card.add_theme_stylebox_override("panel", style)
	sidebar.add_child(card)
	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 8)
	card.add_child(column)
	var character_row := HBoxContainer.new()
	character_row.add_theme_constant_override("separation", 10)
	column.add_child(character_row)
	avatar = CoachAvatar.new()
	avatar.name = "CoachRook"
	character_row.add_child(avatar)
	var speech := VBoxContainer.new()
	speech.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	character_row.add_child(speech)
	speech.add_child(_label("루크  ·  나의 체스 코치", 15, "f4e9c9"))
	coach_dialogue = _label("Choose a game to review its key moments.", 14, "e5edf3")
	coach_dialogue.name = "CoachDialogue"
	speech.add_child(coach_dialogue)
	coach_overview_button = _button("한 판 총평", _toggle_coach_overview, "CoachOverview")
	column.add_child(coach_overview_button)
	var speech_controls := HBoxContainer.new()
	column.add_child(speech_controls)
	speak_button = _button("음성 켜짐", _toggle_speech, "CoachSpeak")
	speak_button.toggle_mode = true
	stop_speech_button = _button("음성 정지", _stop_current_speech, "CoachStopSpeaking")
	stop_speech_button.disabled = true
	speech_controls.add_child(speak_button)
	speech_controls.add_child(stop_speech_button)
	voice_status_label = _label("", 12, "9bd7ca")
	voice_status_label.name = "CoachVoiceStatus"
	column.add_child(voice_status_label)
	coach_course = _label("분석을 마치면 복습 코스를 준비할게.", 13, "9bd7ca")
	column.add_child(coach_course)
	var lessons := HBoxContainer.new()
	column.add_child(lessons)
	lesson_previous = _button("← 이전 배움", _step_lesson.bind(-1), "CoachPreviousLesson")
	lesson_next = _button("다음 배움 →", _step_lesson.bind(1), "CoachNextLesson")
	lessons.add_child(lesson_previous)
	lessons.add_child(lesson_next)
	retry_button = _button("이 국면에서 다시 두기", _begin_retry, "CoachRetry")
	column.add_child(retry_button)
	coach_evidence = VBoxContainer.new()
	coach_evidence.name = "CoachEvidence"
	column.add_child(coach_evidence)
	retry_feedback = _label("", 14, "f4e9c9")
	retry_feedback.name = "CoachRetryFeedback"
	column.add_child(retry_feedback)
	retry_promotion = HBoxContainer.new()
	retry_promotion.name = "CoachRetryPromotion"
	column.add_child(retry_promotion)
	for option in [["q", "퀸"], ["r", "룩"], ["b", "비숍"], ["n", "나이트"]]:
		var button := _button(str(option[1]), _retry_promote.bind(str(option[0])), "CoachRetryPromote" + str(option[0]))
		retry_promotion.add_child(button)
		retry_promotion_buttons[str(option[0])] = button
	var practice := HBoxContainer.new()
	column.add_child(practice)
	retry_hint_button = _button("힌트", _show_retry_hint, "CoachRetryHint")
	retry_again_button = _button("다시 시도", _begin_retry, "CoachRetryAgain")
	retry_reveal_button = _button("해설 보기", _reveal_retry, "CoachRetryReveal")
	practice.add_child(retry_hint_button)
	practice.add_child(retry_again_button)
	practice.add_child(retry_reveal_button)
	retry_line_button = _button("내가 고른 수의 응수 보기", _show_retry_line.bind("retry_candidate"), "CoachRetryLine")
	column.add_child(retry_line_button)


func open_game(pgn: String, color: String, url: String, token: String) -> void:
	_end_retry()
	_clear_coach_focus()
	_coach_overview = false
	_cancel_walkthrough()
	_epoch += 1
	var epoch: int = _epoch
	backend_url = url
	backend_token = token
	source_pgn = pgn
	human_color = color if color in ["white", "black"] else "white"
	side_option.select(0 if human_color == "white" else 1)
	focus_option.select(0)
	report = {}
	_job_replaced = false
	_preview = {}
	_error = ""
	return_warning = ""
	_list_key = ""
	_critical_key = ""
	selected_ply = 1
	show_after = true
	line_kind = ""
	_local_selection = false
	_ready_to_start = offline_mode
	show()
	_render()
	if offline_mode:
		return
	busy = true
	_render()
	# One transport serializes setup/start/cancel; polling has its own transport.
	var preview_response: Dictionary = await _request(requester, "/api/pgn-preview", {"pgn": pgn})
	if epoch != _epoch or not visible:
		busy = false
		return
	if not preview_response.get("ok", false):
		busy = false
		_error = str(preview_response.get("error", "기보를 읽지 못했습니다."))
		_render()
		return
	_preview = preview_response.data
	var response: Dictionary = await _request(requester, "/api/coaching")
	busy = false
	if epoch != _epoch or not visible:
		return
	if response.get("ok", false):
		revision = int(response.data.get("revision", 0))
		_ready_to_start = true
		if not _unconfirmed_job.is_empty() and _unconfirmed_job == str(response.data.get("job_id", "")) and _unconfirmed_pgn == source_pgn:
			_accept_snapshot(response.data, true)
		_unconfirmed_job = ""
		_unconfirmed_pgn = ""
	else:
		_error = str(response.get("error", "코칭에 연결하지 못했습니다."))
	_render()


func _request(client: HTTPRequest, path: String, body: Variant = null) -> Dictionary:
	var headers := PackedStringArray(["X-Regicide-Token: " + backend_token])
	var method: int = HTTPClient.METHOD_GET
	var contents := ""
	if body != null:
		headers.append("Content-Type: application/json")
		method = HTTPClient.METHOD_POST
		contents = JSON.stringify(body)
	var error: int = client.request(backend_url + path, headers, method, contents)
	if error != OK:
		return {"ok": false, "error": "코칭 요청을 보내지 못했습니다."}
	var reply: Array = await client.request_completed
	if int(reply[0]) != HTTPRequest.RESULT_SUCCESS:
		return {"ok": false, "error": "코칭 연결이 끊겼습니다. 다시 시작해 주세요."}
	var parsed: Variant = JSON.parse_string((reply[3] as PackedByteArray).get_string_from_utf8())
	if not parsed is Dictionary:
		return {"ok": false, "error": "코칭 응답을 읽지 못했습니다."}
	return {"ok": int(reply[1]) >= 200 and int(reply[1]) < 300, "data": parsed,
		"error": str(parsed.get("error", "코칭 요청을 처리하지 못했습니다."))}


func _start() -> void:
	if busy or _retry_in_flight or not _ready_to_start or source_pgn.is_empty() or report.get("status") == "running":
		return
	_end_retry()
	_cancel_walkthrough(true)
	await _mutate({"action": "start", "pgn": source_pgn, "human_color": human_color,
		"movetime_ms": time_option.get_selected_id(), "revision": revision})


func _cancel() -> void:
	if busy or report.get("status") != "running":
		return
	await _mutate({"action": "cancel", "revision": revision})


func _mutate(body: Dictionary) -> void:
	busy = true
	_mutation_epoch += 1
	var epoch: int = _epoch
	_error = ""
	_render()
	var response: Dictionary = await _request(requester, "/api/coaching", body)
	busy = false
	if epoch != _epoch or not visible:
		return
	if response.get("ok", false):
		_accept_snapshot(response.data, true, body.get("action") == "start")
	else:
		_error = str(response.get("error", "코칭 요청에 실패했습니다."))
		var data: Dictionary = response.get("data", {})
		if data.get("coaching") is Dictionary:
			revision = int(data.coaching.get("revision", revision))
			if not report.is_empty() and data.coaching.get("job_id") != report.get("job_id"):
				_invalidate_replaced_job()
	_render()


func _poll() -> void:
	if not visible or offline_mode or busy or _polling or report.get("status") != "running":
		return
	_polling = true
	var epoch: int = _epoch
	var mutation: int = _mutation_epoch
	var job: String = str(report.get("job_id", ""))
	var response: Dictionary = await _request(poll_requester, "/api/coaching")
	_polling = false
	if epoch != _epoch or mutation != _mutation_epoch or not visible:
		return
	if response.get("ok", false):
		var next: Dictionary = response.data
		if str(next.get("job_id", "")) == job and int(next.get("revision", -1)) >= revision:
			_error = ""
			_accept_snapshot(next)
		elif str(next.get("job_id", "")) != job:
			revision = int(next.get("revision", revision))
			_invalidate_replaced_job()
	else:
		_error = str(response.get("error", "코칭 진행 상황을 읽지 못했습니다."))
	_render()


func _invalidate_replaced_job() -> void:
	# Keep this game's existing frames, never attach or cancel another client job.
	_job_replaced = true
	report["status"] = "cancelled"
	_end_retry()
	_clear_coach_focus()
	_cancel_walkthrough(true)
	_error = "다른 화면에서 새 코칭 분석을 시작했습니다. 이 기보를 다시 분석하려면 분석 시작을 눌러 주세요."


func _accept_snapshot(value: Dictionary, mutation: bool = false, starting: bool = false) -> void:
	if not mutation and (int(value.get("revision", -1)) < revision or (not report.is_empty() and value.get("job_id") != report.get("job_id"))):
		return
	var previous_status: String = str(report.get("status", ""))
	if starting or value.get("job_id") != report.get("job_id") or int(value.get("revision", revision)) != revision:
		_end_retry()
		_clear_coach_focus()
	if _walkthrough_active and (starting or str(value.get("job_id", "")) != _walkthrough_job):
		_cancel_walkthrough(true)
	if starting:
		line_kind = ""
		line_index = 0
		show_after = true
		_local_selection = false
		previous_status = ""
	report = value.duplicate(true)
	_job_replaced = false
	revision = int(report.get("revision", revision))
	# My color is a local presentation preference; polling must not undo it.
	selected_ply = clampi(selected_ply, 1, maxi(1, _moves().size()))
	if not line_kind.is_empty():
		if _line_frames().size() < 2:
			line_kind = ""
			line_index = 0
		else:
			line_index = clampi(line_index, 0, _line_frames().size() - 1)
	if report.get("status") == "completed" and previous_status != "completed" and not _local_selection:
		var critical: Array = _critical_rows()
		if not critical.is_empty():
			selected_ply = int(critical[0].ply)
			for candidate in report.get("critical_plies", []):
				if critical.any(func(row: Dictionary): return int(row.ply) == int(candidate)):
					selected_ply = int(candidate)
					break
			show_after = true
			line_kind = ""
	_render()


func _return() -> void:
	if busy:
		return
	_end_retry()
	_clear_coach_focus()
	_cancel_walkthrough(true)
	if report.get("status") == "running":
		await _cancel()
		if report.get("status") == "running":
			_unconfirmed_job = str(report.get("job_id", ""))
			_unconfirmed_pgn = source_pgn
			return_warning = "분석 중단 응답을 확인하지 못했습니다. 코칭을 다시 열어 상태를 확인하거나 앱을 종료해 주세요."
	_epoch += 1
	hide()
	returned.emit()


func _change_side(index: int) -> void:
	_end_retry()
	_cancel_walkthrough(true)
	human_color = "white" if index == 0 else "black"
	side_option.select(index)
	_list_key = ""
	_critical_key = ""
	_render()


func _change_focus(index: int) -> void:
	_end_retry()
	_cancel_walkthrough(true)
	focus_option.select(index)
	_critical_key = ""
	_render()


func _moves() -> Array:
	return report.get("moves", _preview.get("moves", []))


func _frames() -> Array:
	return report.get("frames", _preview.get("frames", []))


func _move() -> Dictionary:
	var moves: Array = _moves()
	return moves[selected_ply - 1] if selected_ply > 0 and selected_ply <= moves.size() else {}


func _selected_line() -> Dictionary:
	if line_kind in ["retry_candidate", "retry_best"]:
		var candidate: Variant = _retry_reply.get("line" if line_kind == "retry_candidate" else "best")
		return candidate if candidate is Dictionary else {}
	var value: Variant = _move().get(line_kind)
	return value if value is Dictionary else {}


func _line_frames() -> Array:
	return _selected_line().get("pv_frames", [])


func _shown_frame() -> Dictionary:
	if retry_active:
		return _retry_frame
	if _walkthrough_active:
		var actual: Array = _frames()
		return actual[_walkthrough_cursor] if _walkthrough_cursor >= 0 and _walkthrough_cursor < actual.size() else {}
	if not line_kind.is_empty():
		var frames: Array = _line_frames()
		return frames[clampi(line_index, 0, frames.size() - 1)] if not frames.is_empty() else {}
	var frames: Array = _frames()
	var index: int = selected_ply if show_after else selected_ply - 1
	return frames[clampi(index, 0, frames.size() - 1)] if not frames.is_empty() else {}


func _select_ply(ply: int) -> void:
	_end_retry()
	_clear_coach_focus()
	_coach_overview = false
	_cancel_walkthrough()
	_local_selection = true
	selected_ply = clampi(ply, 1, maxi(1, _moves().size()))
	line_kind = ""
	show_after = true
	_render()


func _first() -> void:
	_select_ply(1)
	show_after = false
	_render()


func _step(direction: int) -> void:
	# Move by actual positions, including the initial position and a paused replay.
	var shown: int = _walkthrough_cursor if _walkthrough_active else (selected_ply if show_after and line_kind.is_empty() else selected_ply - 1)
	var cursor: int = clampi(shown + direction, 0, _moves().size())
	_select_ply(maxi(1, cursor))
	show_after = cursor > 0
	_render()


func _last() -> void:
	_select_ply(_moves().size())
	show_after = true
	_render()


func _show_phase(after: bool) -> void:
	_end_retry()
	_clear_coach_focus()
	_cancel_walkthrough()
	_local_selection = true
	line_kind = ""
	show_after = after
	_render()


func _show_line(kind: String) -> void:
	var value: Variant = _move().get(kind)
	if not value is Dictionary or value.get("pv_frames", []).size() < 2:
		return
	_end_retry()
	_clear_coach_focus()
	_cancel_walkthrough()
	_local_selection = true
	line_kind = kind
	line_index = 0
	_render()


func _line_step(direction: int) -> void:
	line_index = clampi(line_index + direction, 0, maxi(0, _line_frames().size() - 1))
	_render()


func _leave_line() -> void:
	_end_retry()
	_clear_coach_focus()
	line_kind = ""
	show_after = true
	_render()


func flip_view() -> void:
	_cancel_walkthrough(true)
	model.flip_view()
	_render()


func _critical_rows() -> Array:
	var rows: Array = []
	var focus: int = focus_option.get_selected_id()
	for row in _moves():
		var mine: bool = _row_color(row) == human_color
		if row.get("classification", "pending") in ["inaccuracy", "mistake", "blunder", "missed_mate", "allows_mate"] and (focus == 0 or (focus == 1 and mine) or (focus == 2 and not mine)):
			rows.append(row)
	return rows


func _row_color(row: Dictionary) -> String:
	if row.get("color", "") in ["white", "black"]:
		return str(row.color)
	var frames: Array = _frames()
	var index: int = int(row.get("ply", 1)) - 1
	if index >= 0 and index < frames.size():
		return str(frames[index].get("turn", "white"))
	return "white" if index % 2 == 0 else "black"


func _side_label(color: String) -> String:
	return ("백" if color == "white" else "흑") + (" · 내 수" if color == human_color else " · 상대 수")


func _summary_for_side(color: String) -> String:
	var analyzed: int = 0
	var counts: Dictionary = {}
	for row in _moves():
		var classification: String = str(row.get("classification", "pending"))
		if _row_color(row) != color or classification == "pending":
			continue
		analyzed += 1
		counts[classification] = int(counts.get(classification, 0)) + 1
	var labels := {"inaccuracy": "아쉬운 수", "mistake": "실수", "blunder": "큰 실수", "missed_mate": "메이트 기회 놓침", "allows_mate": "메이트 허용", "uncertain": "판단 보류"}
	var details: PackedStringArray = PackedStringArray()
	for key in labels:
		if int(counts.get(key, 0)) > 0:
			details.append(str(labels[key]) + " %d" % int(counts[key]))
	var detail: String = " · ".join(details) if not details.is_empty() else ("해당 실수 없음" if analyzed > 0 else "분석 대기")
	return _side_label(color) + "  |  %d수 분석\n" % analyzed + detail


func _critical_step(direction: int) -> void:
	if _walkthrough_active:
		return
	var rows: Array = _critical_rows()
	if direction < 0:
		rows.reverse()
	for row in rows:
		if (direction > 0 and int(row.ply) > selected_ply) or (direction < 0 and int(row.ply) < selected_ply):
			_navigate_critical(int(row.ply))
			return


func _select_critical(index: int) -> void:
	if not _walkthrough_active:
		_navigate_critical(int(critical_list.get_item_metadata(index)))


func _navigate_critical(target: int) -> void:
	_end_retry()
	_clear_coach_focus()
	_coach_overview = false
	var cursor: int = selected_ply if show_after and line_kind.is_empty() else selected_ply - 1
	if target <= selected_ply or cursor >= target:
		_select_ply(target)
		return
	# PVs are hypothetical. Walk from their real game's original pre-position.
	line_kind = ""
	line_index = 0
	show_after = false
	_local_selection = true
	_walkthrough_from = cursor
	_walkthrough_cursor = cursor
	_walkthrough_target = target
	_walkthrough_job = str(report.get("job_id", ""))
	_walkthrough_active = true
	_walkthrough_paused = false
	_render()
	walkthrough_timer.start()
	# The first timed tick leaves a brief view of the real starting position.


func _advance_walkthrough() -> void:
	if not _walkthrough_active or _walkthrough_paused:
		return
	if not visible or str(report.get("job_id", "")) != _walkthrough_job:
		_cancel_walkthrough(true)
		_render()
		return
	if _walkthrough_cursor >= _walkthrough_target:
		var target: int = _walkthrough_target
		_select_ply(target)
		return
	_walkthrough_cursor += 1
	replay_frame_shown.emit(_walkthrough_cursor)
	if _walkthrough_cursor == _walkthrough_target:
		_select_ply(_walkthrough_target)
		return
	_render()


func _cancel_walkthrough(keep_position: bool = false) -> void:
	if _walkthrough_active and keep_position:
		selected_ply = clampi(_walkthrough_cursor, 1, maxi(1, _moves().size()))
		show_after = _walkthrough_cursor > 0
		line_kind = ""
	_walkthrough_active = false
	_walkthrough_paused = false
	if walkthrough_timer != null:
		walkthrough_timer.stop()


func _pause_walkthrough() -> void:
	if not _walkthrough_active:
		return
	_walkthrough_paused = not _walkthrough_paused
	if _walkthrough_paused:
		walkthrough_timer.stop()
	else:
		walkthrough_timer.start()
	_render()


func _skip_walkthrough() -> void:
	if _walkthrough_active:
		_select_ply(_walkthrough_target)


func _walkthrough_text() -> String:
	if not _walkthrough_active:
		return "실제 수를 차례로 재생하고 목표 수를 둔 뒤 멈춥니다."
	var moves: Array = _moves()
	var first: int = maxi(_walkthrough_from, _walkthrough_cursor - 3)
	var last: int = mini(_walkthrough_target, _walkthrough_cursor + 4)
	var words: PackedStringArray = PackedStringArray()
	if first > _walkthrough_from:
		words.append("…")
	for index in range(first, last):
		var row: Dictionary = moves[index]
		var text: String = _move_prefix(row) + " " + str(row.get("san", ""))
		words.append("【" + text + "】" if index + 1 == _walkthrough_cursor else text)
	if last < _walkthrough_target:
		words.append("…")
	return ("실제 기보 · 일시정지" if _walkthrough_paused else "실제 기보를 따라가는 중") + "  %d/%d\n" % [_walkthrough_cursor - _walkthrough_from, _walkthrough_target - _walkthrough_from] + "  ".join(words)


func _move_prefix(row: Dictionary) -> String:
	var fen: String = str(row.get("fen_before", ""))
	var parts: PackedStringArray = fen.split(" ")
	var number: int = int(parts[5]) if parts.size() == 6 else int((int(row.get("ply", 1)) + 1) / 2)
	var side: String = _row_color(row)
	return "%d%s" % [number, "." if side == "white" else "…"]


func _clear_coach_focus() -> void:
	_coach_focus.clear()
	_coach_focus_fen = ""
	_coach_focus_line = ""
	_coach_focus_index = -1
	if board != null:
		board.coach_squares = []


func _toggle_coach_overview() -> void:
	if retry_active:
		return
	_coach_overview = not _coach_overview
	_clear_coach_focus()
	_render()


func _lesson_plies() -> Array:
	var lessons: Array = []
	var course: Dictionary = report.get("coach", {})
	for candidate in course.get("lesson_plies", []):
		var ply: int = int(candidate)
		if ply >= 1 and ply <= _moves().size() and not lessons.has(ply):
			lessons.append(ply)
		if lessons.size() == 5:
			break
	lessons.sort()
	return lessons


func _step_lesson(direction: int) -> void:
	if _walkthrough_active:
		return
	var lessons: Array = _lesson_plies()
	if direction < 0:
		lessons.reverse()
	for ply in lessons:
		if (direction > 0 and int(ply) > selected_ply) or (direction < 0 and int(ply) < selected_ply):
			_navigate_critical(int(ply))
			return


func _show_evidence(evidence: Dictionary, ply: int, job: String, source_fen: String) -> void:
	if retry_active or ply != selected_ply or job != str(report.get("job_id", "")) or source_fen != str(_move().get("fen_before", "")):
		return
	var kind: String = str(evidence.get("line", ""))
	var line: Variant = _move().get(kind)
	var index: int = int(evidence.get("ply_index", -1))
	if kind not in ["played", "best"] or not line is Dictionary or index < 0 or index >= line.get("pv_frames", []).size():
		return
	_cancel_walkthrough()
	_coach_overview = false
	line_kind = kind
	line_index = index
	_local_selection = true
	_coach_focus = []
	for square in evidence.get("squares", []):
		if model.square_to_display(str(square)).x >= 0:
			_coach_focus.append(str(square))
	_coach_focus_fen = str(line.pv_frames[index].get("fen", ""))
	_coach_focus_line = kind
	_coach_focus_index = index
	_render()


func _can_retry() -> bool:
	var move: Dictionary = _move()
	var frames: Array = _frames()
	return not _job_replaced and report.get("status") in ["completed", "cancelled"] and move.get("classification", "pending") != "pending" and move.get("retry_legal_moves") is Array and not move.retry_legal_moves.is_empty() and selected_ply > 0 and selected_ply <= frames.size() and frames[selected_ply - 1].get("fen") == move.get("fen_before")


func _begin_retry() -> void:
	if not _can_retry() or _retry_in_flight or busy:
		return
	_cancel_walkthrough()
	_end_retry()
	_clear_coach_focus()
	_coach_overview = false
	_local_selection = true
	show_after = false
	line_kind = ""
	line_index = 0
	retry_active = true
	_retry_frame = _frames()[selected_ply - 1].duplicate(true)
	_retry_frame["legal_moves"] = _move().retry_legal_moves.duplicate()
	_retry_frame["last_move"] = null
	_retry_status = "기물을 클릭하거나 끌어서 수를 골라 보세요."
	_retry_hint = false
	_error = ""
	_render()
	# Returning from a solution line must not animate that answer in reverse.
	board.finish_animation()


func _end_retry(clear_reply: bool = true) -> void:
	_stop_speech()
	_retry_epoch += 1
	retry_active = false
	_retry_frame = {}
	_retry_hint = false
	_retry_status = ""
	_retry_chosen_uci = ""
	model.selected_square = ""
	model.pending_promotions.clear()
	if clear_reply:
		_retry_reply = {}


func _on_retry_square(square: String) -> void:
	if not retry_active or _retry_in_flight or not model.pending_promotions.is_empty():
		return
	if square == model.selected_square:
		model.selected_square = ""
		_render()
		return
	var candidates: Array[String] = []
	if not model.selected_square.is_empty():
		for move in _retry_frame.get("legal_moves", []):
			if str(move).begins_with(model.selected_square + square):
				candidates.append(str(move))
	if candidates.size() > 1 or (candidates.size() == 1 and candidates[0].length() == 5):
		model.pending_promotions = candidates
		_render()
		return
	if candidates.size() == 1:
		_submit_retry(candidates[0])
		return
	model.selected_square = ""
	for move in _retry_frame.get("legal_moves", []):
		if str(move).begins_with(square):
			model.selected_square = square
			break
	_render()


func _on_retry_drag(from_square: String, to_square: String) -> void:
	if not retry_active or _retry_in_flight or not model.pending_promotions.is_empty():
		return
	for move in _retry_frame.get("legal_moves", []):
		if str(move).begins_with(from_square):
			model.selected_square = from_square
			_on_retry_square(to_square)
			return


func _retry_promote(kind: String) -> void:
	if not retry_active or _retry_in_flight:
		return
	for move in model.pending_promotions:
		if move.ends_with(kind):
			_submit_retry(move)
			return


func _retry_ticket(uci: String) -> Dictionary:
	return {"epoch": _epoch, "retry_epoch": _retry_epoch, "job_id": str(report.get("job_id", "")),
		"revision": revision, "ply": selected_ply, "uci": uci, "fen": str(_move().get("fen_before", ""))}


func _retry_ticket_current(ticket: Dictionary) -> bool:
	return visible and retry_active and int(ticket.get("epoch", -1)) == _epoch and int(ticket.get("retry_epoch", -1)) == _retry_epoch and ticket.get("job_id") == report.get("job_id") and int(ticket.get("revision", -1)) == revision and int(ticket.get("ply", -1)) == selected_ply and ticket.get("fen") == _move().get("fen_before") and ticket.get("fen") == _retry_frame.get("fen")


func _accept_retry_result(value: Dictionary, ticket: Dictionary) -> bool:
	if not _retry_ticket_current(ticket):
		return false
	for key in ["job_id", "revision", "ply", "uci"]:
		if value.get(key) != ticket.get(key):
			return false
	if value.get("verdict") not in ["recommended", "good", "try_again", "uncertain"]:
		return false
	_retry_reply = value.duplicate(true)
	for key in ["line", "best"]:
		var line: Variant = _retry_reply.get(key)
		if not line is Dictionary or line.get("pv_frames", []).is_empty() or line.pv_frames[0].get("fen") != ticket.fen:
			_retry_reply.erase(key)
	if _retry_reply.get("line") is Dictionary and _retry_reply.line.get("uci") != ticket.uci:
		_retry_reply.erase("line")
	_retry_status = str(value.get("message", "수를 확인했습니다."))
	model.selected_square = ""
	model.pending_promotions.clear()
	_render()
	return true


func _submit_retry(uci: String) -> void:
	if not retry_active or _retry_in_flight or not _retry_frame.get("legal_moves", []).has(uci):
		return
	_retry_chosen_uci = uci
	model.selected_square = ""
	model.pending_promotions.clear()
	retry_submitted.emit(uci)
	if offline_mode:
		_retry_status = "연결된 대국 화면에서 루크가 이 수를 확인해 줄게."
		_render()
		return
	var ticket: Dictionary = _retry_ticket(uci)
	_retry_in_flight = true
	_retry_reply = {}
	_retry_status = "네가 고른 수와 상대의 응수를 살펴보고 있어…"
	_render()
	var response: Dictionary = await _request(retry_requester, "/api/coaching", {"action": "retry", "ply": selected_ply,
		"uci": uci, "revision": revision, "job_id": str(report.get("job_id", ""))})
	_retry_in_flight = false
	if not _retry_ticket_current(ticket):
		if visible:
			_render()
		return
	if response.get("ok", false):
		if not _accept_retry_result(response.data, ticket):
			_retry_status = "다른 위치의 응답이라 표시하지 않았어요. 다시 시도해 주세요."
	else:
		_accept_retry_error(response, ticket)
	_render()


func _accept_retry_error(response: Dictionary, ticket: Dictionary) -> bool:
	if not _retry_ticket_current(ticket):
		return false
	_retry_status = str(response.get("error", "이 수의 분석을 받지 못했습니다. 다시 시도해 주세요."))
	var data: Variant = response.get("data")
	if data is Dictionary and data.get("coaching") is Dictionary:
		var current: Dictionary = data.coaching
		if int(current.get("revision", -1)) > revision:
			if current.get("job_id") == report.get("job_id"):
				_accept_snapshot(current, true)
				_error = "분석 상태가 바뀌어 연습을 닫았습니다. 이 국면에서 다시 두기를 눌러 계속할 수 있어요."
			else:
				revision = int(current.revision)
				_invalidate_replaced_job()
	return true


func _show_retry_hint() -> void:
	if retry_active:
		_retry_hint = true
		_render()


func _reveal_retry() -> void:
	if not retry_active and line_kind not in ["retry_candidate", "retry_best"]:
		return
	if _retry_reply.get("best") is Dictionary:
		_show_retry_line("retry_best")
	else:
		_end_retry()
		_show_line("best")
		_render()


func _show_retry_line(kind: String) -> void:
	var candidate: Variant = _retry_reply.get("line" if kind == "retry_candidate" else "best")
	if not candidate is Dictionary or candidate.get("pv_frames", []).size() < 2:
		return
	_end_retry(false)
	_clear_coach_focus()
	line_kind = kind
	line_index = 0
	show_after = false
	_render()


func _render_coach() -> void:
	var row_coach: Variant = _move().get("coach")
	var coach: Dictionary = row_coach if row_coach is Dictionary else {}
	var course: Dictionary = report.get("coach", {})
	var viewing_retry_line: bool = line_kind in ["retry_candidate", "retry_best"] and not _retry_reply.is_empty()
	var dialogue: String = str(coach.get("dialogue", "I'm checking this move and the opponent's reply."))
	avatar.mood = str(coach.get("mood", "encourage"))
	if viewing_retry_line:
		dialogue = str(_retry_reply.get("message", ""))
		avatar.mood = "think"
	if _coach_overview:
		dialogue = str(course.get("summary", "Start the analysis to find the key moments."))
		avatar.mood = "think"
	if _walkthrough_active:
		dialogue = "Following the moves actually played to the next key moment."
		avatar.mood = "think"
	if retry_active:
		dialogue = str(coach.get("hint", "Check for threats against the king and pieces that can be captured.")) if _retry_hint else "Your turn: find a move, then check the opponent's strongest reply."
		avatar.mood = "think" if _retry_reply.is_empty() else ("celebrate" if _retry_reply.get("verdict") in ["recommended", "good"] else "encourage")
	coach_dialogue.text = dialogue
	_schedule_speech()
	coach_overview_button.text = "이 국면 이야기" if _coach_overview else "한 판 총평"
	coach_overview_button.disabled = retry_active
	var lessons: Array = _lesson_plies()
	var course_index: int = lessons.find(selected_ply)
	coach_course.text = "루크의 복습 코스 · %d곳" % lessons.size() if course_index < 0 else "루크의 복습 코스 · %d / %d" % [course_index + 1, lessons.size()]
	if lessons.is_empty():
		coach_course.text = "선택된 복습 코스가 아직 없어요. 분석된 수는 직접 살펴볼 수 있어요."
	lesson_previous.disabled = _walkthrough_active or retry_active or not lessons.any(func(ply: int): return ply < selected_ply)
	lesson_next.disabled = _walkthrough_active or retry_active or not lessons.any(func(ply: int): return ply > selected_ply)
	coach_evidence.visible = not retry_active and not viewing_retry_line and not _coach_overview and not _walkthrough_active
	var evidence_key: String = str(report.get("job_id", "")) + ":" + str(selected_ply) + JSON.stringify(coach.get("evidence", []))
	if evidence_key != _coach_evidence_key:
		_coach_evidence_key = evidence_key
		for child in coach_evidence.get_children():
			coach_evidence.remove_child(child)
			child.queue_free()
		var count: int = 0
		for evidence in coach.get("evidence", []):
			if not evidence is Dictionary or count >= 3:
				continue
			coach_evidence.add_child(_label(str(evidence.get("text", "")), 12, "b5cbd1"))
			coach_evidence.add_child(_button("이 장면을 판에서 보기", _show_evidence.bind(evidence.duplicate(true), selected_ply, str(report.get("job_id", "")), str(_move().get("fen_before", ""))), "CoachEvidence%d" % count))
			count += 1
	retry_button.visible = not retry_active and not viewing_retry_line
	retry_button.disabled = not _can_retry() or _retry_in_flight or busy or _walkthrough_active
	retry_feedback.visible = retry_active
	retry_feedback.text = _retry_status
	retry_hint_button.visible = retry_active
	retry_again_button.visible = retry_active or viewing_retry_line
	retry_reveal_button.visible = retry_active or (viewing_retry_line and _retry_reply.get("best") is Dictionary)
	retry_reveal_button.text = "새 추천 수순" if viewing_retry_line else "해설 보기"
	retry_again_button.disabled = _retry_in_flight
	retry_hint_button.disabled = _retry_in_flight
	retry_promotion.visible = retry_active and not model.pending_promotions.is_empty()
	for kind in retry_promotion_buttons:
		retry_promotion_buttons[kind].disabled = _retry_in_flight or not model.pending_promotions.any(func(move: String): return move.ends_with(str(kind)))
	retry_line_button.visible = (retry_active or viewing_retry_line) and _retry_reply.get("line") is Dictionary
	retry_line_button.disabled = _retry_in_flight


func _setup_voice() -> void:
	_english_voice = ""
	_speech_error = ""
	_speech_backend = "native"
	if DisplayServer.get_name() != "headless" and DisplayServer.has_feature(DisplayServer.FEATURE_TEXT_TO_SPEECH):
		var voices: PackedStringArray = DisplayServer.tts_get_voices_for_language("en")
		if not voices.is_empty():
			_english_voice = voices[0]
	if _english_voice.is_empty() and OS.get_name() == "Windows" and DisplayServer.get_name() != "headless":
		_setup_windows_voice()
	_render_speech_controls()


func _make_windows_speech() -> WindowsSpeech:
	return WindowsSpeech.new()


func _setup_windows_voice() -> void:
	_speech_backend = "windows"
	_voice_loading = true
	if not is_instance_valid(_windows_speech):
		_windows_speech = _make_windows_speech()
		_windows_speech.voices_ready.connect(_windows_voices_ready)
		_windows_speech.audio_ready.connect(_windows_audio_ready)
		_windows_speech.failed.connect(_windows_speech_failed)
		add_child(_windows_speech)
	if not is_instance_valid(_speech_player):
		_speech_player = AudioStreamPlayer.new()
		_speech_player.name = "CoachLocalVoice"
		add_child(_speech_player)
	_windows_speech.query_voices()


func _windows_voices_ready(voices: Array, error: String) -> void:
	_voice_loading = false
	_speech_error = error
	_english_voice = str(voices[0].id) if not voices.is_empty() else ""
	_render_speech_controls()
	_schedule_speech()


func _windows_audio_ready(ticket: int, stream: AudioStreamWAV) -> void:
	if ticket != _speech_generation or not _speech_owned:
		return
	if not _can_speak() or _speech_context_key() != _speech_last_key:
		_stop_speech()
		return
	_speech_preparing_audio = false
	_play_windows_audio(stream)
	_render_speech_controls()


func _windows_speech_failed(ticket: int, error: String) -> void:
	if ticket != _speech_generation:
		return
	_speech_error = error
	_stop_speech()


func _play_windows_audio(stream: AudioStreamWAV) -> void:
	_speech_player.stream = stream
	_speech_player.play()


func _windows_audio_is_playing() -> bool:
	return is_instance_valid(_speech_player) and _speech_player.playing


func _render_speech_controls() -> void:
	if not is_instance_valid(speak_button):
		return
	speak_button.disabled = _voice_loading or (_english_voice.is_empty() and _speech_error.is_empty())
	speak_button.set_pressed_no_signal(_speech_enabled and not _english_voice.is_empty())
	speak_button.text = "영어 음성 없음" if _english_voice.is_empty() else ("음성 켜짐" if _speech_enabled else "음성 꺼짐")
	speak_button.tooltip_text = "이 컴퓨터에 설치된 영어 음성이 필요합니다. 글로 된 코칭은 그대로 사용할 수 있습니다." if _english_voice.is_empty() else "수를 넘기면 컴퓨터의 영어 음성으로 설명을 읽습니다. 눌러서 켜거나 끌 수 있습니다."
	stop_speech_button.disabled = not _speech_owned and _speech_pending_key.is_empty()
	if _voice_loading:
		speak_button.text = "음성 준비 중"
	elif not _speech_error.is_empty():
		speak_button.text = "음성 다시 시도"
		speak_button.tooltip_text = _speech_error
	if is_instance_valid(voice_status_label):
		voice_status_label.text = _speech_error if not _speech_error.is_empty() else ("컴퓨터의 영어 음성을 찾고 있어요." if _voice_loading else ("영어 음성을 준비하고 있어요." if _speech_preparing_audio else ("내 컴퓨터의 영어 음성 · 수를 넘기면 자동 안내" if not _english_voice.is_empty() else "사용 가능한 영어 음성이 없어 글로 안내합니다.")))


func _toggle_speech() -> void:
	if not _speech_error.is_empty():
		_speech_error = ""
		_speech_enabled = true
		_speech_last_key = ""
		if _english_voice.is_empty():
			_setup_voice()
		else:
			_schedule_speech()
		_render_speech_controls()
		return
	if _english_voice.is_empty():
		return
	_speech_enabled = not _speech_enabled
	_stop_speech()
	if _speech_enabled:
		_speech_last_key = ""
		_schedule_speech()
	_render_speech_controls()


func _spoken_coach_text() -> String:
	# Only use text already revealed in the current card. Never read hidden best moves.
	if retry_active and not _retry_reply.is_empty():
		return str(_retry_reply.get("message", coach_dialogue.text))
	return coach_dialogue.text


func _speech_context_key() -> String:
	var mode: String = "overview" if _coach_overview else ("retry" if retry_active else line_kind)
	return JSON.stringify([_epoch, report.get("job_id", ""), selected_ply, mode, show_after, _retry_reply.get("uci", "") if retry_active else "", _spoken_coach_text()])


func _can_speak() -> bool:
	return is_visible_in_tree() and _speech_enabled and not _voice_loading and _speech_error.is_empty() and not _english_voice.is_empty() and not busy and not _walkthrough_active and not _retry_in_flight and not _spoken_coach_text().is_empty()


func _schedule_speech() -> void:
	if not _can_speak():
		_stop_speech()
		return
	var key: String = _speech_context_key()
	if key == _speech_pending_key:
		return
	if key == _speech_last_key:
		if not _speech_pending_key.is_empty():
			_stop_speech()
		return
	_stop_speech()
	_speech_pending_key = key
	speech_timer.start()
	_render_speech_controls()


func _speak_coach() -> void:
	if not _can_speak() or _speech_pending_key.is_empty() or _speech_pending_key != _speech_context_key():
		_stop_speech()
		return
	_speech_last_key = _speech_pending_key
	_speech_pending_key = ""
	_speech_text = _spoken_coach_text()
	_speech_owned = true
	_voice_speak(_speech_text, _english_voice)
	_render_speech_controls()


func _stop_current_speech() -> void:
	_speech_last_key = _speech_context_key()
	_stop_speech()


func _stop_speech() -> void:
	_speech_generation += 1
	if is_instance_valid(speech_timer):
		speech_timer.stop()
	_speech_pending_key = ""
	if _speech_owned:
		_voice_stop()
	_speech_owned = false
	_speech_preparing_audio = false
	_speech_text = ""
	_render_speech_controls()


func _poll_speech() -> void:
	if _speech_owned and (not is_visible_in_tree() or not _voice_is_speaking()):
		_stop_speech()


func _voice_speak(text: String, voice: String) -> void:
	if _speech_backend == "windows":
		_speech_preparing_audio = true
		_windows_speech.synthesize(_speech_generation, text, voice)
	else:
		DisplayServer.tts_speak(text, voice)


func _voice_stop() -> void:
	if _speech_backend == "windows":
		_windows_speech.cancel_synthesis()
		if is_instance_valid(_speech_player):
			_speech_player.stop()
	else:
		DisplayServer.tts_stop()


func _voice_is_speaking() -> bool:
	if _speech_backend == "windows":
		return _speech_preparing_audio or _windows_audio_is_playing()
	return DisplayServer.tts_is_speaking()


func _score(value: Variant) -> String:
	if not value is Dictionary:
		return "미분석"
	var bound: String = str(value.get("bound", "exact"))
	var prefix: String = "≥ " if bound == "lower" else ("≤ " if bound == "upper" else "")
	if value.get("mate_white") != null:
		var mate: int = int(value.mate_white)
		if mate == 0:
			return prefix + ("백" if value.get("winner") == "white" else "흑") + " 체크메이트"
		return prefix + ("백" if mate > 0 else "흑") + " M%d" % absi(mate)
	if value.get("score_cp_white") != null:
		return prefix + "%+.2f" % (float(value.score_cp_white) / 100.0)
	return "평가 없음"


func _annotation_for_frame(frame: Dictionary) -> Dictionary:
	if retry_active or not line_kind.is_empty() or (not _walkthrough_active and not show_after):
		return {}
	var ply: int = int(frame.get("ply", -1))
	var moves: Array = _moves()
	if ply <= 0 or ply > moves.size():
		return {}
	var row: Dictionary = moves[ply - 1]
	var uci: String = str(row.get("uci", ""))
	if int(row.get("ply", -1)) != ply or str(frame.get("fen", "")).is_empty() or frame.get("fen") != row.get("fen_after") or uci.length() not in [4, 5] or frame.get("last_move") != uci:
		return {}
	var classification: String = str(row.get("classification", "pending"))
	var symbols := {"best": "★", "good": "✓", "inaccuracy": "?!", "mistake": "?", "blunder": "??", "allows_mate": "??", "missed_mate": "?", "uncertain": "…"}
	var labels := {"best": "추천 수", "good": "좋은 수", "inaccuracy": "아쉬운 수", "mistake": "실수", "blunder": "큰 실수", "allows_mate": "메이트 허용", "missed_mate": "메이트 기회 놓침", "uncertain": "판단 보류"}
	if not symbols.has(classification) or not frame.get("board", {}).has(uci.substr(2, 2)):
		return {}
	var metadata: Variant = row.get("annotation")
	if metadata is Dictionary:
		if metadata.get("scope") != "actual_move" or int(metadata.get("ply", -1)) != ply or metadata.get("uci") != uci or metadata.get("fen_after") != frame.fen or metadata.get("square") != uci.substr(2, 2):
			return {}
		if metadata.get("kind") == "great":
			if classification != "best" or metadata.get("symbol") != "!!" or not row.get("best") is Dictionary or row.best.get("uci") != uci:
				return {}
		elif metadata.get("kind") != classification or metadata.get("symbol") != symbols[classification]:
			return {}
		return metadata.duplicate(true)
	# Old reports can retain ordinary labels; never infer the extra !! judgment.
	return {"symbol": symbols[classification], "kind": classification, "label": labels[classification],
		"square": uci.substr(2, 2), "scope": "actual_move", "provisional": true, "ply": ply,
		"uci": uci, "fen_after": frame.fen, "reason": "저장된 제한 시간 분석의 분류입니다. 더 깊게 분석하면 달라질 수 있습니다."}


func _render() -> void:
	if board == null:
		return
	var moves: Array = _moves()
	var move: Dictionary = _move()
	var frame: Dictionary = _shown_frame()
	var running: bool = report.get("status") == "running"
	var has_moves: bool = not moves.is_empty()
	board.state = frame
	board.move_annotation = _annotation_for_frame(frame)
	board.mouse_filter = Control.MOUSE_FILTER_STOP if retry_active else Control.MOUSE_FILTER_IGNORE
	board.hint_move = ""
	board.coach_squares = _coach_focus.duplicate() if not retry_active and not _coach_overview and not _walkthrough_active and frame.get("fen", "") == _coach_focus_fen and line_kind == _coach_focus_line and line_index == _coach_focus_index else []
	var best: Variant = move.get("best")
	var played: Variant = move.get("played")
	var score: Variant = null
	if _walkthrough_active or retry_active:
		pass
	elif not line_kind.is_empty():
		var line: Dictionary = _selected_line()
		var pv: Array = line.get("pv_uci", [])
		if line_index < pv.size():
			board.hint_move = str(pv[line_index])
		# A root search score is not an evaluation of later PV positions.
		if line_index == 0:
			score = line
	elif not show_after and best is Dictionary:
		board.hint_move = str(best.get("uci", ""))
		score = best
	board.queue_redraw()
	var evaluation: Dictionary = {"status": "unavailable", "message": "수순의 각 위치는 별도로 평가하지 않았습니다."}
	if score is Dictionary:
		evaluation = {"status": "ready", "cp": score.get("score_cp_white"), "mate": score.get("mate_white"),
			"winner": score.get("winner"), "depth": score.get("depth"), "bound": score.get("bound", "exact"),
			"fen": frame.get("fen", ""), "turn": frame.get("turn", "")}
	evaluation_bar.present(evaluation, model.view_from_white)
	evaluation_label.text = evaluation_bar.summary_text if score != null else ("선택한 수를 분석하고 있습니다" if move.get("classification", "pending") == "pending" else "이 위치의 별도 평가 없음")
	var prefix: String = _move_prefix(move) + " " + str(move.get("san", "")) + " · " + _side_label(_row_color(move))
	position_label.text = prefix + (" · 둔 뒤" if show_after else " · 두기 전") if has_moves else "기보 준비"
	if not line_kind.is_empty():
		position_label.text = prefix + (" · 추천 수순" if line_kind in ["best", "retry_best"] else (" · 내가 고른 수의 응수" if line_kind == "retry_candidate" else " · 실전 수의 응수")) + " %d/%d" % [line_index, maxi(0, _line_frames().size() - 1)]
	if _walkthrough_active:
		position_label.text = "실제 기보 · 시작 위치" if _walkthrough_cursor == 0 else _move_prefix(moves[_walkthrough_cursor - 1]) + " " + str(moves[_walkthrough_cursor - 1].get("san", "")) + " · " + _side_label(_row_color(moves[_walkthrough_cursor - 1]))
		evaluation_label.text = "실제 기보를 따라가는 중 · 별도 평가 없음"
	if retry_active:
		position_label.text = _move_prefix(move) + " " + _side_label(_row_color(move)) + " · 다시 두기"
		evaluation_label.text = "정답과 평가는 잠시 가렸어요"
	var status: String = str(report.get("status", "idle"))
	var game: Dictionary = report.get("game", _preview.get("summary", {}))
	game_label.text = "%s vs %s  ·  %s" % [str(game.get("white", "백")), str(game.get("black", "흑")), str(game.get("result", "*"))] if not game.is_empty() else ""
	annotation_label.visible = not board.move_annotation.is_empty()
	annotation_label.text = ""
	if not board.move_annotation.is_empty():
		var actual: Dictionary = moves[int(board.move_annotation.ply) - 1]
		annotation_label.text = "실제 착수 · " + _move_prefix(actual) + " " + str(actual.get("san", "")) + " · " + str(board.move_annotation.symbol) + " " + str(board.move_annotation.get("label", ""))
		if board.move_annotation.get("kind") == "great":
			annotation_label.text += "\n제한 시간 안에서 다른 대안까지 비교한 탁월 후보"
		annotation_label.tooltip_text = str(board.move_annotation.get("reason", ""))
	annotation_legend.visible = not retry_active
	status_label.text = {"idle": "한 판을 돌아보며 배우기", "running": "중요한 국면을 찾고 있습니다", "completed": "대국 코칭 분석 완료", "cancelled": "분석 중단 · 완료된 수 복기 가능", "error": "분석을 마치지 못했습니다"}.get(status, "대국 코칭")
	error_label.text = _error if not _error.is_empty() else str(report.get("error", "") if report.get("error") != null else "")
	error_label.visible = not error_label.text.is_empty()
	start_button.disabled = busy or running or _retry_in_flight or not _ready_to_start or not has_moves
	start_button.text = "다시 분석" if not report.is_empty() else "코칭 분석 시작"
	cancel_button.disabled = busy or not running
	cancel_button.visible = running
	return_button.disabled = busy
	return_button.text = "분석 중단하고 돌아가기" if running else "돌아가기"
	time_option.disabled = busy or running
	side_option.disabled = busy and report.is_empty()
	var seconds: float = float(moves.size() * time_option.get_selected_id() * 2) / 1000.0
	estimate_label.text = "최선수와 실전 수를 같은 시간으로 비교합니다.\n총 %d수 · 약 %s 예상" % [moves.size(), ("%d분" % int(ceil(seconds / 60.0))) if seconds >= 60 else ("%d초" % int(ceil(seconds)))]
	var progress: Dictionary = report.get("progress", {})
	progress_bar.max_value = maxi(1, int(progress.get("total", moves.size())))
	progress_bar.value = int(progress.get("completed", 0))
	progress_label.text = "%d / %d수 분석" % [int(progress.get("completed", 0)), int(progress.get("total", moves.size()))]
	if report.get("cached", false):
		progress_label.text += " · 저장된 분석"
	summary_label.text = _summary_for_side("white") + "\n\n" + _summary_for_side("black")
	comparison_label.text = prefix + " · " + str(move.get("label", "분석 대기")) if has_moves else "기보를 준비하고 있습니다."
	if best is Dictionary:
		comparison_label.text += "\n최선 후보  " + str(best.get("san", "")) + "  (" + _score(best) + ")"
	if played is Dictionary:
		comparison_label.text += "\n실전의 수  " + str(move.get("san", "")) + "  (" + _score(played) + ")"
	if move.get("loss_cp") != null:
		comparison_label.text += "\n착수한 쪽의 평가 손실  %.2f" % (float(move.loss_cp) / 100.0)
	explanation_label.text = str(move.get("explanation", "이 수의 최선수와 상대의 응수를 계산한 뒤 설명합니다."))
	if _walkthrough_active:
		comparison_label.text = "목표 국면까지 %d수" % maxi(0, _walkthrough_target - _walkthrough_cursor)
		explanation_label.text = "실제로 두었던 수를 순서대로 보여줍니다. 목표 수를 둔 뒤 멈추고, 착수한 기물 위 배지에서 이번 분석의 평가를 확인합니다."
	if line_kind in ["retry_candidate", "retry_best"]:
		comparison_label.text = "다시 두기 · " + ("내가 고른 수" if line_kind == "retry_candidate" else "새 탐색의 추천 수") + "  " + str(_selected_line().get("san", ""))
		explanation_label.text = str(_retry_reply.get("message", ""))
	if retry_active:
		comparison_label.text = "이 차례의 수를 직접 찾아보세요"
		explanation_label.text = "판 위의 기물을 클릭하거나 끌어 움직이세요. 해설 보기를 누르면 추천을 다시 표시합니다."
	before_button.disabled = not has_moves or (not show_after and line_kind.is_empty())
	after_button.disabled = not has_moves or (show_after and line_kind.is_empty())
	best_button.disabled = not best is Dictionary or best.get("pv_frames", []).size() < 2
	played_button.disabled = not played is Dictionary or played.get("pv_frames", []).size() < 2
	line_previous.disabled = line_kind.is_empty() or line_index <= 0
	line_next.disabled = line_kind.is_empty() or line_index >= _line_frames().size() - 1
	line_return.disabled = line_kind.is_empty()
	before_button.disabled = before_button.disabled or _walkthrough_active or retry_active
	after_button.disabled = after_button.disabled or _walkthrough_active or retry_active
	best_button.disabled = best_button.disabled or _walkthrough_active or retry_active
	played_button.disabled = played_button.disabled or _walkthrough_active or retry_active
	walkthrough_label.text = _walkthrough_text()
	walkthrough_pause.visible = _walkthrough_active
	walkthrough_skip.visible = _walkthrough_active
	walkthrough_pause.text = "계속 재생" if _walkthrough_paused else "일시정지"
	var line: Dictionary = _selected_line()
	line_label.text = ""
	if not line_kind.is_empty():
		var words: PackedStringArray = PackedStringArray()
		for word in line.get("pv_san", []):
			words.append(str(word))
		line_label.text = ("추천 수순: " if line_kind in ["best", "retry_best"] else ("내가 고른 수의 응수: " if line_kind == "retry_candidate" else "실전 수 이후 예상 응수: ")) + " ".join(words) + "\n실제 대국과 다른 참고 수순입니다."
	var shown_cursor: int = selected_ply if show_after and line_kind.is_empty() else selected_ply - 1
	first_button.disabled = not has_moves or shown_cursor <= 0
	previous_button.disabled = not has_moves or shown_cursor <= 0
	next_button.disabled = not has_moves or shown_cursor >= moves.size()
	last_button.disabled = not has_moves or (selected_ply >= moves.size() and show_after and line_kind.is_empty())
	if _walkthrough_active:
		first_button.disabled = _walkthrough_cursor <= 0
		previous_button.disabled = _walkthrough_cursor <= 0
		next_button.disabled = _walkthrough_cursor >= moves.size()
		last_button.disabled = _walkthrough_cursor >= moves.size()
	var list_key: String = human_color + JSON.stringify(moves.map(func(row: Dictionary): return [row.get("san"), row.get("label"), row.get("classification")]))
	if list_key != _list_key:
		_list_key = list_key
		move_list.clear()
		for row in moves:
			move_list.add_item(_move_prefix(row) + " " + str(row.get("san", "")) + " · " + _side_label(_row_color(row)) + " · " + str(row.get("label", "분석 대기")))
	if has_moves:
		move_list.select(selected_ply - 1)
	var critical: Array = _critical_rows()
	var critical_key: String = human_color + JSON.stringify(critical.map(func(row: Dictionary): return [row.get("ply"), row.get("label")]))
	if critical_key != _critical_key:
		_critical_key = critical_key
		critical_list.clear()
		for row in critical:
			var index: int = critical_list.add_item(_move_prefix(row) + " " + str(row.get("san", "")) + " · " + _side_label(_row_color(row)) + " · " + str(row.get("label", "")))
			critical_list.set_item_metadata(index, row.ply)
		if critical.is_empty():
			critical_list.add_item("분석된 수에서 해당 실수 없음" if not running else "실수를 찾고 있습니다…")
			critical_list.set_item_disabled(0, true)
	critical_previous.disabled = true
	critical_next.disabled = true
	for row in critical:
		if int(row.ply) < selected_ply:
			critical_previous.disabled = false
		if int(row.ply) > selected_ply:
			critical_next.disabled = false
	critical_previous.disabled = critical_previous.disabled or _walkthrough_active or retry_active
	critical_next.disabled = critical_next.disabled or _walkthrough_active or retry_active
	critical_list.mouse_filter = Control.MOUSE_FILTER_IGNORE if _walkthrough_active else Control.MOUSE_FILTER_STOP
	critical_list.visible = not retry_active
	move_list.visible = not retry_active
	summary_label.visible = not retry_active
	_render_coach()
