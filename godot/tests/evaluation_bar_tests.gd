extends SceneTree
## White-perspective scores and stale-response protection in the shipped scene.

const EvaluationBar = preload("res://scripts/evaluation_bar.gd")
var checks: int = 0
var failures: int = 0


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func fixture(game_id: String = "evaluation-game", revision: int = 1) -> Dictionary:
	return {"game_id": game_id, "revision": revision, "fen": "8/8/8/8/8/8/4P3/4K2k w - - 0 1",
		"board": {"e2": "P", "e1": "K", "h1": "k"}, "turn": "white",
		"human_color": "white", "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["e2e3", "e2e4", "e1d1"], "moves": [], "last_move": null,
		"in_check": false, "result": "*", "can_undo": false, "status": "대국 중",
		"termination": null, "error": null, "engine": {"name": "Regicide", "movetime_ms": 1000}}


func score_for(position: Dictionary, cp: Variant = 100) -> Dictionary:
	return {"game_id": position.game_id, "revision": position.revision, "fen": position.fen,
		"status": "ready", "cp": cp, "mate": null, "depth": 5, "bound": "exact",
		"white_share": null, "result": "*", "source": "search"}


func _run() -> void:
	root.size = Vector2i(1240, 860)
	var bar := EvaluationBar.new()
	bar.size = Vector2(40, 400)
	root.add_child(bar)
	await process_frame
	var position := fixture()
	var score := score_for(position, 250)
	bar.present(score, true)
	verify(bar.has_score and bar.summary_text.contains("백 우세") and bar.summary_text.contains("+2.50"), "positive cp is a white advantage in pawn units")
	verify(bar.white_share > 0.5, "white advantage enlarges white fill")
	verify(is_equal_approx(bar.white_rect().end.y, bar.bar_rect().end.y), "white fill starts at bottom in white view")
	bar.present(score, false)
	verify(is_equal_approx(bar.white_rect().position.y, bar.bar_rect().position.y), "flipped view anchors white fill at top")
	verify(bar.summary_text.contains("+2.50"), "flipping never changes score sign")
	bar.present(score_for(position, -175), false)
	verify(bar.summary_text.contains("흑 우세") and bar.summary_text.contains("-1.75") and bar.white_share < 0.5, "negative cp is always a black advantage")
	bar.present(score_for(position, 0), true)
	verify(bar.has_score and bar.summary_text == "균형 · 0.00" and is_equal_approx(bar.white_share, 0.5), "real zero is an available balanced score")
	bar.present({"status": "waiting"}, true)
	verify(not bar.has_score and not bar.summary_text.contains("0.00"), "missing evaluation is not a zero score")
	bar.present({"status": "searching", "cp": null, "mate": null}, true)
	verify(not bar.has_score and bar.summary_text.contains("계산 중"), "search before first completed depth stays unknown")
	bar.present({"status": "unavailable"}, true)
	verify(not bar.has_score and bar.summary_text.contains("없음"), "unavailable analysis stays grey")
	score = score_for(position)
	score["source"] = "previous_search"
	bar.present(score, true)
	verify(bar.detail_text.contains("직전 탐색"), "retained root search is identified rather than claiming fresh analysis")
	for bound in ["lower", "upper"]:
		score["bound"] = bound
		bar.present(score, true)
		verify(bar.summary_text.contains("≥" if bound == "lower" else "≤"), "nonexact search bound has visible inequality")
		verify(not bar.summary_text.contains("백 우세"), "a bound is not presented as an exact advantage")
	for mate in [3, -4]:
		score = score_for(position, null)
		score["mate"] = mate
		bar.present(score, true)
		verify(bar.has_score and bar.summary_text.contains("M%d" % absi(mate)), "mate distance is visible")
		verify(bar.white_share == (1.0 if mate > 0 else 0.0), "mate score uses white perspective")
	for winner in ["white", "black"]:
		score["mate"] = 0
		score["winner"] = winner
		score["turn"] = winner  # Explicit winner, not turn, controls signed mate zero.
		bar.present(score, true)
		verify(bar.summary_text == ("백" if winner == "white" else "흑") + " 체크메이트", "mate zero does not become a draw")
	for result in ["1-0", "0-1", "1/2-1/2"]:
		bar.present({"status": "terminal", "result": result, "cp": null}, true)
		verify(bar.has_score and bar.white_share == (1.0 if result == "1-0" else (0.0 if result == "0-1" else 0.5)), "terminal result determines exact bar fill")
	score = score_for(position, 125)
	bar.present(score, true)
	var original_share: float = bar.white_share
	score.merge({"recent_cp_range_white": [-900, -300], "score_swing_cp": 600, "stable_depths": 3}, true)
	for row in [["provisional", "잠정 평가"], ["stable", "탐색 안정"], ["volatile", "평가 변동 큼"], ["bounded", "범위 평가"]]:
		score.reliability = row[0]
		bar.present(score, false)
		verify(bar.summary_text.contains(row[1]) and bar.detail_text.contains(row[1]) and bar.tooltip_text.contains(row[1]), "reliability label appears in summary, detail and tooltip: " + row[0])
		verify(bar.summary_text.contains("+1.25") and bar.white_share == original_share, "reliability metadata never replaces the current score or fill")
		if row[0] == "stable":
			verify(bar.tooltip_text.contains("정답을 보장하지는 않습니다"), "stable search is not a correctness guarantee")
	score.reliability = "constructor"
	bar.present(score, true)
	verify(bar.summary_text == "백 우세 · +1.25", "unknown reliability is ignored for compatibility")
	bar.present({"status": "terminal", "result": "0-1", "reliability": "stable"}, true)
	verify(bar.summary_text == "흑 승리", "terminal outcome is not relabelled as uncertain search")
	score = score_for(position, 125)
	score["status"] = "searching"
	score["refinement"] = {"phase": "refining"}
	bar.present(score, true)
	verify(bar.detail_text.contains("정밀 분석 중") and bar.summary_text.contains("+1.25"), "refining preserves raw score and explains continued search")
	score["status"] = "ready"
	bar.present(score, true)
	verify(not bar.detail_text.contains("정밀 분석 중"), "completed evaluation never claims an ongoing refinement")
	bar.queue_free()
	await process_frame
	var app: Variant = load("res://main.tscn").instantiate()
	app.offline_mode = true
	root.add_child(app)
	await process_frame
	await process_frame
	var board_area: Rect2 = app.board.get_board_rect()
	var bar_area: Rect2 = app.evaluation_bar.bar_rect()
	verify(bar_area.end.x < board_area.position.x and board_area.position.x - bar_area.end.x < 40.0,
		"bar stays beside the actual centered squares rather than the column edge")
	verify(is_equal_approx(bar_area.position.y, board_area.position.y) and is_equal_approx(bar_area.size.y, board_area.size.y),
		"evaluation fill shares the square board's vertical extent")
	verify(app.evaluation_bar.mouse_filter == Control.MOUSE_FILTER_IGNORE, "evaluation overlay preserves board click and drag input")
	app.accept_state(position)
	score = score_for(position, 240)
	score.reliability = "provisional"
	verify(app.accept_evaluation(score), "current-position analysis is accepted")
	verify(app.evaluation_summary.text.contains("+2.40"), "live scene renders the accepted score")
	verify(app.evaluation_detail.text.contains("잠정 평가"), "live scene exposes separate evaluation reliability")
	var before: String = app.evaluation_summary.text
	var original_view: bool = app.evaluation_bar.view_from_white
	app._on_flip_pressed()
	verify(app.evaluation_summary.text == before and app.evaluation_bar.view_from_white != original_view, "scene flip changes bar orientation and preserves score")
	var wrong_fen: Dictionary = score.duplicate(true)
	wrong_fen["fen"] = "8/8/8/8/8/4P3/8/4K2k b - - 0 1"
	verify(not app.accept_evaluation(wrong_fen), "wrong-position callback is rejected even at same revision")
	verify(app.evaluation_detail.text.contains("잠정 평가"), "rejected position cannot replace reliability text")
	var changed: Dictionary = fixture("evaluation-game", 2)
	app.accept_state(changed)
	verify(not app.evaluation_bar.has_score, "a changed revision clears the old score immediately")
	verify(not app.accept_evaluation(score), "older-revision response is rejected")
	# The backend's board revision keeps increasing across new games. A late
	# response must never roll the active game or its evaluation backwards.
	app.accept_state(fixture("new-game", 3))
	verify(app.state.game_id == "new-game" and int(app.state.revision) == 3,
		"new-game response advances the monotonic board revision")
	verify(not app.accept_evaluation(score), "new game rejects the previous game's evaluation")
	app.accept_state(changed)
	verify(app.state.game_id == "new-game" and int(app.state.revision) == 3,
		"delayed previous-game state cannot replace the current game")
	position = fixture("review-game", 4)
	score = score_for(position, 240)
	app.accept_state(position)
	verify(app.accept_evaluation(score), "fresh live position accepts its matching score before review")
	app.review_mode = true
	app.review_frames = [position.duplicate(true)]
	app.review_moves = []
	app.review_summary = {}
	app.review_ply = 0
	app._render()
	verify(not app.evaluation_bar.has_score and app.evaluation_detail.text.contains("저장된 평가값"), "replay never borrows the live score")
	verify(not app.accept_evaluation(score), "in-flight live response is ignored during replay")
	app.review_frames[0]["evaluation"] = score.duplicate(true)
	app._render()
	verify(app.evaluation_summary.text.contains("+2.40"), "a frame's own matching saved evaluation may be shown")
	app.review_frames[0]["evaluation"]["fen"] = "different position"
	app._render()
	verify(not app.evaluation_bar.has_score, "mismatched saved-frame evaluation stays unavailable")
	app._exit_review()
	verify(app.evaluation_summary.text.contains("+2.40"), "returning to live play restores its matching score")
	app.closing = true
	verify(not app.accept_evaluation(score), "late responses are ignored during shutdown")
	app.queue_free()
	await process_frame
	print("Godot evaluation-bar checks: %d, failures: %d" % [checks, failures])
	quit(0 if failures == 0 else 1)
