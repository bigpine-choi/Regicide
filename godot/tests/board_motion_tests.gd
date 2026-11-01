extends SceneTree

const Board = preload("res://scripts/chess_board.gd")
const Motion = preload("res://scripts/board_motion.gd")
const AnalysisBoard = preload("res://scripts/analysis_board.gd")
var checks := 0
var failures := 0

func _initialize() -> void:
	_run.call_deferred()

func check(value: bool, message: String) -> void:
	checks += 1
	if not value:
		failures += 1
		printerr("FAIL: " + message)

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	var fixture_path := ""
	for index in range(args.size() - 1):
		if args[index] == "--cases-file":
			fixture_path = args[index + 1]
	var cases: Array = JSON.parse_string(FileAccess.get_file_as_string(fixture_path))
	var board := Board.new()
	root.add_child(board)
	board.size = Vector2(650, 650)
	await process_frame
	for fixture in cases:
		var before: Dictionary = fixture.before
		var after: Dictionary = fixture.after
		var plan := Motion.between(Motion.snapshot(before), Motion.snapshot(after))
		check(not plan.is_empty(), fixture.name + " recognized")
		if plan.is_empty():
			continue
		check(plan.movements.size() == int(fixture.movers), fixture.name + " exact moving pieces")
		check(plan.capture.is_empty() == not bool(fixture.capture), fixture.name + " capture disposition")
		check(plan.movements[0].from == fixture.uci.substr(0, 2) and plan.movements[0].to == fixture.uci.substr(2, 2), fixture.name + " exact endpoints")
		var reverse := Motion.between(Motion.snapshot(after), Motion.snapshot(before))
		check(not reverse.is_empty() and reverse.reverse, fixture.name + " reverse recognized")
		check(reverse.movements[0].piece == plan.movements[0].arrival and reverse.movements[0].arrival == plan.movements[0].piece, fixture.name + " reverse promotion identity")
		board.finish_animation()
		board.state = before
		board.finish_animation()
		board.state = after
		check(board.is_animating(), fixture.name + " starts visual transition")
		check(board.state.board == after.board and board.state.turn == after.turn, fixture.name + " logical state immediately final")
		board._process(0.10)
		var elapsed: float = board._motion_elapsed
		var repeated := after.duplicate(true)
		repeated.status = "new evaluation"
		board.state = repeated
		check(board.is_animating() and board._motion_elapsed == elapsed, fixture.name + " polling does not restart motion")
		check(board.get_board_rect().size == Vector2.ONE * 596, fixture.name + " animation never changes board geometry")
		board._process(1.0)
		check(not board.is_animating() and board.state.board == after.board, fixture.name + " finishes without ghost sprites")
		board.state = before
		check(board.is_animating() and board._motion.reverse, fixture.name + " undo animates backwards")
		board.finish_animation()
	var first: Dictionary = cases[0].before
	var last: Dictionary = cases[0].after
	board.state = first
	board.finish_animation()
	board.state = last
	board.model.flip_view()
	board._process(0.01)
	check(not board.is_animating(), "flip cancels transition without changing position")
	check(board.state.board == last.board, "flip retains authoritative position")
	board.state = first
	board.animation_enabled = false
	check(not board.is_animating(), "preference off cancels immediately")
	board.state = last
	check(not board.is_animating(), "preference off leaves instant final state")
	board.animation_enabled = true
	board.state = first
	board.hide()
	check(not board.is_animating(), "hidden board cannot keep old animation")
	board.show()
	board.state = last
	board.size = Vector2(620, 620)
	await process_frame
	check(not board.is_animating(), "resizing clears pixel transition")
	var new_game := first.duplicate(true)
	new_game.game_id = "different-game"
	check(Motion.between(Motion.snapshot(last), Motion.snapshot(new_game)).is_empty(), "different games cannot animate across each other")
	var jump := last.duplicate(true)
	jump.ply = 10
	check(Motion.between(Motion.snapshot(first), Motion.snapshot(jump)).is_empty(), "jumped ply cannot look like one move")
	var bad := last.duplicate(true)
	bad.board["a3"] = "Q"
	check(Motion.between(Motion.snapshot(first), Motion.snapshot(bad)).is_empty(), "editor-style extra piece does not animate as accepted move")
	var no_move := last.duplicate(true)
	no_move.last_move = null
	check(Motion.between(Motion.snapshot(first), Motion.snapshot(no_move)).is_empty(), "no accepted move cannot animate")
	var rapid: Dictionary = cases[1].after
	board.state = cases[1].before
	board.finish_animation()
	board.state = rapid
	board._process(0.03)
	board.state = cases[1].before
	check(board._motion.reverse and board._motion_elapsed == 0.0, "rapid reversal replaces pending visual, never queues old moves")
	var editor := AnalysisBoard.new()
	root.add_child(editor)
	check(editor._motion_allowed(), "analysis board supports playback without an editor host")
	var host := EditorHost.new()
	editor.editor_host = host
	editor.state = first
	editor.state = last
	check(not editor.is_animating(), "free placement stays immediate even for a move-shaped edit")
	host.editing = false
	editor.state = first
	check(editor.is_animating(), "analysis play shares move animation")
	host.editing = true
	editor._process(0.01)
	check(not editor.is_animating(), "returning to editor cancels playback")
	editor.free()
	board.free()
	print("Board animation: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)

class EditorHost extends RefCounted:
	var editing := true
