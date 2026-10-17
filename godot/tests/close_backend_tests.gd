extends SceneTree
## Real HTTP close ordering, including a move whose response has not arrived.

var app: Variant
var mode: String = "ready"


func _initialize() -> void:
	_run.call_deferred()


func wait_for(predicate: Callable, message: String) -> bool:
	var deadline: int = Time.get_ticks_msec() + 12000
	while not predicate.call():
		if Time.get_ticks_msec() > deadline:
			printerr("FAIL: close test timeout: ", message)
			quit(1)
			return false
		await create_timer(0.01).timeout
	return true


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size()):
		if args[index] == "--close-case" and index + 1 < args.size():
			mode = args[index + 1]
	app = load("res://main.tscn").instantiate()
	root.add_child(app)
	if not await wait_for(func(): return not app.state.is_empty() and not app.request_in_flight, "initial state"):
		return
	if not app.own_backend:
		printerr("FAIL: close test must own its isolated backend")
		quit(1)
		return
	if mode != "ready":
		app._on_square_pressed("e2")
		app._on_square_pressed("e4")
		if mode == "busy":
			if not await wait_for(func(): return app.engine_thinking, "real 1000 ms engine search"):
				return
		else:
			if not await wait_for(func(): return app.request_in_flight and not app.engine_thinking, "pending move response"):
				return
			# Allow the HTTP worker to receive the request before closing. The test
			# server holds this route for 0.3 s before taking the game-state lock.
			await create_timer(0.05).timeout
	await app._close_app()
	print("Godot own-backend close case passed: ", mode)
