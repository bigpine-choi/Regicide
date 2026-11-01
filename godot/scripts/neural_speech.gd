extends "res://scripts/windows_speech.gd"
## Optional local Kokoro worker; shares the bounded WAV job protocol with SAPI.

var config_path: String = ""
var _config: Dictionary = {}


func _prepare() -> bool:
	if config_path.is_empty():
		config_path = _choose_config(OS.get_cmdline_user_args(), ProjectSettings.globalize_path("res://").path_join("../build/voice-models/config.json").simplify_path(), OS.get_environment("LOCALAPPDATA"))
	if not config_path.is_absolute_path() or not FileAccess.file_exists(config_path):
		return false
	var value: Variant = JSON.parse_string(FileAccess.get_file_as_string(config_path))
	if not value is Dictionary or value.get("version") != 1 or value.get("provider") != "kokoro":
		return false
	for key in ["python", "model", "voices"]:
		var path: String = str(value.get(key, ""))
		if not path.is_absolute_path() or not FileAccess.file_exists(path):
			return false
	# Windows venv python.exe is a redirector. It would create an unowned child
	# process that cannot be safely cancelled by the retained launch handle.
	var interpreter_folder: String = str(value.python).get_base_dir()
	if FileAccess.file_exists(interpreter_folder.path_join("pyvenv.cfg")) or FileAccess.file_exists(interpreter_folder.get_base_dir().path_join("pyvenv.cfg")):
		return false
	_config = value
	return super._prepare()


func _choose_config(arguments: PackedStringArray, project_config: String, local_data: String) -> String:
	for index in range(arguments.size() - 1):
		if arguments[index] == "--voice-config":
			return arguments[index + 1]
	if FileAccess.file_exists(project_config):
		return project_config
	return local_data.path_join("Regicide/rook-voice/config.json") if not local_data.is_empty() else ""


func _helper_resource() -> String:
	return "res://scripts/rook_voice.py"


func _helper_filename() -> String:
	return "rook_voice.py"


func _command(request_file: String) -> Dictionary:
	return {"executable": str(_config.python),
		"arguments": PackedStringArray([_helper, "--config", config_path, "--request", request_file])}
