extends VBoxContainer
## Human-game statistics are external reference data, never live move commands.

signal explore_position(fen: String)

const TOKEN_URL := "https://lichess.org/account/oauth/token/create"
const ALL_RATINGS := [0, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2500]

var backend_url: String = ""
var backend_token: String = ""
var current_fen: String = ""
var offline_mode: bool = false
var snapshot: Dictionary = {}
var contents: VBoxContainer
var source_option: OptionButton
var rating_option: OptionButton
var speed_option: OptionButton
var filter_box: VBoxContainer
var status_label: Label
var connection_label: Label
var summary_label: Label
var rows: VBoxContainer
var games: VBoxContainer
var token_input: LineEdit
var remember_token: CheckBox
var token_button: Button
var token_form: VBoxContainer
var change_token_button: Button
var _connection_context: String = ""
var _connection_generation: int = 0
var _connection_retry_after_ms: int = 0
var _connection_configured: bool = false
var _generation: int = 0
var _context_key: String = ""
var _request_key: String = ""
var _poll_timer: Timer
var _network_busy: bool = false
var _token_busy: bool = false
var _suspended: bool = false
var section_tabs: TabBar
var section_pages: Array[VBoxContainer] = []
var moves_scroll: ScrollContainer
var games_scroll: ScrollContainer
var _connection_auto: bool = false
var _section_active: int = 0


func _ready() -> void:
	name = "OpeningExplorer"
	add_theme_constant_override("separation", 6)
	add_child(_label("오프닝 탐색기", 22))
	contents = VBoxContainer.new()
	contents.name = "ExplorerContents"
	contents.add_theme_constant_override("separation", 6)
	add_child(contents)
	status_label = _label("현재 위치의 실제 대국 통계를 보여줍니다.", 14)
	status_label.name = "ExplorerStatus"
	contents.add_child(status_label)
	section_tabs = TabBar.new()
	section_tabs.name = "ExplorerSections"
	style_tabs(section_tabs)
	for caption in ["통계", "참고 기보", "연결 설정"]:
		section_tabs.add_tab(caption)
	section_tabs.add_theme_font_size_override("font_size", 15)
	section_tabs.tab_alignment = TabBar.ALIGNMENT_CENTER
	contents.add_child(section_tabs)
	connection_label = _label("", 12)
	connection_label.name = "ExplorerConnectionStatus"
	connection_label.hide()
	contents.add_child(connection_label)
	for section_name in ["ExplorerStatistics", "ExplorerReferenceGames", "ExplorerConnection"]:
		var page := VBoxContainer.new()
		page.name = section_name
		page.add_theme_constant_override("separation", 6)
		contents.add_child(page)
		section_pages.append(page)
	var statistics: VBoxContainer = section_pages[0]
	var references: VBoxContainer = section_pages[1]
	var connection: VBoxContainer = section_pages[2]
	var source_row := HBoxContainer.new()
	statistics.add_child(source_row)
	source_option = OptionButton.new()
	source_option.name = "ExplorerSource"
	source_option.add_item("마스터 대국", 0)
	source_option.add_item("리체스 일반 대국", 1)
	source_option.item_selected.connect(_filter_changed)
	source_option.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	source_option.add_theme_font_size_override("font_size", 15)
	source_option.custom_minimum_size.y = 36
	source_row.add_child(source_option)
	var refresh := Button.new()
	refresh.name = "ExplorerRefresh"
	refresh.text = "↻"
	refresh.tooltip_text = "현재 위치 다시 조회"
	refresh.pressed.connect(_refresh)
	source_row.add_child(refresh)
	filter_box = VBoxContainer.new()
	filter_box.hide()
	statistics.add_child(filter_box)
	rating_option = OptionButton.new()
	rating_option.name = "ExplorerRatings"
	for title in ["레이팅 · 1600 이상", "레이팅 · 전체", "레이팅 · 1600–1999", "레이팅 · 2000 이상", "레이팅 · 1600 미만"]:
		rating_option.add_item(title)
	rating_option.item_selected.connect(_filter_changed)
	filter_box.add_child(rating_option)
	speed_option = OptionButton.new()
	speed_option.name = "ExplorerSpeeds"
	for title in ["시간제 · 래피드·클래시컬", "시간제 · 블리츠", "시간제 · 불릿", "시간제 · 전체"]:
		speed_option.add_item(title)
	speed_option.item_selected.connect(_filter_changed)
	filter_box.add_child(speed_option)
	summary_label = _label("", 14)
	summary_label.name = "ExplorerSummary"
	statistics.add_child(summary_label)
	statistics.add_child(_label("수를 누르면 별도 분석에서 이어갑니다.", 14))
	moves_scroll = ScrollContainer.new()
	moves_scroll.name = "ExplorerMoveScroll"
	moves_scroll.custom_minimum_size.y = 330
	moves_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	statistics.add_child(moves_scroll)
	rows = VBoxContainer.new()
	rows.name = "ExplorerMoves"
	rows.add_theme_constant_override("separation", 4)
	rows.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	moves_scroll.add_child(rows)
	games_scroll = ScrollContainer.new()
	games_scroll.name = "ExplorerGameScroll"
	games_scroll.custom_minimum_size.y = 330
	games_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	references.add_child(games_scroll)
	games = VBoxContainer.new()
	games.name = "ExplorerGames"
	games.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	games_scroll.add_child(games)
	connection.add_child(_label("리체스 연결", 13))
	change_token_button = Button.new()
	change_token_button.name = "ExplorerChangeToken"
	change_token_button.text = "연결키 변경"
	change_token_button.hide()
	change_token_button.pressed.connect(func(): token_form.show(); change_token_button.hide())
	connection.add_child(change_token_button)
	token_form = VBoxContainer.new()
	token_form.name = "ExplorerTokenForm"
	connection.add_child(token_form)
	var issue := Button.new()
	issue.name = "ExplorerIssueToken"
	issue.text = "리체스 연결키 만들기 ↗"
	issue.pressed.connect(func(): OS.shell_open(TOKEN_URL))
	token_form.add_child(issue)
	token_form.add_child(_label("처음 한 번만 연결하면 다음 실행부터 저장된 연결키를 사용합니다.\n① 리체스에 로그인하세요.\n② 설명에 Regicide를 적고, 권한은 체크하지 않은 채 연결키(토큰)를 만드세요.\n③ 만들어진 긴 문자열을 복사해 아래에 붙여넣으세요. 채팅에 보내지 마세요.", 12))
	token_input = LineEdit.new()
	token_input.name = "ExplorerToken"
	token_input.secret = true
	token_input.placeholder_text = "리체스 연결키(토큰) 붙여넣기"
	token_input.max_length = 1024
	token_form.add_child(token_input)
	remember_token = CheckBox.new()
	remember_token.name = "ExplorerRemember"
	remember_token.text = "이 Windows 계정에 연결 기억"
	remember_token.button_pressed = OS.get_name() == "Windows"
	remember_token.disabled = OS.get_name() != "Windows"
	remember_token.add_theme_font_size_override("font_size", 12)
	token_form.add_child(remember_token)
	token_button = Button.new()
	token_button.name = "ExplorerConnect"
	token_button.text = "리체스 연결"
	token_button.pressed.connect(_save_token)
	token_form.add_child(token_button)
	section_tabs.tab_changed.connect(select_section)
	select_section(0)
	_poll_timer = Timer.new()
	_poll_timer.wait_time = 0.5
	_poll_timer.one_shot = true
	_poll_timer.timeout.connect(_poll)
	add_child(_poll_timer)
	visibility_changed.connect(_visibility_changed)
	_restore_connection()


func select_section(index: int) -> void:
	index = clampi(index, 0, 2)
	var previous := _section_active
	_section_active = index
	_connection_auto = false
	if section_tabs.current_tab != index:
		section_tabs.set_block_signals(true)
		section_tabs.current_tab = index
		section_tabs.set_block_signals(false)
	for page in range(section_pages.size()):
		section_pages[page].visible = page == index
	if index == 2:
		_generation += 1
		_network_busy = false
		if _poll_timer != null:
			_poll_timer.stop()
	elif previous == 2:
		_refresh()


func _label(text: String, font_size: int = 13) -> Label:
	var label := Label.new()
	label.text = text
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.add_theme_font_size_override("font_size", font_size)
	label.add_theme_color_override("font_color", Color("bccbd8"))
	return label


func set_context(fen: String, url: String, token: String, offline: bool = false) -> void:
	var key := fen + "|" + url + "|" + str(offline)
	backend_url = url.trim_suffix("/")
	backend_token = token
	current_fen = fen
	offline_mode = offline
	if key == _context_key:
		if is_node_ready():
			_restore_connection()
		return
	_context_key = key
	_invalidate()
	if is_node_ready() and is_visible_in_tree():
		_query()
	if is_node_ready():
		_restore_connection()


func _restore_connection() -> void:
	if offline_mode or backend_url.is_empty() or Time.get_ticks_msec() < _connection_retry_after_ms:
		return
	var context := backend_url + "|" + backend_token
	if context == _connection_context:
		return
	_connection_context = context
	_connection_generation += 1
	var generation := _connection_generation
	# This GET only reads local connection metadata. Statistics are requested
	# later, only while this opening panel is visible.
	var response: Dictionary = await _request()
	if generation != _connection_generation:
		return
	if response.has("error"):
		_connection_context = ""
		_connection_retry_after_ms = Time.get_ticks_msec() + 5000
		# No retry timer; a later context/visibility update can retry safely.
		return
	_update_connection(response)


func _update_connection(value: Dictionary) -> void:
	var configured: bool = value.get("configured", false)
	var connection: Variant = value.get("connection", {})
	var message: String = str(connection.get("message", "")) if connection is Dictionary else ""
	if message.is_empty() and configured and (not _connection_configured or connection_label.text.is_empty()):
		message = "연결키가 설정되어 있습니다. 다시 입력할 필요가 없습니다."
	if value.get("status", "") == "auth_required":
		message = str(value.get("message", "리체스 연결이 필요합니다. 연결키를 입력해 주세요."))
		if message.is_empty():
			message = "리체스 연결이 필요합니다. 연결키를 입력해 주세요."
		token_form.show()
		change_token_button.hide()
	else:
		token_form.visible = not configured
		change_token_button.visible = configured
	_connection_configured = configured and value.get("status", "") != "auth_required"
	if not message.is_empty():
		connection_label.text = message
		connection_label.show()


func _invalidate() -> void:
	_generation += 1
	_request_key = ""
	_network_busy = false
	snapshot = {}
	if _poll_timer != null:
		_poll_timer.stop()
	if rows != null:
		_clear(rows)
		_clear(games)
		summary_label.text = ""


func suspend_queries() -> void:
	_suspended = true
	_invalidate()


func resume_queries() -> void:
	_suspended = false
	_query()


func _visibility_changed() -> void:
	if not is_node_ready():
		return
	_invalidate()
	if is_visible_in_tree():
		_query()
	if is_visible_in_tree():
		_restore_connection()


func _filter_changed(_index: int) -> void:
	filter_box.visible = source_option.selected == 1
	_refresh()


func _refresh() -> void:
	_invalidate()
	_query()


func selected_ratings() -> Array:
	match rating_option.selected:
		1: return ALL_RATINGS.duplicate()
		2: return [1600, 1800]
		3: return [2000, 2200, 2500]
		4: return [0, 1000, 1200, 1400]
	return [1600, 1800, 2000, 2200, 2500]


func selected_speeds() -> Array:
	match speed_option.selected:
		1: return ["blitz"]
		2: return ["bullet"]
		3: return ["ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence"]
	return ["rapid", "classical"]


func _filters() -> Dictionary:
	return {"action": "query", "fen": current_fen,
		"source": "masters" if source_option.selected == 0 else "lichess",
		"ratings": selected_ratings(), "speeds": selected_speeds()}


func preferences() -> Dictionary:
	return {"enabled": true, "source": source_option.selected,
		"rating": rating_option.selected, "speed": speed_option.selected}


func apply_preferences(value: Dictionary) -> void:
	source_option.select(clampi(int(value.get("source", 0)), 0, source_option.item_count - 1))
	rating_option.select(clampi(int(value.get("rating", 0)), 0, rating_option.item_count - 1))
	speed_option.select(clampi(int(value.get("speed", 0)), 0, speed_option.item_count - 1))
	filter_box.visible = source_option.selected == 1
	_refresh()


func _query() -> void:
	if _suspended or _token_busy or section_tabs.current_tab == 2 or not is_visible_in_tree():
		return
	if offline_mode or backend_url.is_empty():
		status_label.text = "오프라인입니다. Regicide 연결 후 사용할 수 있습니다."
		return
	if current_fen.is_empty():
		status_label.text = "조회할 보드 위치를 기다립니다."
		return
	var generation := _generation
	status_label.text = "리체스 통계를 조회합니다…"
	_network_busy = true
	var response: Dictionary = await _request(_filters())
	if generation != _generation or not is_visible_in_tree():
		return
	_network_busy = false
	_accept_response(response)


func _poll() -> void:
	if _suspended or _network_busy or _token_busy or section_tabs.current_tab == 2 or not is_visible_in_tree():
		return
	var generation := _generation
	_network_busy = true
	var response: Dictionary = await _request()
	if generation != _generation or not is_visible_in_tree():
		return
	_network_busy = false
	_accept_response(response)


func _request(payload: Variant = null) -> Dictionary:
	var transport := HTTPRequest.new()
	transport.timeout = 12.0
	add_child(transport)
	var headers := PackedStringArray(["X-Regicide-Token: " + backend_token])
	var method := HTTPClient.METHOD_GET
	var body := ""
	if payload != null:
		method = HTTPClient.METHOD_POST
		headers.append("Content-Type: application/json")
		body = JSON.stringify(payload)
	var result := transport.request(backend_url + "/api/opening-explorer", headers, method, body)
	body = ""
	if result != OK:
		transport.queue_free()
		return {"error": "탐색기 요청을 보내지 못했습니다."}
	var response: Array = await transport.request_completed
	transport.queue_free()
	if int(response[0]) != HTTPRequest.RESULT_SUCCESS:
		return {"error": "탐색기에 연결하지 못했습니다. 연결 상태를 확인하고 다시 조회해 주세요."}
	var parsed: Variant = JSON.parse_string(response[3].get_string_from_utf8())
	if not parsed is Dictionary:
		return {"error": "탐색기 응답을 읽지 못했습니다."}
	if int(response[1]) < 200 or int(response[1]) >= 300:
		return {"error": str(parsed.get("error", "탐색기 요청을 처리하지 못했습니다."))}
	return parsed


func _save_token() -> void:
	if _token_busy:
		return
	var token := token_input.text.strip_edges()
	token_input.clear()
	if token.is_empty():
		status_label.text = "만든 리체스 연결키를 붙여넣어 주세요."
		return
	if offline_mode or backend_url.is_empty():
		status_label.text = "Regicide에 연결한 뒤 연결키를 넣어 주세요."
		return
	_invalidate()
	_connection_generation += 1
	_token_busy = true
	token_button.disabled = true
	var payload := {"action": "token", "token": token, "remember": remember_token.button_pressed}
	token = ""
	var response: Dictionary = await _request(payload)
	payload.clear()
	_token_busy = false
	token_button.disabled = false
	# Connection settings belong to the client, independent of a board/filter
	# change while saving. The next query always uses the latest context.
	if response.has("error"):
		status_label.text = str(response.error)
		connection_label.text = str(response.error)
	else:
		# Persistence failures and environment overrides remain visible after query.
		_update_connection(response)
		connection_label.text = str(response.get("message", "연결 설정을 저장했습니다."))
		status_label.text = "현재 위치의 통계를 확인합니다."
		if section_tabs.current_tab == 2:
			select_section(0)
		else:
			_refresh()
	connection_label.visible = not connection_label.text.is_empty()


func _same_position(left: String, right: String) -> bool:
	var a := left.split(" ", false)
	var b := right.split(" ", false)
	return a.size() >= 4 and b.size() >= 4 and a.slice(0, 4) == b.slice(0, 4)


func _same_values(left: Variant, right: Array) -> bool:
	if not left is Array:
		return false
	var a: Array = left.duplicate()
	var b := right.duplicate()
	a.sort()
	b.sort()
	if a.size() != b.size():
		return false
	for index in range(a.size()):
		# JSON decodes rating numbers as floats; native menu values are ints.
		# Compare their exact numeric values without truncating malformed floats
		# or accepting strings/bools as rating numbers.
		if b[index] is int:
			if not (a[index] is int or a[index] is float) or float(a[index]) != float(b[index]):
				return false
		elif typeof(a[index]) != typeof(b[index]) or a[index] != b[index]:
			return false
	return true


func _matches(value: Dictionary) -> bool:
	if not _same_position(str(value.get("fen", "")), current_fen):
		return false
	if str(value.get("source", "")) != ("masters" if source_option.selected == 0 else "lichess"):
		return false
	if source_option.selected == 1 and (not _same_values(value.get("ratings", []), selected_ratings()) or not _same_values(value.get("speeds", []), selected_speeds())):
		return false
	return _request_key.is_empty() or str(value.get("request_key", "")) == _request_key


func _accept_response(value: Dictionary) -> void:
	if value.has("error"):
		status_label.text = str(value.error)
		return
	# The host may be shared by another view. Never paint its position/filter.
	if not _matches(value):
		status_label.text = "위치 또는 필터가 바뀌었습니다. 현재 위치를 다시 조회해 주세요."
		return
	_request_key = str(value.get("request_key", ""))
	snapshot = value.duplicate(true)
	render_snapshot(value)
	if str(value.get("status", "")) == "loading":
		_poll_timer.start()


func _clear(container: Node) -> void:
	for child in container.get_children():
		container.remove_child(child)
		child.queue_free()


func render_snapshot(value: Dictionary) -> void:
	_update_connection(value)
	_clear(rows)
	_clear(games)
	summary_label.text = ""
	var state := str(value.get("status", "idle"))
	status_label.text = str(value.get("message", ""))
	if state == "auth_required":
		status_label.text = str(value.get("message", "리체스 연결이 필요합니다. 연결키(토큰)를 넣어 주세요."))
		if status_label.text.is_empty():
			status_label.text = "리체스 연결이 필요합니다. 연결키(토큰)를 넣어 주세요."
		select_section(2)
		_connection_auto = true
	elif state == "rate_limited":
		status_label.text = "리체스 조회가 잠시 제한되었습니다. %d초 후 다시 조회해 주세요." % int(ceil(float(value.get("retry_after", 60))))
	elif state == "loading":
		status_label.text = "리체스 통계를 조회합니다…"
	elif state == "ready":
		if _connection_auto:
			select_section(0)
		var count := int(value.get("total", 0))
		status_label.text = "현재 위치의 대국이 없습니다." if count == 0 else "현재 위치에서 실제로 둔 다음 수"
		var date := local_query_time(value.get("fetched_at"))
		summary_label.text = "%s · %d판\n%s%s\n백승 / 무승부 / 흑승 · 승률은 최선 수의 증명이 아닙니다." % ["마스터 자료" if value.get("source") == "masters" else "리체스 일반 대국", count, date, " · 저장된 조회" if value.get("cached", false) else ""]
		for move in value.get("moves", []):
			if move is Dictionary:
				_add_move(move)
		if not value.get("games", []).is_empty():
			games.add_child(_label("참고 기보 ↗", 13))
		else:
			games.add_child(_label("현재 위치의 참고 기보가 없습니다.", 12))
		for game in value.get("games", []):
			if game is Dictionary:
				_add_game(game)


func _add_move(move: Dictionary) -> void:
	var row := Button.new()
	row.name = "ExplorerMove" + str(move.get("uci", ""))
	row.custom_minimum_size.y = 76
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.tooltip_text = str(move.get("san", "")) + "부터 별도 분석에서 이어가기"
	var inner := VBoxContainer.new()
	inner.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	inner.offset_left = 7
	inner.offset_right = -7
	inner.offset_top = 4
	inner.offset_bottom = -4
	inner.mouse_filter = Control.MOUSE_FILTER_IGNORE
	inner.add_theme_constant_override("separation", 1)
	row.add_child(inner)
	var title := _label("%s · %d판 · %.1f%% 선택" % [str(move.get("san", "")), int(move.get("count", 0)), float(move.get("play_pct", 0))], 16)
	title.autowrap_mode = TextServer.AUTOWRAP_OFF
	title.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	title.clip_text = true
	title.mouse_filter = Control.MOUSE_FILTER_IGNORE
	inner.add_child(title)
	var bar := HBoxContainer.new()
	bar.custom_minimum_size.y = 10
	bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
	bar.add_theme_constant_override("separation", 0)
	for item in [["white_pct", "e6e8e8"], ["draw_pct", "8294a7"], ["black_pct", "27384c"]]:
		var share := clampf(float(move.get(item[0], 0)), 0, 100)
		if share <= 0:
			continue
		var segment := ColorRect.new()
		segment.color = Color(item[1])
		segment.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		segment.size_flags_stretch_ratio = share
		segment.mouse_filter = Control.MOUSE_FILTER_IGNORE
		bar.add_child(segment)
	inner.add_child(bar)
	var percentages := _label("백 %.1f%% · 무 %.1f%% · 흑 %.1f%%" % [float(move.get("white_pct", 0)), float(move.get("draw_pct", 0)), float(move.get("black_pct", 0))], 14)
	percentages.mouse_filter = Control.MOUSE_FILTER_IGNORE
	inner.add_child(percentages)
	var fen_after := str(move.get("fen_after", ""))
	row.disabled = fen_after.is_empty()
	row.pressed.connect(func(): explore_position.emit(fen_after))
	rows.add_child(row)


func _add_game(game: Dictionary) -> void:
	var url := str(game.get("url", ""))
	if not safe_game_url(url):
		return
	var link := Button.new()
	link.text = str(game.get("white", "백")) + " — " + str(game.get("black", "흑"))
	link.tooltip_text = str(game.get("year", "")) + " · 리체스에서 기보 열기"
	link.add_theme_font_size_override("font_size", 15)
	link.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	link.pressed.connect(func(): OS.shell_open(url))
	games.add_child(link)


static func safe_game_url(url: String) -> bool:
	var allowed := RegEx.new()
	allowed.compile("^https://lichess\\.org/(?:[A-Za-z0-9]{8,12}|[A-Za-z0-9]{8}/(?:white|black)|import/master/[A-Za-z0-9]{8}/(?:white|black))$")
	return allowed.search(url) != null


static func style_tabs(tabs: TabBar) -> void:
	for entry in [["tab_selected", "29495b"], ["tab_unselected", "172c3e"], ["tab_hovered", "223e51"]]:
		var style := StyleBoxFlat.new()
		style.bg_color = Color(entry[1])
		style.set_corner_radius_all(5)
		style.content_margin_left = 12
		style.content_margin_right = 12
		style.content_margin_top = 6
		style.content_margin_bottom = 6
		tabs.add_theme_stylebox_override(entry[0], style)
	tabs.add_theme_font_size_override("font_size", 14)
	tabs.add_theme_color_override("font_selected_color", Color("f6f0de"))
	tabs.add_theme_color_override("font_unselected_color", Color("a3b6c8"))


static func local_query_time(value: Variant) -> String:
	if not value is String or value.length() < 19:
		return "조회 시각 없음"
	# The host emits UTC ISO timestamps. Convert for the native user's clock,
	# as the browser does, rather than silently showing UTC as local time.
	var utc: int = Time.get_unix_time_from_datetime_string(value.substr(0, 19))
	var local: int = utc + int(Time.get_time_zone_from_system().get("bias", 0)) * 60
	return Time.get_datetime_string_from_unix_time(local).replace("T", " ").left(16) + " · 내 컴퓨터 시간"
