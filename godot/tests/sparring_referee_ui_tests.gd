extends SceneTree
## Native presentation fixtures only; no engine strength or correctness claim.

const Main = preload("res://scripts/main.gd")
const EvaluationBar = preload("res://scripts/evaluation_bar.gd")
var checks: int = 0
var failures: int = 0
var app: Variant
var evidence_dir: String = ""


func _initialize() -> void:
	_run.call_deferred()


func verify(ok: bool, message: String) -> void:
	checks += 1
	if not ok:
		failures += 1
		printerr("FAIL: ", message)


func metadata(enabled: bool = true, available: bool = true) -> Dictionary:
	return {"enabled": enabled, "available": available, "name": "Stockfish fixture",
		"policy": "stockfish-sparring-referee-v1", "status": "ready", "message": "UI fixture"}


func position(mode: String = "sparring") -> Dictionary:
	return {"game_id": "referee-ui", "revision": 1, "fen": "8/8/8/8/8/8/4P3/4K2k w - - 0 1",
		"board": {"e2": "P", "e1": "K", "h1": "k"}, "turn": "white", "human_color": "white",
		"human_to_move": true, "engine_to_move": false, "legal_moves": ["e2e3", "e2e4"],
		"moves": [], "last_move": null, "result": "*", "in_check": false, "can_undo": false,
		"status": "UI fixture", "error": null, "engine": {"name": "Regicide", "movetime_ms": 1000,
			"sparring_mode": mode == "sparring", "boss_mode": mode == "boss", "sparring_referee": metadata()}}


func score(state: Dictionary) -> Dictionary:
	return {"game_id": state.game_id, "revision": state.revision, "fen": state.fen, "turn": state.turn,
		"status": "ready", "cp": 350, "mate": null, "depth": 7, "bound": "exact", "white_share": 0.7,
		"source": "search", "reliability": "stable", "sparring_referee": metadata(),
		"reference_evaluation": {"game_id": state.game_id, "revision": state.revision, "fen": state.fen,
			"source": "stockfish_gauge",
			"turn": state.turn, "status": "ready", "cp": -85, "mate": null, "depth": 16,
			"bound": "exact", "white_share": 0.42, "engine_name": "Stockfish fixture"}}


func capture(name: String) -> void:
	if evidence_dir.is_empty() or DisplayServer.get_name() == "headless":
		return
	DirAccess.make_dir_recursive_absolute(evidence_dir)
	await process_frame
	await RenderingServer.frame_post_draw
	verify(root.get_texture().get_image().save_png(evidence_dir.path_join(name + ".png")) == OK,
		"native fixture screenshot saved")


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--evidence-dir":
			evidence_dir = args[index + 1]
	root.size = Vector2i(1240, 860)
	app = Main.new()
	app.offline_mode = true
	root.add_child(app)
	app.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	var state := position()
	app.accept_state(state)
	var own := score(state)
	verify(app.accept_evaluation(own), "matching envelope is accepted")
	verify(app.evaluation_bar.snapshot.cp == -85 and app.evaluation_bar.white_share == 0.42,
		"ready exact reference controls display independently of own score")
	verify(app.evaluation_summary.text.contains("Stockfish 심판") and app.evaluation_summary.text.contains("-0.85"),
		"source and white-perspective reference score are explicit")
	verify(app.evaluation_detail.text.contains("16") and app.evaluation_detail.text.contains("Regicide 자체 +3.50"),
		"reference depth and own score remain visible together")
	verify(app.evaluation.cp == 350 and own.cp == 350, "display choice does not overwrite own search fields")
	verify(app.evaluation_bar.tooltip_text.contains("정답의 보장이 아닙니다"), "external evaluation is not a correctness guarantee")
	var note := Label.new()
	note.text = "화면 검증용 평가입니다. 실제 엔진 계산 결과가 아닙니다."
	note.position = Vector2(24, 80)
	note.add_theme_font_size_override("font_size", 12)
	note.add_theme_color_override("font_color", Color("ffd578"))
	root.add_child(note)
	await capture("native-referee-ready-fixture")
	var invalid_share := score(state)
	invalid_share.reference_evaluation.white_share = NAN
	app.accept_evaluation(invalid_share)
	verify(is_finite(app.evaluation_bar.white_share) and app.evaluation_bar.white_share < 0.5,
		"malformed optional fill falls back to the signed cp visual scale")
	app.accept_evaluation(own)
	var summary: String = app.evaluation_summary.text
	app._on_flip_pressed()
	verify(app.evaluation_summary.text == summary and not app.evaluation_bar.view_from_white,
		"flipping reference gauge never reverses score sign")
	app._on_flip_pressed()
	app.boss_mode_option.select(2)
	app._on_game_mode_selected(2)
	verify(app.boss_mode_option.get_item_text(app.boss_mode_option.get_item_index(2)).contains("Stockfish"),
		"future sparring option identifies installed referee")
	verify(app.game_mode_help.text.contains("수는 Regicide가 선택"), "mode help distinguishes move chooser from referee")
	for changed in [{"game_id": "old"}, {"revision": 0}, {"fen": "other"}, {"turn": "black"},
		{"bound": "lower"}, {"bound": "upper"}, {"bound": ""}, {"depth": 0},
		{"status": "searching"}, {"status": "disabled"}, {"status": "unavailable"}, {"status": "error"},
		{"cp": null, "mate": null}, {"cp": NAN, "mate": null}, {"cp": "0", "mate": null}]:
		var value := score(state)
		value.reference_evaluation.merge(changed, true)
		app.accept_evaluation(value)
		verify(not app.evaluation_bar.has_score and app.evaluation_summary.text.contains("Stockfish"),
			"unusable reference leaves neutral gauge: " + str(changed.keys()))
		verify(not app.evaluation_detail.text.contains("자체 평가 표시"), "native score never fills the Stockfish gauge")
	var missing := score(state)
	missing.sparring_referee.available = false
	app.accept_evaluation(missing)
	verify(not app.evaluation_bar.has_score and app.evaluation_summary.text.contains("사용 불가"),
		"uninstalled referee cannot override even a cached ready value")
	await capture("native-referee-fallback-fixture")
	var disabled := score(state)
	disabled.sparring_referee.enabled = false
	app.accept_evaluation(disabled)
	verify(not app.evaluation_bar.has_score and app.evaluation_summary.text.contains("사용 불가"),
		"disabled Stockfish leaves a neutral gauge")
	for mode in ["normal", "boss"]:
		var other := position(mode)
		other.engine.stockfish_reference = other.engine.sparring_referee
		other.engine.erase("sparring_referee")
		app.accept_state(other)
		var shared := score(other)
		shared.stockfish_reference = shared.sparring_referee
		shared.erase("sparring_referee")
		app.accept_evaluation(shared)
		verify(app.evaluation_bar.snapshot.cp == -85 and app.evaluation_summary.text.contains("Stockfish"),
			"common Stockfish reference applies to " + mode + " game")
	state = position()
	state.engine.erase("sparring_referee")
	app.accept_state(state)
	var legacy := score(state)
	legacy.erase("sparring_referee")
	legacy.erase("reference_evaluation")
	app.accept_evaluation(legacy)
	verify(not app.evaluation_bar.has_score and app.evaluation_summary.text.contains("Stockfish"),
		"old host responses cannot display a native gauge")
	state = position()
	app.accept_state(state)
	for distance in [3, -4, 0]:
		var mate := score(state)
		mate.reference_evaluation.cp = null
		mate.reference_evaluation.mate = distance
		mate.reference_evaluation.winner = "black"
		app.accept_evaluation(mate)
		verify(app.evaluation_bar.has_score and app.evaluation_bar.snapshot.mate == distance,
			"reference mate score remains distinct from missing or zero cp")
	var terminal := position()
	terminal.result = "1-0"
	app.accept_state(terminal)
	app.accept_evaluation(score(terminal))
	verify(app.evaluation_summary.text == "백 승리", "game result has priority over reference search")
	var isolated := EvaluationBar.new()
	root.add_child(isolated)
	isolated.present(score(position()), true)
	verify(isolated.snapshot.cp == 350 and not isolated.summary_text.contains("Stockfish"),
		"shared analysis and coaching gauge component keeps own root fields")
	isolated.free()
	app.free()
	print("Godot sparring-referee UI: %d checks, %d failures" % [checks, failures])
	quit(1 if failures else 0)
