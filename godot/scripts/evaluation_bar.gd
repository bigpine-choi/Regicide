extends Control
## A white-perspective engine estimate, never a probability of winning.
## Presentation alone lives here; the native client does not evaluate chess.

var board: Variant = null
var view_from_white: bool = true
var has_score: bool = false
var white_share: float = 0.5
var summary_text: String = "형세 분석 대기"
var detail_text: String = "현재 체스판의 분석을 기다립니다."
var snapshot: Dictionary = {}


func _ready() -> void:
	custom_minimum_size.x = 40
	size_flags_vertical = Control.SIZE_EXPAND_FILL
	# This overlay must not intercept clicks or drags on the board beneath it.
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	resized.connect(queue_redraw)


func present(value: Dictionary, from_white: bool) -> void:
	snapshot = value.duplicate(true)
	view_from_white = from_white
	has_score = false
	white_share = 0.5
	var status: String = str(value.get("status", "waiting"))
	var result: String = str(value.get("result", "*"))
	if status == "terminal" and result in ["1-0", "0-1", "1/2-1/2"]:
		has_score = true
		white_share = 1.0 if result == "1-0" else (0.0 if result == "0-1" else 0.5)
		summary_text = "백 승리" if result == "1-0" else ("흑 승리" if result == "0-1" else "무승부")
		detail_text = "대국 결과 · " + result
	elif status in ["ready", "searching"] and value.get("mate") != null:
		var mate: int = int(value.mate)
		# Zero is a real UCI mate score. Never turn it into a balanced position.
		if mate == 0:
			var winner: String = str(value.get("winner", ""))
			if winner not in ["white", "black"]:
				var turn: String = str(value.get("turn", ""))
				if turn.is_empty():
					turn = "white" if str(value.get("fen", "")).get_slice(" ", 1) == "w" else "black"
				winner = "black" if turn == "white" else "white"
			white_share = 1.0 if winner == "white" else 0.0
			summary_text = ("백" if winner == "white" else "흑") + " 체크메이트"
		else:
			white_share = 1.0 if mate > 0 else 0.0
			summary_text = "%s 메이트 예고 · M%d" % ["백" if mate > 0 else "흑", absi(mate)]
		has_score = true
		detail_text = _search_detail(value, status)
	elif status in ["ready", "searching"] and value.get("cp") != null:
		var cp: int = int(value.cp)
		var bound: String = str(value.get("bound", "exact"))
		var prefix: String = "≥ " if bound == "lower" else ("≤ " if bound == "upper" else "")
		var score: String = "%+.2f" % (float(cp) / 100.0) if cp != 0 else "0.00"
		var verdict: String = "백 우세" if cp > 0 else ("흑 우세" if cp < 0 else "균형")
		if bound in ["lower", "upper"]:
			verdict = "형세 하한" if bound == "lower" else "형세 상한"
		summary_text = "%s · %s%s" % [verdict, prefix, score]
		# The fill is a visual scale, not an estimated win rate.
		var share: Variant = value.get("white_share")
		white_share = clampf(float(share), 0.0, 1.0) if share != null else 0.5 + 0.5 * float(cp) / (absf(float(cp)) + 400.0)
		has_score = true
		detail_text = _search_detail(value, status)
	else:
		match status:
			"unavailable":
				summary_text = "형세 분석 없음"
				detail_text = str(value.get("message", "현재 체스판의 평가값을 사용할 수 없습니다."))
			"searching":
				summary_text = "형세 계산 중…"
				detail_text = "현재 체스판의 첫 분석값을 기다립니다."
			_:
				summary_text = "형세 분석 대기"
				detail_text = "현재 체스판의 분석을 기다립니다."
	var reliability: String = str(value.get("reliability", ""))
	var labels := {"provisional": "잠정 평가", "stable": "탐색 안정", "volatile": "평가 변동 큼", "bounded": "범위 평가"}
	var explanations := {
		"provisional": "탐색이 더 진행되면 평가가 달라질 수 있습니다.",
		"stable": "최근 탐색값이 비슷합니다. 정답을 보장하지는 않습니다.",
		"volatile": "최근 탐색에서 평가가 크게 달라졌습니다.",
		"bounded": "상한·하한 형태로 얻은 탐색 평가입니다."}
	var show_reliability: bool = has_score and status in ["ready", "searching"] and labels.has(reliability)
	if show_reliability:
		summary_text += " · " + labels[reliability]
		detail_text += " · " + labels[reliability]
	tooltip_text = summary_text + "\n" + detail_text + "\n+는 백, −는 흑 우세입니다. 1.00은 폰 한 개에 해당하는 평가 단위입니다.\n막대는 엔진의 추정 형세이며 승률이 아닙니다."
	if show_reliability:
		tooltip_text += "\n" + explanations[reliability]
	queue_redraw()


func _search_detail(value: Dictionary, status: String) -> String:
	var detail: String = "직전 탐색 · 백 기준" if value.get("source", "search") == "previous_search" else "자체 엔진 추정 · 백 기준"
	if value.get("depth") != null:
		detail += " · 깊이 %d" % int(value.depth)
	var bound: String = str(value.get("bound", "exact"))
	if bound in ["lower", "upper"]:
		detail += " · " + ("하한" if bound == "lower" else "상한") + " 추정"
	if status == "searching":
		detail += " · 계산 중"
	return detail


func bar_rect() -> Rect2:
	# Keep the fill aligned with the actual square board, including small windows.
	var area := Rect2(0.0, 27.0, 0.0, maxf(0.0, size.y - 54.0))
	if is_instance_valid(board):
		area = board.get_board_rect()
	return Rect2(Vector2(area.position.x - 50.0 if is_instance_valid(board) else 9.0, area.position.y), Vector2(22.0, area.size.y))


func white_rect() -> Rect2:
	var area := bar_rect()
	var height: float = area.size.y * white_share
	return Rect2(Vector2(area.position.x, area.end.y - height if view_from_white else area.position.y), Vector2(area.size.x, height))


func _draw() -> void:
	var area := bar_rect()
	if area.size.y <= 0.0:
		return
	draw_rect(area, Color("29394b") if has_score else Color("455568"))
	if has_score:
		draw_rect(white_rect(), Color("f3eddb"))
		draw_line(Vector2(area.position.x - 2.0, area.get_center().y), Vector2(area.end.x + 2.0, area.get_center().y), Color("83a7aa"), 1.0)
	draw_rect(area, Color("8197aa"), false, 1.0)
	var font := get_theme_default_font()
	var upper: String = "흑" if view_from_white else "백"
	var lower: String = "백" if view_from_white else "흑"
	for label in [[upper, area.position.y - 8.0], [lower, area.end.y + 20.0]]:
		var width: float = font.get_string_size(str(label[0]), HORIZONTAL_ALIGNMENT_LEFT, -1, 12).x
		draw_string(font, Vector2(area.get_center().x - width / 2.0, float(label[1])), str(label[0]), HORIZONTAL_ALIGNMENT_LEFT, -1, 12, Color("b4c7d3"))
