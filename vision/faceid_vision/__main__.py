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


def _handle_scan(engine: ScanEngine, msg: dict, args, send) -> dict:
    sid = msg.get("id", "s0")

    # Resolve the active mode. The daemon always sends an explicit
    # "mode"; only a mode-less request (CLI oneshot) needs to classify
    # the stream. A V4L2 node that advertises only monochrome formats
    # is an IR/IR-like stream, where the RGB screen cues would
    # false-positive on sensor noise (see ScanConfig.mode).
    msg_mode = msg.get("mode")
    if msg_mode in ("rgb", "ir", "both", "hybrid"):
        mode = msg_mode
    else:
        cli_mode = getattr(args, "mode", "auto")
        mode = cli_mode if cli_mode != "auto" else _classify_mode(
            args.source if args.source else args.device)

    # The IR camera is secondary analysis hardware. It is opened only
    # for modes that ask for IR cues. An "rgb"-mode scan never opens it,
    # even when the request carries an ir_device field: camera mode is an
    # explicit configuration choice and must win over whatever sensors
    # happen to be attached -- never be inferred from the negotiated
    # frame's channel count.
    if mode in ("ir", "both", "hybrid"):
        ir_device = msg.get("ir_device") or args.ir_device
    else:
        ir_device = None
    if msg_mode not in ("rgb", "ir", "both", "hybrid"):
        log.debug("mode-less request: classified stream as %s", mode)

    cfg = ScanConfig(
        timeout_ms=int(msg.get("timeout_ms", 4000)),
        strictness=Strictness(msg.get("strict", "light")),
        use_challenge=bool(msg.get("challenge", False)),
        mode=mode,
        # Set only by an enrollment request. See the ENROLL PREVIEW
        # INTEGRATION HOOK in scan.py.
        preview=bool(msg.get("preview", False)) or msg.get("op") == "enroll",
    )

    # When mode is "ir", use IR camera as primary; otherwise use RGB camera
    if mode == "ir" and ir_device:
        primary_device = ir_device
        secondary_device = args.source if args.source else args.device
    else:
        primary_device = args.source if args.source else args.device
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
    return ev_done(sid, res.embeddings, res.model_id, res.liveness.to_json(),
                   res.frames, res.usable, res.elapsed_ms)


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
                send({
                    "id": msg.get("id", ""), "ev": "capabilities",
                    "mesh": engine.mesh.available,
                    "antispoof": engine.antispoof.available,
                    "model_id": engine.embedder.model_id,
                    "embedding_dim": engine.embedder.dim,
                    "ir": bool(args.ir_device),
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
    p.add_argument("--socket", default="/run/faceid-nim/vision.sock")
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
