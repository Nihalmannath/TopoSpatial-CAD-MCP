"""Native openings must anchor in the right drawing or fail loudly."""
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from adapters.mixins import architecture_mixin as am  # noqa: E402

Mixin = am.ArchitectureMixin if hasattr(am, "ArchitectureMixin") else next(
    v for k, v in vars(am).items() if k.endswith("Mixin") and isinstance(v, type)
)


class Doc:
    def __init__(self, name, app):
        self.Name, self.app, self.activate_calls = name, app, 0

    def Activate(self):
        self.activate_calls += 1
        self.app.pending = self  # activation lands on the next check


class App:
    def __init__(self):
        self.pending = None
        self._active = None

    @property
    def ActiveDocument(self):
        if self.pending is not None:
            self._active, self.pending = self.pending, None
        return self._active

    @ActiveDocument.setter
    def ActiveDocument(self, doc):
        self.pending = doc


def test_activates_target_drawing(monkeypatch):
    monkeypatch.setattr(am.time, "sleep", lambda s: None)
    app = App()
    other, target = Doc("Other.dwg", app), Doc("Plan.dwg", app)
    app._active = other
    Mixin._ensure_document_active(app, target)
    assert app.ActiveDocument is target


def test_activation_failure_is_loud(monkeypatch):
    monkeypatch.setattr(am.time, "sleep", lambda s: None)
    class StuckApp:
        ActiveDocument = property(lambda self: SimpleNamespace(Name="Other.dwg"), lambda self, d: None)

    app = StuckApp()
    stuck = SimpleNamespace(Name="Plan.dwg", Activate=lambda: None)
    with pytest.raises(RuntimeError, match="did not activate"):
        Mixin._ensure_document_active(app, stuck, attempts=3)


def _ent(lo, hi):
    return SimpleNamespace(GetBoundingBox=lambda: (lo, hi))


def test_opening_at_origin_is_detected():
    wall = _ent((3500, 0, 0), (7400, 250, 3000))
    assert Mixin._opening_on_host(_ent((3600, 100, 0), (4500, 1000, 2100)), wall, 900)
    assert not Mixin._opening_on_host(_ent((0, 0, 0), (900, 900, 2100)), wall, 900)


def test_unreadable_extents_do_not_block():
    broken = SimpleNamespace(GetBoundingBox=lambda: (_ for _ in ()).throw(Exception("no")))
    assert Mixin._opening_on_host(broken, broken, 900)
