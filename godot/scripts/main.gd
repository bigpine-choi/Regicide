extends Control
## The native client never chooses moves or reproduces chess rules.

signal state_changed(revision: int)

const ViewModel = preload("res://scripts/board_view_model.gd")
const ChessBoard = preload("res://scripts/chess_board.gd")
const EvaluationBar = preload("res://scripts/evaluation_bar.gd")
const AnalysisMode = preload("res://scripts/analysis_mode.gd")
const CoachingMode = preload("res://scripts/coaching_mode.gd")

var analysis_overlay: AnalysisMode
var analysis_entry_button: Button
var coaching_overlay: CoachingMode
var coaching_entry_button: Button
var main_contents: MarginContainer

var board_model: ViewModel = ViewModel.new()
var state: Dictionary = {}
var request_in_flight: bool = false
var think_time_request_in_flight: bool = false
var engine_thinking: bool = false
var active_think_time_ms: int = 0
var offline_mode: bool = false
var closing: bool = false
var backend_url: String = ""
var backend_token: String = ""
var own_backend: bool = false
var error_message: String = ""
var _pending_pgn: String = ""
var _save_dialog_open: bool = false
var _open_pgn_dialog_open: bool = false
var _last_pgn_path: String = ""
var _rendered_move_count: int = -1
var _notice_message: String = ""
var _handicap_choices_key: String = ""
var review_mode: bool = false
var review_frames: Array = []
var review_moves: Array = []
var review_ply: int = 0
var review_summary: Dictionary = {}
var review_pgn: String = ""
var _live_selected_square: String = ""
var _live_pending_promotions: Array[String] = []
var _library_games: Array = []
var _library_status_note: String = ""
var evaluation: Dictionary = {}
var evaluation_in_flight: bool = false
var hint: Dictionary = {}
var hint_enabled: bool = false
var hint_in_flight: bool = false
var hint_control_in_flight: bool = false
var _hint_epoch: int = 0
var _hint_toggle_version: int = 0
var _hint_sent_version: int = 0
var _hint_connection_error: String = ""
var _hint_control_failed: bool = false
var _opening_render_key: String = ""
var _opening_rows: Array = []
var _opening_selected: int = -1
var _opening_branch_count: int = 0
var _opening_expanded: bool = false
var _opening_source: Dictionary = {}

var board: ChessBoard
var evaluation_bar: EvaluationBar
var evaluation_summary: Label
var evaluation_detail: Label
var hint_button: Button
var hint_summary: Label
var hint_detail: Label
var hint_legend: VBoxContainer
var hint_candidate_labels: Array[Label] = []
var opening_summary: Label
var opening_status: Label
var opening_toggle: Button
var opening_details: VBoxContainer
var opening_branch_list: RichTextLabel
var opening_catalog_note: Label
var opening_preview_title: Label
var opening_preview: RichTextLabel
var opening_source_button: Button
var opening_source_dialog: AcceptDialog
var opening_source_text: RichTextLabel
var flip_button: Button
var view_label: Label
var status_label: Label
var status_detail: Label
var engine_label: Label
var top_player: Label
var bottom_player: Label
var error_label: Label
var move_history: RichTextLabel
var move_count: Label
var color_option: OptionButton
var time_option: OptionButton
var think_time_help: Label
var sidebar_scroll: ScrollContainer
var live_settings: GridContainer
var live_actions: HBoxContainer
var handicap_section: VBoxContainer
var new_button: Button
var undo_button: Button
var handicap_option: OptionButton
var handicap_button: Button
var handicap_cancel_button: Button
var handicap_help: Label
var resign_button: Button
var save_button: Button
var refresh_button: Button
var library_button: Button
var open_pgn_button: Button
var library_dialog: AcceptDialog
var library_list: ItemList
var library_open_button: Button
var library_detail: Label
var library_search: LineEdit
var review_controls: VBoxContainer
var review_position_label: Label
var review_first_button: Button
var review_previous_button: Button
var review_next_button: Button
var review_last_button: Button
var review_return_button: Button
var promotion_panel: PanelContainer
var promotion_buttons: Dictionary = {}
var save_dialog: FileDialog
var open_pgn_dialog: FileDialog
var http: HTTPRequest
var evaluation_http: HTTPRequest
var evaluation_timer: Timer
var hint_http: HTTPRequest
var hint_control_http: HTTPRequest
var hint_timer: Timer
var think_time_http: HTTPRequest


func _ready() -> void:
	get_tree().auto_accept_quit = false
	_install_theme()
	_build_ui()
	_render()
	if not offline_mode:
		_load_backend_arguments()
		if not backend_url.is_empty():
			_connect_backend()
	_capture_if_requested()


func _install_theme() -> void:
	var ui_theme := Theme.new()
	ui_theme.default_font_size = 16
	var font_path := OS.get_system_font_path("Malgun Gothic")
	if font_path.is_empty():
		font_path = OS.get_system_font_path("Noto Sans CJK KR")
	if not font_path.is_empty():
		var system_font := FontFile.new()
		if system_font.load_dynamic_font(font_path) == OK:
			ui_theme.default_font = system_font
	ui_theme.set_color("font_color", "Label", Color("e5edf3"))
	ui_theme.set_color("font_color", "Button", Color("e5edf3"))
	ui_theme.set_color("font_disabled_color", "Button", Color("667b90"))
	for type_name in ["Button", "OptionButton"]:
		for style_name in ["normal", "hover", "pressed", "focus", "disabled"]:
			var style := _panel_style("203347" if style_name == "normal" else "2b485b", 8)
			if style_name == "disabled":
				style.bg_color = Color("132235")
			style.content_margin_left = 15
			style.content_margin_right = 15
			style.content_margin_top = 7
			style.content_margin_bottom = 7
			if style_name == "focus":
				style.bg_color = Color(0, 0, 0, 0)
				style.border_color = Color("80d0c6")
				style.set_border_width_all(2)
			ui_theme.set_stylebox(style_name, type_name, style)
	theme = ui_theme


func _panel_style(color_hex: String, radius: int = 12) -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = Color(color_hex)
	style.set_corner_radius_all(radius)
	return style


func _label(value: String, font_size: int = 16, tint: String = "e5edf3") -> Label:
	var label := Label.new()
	label.text = value
	label.add_theme_font_size_override("font_size", font_size)
	label.add_theme_color_override("font_color", Color(tint))
	return label


func _button(value: String, action: Callable) -> Button:
	var button := Button.new()
	button.text = value
	button.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	button.pressed.connect(action)
	return button


func _build_ui() -> void:
	var background := ColorRect.new()
	background.color = Color("0c1422")
	background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	background.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(background)
	var margins := MarginContainer.new()
	main_contents = margins
	margins.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	for side in ["left", "right", "top", "bottom"]:
		margins.add_theme_constant_override("margin_" + side, 22)
	add_child(margins)
	var page := VBoxContainer.new()
	page.add_theme_constant_override("separation", 15)
	margins.add_child(page)
	var header := HBoxContainer.new()
	header.add_theme_constant_override("separation", 16)
	page.add_child(header)
	var title_box := VBoxContainer.new()
	title_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(title_box)
	title_box.add_child(_label("REGICIDE", 30, "f6f0de"))
	title_box.add_child(_label("내 컴퓨터에서, 자체 엔진과 두는 체스", 14, "8da6bc"))
	var header_right := VBoxContainer.new()
	header.add_child(header_right)
	engine_label = _label("엔진 연결 준비 중", 15, "9bd7ca")
	engine_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	header_right.add_child(engine_label)
	var local_label := _label("LOCAL PRACTICE  ·  기보 자동 저장", 12, "8197aa")
	local_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	header_right.add_child(local_label)
	var body := HBoxContainer.new()
	body.add_theme_constant_override("separation", 20)
	body.size_flags_vertical = Control.SIZE_EXPAND_FILL
	page.add_child(body)
	var board_column := VBoxContainer.new()
	board_column.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	board_column.add_theme_constant_override("separation", 2)
	body.add_child(board_column)
	var board_toolbar := HBoxContainer.new()
	board_toolbar.add_theme_constant_override("separation", 14)
	board_column.add_child(board_toolbar)
	view_label = _label("백 시점 · 백이 아래", 15, "b4c7d3")
	view_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	board_toolbar.add_child(view_label)
	analysis_entry_button = _button("분석 모드", _on_analysis_pressed)
	analysis_entry_button.name = "OpenAnalysis"
	board_toolbar.add_child(analysis_entry_button)
	flip_button = _button("시점 뒤집기  ⇅", _on_flip_pressed)
	flip_button.name = "FlipView"
	flip_button.tooltip_text = "언제든 백·흑 시점을 전환합니다. 내 색과 대국 상태는 그대로입니다. (F)"
	board_toolbar.add_child(flip_button)
	evaluation_summary = _label("형세 분석 대기", 16, "f6f0de")
	evaluation_summary.name = "EvaluationSummary"
	board_column.add_child(evaluation_summary)
	evaluation_detail = _label("현재 체스판의 분석을 기다립니다.", 12, "8197aa")
	evaluation_detail.name = "EvaluationDetail"
	board_column.add_child(evaluation_detail)
	top_player = _label("흑  ·  Regicide", 15, "bccbd8")
	board_column.add_child(top_player)
	var board_row := HBoxContainer.new()
	board_row.add_theme_constant_override("separation", 0)
	board_row.size_flags_vertical = Control.SIZE_EXPAND_FILL
	board_column.add_child(board_row)
	var evaluation_gutter := Control.new()
	evaluation_gutter.custom_minimum_size.x = 40
	board_row.add_child(evaluation_gutter)
	board = ChessBoard.new()
	board.name = "ChessBoard"
	board.model = board_model
	board.square_pressed.connect(_on_square_pressed)
	board.drag_requested.connect(_on_drag_requested)
	board_row.add_child(board)
	# Overlay against the drawn squares, which may be centered inside a wide row.
	evaluation_bar = EvaluationBar.new()
	evaluation_bar.name = "LiveEvaluationBar"
	evaluation_bar.board = board
	board.add_child(evaluation_bar)
	evaluation_bar.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	board.resized.connect(evaluation_bar.queue_redraw)
	bottom_player = _label("백  ·  당신", 15, "bccbd8")
	board_column.add_child(bottom_player)
	board_column.add_child(_label("기물 → 도착 칸 클릭  /  드래그 이동  /  F 키로 시점 전환", 12, "8197aa"))
	var side_panel := PanelContainer.new()
	side_panel.custom_minimum_size.x = 330
	side_panel.add_theme_stylebox_override("panel", _panel_style("132134"))
	body.add_child(side_panel)
	var side_margins := MarginContainer.new()
	for side in ["left", "right", "top", "bottom"]:
		side_margins.add_theme_constant_override("margin_" + side, 14)
	side_panel.add_child(side_margins)
	# The default window keeps the controls visible; a smaller window may scroll.
	sidebar_scroll = ScrollContainer.new()
	sidebar_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	side_margins.add_child(sidebar_scroll)
	var sidebar := VBoxContainer.new()
	sidebar.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	sidebar.size_flags_vertical = Control.SIZE_EXPAND_FILL
	sidebar.add_theme_constant_override("separation", 8)
	sidebar_scroll.add_child(sidebar)
	status_label = _label("체스판을 준비하고 있어요", 18, "f6f0de")
	status_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(status_label)
	status_detail = _label("잠시만 기다려 주세요.", 13, "a3b6c8")
	status_detail.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(status_detail)
	# A required move choice stays above optional learning content.
	_build_promotion(sidebar)
	_build_opening_guide(sidebar)
	hint_button = _button("추천 수: 끔", _on_hint_pressed)
	hint_button.name = "ToggleMoveHint"
	hint_button.tooltip_text = "Regicide가 다음 수를 최대 3개 추천합니다. 1순위 빨강, 2순위 주황, 3순위 노랑입니다. 표시된 수는 직접 선택해서 두세요."
	sidebar.add_child(hint_button)
	hint_summary = _label("", 15, "9bd7ca")
	hint_summary.name = "MoveHintSummary"
	hint_summary.clip_text = true
	hint_summary.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	sidebar.add_child(hint_summary)
	hint_legend = VBoxContainer.new()
	hint_legend.name = "MoveHintCandidates"
	hint_legend.add_theme_constant_override("separation", 2)
	sidebar.add_child(hint_legend)
	for rank in range(1, 4):
		var row := _label("", 13, ChessBoard.HINT_COLORS[rank - 1].to_html(false))
		row.name = "MoveHintRank%d" % rank
		row.custom_minimum_size.y = 20
		row.clip_text = true
		row.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
		hint_legend.add_child(row)
		hint_candidate_labels.append(row)
	hint_detail = _label("", 12, "a3b6c8")
	hint_detail.name = "MoveHintDetail"
	hint_detail.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(hint_detail)
	error_label = _label("", 13, "ffb4a1")
	error_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(error_label)
	refresh_button = _button("연결 다시 확인", _connect_backend)
	sidebar.add_child(refresh_button)
	coaching_entry_button = _button("대국 코칭 · 실수와 최선수 복기", _on_coaching_pressed)
	coaching_entry_button.name = "OpenCoaching"
	coaching_entry_button.tooltip_text = "끝난 대국이나 보관함의 기보에서 실수와 최선수를 살펴봅니다."
	sidebar.add_child(coaching_entry_button)
	library_button = _button("대국 보관함", _on_library_pressed)
	library_button.name = "GameLibrary"
	library_button.tooltip_text = "자동 보관된 대국을 골라 이전 수순을 다시 봅니다. 현재 대국은 그대로 유지됩니다."
	var library_actions := HBoxContainer.new()
	library_actions.add_theme_constant_override("separation", 8)
	sidebar.add_child(library_actions)
	library_button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	library_actions.add_child(library_button)
	open_pgn_button = _button("PGN 파일 열기", _on_open_pgn_pressed)
	open_pgn_button.name = "OpenPGN"
	open_pgn_button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	open_pgn_button.tooltip_text = "저장한 PGN 파일을 읽기 전용으로 다시 봅니다. 현재 대국은 그대로 유지됩니다."
	library_actions.add_child(open_pgn_button)
	_build_review_controls(sidebar)
	sidebar.add_child(HSeparator.new())
	var settings := GridContainer.new()
	live_settings = settings
	settings.columns = 2
	settings.add_theme_constant_override("h_separation", 12)
	settings.add_theme_constant_override("v_separation", 10)
	sidebar.add_child(settings)
	settings.add_child(_label("내 기물", 14, "a3b6c8"))
	color_option = OptionButton.new()
	color_option.add_item("백으로 두기", 0)
	color_option.add_item("흑으로 두기", 1)
	color_option.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	settings.add_child(color_option)
	settings.add_child(_label("한 수 생각 시간", 14, "a3b6c8"))
	time_option = OptionButton.new()
	for option in [["0.1초", 100], ["1초", 1000], ["3초", 3000], ["5초", 5000]]:
		time_option.add_item(option[0], option[1])
	time_option.select(1)
	time_option.name = "ThinkTime"
	time_option.tooltip_text = "대국 중에도 바꿀 수 있습니다. 이미 생각 중인 수는 기존 시간으로 마치고, 다음 수부터 적용합니다."
	time_option.item_selected.connect(_on_think_time_selected)
	settings.add_child(time_option)
	think_time_help = _label("색 선택은 새 대국에, 생각 시간 변경은 현재 대국에 적용합니다.", 12, "9bd7ca")
	think_time_help.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(think_time_help)
	new_button = _button("새 대국 시작", _on_new_game_pressed)
	sidebar.add_child(new_button)
	var actions := HBoxContainer.new()
	live_actions = actions
	actions.add_theme_constant_override("separation", 10)
	sidebar.add_child(actions)
	undo_button = _button("무르기", _on_undo_pressed)
	undo_button.tooltip_text = "내 수와 Regicide의 응답을 함께 되돌려 내 차례로 돌아갑니다."
	undo_button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	actions.add_child(undo_button)
	resign_button = _button("기권", _on_resign_pressed)
	actions.add_child(resign_button)
	_build_handicap(sidebar)
	sidebar.add_child(HSeparator.new())
	var history_title := HBoxContainer.new()
	sidebar.add_child(history_title)
	var history_label := _label("대국 기보", 16)
	history_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	history_title.add_child(history_label)
	move_count = _label("0수", 13, "8197aa")
	history_title.add_child(move_count)
	move_history = RichTextLabel.new()
	move_history.custom_minimum_size.y = 64
	move_history.size_flags_vertical = Control.SIZE_EXPAND_FILL
	move_history.bbcode_enabled = true
	move_history.scroll_following = true
	move_history.selection_enabled = true
	move_history.add_theme_font_size_override("normal_font_size", 16)
	sidebar.add_child(move_history)
	save_button = _button("기보 파일로 저장 (PGN)", _on_save_pressed)
	sidebar.add_child(save_button)
	var footer := _label("시점은 자유롭게 바뀌며, 당신의 색과 차례는 바뀌지 않습니다.\n반복·50수 청구 무승부는 자동 처리합니다.", 12, "8197aa")
	footer.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	page.add_child(footer)
	http = HTTPRequest.new()
	http.timeout = 15.0
	add_child(http)
	# Search updates must arrive while the move request is still waiting.
	evaluation_http = HTTPRequest.new()
	evaluation_http.timeout = 3.0
	add_child(evaluation_http)
	evaluation_timer = Timer.new()
	evaluation_timer.wait_time = 0.2
	evaluation_timer.timeout.connect(_poll_evaluation)
	add_child(evaluation_timer)
	evaluation_timer.start()
	# A slow main-engine response must never prevent the user from disabling hints.
	hint_http = HTTPRequest.new()
	hint_http.timeout = 3.0
	add_child(hint_http)
	hint_control_http = HTTPRequest.new()
	hint_control_http.timeout = 5.0
	add_child(hint_control_http)
	hint_timer = Timer.new()
	hint_timer.wait_time = 0.2
	hint_timer.timeout.connect(_poll_hint)
	add_child(hint_timer)
	hint_timer.start()
	# A settings request can finish while the ordinary client awaits a search.
	think_time_http = HTTPRequest.new()
	think_time_http.timeout = 15.0
	add_child(think_time_http)
	save_dialog = FileDialog.new()
	save_dialog.title = "기보 저장"
	save_dialog.file_mode = FileDialog.FILE_MODE_SAVE_FILE
	save_dialog.title = "기보 저장"
	save_dialog.access = FileDialog.ACCESS_FILESYSTEM
	# Keep focus and modality in the same game window on every desktop.
	# Native choosers can remain open while FileDialog.visible is false.
	save_dialog.use_native_dialog = false
	save_dialog.exclusive = true
	save_dialog.filters = PackedStringArray(["*.pgn ; Chess PGN"])
	save_dialog.current_dir = OS.get_system_dir(OS.SYSTEM_DIR_DOCUMENTS)
	save_dialog.file_selected.connect(_save_pgn_file)
	save_dialog.canceled.connect(_on_save_dialog_canceled)
	add_child(save_dialog)
	save_dialog.get_ok_button().text = "저장"
	save_dialog.get_cancel_button().text = "취소"
	open_pgn_dialog = FileDialog.new()
	open_pgn_dialog.title = "PGN 기보 열기"
	open_pgn_dialog.file_mode = FileDialog.FILE_MODE_OPEN_FILE
	open_pgn_dialog.title = "PGN 기보 열기"
	open_pgn_dialog.access = FileDialog.ACCESS_FILESYSTEM
	open_pgn_dialog.use_native_dialog = false
	open_pgn_dialog.exclusive = true
	open_pgn_dialog.filters = PackedStringArray(["*.pgn ; Chess PGN"])
	open_pgn_dialog.current_dir = OS.get_system_dir(OS.SYSTEM_DIR_DOCUMENTS)
	open_pgn_dialog.file_selected.connect(_open_pgn_file)
	open_pgn_dialog.canceled.connect(_on_open_pgn_dialog_canceled)
	add_child(open_pgn_dialog)
	open_pgn_dialog.get_ok_button().text = "열기"
	open_pgn_dialog.get_cancel_button().text = "취소"
	_build_library_dialog()
	analysis_overlay = AnalysisMode.new()
	analysis_overlay.name = "AnalysisMode"
	analysis_overlay.returned.connect(_on_analysis_returned)
	add_child(analysis_overlay)
	coaching_overlay = CoachingMode.new()
	coaching_overlay.name = "CoachingMode"
	coaching_overlay.returned.connect(_on_coaching_returned)
	add_child(coaching_overlay)


func _analysis_open() -> bool:
	return analysis_overlay != null and analysis_overlay.visible


func _coaching_open() -> bool:
	return coaching_overlay != null and coaching_overlay.visible


func _can_open_coaching() -> bool:
	if _file_dialog_busy() or request_in_flight or think_time_request_in_flight or closing or backend_url.is_empty() or state.get("handicap_mode", false):
		return false
	if review_mode:
		return not review_pgn.is_empty() and not review_moves.is_empty()
	return state.get("result", "*") != "*" and not state.get("moves", []).is_empty()


func _on_coaching_pressed() -> void:
	if not _can_open_coaching():
		return
	var pgn: String = review_pgn
	var color: String = str(review_summary.get("human_color", "white")) if review_mode else str(state.get("human_color", "white"))
	if not review_mode:
		request_in_flight = true
		_render()
		var response: Dictionary = await _send_request("/api/pgn")
		request_in_flight = false
		if closing:
			return
		if not response.get("ok", false):
			error_message = str(response.get("error", "기보를 읽지 못했습니다."))
			_render()
			return
		pgn = str(response.pgn)
	main_contents.hide()
	_clear_hint()
	coaching_overlay.offline_mode = offline_mode
	coaching_overlay.open_game(pgn, color, backend_url, backend_token)


func _on_coaching_returned() -> void:
	main_contents.show()
	if not coaching_overlay.return_warning.is_empty():
		error_message = coaching_overlay.return_warning
	_render()


func _on_analysis_pressed() -> void:
	if _file_dialog_busy() or request_in_flight or think_time_request_in_flight or closing or backend_url.is_empty():
		return
	main_contents.hide()
	_clear_hint()
	analysis_overlay.offline_mode = offline_mode
	analysis_overlay.open_position(str(_display_state().get("fen", "")), backend_url, backend_token)


func _on_analysis_returned() -> void:
	main_contents.show()
	if not analysis_overlay._error.is_empty():
		error_message = analysis_overlay._error
	_render()


func _build_opening_guide(parent: VBoxContainer) -> void:
	var panel := PanelContainer.new()
	panel.name = "OpeningGuide"
	panel.add_theme_stylebox_override("panel", _panel_style("192c3e", 8))
	parent.add_child(panel)
	var margins := MarginContainer.new()
	for side in ["left", "right", "top", "bottom"]:
		margins.add_theme_constant_override("margin_" + side, 8)
	panel.add_child(margins)
	var contents := VBoxContainer.new()
	contents.add_theme_constant_override("separation", 4)
	margins.add_child(contents)
	var heading := HBoxContainer.new()
	contents.add_child(heading)
	var title := _label("오프닝 안내", 15, "f6f0de")
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	heading.add_child(title)
	opening_source_button = _button("출처", _on_opening_source_pressed)
	opening_source_button.name = "OpeningSource"
	opening_source_button.add_theme_font_size_override("font_size", 12)
	heading.add_child(opening_source_button)
	opening_summary = _label("오프닝 자료를 기다립니다.", 14, "9bd7ca")
	opening_summary.name = "OpeningSummary"
	opening_summary.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	contents.add_child(opening_summary)
	opening_status = _label("", 12, "a3b6c8")
	opening_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	contents.add_child(opening_status)
	opening_toggle = _button("다음 분기 보기", _on_opening_toggle_pressed)
	opening_toggle.name = "OpeningContinuations"
	opening_toggle.add_theme_font_size_override("font_size", 12)
	contents.add_child(opening_toggle)
	opening_details = VBoxContainer.new()
	opening_details.add_theme_constant_override("separation", 5)
	contents.add_child(opening_details)
	var help := _label("이름을 누르면 남은 수순을 봅니다.\n대국에는 착수하지 않습니다.", 12, "a3b6c8")
	help.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	opening_details.add_child(help)
	opening_branch_list = RichTextLabel.new()
	opening_branch_list.name = "OpeningBranchList"
	opening_branch_list.custom_minimum_size.y = 145
	opening_branch_list.selection_enabled = true
	opening_branch_list.add_theme_font_size_override("normal_font_size", 13)
	opening_branch_list.meta_clicked.connect(_on_opening_line_selected)
	opening_details.add_child(opening_branch_list)
	opening_catalog_note = _label("", 12, "8197aa")
	opening_catalog_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	opening_details.add_child(opening_catalog_note)
	opening_preview_title = _label("이름을 선택해 수순을 살펴보세요.", 13, "9bd7ca")
	opening_preview_title.name = "OpeningPreviewTitle"
	opening_preview_title.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	opening_details.add_child(opening_preview_title)
	opening_preview = RichTextLabel.new()
	opening_preview.name = "OpeningPreviewSequence"
	opening_preview.custom_minimum_size.y = 72
	opening_preview.selection_enabled = true
	opening_preview.add_theme_font_size_override("normal_font_size", 13)
	opening_details.add_child(opening_preview)
	opening_source_dialog = AcceptDialog.new()
	opening_source_dialog.title = "오프닝 자료 출처"
	opening_source_dialog.min_size = Vector2i(470, 280)
	opening_source_text = RichTextLabel.new()
	opening_source_text.custom_minimum_size = Vector2(450, 240)
	opening_source_text.selection_enabled = true
	opening_source_text.add_theme_font_size_override("normal_font_size", 14)
	opening_source_dialog.add_child(opening_source_text)
	add_child(opening_source_dialog)


func _opening_name(value: Dictionary) -> String:
	var name: String = str(value.get("name", ""))
	var eco: String = str(value.get("eco", ""))
	return name + (" · " + eco if not eco.is_empty() else "")


func _append_opening_line(value: Dictionary, san: String, uci: String, immediate: bool) -> void:
	if str(value.get("name", "")).is_empty():
		return
	var row: Dictionary = value.duplicate(true)
	if immediate:
		row["distance_plies"] = 1
		row["remaining_uci"] = [uci]
		row["remaining_san"] = san
	var key: String = str(row.get("name", "")) + "|" + str(row.get("eco", "")) + "|" + JSON.stringify(row.get("remaining_uci", []))
	for old in _opening_rows:
		if old.get("_key", "") == key:
			# A reachable record carries the catalog's complete numbered SAN,
			# including the move number when this name is only one move away.
			if not immediate and not str(row.get("remaining_san", "")).is_empty():
				old["remaining_san"] = row["remaining_san"]
			return
	row["_key"] = key
	var index: int = _opening_rows.size()
	_opening_rows.append(row)
	opening_branch_list.append_text("  ")
	opening_branch_list.push_color(Color("9bd7ca"))
	opening_branch_list.push_meta(index)
	opening_branch_list.append_text(_opening_name(row))
	opening_branch_list.pop()
	opening_branch_list.pop()
	var distance: int = maxi(1, int(row.get("distance_plies", 1)))
	opening_branch_list.append_text("  · 다음 수\n" if distance == 1 else "  · %d번 착수 뒤\n" % distance)


func _render_opening_guide(shown: Dictionary) -> void:
	if opening_summary == null:
		return
	var candidate: Variant = shown.get("opening_guide", {})
	var guide: Dictionary = candidate if candidate is Dictionary else {}
	var matches: Variant = guide.get("current_matches", [])
	var current: Dictionary = matches[0] if matches is Array and not matches.is_empty() and matches[0] is Dictionary else {}
	var last: Variant = guide.get("matched_opening")
	var status: String = str(guide.get("status", "unavailable"))
	var scope: String = "사용자 시작 위치 · " if guide.get("history_scope", "standard") == "custom" else ""
	if not current.is_empty():
		opening_summary.text = _opening_name(current)
		var kind: String = str(current.get("match_type", "exact"))
		opening_status.text = scope + ("다른 수순으로 같은 위치에 도달했습니다." if kind == "transposition" else "현재 위치가 등록된 이름과 일치합니다." if kind == "position" else "등록된 수순과 일치합니다.")
		var aliases: PackedStringArray = []
		for item in matches:
			if item is Dictionary:
				aliases.append(_opening_name(item))
		opening_summary.tooltip_text = "같은 위치의 등록 이름\n" + "\n".join(aliases)
	elif last is Dictionary and not last.is_empty():
		opening_summary.text = "마지막 확인: " + _opening_name(last)
		opening_summary.tooltip_text = ""
		if status == "in_catalog":
			opening_status.text = scope + "등록된 오프닝으로 이어지는 중간 위치입니다."
		elif status == "beyond_catalog":
			opening_status.text = scope + "현재 위치는 등록 수순 밖입니다."
		else:
			opening_status.text = scope + "현재 위치의 등록 이름은 없습니다."
	else:
		opening_summary.text = "등록된 오프닝 이름 없음" if not guide.is_empty() and guide.get("status", "unavailable") != "unavailable" else "오프닝 자료를 기다립니다."
		opening_summary.tooltip_text = ""
		if status == "in_catalog":
			opening_status.text = scope + "등록된 오프닝으로 이어지는 중간 위치입니다."
		else:
			opening_status.text = scope + ("이 자료에서 이름을 찾지 못했습니다." if not guide.is_empty() and status != "unavailable" else "현재 위치의 안내 자료를 받지 못했습니다.")
	if bool(guide.get("current_matches_truncated", false)):
		opening_status.text += "\n현재 위치의 이름은 일부만 표시합니다."
	var source: Variant = guide.get("source", {})
	_opening_source = source.duplicate(true) if source is Dictionary else {}
	opening_source_button.disabled = _opening_source.is_empty()
	opening_source_text.text = "%s\n\n저장소: %s\n고정 버전: %s\n이용 조건: %s\n\n%s" % [str(_opening_source.get("attribution", "")), str(_opening_source.get("repository_url", "")), str(_opening_source.get("commit", "")), str(_opening_source.get("license", "")), str(guide.get("disclaimer", "등록된 이름과 수순을 살펴보는 학습 안내입니다."))]
	var key: String = JSON.stringify(guide)
	if key != _opening_render_key:
		_opening_render_key = key
		_opening_rows.clear()
		_opening_selected = -1
		opening_branch_list.clear()
		var branches: Variant = guide.get("continuations", [])
		var count: int = 0
		var clipped: bool = bool(guide.get("continuations_truncated", false))
		if branches is Array:
			for branch in branches:
				if not branch is Dictionary:
					continue
				var san: String = str(branch.get("san", ""))
				var uci: String = str(branch.get("uci", ""))
				if san.is_empty() or uci.is_empty():
					continue
				count += 1
				opening_branch_list.push_color(Color("f6f0de"))
				opening_branch_list.append_text(san + "\n")
				opening_branch_list.pop()
				var immediate: Variant = branch.get("immediate_openings", [])
				if immediate is Array:
					for line in immediate:
						if line is Dictionary:
							_append_opening_line(line, san, uci, true)
				var reachable: Variant = branch.get("reachable_openings", [])
				if reachable is Array:
					for line in reachable:
						if line is Dictionary:
							_append_opening_line(line, san, uci, false)
				clipped = clipped or bool(branch.get("immediate_truncated", false)) or bool(branch.get("reachable_truncated", false))
				opening_branch_list.append_text("\n")
		_opening_branch_count = count
		opening_toggle.text = "다음 분기 접기" if _opening_expanded else "다음 분기 보기 (%d개 첫 수)" % _opening_branch_count
		opening_catalog_note.text = "등록된 이름으로 이어지는 수가 없습니다." if count == 0 else "등록 자료의 이름 있는 분기입니다."
		if clipped:
			opening_catalog_note.text += "\n분기·이름 목록을 일부만 표시합니다."
		opening_catalog_note.text += "\n" + str(guide.get("ordering", "SAN 표기순이며 추천 순위가 아닙니다."))
		opening_branch_list.visible = count > 0
		_render_opening_preview()
	opening_details.visible = _opening_expanded


func _render_opening_preview() -> void:
	if _opening_selected < 0 or _opening_selected >= _opening_rows.size():
		opening_preview_title.text = "이름을 선택해 수순을 살펴보세요."
		opening_preview.text = ""
		return
	var row: Dictionary = _opening_rows[_opening_selected]
	opening_preview_title.text = _opening_name(row) + "\n총 %d번의 착수 뒤 이 이름에 도달합니다." % maxi(1, int(row.get("distance_plies", 1)))
	opening_preview.text = str(row.get("remaining_san", "수순 정보 없음"))


func _on_opening_line_selected(meta: Variant) -> void:
	if not meta is int or meta < 0 or meta >= _opening_rows.size():
		return
	_opening_selected = int(meta)
	_render_opening_preview()


func _on_opening_toggle_pressed() -> void:
	_opening_expanded = not _opening_expanded
	opening_details.visible = _opening_expanded
	opening_toggle.text = "다음 분기 접기" if _opening_expanded else "다음 분기 보기 (%d개 첫 수)" % _opening_branch_count


func _on_opening_source_pressed() -> void:
	if not _opening_source.is_empty():
		opening_source_dialog.popup_centered()


func _build_review_controls(parent: VBoxContainer) -> void:
	review_controls = VBoxContainer.new()
	review_controls.name = "RecordReplayControls"
	review_controls.add_theme_constant_override("separation", 5)
	parent.add_child(review_controls)
	review_position_label = _label("처음 위치", 14, "9bd7ca")
	review_position_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	review_controls.add_child(review_position_label)
	var navigation := HBoxContainer.new()
	navigation.add_theme_constant_override("separation", 5)
	review_controls.add_child(navigation)
	review_first_button = _button("처음", _set_review_ply.bind(0))
	review_previous_button = _button("이전", _step_review.bind(-1))
	review_next_button = _button("다음", _step_review.bind(1))
	review_last_button = _button("끝", _last_review_position)
	for button in [review_first_button, review_previous_button, review_next_button, review_last_button]:
		button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		navigation.add_child(button)
	review_return_button = _button("현재 대국으로 돌아가기", _exit_review)
	review_return_button.name = "ReturnToLiveGame"
	review_controls.add_child(review_return_button)


func _build_library_dialog() -> void:
	library_dialog = AcceptDialog.new()
	library_dialog.name = "GameLibraryDialog"
	library_dialog.title = "대국 보관함"
	library_dialog.min_size = Vector2i(620, 560)
	library_dialog.size = Vector2i(780, 600)
	library_dialog.unresizable = false
	library_dialog.dialog_hide_on_ok = false
	var close_button: Button = library_dialog.add_cancel_button("닫기")
	close_button.name = "GameLibraryClose"
	library_dialog.confirmed.connect(_open_selected_record)
	add_child(library_dialog)
	# Wrapped labels briefly report their height at zero width during setup.
	# Let the requested window size own layout instead of latching that height.
	library_dialog.wrap_controls = false
	library_open_button = library_dialog.get_ok_button()
	library_open_button.text = "선택한 대국 보기"
	var contents := VBoxContainer.new()
	contents.name = "GameLibraryContents"
	contents.add_theme_constant_override("separation", 10)
	library_dialog.add_child(contents)
	var detail_scroll := ScrollContainer.new()
	detail_scroll.name = "GameLibraryDetailScroll"
	detail_scroll.custom_minimum_size.y = 108
	detail_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	contents.add_child(detail_scroll)
	library_detail = _label("보관된 대국을 불러오고 있습니다.", 14, "a3b6c8")
	library_detail.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	library_detail.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	detail_scroll.add_child(library_detail)
	library_search = LineEdit.new()
	library_search.name = "GameLibrarySearch"
	library_search.placeholder_text = "날짜·시간·결과 검색 (예: 2026-11-01, 21:30, 백 승)"
	library_search.clear_button_enabled = true
	library_search.text_changed.connect(_filter_library)
	contents.add_child(library_search)
	library_list = ItemList.new()
	library_list.name = "ArchivedGames"
	library_list.custom_minimum_size = Vector2(580, 260)
	library_list.size_flags_vertical = Control.SIZE_EXPAND_FILL
	library_list.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	library_list.item_selected.connect(_on_library_selection_changed)
	library_list.item_activated.connect(_on_library_item_activated)
	contents.add_child(library_list)
	var footer := _label("첫 수부터 자동 저장합니다. 날짜를 골라 열고, 대국 코칭으로 이어서 복기할 수 있습니다.\n현재 진행 중인 대국은 그대로 유지됩니다.", 13, "9bd7ca")
	footer.name = "GameLibraryFooter"
	contents.add_child(footer)


func _build_handicap(parent: VBoxContainer) -> void:
	handicap_section = VBoxContainer.new()
	handicap_section.add_theme_constant_override("separation", 8)
	parent.add_child(handicap_section)
	handicap_section.add_child(HSeparator.new())
	var section := VBoxContainer.new()
	section.add_theme_constant_override("separation", 5)
	handicap_section.add_child(section)
	handicap_option = OptionButton.new()
	handicap_option.name = "HandicapMove"
	handicap_option.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	handicap_option.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	handicap_option.tooltip_text = "바꾸고 싶은 Regicide의 수를 고르세요. 가장 최근 수가 기본 선택됩니다."
	section.add_child(handicap_option)
	handicap_button = _button("Regicide 수 바꾸기", _on_handicap_pressed)
	handicap_button.name = "ReplaceEngineMove"
	handicap_button.tooltip_text = "선택한 수 이후를 되돌리고, Regicide 대신 합법 수 중 한 수를 둡니다."
	section.add_child(handicap_button)
	handicap_cancel_button = _button("수 바꾸기 취소", _on_handicap_cancel_pressed)
	handicap_cancel_button.name = "CancelEngineReplacement"
	handicap_cancel_button.tooltip_text = "수 바꾸기를 시작하기 전의 대국으로 돌아갑니다."
	section.add_child(handicap_cancel_button)
	handicap_help = _label("선택한 수 이후를 되돌리고,\nRegicide 대신 합법 수 중 한 수를 둡니다.", 12, "a3b6c8")
	handicap_help.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	section.add_child(handicap_help)


func _build_promotion(parent: VBoxContainer) -> void:
	promotion_panel = PanelContainer.new()
	promotion_panel.name = "PromotionChoices"
	var style := _panel_style("254747", 8)
	style.content_margin_left = 10
	style.content_margin_right = 10
	style.content_margin_top = 10
	style.content_margin_bottom = 10
	promotion_panel.add_theme_stylebox_override("panel", style)
	parent.add_child(promotion_panel)
	var column := VBoxContainer.new()
	promotion_panel.add_child(column)
	column.add_child(_label("승격할 기물을 고르세요", 14, "c3eee0"))
	var choices := HBoxContainer.new()
	choices.add_theme_constant_override("separation", 6)
	column.add_child(choices)
	for option in [["q", "퀸"], ["r", "룩"], ["b", "비숍"], ["n", "나이트"]]:
		var kind: String = option[0]
		var button := _button(option[1], _on_promotion_pressed.bind(kind))
		button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		promotion_buttons[kind] = button
		choices.add_child(button)
	var cancel := _button("선택 취소", _cancel_promotion)
	column.add_child(cancel)


func _load_backend_arguments() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--own-backend":
			own_backend = true
		if args[index] == "--backend-file" and index + 1 < args.size():
			var file := FileAccess.open(args[index + 1], FileAccess.READ)
			if file == null:
				error_message = "대국 연결 정보를 읽지 못했습니다. 실행 파일로 다시 열어 주세요."
				continue
			var config: Variant = JSON.parse_string(file.get_as_text())
			if config is Dictionary:
				backend_url = str(config.get("url", "")).trim_suffix("/")
				backend_token = str(config.get("token", ""))
	var local_address := RegEx.new()
	local_address.compile("^http://127[.]0[.]0[.]1:[0-9]{1,5}$")
	var port: int = backend_url.get_slice(":", 2).to_int()
	if local_address.search(backend_url) == null or port < 1 or port > 65535 or backend_token.is_empty():
		backend_url = ""
		error_message = "전용 실행 파일로 대국 관리기에 연결해 주세요."
	_render()


func _on_library_pressed() -> void:
	if _file_dialog_busy() or request_in_flight or think_time_request_in_flight or closing or backend_url.is_empty() or state.get("handicap_mode", false):
		return
	request_in_flight = true
	error_message = ""
	_library_games.clear()
	_library_status_note = ""
	library_search.text = ""
	library_list.clear()
	library_open_button.disabled = true
	library_detail.text = "보관된 대국을 불러오고 있습니다."
	library_dialog.popup_centered_ratio(0.72)
	_render()
	var response: Dictionary = await _send_request("/api/games")
	request_in_flight = false
	if response.get("ok", false):
		var data: Dictionary = response.data
		_library_games = data.get("games", [])
		var total: int = int(data.get("total", _library_games.size()))
		_library_status_note = "자동 보관된 대국 %d개 · 최근 대국부터 표시합니다." % total if total > 0 else "아직 보관된 대국이 없습니다. 첫 수부터 자동으로 차곡차곡 보관합니다."
		if int(data.get("invalid_count", 0)) > 0:
			_library_status_note += "\n읽을 수 없는 기보 %d개는 원본을 보존하고 목록에서 제외했습니다." % int(data.invalid_count)
		library_detail.tooltip_text = str(data.get("storage_path", ""))
		_filter_library()
	else:
		library_detail.text = str(response.get("error", "기보 목록을 불러오지 못했습니다."))
	_render()


func _record_description(game: Dictionary) -> String:
	if game.get("source_kind", "") == "pgn":
		return "%s  ·  %d수  ·  %s" % [str(game.get("title", "PGN 파일")), int(game.get("ply_count", 0)), str(game.get("result", "*"))]
	var date := _record_timestamp(game, "started")
	var color_name := "백" if game.get("human_color", "white") == "white" else "흑"
	var result_text: String = str(game.get("result", "*"))
	match result_text:
		"1-0": result_text = "백 승"
		"0-1": result_text = "흑 승"
		"1/2-1/2": result_text = "무승부"
		_: result_text = "진행 중" if game.get("record_status", "") == "in_progress" else "미완료"
	if game.get("record_status", "") == "error":
		result_text = "오류로 중단"
	var handicap: int = int(game.get("handicap_moves", 0))
	return "%s  ·  내 %s  ·  %s  ·  %d수%s" % [date, color_name, result_text,
		int(game.get("ply_count", 0)), "  ·  핸디캡 %d회" % handicap if handicap > 0 else ""]


func _record_local_date(value: String) -> String:
	# Archive timestamps are UTC. Show them in the computer's local time.
	if value.length() < 19 or value[10] != "T":
		return value.replace("T", " ").left(19)
	var utc_seconds: int = Time.get_unix_time_from_datetime_string(value.left(19))
	var bias_minutes: int = int(Time.get_time_zone_from_system().get("bias", 0))
	return Time.get_datetime_string_from_unix_time(utc_seconds + bias_minutes * 60).replace("T", " ")


func _record_timestamp(game: Dictionary, kind: String) -> String:
	var local_key: String = kind + "_at_local"
	if game.has(local_key):
		return str(game[local_key])
	var legacy: String = str(game.get(kind + "_at", "") if game.get(kind + "_at") != null else "")
	return _record_local_date(legacy) if not legacy.is_empty() else "시간 정보 없음"


func _record_details(game: Dictionary) -> String:
	var offset: String = str(game.get("started_at_utc_offset", "") if game.get("started_at_utc_offset") != null else "")
	var local_zone: String = " (UTC" + offset + ")" if not offset.is_empty() else ""
	var end_offset: String = str(game.get("ended_at_utc_offset", "") if game.get("ended_at_utc_offset") != null else "")
	var end_zone: String = " (UTC" + end_offset + ")" if not end_offset.is_empty() else ""
	return _record_description(game) + "\n시작: " + _record_timestamp(game, "started") + local_zone + "\n종료: " + _record_timestamp(game, "ended") + end_zone


func _selected_library_game() -> Dictionary:
	var selected: PackedInt32Array = library_list.get_selected_items()
	if selected.is_empty():
		return {}
	var game_id: String = str(library_list.get_item_metadata(selected[0]))
	for game in _library_games:
		if str(game.get("game_id", "")) == game_id:
			return game
	return {}


func _filter_library(_query: String = "") -> void:
	var selected_id: String = str(_selected_library_game().get("game_id", ""))
	library_list.clear()
	var query: String = library_search.text.strip_edges().to_lower()
	var chosen: int = -1
	for game in _library_games:
		var description: String = _record_description(game)
		var haystack: String = (description + " " + _record_details(game) + " " + str(game.get("status", "")) + " " + str(game.get("game_id", "")) + " " + str(game.get("engine_name", ""))).to_lower()
		if not query.is_empty() and not haystack.contains(query):
			continue
		var index: int = library_list.add_item(description)
		var game_id: String = str(game.get("game_id", ""))
		library_list.set_item_metadata(index, game_id)
		library_list.set_item_tooltip(index, _record_details(game))
		if game_id == selected_id:
			chosen = index
	if library_list.item_count > 0:
		library_list.select(chosen if chosen >= 0 else 0)
	_on_library_selection_changed(chosen)


func _on_library_selection_changed(_index: int) -> void:
	library_open_button.disabled = request_in_flight or library_list.get_selected_items().is_empty()
	var selected: Dictionary = _selected_library_game()
	library_detail.text = _library_status_note
	if not selected.is_empty():
		library_detail.text += "\n" + _record_details(selected)
	elif not library_search.text.strip_edges().is_empty():
		library_detail.text += "\n검색 결과가 없습니다. 날짜나 검색어를 바꿔 주세요."


func _on_library_item_activated(index: int) -> void:
	library_list.select(index)
	_open_selected_record()


func _open_selected_record() -> void:
	var selected := library_list.get_selected_items()
	if _file_dialog_busy() or request_in_flight or closing or selected.is_empty():
		return
	var game_id: String = str(_selected_library_game().get("game_id", ""))
	if game_id.is_empty():
		return
	request_in_flight = true
	library_open_button.disabled = true
	library_detail.text = "선택한 기보를 불러오고 있습니다."
	_render()
	var response: Dictionary = await _send_request("/api/games/" + game_id.uri_encode())
	request_in_flight = false
	# Closing the chooser while it loads cancels presentation only; no game
	# mutation or replay should appear after the user has dismissed it.
	if closing or not library_dialog.visible:
		_render()
		return
	if response.get("ok", false):
		if _adopt_review(response.data):
			library_dialog.hide()
		else:
			library_detail.text = "기보의 수순을 읽지 못했습니다. 다른 대국을 선택해 주세요."
	else:
		library_detail.text = str(response.get("error", "기보를 불러오지 못했습니다."))
	library_open_button.disabled = library_list.get_selected_items().is_empty()
	_render()


func _adopt_review(detail: Dictionary) -> bool:
	var frames: Variant = detail.get("frames", [])
	var moves: Variant = detail.get("moves", [])
	var summary: Variant = detail.get("summary", {})
	if not frames is Array or not moves is Array or not summary is Dictionary or frames.is_empty() or frames.size() != moves.size() + 1:
		return false
	if not review_mode:
		_live_selected_square = board_model.selected_square
		_live_pending_promotions = board_model.pending_promotions.duplicate()
	review_frames = frames.duplicate(true)
	review_moves = moves.duplicate(true)
	review_summary = summary.duplicate(true)
	review_pgn = str(detail.get("pgn", ""))
	review_mode = true
	_clear_hint()
	review_ply = 0
	board_model.selected_square = ""
	board_model.pending_promotions.clear()
	_notice_message = ""
	error_message = ""
	return true


func _display_state() -> Dictionary:
	if not review_mode or review_frames.is_empty():
		return state
	var shown: Dictionary = review_frames[review_ply].duplicate(true)
	shown["moves"] = review_moves.slice(0, review_ply)
	shown["human_color"] = review_summary.get("human_color", "white")
	if not shown.has("engine"):
		var saved_movetime: Variant = review_summary.get("movetime_ms", 1000)
		shown["engine"] = {"name": str(review_summary.get("engine_name", "Regicide")),
			"movetime_ms": int(saved_movetime) if saved_movetime != null else 1000}
	return shown


func _set_review_ply(ply: int) -> void:
	if _file_dialog_busy() or not review_mode or closing or review_frames.is_empty():
		return
	review_ply = clampi(ply, 0, review_frames.size() - 1)
	_render()


func _step_review(change: int) -> void:
	_set_review_ply(review_ply + change)


func _last_review_position() -> void:
	_set_review_ply(review_frames.size() - 1)


func _exit_review() -> void:
	if _file_dialog_busy() or not review_mode or request_in_flight or closing:
		return
	review_mode = false
	review_frames.clear()
	review_moves.clear()
	review_summary.clear()
	review_pgn = ""
	review_ply = 0
	board_model.selected_square = _live_selected_square
	board_model.pending_promotions = _live_pending_promotions.duplicate()
	board_model.apply_state(state)
	_notice_message = "현재 대국으로 돌아왔습니다."
	_render()


func _send_request(path: String, payload: Variant = null, client: HTTPRequest = null) -> Dictionary:
	var requester: HTTPRequest = http if client == null else client
	var headers := PackedStringArray(["X-Regicide-Token: " + backend_token])
	var method := HTTPClient.METHOD_GET
	var contents := ""
	if payload != null:
		method = HTTPClient.METHOD_POST
		headers.append("Content-Type: application/json")
		contents = JSON.stringify(payload)
	var start_error := requester.request(backend_url + path, headers, method, contents)
	if start_error != OK:
		return {"ok": false, "error": "대국 관리기에 요청을 보내지 못했습니다."}
	var response: Array = await requester.request_completed
	if response[0] != HTTPRequest.RESULT_SUCCESS:
		return {"ok": false, "error": "대국 관리기와 연결이 끊겼습니다. 연결을 다시 확인해 주세요."}
	var text: String = response[3].get_string_from_utf8()
	if (path == "/api/pgn" or path.ends_with("/pgn")) and int(response[1]) == 200:
		return {"ok": true, "pgn": text}
	var parsed: Variant = JSON.parse_string(text)
	if not parsed is Dictionary:
		return {"ok": false, "error": "대국 상태를 읽지 못했습니다."}
	if int(response[1]) < 200 or int(response[1]) >= 300:
		return {"ok": false, "error": str(parsed.get("error", "요청을 처리하지 못했습니다.")), "state": parsed.get("state", {})}
	return {"ok": true, "data": parsed}


static func merge_state_versions(current: Dictionary, next: Dictionary) -> Dictionary:
	if current.is_empty():
		return next.duplicate(true)
	if current.get("game_id", "") != next.get("game_id", ""):
		# The host advances board revisions across new games as well. An old
		# game's delayed response must never replace the current live game.
		return next.duplicate(true) if int(next.get("revision", 0)) >= int(current.get("revision", 0)) else current.duplicate(true)
	# Board and settings have independent versions. Neither a delayed search
	# response nor a delayed settings response may roll the other one back.
	var newer_board: bool = int(next.get("revision", 0)) >= int(current.get("revision", 0))
	var merged: Dictionary = next.duplicate(true) if newer_board else current.duplicate(true)
	var old_engine: Dictionary = current.get("engine", {})
	var next_engine: Dictionary = next.get("engine", {})
	var newest_settings: Dictionary = next_engine if int(next_engine.get("settings_revision", 0)) >= int(old_engine.get("settings_revision", 0)) else old_engine
	var merged_engine: Dictionary = merged.get("engine", {}).duplicate(true)
	for key in ["movetime_ms", "settings_revision"]:
		if newest_settings.has(key):
			merged_engine[key] = newest_settings[key]
	merged["engine"] = merged_engine
	return merged


func accept_state(next: Dictionary) -> void:
	state = merge_state_versions(state, next)
	if _hint_toggle_version == 0 and state.has("hint_enabled"):
		hint_enabled = bool(state.hint_enabled)
	if not _evaluation_matches(evaluation, state):
		evaluation.clear()
	if not _evaluation_matches(hint, state) or not _hint_position_available():
		_clear_hint()
	if not review_mode:
		board_model.apply_state(state)
	_render()
	state_changed.emit(int(state.get("revision", 0)))


func _evaluation_matches(value: Dictionary, position: Dictionary) -> bool:
	return not value.is_empty() and not str(position.get("game_id", "")).is_empty() \
		and str(value.get("game_id", "")) == str(position.get("game_id", "")) \
		and int(value.get("revision", -1)) == int(position.get("revision", -2)) \
		and str(value.get("fen", "")) == str(position.get("fen", ""))


func accept_evaluation(value: Dictionary) -> bool:
	# Both a move and a new game can finish before an older GET callback.
	if review_mode or closing or not _evaluation_matches(value, state):
		return false
	evaluation = value.duplicate(true)
	_render_evaluation()
	return true


func _poll_evaluation() -> void:
	if _analysis_open() or _coaching_open() or evaluation_in_flight or offline_mode or review_mode or closing or backend_url.is_empty() or state.is_empty():
		return
	evaluation_in_flight = true
	var requested_position: Dictionary = state.duplicate(true)
	var response: Dictionary = await _send_request("/api/evaluation", null, evaluation_http)
	evaluation_in_flight = false
	if review_mode or closing or not _evaluation_matches(requested_position, state):
		return
	if response.get("ok", false):
		accept_evaluation(response.data)
	else:
		var unavailable: Dictionary = requested_position.duplicate(true)
		unavailable["status"] = "unavailable"
		unavailable["message"] = "형세 분석에 연결할 수 없습니다. 대국은 계속할 수 있습니다."
		accept_evaluation(unavailable)


func _render_evaluation() -> void:
	if evaluation_bar == null:
		return
	var shown: Dictionary = _display_state()
	var presented: Dictionary = {"status": "waiting"}
	if review_mode:
		# Older records contain no analysis. A live score must not leak into replay.
		presented = {"status": "unavailable", "message": "이 기보 위치에는 저장된 평가값이 없습니다."}
		var saved: Variant = shown.get("evaluation")
		if saved is Dictionary and str(saved.get("fen", "")) == str(shown.get("fen", "")):
			presented = saved
	elif shown.get("result", "*") in ["1-0", "0-1", "1/2-1/2"]:
		presented = {"status": "terminal", "source": "result", "result": shown.result}
	elif _evaluation_matches(evaluation, shown):
		presented = evaluation
	evaluation_bar.present(presented, board_model.view_from_white)
	evaluation_summary.text = evaluation_bar.summary_text
	evaluation_detail.text = evaluation_bar.detail_text
	evaluation_summary.tooltip_text = evaluation_bar.tooltip_text
	evaluation_detail.tooltip_text = evaluation_bar.tooltip_text


func _hint_position_available() -> bool:
	return not review_mode and not _analysis_open() and not _coaching_open() and not closing and not request_in_flight and not engine_thinking \
		and not state.is_empty() and state.get("human_to_move", false) \
		and not state.get("handicap_mode", false) and state.get("result", "*") == "*" \
		and state.get("error") == null and not state.get("legal_moves", []).is_empty()


func _clear_hint() -> void:
	# Invalidate already-dispatched polls even if an undo later restores this FEN.
	_hint_epoch += 1
	hint.clear()
	if board != null:
		board.hint_move = ""
		board.hint_moves = []
		board.queue_redraw()


func _valid_hint_candidates(value: Dictionary) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	var raw: Variant = value.get("candidates", [value.merged({"rank": 1}, true)])
	if not raw is Array:
		return result
	var ordered: Array[Dictionary] = []
	for item in raw:
		if not item is Dictionary or typeof(item.get("rank")) not in [TYPE_INT, TYPE_FLOAT]:
			continue
		var rank: int = int(item.rank)
		if rank < 1 or rank > 3 or float(item.rank) != rank:
			continue
		ordered.append(item)
	ordered.sort_custom(func(a: Dictionary, b: Dictionary): return int(a.rank) < int(b.rank))
	var seen: Dictionary = {}
	var ranks: Dictionary = {}
	for item in ordered:
		var move: String = str(item.get("move", ""))
		var rank: int = int(item.rank)
		if seen.has(move) or ranks.has(rank) or not state.get("legal_moves", []).has(move):
			continue
		if item.has("from") and item["from"] != move.substr(0, 2):
			continue
		if item.has("to") and item.to != move.substr(2, 2):
			continue
		var promotion: String = move.substr(4) if move.length() == 5 else ""
		if item.get("promotion") != null and str(item.promotion) != promotion:
			continue
		seen[move] = true
		ranks[rank] = true
		result.append({"rank": rank, "move": move, "san": str(item.get("san", "")),
			"from": move.substr(0, 2), "to": move.substr(2, 2), "promotion": promotion, "depth": item.get("depth", 0)})
	return result


func accept_hint(value: Dictionary) -> bool:
	if not hint_enabled or not _hint_position_available() or not _evaluation_matches(value, state) \
		or not value.get("enabled", false):
		return false
	var candidates: Array[Dictionary] = _valid_hint_candidates(value)
	if value.get("status", "") == "ready" and candidates.is_empty():
		return false
	hint = value.duplicate(true)
	hint["candidates"] = candidates
	if not candidates.is_empty():
		hint.merge(candidates[0], true)
	if not _hint_control_failed:
		_hint_connection_error = ""
	_render_hint()
	return true


func _on_hint_pressed() -> void:
	if _file_dialog_busy() or closing or review_mode or state.is_empty() or backend_url.is_empty():
		return
	hint_enabled = not hint_enabled
	_hint_toggle_version += 1
	_hint_connection_error = ""
	_hint_control_failed = false
	_clear_hint()
	_render_hint()
	_flush_hint_toggle()


func _flush_hint_toggle() -> void:
	if hint_control_in_flight or offline_mode or closing:
		return
	hint_control_in_flight = true
	# Serialize control requests, coalescing rapid clicks to the latest preference.
	# Poll replies have a separate generation barrier and cannot turn hints on.
	while _hint_sent_version != _hint_toggle_version and not closing:
		var version: int = _hint_toggle_version
		var epoch: int = _hint_epoch
		var desired: bool = hint_enabled
		var response: Dictionary = await _send_request("/api/hint", {"enabled": desired}, hint_control_http)
		_hint_sent_version = version
		if version != _hint_toggle_version:
			continue
		if response.get("ok", false):
			_hint_connection_error = ""
			_hint_control_failed = false
			if epoch == _hint_epoch:
				accept_hint(response.data)
		else:
			_hint_control_failed = true
			_hint_connection_error = "추천 수에 연결할 수 없습니다. 버튼을 껐다 켜서 다시 시도하세요."
	hint_control_in_flight = false
	_render_hint()


func _poll_hint() -> void:
	if _analysis_open() or _coaching_open() or not hint_enabled or hint_in_flight or hint_control_in_flight or offline_mode \
		or closing or review_mode or backend_url.is_empty() or state.is_empty():
		return
	hint_in_flight = true
	var epoch: int = _hint_epoch
	var position: Dictionary = state.duplicate(true)
	var response: Dictionary = await _send_request("/api/hint", null, hint_http)
	hint_in_flight = false
	if epoch != _hint_epoch or not hint_enabled or not _evaluation_matches(position, state) \
		or review_mode or closing:
		return
	if response.get("ok", false):
		# Keep a failed toggle visible until it is successfully retried.
		accept_hint(response.data)
	else:
		hint.clear()
		_hint_connection_error = "추천 수에 연결할 수 없습니다. 대국은 계속할 수 있습니다."
	_render_hint()


func _render_hint() -> void:
	if hint_button == null:
		return
	hint_button.text = "추천 수: 켬" if hint_enabled else "추천 수: 끔"
	# Deliberately independent of request_in_flight and engine_thinking.
	hint_button.disabled = _file_dialog_busy() or closing or review_mode or state.is_empty() or backend_url.is_empty()
	hint_summary.visible = hint_enabled and not review_mode
	hint_detail.visible = hint_enabled and not review_mode
	hint_legend.visible = hint_enabled and not review_mode
	for row in hint_candidate_labels:
		row.text = ""
		row.tooltip_text = ""
	board.hint_move = ""
	board.hint_moves = []
	if not hint_enabled or review_mode:
		hint_summary.text = ""
		hint_detail.text = ""
	elif not _hint_connection_error.is_empty():
		hint_summary.text = "Regicide 추천 · 연결 확인 필요"
		hint_detail.text = _hint_connection_error
	elif state.get("result", "*") != "*":
		hint_summary.text = "Regicide 추천 · 대국 종료"
		hint_detail.text = "새 대국에서 다시 추천합니다."
	elif state.get("handicap_mode", false):
		hint_summary.text = "Regicide 추천 · 대기"
		hint_detail.text = "Regicide의 수를 바꾸는 동안 추천을 쉽니다."
	elif not _hint_position_available():
		hint_summary.text = "Regicide 추천 · 대기"
		hint_detail.text = "당신의 차례가 되면 다음 수를 추천합니다."
	elif not _evaluation_matches(hint, state) or hint.get("status", "") in ["waiting", "disabled"]:
		hint_summary.text = "Regicide 추천 · 준비 중"
		hint_detail.text = "현재 위치의 추천 수를 기다리고 있습니다."
	elif hint.get("status", "") == "searching":
		hint_summary.text = "Regicide 추천 · 생각 중"
		hint_detail.text = "현재 위치에서 다음 수를 계산하고 있습니다."
	elif hint.get("status", "") == "ready" and state.get("legal_moves", []).has(hint.get("move")):
		var move: String = str(hint.move)
		board.hint_move = move
		board.hint_moves = _valid_hint_candidates(hint)
		var san: String = str(hint.get("san", ""))
		hint_summary.text = "Regicide 추천 · " + (san if not san.is_empty() else move)
		if board.hint_moves.size() > 1:
			hint_summary.text = "Regicide 추천 · %d개 후보" % board.hint_moves.size()
		for candidate in board.hint_moves:
			var rank: int = int(candidate.rank)
			var label: String = str(candidate.san) if not str(candidate.san).is_empty() else str(candidate.move)
			var promotion_name: String = ""
			if not str(candidate.promotion).is_empty():
				promotion_name = " · %s 승격" % {"q": "퀸", "r": "룩", "b": "비숍", "n": "나이트"}.get(candidate.promotion, candidate.promotion)
			var text: String = "%d  %s  ·  %s → %s%s" % [rank, label, candidate["from"], candidate.to, promotion_name]
			hint_candidate_labels[rank - 1].text = text
			hint_candidate_labels[rank - 1].tooltip_text = text
		var promotion_text: String = ""
		if move.length() == 5:
			promotion_text = " · %s 승격" % {"q": "퀸", "r": "룩", "b": "비숍", "n": "나이트"}.get(move.substr(4), move.substr(4))
		hint_detail.text = "%s → %s%s\n표시된 수는 직접 선택해서 두세요." % [move.substr(0, 2), move.substr(2, 2), promotion_text]
		if board.hint_moves.size() > 1:
			hint_detail.text = "1순위 빨강 · 2순위 주황 · 3순위 노랑\n표시된 수는 직접 선택해서 두세요."
	else:
		hint_summary.text = "Regicide 추천 · 분석 없음"
		hint_detail.text = str(hint.get("message", "추천 수를 얻지 못했습니다. 대국은 계속할 수 있습니다."))
	board.queue_redraw()


func _handle_response(response: Dictionary) -> bool:
	if not response.get("ok", false):
		if response.get("state", {}) is Dictionary and not response.get("state", {}).is_empty():
			accept_state(response.state)
		error_message = str(response.get("error", "요청을 처리하지 못했습니다."))
		return false
	accept_state(response.data)
	return true


func _connect_backend() -> void:
	if request_in_flight or think_time_request_in_flight or backend_url.is_empty() or closing or review_mode:
		return
	request_in_flight = true
	error_message = ""
	_render()
	if _handle_response(await _send_request("/api/state")):
		await _play_engine_if_needed()
	request_in_flight = false
	_render()


func _mutate(path: String, payload: Dictionary = {}) -> void:
	if _file_dialog_busy() or request_in_flight or think_time_request_in_flight or state.is_empty() or closing or backend_url.is_empty() or review_mode:
		return
	request_in_flight = true
	error_message = ""
	_notice_message = ""
	board_model.selected_square = ""
	board_model.pending_promotions.clear()
	_clear_hint()
	# JSON numbers are decoded as floats by Godot. The host requires integers.
	payload["revision"] = int(state.revision)
	_render()
	if _handle_response(await _send_request(path, payload)):
		await _play_engine_if_needed()
	request_in_flight = false
	_render()


func _play_engine_if_needed() -> void:
	if closing or review_mode or state.get("handicap_mode", false) or not state.get("engine_to_move", false) or state.get("result", "*") != "*" or state.get("error") != null:
		return
	engine_thinking = true
	_clear_hint()
	active_think_time_ms = int(state.get("engine", {}).get("movetime_ms", 1000))
	_render()
	_handle_response(await _send_request("/api/engine", {"revision": int(state.revision)}))
	engine_thinking = false
	active_think_time_ms = 0


func _can_change_think_time() -> bool:
	return not _file_dialog_busy() and not state.is_empty() and not closing and not review_mode and not state.get("handicap_mode", false) and not think_time_request_in_flight and not backend_url.is_empty() and (not request_in_flight or engine_thinking)


func _on_think_time_selected(index: int) -> void:
	if not _can_change_think_time() or index < 0 or index >= time_option.item_count:
		_render()
		return
	var requested_ms: int = time_option.get_item_id(index)
	var settings: Dictionary = state.get("engine", {})
	if requested_ms == int(settings.get("movetime_ms", 1000)):
		return
	think_time_request_in_flight = true
	error_message = ""
	_render()
	var response: Dictionary = await _send_request("/api/think-time", {
		"game_id": str(state.get("game_id", "")),
		"settings_revision": int(settings.get("settings_revision", 0)),
		"movetime_ms": requested_ms}, think_time_http)
	if _handle_response(response):
		_notice_message = "생각 시간을 변경했습니다. 다음 수부터 적용합니다."
	think_time_request_in_flight = false
	_render()


func _on_flip_pressed() -> void:
	if _file_dialog_busy():
		return
	if _analysis_open():
		analysis_overlay.flip_view()
		return
	if _coaching_open():
		coaching_overlay.flip_view()
		return
	# Always available, even while waiting for HTTP, promotion or game end.
	board_model.flip_view()
	_render()


func _unhandled_key_input(event: InputEvent) -> void:
	var focus_owner := get_viewport().gui_get_focus_owner()
	if _file_dialog_busy() or save_dialog.visible or open_pgn_dialog.visible or focus_owner is LineEdit or focus_owner is TextEdit:
		return
	if event is InputEventKey and event.pressed and not event.echo and event.keycode == KEY_F:
		_on_flip_pressed()
		get_viewport().set_input_as_handled()


func _on_square_pressed(square: String) -> void:
	if request_in_flight or think_time_request_in_flight or closing or not _can_select_move() or not board_model.pending_promotions.is_empty():
		return
	if square == board_model.selected_square:
		board_model.selected_square = ""
		_render()
		return
	var candidates: Array[String] = []
	if not board_model.selected_square.is_empty():
		for move in state.get("legal_moves", []):
			if str(move).begins_with(board_model.selected_square + square):
				candidates.append(str(move))
	if candidates.size() > 1 or (candidates.size() == 1 and candidates[0].length() == 5):
		board_model.pending_promotions = candidates
		_render()
		return
	if candidates.size() == 1:
		_submit_selected_move(candidates[0])
		return
	board_model.selected_square = ""
	for move in state.get("legal_moves", []):
		if str(move).begins_with(square):
			board_model.selected_square = square
			break
	_render()


func _on_promotion_pressed(kind: String) -> void:
	if review_mode or think_time_request_in_flight:
		return
	for move in board_model.pending_promotions:
		if move.ends_with(kind):
			_submit_selected_move(move)
			return


func _can_select_move() -> bool:
	return not _file_dialog_busy() and not review_mode and (state.get("human_to_move", false) or state.get("handicap_mode", false))


func _submit_selected_move(move: String) -> void:
	_mutate("/api/forced-move" if state.get("handicap_mode", false) else "/api/move", {"move": move})


func _on_drag_requested(from_square: String, to_square: String) -> void:
	if request_in_flight or think_time_request_in_flight or closing or not _can_select_move() or not board_model.pending_promotions.is_empty():
		return
	# Re-select the logical origin even when pressing an already selected piece
	# toggled its click selection off. A flipped view still supplies the same UCI.
	for move in state.get("legal_moves", []):
		if str(move).begins_with(from_square):
			board_model.selected_square = from_square
			_on_square_pressed(to_square)
			return


func _cancel_promotion() -> void:
	board_model.pending_promotions.clear()
	_render()


func _on_new_game_pressed() -> void:
	_mutate("/api/new", {"color": "white" if color_option.selected == 0 else "black",
		"movetime_ms": time_option.get_selected_id()})


func _on_undo_pressed() -> void:
	_mutate("/api/undo")


func _on_handicap_pressed() -> void:
	if not state.get("can_handicap", false) or state.get("handicap_mode", false) or handicap_option.selected < 0:
		return
	var ply: int = handicap_option.get_selected_id()
	if ply > 0:
		_mutate("/api/handicap", {"ply": ply})


func _on_handicap_cancel_pressed() -> void:
	if state.get("handicap_mode", false):
		_mutate("/api/handicap-cancel")


func _on_resign_pressed() -> void:
	_mutate("/api/resign")


func _file_dialog_busy() -> bool:
	return _save_dialog_open or _open_pgn_dialog_open


func _on_save_pressed() -> void:
	if _file_dialog_busy() or request_in_flight or think_time_request_in_flight or state.is_empty() or closing or backend_url.is_empty() or state.get("handicap_mode", false):
		return
	_save_dialog_open = true
	_notice_message = ""
	error_message = ""
	if review_mode:
		_pending_pgn = review_pgn
		save_dialog.current_file = "Regicide-" + str(review_summary.get("game_id", "record")) + ".pgn"
		save_dialog.popup_centered_ratio(0.75)
		_render()
		return
	request_in_flight = true
	_render()
	var response: Dictionary = await _send_request("/api/pgn")
	request_in_flight = false
	if closing:
		_save_dialog_open = false
		_pending_pgn = ""
		return
	if response.get("ok", false):
		_pending_pgn = response.pgn
		save_dialog.current_file = "Regicide-" + Time.get_datetime_string_from_system().replace(":", "-") + ".pgn"
		save_dialog.popup_centered_ratio(0.75)
	else:
		_save_dialog_open = false
		_pending_pgn = ""
		error_message = str(response.get("error", "기보를 읽지 못했습니다."))
	_render()


func _on_save_dialog_canceled() -> void:
	_save_dialog_open = false
	_pending_pgn = ""
	_notice_message = "기보 저장을 취소했습니다."
	_render()


func _save_pgn_file(path: String) -> void:
	if not _save_dialog_open or _pending_pgn.is_empty():
		return
	var contents: PackedByteArray = _pending_pgn.to_utf8_buffer()
	_save_dialog_open = false
	_pending_pgn = ""
	save_dialog.hide()
	_notice_message = ""
	if not path.to_lower().ends_with(".pgn"):
		path += ".pgn"
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		error_message = "기보 파일을 저장하지 못했습니다. 다른 폴더를 선택해 주세요."
	else:
		file.store_buffer(contents)
		file.flush()
		var write_ok: bool = file.get_error() == OK and file.get_length() == contents.size()
		file.close()
		# Confirm the bytes after closing before telling the player it is saved.
		if write_ok and FileAccess.get_file_as_bytes(path) == contents:
			error_message = ""
			_last_pgn_path = path
			save_dialog.current_dir = path.get_base_dir()
			_notice_message = "기보를 저장했습니다: " + path.get_file()
		else:
			error_message = "기보 파일을 끝까지 저장하지 못했습니다. 다른 폴더에서 다시 저장해 주세요."
	_render()


func _on_open_pgn_pressed() -> void:
	if _file_dialog_busy() or request_in_flight or think_time_request_in_flight or closing or state.is_empty() or backend_url.is_empty() or state.get("handicap_mode", false):
		return
	_open_pgn_dialog_open = true
	_notice_message = ""
	error_message = ""
	if not _last_pgn_path.is_empty() and FileAccess.file_exists(_last_pgn_path):
		open_pgn_dialog.current_dir = _last_pgn_path.get_base_dir()
	# An auto-selected current_path can leave Godot's Open button disabled.
	# Start with the recent folder, then let the user explicitly select a file.
	open_pgn_dialog.current_file = ""
	open_pgn_dialog.popup_centered_ratio(0.75)
	_render()


func _on_open_pgn_dialog_canceled() -> void:
	_open_pgn_dialog_open = false
	_notice_message = "기보 열기를 취소했습니다."
	_render()


func _open_pgn_file(path: String) -> void:
	if not _open_pgn_dialog_open or closing:
		return
	_open_pgn_dialog_open = false
	open_pgn_dialog.hide()
	_notice_message = ""
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		error_message = "기보 파일을 읽지 못했습니다. 파일과 폴더를 확인해 주세요."
		_render()
		return
	var length: int = file.get_length()
	if length == 0 or length > 1048576:
		file.close()
		error_message = "비어 있지 않은 1MB 이하의 PGN 파일을 선택해 주세요."
		_render()
		return
	var bytes: PackedByteArray = file.get_buffer(length)
	file.close()
	var text: String = bytes.get_string_from_utf8()
	# Godot strips the optional UTF-8 BOM. Keep it for byte-exact re-export.
	if bytes.size() >= 3 and bytes[0] == 0xef and bytes[1] == 0xbb and bytes[2] == 0xbf:
		text = String.chr(0xfeff) + text
	if bytes.size() != length or text.to_utf8_buffer() != bytes:
		error_message = "UTF-8 형식의 PGN 파일을 선택해 주세요."
		_render()
		return
	request_in_flight = true
	_render()
	var response: Dictionary = await _send_request("/api/pgn-preview", {"pgn": text})
	request_in_flight = false
	if closing:
		return
	if response.get("ok", false) and _adopt_review(response.data):
		_last_pgn_path = path
		review_summary["title"] = path.get_file()
		_notice_message = "PGN 파일을 열었습니다. 현재 대국은 그대로 유지됩니다."
	else:
		error_message = str(response.get("error", "기보의 수순을 읽지 못했습니다."))
	_render()


func _render() -> void:
	if board == null:
		return
	if analysis_entry_button != null:
		analysis_entry_button.disabled = _file_dialog_busy() or request_in_flight or think_time_request_in_flight or closing or backend_url.is_empty()
	if coaching_entry_button != null:
		coaching_entry_button.disabled = not _can_open_coaching()
	var shown: Dictionary = _display_state()
	board.state = shown
	board.queue_redraw()
	_render_evaluation()
	_render_hint()
	_render_opening_guide(shown)
	view_label.text = "백 시점 · 백이 아래" if board_model.view_from_white else "흑 시점 · 흑이 아래"
	flip_button.disabled = _file_dialog_busy()
	var human_color: String = str(shown.get("human_color", "white"))
	var bottom_color: String = "white" if board_model.view_from_white else "black"
	var upper_color: String = "black" if board_model.view_from_white else "white"
	top_player.text = _player_text(upper_color, human_color)
	bottom_player.text = _player_text(bottom_color, human_color)
	var engine: Dictionary = shown.get("engine", {})
	engine_label.text = str(engine.get("name", "엔진 연결 준비 중"))
	if not engine.is_empty():
		engine_label.tooltip_text = "실행 파일 SHA-256\n" + str(engine.get("binary_sha256", ""))
	var ready: bool = not state.is_empty() and not closing
	var active: bool = ready and state.get("result", "*") == "*" and state.get("error") == null
	var enabled: bool = ready and not _file_dialog_busy() and not request_in_flight and not think_time_request_in_flight and not review_mode
	var replacing: bool = state.get("handicap_mode", false)
	live_settings.visible = not review_mode
	live_actions.visible = not review_mode
	handicap_section.visible = not review_mode
	new_button.visible = not review_mode
	new_button.disabled = not enabled or replacing
	color_option.disabled = not enabled or replacing
	time_option.disabled = not _can_change_think_time()
	if not think_time_request_in_flight:
		var time_index: int = time_option.get_item_index(int(engine.get("movetime_ms", 1000)))
		if time_index >= 0:
			time_option.select(time_index)
	think_time_help.visible = not review_mode
	var thinking_now: bool = engine_thinking or bool(state.get("engine_thinking", false))
	if thinking_now:
		var current_ms: int = active_think_time_ms
		if engine.get("active_movetime_ms") != null:
			current_ms = int(engine.active_movetime_ms)
		think_time_help.text = "이번 수 %s초 · 다음 수부터 %s초\n생각 중에도 변경할 수 있습니다." % [str(float(current_ms) / 1000.0), str(float(engine.get("movetime_ms", 1000)) / 1000.0)]
	else:
		think_time_help.text = "Regicide는 한 수에 %s초 생각합니다.\n시간은 현재 대국에, 색 선택은 새 대국에 적용합니다." % str(float(engine.get("movetime_ms", 1000)) / 1000.0)
	if think_time_request_in_flight:
		think_time_help.text += "\n변경한 시간을 저장하고 있습니다."
	undo_button.disabled = not enabled or replacing or not state.get("can_undo", false)
	resign_button.disabled = not enabled or not active or replacing
	save_button.disabled = not ready or _file_dialog_busy() or request_in_flight or think_time_request_in_flight or replacing or (review_mode and review_pgn.is_empty())
	open_pgn_button.disabled = not ready or _file_dialog_busy() or request_in_flight or think_time_request_in_flight or replacing or backend_url.is_empty()
	save_button.text = "이 기보 파일로 저장 (PGN)" if review_mode else "기보 파일로 저장 (PGN)"
	refresh_button.disabled = request_in_flight or think_time_request_in_flight or backend_url.is_empty() or closing or review_mode
	refresh_button.visible = not review_mode and (not error_message.is_empty() or state.is_empty())
	library_button.disabled = _file_dialog_busy() or request_in_flight or think_time_request_in_flight or backend_url.is_empty() or closing or replacing
	review_controls.visible = review_mode
	if review_mode:
		review_position_label.text = "%d / %d수 · %s" % [review_ply, review_moves.size(), "시작 배치" if review_ply == 0 else str(review_moves[review_ply - 1].get("san", ""))]
		review_first_button.disabled = _file_dialog_busy() or review_ply == 0
		review_previous_button.disabled = _file_dialog_busy() or review_ply == 0
		review_next_button.disabled = _file_dialog_busy() or review_ply >= review_frames.size() - 1
		review_last_button.disabled = _file_dialog_busy() or review_ply >= review_frames.size() - 1
		review_return_button.disabled = _file_dialog_busy() or request_in_flight or closing
	_render_handicap(enabled)
	error_label.text = error_message if not error_message.is_empty() else str(state.get("error", "") if state.get("error") != null else "")
	error_label.visible = not error_label.text.is_empty()
	if closing:
		status_label.text = "대국을 저장하고 종료합니다"
		status_detail.text = "진행한 수는 자동 저장돼 있습니다."
	elif review_mode:
		status_label.text = "기보 다시 보기 · 읽기 전용"
		status_detail.text = _record_description(review_summary) + "\n처음·이전·다음·끝으로 수순을 살펴보세요."
	elif thinking_now:
		status_label.text = "Regicide가 생각하고 있어요"
		status_detail.text = "기다리는 동안에도 시점을 자유롭게 뒤집어 볼 수 있습니다."
	elif not ready:
		status_label.text = "체스판을 준비하고 있어요"
		status_detail.text = "대국 관리기와 연결 중입니다."
	else:
		status_label.text = str(state.get("status", "대국 중"))
		if not active:
			status_detail.text = "결과 %s · 대국 코칭에서 실수와 최선수를 복기할 수 있습니다." % state.get("result", "*")
		elif not board_model.pending_promotions.is_empty():
			status_detail.text = "승격할 기물을 선택하면 수가 적용됩니다."
		elif not board_model.selected_square.is_empty():
			status_detail.text = "%s 선택 · 표시된 칸으로 이동하세요." % board_model.selected_square
		elif replacing:
			status_detail.text = "Regicide의 기물과 도착 칸을 선택하세요.\n이 수부터 새 기보로 이어집니다."
		else:
			var color_name := "백" if human_color == "white" else "흑"
			status_detail.text = "당신은 %s · 한 수에 %s초 생각합니다." % [color_name, str(float(engine.get("movetime_ms", 1000)) / 1000.0)]
	if not _notice_message.is_empty():
		status_detail.text += "\n" + _notice_message
	var promotion_was_visible: bool = promotion_panel.visible
	promotion_panel.visible = not review_mode and not board_model.pending_promotions.is_empty()
	if promotion_panel.visible and not promotion_was_visible:
		_reveal_promotion()
	for kind in promotion_buttons:
		var available := false
		for move in board_model.pending_promotions:
			if move.ends_with(str(kind)):
				available = true
		promotion_buttons[kind].disabled = request_in_flight or think_time_request_in_flight or not available
	_render_history()


func _reveal_promotion() -> void:
	await get_tree().process_frame
	if not closing and promotion_panel.visible:
		sidebar_scroll.ensure_control_visible(promotion_panel)


func _player_text(color_name: String, human_color: String) -> String:
	var text := "백" if color_name == "white" else "흑"
	if review_mode and review_summary.get("source_kind", "") == "pgn":
		text += "  ·  " + str(review_summary.get(color_name, "?"))
	else:
		text += "  ·  당신" if color_name == human_color else "  ·  Regicide"
	var shown: Dictionary = _display_state()
	if shown.get("result", "*") == "*" and shown.get("turn", "") == color_name:
		text += "   ● 차례"
	return text


func _render_handicap(enabled: bool) -> void:
	var choices: Array = []
	for move in state.get("moves", []):
		if move.get("actor", "") == "engine":
			choices.append({"ply": int(move.get("ply", 0)), "san": str(move.get("san", "")),
				"forced": bool(move.get("forced", false))})
	var key := JSON.stringify(choices)
	if key != _handicap_choices_key or handicap_option.item_count == 0:
		var previous_ply: int = handicap_option.get_selected_id() if handicap_option.selected >= 0 else -1
		var prior_count: int = handicap_option.item_count
		handicap_option.clear()
		for move in choices:
			var move_number: int = (int(move.ply) + 1) / 2
			var color_name := "백" if int(move.ply) % 2 == 1 else "흑"
			var description: String = "%d수 %s · %s%s" % [move_number, color_name, move.san, " (핸디캡)" if move.forced else ""]
			handicap_option.add_item(description, int(move.ply))
		if choices.is_empty():
			handicap_option.add_item("바꿀 Regicide 수가 없습니다", -1)
			handicap_option.set_item_disabled(0, true)
		else:
			var previous_index: int = handicap_option.get_item_index(previous_ply)
			# New engine replies become the default target; view-only renders never
			# reset an explicitly chosen earlier move.
			handicap_option.select(choices.size() - 1 if choices.size() > prior_count or previous_index < 0 else previous_index)
		_handicap_choices_key = key
	var replacing: bool = state.get("handicap_mode", false)
	handicap_option.visible = not replacing
	handicap_option.disabled = not enabled or not state.get("can_handicap", false) or choices.is_empty()
	handicap_button.visible = not replacing
	handicap_button.disabled = not enabled or not state.get("can_handicap", false) or choices.is_empty()
	handicap_cancel_button.visible = replacing
	handicap_cancel_button.disabled = not enabled
	handicap_help.text = "Regicide 대신 합법 수를 직접 두는 중입니다.\n취소하면 원래 대국을 복원합니다." if replacing else "선택한 수 이후를 되돌리고,\nRegicide 대신 합법 수 중 한 수를 둡니다."


func _render_history() -> void:
	var moves: Array = _display_state().get("moves", [])
	move_count.text = "%d수" % moves.size()
	var contents := ""
	if moves.is_empty():
		contents = "[color=#8197aa]시작 배치입니다. 다음 수를 눌러 살펴보세요.[/color]" if review_mode else "[color=#8197aa]첫 수를 기다리고 있습니다.[/color]"
	else:
		var first_fullmove: int = 1
		var first_column: int = 0
		if review_mode:
			var initial_fens: Array = [review_summary.get("initial_fen", "")]
			if not review_frames.is_empty():
				initial_fens.append(review_frames[0].get("fen", ""))
			for initial_fen in initial_fens:
				var fields: PackedStringArray = str(initial_fen).split(" ", false)
				if fields.size() >= 6 and fields[1] in ["w", "b"] and fields[5].is_valid_int() and int(fields[5]) > 0:
					first_fullmove = int(fields[5])
					first_column = 1 if fields[1] == "b" else 0
					break
		contents = "[table=3]"
		for index in range(0, moves.size() + first_column, 2):
			contents += "[cell][color=#8197aa]%d.   [/color][/cell]" % (first_fullmove + index / 2)
			for column in range(2):
				var ply: int = index + column - first_column
				var has_move: bool = ply >= 0 and ply < moves.size()
				var san: String = str(moves[ply].get("san", "")) if has_move else ""
				if has_move and moves[ply].get("forced", false):
					san += " [hint=사용자가 Regicide 대신 지정한 수입니다.][color=#ffd578]핸디캡[/color][/hint]"
				if has_move and ply == moves.size() - 1:
					san = "[color=#98dfcb]" + san + "[/color]"
				contents += "[cell]" + san + "     [/cell]"
		contents += "[/table]"
	if move_history.text != contents:
		move_history.text = contents
		if moves.size() != _rendered_move_count:
			move_history.scroll_to_line(move_history.get_line_count())
	_rendered_move_count = moves.size()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST and not closing:
		_close_app()


func _close_app() -> void:
	closing = true
	_render()
	# Both clients may have a response pending; let them finish before closing
	# or asking an owned backend for its final board revision.
	var finish_deadline: int = Time.get_ticks_msec() + 20000
	while (request_in_flight or think_time_request_in_flight) and Time.get_ticks_msec() < finish_deadline:
		await get_tree().process_frame
	if own_backend and not backend_url.is_empty():
		# A separate request then closes the host using the latest revision.
		var shutdown_client := HTTPRequest.new()
		shutdown_client.timeout = 12.0
		add_child(shutdown_client)
		var current: Dictionary = await _send_request("/api/state", null, shutdown_client)
		if current.get("ok", false):
			await _send_request("/api/shutdown", {"revision": int(current.data.revision)}, shutdown_client)
	get_tree().quit()


func _capture_if_requested() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--screenshot" and index + 1 < args.size():
			await get_tree().create_timer(2.0).timeout
			await RenderingServer.frame_post_draw
			var image := get_viewport().get_texture().get_image()
			var destination: String = args[index + 1]
			DirAccess.make_dir_recursive_absolute(destination.get_base_dir())
			image.save_png(destination)
