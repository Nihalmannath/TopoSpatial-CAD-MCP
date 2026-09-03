from pathlib import Path


SOURCE = Path("plugins/autocad/src/TopoSpatialPlugin.cs")


def test_show_editor_does_not_enable_events() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    body = source.split('CommandMethod("TOPOSTUDIO"', 1)[1].split('CommandMethod("TOPOSTUDIORELOAD"', 1)[0]
    assert "_eventsEnabled = true" not in body
    assert "AttachGlobalEvents()" not in body


def test_plugin_never_activates_or_creates_documents() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "Documents.Add" not in source
    assert ".Activate()" not in source
    assert "MdiActiveDocument =" not in source
