"""Vision worker entry point.

Runs unprivileged as the `faceid` user (member of `video`). Listens on
a Unix socket, answers one request at a time, owns the camera and
nothing else.

  python -m faceid_vision --socket /run/faceid-nim/vision.sock
  python -m faceid_vision --oneshot --source clip.mp4     # CI / debugging
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from .camera import is_greyscale_only, open_source
from .fuse import Strictness
from .protocol import (ProtocolError, decode, encode, ev_done, ev_error,
                       validate_request)
from .scan import ScanConfig, ScanEngine, build_engine

log = logging.getLogger("faceid.vision")


def _classify_mode(device: str) -> str:
    """Default stream classification for a mode-less (CLI) request.

    A V4L2 node that advertises only monochrome formats is treated as
    an IR stream so the RGB-only screen cues are gated off; anything
    else (or a node we cannot probe) stays rgb.
    """
    return "ir" if is_greyscale_only(device) else "rgb"


def _open_with_retry(device: str, attempts: int = 2):
    """Open the camera with one retry after a short settle.

    Hibernate/wake and v4l2 loopback races leave some drivers half-open:
    the device opens yet immediately errors. One 150ms retry clears most
    of those without ever slowing the happy path."""
    import contextlib

    @contextlib.contextmanager
    def _open(device, attempts):
        last: BaseException | None = None
        for attempt in range(max(1, attempts)):
            try:
                cam = open_source(device).__enter__()
            except Exception as e:
                last = e
                if attempt == attempts - 1:
                    raise
                time.sleep(0.15)
                continue
            try:
                yield cam
            finally:
                cam.__exit__(None, None, None)
            return
        raise last if last is not None else AssertionError("unreachable")

    return _open(device, attempts)


# Last auto decision in this worker process, for hysteresis: an
# in-band reading keeps the previous spectrum instead of flickering.
_LAST_AUTO = "rgb"


def _sample_luma(device: str, frames: int = 5) -> float | None:
    """Open the RGB device briefly and return mean luma, or None."""
    try:
        from .meter import mean_luma_bgr  # noqa: PLC0415
        with _open_with_retry(device) as cam:
            acc: list[float] = []
            try:
                for _ts, frame in cam.frames(0.6):
                    acc.append(mean_luma_bgr(frame))
                    if len(acc) >= frames:
                        break
            except Exception:
                pass
            if not acc:
                return None
            return sum(acc) / len(acc)
    except Exception:
        return None


def _handle_scan(engine: ScanEngine, msg: dict, args, send) -> dict:
    sid = msg.get("id", "s0")
    global _LAST_AUTO

    # Resolve the active mode. The daemon always sends an explicit
    # "mode"; only a mode-less request (CLI oneshot) needs to classify
    # the stream. A V4L2 node that advertises only monochrome formats
    # is an IR/IR-like stream, where the RGB screen cues would
    # false-positive on sensor noise (see ScanConfig.mode).
    # "auto" measures room light on the RGB device per scan and resolves
    # to "ir" (dark) or "rgb" (lit) before anything else runs.
    msg_mode = msg.get("mode")
    if msg_mode in ("rgb", "ir", "both", "hybrid", "auto"):
        mode = msg_mode
    else:
        cli_mode = getattr(args, "mode", "auto")
        mode = cli_mode if cli_mode != "auto" else _classify_mode(
            args.source if args.source else args.device)

    req_device = msg.get("device") or None
    cli_primary = args.source if args.source else args.device
    rgb_device = req_device or cli_primary
    ir_dev_field = msg.get("ir_device") or getattr(args, "ir_device", None)

    auto_luma: float | None = None
    if mode == "auto":
        from .meter import AUTO_DARK_LUMA, AUTO_LIGHT_LUMA, pick_spectrum  # noqa: PLC0415
        dark_thr = float(getattr(args, "auto_dark_luma", AUTO_DARK_LUMA))
        light_thr = float(getattr(args, "auto_light_luma", AUTO_LIGHT_LUMA))
        auto_luma = _sample_luma(rgb_device)
        if auto_luma is None:
            # RGB unreadable (busy/missing): prefer IR when hardware
            # exists, else fall back to RGB and let the scan report it.
            mode = "ir" if ir_dev_field else "rgb"
            log.info("auto: RGB metering failed, falling back to %s", mode)
        else:
            picked, _ = pick_spectrum(auto_luma, _LAST_AUTO,
                                      dark=dark_thr, light=light_thr)
            # Dark is only useful when an IR sensor exists; otherwise a
            # dark room still scans (poorly) on RGB rather than erroring.
            if picked == "ir" and not ir_dev_field:
                picked = "rgb"
            mode = picked
            _LAST_AUTO = picked
            log.info("auto: luma=%.1f -> %s", auto_luma, mode)

    # The IR camera is secondary analysis hardware. It is opened only
    # for modes that ask for IR cues. An "rgb"-mode scan never opens it,
    # even when the request carries an ir_device field: camera mode is an
    # explicit configuration choice and must win over whatever sensors
    # happen to be attached -- never be inferred from the negotiated
    # frame's channel count.
    if mode in ("ir", "both", "hybrid"):
        ir_device = ir_dev_field
    else:
        ir_device = None
    if msg_mode not in ("rgb", "ir", "both", "hybrid", "auto"):
        log.debug("mode-less request: classified stream as %s", mode)

    cfg = None
    try:
        cfg = ScanConfig(
            timeout_ms=int(msg.get("timeout_ms", 4000)),
            strictness=Strictness(msg.get("strict", "light")),
            use_challenge=bool(msg.get("challenge", False)),
            mode=mode,
            # Set only by an enrollment request. See the ENROLL PREVIEW
            # INTEGRATION HOOK in scan.py.
            preview=bool(msg.get("preview", False)) or msg.get("op") == "enroll",
        )
    except (ValueError, TypeError) as e:
        return ev_error(sid, f"bad_request: {e}")

    # When mode is "ir", use IR camera as primary; otherwise use RGB camera.
    # The daemon sends the resolved primary in "device" (from
    # /etc/faceid-nim/config.toml camera). Fall back to the CLI default
    # only for old daemons / manual oneshot runs. (req_device/cli_primary
    # already resolved above for the auto light meter; reuse them.)
    if mode == "ir" and ir_device:
        primary_device = ir_device
        secondary_device = rgb_device
    else:
        primary_device = rgb_device
        secondary_device = ir_device

    try:
        with _open_with_retry(primary_device) as cam:
            secondary_cam = None
            if secondary_device:
                try:
                    secondary_cam = open_source(secondary_device).__enter__()
                except Exception:
                    secondary_cam = None
            try:
                # For IR mode, pass RGB camera as ir_camera for supplementary checks
                # For RGB mode, pass IR camera as ir_camera for supplementary checks
                res = engine.scan(cam, cfg,
                                  emit=lambda e: send({"id": sid, **e}),
                                  ir_camera=secondary_cam)
            finally:
                if secondary_cam is not None:
                    secondary_cam.__exit__(None, None, None)
    except Exception as e:
        return ev_error(sid, f"camera_unavailable: {type(e).__name__}")

    if res.error:
        return ev_error(sid, res.error)
    # Recognition spectrum is the resolved primary: "ir" when the IR
    # sensor was primary, else "rgb". ("both"/"hybrid" stay RGB-primary.)
    spectrum = "ir" if mode == "ir" else "rgb"
    return ev_done(sid, res.embeddings, res.model_id, res.liveness.to_json(),
                   res.frames, res.usable, res.elapsed_ms,
                   spectrum=spectrum, luma=auto_luma)


def _accel_info(embedder) -> dict:
    """What the recogniser is really running on.

    `providers` is read back from the live session rather than from what
    was requested: onnxruntime downgrades to CPU with only a log line
    when a provider's driver is unusable, so the request is not evidence.
    """
    from . import hardware

    try:
        live = list(embedder.providers)
    except Exception:
        live = []
    try:
        prof = hardware.profile()
    except Exception:
        prof = None
    out = {
        "providers": live or [hardware.CPU_EXECUTION_PROVIDER],
        "cpu_count": prof.cpu_count if prof else 0,
        "threads": prof.intra_op_threads if prof else 0,
        "features": list(prof.cpu_features) if prof else [],
        "arch": prof.arch if prof else "",
    }
    if prof and prof.unverified:
        out["unverified"] = True
    return out


def _serve(engine: ScanEngine, args) -> int:
    path = Path(args.socket)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    os.chmod(path, 0o660)          # daemon and worker share a group; nobody else
    srv.listen(4)
    log.info("listening on %s", path)

    running = True

    def stop(*_a):
        nonlocal running
        running = False
        srv.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        with conn:
            _session(engine, conn, args)
    if path.exists():
        path.unlink()
    return 0


def _session(engine: ScanEngine, conn: socket.socket, args) -> None:
    def send(obj: dict) -> None:
        try:
            conn.sendall(encode(obj))
        except OSError:
            pass

    buf = b""
    while True:
        try:
            chunk = conn.recv(65536)
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        if len(buf) > (1 << 20):
            send(ev_error("?", "bad_request: line too long"))
            return
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = validate_request(decode(line))
            except ProtocolError as e:
                send(ev_error("?", f"bad_request: {e}"))
                continue
            op = msg["op"]
            if op == "ping":
                send({"id": msg.get("id", ""), "ev": "pong"})
            elif op == "capabilities":
                # Reports what is *actually* running, plus why anything
                # optional is not. Every field here is a claim the app and
                # `faceid-nim status` are allowed to show the user, so an
                # unproven claim here is a lie in the UI. Nothing may
                # report a feature as working unless it is.
                mesh = engine.mesh
                anti = engine.antispoof
                emb = engine.embedder
                send({
                    "id": msg.get("id", ""), "ev": "capabilities",
                    "mesh": bool(getattr(mesh, "available", False)),
                    "landmarks": getattr(mesh, "backend", "none"),
                    "landmarks_reason": getattr(mesh, "reason", ""),
                    "antispoof": bool(getattr(anti, "available", False)),
                    "antispoof_reason": getattr(anti, "reason", ""),
                    "model_id": emb.model_id,
                    "embedding_dim": emb.dim,
                    "ir": bool(args.ir_device),
                    "accel": _accel_info(emb),
                })
            elif op in ("scan", "enroll"):
                send(_handle_scan(engine, msg, args, send))
            elif op == "cancel":
                send({"id": msg.get("id", ""), "ev": "cancelled"})


def main(argv: list[str] | None = None) -> int:
    # onnxruntime logs a screenful of "Initializer X appears in graph
    # inputs" warnings to stderr once per model load. They are noise for
    # an operator tailing the journal; thread them at ERROR level.
    try:
        import onnxruntime as _ort  # noqa: PLC0415
        _ort.set_default_logger_severity(3)
    except Exception:
        pass

    p = argparse.ArgumentParser(prog="faceid-vision")
    p.add_argument("--socket", default="/run/faceid-nim/worker/vision.sock")
    p.add_argument("--device", default="/dev/video0")
    p.add_argument("--ir-device", default=None)
    p.add_argument("--source", default=None,
                   help="video file instead of a camera (CI replay)")
    p.add_argument("--mode", default="auto", choices=["auto", "rgb", "ir", "both", "hybrid"],
                   help="camera mode for liveness-cue selection "
                        "(auto: monochrome-only nodes are treated as IR)")
    p.add_argument("--model-dir", default=None)
    p.add_argument("--manifest", default=None)
    p.add_argument("--no-verify", action="store_true",
                   help="skip model checksum verification (development only)")
    p.add_argument("--oneshot", action="store_true",
                   help="run a single scan, print JSON, exit")
    p.add_argument("--strict", default="light", choices=[s.value for s in Strictness])
    p.add_argument("--timeout-ms", type=int, default=4000)
    p.add_argument("--auto-dark-luma", type=float, default=28.0,
                   help="below this mean luma, auto mode uses IR")
    p.add_argument("--auto-light-luma", type=float, default=42.0,
                   help="above this mean luma, auto mode uses RGB")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.no_verify:
        log.warning("model checksum verification DISABLED -- development only")

    try:
        engine = build_engine(
            model_dir=Path(args.model_dir) if args.model_dir else None,
            manifest=Path(args.manifest) if args.manifest else None,
            verify=not args.no_verify)
    except Exception as e:
        log.error("cannot load models: %s", e)
        return 2

    if args.oneshot:
        out = _handle_scan(engine,
                           {"id": "one", "op": "scan", "strict": args.strict,
                            "timeout_ms": args.timeout_ms},
                           args, send=lambda _e: None)
        if args.verbose and out.get("ev") == "done":
            lv = out.get("liveness", {})
            notes = lv.get("notes", {})
            stats = out.get("stats", {})
            print("\n=== face sample metrics ===", file=sys.stderr)
            print(f"  moire_score   : {notes.get('moire', 'n/a')} "
                  f"(threshold {notes.get('moire_threshold', 'n/a')})",
                  file=sys.stderr)
            print(f"  moire_window  : {notes.get('moire_window', 'n/a')}",
                  file=sys.stderr)
            print(f"  deny_cues     : {lv.get('deny')}", file=sys.stderr)
            print(f"  confirm       : {lv.get('confirm')}", file=sys.stderr)
            print(f"  frames/usable : {stats.get('frames')}/{stats.get('usable')}",
                  file=sys.stderr)
            print(f"  elapsed_ms    : {stats.get('elapsed_ms')}", file=sys.stderr)
        json.dump(out, sys.stdout, indent=2)
        print()
        return 0 if out.get("ev") == "done" else 1

    return _serve(engine, args)


if __name__ == "__main__":
    raise SystemExit(main())
