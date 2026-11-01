extends SceneTree
## Provider routing and cancellation only: no synthesis process or audio player.

const NeuralAdapter = preload("res://scripts/neural_speech.gd")

class MockProvider extends "res://scripts/windows_speech.gd":
	var queries: int = 0
	var cancellations: int = 0
	var requests: Array[Dictionary] = []
	func query_voices() -> void:
		queries += 1
	func synthesize(ticket: int, text: String, voice: String) -> void:
		requests.append({"ticket": ticket, "text": text, "voice": voice})
	func cancel_synthesis() -> void:
		cancellations += 1

class LocalAdapter extends "res://scripts/local_speech.gd":
	func _make_neural_adapter() -> DesktopAdapter:
		return MockProvider.new()
	func _make_desktop_adapter() -> DesktopAdapter:
		return MockProvider.new()

var checks: int = 0
var failures: int = 0
var inventories: Array[Dictionary] = []
var audio_tickets: Array[int] = []
var errors: Array[Dictionary] = []
var notices: Array[Dictionary] = []

func _initialize() -> void:
	_run.call_deferred()

func verify(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		printerr("FAIL: ", message)

func attach() -> LocalAdapter:
	var adapter := LocalAdapter.new()
	root.add_child(adapter)
	adapter.voices_ready.connect(func(voices: Array, error: String): inventories.append({"voices": voices, "error": error}))
	adapter.audio_ready.connect(func(ticket: int, _stream: AudioStreamWAV): audio_tickets.append(ticket))
	adapter.failed.connect(func(ticket: int, error: String): errors.append({"ticket": ticket, "error": error}))
	adapter.status_changed.connect(func(provider: String, message: String): notices.append({"provider": provider, "message": message}))
	return adapter

func release(adapter: LocalAdapter) -> void:
	root.remove_child(adapter)
	adapter.free()

func _run() -> void:
	var neural_voices: Array = [{"id": "kokoro:am_michael", "language": "en-US", "provider": "kokoro"}]
	var desktop_voices: Array = [{"id": "Microsoft Zira Desktop", "language": "en-US"}]
	var wav := AudioStreamWAV.new()
	wav.data = PackedByteArray([1, 0, 2, 0, 3, 0, 4, 0])
	var adapter := attach()
	adapter.query_voices()
	adapter.query_voices()
	var neural: MockProvider = adapter._neural
	var desktop: MockProvider = adapter._desktop
	verify(neural.queries == 1 and desktop.queries == 0, "prefer neural and coalesce discovery requests")
	neural.voices_ready.emit(neural_voices, "")
	verify(inventories.size() == 1 and inventories[-1].voices[0].id == "kokoro:am_michael", "local neural inventory preserves provider voice id")
	verify(notices[-1].provider == "kokoro", "neural provider is visible")
	adapter.synthesize(1, "White develops a knight.", "kokoro:am_michael")
	verify(neural.requests[-1].ticket == 1 and desktop.requests.is_empty(), "neural synthesis receives original ticket and no duplicate SAPI job")
	neural.audio_ready.emit(1, wav)
	verify(audio_tickets == [1], "neural WAV delivered once")
	neural.audio_ready.emit(1, wav)
	verify(audio_tickets == [1], "duplicate completed WAV ignored")
	adapter.synthesize(2, "This old request must be discarded.", "kokoro:am_michael")
	adapter.cancel_synthesis()
	neural.failed.emit(2, "cancelled old process")
	neural.audio_ready.emit(2, wav)
	verify(desktop.queries == 0 and errors.is_empty() and audio_tickets == [1], "cancelled neural failures never trigger fallback or output")
	adapter.synthesize(3, "Black can capture the bishop.", "kokoro:am_michael")
	neural.failed.emit(3, "neural runtime failed")
	verify(desktop.queries == 1 and errors.is_empty() and notices[-1].provider == "sapi", "neural failure starts one visible SAPI fallback without final error")
	adapter.synthesize(4, "White should check the threat.", "kokoro:am_michael")
	verify(desktop.queries == 1, "newest request shares in-flight SAPI discovery")
	desktop.voices_ready.emit(desktop_voices, "")
	verify(desktop.requests.size() == 1 and desktop.requests[0] == {"ticket": 4, "text": "White should check the threat.", "voice": "Microsoft Zira Desktop"}, "fallback uses latest text and the real Desktop id")
	neural.audio_ready.emit(3, wav)
	neural.failed.emit(3, "late error")
	desktop.audio_ready.emit(4, wav)
	verify(audio_tickets == [1, 4] and errors.is_empty(), "only newest fallback WAV is emitted")
	adapter.synthesize(5, "Another local explanation.", "kokoro:am_michael")
	verify(desktop.requests[-1].ticket == 5 and neural.requests.size() == 3, "fallback stays stable on subsequent moves without retry storms")
	desktop.failed.emit(5, "Both local providers unavailable")
	desktop.failed.emit(5, "duplicate")
	verify(errors.size() == 1 and errors[0].ticket == 5, "final Desktop failure is surfaced once")
	release(adapter)

	adapter = attach()
	adapter.query_voices()
	neural = adapter._neural
	desktop = adapter._desktop
	neural.voices_ready.emit([], "configuration absent")
	verify(desktop.queries == 1, "missing config automatically discovers SAPI")
	desktop.voices_ready.emit(desktop_voices, "")
	verify(inventories[-1].error.is_empty() and inventories[-1].voices[0].id == "Microsoft Zira Desktop", "SAPI fallback is a usable normal voice inventory")
	release(adapter)

	adapter = attach()
	adapter.query_voices()
	adapter._neural.voices_ready.emit([], "no neural voice")
	adapter._desktop.voices_ready.emit([], "")
	verify(not inventories[-1].error.is_empty() and inventories[-1].voices.is_empty(), "both inventories empty becomes an explicit text-only state")
	release(adapter)

	adapter = attach()
	adapter.query_voices()
	adapter._neural.voices_ready.emit(neural_voices, "")
	adapter.synthesize(6, "Never spoken after hiding.", "kokoro:am_michael")
	adapter._neural.failed.emit(6, "runtime unavailable")
	adapter.cancel_synthesis()
	adapter._desktop.voices_ready.emit(desktop_voices, "")
	verify(adapter._desktop.requests.is_empty(), "cancellation during fallback discovery prevents later synthesis")
	release(adapter)

	var provider := NeuralAdapter.new()
	verify(provider._choose_config(PackedStringArray(["--voice-config", "C:/launcher/config.json"]), "res://project.godot", "C:/local") == "C:/launcher/config.json", "launcher configuration wins over project and virtualized LocalAppData")
	verify(provider._choose_config([], "res://project.godot", "C:/local") == "res://project.godot", "existing repository configuration precedes LocalAppData")
	verify(provider._choose_config([], "res://missing-voice-config.json", "C:/local") == "C:/local/Regicide/rook-voice/config.json", "legacy LocalAppData configuration remains a final fallback")
	provider.config_path = "relative/config.json"
	verify(not provider._prepare(), "relative config paths cannot launch Python")
	provider.config_path = "C:/local voice/config.json"
	provider._config = {"python": "C:/Python Runtime/python.exe"}
	provider._helper = "C:/private temp/rook_voice.py"
	var command: Dictionary = provider._command("C:/private temp/request_1/request.json")
	verify(command.executable == "C:/Python Runtime/python.exe" and command.arguments == PackedStringArray(["C:/private temp/rook_voice.py", "--config", "C:/local voice/config.json", "--request", "C:/private temp/request_1/request.json"]), "Python uses distinct fixed path arguments without shell or spoken text interpolation")
	var directory: String = ProjectSettings.globalize_path("user://").path_join("redirector_fixture_%d" % Time.get_ticks_usec())
	DirAccess.make_dir_recursive_absolute(directory.path_join("Scripts"))
	for filename in ["Scripts/python.exe", "pyvenv.cfg", "model.onnx", "voices.bin"]:
		var stub := FileAccess.open(directory.path_join(filename), FileAccess.WRITE)
		stub.store_string("test")
		stub.close()
	var config_file := FileAccess.open(directory.path_join("config.json"), FileAccess.WRITE)
	config_file.store_string(JSON.stringify({"version": 1, "provider": "kokoro", "python": directory.path_join("Scripts/python.exe"), "model": directory.path_join("model.onnx"), "voices": directory.path_join("voices.bin")}))
	config_file.close()
	provider.config_path = directory.path_join("config.json")
	verify(not provider._prepare() and provider._jobs.is_empty(), "legacy Windows venv redirector is rejected before any child process can be created")
	for filename in ["Scripts/python.exe", "pyvenv.cfg", "model.onnx", "voices.bin", "config.json"]:
		DirAccess.remove_absolute(directory.path_join(filename))
	DirAccess.remove_absolute(directory.path_join("Scripts"))
	DirAccess.remove_absolute(directory)
	provider.free()
	print("Local neural voice routing: ", checks, " checks, ", failures, " failures; no processes or audio playback")
	quit(1 if failures else 0)
