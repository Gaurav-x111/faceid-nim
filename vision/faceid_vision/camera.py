"""Camera sources: V4L2 via OpenCV, plus a file source for CI replay."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Iterator

import cv2
import numpy as np

log = logging.getLogger("faceid.vision.camera")


@dataclass
class CameraConfig:
    device: str = "/dev/video0"       # or an int index, or a path to a video file
    width: int = 640
    height: int = 480
    fps: int = 30
    warmup_frames: int = 3            # reduced warmup for faster start
    fourcc: str | None = "MJPG"
    is_file: bool = False


class CameraError(RuntimeError):
    pass


@dataclass(frozen=True)
class CameraFormat:
    """One pixel format + size combination a device advertises."""
    fourcc: str | None              # "GREY", "MJPG", ... None when unknown
    width: int | None = None
    height: int | None = None
    fps: tuple[int, ...] = ()       # discrete integer framerates, may be empty

    def __str__(self) -> str:
        fps = f"@{self.fps[0]}" if self.fps else ""
        return f"{self.fourcc or 'fourcc-any'} {self.width}x{self.height}{fps}"


def _pad4(code: str) -> str:
    return (code or "    ").ljust(4)[:4]


def _same_fourcc(a: str | None, b: str | None) -> bool:
    na, nb = _pad4(a).rstrip().upper(), _pad4(b).rstrip().upper()
    return na == nb and bool(na)


def _run_v4l2(device: str, *args: str) -> list[str] | None:
    """Run v4l2-ctl and return its output lines, or None if the tool is
    missing or the device is not a readable V4L2 node."""
    if shutil.which("v4l2-ctl") is None:
        return None
    try:
        proc = subprocess.run(
            ["v4l2-ctl", "-d", device, *args],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return out.splitlines()


_FOURCC_RE = re.compile(r"^\s*\[\d+\]:\s*'([^']{4})'")
_SIZE_RE = re.compile(r"^\s*Size:\s+Discrete\s+(\d+)x(\d+)\s*$")
_INTERVAL_RE = re.compile(r"^\s*Interval:\s+Discrete\s+\S+\s+\((\d+(?:\.\d+)?)\s+fps\)")


def probe_formats(device: str) -> list[CameraFormat]:
    """Discover the formats the device actually advertises.

    Parses `v4l2-ctl --list-formats-ext`. Returns an empty list when
    the device is not a V4L2 node, the tool is missing, or the driver
    only reports stepwise/continuous sizes that cannot be matched
    exactly. Never raises.
    """
    if not str(device).startswith("/dev/"):
        return []
    lines = _run_v4l2(device, "--list-formats-ext")
    if not lines:
        return []

    formats: list[CameraFormat] = []
    cur: CameraFormat | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None and cur.width and cur.height:
            formats.append(cur)
        cur = None

    for line in lines:
        m = _FOURCC_RE.match(line)
        if m:
            flush()
            cur = CameraFormat(fourcc=m.group(1))
            continue
        if cur is None:
            continue
        m = _SIZE_RE.match(line)
        if m:
            fourcc = cur.fourcc
            flush()
            cur = CameraFormat(fourcc=fourcc,
                               width=int(m.group(1)), height=int(m.group(2)))
            continue
        m = _INTERVAL_RE.match(line)
        if m and cur.width and cur.height:
            fps = round(float(m.group(1)))
            if fps not in cur.fps:
                cur = CameraFormat(
                    fourcc=cur.fourcc, width=cur.width, height=cur.height,
                    fps=cur.fps + (fps,))
    flush()
    return formats


def current_fourcc(device: str) -> str | None:
    """The pixel format the driver reports *right now*, from
    `v4l2-ctl --get-fmt-video`. CAP_PROP_FOURCC reads 0/NULL on UVC
    builds, so this is the reliable ground truth for verification."""
    for line in _run_v4l2(device, "--get-fmt-video") or []:
        m = re.search(r"Pixel Format\s*:\s*'([^']{4})'", line)
        if m:
            return m.group(1)
    return None


# V4L2 monochrome pixel formats. Used to classify a GREY-only capture
# node as an IR/monochrome stream so RGB-specific liveness cues are
# gated off (their thresholds are tuned for colour webcam texture).
_MONO_FOURCC = {c.rstrip().upper() for c in (
    "GREY", "Y800", "Y8  ", "Y10 ", "Y12 ", "Y14 ", "Y16 ", "Y04 ")}


def is_greyscale_only(device: str) -> bool:
    """True when the device advertises formats and every one of them is
    monochrome. A device we cannot probe, or that announces any colour
    format, is not classified as greyscale-only. Never raises."""
    fmts = probe_formats(device)
    return bool(fmts) and all(
        f.fourcc and _pad4(f.fourcc).rstrip().upper() in _MONO_FOURCC
        for f in fmts)


def pick_format(cfg: CameraConfig, formats: list[CameraFormat]) -> CameraFormat | None:
    """Only ever ask the driver for a combination it advertises.

    Exact (fourcc, width, height, fps) match wins; otherwise the
    advertised combo closest to the request by format, dimensions and
    framerate. Returns None when nothing usable is advertised, which
    means the caller should leave the driver's current settings alone.
    """
    req_fps = cfg.fps
    for f in formats:
        if f.width == cfg.width and f.height == cfg.height and \
                f.fourcc and _same_fourcc(f.fourcc, cfg.fourcc) and \
                (cfg.fps in f.fps or not f.fps):
            return f
    best: tuple | None = None
    best_fmt = None
    for f in formats:
        if not f.width or not f.height:
            continue
        exact_fourcc = f.fourcc and _same_fourcc(f.fourcc, cfg.fourcc)
        fps_dist = min((abs(p - req_fps) for p in f.fps), default=0)
        key = (0 if exact_fourcc else 1,
               abs(f.width - cfg.width) + abs(f.height - cfg.height),
               abs(f.width * f.height - cfg.width * cfg.height),
               fps_dist)
        if best is None or key < best:
            best, best_fmt = key, f
    return best_fmt


class Camera:
    """Opens on enter, closes on exit.

    The camera stays shut until a scan is triggered so the privacy LED
    means something.
    """

    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.cap: cv2.VideoCapture | None = None
        self._asked: CameraFormat | None = None   # what we requested, for verify

    def __enter__(self) -> "Camera":
        src: str | int = self.cfg.device
        if not self.cfg.is_file:
            try:
                src = int(str(self.cfg.device).rsplit("video", 1)[-1])
            except ValueError:
                src = self.cfg.device
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"cannot open camera source {self.cfg.device!r}")
        if not self.cfg.is_file:
            self._negotiate(cap)
            good = 0
            sample = None
            for _ in range(self.cfg.warmup_frames):
                ok, sample = cap.read()
                if ok:
                    good += 1
            if good == 0:
                # A camera that opens but yields no frames is broken,
                # not "timed out". Say so loudly instead of letting the
                # caller burn a full scan window on silence.
                cap.release()
                raise CameraError(
                    f"camera {self.cfg.device!r} opened but produced no "
                    "frames")
            self._verify(cap, sample)
        self.cap = cap
        return self

    # ---- negotiation + verification -----------------------------------
    def _negotiate(self, cap: cv2.VideoCapture) -> None:
        """Request only what the device advertises; otherwise leave the
        driver's current settings alone instead of guessing."""
        advertised = probe_formats(self.cfg.device)
        target = pick_format(self.cfg, advertised) if advertised else None
        if target is None:
            # Rather than blind-setting values and hoping, fall back to
            # whatever the driver already reports as current.
            log.debug("camera %s: no advertised format to request; "
                      "leaving driver defaults", self.cfg.device)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return

        self._asked = target
        log.info("camera %s: requesting advertised %s (requested %s)",
                 self.cfg.device, target, _requested_label(self.cfg))
        if target.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC,
                    cv2.VideoWriter_fourcc(*_pad4(target.fourcc)))
        if target.width and target.height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, target.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, target.height)
        if target.fps:
            cap.set(cv2.CAP_PROP_FPS,
                    min(target.fps, key=lambda f: abs(f - self.cfg.fps)))
        # Keep the V4L2 queue small. On some OpenCV 5 / UVC builds
        # omitting this lets libopencv reconfigure the stream and
        # die with "ioctl(VIDIOC_QBUF): Bad file descriptor" on the
        # first read; with it the stream survives.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _verify(self, cap: cv2.VideoCapture, sample: np.ndarray | None) -> None:
        """Check what the driver actually negotiated and log any change
        loudly, so a silent resolution/format mismatch never hides as
        "no_usable_face" several files downstream."""
        actual_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        # CAP_PROP_FOURCC reads 0 on UVC builds; trust the driver's
        # V4L2 format instead, logged raw at debug for the curious.
        v4l_fourcc = current_fourcc(self.cfg.device)
        log.debug("camera %s: CAP_PROP_FOURCC reported %r",
                  self.cfg.device, int(cap.get(cv2.CAP_PROP_FOURCC)))

        channels = 3 if sample is not None and sample.ndim == 3 else 1
        chan_note = (f" frames arrive as {channels}-channel "
                     f"({'BGR' if channels == 3 else 'grayscale'})") \
            if sample is not None else ""
        log.info(
            "negotiated %s = %s %.0fx%.0f @%.0ffps (requested %s)%s",
            self.cfg.device, v4l_fourcc or "fourcc-unknown",
            actual_w, actual_h, actual_fps, _requested_label(self.cfg),
            chan_note)

        asked = self._asked
        if asked is None:
            return
        refused = (
            (asked.width is not None and actual_w != asked.width) or
            (asked.height is not None and actual_h != asked.height) or
            (asked.fps and round(actual_fps) not in asked.fps) or
            (asked.fourcc and v4l_fourcc and
             not _same_fourcc(v4l_fourcc, asked.fourcc)))
        if refused:
            log.warning(
                "camera %s refused the requested advertised format %s; "
                "driver negotiated %s %.0fx%.0f @%.0ffps",
                self.cfg.device, asked, v4l_fourcc or "fourcc-unknown",
                actual_w, actual_h, actual_fps)

    def __exit__(self, *exc) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def frames(self, timeout_s: float) -> Iterator[tuple[float, np.ndarray]]:
        """Yield (monotonic_timestamp, frame) until timeout or EOF.

        Frames are what the driver hands over: 3-channel BGR on UVC
        builds, possibly single-channel GREY on others. Every consumer
        in this package guards on ``frame.ndim`` before any cvtColor.
        """
        if self.cap is None:
            raise CameraError("camera not open")
        yielded = 0
        t0 = time.monotonic()
        while True:
            now = time.monotonic()
            if now - t0 > timeout_s:
                return
            ok, frame = self.cap.read()
            if not ok:
                if yielded == 0:
                    raise CameraError(
                        f"camera {self.cfg.device!r} reported VIDIOC "
                        "failure on the first read")
                # The stream died mid-scan: surface it as a camera
                # problem, never as a quiet timeout.
                raise CameraError(
                    f"camera {self.cfg.device!r} stream stopped after "
                    f"{yielded} frames")
            yielded += 1
            yield now, frame


def open_source(device: str, **kw) -> Camera:
    is_file = not str(device).startswith("/dev/") and not str(device).isdigit()
    return Camera(CameraConfig(device=device, is_file=is_file, **kw))


def _requested_label(cfg: CameraConfig) -> str:
    return f"{cfg.fourcc or 'fourcc-any'} {cfg.width}x{cfg.height} @{cfg.fps}fps"