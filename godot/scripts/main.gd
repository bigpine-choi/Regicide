extends Control
## The native client never chooses moves or reproduces chess rules.

signal state_changed(revision: int)

const ViewModel = preload("res://scripts/board_view_model.gd")
const ChessBoard = preload("res://scripts/chess_board.gd")
const EvaluationBar = preload("res://scripts/evaluation_bar.gd")

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

var board: ChessBoard
var evaluation_bar: EvaluationBar
var evaluation_summary: Label
var evaluation_detail: Label
var hint_button: Button
var hint_summary: Label
var hint_detail: Label
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
var library_dialog: AcceptDialog
var library_list: ItemList
var library_open_button: Button
var library_detail: Label
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
	var sidebar_scroll := ScrollContainer.new()
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
	hint_button = _button("추천 수: 끔", _on_hint_pressed)
	hint_button.name = "ToggleMoveHint"
	hint_button.tooltip_text = "Regicide가 당신의 다음 수를 추천합니다. 추천만 표시하며 직접 착수하지 않습니다."
	sidebar.add_child(hint_button)
	hint_summary = _label("", 15, "9bd7ca")
	hint_summary.name = "MoveHintSummary"
	hint_summary.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(hint_summary)
	hint_detail = _label("", 12, "a3b6c8")
	hint_detail.name = "MoveHintDetail"
	hint_detail.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(hint_detail)
	error_label = _label("", 13, "ffb4a1")
	error_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(error_label)
	refresh_button = _button("연결 다시 확인", _connect_backend)
	sidebar.add_child(refresh_button)
	library_button = _button("내 대국 기보", _on_library_pressed)
	library_button.name = "GameLibrary"
	library_button.tooltip_text = "자동 보관된 대국을 골라 이전 수순을 다시 봅니다. 현재 대국은 그대로 유지됩니다."
	sidebar.add_child(library_button)
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
	_build_promotion(sidebar)
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
	save_dialog.access = FileDialog.ACCESS_FILESYSTEM
	save_dialog.use_native_dialog = true
	save_dialog.filters = PackedStringArray(["*.pgn ; Chess PGN"])
	save_dialog.current_dir = OS.get_system_dir(OS.SYSTEM_DIR_DOCUMENTS)
	save_dialog.file_selected.connect(_save_pgn_file)
	add_child(save_dialog)
	_build_library_dialog()


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
	library_dialog.title = "내 대국 기보"
	library_dialog.min_size = Vector2i(620, 360)
	library_dialog.size = Vector2i(780, 440)
	library_dialog.unresizable = false
	library_dialog.dialog_hide_on_ok = false
	library_dialog.add_cancel_button("닫기")
	library_dialog.confirmed.connect(_open_selected_record)
	add_child(library_dialog)
	library_open_button = library_dialog.get_ok_button()
	library_open_button.text = "선택한 대국 보기"
	var contents := VBoxContainer.new()
	contents.add_theme_constant_override("separation", 10)
	library_dialog.add_child(contents)
	library_detail = _label("보관된 대국을 불러오고 있습니다.", 14, "a3b6c8")
	library_detail.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	contents.add_child(library_detail)
	library_list = ItemList.new()
	library_list.name = "ArchivedGames"
	library_list.custom_minimum_size = Vector2(580, 260)
	library_list.size_flags_vertical = Control.SIZE_EXPAND_FILL
	library_list.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	library_list.item_selected.connect(_on_library_selection_changed)
	library_list.item_activated.connect(_on_library_item_activated)
	contents.add_child(library_list)
	contents.add_child(_label("대국을 다시 봐도 현재 진행 중인 대국은 바뀌지 않습니다.", 13, "9bd7ca"))


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
	if request_in_flight or think_time_request_in_flight or closing or backend_url.is_empty() or state.get("handicap_mode", false):
		return
	request_in_flight = true
	error_message = ""
	_library_games.clear()
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
		for game in _library_games:
			var description := _record_description(game)
			library_list.add_item(description)
			library_list.set_item_tooltip(library_list.item_count - 1, description)
		var total: int = int(data.get("total", _library_games.size()))
		library_detail.text = "자동 보관된 대국 %d개 · 최근 대국부터 표시합니다." % total if total > 0 else "아직 보관된 대국이 없습니다. 첫 수부터 자동으로 차곡차곡 보관합니다."
		if int(data.get("invalid_count", 0)) > 0:
			library_detail.text += "\n읽을 수 없는 기보 %d개는 원본을 보존하고 목록에서 제외했습니다." % int(data.invalid_count)
		library_detail.tooltip_text = str(data.get("storage_path", ""))
		if not _library_games.is_empty():
			library_list.select(0)
			library_open_button.disabled = false
	else:
		library_detail.text = str(response.get("error", "기보 목록을 불러오지 못했습니다."))
	_render()


func _record_description(game: Dictionary) -> String:
	var date := _record_local_date(str(game.get("started_at", game.get("updated_at", ""))))
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


func _on_library_selection_changed(_index: int) -> void:
	library_open_button.disabled = request_in_flight or library_list.get_selected_items().is_empty()


func _on_library_item_activated(index: int) -> void:
	library_list.select(index)
	_open_selected_record()


func _open_selected_record() -> void:
	var selected := library_list.get_selected_items()
	if request_in_flight or closing or selected.is_empty():
		return
	var index: int = selected[0]
	if index < 0 or index >= _library_games.size():
		return
	var game_id: String = str(_library_games[index].get("game_id", ""))
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
		var detail: Dictionary = response.data
		var frames: Variant = detail.get("frames", [])
		var moves: Variant = detail.get("moves", [])
		if frames is Array and moves is Array and not frames.is_empty() and frames.size() == moves.size() + 1:
			if not review_mode:
				_live_selected_square = board_model.selected_square
				_live_pending_promotions = board_model.pending_promotions.duplicate()
			review_frames = frames.duplicate(true)
			review_moves = moves.duplicate(true)
			review_summary = detail.get("summary", {}).duplicate(true)
			review_pgn = str(detail.get("pgn", ""))
			review_mode = true
			_clear_hint()
			review_ply = 0
			board_model.selected_square = ""
			board_model.pending_promotions.clear()
			_notice_message = ""
			error_message = ""
			library_dialog.hide()
		else:
			library_detail.text = "기보의 수순을 읽지 못했습니다. 다른 대국을 선택해 주세요."
	else:
		library_detail.text = str(response.get("error", "기보를 불러오지 못했습니다."))
	library_open_button.disabled = library_list.get_selected_items().is_empty()
	_render()


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
	if not review_mode or closing or review_frames.is_empty():
		return
	review_ply = clampi(ply, 0, review_frames.size() - 1)
	_render()


func _step_review(change: int) -> void:
	_set_review_ply(review_ply + change)


func _last_review_position() -> void:
	_set_review_ply(review_frames.size() - 1)


func _exit_review() -> void:
	if not review_mode or request_in_flight or closing:
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
	if evaluation_in_flight or offline_mode or review_mode or closing or backend_url.is_empty() or state.is_empty():
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
	return not review_mode and not closing and not request_in_flight and not engine_thinking \
		and not state.is_empty() and state.get("human_to_move", false) \
		and not state.get("handicap_mode", false) and state.get("result", "*") == "*" \
		and state.get("error") == null and not state.get("legal_moves", []).is_empty()


func _clear_hint() -> void:
	# Invalidate already-dispatched polls even if an undo later restores this FEN.
	_hint_epoch += 1
	hint.clear()
	if board != null:
		board.hint_move = ""
		board.queue_redraw()


func accept_hint(value: Dictionary) -> bool:
	if not hint_enabled or not _hint_position_available() or not _evaluation_matches(value, state) \
		or not value.get("enabled", false):
		return false
	var move: String = str(value.get("move", "")) if value.get("move") != null else ""
	if value.get("status", "") == "ready" and not state.get("legal_moves", []).has(move):
		return false
	hint = value.duplicate(true)
	if not _hint_control_failed:
		_hint_connection_error = ""
	_render_hint()
	return true


func _on_hint_pressed() -> void:
	if closing or review_mode or state.is_empty() or backend_url.is_empty():
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
	if not hint_enabled or hint_in_flight or hint_control_in_flight or offline_mode \
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
	hint_button.disabled = closing or review_mode or state.is_empty() or backend_url.is_empty()
	hint_summary.visible = hint_enabled and not review_mode
	hint_detail.visible = hint_enabled and not review_mode
	board.hint_move = ""
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
		var san: String = str(hint.get("san", ""))
		hint_summary.text = "Regicide 추천 · " + (san if not san.is_empty() else move)
		var promotion_text: String = ""
		if move.length() == 5:
			promotion_text = " · %s 승격" % {"q": "퀸", "r": "룩", "b": "비숍", "n": "나이트"}.get(move.substr(4), move.substr(4))
		hint_detail.text = "%s → %s%s\n표시된 수는 직접 선택해서 두세요." % [move.substr(0, 2), move.substr(2, 2), promotion_text]
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
	if request_in_flight or think_time_request_in_flight or state.is_empty() or closing or backend_url.is_empty() or review_mode:
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
	return not state.is_empty() and not closing and not review_mode and not state.get("handicap_mode", false) and not think_time_request_in_flight and not backend_url.is_empty() and (not request_in_flight or engine_thinking)


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
	# Always available, even while waiting for HTTP, promotion or game end.
	board_model.flip_view()
	_render()


func _unhandled_key_input(event: InputEvent) -> void:
	var focus_owner := get_viewport().gui_get_focus_owner()
	if save_dialog.visible or focus_owner is LineEdit or focus_owner is TextEdit:
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
	return not review_mode and (state.get("human_to_move", false) or state.get("handicap_mode", false))


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


func _on_save_pressed() -> void:
	if request_in_flight or state.is_empty() or closing or backend_url.is_empty() or state.get("handicap_mode", false):
		return
	if review_mode:
		_pending_pgn = review_pgn
		save_dialog.current_file = "Regicide-" + str(review_summary.get("game_id", "record")) + ".pgn"
		save_dialog.popup_centered_ratio(0.75)
		return
	request_in_flight = true
	_render()
	var response: Dictionary = await _send_request("/api/pgn")
	request_in_flight = false
	if response.get("ok", false):
		_pending_pgn = response.pgn
		save_dialog.current_file = "Regicide-" + Time.get_datetime_string_from_system().replace(":", "-") + ".pgn"
		save_dialog.popup_centered_ratio(0.75)
	else:
		error_message = str(response.get("error", "기보를 읽지 못했습니다."))
	_render()


func _save_pgn_file(path: String) -> void:
	if not path.to_lower().ends_with(".pgn"):
		path += ".pgn"
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		error_message = "기보 파일을 저장하지 못했습니다. 다른 폴더를 선택해 주세요."
	else:
		file.store_string(_pending_pgn)
		error_message = ""
		_notice_message = "기보를 저장했습니다: " + path.get_file()
	_render()


func _render() -> void:
	if board == null:
		return
	var shown: Dictionary = _display_state()
	board.state = shown
	board.queue_redraw()
	_render_evaluation()
	_render_hint()
	view_label.text = "백 시점 · 백이 아래" if board_model.view_from_white else "흑 시점 · 흑이 아래"
	flip_button.disabled = false
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
	var enabled: bool = ready and not request_in_flight and not think_time_request_in_flight and not review_mode
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
	save_button.disabled = not ready or request_in_flight or replacing or (review_mode and review_pgn.is_empty())
	save_button.text = "이 기보 파일로 저장 (PGN)" if review_mode else "기보 파일로 저장 (PGN)"
	refresh_button.disabled = request_in_flight or think_time_request_in_flight or backend_url.is_empty() or closing or review_mode
	refresh_button.visible = not review_mode and (not error_message.is_empty() or state.is_empty())
	library_button.disabled = request_in_flight or think_time_request_in_flight or backend_url.is_empty() or closing or replacing
	review_controls.visible = review_mode
	if review_mode:
		review_position_label.text = "%d / %d수 · %s" % [review_ply, review_moves.size(), "시작 배치" if review_ply == 0 else str(review_moves[review_ply - 1].get("san", ""))]
		review_first_button.disabled = review_ply == 0
		review_previous_button.disabled = review_ply == 0
		review_next_button.disabled = review_ply >= review_frames.size() - 1
		review_last_button.disabled = review_ply >= review_frames.size() - 1
		review_return_button.disabled = request_in_flight or closing
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
			status_detail.text = "결과 %s · 새 대국을 시작하거나 기보를 저장하세요." % state.get("result", "*")
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
	promotion_panel.visible = not review_mode and not board_model.pending_promotions.is_empty()
	for kind in promotion_buttons:
		var available := false
		for move in board_model.pending_promotions:
			if move.ends_with(str(kind)):
				available = true
		promotion_buttons[kind].disabled = request_in_flight or think_time_request_in_flight or not available
	_render_history()


func _player_text(color_name: String, human_color: String) -> String:
	var text := "백" if color_name == "white" else "흑"
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
		contents = "[table=3]"
		for index in range(0, moves.size(), 2):
			contents += "[cell][color=#8197aa]%d.   [/color][/cell]" % (index / 2 + 1)
			for ply in range(index, index + 2):
				var san: String = str(moves[ply].get("san", "")) if ply < moves.size() else ""
				if ply < moves.size() and moves[ply].get("forced", false):
					san += " [hint=사용자가 Regicide 대신 지정한 수입니다.][color=#ffd578]핸디캡[/color][/hint]"
				if ply == moves.size() - 1:
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
