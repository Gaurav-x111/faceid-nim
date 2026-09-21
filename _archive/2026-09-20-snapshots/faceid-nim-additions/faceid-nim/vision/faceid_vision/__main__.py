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
from pathlib import Path

from .camera import open_source
from .fuse import Strictness
from .protocol import (ProtocolError, decode, encode, ev_done, ev_error,
                       validate_request)
from .scan import ScanConfig, ScanEngine, build_engine

log = logging.getLogger("faceid.vision")


def _handle_scan(engine: ScanEngine, msg: dict, args, send) -> dict:
    sid = msg.get("id", "s0")
    cfg = ScanConfig(
        timeout_ms=int(msg.get("timeout_ms", 4000)),
        strictness=Strictness(msg.get("strict", "light")),
        use_challenge=bool(msg.get("challenge", False)),
        # Set only by an enrollment request. See the ENROLL PREVIEW
        # INTEGRATION HOOK in scan.py.
        preview=bool(msg.get("preview", False)) or msg.get("op") == "enroll",
    )
    mode = msg.get("mode", "rgb")
    device = args.source if args.source else args.device
    ir_device = args.ir_device if mode in ("ir", "both") else None

    try:
        with open_source(device) as cam:
            ir_cam = None
            if ir_device:
                try:
                    ir_cam = open_source(ir_device).__enter__()
                except Exception:
                    ir_cam = None
            try:
                res = engine.scan(cam, cfg,
                                  emit=lambda e: send({"id": sid, **e}),
                                  ir_camera=ir_cam)
            finally:
                if ir_cam is not None:
                    ir_cam.__exit__(None, None, None)
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
    p = argparse.ArgumentParser(prog="faceid-vision")
    p.add_argument("--socket", default="/run/faceid-nim/vision.sock")
    p.add_argument("--device", default="/dev/video0")
    p.add_argument("--ir-device", default=None)
    p.add_argument("--source", default=None,
                   help="video file instead of a camera (CI replay)")
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
        json.dump(out, sys.stdout, indent=2)
        print()
        return 0 if out.get("ev") == "done" else 1

    return _serve(engine, args)


if __name__ == "__main__":
    raise SystemExit(main())
