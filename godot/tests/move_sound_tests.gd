extends SceneTree
## Checks actual player requests and cancellation, without assuming speakers.

const Board = preload("res://scripts/chess_board.gd")
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
	check(board._move_stream.get_length() > 0.05 and board._move_stream.get_length() < 0.3, "short move audio loaded")
	check(board._capture_stream.get_length() > 0.05 and board._capture_stream.get_length() < 0.3, "short capture audio loaded")
	check(board._move_stream != board._capture_stream, "capture has a distinct sound")
	check(board._move_sound.volume_db == -8.0, "effects have restrained independent volume")
	for fixture in cases:
		board.finish_animation()
		board.state = fixture.before
		board.finish_animation()
		board._move_sound.stop()
		board.state = fixture.after
		board._process(0.10)
		check(not board._move_sound.playing, fixture.name + " waits for landing")
		board._process(0.20)
		check(board._move_sound.playing, fixture.name + " plays on landing")
		check(board._move_sound.stream == (board._capture_stream if fixture.capture else board._move_stream), fixture.name + " correct effect")
		check(board._pending_move_sound.is_empty(), fixture.name + " one request per move including castling")
		board._move_sound.stop()
		var repeated: Dictionary = fixture.after.duplicate(true)
		repeated.status = "new evaluation"
		board.state = repeated
		board._process(1.0)
		check(not board._move_sound.playing, fixture.name + " polling and later frames cannot replay")
		board.state = fixture.before
		board._process(1.0)
		check(not board._move_sound.playing, fixture.name + " undo stays silent")
	var first: Dictionary = cases[0].before
	var last: Dictionary = cases[0].after
	board.state = first
	board.finish_animation()
	board._move_sound.stop()
	check(not board._move_sound.playing, "initial position is silent")
	board.state = last
	board._process(0.05)
	board.state = first
	board._process(1.0)
	check(not board._move_sound.playing, "rapid undo cancels pending sound")
	board.state = last
	board.model.flip_view()
	board._process(1.0)
	check(not board._move_sound.playing, "flip cancels pending sound")
	board.state = first
	board.finish_animation()
	board.state = last
	board.hide()
	board._process(1.0)
	check(not board._move_sound.playing, "hidden board cancels pending sound")
	board.show()
	board.state = first
	board.finish_animation()
	board.state = last
	board._on_board_resized()
	board._process(1.0)
	check(not board._move_sound.playing, "resize cancels pending sound")
	board.state = first
	board.finish_animation()
	var jump := last.duplicate(true)
	jump.ply = 8
	board.state = jump
	board._process(1.0)
	check(not board._move_sound.playing, "jumping into review is silent")
	var new_game := first.duplicate(true)
	new_game.game_id = "new-game"
	board.state = new_game
	board._process(1.0)
	check(not board._move_sound.playing, "loading a new game is silent")
	board.state = first
	board.finish_animation()
	board.animation_enabled = false
	board.state = last
	check(board._move_sound.playing, "instant forward moves still have a sound")
	board._move_sound.stop()
	board.animation_enabled = true
	board.state = first
	board.finish_animation()
	board.state = last
	board.move_sounds_enabled = false
	board._process(1.0)
	check(not board._move_sound.playing, "disabling effects cancels the pending sound")
	board.move_sounds_enabled = true
	board.state = first
	board.finish_animation()
	board.state = last
	board._process(0.03)
	var reply := last.duplicate(true)
	reply.board.erase("e7")
	reply.board["e5"] = "p"
	reply.turn = "white"
	reply.last_move = "e7e5"
	reply.ply = 2
	board.state = reply
	check(board._move_sound.playing, "fast reply settles the previous forward move with a tap")
	check(board._pending_move_sound == "move", "fast reply keeps its own landing tap")
	board._move_sound.stop()
	board._process(0.30)
	check(board._move_sound.playing, "fast reply plays once at its own landing")
	board._move_sound.stop()
	var editor := AnalysisBoard.new()
	root.add_child(editor)
	var host := EditorHost.new()
	editor.editor_host = host
	editor.state = first
	editor.state = last
	editor._process(1.0)
	check(not editor._move_sound.playing, "move-shaped free placement is silent")
	host.editing = false
	editor.state = first
	editor.finish_animation()
	editor.state = last
	editor._process(0.30)
	check(editor._move_sound.playing, "analysis forward play shares landing effects")
	editor._move_sound.stop()
	editor.state = first
	editor.finish_animation()
	editor.state = last
	host.editing = true
	editor._process(1.0)
	check(not editor._move_sound.playing, "returning to free placement cancels pending effects")
	editor.free()
	board.free()
	# These synthetic cases play/stop many sounds in one frame; let the audio
	# mixer release its pending playback references before the process exits.
	await create_timer(0.25).timeout
	print("Move sound: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)

class EditorHost extends RefCounted:
	var editing := true
