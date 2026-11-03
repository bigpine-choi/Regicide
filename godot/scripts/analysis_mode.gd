extends Control
## Position editing is presentation only; the host validates and chooses moves.

signal returned

const AnalysisBoard = preload("res://scripts/analysis_board.gd")
const EvaluationBar = preload("res://scripts/evaluation_bar.gd")
const ViewModel = preload("res://scripts/board_view_model.gd")
const OpeningExplorer = preload("res://scripts/opening_explorer.gd")
const START_FEN := "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

var backend_url: String = ""
var backend_token: String = ""
var offline_mode: bool = false
var editing: bool = true
var busy: bool = false
var analysis_state: Dictionary = {}
var editor_pieces: Dictionary = {}
var editor_fields: PackedStringArray = PackedStringArray(["w", "-", "-", "0", "1"])
var selected_piece: String = "P"
var source_fen: String = START_FEN
var model: ViewModel = ViewModel.new()
var board: AnalysisBoard
var evaluation_bar: EvaluationBar
var evaluation_summary: Label
var evaluation_detail: Label
var requester: HTTPRequest
var poll_requester: HTTPRequest
var poll_timer: Timer
var editor_controls: VBoxContainer
var play_controls: VBoxContainer
var fen_input: LineEdit
var turn_option: OptionButton
var time_option: OptionButton
var status_label: Label
var recommendation: Label
var recommendation_detail: Label
var error_label: Label
var selected_label: Label
var turn_label: Label
var history: Label
var start_button: Button
var apply_button: Button
var undo_button: Button
var edit_button: Button
var return_button: Button
var promotion_row: HBoxContainer
var palette_buttons: Dictionary = {}
var castling_checks: Dictionary = {}
var castling_buttons: Dictionary = {}
var castle_actions: HFlowContainer
var castle_reason: Label
var trash_area: PanelContainer
var _editor_revision: int = 0
var _editor_drag: Dictionary = {}
var _editor_drag_cancelled: bool = false
var _editor_drag_sequence: int = 0
var _epoch: int = 0
var _mutation_epoch: int = 0
var _poll_in_flight: bool = false
var _state_ready: bool = false
var _error: String = ""
var opening_explorer: OpeningExplorer
var sidebar_tabs: TabBar
var sidebar_scroll: ScrollContainer
var sidebar_pages: Array[VBoxContainer] = []
var quick_actions: GridContainer
var _sidebar_active: int = 0
var _sidebar_scroll_positions: Array[int] = [0, 0]


func _ready() -> void:
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_build_ui()
	_set_editor_fen(START_FEN)
	hide()


func _label(text: String, font_size: int = 15, color: String = "e5edf3") -> Label:
	var item := Label.new()
	item.text = text
	item.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	item.add_theme_font_size_override("font_size", font_size)
	item.add_theme_color_override("font_color", Color(color))
	return item


func _button(text: String, action: Callable, node_name: String = "") -> Button:
	var item := Button.new()
	item.text = text
	item.name = node_name if not node_name.is_empty() else text
	item.pressed.connect(action)
	item.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	return item


func _build_ui() -> void:
	var background := ColorRect.new()
	background.color = Color("0c1422")
	background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	add_child(background)
	var margin := MarginContainer.new()
	margin.name = "AnalysisMargins"
	margin.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	for side in ["left", "right", "top", "bottom"]:
		margin.add_theme_constant_override("margin_" + side, 20)
	add_child(margin)
	var page := VBoxContainer.new()
	page.name = "AnalysisPage"
	page.add_theme_constant_override("separation", 10)
	margin.add_child(page)
	var header := HBoxContainer.new()
	header.name = "AnalysisHeader"
	page.add_child(header)
	var title := _label("REGICIDE  ·  분석 모드", 25, "f6f0de")
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(title)
	return_button = _button("대국으로 돌아가기", _return_to_game, "AnalysisReturn")
	header.add_child(return_button)
	var body := HBoxContainer.new()
	body.name = "AnalysisBody"
	body.size_flags_vertical = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 16)
	page.add_child(body)
	var board_column := VBoxContainer.new()
	board_column.name = "AnalysisBoardColumn"
	board_column.add_theme_constant_override("separation", 8)
	board_column.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	body.add_child(board_column)
	var toolbar := HBoxContainer.new()
	toolbar.name = "AnalysisBoardToolbar"
	toolbar.custom_minimum_size.y = 36
	board_column.add_child(toolbar)
	turn_label = _label("기물 배치 편집", 16, "9bd7ca")
	turn_label.name = "AnalysisBoardTurn"
	turn_label.autowrap_mode = TextServer.AUTOWRAP_OFF
	turn_label.clip_text = true
	turn_label.max_lines_visible = 1
	turn_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	turn_label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	turn_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	toolbar.add_child(turn_label)
	toolbar.add_child(_button("시점 뒤집기 ⇅", flip_view, "AnalysisFlip"))
	castle_actions = HFlowContainer.new()
	castle_actions.name = "AnalysisCastlingActions"
	for item in [["king", "킹사이드 캐슬링 O-O"], ["queen", "퀸사이드 캐슬링 O-O-O"]]:
		var castle_button := _button(str(item[1]), _castle.bind(str(item[0])), "AnalysisCastle" + str(item[0]))
		castle_button.add_theme_font_size_override("font_size", 14)
		castle_actions.add_child(castle_button)
		castling_buttons[item[0]] = castle_button
	castle_reason = _label("", 12, "a3b6c8")
	castle_reason.name = "AnalysisCastlingReason"
	evaluation_summary = _label("형세 분석 대기", 18, "f6f0de")
	evaluation_summary.name = "AnalysisEvaluationSummary"
	evaluation_summary.autowrap_mode = TextServer.AUTOWRAP_OFF
	evaluation_summary.clip_text = true
	evaluation_summary.max_lines_visible = 1
	evaluation_summary.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	evaluation_summary.custom_minimum_size.y = 26
	evaluation_summary.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	board_column.add_child(evaluation_summary)
	evaluation_detail = _label("+는 백, −는 흑 우세 · 자체 엔진의 추정 형세입니다.", 12, "a3b6c8")
	evaluation_detail.name = "AnalysisEvaluationDetail"
	var board_row := HBoxContainer.new()
	board_row.name = "AnalysisBoardRow"
	board_row.size_flags_vertical = Control.SIZE_EXPAND_FILL
	board_row.add_theme_constant_override("separation", 0)
	board_column.add_child(board_row)
	var evaluation_gutter := Control.new()
	evaluation_gutter.custom_minimum_size.x = 40
	board_row.add_child(evaluation_gutter)
	board = AnalysisBoard.new()
	board.editor_host = self
	board.name = "AnalysisBoard"
	board.model = model
	board.square_pressed.connect(_on_square)
	board.drag_requested.connect(_on_drag)
	board_row.add_child(board)
	evaluation_bar = EvaluationBar.new()
	evaluation_bar.name = "AnalysisEvaluationBar"
	evaluation_bar.board = board
	board.add_child(evaluation_bar)
	evaluation_bar.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	board.resized.connect(evaluation_bar.queue_redraw)
	var panel := PanelContainer.new()
	panel.name = "AnalysisSidebarPanel"
	panel.custom_minimum_size.x = 360
	panel.size_flags_horizontal = Control.SIZE_SHRINK_END
	body.add_child(panel)
	var panel_margin := MarginContainer.new()
	for side in ["left", "right", "top", "bottom"]:
		panel_margin.add_theme_constant_override("margin_" + side, 12)
	panel.add_child(panel_margin)
	var side_shell := VBoxContainer.new()
	side_shell.name = "AnalysisSidebarShell"
	side_shell.add_theme_constant_override("separation", 7)
	panel_margin.add_child(side_shell)
	sidebar_tabs = TabBar.new()
	sidebar_tabs.name = "AnalysisSidebarTabs"
	OpeningExplorer.style_tabs(sidebar_tabs)
	for tab_caption in ["분석", "오프닝"]:
		sidebar_tabs.add_tab(tab_caption)
	sidebar_tabs.tab_alignment = TabBar.ALIGNMENT_CENTER
	sidebar_tabs.tab_changed.connect(select_sidebar_tab)
	side_shell.add_child(sidebar_tabs)
	quick_actions = GridContainer.new()
	quick_actions.name = "AnalysisQuickActions"
	quick_actions.columns = 2
	quick_actions.add_theme_constant_override("h_separation", 5)
	quick_actions.add_theme_constant_override("v_separation", 5)
	side_shell.add_child(quick_actions)
	var scroll := ScrollContainer.new()
	sidebar_scroll = scroll
	scroll.name = "AnalysisSidebarScroll"
	# The sidebar owns overflow; its contents must never widen the board row.
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_AUTO
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	side_shell.add_child(scroll)
	var page_stack := VBoxContainer.new()
	page_stack.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(page_stack)
	var sidebar := VBoxContainer.new()
	sidebar.name = "AnalysisSidebarContents"
	sidebar.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	sidebar.add_theme_constant_override("separation", 8)
	page_stack.add_child(sidebar)
	sidebar_pages.append(sidebar)
	var opening_sidebar := VBoxContainer.new()
	opening_sidebar.name = "AnalysisOpeningContents"
	opening_sidebar.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	page_stack.add_child(opening_sidebar)
	sidebar_pages.append(opening_sidebar)
	select_sidebar_tab(0)
	status_label = _label("원하는 위치를 만들어 보세요", 19, "f6f0de")
	sidebar.add_child(status_label)
	error_label = _label("", 13, "ffb4a1")
	error_label.name = "AnalysisError"
	side_shell.add_child(error_label)
	side_shell.move_child(error_label, 2)
	sidebar.add_child(evaluation_detail)
	sidebar.add_child(castle_actions)
	sidebar.add_child(castle_reason)
	editor_controls = VBoxContainer.new()
	editor_controls.add_theme_constant_override("separation", 8)
	sidebar.add_child(editor_controls)
	selected_label = _label("배치할 기물 · 백 폰", 14, "9bd7ca")
	editor_controls.add_child(selected_label)
	var palette := GridContainer.new()
	palette.columns = 3
	palette.add_theme_constant_override("h_separation", 5)
	palette.add_theme_constant_override("v_separation", 5)
	editor_controls.add_child(palette)
	var group := ButtonGroup.new()
	for color in ["white", "black"]:
		for kind in [["k", "왕"], ["q", "퀸"], ["r", "룩"], ["b", "비숍"], ["n", "나이트"], ["p", "폰"]]:
			var symbol: String = str(kind[0]).to_upper() if color == "white" else str(kind[0])
			var label: String = ("백 " if color == "white" else "흑 ") + str(kind[1])
			var choice := AnalysisBoard.PalettePiece.new()
			choice.text = label
			choice.name = "AnalysisPiece" + symbol
			choice.editor_host = self
			choice.piece = symbol
			choice.pressed.connect(_choose_piece.bind(symbol, label))
			choice.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
			choice.toggle_mode = true
			choice.button_group = group
			choice.size_flags_horizontal = Control.SIZE_EXPAND_FILL
			palette.add_child(choice)
			palette_buttons[symbol] = choice
	var erase := _button("지우개 · 칸 비우기", _choose_piece.bind("", "지우개"), "AnalysisEraser")
	erase.toggle_mode = true
	erase.button_group = group
	editor_controls.add_child(erase)
	palette_buttons[""] = erase
	trash_area = AnalysisBoard.TrashArea.new()
	trash_area.name = "AnalysisTrash"
	trash_area.editor_host = self
	trash_area.custom_minimum_size.y = 50
	var trash_style := StyleBoxFlat.new()
	trash_style.bg_color = Color("3b2933")
	trash_style.border_color = Color("c97e78")
	trash_style.set_border_width_all(1)
	trash_style.set_corner_radius_all(6)
	trash_style.content_margin_left = 10
	trash_style.content_margin_top = 7
	trash_style.content_margin_bottom = 7
	trash_area.add_theme_stylebox_override("panel", trash_style)
	var trash_label := _label("기물 버리기 · 여기로 끌어놓기", 14, "ffc3b1")
	trash_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	trash_area.add_child(trash_label)
	editor_controls.add_child(trash_area)
	var presets := HBoxContainer.new()
	editor_controls.add_child(presets)
	presets.add_child(_button("기본 배치", _load_preset.bind(START_FEN), "AnalysisStandard"))
	presets.add_child(_button("모두 비우기", _load_preset.bind("8/8/8/8/8/8/8/8 w - - 0 1"), "AnalysisClear"))
	editor_controls.add_child(_button("들어오기 전 배치 가져오기", _load_source, "AnalysisCurrent"))
	turn_option = OptionButton.new()
	turn_option.name = "AnalysisTurn"
	turn_option.add_item("백 차례", 0)
	turn_option.add_item("흑 차례", 1)
	turn_option.item_selected.connect(_change_turn)
	editor_controls.add_child(turn_option)
	editor_controls.add_child(_label("캐슬링 권리 · 왕과 룩을 한 번도 움직이지 않은 쪽", 12, "a3b6c8"))
	var castling_grid := GridContainer.new()
	castling_grid.columns = 2
	editor_controls.add_child(castling_grid)
	for item in [["K", "백 O-O"], ["Q", "백 O-O-O"], ["k", "흑 O-O"], ["q", "흑 O-O-O"]]:
		var check := CheckBox.new()
		check.name = "AnalysisCastlingRight" + str(item[0])
		check.text = str(item[1])
		check.toggled.connect(_change_castling_right.bind(str(item[0])))
		castling_grid.add_child(check)
		castling_checks[item[0]] = check
	editor_controls.add_child(_label("목록의 기물은 끌어서 복사합니다. 판 밖·버리기로 끌면 지우고 Esc로 취소합니다. 캐슬링 권리는 해당 왕·룩을 건드린 경우만 지우며, 사라진 권리는 자동으로 되살리지 않습니다.", 12, "a3b6c8"))
	sidebar.add_child(_label("위치 기록 (FEN)", 13, "a3b6c8"))
	fen_input = LineEdit.new()
	fen_input.name = "AnalysisFEN"
	fen_input.placeholder_text = "FEN 붙여넣기"
	fen_input.expand_to_text_length = false
	fen_input.max_length = 256
	sidebar.add_child(fen_input)
	var fen_buttons := HBoxContainer.new()
	sidebar.add_child(fen_buttons)
	fen_buttons.add_child(_button("FEN 불러오기", _import_fen, "AnalysisImportFEN"))
	fen_buttons.add_child(_button("FEN 복사", _copy_fen, "AnalysisCopyFEN"))
	time_option = OptionButton.new()
	time_option.name = "AnalysisTime"
	for option in [["분석 시간 · 0.1초", 100], ["분석 시간 · 1초", 1000], ["분석 시간 · 3초", 3000], ["분석 시간 · 5초", 5000]]:
		time_option.add_item(option[0], option[1])
	time_option.select(1)
	sidebar.add_child(time_option)
	start_button = _button("이 배치로 분석 시작", _start_analysis, "AnalysisStart")
	quick_actions.add_child(start_button)
	play_controls = VBoxContainer.new()
	play_controls.add_theme_constant_override("separation", 8)
	sidebar.add_child(play_controls)
	recommendation = _label("추천 수를 기다립니다", 19, "9bd7ca")
	recommendation.name = "AnalysisRecommendation"
	play_controls.add_child(recommendation)
	recommendation_detail = _label("", 13, "a3b6c8")
	play_controls.add_child(recommendation_detail)
	apply_button = _button("추천 수 따라 두기", _apply_recommendation, "AnalysisApply")
	quick_actions.add_child(apply_button)
	promotion_row = HBoxContainer.new()
	play_controls.add_child(promotion_row)
	for kind in [["q", "퀸"], ["r", "룩"], ["b", "비숍"], ["n", "나이트"]]:
		promotion_row.add_child(_button(str(kind[1]), _promote.bind(str(kind[0])), "AnalysisPromote" + str(kind[0])))
	undo_button = _button("한 수 무르기", _mutate.bind("undo", {}), "AnalysisUndo")
	quick_actions.add_child(undo_button)
	quick_actions.move_child(undo_button, 1)
	edit_button = _button("배치 편집", _edit_position, "AnalysisEdit")
	quick_actions.add_child(edit_button)
	for action_button in [start_button, undo_button, apply_button, edit_button]:
		action_button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		action_button.add_theme_font_size_override("font_size", 14)
	history = _label("", 13, "a3b6c8")
	play_controls.add_child(history)
	play_controls.add_child(_label("백·흑 양쪽 수를 직접 둘 수 있습니다. 추천은 현재 차례의 Regicide가 계산합니다.", 12, "8197aa"))
	opening_explorer = OpeningExplorer.new()
	opening_explorer.explore_position.connect(_on_explorer_position)
	opening_sidebar.add_child(opening_explorer)
	requester = HTTPRequest.new()
	requester.timeout = 6.0
	add_child(requester)
	# Snapshot polling must not lock out a board click or a promotion choice.
	# Mutations use their own transport and invalidate earlier GET callbacks.
	poll_requester = HTTPRequest.new()
	poll_requester.timeout = 6.0
	add_child(poll_requester)
	poll_timer = Timer.new()
	poll_timer.wait_time = 0.25
	poll_timer.timeout.connect(_poll)
	add_child(poll_timer)
	poll_timer.start()


func open_position(fen: String, url: String, token: String, explorer_preferences: Dictionary = {}) -> void:
	_epoch += 1
	var epoch: int = _epoch
	# Publish the new position and inherited filters as one lookup context.
	# Showing a previously enabled view must never query its old filters first.
	opening_explorer.suspend_queries()
	_state_ready = offline_mode
	backend_url = url
	backend_token = token
	source_fen = fen if not fen.is_empty() else START_FEN
	editing = true
	_error = ""
	_set_editor_fen(source_fen)
	model.selected_square = ""
	model.pending_promotions.clear()
	opening_explorer.set_context(source_fen, backend_url, backend_token, offline_mode)
	if not explorer_preferences.is_empty():
		opening_explorer.apply_preferences(explorer_preferences)
	select_sidebar_tab(1 if not explorer_preferences.is_empty() and explorer_preferences.get("enabled", false) else 0)
	show()
	_render()
	if not offline_mode:
		# An earlier view can still have one bounded snapshot request pending.
		# Wait for that transport before requesting this view's initial revision.
		while _poll_in_flight:
			await get_tree().process_frame
			if epoch != _epoch or not visible:
				return
		await _refresh()
	if epoch == _epoch and visible:
		opening_explorer.resume_queries()


func explore_position(fen: String, url: String, token: String, explorer_preferences: Dictionary = {}) -> void:
	await open_position(fen, url, token, explorer_preferences)
	if visible and not busy and source_fen == fen and _state_ready:
		await _start_analysis()


func _on_explorer_position(fen: String) -> void:
	if busy or fen.is_empty():
		return
	await explore_position(fen, backend_url, backend_token, opening_explorer.preferences())


func select_sidebar_tab(index: int) -> void:
	if sidebar_pages.is_empty():
		return
	index = clampi(index, 0, sidebar_pages.size() - 1)
	if index != _sidebar_active:
		_sidebar_scroll_positions[_sidebar_active] = sidebar_scroll.scroll_vertical
	_sidebar_active = index
	if sidebar_tabs.current_tab != index:
		sidebar_tabs.current_tab = index
	for page in range(sidebar_pages.size()):
		sidebar_pages[page].visible = page == index
	sidebar_scroll.set_deferred("scroll_vertical", _sidebar_scroll_positions[index])


func _choose_piece(symbol: String, label: String) -> void:
	_cancel_editor_drag()
	selected_piece = symbol
	selected_label.text = "배치할 기물 · " + label
	palette_buttons[symbol].set_pressed_no_signal(true)


func begin_editor_drag(origin: String, piece: String, square: String = "") -> Dictionary:
	if not visible or not editing or busy or piece.length() != 1 or piece not in "KQRBNPkqrbnp" or origin not in ["board", "palette"]:
		return {}
	if origin == "board" and (square.is_empty() or editor_pieces.get(square, "") != piece):
		return {}
	_cancel_editor_drag()
	_editor_drag_sequence += 1
	_editor_drag_cancelled = false
	_editor_drag = {"kind": "regicide-analysis-editor", "owner": get_instance_id(),
		"editor_revision": _editor_revision, "drag_id": _editor_drag_sequence,
		"origin": origin, "piece": piece, "square": square}
	return _editor_drag.duplicate()


func can_editor_drop(data: Variant) -> bool:
	if not visible or not editing or busy or _editor_drag_cancelled or not data is Dictionary or _editor_drag.is_empty():
		return false
	if data != _editor_drag or int(data.get("owner", -1)) != get_instance_id() or int(data.get("editor_revision", -1)) != _editor_revision:
		return false
	return data.get("origin") == "palette" or editor_pieces.get(data.get("square"), "") == data.get("piece")


func editor_drag_preview(piece: String) -> Control:
	var preview := Control.new()
	preview.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var icon := TextureRect.new()
	icon.mouse_filter = Control.MOUSE_FILTER_IGNORE
	icon.texture = board.piece_textures.get(piece)
	icon.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	icon.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	icon.position = Vector2(-32, -32)
	icon.size = Vector2(64, 64)
	preview.add_child(icon)
	return preview


func drop_editor_piece(data: Variant, square: String) -> void:
	if not can_editor_drop(data) or model.square_to_display(square).x < 0:
		return
	if data.get("origin") == "board" and data.get("square") == square:
		_cancel_editor_drag()
		return
	var next: Dictionary = editor_pieces.duplicate()
	if data.get("origin") == "board":
		next.erase(data.square)
	next[square] = data.piece
	_commit_editor_pieces(next)


func discard_editor_piece(data: Variant) -> void:
	if not can_editor_drop(data) or data.get("origin") != "board":
		return
	var next: Dictionary = editor_pieces.duplicate()
	next.erase(data.square)
	_commit_editor_pieces(next)


func _commit_editor_pieces(next: Dictionary) -> void:
	if next == editor_pieces:
		_cancel_editor_drag()
		return
	var preserved_rights := ""
	for right in "KQkq":
		if not editor_fields[1].contains(right):
			continue
		var rank_number := "1" if right == right.to_upper() else "8"
		var king_home := "e" + rank_number
		var rook_home := ("h" if right.to_lower() == "k" else "a") + rank_number
		if editor_pieces.get(king_home, "") == next.get(king_home, "") and editor_pieces.get(rook_home, "") == next.get(rook_home, ""):
			preserved_rights += right
	_invalidate_editor_drag()
	editor_pieces = next
	editor_fields[1] = preserved_rights if not preserved_rights.is_empty() else "-"
	editor_fields[2] = "-"
	editor_fields[3] = "0"
	fen_input.text = editor_fen()
	_error = ""
	_render()


func _cancel_editor_drag() -> void:
	_editor_drag.clear()
	_editor_drag_cancelled = true
	if board != null:
		board.cancel_editor_press()
	for item in palette_buttons.values():
		if item.has_method("cancel_editor_press"):
			item.cancel_editor_press()


func _invalidate_editor_drag() -> void:
	_editor_revision += 1
	_cancel_editor_drag()


func _can_drop_data(at_position: Vector2, data: Variant) -> bool:
	var global_point: Vector2 = get_global_transform() * at_position
	return can_editor_drop(data) and get_global_rect().has_point(global_point) and board.square_at_point(board.get_global_transform().affine_inverse() * global_point).is_empty()


func _drop_data(at_position: Vector2, data: Variant) -> void:
	if not _can_drop_data(at_position, data):
		return
	if data.get("origin") == "board":
		discard_editor_piece(data)
	else:
		_cancel_editor_drag()


func finish_editor_drag(success: bool, global_point: Vector2, cancelled: bool = false) -> void:
	if _editor_drag.is_empty():
		return
	var data: Dictionary = _editor_drag.duplicate()
	# A release beyond the app or an Esc cancellation leaves the draft intact.
	if not success and not cancelled and can_editor_drop(data) and get_global_rect().has_point(global_point) and board.square_at_point(board.get_global_transform().affine_inverse() * global_point).is_empty():
		if data.get("origin") == "board":
			discard_editor_piece(data)
	_cancel_editor_drag()


func _input(event: InputEvent) -> void:
	if not _editor_drag.is_empty() and event is InputEventKey and event.pressed and event.keycode == KEY_ESCAPE:
		_cancel_editor_drag()


func _notification(what: int) -> void:
	if what == NOTIFICATION_DRAG_END and not _editor_drag.is_empty():
		finish_editor_drag(get_viewport().gui_is_drag_successful(), get_global_mouse_position(), _editor_drag_cancelled or Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT))
	elif what == NOTIFICATION_WM_WINDOW_FOCUS_OUT:
		_cancel_editor_drag()


func _load_preset(fen: String) -> void:
	if busy or not editing:
		return
	_error = ""
	_set_editor_fen(fen)
	_render()


func _load_source() -> void:
	_load_preset(source_fen)


func _set_editor_fen(fen: String) -> bool:
	_invalidate_editor_drag()
	var fields := fen.strip_edges().split(" ", false)
	if fields.size() != 6 or fields[1] not in ["w", "b"]:
		_error = "FEN은 기물 배치·차례·캐슬링·앙파상·반수·수 번호의 6항목입니다."
		return false
	var ranks := fields[0].split("/")
	if ranks.size() != 8:
		_error = "FEN에는 8개 행이 필요합니다."
		return false
	var pieces: Dictionary = {}
	for row in range(8):
		var column := 0
		for symbol in ranks[row]:
			if symbol in "12345678":
				column += int(symbol)
			elif symbol in "KQRBNPkqrbnp" and column < 8:
				pieces["abcdefgh"[column] + str(8 - row)] = symbol
				column += 1
			else:
				_error = "FEN 기물이나 칸 수를 확인하세요."
				return false
		if column != 8:
			_error = "FEN의 각 행은 8칸이어야 합니다."
			return false
	editor_pieces = pieces
	editor_fields = fields.slice(1)
	turn_option.select(0 if fields[1] == "w" else 1)
	fen_input.text = editor_fen()
	return true


func editor_fen() -> String:
	var ranks := PackedStringArray()
	for rank_number in range(8, 0, -1):
		var row := ""
		var empty := 0
		for file_name in "abcdefgh":
			var piece: String = str(editor_pieces.get(file_name + str(rank_number), ""))
			if piece.is_empty():
				empty += 1
			else:
				if empty > 0:
					row += str(empty)
					empty = 0
				row += piece
		if empty > 0:
			row += str(empty)
		ranks.append(row)
	return "/".join(ranks) + " " + " ".join(editor_fields)


func _change_turn(index: int) -> void:
	if editing and not busy:
		_invalidate_editor_drag()
		editor_fields[0] = "w" if index == 0 else "b"
		editor_fields[2] = "-"
		fen_input.text = editor_fen()
		_render()


func _castling_home_ready(right: String) -> bool:
	var homes: Dictionary = {"K": ["e1", "K", "h1", "R"], "Q": ["e1", "K", "a1", "R"],
		"k": ["e8", "k", "h8", "r"], "q": ["e8", "k", "a8", "r"]}
	if not homes.has(right):
		return false
	var home: Array = homes[right]
	return editor_pieces.get(home[0], "") == home[1] and editor_pieces.get(home[2], "") == home[3]


func _change_castling_right(enabled: bool, right: String) -> void:
	if busy or not editing or not _castling_home_ready(right):
		_render()
		return
	_invalidate_editor_drag()
	var rights: String = editor_fields[1].replace("-", "").replace(right, "")
	if enabled:
		rights += right
	var ordered := ""
	for candidate in "KQkq":
		if rights.contains(candidate):
			ordered += candidate
	editor_fields[1] = ordered if not ordered.is_empty() else "-"
	fen_input.text = editor_fen()
	_error = ""
	_render()


func _castle_move(side: String) -> String:
	var rank_number: String = "1" if analysis_state.get("turn", "white") == "white" else "8"
	return "e" + rank_number + ("g" if side == "king" else "c") + rank_number


func _castle_gesture_move(from_square: String, to_square: String) -> String:
	for side in ["king", "queen"]:
		var canonical: String = _castle_move(side)
		if not _can_castle(side):
			continue
		var king_home: String = canonical.substr(0, 2)
		var rook_home: String = ("h" if side == "king" else "a") + canonical.substr(1, 1)
		if (from_square == king_home and to_square == rook_home) or (from_square == rook_home and to_square == king_home):
			return canonical
	return ""


func _can_castle(side: String) -> bool:
	return not editing and not busy and _error.is_empty() and analysis_state.get("active", false) and analysis_state.get("status", "") == "active" \
		and model.pending_promotions.is_empty() and analysis_state.get("legal_moves", []).has(_castle_move(side))


func _castle(side: String) -> void:
	if _can_castle(side):
		_mutate("move", {"move": _castle_move(side)})


func _render_castling() -> void:
	castle_actions.visible = not editing
	castle_reason.visible = not editing
	for right in castling_checks:
		var available: bool = _castling_home_ready(str(right))
		var check: CheckBox = castling_checks[right]
		check.set_pressed_no_signal(editor_fields[1].contains(str(right)))
		check.disabled = busy or not editing or not available
		check.tooltip_text = "왕과 해당 룩이 원래 칸에 있어야 이 권리를 설정할 수 있습니다." if not available else "왕과 이 룩을 아직 움직이지 않았을 때만 선택하세요. 실제 캐슬링은 체크와 통과 칸 공격·가로막힌 기물을 별도로 검사합니다."
		if check.button_pressed and not available:
			check.tooltip_text = "FEN에 적힌 캐슬링 권리와 왕·룩의 배치가 맞지 않습니다. FEN을 수정하거나 기물을 다시 배치하세요. 분석 시작 시 이 배치를 검사합니다."
	var reasons := PackedStringArray()
	var diagnostics: Dictionary = analysis_state.get("castling", {})
	for side in castling_buttons:
		var button: Button = castling_buttons[side]
		button.disabled = not _can_castle(str(side))
		var detail: Dictionary = diagnostics.get(side, {})
		var reason: String = str(detail.get("reason", "현재 위치의 합법 수에 있는 캐슬링만 실행할 수 있습니다."))
		button.tooltip_text = reason
		if button.disabled:
			reasons.append(("O-O: " if side == "king" else "O-O-O: ") + reason)
	castle_reason.text = "\n".join(reasons)
	if busy:
		castle_reason.text = "현재 수를 반영하고 있습니다."
	elif not model.pending_promotions.is_empty():
		castle_reason.text = "승격 기물을 먼저 선택하세요."
	elif not _error.is_empty():
		castle_reason.text = _error


func _import_fen() -> void:
	if busy:
		return
	if not editing:
		await _edit_position()
	if not editing:
		return
	_error = ""
	_set_editor_fen(fen_input.text)
	_render()


func _copy_fen() -> void:
	DisplayServer.clipboard_set(editor_fen() if editing else str(analysis_state.get("fen", "")))


func _on_square(square: String) -> void:
	if busy or not visible:
		return
	if editing:
		var next: Dictionary = editor_pieces.duplicate()
		if selected_piece.is_empty():
			next.erase(square)
		else:
			next[square] = selected_piece
		_commit_editor_pieces(next)
		return
	if not model.pending_promotions.is_empty():
		return
	var castle: String = _castle_gesture_move(model.selected_square, square)
	if not castle.is_empty():
		_mutate("move", {"move": castle})
		return
	if model.selected_square == square:
		model.selected_square = ""
		_render()
		return
	var candidates: Array[String] = []
	for move in analysis_state.get("legal_moves", []):
		if not model.selected_square.is_empty() and str(move).begins_with(model.selected_square + square):
			candidates.append(str(move))
	if candidates.size() > 1 or (candidates.size() == 1 and candidates[0].length() == 5):
		model.pending_promotions = candidates
	elif candidates.size() == 1:
		_mutate("move", {"move": candidates[0]})
		return
	else:
		model.selected_square = ""
		for move in analysis_state.get("legal_moves", []):
			if str(move).begins_with(square):
				model.selected_square = square
				break
	_render()


func _on_drag(from_square: String, to_square: String) -> void:
	if busy or editing or not model.pending_promotions.is_empty():
		return
	for move in analysis_state.get("legal_moves", []):
		if str(move).begins_with(from_square):
			model.selected_square = from_square
			_on_square(to_square)
			return


func _promote(kind: String) -> void:
	for move in model.pending_promotions:
		if move.ends_with(kind):
			_mutate("move", {"move": move})
			return


func flip_view() -> void:
	_invalidate_editor_drag()
	model.flip_view()
	_render()


func _start_analysis() -> void:
	if busy:
		return
	# Import pasted FEN before sending; semantic legality belongs to the host.
	if editing and not _set_editor_fen(fen_input.text):
		_render()
		return
	await _mutate("start", {"fen": editor_fen() if editing else str(analysis_state.get("fen", "")), "movetime_ms": time_option.get_selected_id()})


func _apply_recommendation() -> void:
	var value: Dictionary = analysis_state.get("hint", {})
	if _hint_ready(value):
		_mutate("move", {"move": str(value.move)})


func _edit_position() -> void:
	if busy:
		return
	var fen: String = str(analysis_state.get("fen", editor_fen()))
	if not await _mutate("stop", {}):
		return
	editing = true
	select_sidebar_tab(0)
	_set_editor_fen(fen)
	_render()


func _return_to_game() -> void:
	if busy:
		return
	_invalidate_editor_drag()
	# A disconnected host must not trap the user in this view. The helper has
	# a bounded search budget; discard callbacks locally even if stop fails.
	if not offline_mode and _state_ready:
		await _mutate("stop", {})
	_epoch += 1
	hide()
	returned.emit()


func _request(payload: Variant = null, client: HTTPRequest = null) -> Dictionary:
	if offline_mode or backend_url.is_empty():
		return {"ok": false, "error": "분석 관리기에 연결되지 않았습니다."}
	var headers := PackedStringArray(["X-Regicide-Token: " + backend_token])
	var method := HTTPClient.METHOD_GET
	var contents := ""
	if payload != null:
		method = HTTPClient.METHOD_POST
		headers.append("Content-Type: application/json")
		contents = JSON.stringify(payload)
	var transport: HTTPRequest = requester if client == null else client
	if transport.request(backend_url + "/api/analysis", headers, method, contents) != OK:
		return {"ok": false, "error": "분석 요청을 보내지 못했습니다."}
	var response: Array = await transport.request_completed
	if response[0] != HTTPRequest.RESULT_SUCCESS:
		return {"ok": false, "error": "분석 관리기와 연결이 끊겼습니다."}
	var parsed: Variant = JSON.parse_string(response[3].get_string_from_utf8())
	if not parsed is Dictionary:
		return {"ok": false, "error": "분석 상태를 읽지 못했습니다."}
	if int(response[1]) < 200 or int(response[1]) >= 300:
		return {"ok": false, "error": str(parsed.get("error", "분석 요청을 처리하지 못했습니다.")), "analysis": parsed.get("analysis", {})}
	return {"ok": true, "data": parsed}


func accept_state(next: Dictionary) -> void:
	if int(next.get("revision", -1)) < int(analysis_state.get("revision", -1)):
		return
	analysis_state = next.duplicate(true)
	_state_ready = true
	model.apply_state(analysis_state)
	_render()


func _refresh() -> void:
	if busy or _poll_in_flight:
		return
	_poll_in_flight = true
	var epoch: int = _epoch
	var mutation_epoch: int = _mutation_epoch
	var response: Dictionary = await _request(null, poll_requester)
	_poll_in_flight = false
	if epoch != _epoch or mutation_epoch != _mutation_epoch or busy or not visible:
		return
	if response.get("ok", false):
		_error = ""
		accept_state(response.data)
	else:
		_error = str(response.get("error", "분석 상태를 받지 못했습니다."))
		board.hint_move = ""
	_render()


func _mutate(action: String, payload: Dictionary) -> bool:
	if busy or not _state_ready:
		return false
	_invalidate_editor_drag()
	_mutation_epoch += 1
	busy = true
	_error = ""
	model.selected_square = ""
	model.pending_promotions.clear()
	board.hint_move = ""
	payload["action"] = action
	payload["revision"] = int(analysis_state.get("revision", 0))
	_render()
	var response: Dictionary = await _request(payload)
	busy = false
	if not response.get("ok", false):
		_error = str(response.get("error", "분석 요청을 처리하지 못했습니다."))
		if response.get("analysis", {}) is Dictionary and not response.get("analysis", {}).is_empty():
			accept_state(response.analysis)
		_render()
		return false
	if action == "start":
		editing = false
	accept_state(response.data)
	return true


func _poll() -> void:
	if visible and not editing and not busy and not offline_mode:
		_refresh()


func _hint_ready(value: Dictionary) -> bool:
	return not editing and not busy and _error.is_empty() and value.get("status", "") == "ready" \
		and value.get("game_id", "") == analysis_state.get("game_id", "-") \
		and int(value.get("revision", -1)) == int(analysis_state.get("revision", -2)) \
		and value.get("fen", "") == analysis_state.get("fen", "-") \
		and analysis_state.get("legal_moves", []).has(value.get("move"))


func _evaluation_matches(value: Dictionary) -> bool:
	return not value.is_empty() and value.get("game_id", "") == analysis_state.get("game_id", "-") \
		and int(value.get("revision", -1)) == int(analysis_state.get("revision", -2)) \
		and value.get("fen", "") == analysis_state.get("fen", "-") \
		and value.get("turn", "") == analysis_state.get("turn", "-")


func _render_evaluation() -> void:
	if evaluation_bar == null:
		return
	var presented: Dictionary = {"status": "waiting"}
	if not _error.is_empty():
		presented = {"status": "unavailable", "message": _error}
	elif busy:
		presented = {"status": "searching"}
	elif not editing and analysis_state.get("active", false):
		var value: Variant = analysis_state.get("evaluation", {})
		if value is Dictionary and _evaluation_matches(value):
			presented = value
	evaluation_bar.present(presented, model.view_from_white)
	evaluation_summary.text = evaluation_bar.summary_text
	evaluation_detail.text = evaluation_bar.detail_text + "\n+는 백, −는 흑 우세 · 1.00은 폰 한 개의 평가 단위이며 승률이 아닙니다."
	if editing and _error.is_empty():
		evaluation_detail.text = "기물 배치를 마치고 분석을 시작하면 현재 위치의 우세를 표시합니다."
	evaluation_summary.tooltip_text = evaluation_bar.tooltip_text
	evaluation_detail.tooltip_text = evaluation_bar.tooltip_text


func _render() -> void:
	if board == null:
		return
	if opening_explorer != null:
		opening_explorer.set_context(editor_fen() if editing else str(analysis_state.get("fen", "")), backend_url, backend_token, offline_mode)
	if not _editor_drag.is_empty() and (busy or not editing or not visible):
		_invalidate_editor_drag()
	editor_controls.visible = editing
	play_controls.visible = not editing
	start_button.text = "이 배치로 분석 시작" if editing else "현재 위치 다시 분석"
	start_button.disabled = busy or not _state_ready or backend_url.is_empty()
	apply_button.disabled = editing or not _hint_ready(analysis_state.get("hint", {}))
	undo_button.disabled = editing or busy or not analysis_state.get("can_undo", false)
	edit_button.disabled = editing or busy
	if not model.pending_promotions.is_empty() and _sidebar_active != 0:
		select_sidebar_tab(0)
	return_button.disabled = busy
	turn_option.disabled = busy
	time_option.disabled = busy
	fen_input.editable = editing and not busy
	board.state = {"board": editor_pieces, "turn": "white" if editor_fields[0] == "w" else "black", "legal_moves": []} if editing else analysis_state
	board.hint_move = ""
	error_label.text = _error
	error_label.visible = not _error.is_empty()
	status_label.text = "기물을 자유롭게 배치하세요" if editing else str(analysis_state.get("message", "현재 위치 분석"))
	turn_label.text = "기물 배치 편집" if editing else ("백 차례" if analysis_state.get("turn", "white") == "white" else "흑 차례")
	turn_label.tooltip_text = "원하는 기물을 배치한 뒤 분석을 시작하세요." if editing else "백·흑 양쪽 수를 직접 둘 수 있습니다."
	if editing:
		palette_buttons[selected_piece].set_pressed_no_signal(true)
	else:
		fen_input.text = str(analysis_state.get("fen", ""))
		var value: Dictionary = analysis_state.get("hint", {})
		if _hint_ready(value):
			var move: String = str(value.move)
			board.hint_move = move
			recommendation.text = "Regicide 추천 · " + str(value.get("san", move))
			recommendation_detail.text = "%s → %s%s" % [move.substr(0, 2), move.substr(2, 2), " · 승격 " + move.substr(4).to_upper() if move.length() == 5 else ""]
		else:
			recommendation.text = "추천 수 계산 중" if value.get("status", "") == "searching" else "추천 수 대기"
			recommendation_detail.text = str(value.get("message", "현재 위치의 추천 수를 기다립니다."))
		apply_button.disabled = not _hint_ready(value)
		undo_button.disabled = busy or not analysis_state.get("can_undo", false)
		edit_button.disabled = busy
		promotion_row.visible = not model.pending_promotions.is_empty()
		var moves := PackedStringArray()
		for move in analysis_state.get("moves", []):
			moves.append(str(move.get("san", "")))
		history.text = "분석 수순 · " + " ".join(moves)
	_render_evaluation()
	_render_castling()
	board.queue_redraw()
