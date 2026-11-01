extends "res://scripts/windows_speech.gd"
## Prefer optional neural speech, with a one-way SAPI fallback for this session.
## Both providers use local files only. Cancelled tickets never trigger fallback.

const DesktopAdapter = preload("res://scripts/windows_speech.gd")
const NeuralAdapter = preload("res://scripts/neural_speech.gd")

var _neural: DesktopAdapter
var _desktop: DesktopAdapter
var _prefer_neural: bool = true
var _querying: bool = false
var _desktop_querying: bool = false
var _desktop_voice: String = ""
var _active: Dictionary = {}
var _fallback_notice: String = ""


func _make_neural_adapter() -> DesktopAdapter:
	return NeuralAdapter.new()


func _make_desktop_adapter() -> DesktopAdapter:
	return DesktopAdapter.new()


func _ensure_providers() -> void:
	if is_instance_valid(_neural):
		return
	_neural = _make_neural_adapter()
	_desktop = _make_desktop_adapter()
	add_child(_neural)
	add_child(_desktop)
	_neural.voices_ready.connect(_neural_voices_ready)
	_neural.audio_ready.connect(_neural_audio_ready)
	_neural.failed.connect(_neural_failed)
	_desktop.voices_ready.connect(_desktop_voices_ready)
	_desktop.audio_ready.connect(_desktop_audio_ready)
	_desktop.failed.connect(_desktop_failed)


func query_voices() -> void:
	_ensure_providers()
	if _querying:
		return
	_querying = true
	if _prefer_neural:
		_neural.query_voices()
	else:
		if not _fallback_notice.is_empty():
			status_changed.emit("sapi", _fallback_notice)
		_query_desktop()


func _query_desktop() -> void:
	if _desktop_querying:
		return
	_desktop_querying = true
	_desktop.query_voices()


func _neural_voices_ready(voices: Array, error: String) -> void:
	if not _querying or not _prefer_neural:
		return
	if error.is_empty() and not voices.is_empty():
		_querying = false
		status_changed.emit("kokoro", "루크 · 미국식 영어 · 수를 넘기면 자동 안내")
		voices_ready.emit(voices, "")
	else:
		_use_desktop("루크 음성이 준비되지 않아 기본 영어 음성으로 안내합니다.")
		_query_desktop()


func _use_desktop(message: String) -> void:
	_prefer_neural = false
	_fallback_notice = message
	status_changed.emit("sapi", message)


func _desktop_voices_ready(voices: Array, error: String) -> void:
	if not _desktop_querying:
		return
	_desktop_querying = false
	_desktop_voice = str(voices[0].get("id", "")) if not voices.is_empty() else ""
	var failure: String = error if not error.is_empty() else ("사용 가능한 영어 음성이 없어 글로 안내합니다." if _desktop_voice.is_empty() else "")
	if _querying:
		_querying = false
		voices_ready.emit(voices, failure)
	if not _active.is_empty() and _active.get("provider") == "sapi":
		if failure.is_empty():
			_desktop.synthesize(int(_active.ticket), str(_active.text), _desktop_voice)
		else:
			var ticket: int = int(_active.ticket)
			_active = {}
			failed.emit(ticket, failure)


func synthesize(ticket: int, text: String, voice: String) -> void:
	_ensure_providers()
	cancel_synthesis()
	_active = {"ticket": ticket, "text": text, "provider": "kokoro" if _prefer_neural else "sapi"}
	if _prefer_neural:
		_neural.synthesize(ticket, text, voice)
	elif not _desktop_voice.is_empty():
		_desktop.synthesize(ticket, text, _desktop_voice)
	else:
		_query_desktop()


func _neural_audio_ready(ticket: int, stream: AudioStreamWAV) -> void:
	if _active.get("ticket") != ticket or _active.get("provider") != "kokoro":
		return
	_active = {}
	audio_ready.emit(ticket, stream)


func _neural_failed(ticket: int, _error: String) -> void:
	if _active.get("ticket") != ticket or _active.get("provider") != "kokoro":
		return
	_use_desktop("루크 음성을 만들지 못해 기본 영어 음성으로 전환했습니다.")
	_active.provider = "sapi"
	if not _desktop_voice.is_empty():
		_desktop.synthesize(ticket, str(_active.text), _desktop_voice)
	else:
		_query_desktop()


func _desktop_audio_ready(ticket: int, stream: AudioStreamWAV) -> void:
	if _active.get("ticket") != ticket or _active.get("provider") != "sapi":
		return
	_active = {}
	audio_ready.emit(ticket, stream)


func _desktop_failed(ticket: int, error: String) -> void:
	if _active.get("ticket") != ticket or _active.get("provider") != "sapi":
		return
	_active = {}
	failed.emit(ticket, error)


func cancel_synthesis() -> void:
	_active = {}
	if is_instance_valid(_neural):
		_neural.cancel_synthesis()
	if is_instance_valid(_desktop):
		_desktop.cancel_synthesis()
