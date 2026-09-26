import importlib.util
import sys
import types
from pathlib import Path


class FakeError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class FakeVariant(tuple):
    def __new__(cls, signature, value):
        return tuple.__new__(cls, (signature, value))


class FakeProxy:
    def __init__(self, reply=None, error=None):
        self.calls = []
        self.reply = reply
        self.error = error

    def call(self, method, variant, flags, timeout_ms, cancellable, callback, user_data):
        self.calls.append((method, variant, timeout_ms))
        callback(self, self.reply if self.error is None else None, user_data)

    def call_finish(self, result):
        if self.error is not None:
            raise FakeError(self.error)
        return result


def load_client(monkeypatch):
    gi = types.ModuleType("gi")
    repository = types.ModuleType("gi.repository")
    gio = types.SimpleNamespace(
        DBusCallFlags=types.SimpleNamespace(ALLOW_INTERACTIVE_AUTHORIZATION=object()),
    )
    glib = types.SimpleNamespace(Error=FakeError, Variant=FakeVariant)
    repository.Gio = gio
    repository.GLib = glib
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    path = Path(__file__).resolve().parents[1] / "faceid_app" / "dbus_client.py"
    spec = importlib.util.spec_from_file_location("faceid_dbus_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_async_mutation_reports_dbus_errors(monkeypatch):
    client = load_client(monkeypatch)
    daemon = client.Daemon.__new__(client.Daemon)
    daemon._proxy = FakeProxy(error="denied")
    seen = []
    daemon.set_identity_enabled_async("me", True, seen.append)
    assert seen == ["denied"]


def test_async_timeline_parses_valid_json(monkeypatch):
    client = load_client(monkeypatch)
    daemon = client.Daemon.__new__(client.Daemon)
    daemon._proxy = FakeProxy(reply=('[{"result": "success"}]',))
    seen = []
    daemon.get_timeline_async(lambda items, error: seen.append((items, error)))
    assert seen == [([{"result": "success"}], None)]


def test_async_timeline_rejects_invalid_json(monkeypatch):
    client = load_client(monkeypatch)
    daemon = client.Daemon.__new__(client.Daemon)
    daemon._proxy = FakeProxy(reply=("not json",))
    seen = []
    daemon.get_timeline_async(lambda items, error: seen.append((items, error)))
    assert seen[0][0] == []
    assert seen[0][1]
