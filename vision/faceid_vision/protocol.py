"""JSON-lines protocol between the daemon and the vision worker.

Frames never cross this socket. The worker sends query embeddings and
liveness cues; the daemon owns the templates and makes the decision.

  daemon -> worker  {"op":"scan","id":"s1","mode":"rgb","timeout_ms":4000,"strict":"light"}
  worker -> daemon  {"id":"s1","ev":"face_found"}
  worker -> daemon  {"id":"s1","ev":"progress","p":0.6}
  worker -> daemon  {"id":"s1","ev":"challenge","prompt":"Turn your head left"}
  worker -> daemon  {"id":"s1","ev":"done","embeddings":[[...]],
                     "model_id":"sface_v1",
                     "liveness":{"deny":[],"confirm":["blink"],"score":0.85}}
  worker -> daemon  {"id":"s1","ev":"error","reason":"camera_unavailable"}
"""
from __future__ import annotations

import json
from typing import Any, Iterator


class ProtocolError(ValueError):
    pass


OPS = {"scan", "enroll", "capabilities", "ping", "cancel"}


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="strict")
    line = line.strip()
    if not line:
        raise ProtocolError("empty line")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"malformed json: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError("top-level value must be an object")
    return msg


def validate_request(msg: dict[str, Any]) -> dict[str, Any]:
    op = msg.get("op")
    if op not in OPS:
        raise ProtocolError(f"unknown op {op!r}")
    if not isinstance(msg.get("id", ""), str):
        raise ProtocolError("id must be a string")
    if op in {"scan", "enroll"}:
        t = msg.get("timeout_ms", 4000)
        if not isinstance(t, int) or not (200 <= t <= 60000):
            raise ProtocolError("timeout_ms out of range")
    return msg


def read_lines(sock) -> Iterator[dict[str, Any]]:
    """Yield decoded messages from a connected socket."""
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        buf += chunk
        if len(buf) > 1 << 20:
            raise ProtocolError("line too long")
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                yield decode(line)


# Event constructors -- keeps spelling consistent across the codebase.
def ev_face_found(sid: str) -> dict:           return {"id": sid, "ev": "face_found"}
def ev_progress(sid: str, p: float) -> dict:   return {"id": sid, "ev": "progress", "p": round(float(p), 3)}
def ev_challenge(sid: str, prompt: str) -> dict:return {"id": sid, "ev": "challenge", "prompt": prompt}
def ev_error(sid: str, reason: str) -> dict:   return {"id": sid, "ev": "error", "reason": reason}

def ev_done(sid: str, embeddings, model_id: str, liveness: dict,
            frames: int, usable: int, elapsed_ms: int) -> dict:
    return {
        "id": sid, "ev": "done",
        "embeddings": [[round(float(x), 6) for x in e] for e in embeddings],
        "model_id": model_id,
        "liveness": liveness,
        "stats": {"frames": frames, "usable": usable, "elapsed_ms": elapsed_ms},
    }
