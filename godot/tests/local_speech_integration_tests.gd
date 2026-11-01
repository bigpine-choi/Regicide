extends SceneTree
## Real configured local worker -> WAV decode. No AudioStreamPlayer is created.

const LocalAdapter = preload("res://scripts/local_speech.gd")

class RecordedNeural extends "res://scripts/neural_speech.gd":
	var receipts: Array[Dictionary] = []
	func _accept_result(job: Dictionary) -> void:
		var result: Variant = JSON.parse_string(FileAccess.get_file_as_string(str(job.folder).path_join("response.json")))
		receipts.append({"launch_pid": job.pid, "runtime_pid": result.get("runtime_pid", -1) if result is Dictionary else -1, "action": job.action})
		super._accept_result(job)

class RecordedLocal extends "res://scripts/local_speech.gd":
	func _make_neural_adapter() -> DesktopAdapter:
		return RecordedNeural.new()

var adapter: RecordedLocal
var inventory: Dictionary = {}
var outputs: Array[Dictionary] = []
var errors: Array[Dictionary] = []
var statuses: Array[Dictionary] = []
var checks: int = 0
var failures: int = 0

func _initialize() -> void:
	_run.call_deferred()

func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)

func wait_until(condition: Callable, milliseconds: int = 25000) -> bool:
	var deadline: int = Time.get_ticks_msec() + milliseconds
	while not condition.call() and Time.get_ticks_msec() < deadline:
		await create_timer(0.02).timeout
	return condition.call()

func _run() -> void:
	if OS.get_name() != "Windows":
		print("SKIP local Windows voice integration")
		quit(0)
		return
	adapter = RecordedLocal.new()
	root.add_child(adapter)
	adapter.voices_ready.connect(func(voices: Array, error: String): inventory = {"voices": voices, "error": error})
	adapter.audio_ready.connect(func(ticket: int, stream: AudioStreamWAV): outputs.append({"ticket": ticket, "stream": stream}))
	adapter.failed.connect(func(ticket: int, error: String): errors.append({"ticket": ticket, "error": error}))
	adapter.status_changed.connect(func(provider: String, message: String): statuses.append({"provider": provider, "message": message}))
	adapter.query_voices()
	verify(await wait_until(func(): return not inventory.is_empty()), "local voice discovery completes")
	var voices: Array = inventory.get("voices", [])
	verify(inventory.get("error", "missing").is_empty() and not voices.is_empty(), "configured voice is available")
	var require_neural: bool = OS.get_cmdline_user_args().has("--require-neural")
	if not voices.is_empty():
		var selected: String = str(voices[0].id)
		verify(not require_neural or selected.begins_with("kokoro:"), "required neural provider is selected ahead of Windows SAPI")
		var sentence: String = "Your knight protects the center. Before you move again, check what your opponent is threatening."
		var started: int = Time.get_ticks_msec()
		adapter.synthesize(1, sentence, selected)
		verify(await wait_until(func(): return not outputs.is_empty() or not errors.is_empty()), "real local neural synthesis completes")
		var first_ms: int = Time.get_ticks_msec() - started
		verify(errors.is_empty() and outputs.size() == 1, "real worker delivers one WAV")
		if not outputs.is_empty():
			var stream: AudioStreamWAV = outputs[0].stream
			var peak: int = 0
			for index in range(0, stream.data.size() - 1, 2):
				peak = maxi(peak, absi(stream.data.decode_s16(index)))
			verify(stream.mix_rate == 24000 and stream.get_length() > 1 and peak > 500, "Godot decodes non-silent 24kHz neural speech PCM")
			verify(not require_neural or statuses[-1].provider == "kokoro", "real synthesis did not silently fall back to SAPI")
			adapter.synthesize(2, "This cancelled explanation must not reach the board.", selected)
			var cancelled_pid: int = int(adapter._neural._jobs[-1].pid)
			await create_timer(0.35).timeout
			adapter.cancel_synthesis()
			started = Time.get_ticks_msec()
			adapter.synthesize(3, sentence, selected)
			verify(await wait_until(func(): return outputs.size() > 1 or not errors.is_empty()), "repeat utterance returns through the same worker protocol")
			var repeat_ms: int = Time.get_ticks_msec() - started
			verify(outputs.size() == 2 and outputs[-1].ticket == 3 and errors.is_empty(), "cancelled generation cannot interrupt the newest cached utterance")
			verify(await wait_until(func(): return adapter._neural._jobs.is_empty() and adapter._desktop._jobs.is_empty()), "all local jobs finish or retire within their deadlines")
			verify(adapter._neural._owned_pids.is_empty() and adapter._desktop._owned_pids.is_empty(), "only owned helper process handles are released")
			verify(not OS.is_process_running(cancelled_pid), "cancelled inference process itself is no longer running")
			var receipts: Array = adapter._neural.receipts
			verify(receipts.size() == 3 and receipts.all(func(item: Dictionary): return item.launch_pid == item.runtime_pid), "Godot directly owns each real Python inference PID, with no venv redirector")
			print(JSON.stringify({"voice": selected, "provider": statuses[-1].provider, "first_ms": first_ms, "repeat_ms": repeat_ms,
				"wav_seconds": stream.get_length(), "pcm_peak": peak, "audio_played": false,
				"process_receipts": receipts, "cancelled_pid": cancelled_pid}))
	var neural_folder: String = adapter._neural._folder
	root.remove_child(adapter)
	adapter.free()
	verify(neural_folder.is_empty() or not DirAccess.dir_exists_absolute(neural_folder), "private neural request folder is removed")
	print("Local neural voice integration: ", checks, " checks, ", failures, " failures; no audio playback")
	quit(1 if failures else 0)
