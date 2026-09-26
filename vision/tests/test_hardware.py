"""The hardware probe must be right, and above all must never raise.

A bug in this module must not stop somebody unlocking their laptop, so
"never raises" is asserted directly. The rest of the file pins the two
behaviours that actually bite in the field:

  * a container with 168 host cores and a 2-CPU cpuset must not get 168
    inference threads, and
  * an accelerator that onnxruntime merely *lists* must not be believed
    until a real session comes back with it, because ORT downgrades to
    CPU with only a log line when a driver is unusable.
"""
import json
import os
import sys

import pytest

from faceid_vision import hardware as H
from faceid_vision import rt


# ---- CPU / container sizing -------------------------------------------

def test_cgroup_quota_wins_over_host_core_count(monkeypatch):
    """The bug this prevents: 168 host cores, 2 allowed, 168 threads."""
    monkeypatch.setattr(os, "cpu_count", lambda: 168)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(168)),
                        raising=False)
    monkeypatch.setattr(H, "_read_first_line", lambda path: {
        "/sys/fs/cgroup/cpu.max": "200000 100000",      # 2 CPUs
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "",
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "",
    }.get(path, ""))
    assert H.usable_cpus() == 2


def test_cgroup_v1_quota_is_honoured(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(64)),
                        raising=False)
    monkeypatch.setattr(H, "_read_first_line", lambda path: {
        "/sys/fs/cgroup/cpu.max": "",
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "150000",
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
    }.get(path, ""))
    assert H.usable_cpus() == 2      # ceil(1.5)


def test_affinity_wins_when_tighter_than_cgroup(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 32)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2},
                        raising=False)
    monkeypatch.setattr(H, "_read_first_line", lambda _path: "")
    assert H.usable_cpus() == 3


def test_unlimited_cgroup_falls_back_to_host_cpus(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)),
                        raising=False)
    monkeypatch.setattr(H, "_read_first_line", lambda path: {
        "/sys/fs/cgroup/cpu.max": "max 100000",
    }.get(path, ""))
    assert H.usable_cpus() == 8


def test_usable_cpus_never_returns_zero(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    monkeypatch.setattr(os, "sched_getaffinity",
                        lambda _pid: (_ for _ in ()).throw(OSError()),
                        raising=False)
    monkeypatch.setattr(H, "_cgroup_cpu_limit", lambda: None)
    assert H.usable_cpus() == 1


def test_cpu_flags_are_interesting_subset_and_stable():
    flags = H.cpu_flags()
    assert isinstance(flags, tuple)
    assert list(flags) == sorted(flags)
    assert set(flags) <= set(H._INTERESTING_FLAGS)


def test_container_detection_by_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(os.path, "exists", lambda p: str(p) == "/.dockerenv")
    monkeypatch.setattr(H, "_read_first_line", lambda p: "")
    assert H.detect_container() is True


# ---- thread sizing ----------------------------------------------------

def test_threads_are_capped_for_latency(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "usable_cpus", lambda: 64)
    monkeypatch.setattr(H, "cpu_flags", lambda: ("avx2",))
    monkeypatch.setattr(H, "detect_container", lambda: False)
    monkeypatch.setattr(H, "probe_providers",
                        lambda mp: (("CPUExecutionProvider",), "cpu", True))
    monkeypatch.setenv("FACEID_HARDWARE_CACHE", str(tmp_path / "h.json"))
    for var in ("FACEID_INTRA_OP_THREADS", "FACEID_INTER_OP_THREADS",
                "FACEID_MAX_THREADS"):
        monkeypatch.delenv(var, raising=False)
    p = H.probe(use_cache=False)
    assert p.intra_op_threads == H._DEFAULT_MAX_THREADS
    assert p.inter_op_threads == 1


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "usable_cpus", lambda: 64)
    monkeypatch.setattr(H, "cpu_flags", lambda: ())
    monkeypatch.setattr(H, "detect_container", lambda: False)
    monkeypatch.setattr(H, "probe_providers",
                        lambda mp: (("CPUExecutionProvider",), "cpu", True))
    monkeypatch.setenv("FACEID_HARDWARE_CACHE", str(tmp_path / "h.json"))
    monkeypatch.setenv("FACEID_INTRA_OP_THREADS", "1")
    p = H.probe(use_cache=False)
    assert p.intra_op_threads == 1


def test_a_hostile_env_override_is_ignored(monkeypatch, tmp_path):
    """A typo must not become 0 threads or 10^9 threads."""
    monkeypatch.setattr(H, "usable_cpus", lambda: 4)
    monkeypatch.setattr(H, "cpu_flags", lambda: ())
    monkeypatch.setattr(H, "detect_container", lambda: False)
    monkeypatch.setattr(H, "probe_providers",
                        lambda mp: (("CPUExecutionProvider",), "cpu", True))
    monkeypatch.setenv("FACEID_HARDWARE_CACHE", str(tmp_path / "h.json"))
    for bad in ("0", "-4", "abc", ""):
        monkeypatch.setenv("FACEID_INTRA_OP_THREADS", bad)
        p = H.probe(use_cache=False)
        assert p.intra_op_threads >= 1


# ---- provider selection ------------------------------------------------

def test_preference_order_is_respected(monkeypatch):
    listed = ["CPUExecutionProvider", "CUDAExecutionProvider",
              "TensorrtExecutionProvider", "DmlExecutionProvider"]
    monkeypatch.setattr(H, "_ort", lambda: type(
        "O", (), {"get_available_providers": staticmethod(lambda: listed)})())
    providers, detail, verified = H.probe_providers(None)
    assert list(providers) == [
        "TensorrtExecutionProvider", "CUDAExecutionProvider",
        "DmlExecutionProvider", "CPUExecutionProvider",
    ]
    assert verified is False, "no model means unverified, and it must say so"


def test_a_listed_provider_that_falls_back_is_not_believed(monkeypatch,
                                                          tmp_path):
    """ORT downgrades to CPU with only a log line. The request is not
    evidence; what came back is."""
    model = tmp_path / "m.onnx"
    model.write_bytes(b"stub")

    class _Sess:
        def __init__(self, providers):
            # Emulate the silent downgrade ORT performs.
            self._p = (["CPUExecutionProvider"]
                       if providers != ["CPUExecutionProvider"] else providers)

        def get_providers(self):
            return self._p

    built = []

    class _Ort:
        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(path, sess_options=None, providers=None):
            built.append(list(providers))
            return _Sess(providers)

        class SessionOptions:
            intra_op_num_threads = 0
            inter_op_num_threads = 0
            graph_optimization_level = None

        class GraphOptimizationLevel:
            ORT_ENABLE_ALL = 1

    monkeypatch.setattr(H, "_ort", lambda: _Ort)
    providers, detail, verified = H.probe_providers(model)
    assert list(providers) == ["CPUExecutionProvider"]
    assert detail == "CPUExecutionProvider"
    assert verified is True
    assert built, "a session must actually have been constructed"


def test_a_working_accelerator_is_kept(monkeypatch, tmp_path):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"stub")

    class _Sess:
        def __init__(self, providers):
            self._p = list(providers)

        def get_providers(self):
            return self._p

    class _Ort:
        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(path, sess_options=None, providers=None):
            return _Sess(providers)

        class SessionOptions:
            intra_op_num_threads = 0
            inter_op_num_threads = 0
            graph_optimization_level = None

        class GraphOptimizationLevel:
            ORT_ENABLE_ALL = 1

    monkeypatch.setattr(H, "_ort", lambda: _Ort)
    providers, detail, _verified = H.probe_providers(model)
    assert list(providers) == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert "probed" in detail


def test_missing_onnxruntime_degrades_to_cpu(monkeypatch):
    def _boom():
        raise ImportError("no onnxruntime")

    monkeypatch.setattr(H, "_ort", _boom)
    providers, detail, verified = H.probe_providers(None)
    assert list(providers) == ["CPUExecutionProvider"]
    assert "unavailable" in detail
    assert verified is False


# ---- never raises -----------------------------------------------------

def test_probe_never_raises_however_bad_the_host(monkeypatch, tmp_path):
    monkeypatch.setenv("FACEID_HARDWARE_CACHE", str(tmp_path / "h.json"))
    monkeypatch.setattr(H, "usable_cpus",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(H, "cpu_flags",
                        lambda: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(H, "detect_container",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(H, "probe_providers",
                        lambda mp: (_ for _ in ()).throw(RuntimeError("boom")))
    p = H.probe(use_cache=False)
    assert p.intra_op_threads >= 1
    assert p.accel == "cpu"
    assert p.notes, "a degraded probe must leave a note"


def test_probe_never_raises_when_the_cache_is_unwritable(monkeypatch,
                                                         tmp_path):
    """The worker runs unprivileged and must not be able to write
    /var/lib/faceid-nim -- a cache miss is not an error."""
    monkeypatch.setenv("FACEID_HARDWARE_CACHE", "/proc/nope/h.json")
    monkeypatch.setattr(H, "usable_cpus", lambda: 4)
    monkeypatch.setattr(H, "cpu_flags", lambda: ())
    monkeypatch.setattr(H, "detect_container", lambda: False)
    monkeypatch.setattr(H, "probe_providers",
                        lambda mp: (("CPUExecutionProvider",), "cpu", True))
    p = H.probe(use_cache=False)
    assert p.cpu_count == 4


def test_cache_roundtrip_and_invalidation(tmp_path, monkeypatch):
    cache = tmp_path / "hardware.json"
    monkeypatch.setenv("FACEID_HARDWARE_CACHE", str(cache))
    monkeypatch.setattr(H, "usable_cpus", lambda: 4)
    monkeypatch.setattr(H, "cpu_flags", lambda: ("avx2",))
    monkeypatch.setattr(H, "detect_container", lambda: False)
    monkeypatch.setattr(H, "probe_providers",
                        lambda mp: (("CPUExecutionProvider",), "cpu", True))

    calls = []

    def _counting(mp):
        calls.append(mp)
        return (("CPUExecutionProvider",), "cpu", True)

    monkeypatch.setattr(H, "probe_providers", _counting)
    first = H.probe(use_cache=True)
    assert len(calls) == 1
    second = H.probe(use_cache=True)
    assert len(calls) == 1, "the second probe must come from the cache"
    assert second == first
    assert json.loads(cache.read_text())["cpu_count"] == 4

    # A corrupt cache is ignored, not fatal.
    cache.write_text("{not json")
    third = H.probe(use_cache=True)
    assert len(calls) == 2
    assert third.cpu_count == 4


# ---- the session factory ----------------------------------------------

def test_session_falls_back_to_cpu_when_the_accelerator_fails(
        monkeypatch, tmp_path):
    """An accelerator that cannot initialise must cost speed, never the
    feature."""
    model = tmp_path / "m.onnx"
    model.write_bytes(b"stub")
    seen = {}

    class _Sess:
        def __init__(self, providers):
            seen["providers"] = list(providers)

        def get_providers(self):
            return seen["providers"]

    class _Ort:
        class SessionOptions:
            intra_op_num_threads = 0
            inter_op_num_threads = 0
            graph_optimization_level = None

        class GraphOptimizationLevel:
            ORT_ENABLE_ALL = 1

        @staticmethod
        def InferenceSession(path, sess_options=None, providers=None):
            if providers != ["CPUExecutionProvider"]:
                raise RuntimeError("libcuda.so.1: cannot open shared object")
            return _Sess(providers)

    cache = tmp_path / "h.json"
    cache.write_text(json.dumps({
        "arch": "x86_64", "cpu_count": 8, "cpu_features": ["avx2"],
        "containerized": False, "accel": "cuda", "accel_detail": "CUDA",
        "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "intra_op_threads": 4, "inter_op_threads": 1, "unverified": False,
        "notes": [],
    }))
    monkeypatch.setenv("FACEID_HARDWARE_CACHE", str(cache))
    # rt.session() imports onnxruntime lazily, so the stub goes into
    # sys.modules rather than onto the real package.
    monkeypatch.setitem(sys.modules, "onnxruntime", _Ort)

    rt.session(model)
    assert seen["providers"] == ["CPUExecutionProvider"]


def test_session_honours_an_explicit_provider_override(monkeypatch, tmp_path):
    """`providers=` still wins, for tests and for anyone who knows better
    than the probe."""
    model = tmp_path / "m.onnx"
    model.write_bytes(b"stub")
    seen = {}

    class _Sess:
        def __init__(self, providers):
            seen["providers"] = list(providers)

        def get_providers(self):
            return seen["providers"]

    class _Ort:
        class SessionOptions:
            intra_op_num_threads = 0
            inter_op_num_threads = 0
            graph_optimization_level = None

        class GraphOptimizationLevel:
            ORT_ENABLE_ALL = 1

        @staticmethod
        def InferenceSession(path, sess_options=None, providers=None):
            return _Sess(providers)

    monkeypatch.setitem(sys.modules, "onnxruntime", _Ort)
    rt.session(model, providers=["CPUExecutionProvider"])
    assert seen["providers"] == ["CPUExecutionProvider"]


def test_profile_summary_is_one_line():
    p = H.HardwareProfile(cpu_count=8, intra_op_threads=4,
                          accel_detail="CPUExecutionProvider")
    s = p.summary()
    assert "\n" not in s
    assert "8 CPU threads" in s
