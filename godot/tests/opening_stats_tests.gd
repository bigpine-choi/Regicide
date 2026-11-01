extends SceneTree
## Isolated scene tests: analysis input, historical-result semantics, stale replies.

class RecordingApp:
	extends "res://scripts/main.gd"
	var submitted: Dictionary = {}
	func _analysis_mutate(path: String, payload: Dictionary = {}) -> void:
		submitted = {"path": path, "payload": payload.duplicate(true)}


var app: Variant
var checks := 0
var failures := 0


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func fixture() -> Dictionary:
	var pieces: Dictionary = {}
	for file_index in range(8):
		var file_name: String = "abcdefgh"[file_index]
		pieces[file_name + "1"] = "RNBQKBNR"[file_index]
		pieces[file_name + "2"] = "P"
		pieces[file_name + "7"] = "p"
		pieces[file_name + "8"] = "rnbqkbnr"[file_index]
	return {"game_id": "live", "revision": 8,
		"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
		"board": pieces,
		"turn": "white", "human_color": "white", "human_to_move": true,
		"engine_to_move": false, "legal_moves": ["e2e4"], "moves": [],
		"last_move": null, "result": "*", "error": null, "status": "당신 차례",
		"can_undo": false, "handicap_mode": false,
		"engine": {"name": "Regicide", "movetime_ms": 1000}}


func stats(position: Dictionary) -> Dictionary:
	return {"fen": position.fen, "source": "local", "source_label": "내 완료 기보",
		"status": "ready", "total": 100, "white": 40, "draws": 30, "black": 30,
		"white_pct": 40.0, "draw_pct": 30.0, "black_pct": 30.0,
		"opening": null, "moves": [{"uci": "e7e5", "san": "e5", "total": 20,
			"frequency_pct": 20.0, "white_pct": 35.0, "draw_pct": 25.0, "black_pct": 40.0}]}


func _run() -> void:
	app = RecordingApp.new()
	app.offline_mode = true
	root.size = Vector2i(1240, 860)
	root.add_child(app)
	await process_frame
	await process_frame
	app.backend_url = "http://127.0.0.1:1"
	var live := fixture()
	app.accept_state(live)
	verify(not app.opening_stats_panel.visible, "normal game does not query or display opening statistics")
	var study: Dictionary = live.duplicate(true)
	study.analysis_mode = true
	study.game_id = "analysis-test"
	study.revision = 1
	study.turn = "black"
	study.fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
	study.board.erase("e2")
	study.board.e4 = "P"
	study.legal_moves = ["e7e5", "e7e6"]
	study.moves = [{"ply": 1, "uci": "e2e4", "san": "e4", "actor": "analysis"}]
	study.can_undo = true
	study.can_redo = false
	app.analysis_mode = true
	app.accept_analysis_state(study)
	verify(app.board.state.fen == study.fen and app.state.fen == live.fen, "analysis displays an independent board and preserves the live game")
	verify(app.opening_stats_panel.visible and app.analysis_controls.visible, "analysis exposes statistics and navigation")
	verify(not app.live_actions.visible and app.hint_button.disabled, "analysis cannot resign or alter the live game and hides live hints")
	app._on_square_pressed("e7")
	verify(app.board_model.selected_square == "e7", "black pieces can be selected during analysis")
	app._on_square_pressed("e5")
	verify(app.submitted.get("path") == "/api/analysis/move" and app.submitted.get("payload", {}).get("move") == "e7e5", "black analysis move uses the independent analysis endpoint")
	app.submitted.clear()
	app._on_drag_requested("e7", "e6")
	verify(app.submitted.get("payload", {}).get("move") == "e7e6", "analysis drag uses the displayed legal moves")
	var value := stats(study)
	var key: String = app._stats_key
	var epoch: int = app._stats_epoch
	verify(app.accept_opening_stats(value, key, epoch), "matching position statistics accepted")
	verify(app.opening_stats_panel.summary.text.contains("100판") and app.opening_stats_panel.results.text.contains("백승 40.0%"), "source, sample count and White results are shown")
	verify(app.opening_stats_panel.rows.get_child_count() == 1 and app.opening_stats_panel.rows.get_child(0).get_child(0).text.contains("20.0%"), "next-move frequency uses backend population percentage")
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--evidence-file" and index + 1 < args.size() and DisplayServer.get_name() != "headless":
			await process_frame
			await RenderingServer.frame_post_draw
			var destination: String = args[index + 1]
			DirAccess.make_dir_recursive_absolute(destination.get_base_dir())
			verify(root.get_texture().get_image().save_png(destination) == OK, "rendered analysis statistics screenshot saved")
	var result_text: String = app.opening_stats_panel.results.text
	app._on_flip_pressed()
	verify(app.opening_stats_panel.results.text == result_text, "board rotation never swaps historical White and Black result percentages")
	var later: Dictionary = study.duplicate(true)
	later.revision = 2
	later.fen = study.fen.replace(" e3 ", " - ")
	app.accept_analysis_state(later)
	verify(not app.accept_opening_stats(value, key, epoch), "late statistics from a previous position cannot overwrite the current position")
	app.accept_analysis_state(study)
	verify(not app.accept_opening_stats(value, key, epoch), "undo to the same FEN does not resurrect a previous request generation")
	value = stats(study)
	value.status = "empty"
	value.total = 0
	value.moves = []
	app.accept_opening_stats(value, app._stats_key, app._stats_epoch)
	verify(app.opening_stats_panel.summary.text == "표본 없음" and app.opening_stats_panel.results.text.is_empty(), "zero games are not presented as zero-percent wins")
	app.opening_stats_panel.source_option.select(1)
	app._on_stats_source_changed()
	verify(not app.accept_opening_stats(stats(study), app._stats_key, app._stats_epoch), "source mismatch is rejected even for the same board")
	value = {"fen": study.fen, "source": "masters", "status": "auth_required", "message": "토큰 설정 필요"}
	app.accept_opening_stats(value, app._stats_key, app._stats_epoch)
	verify(app.opening_stats_panel.summary.text == "Lichess 토큰 설정 필요" and app.opening_stats_panel.results.text.is_empty(), "authentication failure has no invented statistics")
	app._exit_analysis()
	verify(not app.analysis_mode and app.board.state.fen == live.fen and not app.opening_stats_panel.visible, "exit analysis restores the live board and hides statistics")
	verify(not app.accept_opening_stats(value, key, epoch), "late statistics cannot reappear after exiting analysis")
	app.review_mode = true
	app.review_frames = [study.duplicate(true)]
	app.review_moves = []
	app.review_ply = 0
	app._render()
	verify(app.opening_stats_panel.visible and not app._can_select_move(), "read-only game replay also shows position statistics")
	app.analysis_mode = true
	app.accept_analysis_state(study)
	verify(app._can_select_move(), "analysis started from replay permits both sides to move")
	app._exit_analysis()
	verify(app.review_mode and app.board.state.fen == study.fen and not app._can_select_move(), "exit analysis returns to the original read-only replay")
	app.analysis_mode = true
	study.root_fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 42"
	study.moves = [{"uci": "e7e5", "san": "e5"}]
	app.accept_analysis_state(study)
	verify(app.move_history.text.contains("42..."), "analysis move numbers respect a black-to-move custom starting position")
	var promotion: Dictionary = study.duplicate(true)
	promotion.fen = "7k/P7/8/8/8/8/8/7K w - - 0 42"
	promotion.board = {"a7": "P", "h1": "K", "h8": "k"}
	promotion.turn = "white"
	promotion.legal_moves = ["a7a8q", "a7a8r", "a7a8b", "a7a8n"]
	app.accept_analysis_state(promotion)
	app._on_square_pressed("a7")
	app._on_square_pressed("a8")
	verify(app.promotion_panel.visible and app.board_model.pending_promotions.size() == 4, "analysis entered from replay presents explicit promotion choices")
	app._on_promotion_pressed("n")
	verify(app.submitted.get("payload", {}).get("move") == "a7a8n", "analysis preserves chosen underpromotion UCI")
	var drawn: Dictionary = study.duplicate(true)
	drawn.result = "1/2-1/2"
	drawn.human_to_move = true
	app.accept_analysis_state(drawn)
	verify(app._can_select_move() and app._player_text("black", "white").contains("차례"), "a reported automatic draw does not disable a freely playable study position")
	app.closing = true
	app.queue_free()
	await process_frame
	print("opening statistics scene: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)
