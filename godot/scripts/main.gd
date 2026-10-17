extends Control
## The native client never chooses moves or reproduces chess rules.

signal state_changed(revision: int)

const ViewModel = preload("res://scripts/board_view_model.gd")
const ChessBoard = preload("res://scripts/chess_board.gd")

var board_model: ViewModel = ViewModel.new()
var state: Dictionary = {}
var request_in_flight: bool = false
var engine_thinking: bool = false
var offline_mode: bool = false
var closing: bool = false
var backend_url: String = ""
var backend_token: String = ""
var own_backend: bool = false
var error_message: String = ""
var _pending_pgn: String = ""
var _rendered_move_count: int = -1
var _notice_message: String = ""

var board: ChessBoard
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
var new_button: Button
var undo_button: Button
var resign_button: Button
var save_button: Button
var refresh_button: Button
var promotion_panel: PanelContainer
var promotion_buttons: Dictionary = {}
var save_dialog: FileDialog
var http: HTTPRequest


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
			style.content_margin_top = 10
			style.content_margin_bottom = 10
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
	top_player = _label("흑  ·  Regicide", 15, "bccbd8")
	board_column.add_child(top_player)
	board = ChessBoard.new()
	board.name = "ChessBoard"
	board.model = board_model
	board.square_pressed.connect(_on_square_pressed)
	board.drag_requested.connect(_on_drag_requested)
	board_column.add_child(board)
	bottom_player = _label("백  ·  당신", 15, "bccbd8")
	board_column.add_child(bottom_player)
	board_column.add_child(_label("기물 → 도착 칸 클릭  /  드래그 이동  /  F 키로 시점 전환", 12, "8197aa"))
	var side_panel := PanelContainer.new()
	side_panel.custom_minimum_size.x = 330
	side_panel.add_theme_stylebox_override("panel", _panel_style("132134"))
	body.add_child(side_panel)
	var side_margins := MarginContainer.new()
	for side in ["left", "right", "top", "bottom"]:
		side_margins.add_theme_constant_override("margin_" + side, 18)
	side_panel.add_child(side_margins)
	var sidebar := VBoxContainer.new()
	sidebar.add_theme_constant_override("separation", 13)
	side_margins.add_child(sidebar)
	status_label = _label("체스판을 준비하고 있어요", 20, "f6f0de")
	status_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(status_label)
	status_detail = _label("잠시만 기다려 주세요.", 13, "a3b6c8")
	status_detail.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(status_detail)
	error_label = _label("", 13, "ffb4a1")
	error_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(error_label)
	refresh_button = _button("연결 다시 확인", _connect_backend)
	sidebar.add_child(refresh_button)
	sidebar.add_child(HSeparator.new())
	var settings := GridContainer.new()
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
	settings.add_child(time_option)
	new_button = _button("새 대국 시작", _on_new_game_pressed)
	sidebar.add_child(new_button)
	var actions := HBoxContainer.new()
	actions.add_theme_constant_override("separation", 10)
	sidebar.add_child(actions)
	undo_button = _button("한 턴 되돌리기", _on_undo_pressed)
	undo_button.tooltip_text = "내 수와 엔진의 응답을 함께 되돌립니다."
	undo_button.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	actions.add_child(undo_button)
	resign_button = _button("기권", _on_resign_pressed)
	actions.add_child(resign_button)
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
	move_history.custom_minimum_size.y = 80
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
	save_dialog = FileDialog.new()
	save_dialog.title = "기보 저장"
	save_dialog.file_mode = FileDialog.FILE_MODE_SAVE_FILE
	save_dialog.access = FileDialog.ACCESS_FILESYSTEM
	save_dialog.use_native_dialog = true
	save_dialog.filters = PackedStringArray(["*.pgn ; Chess PGN"])
	save_dialog.current_dir = OS.get_system_dir(OS.SYSTEM_DIR_DOCUMENTS)
	save_dialog.file_selected.connect(_save_pgn_file)
	add_child(save_dialog)


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
	if path == "/api/pgn" and int(response[1]) == 200:
		return {"ok": true, "pgn": text}
	var parsed: Variant = JSON.parse_string(text)
	if not parsed is Dictionary:
		return {"ok": false, "error": "대국 상태를 읽지 못했습니다."}
	if int(response[1]) < 200 or int(response[1]) >= 300:
		return {"ok": false, "error": str(parsed.get("error", "요청을 처리하지 못했습니다.")), "state": parsed.get("state", {})}
	return {"ok": true, "data": parsed}


func accept_state(next: Dictionary) -> void:
	state = next.duplicate(true)
	board_model.apply_state(state)
	_render()
	state_changed.emit(int(state.get("revision", 0)))


func _handle_response(response: Dictionary) -> bool:
	if not response.get("ok", false):
		if response.get("state", {}) is Dictionary and not response.get("state", {}).is_empty():
			accept_state(response.state)
		error_message = str(response.get("error", "요청을 처리하지 못했습니다."))
		return false
	accept_state(response.data)
	return true


func _connect_backend() -> void:
	if request_in_flight or backend_url.is_empty() or closing:
		return
	request_in_flight = true
	error_message = ""
	_render()
	if _handle_response(await _send_request("/api/state")):
		await _play_engine_if_needed()
	request_in_flight = false
	_render()


func _mutate(path: String, payload: Dictionary = {}) -> void:
	if request_in_flight or state.is_empty() or closing or backend_url.is_empty():
		return
	request_in_flight = true
	error_message = ""
	_notice_message = ""
	board_model.selected_square = ""
	board_model.pending_promotions.clear()
	# JSON numbers are decoded as floats by Godot. The host requires integers.
	payload["revision"] = int(state.revision)
	_render()
	if _handle_response(await _send_request(path, payload)):
		await _play_engine_if_needed()
	request_in_flight = false
	_render()


func _play_engine_if_needed() -> void:
	if closing or not state.get("engine_to_move", false) or state.get("result", "*") != "*" or state.get("error") != null:
		return
	engine_thinking = true
	_render()
	_handle_response(await _send_request("/api/engine", {"revision": int(state.revision)}))
	engine_thinking = false


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
	if request_in_flight or closing or not state.get("human_to_move", false) or not board_model.pending_promotions.is_empty():
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
		_mutate("/api/move", {"move": candidates[0]})
		return
	board_model.selected_square = ""
	for move in state.get("legal_moves", []):
		if str(move).begins_with(square):
			board_model.selected_square = square
			break
	_render()


func _on_promotion_pressed(kind: String) -> void:
	for move in board_model.pending_promotions:
		if move.ends_with(kind):
			_mutate("/api/move", {"move": move})
			return


func _on_drag_requested(from_square: String, to_square: String) -> void:
	if request_in_flight or closing or not state.get("human_to_move", false) or not board_model.pending_promotions.is_empty():
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


func _on_resign_pressed() -> void:
	_mutate("/api/resign")


func _on_save_pressed() -> void:
	if request_in_flight or state.is_empty() or closing or backend_url.is_empty():
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
	board.state = state
	board.queue_redraw()
	view_label.text = "백 시점 · 백이 아래" if board_model.view_from_white else "흑 시점 · 흑이 아래"
	flip_button.disabled = false
	var human_color: String = str(state.get("human_color", "white"))
	var bottom_color: String = "white" if board_model.view_from_white else "black"
	var upper_color: String = "black" if board_model.view_from_white else "white"
	top_player.text = _player_text(upper_color, human_color)
	bottom_player.text = _player_text(bottom_color, human_color)
	var engine: Dictionary = state.get("engine", {})
	engine_label.text = str(engine.get("name", "엔진 연결 준비 중"))
	if not engine.is_empty():
		engine_label.tooltip_text = "실행 파일 SHA-256\n" + str(engine.get("binary_sha256", ""))
	var ready: bool = not state.is_empty() and not closing
	var active: bool = ready and state.get("result", "*") == "*" and state.get("error") == null
	var enabled: bool = ready and not request_in_flight
	new_button.disabled = not enabled
	color_option.disabled = not enabled
	time_option.disabled = not enabled
	undo_button.disabled = not enabled or not state.get("can_undo", false)
	resign_button.disabled = not enabled or not active
	save_button.disabled = not enabled
	refresh_button.disabled = request_in_flight or backend_url.is_empty() or closing
	refresh_button.visible = not error_message.is_empty() or state.is_empty()
	error_label.text = error_message if not error_message.is_empty() else str(state.get("error", "") if state.get("error") != null else "")
	error_label.visible = not error_label.text.is_empty()
	if closing:
		status_label.text = "대국을 저장하고 종료합니다"
		status_detail.text = "진행한 수는 자동 저장돼 있습니다."
	elif engine_thinking:
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
		else:
			var color_name := "백" if human_color == "white" else "흑"
			status_detail.text = "당신은 %s · 한 수에 %s초 생각합니다." % [color_name, str(float(engine.get("movetime_ms", 1000)) / 1000.0)]
	if not _notice_message.is_empty():
		status_detail.text += "\n" + _notice_message
	promotion_panel.visible = not board_model.pending_promotions.is_empty()
	for kind in promotion_buttons:
		var available := false
		for move in board_model.pending_promotions:
			if move.ends_with(str(kind)):
				available = true
		promotion_buttons[kind].disabled = request_in_flight or not available
	_render_history()


func _player_text(color_name: String, human_color: String) -> String:
	var text := "백" if color_name == "white" else "흑"
	text += "  ·  당신" if color_name == human_color else "  ·  Regicide"
	if state.get("result", "*") == "*" and state.get("turn", "") == color_name:
		text += "   ● 차례"
	return text


func _render_history() -> void:
	var moves: Array = state.get("moves", [])
	move_count.text = "%d수" % moves.size()
	var contents := ""
	if moves.is_empty():
		contents = "[color=#8197aa]첫 수를 기다리고 있습니다.[/color]"
	else:
		contents = "[table=3]"
		for index in range(0, moves.size(), 2):
			contents += "[cell][color=#8197aa]%d.   [/color][/cell]" % (index / 2 + 1)
			for ply in range(index, index + 2):
				var san: String = str(moves[ply].get("san", "")) if ply < moves.size() else ""
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
	if own_backend and not backend_url.is_empty():
		# Let our existing mutation finish before reading its final revision. A
		# request may still be queued before the host acquires the game lock.
		var finish_deadline: int = Time.get_ticks_msec() + 20000
		while request_in_flight and Time.get_ticks_msec() < finish_deadline:
			await get_tree().process_frame
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
