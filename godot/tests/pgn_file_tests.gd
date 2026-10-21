extends SceneTree
## Real FileDialog button/signals and disk IO; synthetic mode replaces only HTTP.

const CaptureApp = preload("res://tests/think_time_capture_scene.gd")
const SAMPLE_PGN = "[Event \"PGN file fixture\"]\n[White \"Human\"]\n[Black \"Regicide\"]\n[Result \"*\"]\n\n1. e4 e5 *\n"
var app: Variant
var checks: int = 0
var failures: int = 0
var work_dir: String = ""
var live_mode: bool = false
var expected: Dictionary = {}


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func wait_for(predicate: Callable, message: String) -> bool:
	var deadline: int = Time.get_ticks_msec() + 30000
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			verify(false, "timeout: " + message + "; " + str(app.error_message))
			return false
		await create_timer(0.01).timeout
	verify(true, message)
	return true


func fixture(revision: int = 1) -> Dictionary:
	return {"game_id": "pgn-file-fixture", "revision": revision,
		"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
		"board": {"e1": "K", "e8": "k", "e2": "P", "e7": "p"},
		"turn": "white", "human_color": "white", "human_to_move": true, "engine_to_move": false,
		"legal_moves": ["e2e4", "e2e3"], "moves": [], "last_move": null,
		"in_check": false, "result": "*", "can_undo": false, "status": "대국 중",
		"termination": null, "error": null, "engine": {"name": "PGN fixture", "movetime_ms": 1000}}


func preview_fixture() -> Dictionary:
	var first := fixture()
	first.human_to_move = false
	first.legal_moves = []
	var second := first.duplicate(true)
	second.board.erase("e2")
	second.board.e4 = "P"
	second.turn = "black"
	second.last_move = "e2e4"
	var third := second.duplicate(true)
	third.board.erase("e7")
	third.board.e5 = "p"
	third.turn = "white"
	third.last_move = "e7e5"
	return {"summary": {"game_id": "pgn-file-preview", "human_color": "white", "result": "*",
		"engine_name": "Imported fixture", "movetime_ms": null, "status": "PGN 파일"},
		"moves": [{"ply": 1, "uci": "e2e4", "san": "e4", "forced": false},
			{"ply": 2, "uci": "e7e5", "san": "e5", "forced": false}],
		"frames": [first, second, third], "pgn": SAMPLE_PGN}


func find_file_row(node: Node, filename: String) -> Dictionary:
	if node is ItemList:
		var items: ItemList = node
		for index in range(items.item_count):
			if items.get_item_text(index) == filename:
				return {"list": items, "index": index}
	for child in node.get_children(true):
		var found := find_file_row(child, filename)
		if not found.is_empty():
			return found
	return {}


func history_cells() -> Array[String]:
	var cell_pattern := RegEx.new()
	cell_pattern.compile("\\[cell\\](.*?)\\[/cell\\]")
	var tags := RegEx.new()
	tags.compile("\\[[^]]+\\]")
	var cells: Array[String] = []
	for found in cell_pattern.search_all(app.move_history.text):
		cells.append(tags.sub(found.get_string(1), "", true).strip_edges())
	return cells


func press_dialog_ok(dialog: FileDialog, path: String) -> bool:
	if dialog.current_dir.replace("\\", "/").simplify_path() != path.get_base_dir().replace("\\", "/").simplify_path():
		dialog.current_dir = path.get_base_dir()
	await process_frame
	await process_frame
	if dialog.file_mode == FileDialog.FILE_MODE_OPEN_FILE:
		var row := find_file_row(dialog, path.get_file())
		verify(not row.is_empty(), "existing file is offered in the chooser: " + path.get_file())
		if row.is_empty():
			return false
		# Simulate a single list selection, not a disabled-button or double-click bypass.
		var items: ItemList = row.list
		items.select(int(row.index))
		items.item_selected.emit(int(row.index))
	else:
		var filename: LineEdit = dialog.get_line_edit()
		filename.text = path.get_file()
		filename.text_changed.emit(filename.text)
	await process_frame
	var enabled: bool = not dialog.get_ok_button().disabled
	verify(enabled, "file chooser confirmation is enabled for the selected path: " + dialog.current_file)
	if not enabled:
		return false
	dialog.get_ok_button().pressed.emit()
	await process_frame
	return true


func cancel_dialog(dialog: FileDialog) -> void:
	dialog.get_cancel_button().pressed.emit()
	await process_frame


func check_modal_guards() -> void:
	var before: String = JSON.stringify(app.state)
	var selected: String = app.board_model.selected_square
	var view: bool = app.board_model.view_from_white
	app._on_save_pressed()
	app._on_open_pgn_pressed()
	app._on_square_pressed("e2")
	app._on_drag_requested("e2", "e4")
	app._on_new_game_pressed()
	app._on_undo_pressed()
	var focused: Control = app.get_viewport().gui_get_focus_owner()
	if focused != null:
		focused.release_focus()
	var key := InputEventKey.new()
	key.keycode = KEY_F
	key.pressed = true
	app._unhandled_key_input(key)
	await process_frame
	verify(JSON.stringify(app.state) == before and app.board_model.selected_square == selected,
		"modal file chooser prevents board selection, drag and game mutations")
	verify(app.board_model.view_from_white == view, "F shortcut does not flip the board while a file chooser is open")
	verify(app.save_button.disabled and app.open_pgn_button.disabled, "both file commands are disabled during either chooser")


func synthetic() -> void:
	app = CaptureApp.new()
	app.offline_mode = true
	root.add_child(app)
	app.backend_url = "http://127.0.0.1:1"
	app.accept_state(fixture())
	app.save_dialog.current_dir = work_dir
	app.open_pgn_dialog.current_dir = work_dir
	await process_frame
	verify(not app.save_dialog.use_native_dialog and not app.open_pgn_dialog.use_native_dialog,
		"save and open use testable in-game file choosers")
	verify(app.save_dialog.exclusive and app.open_pgn_dialog.exclusive, "both file choosers are modal")
	app.next_response = {"ok": true, "pgn": SAMPLE_PGN}
	app.hold_response = true
	app._on_save_pressed()
	await process_frame
	app._on_save_pressed()
	app._on_open_pgn_pressed()
	await process_frame
	verify(app.captured_requests.size() == 1 and app.request_in_flight, "pending PGN fetch cannot open or issue a second export/import")
	app.hold_response = false
	if not await wait_for(func(): return app.save_dialog.visible, "save chooser opens after PGN fetch"):
		return
	verify(app._file_dialog_busy() and app._save_dialog_open, "save guard remains set after the fetch finishes")
	await check_modal_guards()
	verify(app.captured_requests.size() == 1 and not app.open_pgn_dialog.visible, "open save dialog blocks duplicate export and competing import")
	await cancel_dialog(app.save_dialog)
	verify(not app._file_dialog_busy() and not app.save_dialog.visible and app._pending_pgn.is_empty(),
		"actual cancel button clears save visibility, pending text and busy guard")
	app._on_save_pressed()
	if not await wait_for(func(): return app.save_dialog.visible, "save can be retried after cancel"):
		return
	app.accept_state(fixture(2))
	app.review_pgn = "unrelated later review text"
	app._render()
	verify(app._pending_pgn == SAMPLE_PGN, "PGN snapshot stays frozen across state and presentation updates")
	var saved: String = work_dir.path_join("synthetic-selected.pgn")
	if not await press_dialog_ok(app.save_dialog, saved):
		return
	verify(FileAccess.file_exists(saved) and FileAccess.get_file_as_string(saved) == SAMPLE_PGN,
		"FileDialog OK emits file_selected and writes the exact selected PGN snapshot")
	verify(not app._file_dialog_busy() and not app.save_dialog.visible and app.error_message.is_empty(), "successful save releases its modal guard")
	verify(app._notice_message.contains("synthetic-selected.pgn"), "successful save names the actual output file")
	app._on_save_pressed()
	if not await wait_for(func(): return app.save_dialog.visible, "another export can open after successful save"):
		return
	# Simulate a selected directory disappearing before the write; the error is real disk IO.
	app.save_dialog.hide()
	app.save_dialog.file_selected.emit(work_dir.path_join("missing-directory/failed.pgn"))
	await process_frame
	verify(not app.error_message.is_empty() and not FileAccess.file_exists(work_dir.path_join("missing-directory/failed.pgn")),
		"filesystem write failure is surfaced and creates no successful output")
	verify(not app._file_dialog_busy() and not app._notice_message.contains("기보를 저장"), "failed save releases the guard and clears any stale success notice")
	app._on_save_pressed()
	if not await wait_for(func(): return app.save_dialog.visible, "save failure permits immediate retry"):
		return
	if not await press_dialog_ok(app.save_dialog, work_dir.path_join("retry-no-extension")):
		return
	verify(FileAccess.get_file_as_string(work_dir.path_join("retry-no-extension.pgn")) == SAMPLE_PGN,
		"successful retry adds the PGN extension when omitted")
	var baseline: int = app.captured_requests.size()
	app._on_open_pgn_pressed()
	await process_frame
	verify(app.open_pgn_dialog.visible and app._open_pgn_dialog_open, "open command presents a guarded file chooser")
	await check_modal_guards()
	verify(app.captured_requests.size() == baseline and not app.save_dialog.visible, "open chooser blocks competing save and duplicate reads")
	await cancel_dialog(app.open_pgn_dialog)
	verify(not app._file_dialog_busy() and not app.open_pgn_dialog.visible, "actual cancel button clears the open guard")
	app._on_open_pgn_pressed()
	await process_frame
	app.open_pgn_dialog.hide()
	app.open_pgn_dialog.file_selected.emit(work_dir.path_join("missing-source.pgn"))
	await process_frame
	verify(not app.error_message.is_empty() and not app._file_dialog_busy() and app.captured_requests.size() == baseline,
		"missing source file produces a local error without an HTTP preview or stuck guard")
	app.next_response = {"ok": false, "error": "Synthetic invalid PGN rejected"}
	app._on_open_pgn_pressed()
	await process_frame
	if not await press_dialog_ok(app.open_pgn_dialog, saved):
		return
	if not await wait_for(func(): return not app.request_in_flight, "failed preview request finishes"):
		return
	verify(not app.review_mode and app.error_message.contains("invalid PGN") and not app._file_dialog_busy(),
		"parser error preserves the live view and permits another open attempt")
	var live_before: String = JSON.stringify(app.state)
	app.next_response = {"ok": true, "data": preview_fixture()}
	app._on_open_pgn_pressed()
	await process_frame
	if not await press_dialog_ok(app.open_pgn_dialog, saved):
		return
	if not await wait_for(func(): return app.review_mode and not app.request_in_flight, "chosen file becomes read-only replay"):
		return
	var request: Dictionary = app.captured_requests.back()
	verify(request.path == "/api/pgn-preview" and request.payload.pgn == SAMPLE_PGN, "open reads actual selected bytes into the read-only preview request")
	verify(app.review_moves.size() == 2 and app.review_frames.size() == 3, "preview exposes every move and board frame")
	app._last_review_position()
	verify(app._display_state().board.has("e4") and app._display_state().board.has("e5"), "imported replay reaches the parsed final board")
	verify(JSON.stringify(app.state) == live_before and FileAccess.get_file_as_string(saved) == SAMPLE_PGN,
		"import and replay leave the live state and selected source file unchanged")
	app._exit_review()
	verify(not app.review_mode and JSON.stringify(app.state) == live_before, "leaving imported replay restores the live game")


func live() -> void:
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "isolated real HTTP game loads"):
		return
	app.save_dialog.current_dir = work_dir
	app.open_pgn_dialog.current_dir = work_dir
	var original: String = JSON.stringify(app.state)
	verify(not app.save_button.disabled, "actual live PGN save command is enabled")
	app.save_button.pressed.emit()
	if not await wait_for(func(): return app.save_dialog.visible, "real PGN response opens the save chooser"):
		return
	await check_modal_guards()
	var saved: String = work_dir.path_join("live-selected.pgn")
	if not await press_dialog_ok(app.save_dialog, saved):
		return
	verify(FileAccess.file_exists(saved) and FileAccess.get_file_as_string(saved) == str(expected.pgn),
		"real live PGN is saved through the FileDialog confirmation path")
	verify(not app._file_dialog_busy() and app.error_message.is_empty(), "real file save succeeds and leaves no modal state")
	var source_digest: String = FileAccess.get_sha256(saved)
	verify(not app.open_pgn_button.disabled, "actual file open command is enabled after save")
	app.open_pgn_button.pressed.emit()
	await process_frame
	await process_frame
	verify(app.open_pgn_dialog.visible and app._file_dialog_busy(), "real import command opens the in-game chooser")
	print("Open chooser initial filename=", JSON.stringify(app.open_pgn_dialog.current_file),
		", confirmation_disabled=", app.open_pgn_dialog.get_ok_button().disabled)
	verify(app.open_pgn_dialog.current_file.is_empty() or (FileAccess.file_exists(app.open_pgn_dialog.current_path)
		and not app.open_pgn_dialog.get_ok_button().disabled),
		"initial open state is empty or offers an existing file with an enabled confirmation")
	if not await press_dialog_ok(app.open_pgn_dialog, saved):
		return
	if not await wait_for(func(): return app.review_mode and not app.request_in_flight, "actual exported file parses through authenticated HTTP into replay"):
		return
	var actual_moves: Array = []
	for move in app.review_moves:
		actual_moves.append(str(move.uci))
	verify(actual_moves == expected.uci_moves, "file replay has exactly the independently prepared UCI sequence")
	verify(app.review_frames.size() == expected.uci_moves.size() + 1, "file replay contains initial and every resulting board")
	app._last_review_position()
	verify(app._display_state().board == expected.final_board, "file replay final board agrees with independent python-chess replay")
	verify(app.review_moves.any(func(move): return move.get("forced", false)), "PGN handicap annotation survives file reopen")
	verify(JSON.stringify(app.state) == original and not app._can_select_move(), "imported replay preserves the live game and disables playing moves")
	verify(FileAccess.get_sha256(saved) == source_digest, "file open and replay never rewrite the selected PGN")
	app.save_button.pressed.emit()
	await process_frame
	verify(app.save_dialog.visible and app._pending_pgn == app.review_pgn, "saving imported replay uses that file's parsed game")
	if not await press_dialog_ok(app.save_dialog, work_dir.path_join("roundtrip-reexport.pgn")):
		return
	verify(FileAccess.file_exists(work_dir.path_join("roundtrip-reexport.pgn")) and app.error_message.is_empty(),
		"imported game can be exported again through the same chooser path")
	app._exit_review()
	verify(not app.review_mode and JSON.stringify(app.state) == original, "file replay exit returns to the exact original live game")
	var bom_source: String = work_dir.path_join("bom-crlf-source.pgn")
	var bom_contents: PackedByteArray = FileAccess.get_file_as_bytes(bom_source)
	verify(bom_contents.size() > 3 and bom_contents[0] == 239 and bom_contents[1] == 187 and bom_contents[2] == 191,
		"independent BOM and CRLF source exists as actual UTF-8 bytes")
	app.open_pgn_button.pressed.emit()
	await process_frame
	if not await press_dialog_ok(app.open_pgn_dialog, bom_source):
		return
	if not await wait_for(func(): return app.review_mode and not app.request_in_flight,
		"BOM and CRLF file opens through the actual Godot decoder and HTTP preview"):
		return
	verify(app.review_pgn.to_utf8_buffer() == bom_contents, "import preserves the original BOM, CRLF and complete PGN text")
	verify(app.review_moves.size() == expected.uci_moves.size(), "BOM source still parses the complete move history")
	app.save_button.pressed.emit()
	await process_frame
	var bom_reexport: String = work_dir.path_join("bom-crlf-reexport.pgn")
	if not await press_dialog_ok(app.save_dialog, bom_reexport):
		return
	verify(FileAccess.get_file_as_bytes(bom_reexport) == bom_contents, "BOM and CRLF survive read-only import and UI reexport byte for byte")
	verify(FileAccess.get_file_as_bytes(bom_source) == bom_contents, "BOM file preview does not mutate the original selected file")
	app._exit_review()
	verify(not app.review_mode and JSON.stringify(app.state) == original, "BOM replay also restores the untouched live game")
	var black_root: String = work_dir.path_join("black-root.pgn")
	var black_root_digest: String = FileAccess.get_sha256(black_root)
	app.open_pgn_button.pressed.emit()
	await process_frame
	if not await press_dialog_ok(app.open_pgn_dialog, black_root):
		return
	if not await wait_for(func(): return app.review_mode and not app.request_in_flight, "custom black-to-move FEN file opens as replay"):
		return
	app._last_review_position()
	verify(app.review_moves.size() == 1 and app.review_moves[0].uci == "e4d3"
		and app._display_state().board == expected.black_root_final_board,
		"custom FEN replay performs the independently validated en passant move")
	verify(history_cells() == ["2.", "", "exd3"],
		"black-to-move fullmove 2 is displayed in row 2 with an empty white cell and the black SAN cell")
	verify(FileAccess.get_sha256(black_root) == black_root_digest, "custom-root import preserves its source PGN")
	app._exit_review()
	verify(not app.review_mode and JSON.stringify(app.state) == original, "custom-root replay returns to the untouched live game")
	var summary := {"source_sha256_before": source_digest, "source_sha256_after": FileAccess.get_sha256(saved),
		"uci_moves": actual_moves, "live_state_unchanged": JSON.stringify(app.state) == original,
		"source_unchanged": FileAccess.get_sha256(saved) == source_digest,
		"bom_crlf_source_sha256": FileAccess.get_sha256(bom_source),
		"bom_crlf_reexport_sha256": FileAccess.get_sha256(bom_reexport),
		"black_root_source_sha256": black_root_digest, "manual_play": false}
	var summary_file := FileAccess.open(work_dir.path_join("scene-roundtrip.json"), FileAccess.WRITE)
	summary_file.store_string(JSON.stringify(summary, "  ") + "\n")
	summary_file.close()


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--work-dir":
			work_dir = args[index + 1]
		if args[index] == "--expected-file":
			expected = JSON.parse_string(FileAccess.get_file_as_string(args[index + 1]))
		if args[index] == "--backend-file":
			live_mode = true
	root.size = Vector2i(1280, 900)
	if live_mode:
		await live()
	else:
		await synthetic()
	if app != null:
		app.queue_free()
	await process_frame
	print("Godot PGN file %s checks: %d, failures: %d" % ["live" if live_mode else "synthetic", checks, failures])
	quit(0 if failures == 0 else 1)
