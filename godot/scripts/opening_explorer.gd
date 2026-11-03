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
var enabled_toggle: CheckButton
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
var _generation: int = 0
var _context_key: String = ""
var _request_key: String = ""
var _poll_timer: Timer
var _network_busy: bool = false
var _token_busy: bool = false
var _suspended: bool = false


func _ready() -> void:
	name = "OpeningExplorer"
	add_theme_constant_override("separation", 6)
	enabled_toggle = CheckButton.new()
	enabled_toggle.name = "ExplorerEnabled"
	enabled_toggle.text = "통계형 오프닝 탐색기 · 켜기"
	enabled_toggle.add_theme_font_size_override("font_size", 14)
	enabled_toggle.toggled.connect(_toggle)
	add_child(enabled_toggle)
	contents = VBoxContainer.new()
	contents.name = "ExplorerContents"
	contents.add_theme_constant_override("separation", 6)
	contents.hide()
	add_child(contents)
	contents.add_child(_label("켜면 현재 위치를 리체스로 조회합니다.\n통계와 Regicide의 평가는 서로 다른 자료입니다.", 12))
	source_option = OptionButton.new()
	source_option.name = "ExplorerSource"
	source_option.add_item("마스터 대국", 0)
	source_option.add_item("리체스 일반 대국", 1)
	source_option.item_selected.connect(_filter_changed)
	contents.add_child(source_option)
	filter_box = VBoxContainer.new()
	filter_box.hide()
	contents.add_child(filter_box)
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
	status_label = _label("탐색기를 켜고 리체스를 연결해 주세요.", 13)
	status_label.name = "ExplorerStatus"
	contents.add_child(status_label)
	summary_label = _label("", 12)
	summary_label.name = "ExplorerSummary"
	contents.add_child(summary_label)
	rows = VBoxContainer.new()
	rows.name = "ExplorerMoves"
	rows.add_theme_constant_override("separation", 7)
	contents.add_child(rows)
	games = VBoxContainer.new()
	games.name = "ExplorerGames"
	contents.add_child(games)
	contents.add_child(_label("리체스 연결", 13))
	connection_label = _label("", 12)
	connection_label.name = "ExplorerConnectionStatus"
	contents.add_child(connection_label)
	var issue := Button.new()
	issue.name = "ExplorerIssueToken"
	issue.text = "리체스 연결키 만들기 ↗"
	issue.pressed.connect(func(): OS.shell_open(TOKEN_URL))
	contents.add_child(issue)
	contents.add_child(_label("① 리체스에 로그인하세요.\n② 설명에 Regicide를 적고, 권한은 체크하지 않은 채 연결키(토큰)를 만드세요.\n③ 만들어진 긴 문자열을 복사해 아래에 붙여넣으세요. 채팅에 보내지 마세요.", 12))
	token_input = LineEdit.new()
	token_input.name = "ExplorerToken"
	token_input.secret = true
	token_input.placeholder_text = "리체스 연결키(토큰) 붙여넣기"
	token_input.max_length = 1024
	contents.add_child(token_input)
	remember_token = CheckBox.new()
	remember_token.name = "ExplorerRemember"
	remember_token.text = "이 Windows 계정에 연결 기억"
	remember_token.button_pressed = OS.get_name() == "Windows"
	remember_token.disabled = OS.get_name() != "Windows"
	remember_token.add_theme_font_size_override("font_size", 12)
	contents.add_child(remember_token)
	token_button = Button.new()
	token_button.name = "ExplorerConnect"
	token_button.text = "리체스 연결"
	token_button.pressed.connect(_save_token)
	contents.add_child(token_button)
	var refresh := Button.new()
	refresh.name = "ExplorerRefresh"
	refresh.text = "현재 위치 다시 조회"
	refresh.pressed.connect(_refresh)
	contents.add_child(refresh)
	_poll_timer = Timer.new()
	_poll_timer.wait_time = 0.5
	_poll_timer.one_shot = true
	_poll_timer.timeout.connect(_poll)
	add_child(_poll_timer)
	visibility_changed.connect(_visibility_changed)


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
		return
	_context_key = key
	_invalidate()
	if is_node_ready() and enabled_toggle.button_pressed and is_visible_in_tree():
		_query()


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
	if is_visible_in_tree() and enabled_toggle.button_pressed:
		_query()


func _toggle(on: bool) -> void:
	contents.visible = on
	enabled_toggle.text = "통계형 오프닝 탐색기 · 켜짐" if on else "통계형 오프닝 탐색기 · 켜기"
	_invalidate()
	if on:
		_query()
	else:
		token_input.clear()


func _filter_changed(_index: int) -> void:
	filter_box.visible = source_option.selected == 1
	_refresh()


func _refresh() -> void:
	_invalidate()
	if enabled_toggle.button_pressed:
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
	return {"enabled": enabled_toggle.button_pressed, "source": source_option.selected,
		"rating": rating_option.selected, "speed": speed_option.selected}


func apply_preferences(value: Dictionary) -> void:
	source_option.select(clampi(int(value.get("source", 0)), 0, source_option.item_count - 1))
	rating_option.select(clampi(int(value.get("rating", 0)), 0, rating_option.item_count - 1))
	speed_option.select(clampi(int(value.get("speed", 0)), 0, speed_option.item_count - 1))
	filter_box.visible = source_option.selected == 1
	var enabled: bool = value.get("enabled", false)
	enabled_toggle.set_pressed_no_signal(enabled)
	_toggle(enabled)


func _query() -> void:
	if _suspended or _token_busy or not enabled_toggle.button_pressed or not is_visible_in_tree():
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
	if generation != _generation or not is_visible_in_tree() or not enabled_toggle.button_pressed:
		return
	_network_busy = false
	_accept_response(response)


func _poll() -> void:
	if _suspended or _network_busy or _token_busy or not enabled_toggle.button_pressed or not is_visible_in_tree():
		return
	var generation := _generation
	_network_busy = true
	var response: Dictionary = await _request()
	if generation != _generation or not is_visible_in_tree() or not enabled_toggle.button_pressed:
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
		connection_label.text = str(response.get("message", "연결 설정을 저장했습니다."))
		status_label.text = "현재 위치의 통계를 확인합니다."
		_query()


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
	return a == b


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
	_clear(rows)
	_clear(games)
	summary_label.text = ""
	var state := str(value.get("status", "idle"))
	status_label.text = str(value.get("message", ""))
	if state == "auth_required":
		status_label.text = "리체스 연결이 필요합니다. 아래에 연결키(토큰)를 넣어 주세요."
	elif state == "rate_limited":
		status_label.text = "리체스 조회가 잠시 제한되었습니다. %d초 후 다시 조회해 주세요." % int(ceil(float(value.get("retry_after", 60))))
	elif state == "loading":
		status_label.text = "리체스 통계를 조회합니다…"
	elif state == "ready":
		var count := int(value.get("total", 0))
		status_label.text = "현재 위치의 대국이 없습니다." if count == 0 else "현재 위치에서 실제로 둔 다음 수"
		var date := local_query_time(value.get("fetched_at"))
		summary_label.text = "%s · %d판\n%s%s\n백승 / 무승부 / 흑승 · 승률은 최선 수의 증명이 아닙니다." % ["마스터 자료" if value.get("source") == "masters" else "리체스 일반 대국", count, date, " · 저장된 조회" if value.get("cached", false) else ""]
		for move in value.get("moves", []):
			if move is Dictionary:
				_add_move(move)
		if not value.get("games", []).is_empty():
			games.add_child(_label("참고 기보 ↗", 13))
		for game in value.get("games", []):
			if game is Dictionary:
				_add_game(game)


func _add_move(move: Dictionary) -> void:
	var row := VBoxContainer.new()
	row.add_theme_constant_override("separation", 2)
	var heading := HBoxContainer.new()
	var title := _label("%s · %d판 · %.1f%% 선택" % [str(move.get("san", "")), int(move.get("count", 0)), float(move.get("play_pct", 0))], 13)
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	heading.add_child(title)
	row.add_child(heading)
	var bar := HBoxContainer.new()
	bar.custom_minimum_size.y = 12
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
	row.add_child(bar)
	row.add_child(_label("백 %.1f%% · 무 %.1f%% · 흑 %.1f%%" % [float(move.get("white_pct", 0)), float(move.get("draw_pct", 0)), float(move.get("black_pct", 0))], 12))
	var advance := Button.new()
	advance.text = str(move.get("san", "")) + " · 이 수로 분석 탐색"
	advance.add_theme_font_size_override("font_size", 12)
	var fen_after := str(move.get("fen_after", ""))
	advance.disabled = fen_after.is_empty()
	advance.pressed.connect(func(): explore_position.emit(fen_after))
	row.add_child(advance)
	rows.add_child(row)


func _add_game(game: Dictionary) -> void:
	var url := str(game.get("url", ""))
	if not safe_game_url(url):
		return
	var link := Button.new()
	link.text = str(game.get("white", "백")) + " — " + str(game.get("black", "흑"))
	link.tooltip_text = str(game.get("year", "")) + " · 리체스에서 기보 열기"
	link.add_theme_font_size_override("font_size", 12)
	link.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	link.pressed.connect(func(): OS.shell_open(url))
	games.add_child(link)


static func safe_game_url(url: String) -> bool:
	var allowed := RegEx.new()
	allowed.compile("^https://lichess\\.org/(?:[A-Za-z0-9]{8,12}|[A-Za-z0-9]{8}/(?:white|black)|import/master/[A-Za-z0-9]{8}/(?:white|black))$")
	return allowed.search(url) != null


static func local_query_time(value: Variant) -> String:
	if not value is String or value.length() < 19:
		return "조회 시각 없음"
	# The host emits UTC ISO timestamps. Convert for the native user's clock,
	# as the browser does, rather than silently showing UTC as local time.
	var utc: int = Time.get_unix_time_from_datetime_string(value.substr(0, 19))
	var local: int = utc + int(Time.get_time_zone_from_system().get("bias", 0)) * 60
	return Time.get_datetime_string_from_unix_time(local).replace("T", " ").left(16) + " · 내 컴퓨터 시간"
