extends VBoxContainer
## Historical game results, always in White's point of view.

signal source_changed
signal refresh_requested

var source_option: OptionButton
var summary: Label
var results: Label
var detail: Label
var rows: VBoxContainer


func _ready() -> void:
	name = "OpeningStatistics"
	add_theme_constant_override("separation", 6)
	var title := Label.new()
	title.text = "오프닝 통계"
	title.add_theme_font_size_override("font_size", 17)
	add_child(title)
	source_option = OptionButton.new()
	source_option.name = "OpeningStatisticsSource"
	source_option.add_item("내 완료 기보", 0)
	source_option.add_item("Lichess 마스터 기보 · 온라인", 1)
	source_option.item_selected.connect(func(_index: int) -> void: source_changed.emit())
	source_option.tooltip_text = "온라인 마스터 기보는 REGICIDE_LICHESS_TOKEN 설정이 필요합니다."
	add_child(source_option)
	var refresh := Button.new()
	refresh.text = "통계 다시 조회"
	refresh.pressed.connect(func() -> void: refresh_requested.emit())
	add_child(refresh)
	summary = _text("현재 위치의 기보를 조회합니다.", 14)
	results = _text("", 13)
	detail = _text("실제 기보 결과 · 백승 / 무승부 / 흑승 순서", 12)
	detail.add_theme_color_override("font_color", Color("8197aa"))
	rows = VBoxContainer.new()
	rows.name = "OpeningStatisticsMoves"
	rows.add_theme_constant_override("separation", 8)
	add_child(rows)


func _text(value: String, font_size: int) -> Label:
	var label := Label.new()
	label.text = value
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.add_theme_font_size_override("font_size", font_size)
	add_child(label)
	return label


func selected_source() -> String:
	return "masters" if source_option != null and source_option.selected == 1 else "local"


func _clear_rows() -> void:
	for row in rows.get_children():
		rows.remove_child(row)
		row.queue_free()


func show_loading() -> void:
	if summary == null:
		return
	_clear_rows()
	summary.text = "현재 위치의 기보를 조회하고 있습니다."
	results.text = ""
	detail.text = "내 완료 기보" if selected_source() == "local" else "Lichess Opening Explorer · 마스터 기보"


func present(value: Dictionary) -> void:
	if summary == null:
		return
	_clear_rows()
	results.text = ""
	var label := str(value.get("source_label", "내 완료 기보" if selected_source() == "local" else "Lichess 마스터 기보"))
	var status := str(value.get("status", "unavailable"))
	var total := int(value.get("total", 0))
	if status != "ready" or total <= 0:
		summary.text = "표본 없음" if status == "empty" or (status == "ready" and total <= 0) else "통계를 불러오지 못했습니다."
		if status == "auth_required":
			summary.text = "Lichess 토큰 설정 필요"
		elif status == "rate_limited":
			summary.text = "조회 제한 · 잠시 후 다시 시도하세요"
		detail.text = label + "\n" + str(value.get("message", "이 위치와 일치하는 완료 기보가 없습니다."))
		return
	summary.text = "%s · %s판" % [label, _count(total)]
	var opening: Variant = value.get("opening")
	if opening is Dictionary and not str(opening.get("name", "")).is_empty():
		summary.text += "\n" + str(opening.get("eco", "")) + " " + str(opening.name)
	results.text = "백승 %.1f%% / 무 %.1f%% / 흑승 %.1f%%" % [
		float(value.get("white_pct", 0.0)), float(value.get("draw_pct", 0.0)), float(value.get("black_pct", 0.0))]
	detail.text = "실제 기보 결과 · 엔진 평가와 별도\n다음 수 · 선택 비율 / 기보 수 / 백승·무·흑승"
	var moves: Array = value.get("moves", [])
	for move in moves.slice(0, 12):
		if not move is Dictionary or int(move.get("total", 0)) <= 0:
			continue
		var row := VBoxContainer.new()
		row.add_theme_constant_override("separation", 1)
		var heading := Label.new()
		heading.text = "%s   %.1f%% · %s판" % [str(move.get("san", move.get("uci", ""))),
			float(move.get("frequency_pct", 0.0)), _count(int(move.get("total", 0)))]
		heading.add_theme_font_size_override("font_size", 14)
		row.add_child(heading)
		var outcome := Label.new()
		outcome.text = "백 %.1f%% / 무 %.1f%% / 흑 %.1f%%" % [float(move.get("white_pct", 0.0)),
			float(move.get("draw_pct", 0.0)), float(move.get("black_pct", 0.0))]
		outcome.add_theme_font_size_override("font_size", 12)
		outcome.add_theme_color_override("font_color", Color("a3b6c8"))
		row.add_child(outcome)
		rows.add_child(row)
	if rows.get_child_count() == 0:
		var end := Label.new()
		end.text = "등록된 다음 수가 없습니다."
		rows.add_child(end)


func _count(value: int) -> String:
	var raw := str(value)
	var result := ""
	for index in range(raw.length()):
		if index > 0 and (raw.length() - index) % 3 == 0:
			result += ","
		result += raw[index]
	return result
