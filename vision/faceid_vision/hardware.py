"""What this machine can actually do, decided once and cached.

One probe, used by the worker, `faceid-nim status`, the app and the
tests, so none of them can disagree about whether an accelerator or a
landmark backend is available.

Two rules shape everything here:

1. **Never raise.** A bug in this module must not stop somebody from
   unlocking their laptop. Every failure path ends at "CPU", with a
   human-readable note explaining the downgrade.

2. **Probe, don't ask.** ``onnxruntime.get_available_providers()`` lists
   a provider when its shared library is *present*; the driver can still
   be missing, out of date, or unusable without ``nvidia-uvm``. ORT then
   silently falls back to CPU. So a candidate is only believed once a
   real session has been constructed with it AND that provider actually
   came back in ``get_providers()``.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("faceid.vision.hardware")

#: Preference order. Fastest-supported first; CPU is the last resort and
#: is always available, so this list is never exhausted.
PROVIDER_PREFERENCE = (
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "ROCMExecutionProvider",
    "OpenVINOExecutionProvider",
    "CoreMLExecutionProvider",
    "DmlExecutionProvider",
    "CPUExecutionProvider",
)

CPU_EXECUTION_PROVIDER = "CPUExecutionProvider"

#: Feature flags worth reporting. NEON on ARM, AVX2/AVX512 on x86.
_INTERESTING_FLAGS = (
    "avx", "avx2", "avx512f", "f16c", "sse4_2", "sse4_1",
    "neon", "asimd", "fma",
)

#: A scan is latency-bound, not throughput-bound: one face, one verdict.
#: Past a handful of threads the extra pool costs more than it returns,
#: so the cap is deliberate rather than "one per core".
_DEFAULT_MAX_THREADS = 4


@dataclass(frozen=True)
class HardwareProfile:
    arch: str = ""
    cpu_count: int = 1
    cpu_features: tuple[str, ...] = ()
    containerized: bool = False
    accel: str = "cpu"
    accel_detail: str = "CPUExecutionProvider"
    providers: tuple[str, ...] = (CPU_EXECUTION_PROVIDER,)
    intra_op_threads: int = 1
    inter_op_threads: int = 1
    #: True when the provider list was believed without building a real
    #: session, i.e. no model was available to probe with.
    unverified: bool = True
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        d = asdict(self)
        d["cpu_features"] = list(self.cpu_features)
        d["providers"] = list(self.providers)
        d["notes"] = list(self.notes)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "HardwareProfile":
        return cls(
            arch=str(d.get("arch", "")),
            cpu_count=int(d.get("cpu_count", 1)),
            cpu_features=tuple(d.get("cpu_features", ())),
            containerized=bool(d.get("containerized", False)),
            accel=str(d.get("accel", "cpu")),
            accel_detail=str(d.get("accel_detail", "")),
            providers=tuple(d.get("providers", (CPU_EXECUTION_PROVIDER,))),
            intra_op_threads=int(d.get("intra_op_threads", 1)),
            inter_op_threads=int(d.get("inter_op_threads", 1)),
            unverified=bool(d.get("unverified", True)),
            notes=tuple(d.get("notes", ())),
        )

    def summary(self) -> str:
        """One line for `faceid-nim status` and the app's camera page."""
        bits = [f"{self.accel_detail}"]
        bits.append(f"{self.cpu_count} CPU threads, "
                    f"{self.intra_op_threads} per inference")
        if self.containerized:
            bits.append("containerised")
        return "; ".join(bits)


# ---- CPU / container detection -----------------------------------------

def _read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def _cgroup_cpu_limit() -> int | None:
    """CPUs this cgroup is allowed to use, or None when unlimited.

    A container can see 168 host cores while being pinned to 2. Sizing
    the thread pool from ``os.cpu_count()`` there is how a "face unlock"
    ends up spawning 168 threads to answer one 4-second scan.
    """
    # cgroup v2: "<quota> <period>", quota "max" means unlimited.
    raw = _read_first_line("/sys/fs/cgroup/cpu.max")
    if raw:
        parts = raw.split()
        if parts and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    return max(1, -(-quota // period))    # ceil
            except (ValueError, IndexError):
                pass
    # cgroup v1
    quota = _read_first_line("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_first_line("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period:
        try:
            q, p = int(quota), int(period)
            if q > 0 and p > 0:
                return max(1, -(-q // p))
        except ValueError:
            pass
    return None


def detect_container() -> bool:
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    for marker in ("/proc/1/cgroup", "/proc/self/cgroup"):
        try:
            with open(marker, "r", encoding="utf-8", errors="replace") as fh:
                blob = fh.read()
        except OSError:
            continue
        if any(t in blob for t in ("docker", "kubepods", "containerd", "lxc")):
            return True
    return False


def usable_cpus() -> int:
    """CPUs we may actually run on right now. Never returns < 1."""
    candidates: list[int] = []
    try:
        candidates.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    if os.cpu_count():
        candidates.append(os.cpu_count())
    limit = _cgroup_cpu_limit()
    if limit:
        candidates.append(limit)
    return max(1, min(candidates)) if candidates else 1


def cpu_flags() -> tuple[str, ...]:
    """Interesting SIMD/ISA flags from /proc/cpuinfo, order-stable."""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
            blob = fh.read()
    except OSError:
        return ()
    found: set[str] = set()
    for line in blob.splitlines():
        if line.lower().startswith(("flags", "features")):
            for tok in line.split(":", 1)[-1].split():
                low = tok.lower()
                if low in _INTERESTING_FLAGS:
                    found.add(low)
            break
    return tuple(sorted(found))


# ---- provider selection ------------------------------------------------

def _ort():
    import onnxruntime as ort  # noqa: PLC0415
    return ort


def probe_providers(model_path: Path | None) -> tuple[tuple[str, ...], str, bool]:
    """Return (providers_to_pass, detail, verified).

    With a model, each candidate is *proven*: a session is built with it
    and the provider is only believed if ORT actually kept it. Without a
    model, the installed provider list is filtered by preference and
    flagged unverified -- better than nothing, and honestly labelled.
    """
    notes: list[str] = []
    try:
        ort = _ort()
        available = list(ort.get_available_providers())
    except Exception as e:
        return (CPU_EXECUTION_PROVIDER,), \
            f"onnxruntime unavailable ({type(e).__name__}); CPU only", False

    ordered = [p for p in PROVIDER_PREFERENCE if p in available]
    if not ordered:
        ordered = [CPU_EXECUTION_PROVIDER]

    if model_path is None or not Path(model_path).exists():
        detail = ordered[0]
        notes.append("accelerator not verified: no model available to probe")
        return tuple(ordered), detail, False

    for candidate in ordered:
        if candidate == CPU_EXECUTION_PROVIDER:
            return (CPU_EXECUTION_PROVIDER,), "CPUExecutionProvider", True
        try:
            sess = ort.InferenceSession(
                str(model_path), sess_options=_session_options(1, 1),
                providers=[candidate])
        except Exception as e:
            notes.append(f"{candidate}: session build failed "
                         f"({type(e).__name__})")
            continue
        # ORT falls back to CPU with only a log line when a provider's
        # driver is unusable, so the request is not evidence. What came
        # back is.
        got = list(sess.get_providers())
        if got and got[0] == candidate:
            return (candidate, CPU_EXECUTION_PROVIDER), \
                f"{candidate} (probed)", True
        notes.append(f"{candidate}: listed but fell back to CPU at runtime")

    return (CPU_EXECUTION_PROVIDER,), "CPUExecutionProvider", True


def _session_options(intra: int, inter: int):
    ort = _ort()
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, int(intra))
    so.inter_op_num_threads = max(1, int(inter))
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return so


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


# ---- cache -------------------------------------------------------------

def cache_path() -> Path | None:
    """Where the probe result is cached, or None when not writable.

    Prefers the worker's own runtime directory. It deliberately does NOT
    use /var/lib/faceid-nim: that root is 0711 traverse-only by design,
    so the unprivileged worker cannot write there -- and widening it for
    a cache would be a real privilege regression for no benefit.
    """
    env = os.environ.get("FACEID_HARDWARE_CACHE")
    if env:
        return Path(env)
    return Path("/run/faceid-nim/worker/hardware.json")


def _load_cache(path: Path) -> HardwareProfile | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return HardwareProfile.from_dict(json.load(fh))
    except (OSError, ValueError, TypeError):
        return None


def _store_cache(path: Path, profile: HardwareProfile) -> None:
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(profile.as_dict(), fh)
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        log.debug("hardware profile not cached at %s: %s", path, e)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


# ---- the probe ---------------------------------------------------------

def probe(model_path: Path | None = None, *, use_cache: bool = True) -> HardwareProfile:
    """Build the profile for this machine. Cannot raise."""
    path = cache_path()
    if use_cache and path is not None:
        cached = _load_cache(path)
        if cached is not None:
            return cached

    notes: list[str] = []
    try:
        arch = platform.machine() or "unknown"
    except Exception:
        arch = "unknown"
    cpus = 1
    flags: tuple[str, ...] = ()
    containerized = False
    try:
        cpus = usable_cpus()
        flags = cpu_flags()
        containerized = detect_container()
    except Exception as e:                                # pragma: no cover
        notes.append(f"CPU probe degraded: {type(e).__name__}: {e}")

    try:
        providers, detail, verified = probe_providers(model_path)
    except Exception as e:                                # pragma: no cover
        providers, detail, verified = (CPU_EXECUTION_PROVIDER,), \
            f"CPU only (probe failed: {type(e).__name__})", False
        notes.append(f"accelerator probe failed: {type(e).__name__}: {e}")

    cap = _int_env("FACEID_MAX_THREADS") or _DEFAULT_MAX_THREADS
    intra = _int_env("FACEID_INTRA_OP_THREADS") or max(1, min(cpus, cap))
    inter = _int_env("FACEID_INTER_OP_THREADS") or 1

    if not verified:
        notes.append("accelerator reported by onnxruntime but not proven "
                     "at runtime; it may fall back to CPU")
    if cpus < (os.cpu_count() or cpus):
        notes.append(f"restricted to {cpus} of {os.cpu_count()} host CPUs "
                     "(affinity/cgroup)")
    if not flags:
        notes.append("CPU feature flags unreadable; thread count is a guess")

    accel = "cpu"
    for p in providers:
        if p != CPU_EXECUTION_PROVIDER:
            accel = p.replace("ExecutionProvider", "").lower()
            break

    profile = HardwareProfile(
        arch=arch,
        cpu_count=cpus,
        cpu_features=flags,
        containerized=containerized,
        accel=accel,
        accel_detail=detail,
        providers=providers,
        intra_op_threads=intra,
        inter_op_threads=inter,
        unverified=not verified,
        notes=tuple(notes),
    )
    if path is not None:
        _store_cache(path, profile)
    return profile


# Convenience for callers that do not care about a model.
def profile() -> HardwareProfile:
    return probe(None)


__all__ = [
    "HardwareProfile",
    "PROVIDER_PREFERENCE",
    "CPU_EXECUTION_PROVIDER",
    "probe",
    "profile",
    "probe_providers",
    "usable_cpus",
    "cpu_flags",
    "detect_container",
    "cache_path",
]
