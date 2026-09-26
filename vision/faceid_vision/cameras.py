"""Hardware camera discovery for faceid-nim.

Enumerates /dev/video* capture devices through V4L2 ioctls directly
(no hardcoded paths, no third-party libraries) and classifies each as
RGB, IR or unusable using multiple independent signals:

  1. pixel-format fivecc (GREY/Y16/Y10/Y12 == monochrome == likely IR)
  2. device card name hints ("ir", "infrared", "thermal", "mono")
  3. optional live-capture probe result reported by the vision worker

Lives in the vision package, not the app, for one reason: headless
provisioning. The worker, `faceid-nim status` and the install-time
camera auto-configuration all need this, and none of them can import
anything out of the GTK app. The app re-exports it (see
app/faceid_app/camera_discovery.py) so there is exactly one
implementation and the UI can never disagree with the CLI.

It still uses nothing but the standard library, so the app can import
it before the vision venv is known to be alive.

IOCTL constants below were verified against linux/videodev2.h on the
target host (kernel 6.x): VIDIOC_QUERYCAP=0x80685600 (104B),
VIDIOC_ENUM_FMT=0xc0405602 (64B), VIDIOC_ENUM_FRAMESIZES=0xc02c564a
(44B).
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import struct
from dataclasses import dataclass, field

log = logging.getLogger("faceid.vision.cameras")

# --- V4L2 ioctl codes (verified against host linux/videodev2.h) -------
VIDIOC_QUERYCAP = 0x80685600
VIDIOC_ENUM_FMT = 0xC0405602
VIDIOC_ENUM_FRAMESIZES = 0xC02C564A

# v4l2_capability field offsets
CAP_DRIVER = 0x00  # 16 s, driver
CAP_CARD = 0x10  # 32 s, card
CAP_BUS_INFO = 0x30  # 32 s, bus_info
CAP_VERSION = 0x50  # u32 (offset currently unused)
CAP_CAPABILITIES = 0x54  # u32 (offset currently unused)
CAP_DEVICE_CAPS = 0x58  # u32: device-local caps, the field that matters

CAP_FORMAT = "<3I"
CAP_STR = "<16s32s32s3I"
CAP_TAIL = "<3I"

# v4l2_fmtdesc field offsets
FMT_INDEX = 0x00  # u32
FMT_TYPE = 0x04  # u32
FMT_FLAGS = 0x08  # u32
FMT_DESC = 0x0C  # 32 s
FMT_PIXFMT = 0x2C  # u32
FMT_STR = "<3I32s3I3I"

# v4l2_frmsizeenum field offsets
FRS_INDEX = 0x00  # u32
FRS_PIXFMT = 0x04  # u32
FRS_TYPE = 0x08  # u32
FRS_WIDTH = 0x0C  # u32 (discrete / stepwise share this offset)
FRS_HEIGHT = 0x10  # u32
FRS_STR = "<3I6I2I"

# flags / types
V4L2_BUF_TYPE_VIDEO_CAPTURE = 0x1
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_STREAMING = 0x04000000
V4L2_CAP_META_CAPTURE = 0x00800000
V4L2_CAP_DEVICE = 0x80000000
V4L2_FRMSIZE_TYPE_DISCRETE = 0x1
V4L2_FRMSIZE_TYPE_STEPWISE = 0x3

O_RDONLY = os.O_RDONLY
O_NONBLOCK = os.O_NONBLOCK
O_CLOEXEC = os.O_CLOEXEC

# fivecc values of monochrome formats.  A camera that only exposes
# 8/10/12-bit mono pixels is overwhelmingly an IR / depth device; this
# is the strongest single signal we have without actually capturing.
MONO_FIVECC = {
    "GREY",
    "Y8  ",
    "Y10 ",
    "Y12 ",
    "Y14 ",
    "Y16 ",
    "Y8I ",
    "Y10I",
    "Y12I",
    "Y16I",
    "Z16 ",
    "Z14 ",
}

IR_NAME_HINTS = (
    "ir",
    "infrared",
    "invisible",
    "thermal",
    "depth",
    "mono",
)

KW_RGB = "video"


def fivecc(f) -> str:
    """Unexpand a u32 pixel format into its 4-char fivecc."""
    return "".join(chr((f >> (8 * i)) & 0xFF) for i in range(4))


def fivecc_to_u32(s: str) -> int:
    out = 0
    for i, ch in enumerate(s):
        out |= ord(ch) << (8 * i)
    return out


@dataclass
class CameraFormat:
    fivecc: str
    description: str
    sizes: list[tuple[int, int]] = field(default_factory=list)
    frame_intervals_ms: list[int] = field(default_factory=list)


@dataclass
class CameraInfo:
    index: int
    path: str
    card: str
    driver: str
    bus_info: str
    device_caps: int
    formats: list[CameraFormat] = field(default_factory=list)
    readable: bool = False
    busy: bool = False
    probe_error: str = ""
    # Set when this /dev/videoN is an alternate node of an RGB sensor
    # (same card, mono-only formats).  It is NOT an IR camera — a mono
    # node on the same physical webcam is just another tune of that
    # sensor (e.g. a GREY exposure/viewport enum), so it must never be
    # treated as an IR device.
    alt_of_rgb: bool = False

    @property
    def kind(self) -> str:
        """'ir', 'rgb', or 'none' — classification used by the app."""
        if self.alt_of_rgb:
            return "none"
        mono = any(f.fivecc in MONO_FIVECC for f in self.formats)
        color = any(f.fivecc not in MONO_FIVECC for f in self.formats)
        name = self.card.lower()
        name_hint = any(h in name for h in IR_NAME_HINTS)
        if mono and not color:
            return "ir"
        if mono and color and (name_hint or "grey" in name):
            return "ir"
        if color:
            return "rgb"
        if name_hint:
            return "ir"
        return "none"

    @property
    def has_capture(self) -> bool:
        return bool(self.device_caps & V4L2_CAP_VIDEO_CAPTURE)

    @property
    def streaming(self) -> bool:
        return bool(self.device_caps & V4L2_CAP_STREAMING)

    @property
    def max_resolution(self) -> tuple[int, int]:
        best = (0, 0)
        for f in self.formats:
            for w, h in f.sizes:
                if w * h > best[0] * best[1]:
                    best = (w, h)
        return best

    @property
    def fps_estimate(self) -> float:
        """Best-effort frames-per-second figure for the test window."""
        best = 0.0
        for f in self.formats:
            for ms in f.frame_intervals_ms:
                if ms > 0:
                    best = max(best, 1000.0 / ms)
        return best


def _open(path: str) -> int:
    return os.open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC)


def _ioctl_err(fd: int, req: int, data: bytes) -> bytes | None:
    try:
        return fcntl.ioctl(fd, req, data)
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENOTTY, errno.ENODEV):
            return None
        raise


def _query_cap(fd: int) -> dict:
    buf = _ioctl_err(fd, VIDIOC_QUERYCAP, b"\x00" * 104)
    if not buf:
        return {"driver": "", "card": "", "bus_info": "", "device_caps": 0}
    driver, card, bus_info, _version, capabilities, device_caps = struct.unpack(
        "<16s32s32sIII", buf[:92]
    )
    drv = driver.split(b"\x00")[0].decode("utf-8", "replace")
    crd = card.split(b"\x00")[0].decode("utf-8", "replace")
    bi = bus_info.split(b"\x00")[0].decode("utf-8", "replace")
    return {
        "driver": drv,
        "card": crd,
        "bus_info": bi,
        "version": _version,
        "capabilities": capabilities,
        # device_caps is authoritative; fall back to capabilities when a
        # driver leaves device_caps unset on the node we hold open.
        "device_caps": device_caps or capabilities,
    }


def _enum_formats(fd: int) -> list[CameraFormat]:
    out: list[CameraFormat] = []
    idx = 0
    while idx < 48:
        full = struct.pack("<3I32s2I3I", idx, 1, 0, b"", 0, 0, 0, 0, 0)
        raw = _ioctl_err(fd, VIDIOC_ENUM_FMT, full)
        if not raw:
            break
        desc = raw[FMT_DESC : FMT_DESC + 32].split(b"\x00")[0].decode("utf-8", "replace")
        pixfmt = struct.unpack("<I", raw[FMT_PIXFMT : FMT_PIXFMT + 4])[0]
        out.append(CameraFormat(fivecc=fivecc(pixfmt), description=desc))
        idx += 1
    return out


def _enum_framesizes(fd: int, pixfmt: int, cap: int = 16) -> list[tuple[int, int]]:
    sizes: list[tuple[int, int]] = []
    idx = 0
    while idx < cap:
        raw = _ioctl_err(
            fd, VIDIOC_ENUM_FRAMESIZES, struct.pack("<3I6I2I", idx, pixfmt, 0, *([0] * 8))
        )
        if not raw:
            break
        typ = struct.unpack("<I", raw[FRS_TYPE : FRS_TYPE + 4])[0]
        w, h = struct.unpack("<II", raw[FRS_WIDTH : FRS_WIDTH + 8])
        if typ == V4L2_FRMSIZE_TYPE_DISCRETE:
            sizes.append((w, h))
        elif typ == V4L2_FRMSIZE_TYPE_STEPWISE:
            # stepwise is a continuous range; record min and max only.
            min_w, max_w = struct.unpack("<II", raw[FRS_WIDTH + 0 : FRS_WIDTH + 8])
            min_h, max_h = struct.unpack("<II", raw[FRS_WIDTH + 12 : FRS_WIDTH + 20])
            sizes.extend([(min_w, min_h), (max_w, max_h)])
        idx += 1
    return sizes


def probe(path: str) -> CameraInfo:
    """Return classification for a single /dev/videoN node."""
    index = 0
    if path.startswith("/dev/video"):
        try:
            index = int(path[len("/dev/video") :])
        except ValueError:
            pass
    info = CameraInfo(
        index=index,
        path=path,
        card="",
        driver="",
        bus_info="",
        device_caps=0,
    )
    try:
        fd = _open(path)
    except OSError as e:
        if e.errno == errno.EBUSY:
            info.busy = True
            info.probe_error = "device in use"
        elif e.errno in (errno.EACCES, errno.EPERM):
            info.probe_error = "permission denied"
        else:
            info.probe_error = e.strerror or str(e)
        return info

    try:
        cap = _query_cap(fd)
        info.card, info.driver, info.bus_info = cap["card"], cap["driver"], cap["bus_info"]
        info.device_caps = cap["device_caps"]
        if info.has_capture:
            info.formats = _enum_formats(fd)
            for f in info.formats:
                f.sizes = _enum_framesizes(fd, fivecc_to_u32(f.fivecc))
            info.readable = True
    except OSError as e:
        info.probe_error = e.strerror or str(e)
    finally:
        os.close(fd)
    return info


def _dedupe_same_sensor(cams: list[CameraInfo]) -> list[CameraInfo]:
    """Flag mono-only nodes that are really just handles of a color sensor.

    A single physical camera can expose several /dev/videoN nodes (one
    colour, plus metadata/control nodes that carry the same V4L2 card
    string). Those nodes expose no capture frame sizes, so a mono-only
    member of a card group is a harmless handle, never a camera.

    A mono-only node that DOES expose real capture frame sizes is a
    true IR / depth sensor (combo RGB+IR webcams register both sensors
    under the same UVC card name, e.g. ``Integrated_Webcam_FHD``) — it
    stays classified ``ir`` so hybrid and IR modes can use it.
    """
    groups: dict[str, list[CameraInfo]] = {}
    for c in cams:
        if c.card.strip():
            groups.setdefault(c.card.strip(), []).append(c)
    for group in groups.values():
        has_color = any(
            any(f.fivecc not in MONO_FIVECC for f in c.formats)
            for c in group
        )
        if not has_color:
            continue
        for c in group:
            mono_only = c.formats and all(f.fivecc in MONO_FIVECC for f in c.formats)
            streams_frames = mono_only and any(f.sizes for f in c.formats)
            if mono_only and not streams_frames:
                c.alt_of_rgb = True
    return cams


def discover() -> list[CameraInfo]:
    """Enumerate every /dev/videoN node in index order."""
    out: list[CameraInfo] = []
    n = 0
    missing = 0
    while n < 64:
        path = f"/dev/video{n}"
        if not os.path.exists(path):
            missing += 1
            if missing >= 4:
                break
            n += 1
            continue
        missing = 0
        out.append(probe(path))
        n += 1
    return _dedupe_same_sensor(out)


def pick(cameras: list[CameraInfo]) -> dict:
    """Best-effort camera picks for the app given a discovered set.

    Chooses the first usable RGB camera and the first usable IR camera
    (preferring larger resolutions, ignoring v4l2loopback devices which
    carry no real sensor).  Returns the values as a dict so the caller
    decides what to persist.
    """
    def _real(c: CameraInfo) -> bool:
        name = (c.card + " " + c.driver).lower()
        return not ("loopback" in name or "dummy" in name)

    rgb = [c for c in cameras if c.kind == "rgb" and c.readable and not c.busy and _real(c)]
    ir = [c for c in cameras if c.kind == "ir" and c.readable and not c.busy and _real(c)]

    def _best(group):
        if not group:
            return None
        return max(group, key=lambda c: c.max_resolution[0] * c.max_resolution[1])

    return {
        "rgb": _best(rgb),
        "ir": _best(ir),
    }


def resolve_plan(prefs: dict, cams: list[CameraInfo]) -> dict:
    """Turn user intent + a live camera list into concrete daemon values.

    Returns a dict consumed by the settings app and pushed to the
    daemon as (mode, camera, ir_camera), plus human-readable UI state:

      mode       : "rgb" | "ir" | "hybrid" | "auto"
      camera     : RGB device path or None
      ir_camera  : IR device path or None
      choosing   : which camera mode is driving this plan ("auto"|...)
      reminders  : list of short user-facing notes (empty when healthy)
    """
    wants = prefs.get("camera_mode", "auto")
    rgb_pin = prefs.get("rgb_dev") or ""
    ir_pin = prefs.get("ir_dev") or ""

    picks = pick(cams)

    def _by_path(pin: str, group):
        for c in group:
            if c.path == pin:
                return c
        return None

    rgb_cam = _by_path(rgb_pin, [c for c in cams if c.kind == "rgb"]) or picks["rgb"]
    ir_cam = _by_path(ir_pin, [c for c in cams if c.kind == "ir"]) or picks["ir"]

    reminders: list[str] = []

    # camera stays the RGB path whenever one exists: IR-mode scans use
    # ir_camera, but a later switch back to RGB never inherits an empty
    # device path.
    camera = rgb_cam.path if rgb_cam else None

    if wants == "rgb":
        mode, ir_camera = "rgb", None
        if not rgb_cam:
            reminders.append("No usable RGB camera detected.")
            camera = None
    elif wants == "ir":
        if ir_cam:
            mode, ir_camera = "ir", ir_cam.path
        else:
            mode, ir_camera = "rgb", None
            reminders.append("The IR camera was not found; using RGB instead.")
        if not rgb_cam and not ir_cam:
            camera = None
    elif wants == "hybrid":
        # Recognition runs on RGB while the IR stream feeds live
        # anti-spoof cues; the daemon treats "hybrid" as mode "both".
        mode, ir_camera = "hybrid", ir_cam.path if ir_cam else None
        if not ir_cam:
            reminders.append(
                "The IR camera could not be found — falling back to RGB only."
            )
            mode, ir_camera = "rgb", None
        elif not rgb_cam:
            reminders.append("No usable RGB camera detected.")
            camera = None
    else:  # auto — worker measures room light per scan (dark->IR, lit->RGB)
        if rgb_cam and ir_cam:
            mode, ir_camera = "auto", ir_cam.path
        elif rgb_cam:
            # RGB-only box: auto degrades to plain RGB (metering would
            # always pick RGB anyway); keeps old-daemon compat.
            mode, ir_camera = "rgb", None
        elif ir_cam:
            mode, ir_camera = "ir", ir_cam.path
        else:
            mode, ir_camera, camera = "rgb", None, None
            reminders.append("No usable face camera was found.")

    return {
        "mode": mode,
        "camera": camera,
        "ir_camera": ir_camera,
        "choosing": wants,
        "rgb_info": rgb_cam,
        "ir_info": ir_cam,
        "reminders": reminders,
        "cameras": cams,
    }


def sample_light(device: str | None, frames: int = 5) -> float | None:
    """Sample room light (mean luma 0-255) from an RGB device.

    Opens the camera briefly and releases it. Returns None when the
    device is busy, missing, or unreadable — never raises, so the UI
    can always fall back to "unknown".
    """
    if not device:
        return None
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return None
    cap = None
    try:
        try:
            idx = int(str(device).rsplit("video", 1)[-1])
            cap = cv2.VideoCapture(idx)
        except ValueError:
            cap = cv2.VideoCapture(device)
        if cap is None or not cap.isOpened():
            return None
        vals: list[float] = []
        for _ in range(max(1, frames)):
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            try:
                import numpy as np  # noqa: PLC0415
                if frame.ndim == 2:
                    vals.append(float(frame.mean()))
                else:
                    b = frame[:, :, 0].astype("float32")
                    g = frame[:, :, 1].astype("float32")
                    r = frame[:, :, 2].astype("float32")
                    vals.append(float((0.114 * b + 0.587 * g + 0.299 * r).mean()))
            except Exception:
                break
        if not vals:
            return None
        return sum(vals) / len(vals)
    except Exception:
        return None
    finally:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass


def light_label(luma: float | None, dark: float = 28.0,
                light: float = 42.0) -> str:
    """Human row text for the room-light readout."""
    if luma is None:
        return "Room light: unknown (camera busy?)"
    level = "dark" if luma < dark else ("bright" if luma > light else "dim")
    use = "IR" if luma < dark else ("RGB" if luma > light else "either (keeps last)")
    return f"Room light: {level} ({luma:.0f}) → will use {use}"


def summary(cam: CameraInfo | None) -> str:
    """Short human label for a camera, or a clear 'none found' message."""
    if cam is None:
        return "none detected"
    w, h = cam.max_resolution
    res = f"{w}x{h}" if w else "?"
    fps = f"{cam.fps_estimate:.0f}fps" if cam.fps_estimate > 0 else ""
    kind = "IR" if cam.kind == "ir" else ("RGB" if cam.kind == "rgb" else "?")
    name = cam.card.strip() or cam.path
    tail = f" · {res}" + (f" · {fps}" if fps else "")
    return f"{kind} {cam.path} · {name}{tail}"


# ---- headless auto-configuration --------------------------------------

def auto_config(camera: str | None = None,
                ir_camera: str | None = None) -> dict:
    """Decide the camera/ir_camera pair for a machine with no config yet.

    Used by the daemon on first start, so a fresh install gets a working
    camera without the user having to open the app and answer questions.
    Returns the same keys `resolve_plan` produces, so there is one
    decision function rather than two that can disagree.

    `camera` is the configured primary: if it is already set to a node
    that exists, it wins, because an explicit choice must never be
    overridden by a re-probe. `None` (or a node that has gone away) means
    "decide for me".
    """
    cams = discover()
    picks = pick(cams)
    rgb = picks.get("rgb")
    ir = picks.get("ir")

    # An explicitly configured node that is actually present wins. Only a
    # node that no longer exists is replaced, and then loudly.
    if camera and any(c.path == camera for c in cams):
        chosen_rgb = next((c for c in cams if c.path == camera), None)
    else:
        if camera:
            log.warning("configured camera %s is not present; re-selecting",
                        camera)
        chosen_rgb = rgb

    if ir_camera and any(c.path == ir_camera for c in cams):
        chosen_ir = next((c for c in cams if c.path == ir_camera), None)
    else:
        if ir_camera:
            log.warning("configured IR camera %s is not present; re-selecting",
                        ir_camera)
        chosen_ir = ir

    reminders: list[str] = []
    if chosen_rgb is None and chosen_ir is None:
        reminders.append("No usable face camera was found.")
    elif chosen_rgb is None:
        reminders.append(
            "No usable RGB camera detected; recognition will use the IR sensor.")

    if chosen_rgb is not None:
        log.info("auto-selected %s (%s, %s); IR: %s",
                 chosen_rgb.path, chosen_rgb.kind,
                 chosen_rgb.card.strip() or chosen_rgb.driver,
                 chosen_ir.path if chosen_ir else "none found")

    return {
        # `camera` stays the RGB path whenever one exists: an IR-mode scan
        # uses ir_camera, so a later switch back to RGB must not inherit
        # an empty device path.
        "camera": chosen_rgb.path if chosen_rgb else None,
        "ir_camera": chosen_ir.path if chosen_ir else None,
        "rgb_info": chosen_rgb,
        "ir_info": chosen_ir,
        "cameras": cams,
        "reminders": reminders,
    }


def config_lines(plan: dict) -> list[str]:
    """Render an `auto_config` plan as top-level TOML key lines.

    This is the contract `postinst` relies on: the returned lines are
    concatenated straight into a config file it has just created, so
    every line must be a bare `key = "value"` for a key that is not
    already present, and nothing else may ever be emitted. A device that
    was not found produces no line at all -- an invented `ir_camera` would
    point the daemon at a node that does not exist.
    """
    out: list[str] = []
    if plan.get("camera"):
        out.append(f'camera = "{plan["camera"]}"')
    if plan.get("ir_camera"):
        out.append(f'ir_camera = "{plan["ir_camera"]}"')
    return out


if __name__ == "__main__":
    import sys

    # `--config-lines` is how postinst gets the auto-detected cameras into
    # a freshly created config without hand-parsing TOML in shell.
    if len(sys.argv) > 1 and sys.argv[1] == "--config-lines":
        try:
            plan = auto_config()
        except Exception as e:                      # never fail an install
            log.warning("camera auto-detection failed: %s", e)
            plan = {}
        for line in config_lines(plan):
            print(line)
        for r in plan.get("reminders", []):
            print(f"# {r}", file=sys.stderr)
        raise SystemExit(0)

    targets = sys.argv[1:] or None
    cams = discover()
    for c in cams:
        if targets and c.path not in targets:
            continue
        flag = "BUSY" if c.busy else ("OK" if c.readable else "NOACC")
        kinds = {f.fivecc.strip(): f.description for f in c.formats}
        res = c.max_resolution
        print(
            f"{c.path} [{flag}] kind={c.kind!r} caps={c.device_caps:#x} "
            f"card={c.card!r} driver={c.driver!r} bus={c.bus_info!r} "
            f"max={res[0]}x{res[1]} fmts={list(kinds)}"
        )