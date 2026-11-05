extends SceneTree
## Exercise actual navigation/input/version guards, substituting HTTP only.

const App = preload("res://tests/think_time_capture_scene.gd")
var app: Variant
var failures: int = 0
var checks: int = 0

func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: " + message)

func _initialize() -> void:
	_run.call_deferred()

func fixture() -> Dictionary:
	var first: Dictionary = {"game_id":"live-history", "revision":2,
		"fen":"4k3/4p3/8/8/8/8/4P3/4K3 w - - 0 1",
		"board":{"e1":"K","e8":"k","e2":"P","e7":"p"},
		"turn":"white","human_color":"white","human_to_move":false,"engine_to_move":false,
		"legal_moves":[],"in_check":false,"last_move":null}
	var second: Dictionary = first.duplicate(true)
	second.board.erase("e2"); second.board.e4 = "P"; second.turn = "black"
	second.fen = "4k3/4p3/8/8/4P3/8/8/4K3 b - - 0 1"; second.last_move = "e2e4"
	var third: Dictionary = second.duplicate(true)
	third.board.erase("e7"); third.board.e5 = "p"; third.turn = "white"
	third.fen = "4k3/8/8/4p3/4P3/8/8/4K3 w - - 0 2"; third.last_move = "e7e5"
	return {"game_id":"live-history","revision":2,"frames":[first,second,third],
		"moves":[{"ply":1,"uci":"e2e4","san":"e4","actor":"human"},
		{"ply":2,"uci":"e7e5","san":"e5","actor":"engine"}]}

func live_state(detail: Dictionary) -> Dictionary:
	var value: Dictionary = detail.frames[-1].duplicate(true)
	value.moves = detail.moves.duplicate(true)
	value.engine = {"name":"History fixture","movetime_ms":1000,"settings_revision":0}
	value.human_to_move = true; value.legal_moves = ["e1d1"]
	value.status = "당신 차례입니다"; value.result = "*"; value.error = null
	value.can_undo = true; value.engine_thinking = false
	value.clock = {"enabled":true,"status":"running","running_color":"white", "label":"10분",
		"control_id":"10+0","white_ms":600000,"black_ms":600000,"turn_elapsed_ms":0,
		"edit_revision":0,"sampled_monotonic_ms":null,"editable":true}
	return value

func _run() -> void:
	app = App.new(); app.offline_mode = true
	root.add_child(app)
	await process_frame
	app.backend_url = "http://history-fixture"
	var detail: Dictionary = fixture()
	var current: Dictionary = live_state(detail)
	app.accept_state(current)
	var original: String = JSON.stringify(app.state)
	app.next_response = {"ok":true,"data":detail}
	app.request_in_flight = true; app.engine_thinking = true
	await app._step_live_history(-1)
	verify(app.live_history_ply == 1 and app.board.state.board == detail.frames[1].board,
		"previous arrow reads the exact past board while engine transport remains busy")
	verify(JSON.stringify(app.state) == original and app.request_in_flight and app.engine_thinking,
		"browsing never changes or cancels actual game/search state")
	verify(not app._can_select_move() and not app.board.state.human_to_move and app.board.state.legal_moves.is_empty(),
		"historical board disables moves and legal input")
	var sent: int = app.captured_requests.size()
	app._on_square_pressed("e4")
	app._on_promotion_pressed("q")
	await app._mutate("/api/move", {"move":"e1d1"})
	verify(app.board_model.selected_square.is_empty() and app.captured_requests.size() == sent,
		"click/promotion/direct mutation cannot submit a move from history")
	var remaining: float = app._clock_remaining(app.state.clock, "white")
	await create_timer(0.04).timeout
	verify(app._clock_remaining(app.state.clock, "white") < remaining and app.state.clock.status == "running",
		"live countdown keeps running while an old position is displayed")
	var newer: Dictionary = current.duplicate(true)
	newer.revision = 3; newer.moves.append({"ply":3,"uci":"e1d1","san":"Kd1","actor":"human"})
	newer.board.erase("e1"); newer.board.d1 = "K"; newer.last_move = "e1d1"
	newer.fen = "4k3/8/8/4p3/4P3/8/8/3K4 b - - 1 2"; newer.turn = "black"
	app.accept_state(newer)
	verify(app.live_history_ply == 1 and app.board.state.board == detail.frames[1].board,
		"a new engine/state move leaves deliberate historical selection unchanged")
	await app._step_live_history(1)
	verify(app.live_history_ply == 2, "forward arrow visits the next cached ply")
	await app._step_live_history(1)
	verify(app.live_history_ply == -1 and app.board.state.board == newer.board,
		"forward at the newest ply returns to the actual current board")
	app.request_in_flight = false; app.engine_thinking = false
	await app._step_live_history(-1)
	var replacement: Dictionary = current.duplicate(true)
	replacement.game_id = "new-game"; replacement.revision = 4
	app.accept_state(replacement)
	verify(app.live_history_ply == -1 and app.live_history_frames.is_empty(),
		"new game clears old browsing and cached frames")
	app.accept_state(current.merged({"revision":5}, true))
	app.next_response = {"ok":true,"data":detail}
	await app._step_live_history(-1)
	var undone: Dictionary = current.duplicate(true)
	undone.revision = 6; undone.moves = undone.moves.slice(0, 1)
	undone.board = detail.frames[1].board.duplicate(true); undone.fen = detail.frames[1].fen
	app.accept_state(undone)
	verify(app.live_history_ply == -1 and app.live_history_frames.is_empty(),
		"undo or branch truncation clears stale historical selection")
	app.accept_state(current.merged({"revision":7}, true))
	app.hold_response = true; app.next_response = {"ok":true,"data":detail}
	app._step_live_history(-1)
	await process_frame
	app.accept_state(replacement.merged({"revision":8}, true))
	app.hold_response = false
	for ignored in range(4): await process_frame
	verify(app.live_history_ply == -1 and app.live_history_frames.is_empty() and not app._live_history_loading,
		"late old-game history response cannot replace the new game")
	app.queue_free(); await process_frame
	print("%d live-history checks; %d failures" % [checks, failures])
	quit(1 if failures else 0)
