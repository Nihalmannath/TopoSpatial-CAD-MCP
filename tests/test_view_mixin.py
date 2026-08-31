"""Window discovery tests for CAD product-specific shells."""

from types import SimpleNamespace

from adapters.mixins.view_mixin import ViewMixin


class _ViewAdapter(ViewMixin):
    cad_type = "autocad"

    def __init__(self, hwnd: int) -> None:
        self._application = SimpleNamespace(HWND=hwnd)

    def _get_application(self, operation: str = "operation"):
        return self._application


def test_find_cad_window_prefers_live_application_hwnd(monkeypatch):
    """ACA titles/classes do not matter when COM exposes the exact HWND."""
    monkeypatch.setattr("adapters.mixins.view_mixin.win32gui.IsWindow", lambda h: True)
    monkeypatch.setattr(
        "adapters.mixins.view_mixin.win32gui.EnumWindows",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fallback not expected")),
    )

    assert _ViewAdapter(525304)._find_cad_window() == 525304


def test_find_cad_window_accepts_architecture_mfc_shell(monkeypatch):
    """Fallback discovery recognizes AutoCAD Architecture's MFC main frame."""
    adapter = _ViewAdapter(0)

    monkeypatch.setattr("adapters.mixins.view_mixin.win32gui.IsWindow", lambda h: False)
    monkeypatch.setattr(
        "adapters.mixins.view_mixin.win32gui.IsWindowVisible", lambda h: True
    )
    monkeypatch.setattr(
        "adapters.mixins.view_mixin.win32gui.GetWindowText",
        lambda h: "AutoCAD Architecture 2024 - [Example.dwg]",
    )
    monkeypatch.setattr(
        "adapters.mixins.view_mixin.win32gui.GetClassName",
        lambda h: "AfxMDIFrame140u",
    )
    monkeypatch.setattr(
        "adapters.mixins.view_mixin.win32gui.EnumWindows",
        lambda callback, data: callback(991, data),
    )

    assert adapter._find_cad_window() == 991
