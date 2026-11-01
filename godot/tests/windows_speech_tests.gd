extends SceneTree
## Real Windows Desktop helper -> Godot WAV decode, without any audio player.

const DesktopSpeech = preload("res://scripts/windows_speech.gd")
var helper: DesktopSpeech
var inventory: Dictionary = {}
var received: Array[Dictionary] = []
var errors: Array[Dictionary] = []
var failures: int = 0
var checks: int = 0


func _initialize() -> void:
	_run.call_deferred()


func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)


func wait_until(condition: Callable, milliseconds: int = 12000) -> bool:
	var deadline: int = Time.get_ticks_msec() + milliseconds
	while not condition.call() and Time.get_ticks_msec() < deadline:
		await create_timer(0.02).timeout
	return condition.call()


func _run() -> void:
	if OS.get_name() != "Windows":
		print("SKIP Windows Desktop voice integration")
		quit(0)
		return
	var recovery := DesktopSpeech.new()
	root.add_child(recovery)
	recovery._folder = ProjectSettings.globalize_path("user://rook_voice/").path_join("retry_%d_%d" % [OS.get_process_id(), Time.get_ticks_usec()])
	DirAccess.make_dir_recursive_absolute(recovery._folder)
	recovery._helper = recovery._folder.path_join("missing-parent/windows_speech.ps1")
	verify(not recovery._prepare() and not recovery._helper_ready, "failed helper write is not mistaken for ready just because its folder exists")
	recovery._helper = recovery._folder.path_join("windows_speech.ps1")
	verify(recovery._prepare() and recovery._helper_ready, "helper setup can recover on a subsequent retry")
	root.remove_child(recovery)
	recovery.free()
	helper = DesktopSpeech.new()
	root.add_child(helper)
	helper.voices_ready.connect(func(voices: Array, error: String): inventory = {"voices": voices, "error": error})
	helper.audio_ready.connect(func(ticket: int, stream: AudioStreamWAV): received.append({"ticket": ticket, "stream": stream}))
	helper.failed.connect(func(ticket: int, error: String): errors.append({"ticket": ticket, "error": error}))
	helper.query_voices()
	verify(await wait_until(func(): return not inventory.is_empty()), "Desktop voice query completes")
	verify(inventory.get("error", "missing").is_empty(), "Desktop voice discovery has no error")
	var voices: Array = inventory.get("voices", [])
	if voices.is_empty():
		if OS.get_cmdline_user_args().has("--require-english"):
			verify(false, "enabled English Desktop voice is installed")
		else:
			print("SKIP no enabled English Desktop voice installed")
	else:
		var selected: String = str(voices[0].id)
		helper.synthesize(1, "White develops the knight. Check the opponent's strongest reply.", selected)
		verify(await wait_until(func(): return not received.is_empty() or not errors.is_empty()), "Desktop synthesis completes asynchronously")
		verify(errors.is_empty() and received.size() == 1, "Desktop synthesis returns exactly one valid WAV")
		if not received.is_empty():
			var stream: AudioStreamWAV = received[0].stream
			var peak: int = 0
			for index in range(0, stream.data.size() - 1, 2):
				peak = maxi(peak, absi(stream.data.decode_s16(index)))
			verify(stream.get_length() > 1.0 and peak > 500, "Godot decodes non-silent Desktop speech PCM")
			print(JSON.stringify({"voice": selected, "seconds": stream.get_length(), "pcm_peak": peak, "audio_played": false}))
		helper.synthesize(2, "This old explanation must never be delivered.", selected)
		helper.cancel_synthesis()
		helper.synthesize(3, "Black gives check. White must answer the threat first.", selected)
		verify(await wait_until(func(): return received.any(func(item: Dictionary): return item.ticket == 3) or not errors.is_empty()), "latest synthesis completes after cancellation")
		verify(not received.any(func(item: Dictionary): return item.ticket == 2), "cancelled generation never emits a WAV")
		verify(await wait_until(func(): return helper._jobs.is_empty()), "all helper requests are bounded and retired")
		verify(helper._owned_pids.is_empty(), "owned process handles are released")
	var folder: String = helper._folder
	root.remove_child(helper)
	helper.free()
	verify(not DirAccess.dir_exists_absolute(folder), "only this adapter's temporary session is removed")
	print("Windows Desktop voice integration: ", checks, " checks, ", failures, " failures; no audio playback")
	quit(1 if failures else 0)
