extends Node
## Local Desktop SAPI -> WAV adapter. No text is interpolated into shell commands.

signal voices_ready(voices: Array, error: String)
signal audio_ready(ticket: int, stream: AudioStreamWAV)
signal failed(ticket: int, error: String)

const HELPER_RESOURCE: String = "res://scripts/windows_speech.ps1"
const REQUEST_TIMEOUT_MS: int = 20000
const CANCEL_GRACE_MS: int = 1000
const JOB_FILES: Array[String] = ["request.json", "response.json", "response.tmp", "speech.wav", "cancel"]

var _folder: String = ""
var _helper: String = ""
var _helper_ready: bool = false
var _serial: int = 0
var _jobs: Array[Dictionary] = []
var _owned_pids: Dictionary = {}
var _poll_elapsed: float = 0.0


func query_voices() -> void:
	_launch("voices", 0, "", "")


func synthesize(ticket: int, text: String, voice: String) -> void:
	cancel_synthesis()
	_launch("synthesize", ticket, text, voice)


func cancel_synthesis() -> void:
	for job in _jobs:
		if job.action == "synthesize" and not job.cancelled:
			job.cancelled = true
			job.cancelled_at = Time.get_ticks_msec()
			_touch_cancel(job)


func _prepare() -> bool:
	if _helper_ready and FileAccess.file_exists(_helper):
		return true
	if OS.get_name() != "Windows":
		return false
	_helper_ready = false
	if _folder.is_empty():
		_folder = ProjectSettings.globalize_path("user://rook_voice/").path_join("session_%d_%d" % [OS.get_process_id(), Time.get_ticks_usec()])
		if DirAccess.make_dir_recursive_absolute(_folder) != OK:
			_folder = ""
			return false
		_helper = _folder.path_join("windows_speech.ps1")
	var script: String = FileAccess.get_file_as_string(HELPER_RESOURCE)
	if script.is_empty():
		return false
	var output := FileAccess.open(_helper, FileAccess.WRITE)
	if output == null:
		return false
	output.store_string(script)
	_helper_ready = output.get_error() == OK
	output.close()
	return _helper_ready


func _launch(action: String, ticket: int, text: String, voice: String) -> void:
	if not _prepare():
		_emit_error(action, ticket, "Windows 음성 도우미를 준비하지 못했습니다.")
		return
	_serial += 1
	var request_id: String = "request_%d" % _serial
	var directory: String = _folder.path_join(request_id)
	if DirAccess.make_dir_recursive_absolute(directory) != OK:
		_emit_error(action, ticket, "음성 임시 폴더를 만들지 못했습니다.")
		return
	var request_file: String = directory.path_join("request.json")
	var file := FileAccess.open(request_file, FileAccess.WRITE)
	if file == null:
		_emit_error(action, ticket, "음성 요청을 저장하지 못했습니다.")
		return
	file.store_string(JSON.stringify({"request_id": request_id, "action": action, "text": text, "voice_id": voice}))
	file.close()
	var executable: String = OS.get_environment("SystemRoot").path_join("System32/WindowsPowerShell/v1.0/powershell.exe")
	var pid: int = OS.create_process(executable, PackedStringArray(["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", _helper, "-RequestPath", request_file]), false)
	var job: Dictionary = {"pid": pid, "action": action, "ticket": ticket, "request_id": request_id, "folder": directory, "started": Time.get_ticks_msec(), "cancelled": false, "cancelled_at": 0}
	if pid <= 0:
		_cleanup(job)
		_emit_error(action, ticket, "Windows 음성 도우미를 실행하지 못했습니다.")
		return
	_owned_pids[pid] = true
	_jobs.append(job)
	set_process(true)


func _process(delta: float) -> void:
	_poll_elapsed += delta
	if _poll_elapsed < 0.05:
		return
	_poll_elapsed = 0.0
	_poll_jobs()


func _poll_jobs() -> void:
	for index in range(_jobs.size() - 1, -1, -1):
		var job: Dictionary = _jobs[index]
		var now: int = Time.get_ticks_msec()
		var expired: bool = now - int(job.started) > REQUEST_TIMEOUT_MS
		var cancel_expired: bool = bool(job.cancelled) and now - int(job.cancelled_at) > CANCEL_GRACE_MS
		var running: bool = OS.is_process_running(int(job.pid))
		if running and not expired and not cancel_expired:
			continue
		# Godot retains a Windows process HANDLE for our create_process result.
		# Retire it exactly once; never enumerate, match names, or kill arbitrary PIDs.
		_retire_owned_process(int(job.pid))
		_jobs.remove_at(index)
		if not job.cancelled:
			if expired:
				_emit_error(str(job.action), int(job.ticket), "음성 준비 시간이 초과됐습니다. 다시 켜서 시도해 주세요.")
			else:
				_accept_result(job)
		_cleanup(job)
	set_process(not _jobs.is_empty())


func _accept_result(job: Dictionary) -> void:
	var response: String = str(job.folder).path_join("response.json")
	var value: Variant = JSON.parse_string(FileAccess.get_file_as_string(response)) if FileAccess.file_exists(response) else null
	if not value is Dictionary or value.get("request_id") != job.request_id or not bool(value.get("ok", false)):
		_emit_error(str(job.action), int(job.ticket), "Windows 음성을 만들지 못했습니다. 다시 켜서 시도해 주세요.")
		return
	if job.action == "voices":
		var voices: Array = []
		for voice in value.get("voices", []):
			if voice is Dictionary and str(voice.get("language", "")).to_lower().replace("_", "-").get_slice("-", 0) == "en" and not str(voice.get("id", "")).is_empty():
				voices.append(voice)
		voices_ready.emit(voices, "")
	else:
		var path: String = str(job.folder).path_join("speech.wav")
		var wav: AudioStreamWAV = AudioStreamWAV.load_from_file(path) if FileAccess.file_exists(path) else null
		if wav == null or wav.data.is_empty() or wav.get_length() <= 0.0:
			_emit_error(str(job.action), int(job.ticket), "생성된 음성을 읽지 못했습니다. 다시 켜서 시도해 주세요.")
		else:
			audio_ready.emit(int(job.ticket), wav)


func _emit_error(action: String, ticket: int, message: String) -> void:
	if action == "voices":
		voices_ready.emit([], message)
	else:
		failed.emit(ticket, message)


func _touch_cancel(job: Dictionary) -> void:
	var file := FileAccess.open(str(job.folder).path_join("cancel"), FileAccess.WRITE)
	if file != null:
		file.store_string("cancel")
		file.close()


func _retire_owned_process(pid: int) -> void:
	if not _owned_pids.has(pid):
		return
	_owned_pids.erase(pid)
	# Also closes the retained HANDLE after a normal exit (no PID reuse race).
	OS.kill(pid)


func _cleanup(job: Dictionary) -> void:
	var directory: String = str(job.folder)
	if _folder.is_empty() or directory.get_base_dir() != _folder or not directory.get_file().begins_with("request_"):
		return
	for name in JOB_FILES:
		var path: String = directory.path_join(name)
		if FileAccess.file_exists(path):
			DirAccess.remove_absolute(path)
	DirAccess.remove_absolute(directory)


func _exit_tree() -> void:
	for job in _jobs:
		_touch_cancel(job)
		_retire_owned_process(int(job.pid))
		_cleanup(job)
	_jobs.clear()
	if not _helper.is_empty() and FileAccess.file_exists(_helper):
		DirAccess.remove_absolute(_helper)
	if not _folder.is_empty():
		DirAccess.remove_absolute(_folder)
