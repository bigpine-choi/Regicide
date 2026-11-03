extends BoxContainer
## A dedicated opening column stays visible beside the board on wide windows.
## On smaller windows the page scrolls to the same full-size panel below it.

var opening_panel: PanelContainer


func _ready() -> void:
	name = "OpeningWorkspace"
	size_flags_horizontal = Control.SIZE_EXPAND_FILL
	size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_theme_constant_override("separation", 20)
	get_viewport().size_changed.connect(_reflow)
	call_deferred("_reflow")


func add_opening_panel() -> VBoxContainer:
	opening_panel = PanelContainer.new()
	opening_panel.name = "OpeningPanel"
	opening_panel.custom_minimum_size.x = 430
	var style := StyleBoxFlat.new()
	style.bg_color = Color("132134")
	style.set_corner_radius_all(10)
	style.set_content_margin_all(16)
	opening_panel.add_theme_stylebox_override("panel", style)
	add_child(opening_panel)
	var scroll := ScrollContainer.new()
	scroll.name = "OpeningPanelScroll"
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	opening_panel.add_child(scroll)
	var contents := VBoxContainer.new()
	contents.name = "OpeningContents"
	contents.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	contents.add_theme_constant_override("separation", 16)
	scroll.add_child(contents)
	return contents


func _reflow() -> void:
	if opening_panel == null or get_child_count() < 2:
		return
	vertical = get_viewport_rect().size.x < 1450
	var play_area := get_child(0) as Control
	play_area.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	play_area.size_flags_vertical = Control.SIZE_EXPAND_FILL
	play_area.custom_minimum_size.y = 560 if vertical else 0
	opening_panel.custom_minimum_size.y = 720 if vertical else 0
	opening_panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL if vertical else Control.SIZE_FILL
	queue_sort()
