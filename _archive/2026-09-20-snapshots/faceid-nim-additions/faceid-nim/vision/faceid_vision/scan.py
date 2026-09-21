"""Scan session: one trigger, one camera open, one verdict's worth of
evidence.

This module produces *evidence*, not decisions. It returns query
embeddings plus a LivenessState. The daemon compares against templates
and decides. Keeping it that way is what lets the worker run
unprivileged with no access to biometric storage.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from . import enrollpreview as _ep
from .align import align
from .camera import Camera, CameraError, open_source
from .detect import Detector, Face
from .embed import Embedder
from .fuse import LivenessState, Strictness
from .landmarks import FaceMesh
from .liveness.blink import BlinkDetector
from .liveness.challenge import ChallengeRunner
from .liveness.geometry import PlanarityTracker
from .liveness.ir import analyse_ir_face
from .liveness.passive import PassiveAntiSpoof
from .liveness.screen import screen_report
from .quality import QualityConfig, assess

Emit = Callable[[dict], None]


@dataclass
class ScanConfig:
    timeout_ms: int = 4000
    strictness: Strictness = Strictness.LIGHT
    max_embeddings: int = 8           # enough for voting; no need to ship more
    min_embeddings: int = 3
    embed_every: int = 1              # embed every Nth usable frame
    quality: QualityConfig = field(default_factory=QualityConfig)
    use_challenge: bool = False
    require_attention: bool = True
    # Enrollment preview. Off for every unlock scan: a preview frame
    # must only ever exist for a session the enrolling app owns.
    preview: bool = False


@dataclass
class ScanResult:
    embeddings: list[np.ndarray] = field(default_factory=list)
    liveness: LivenessState = field(default_factory=LivenessState)
    model_id: str = ""
    frames: int = 0
    usable: int = 0
    elapsed_ms: int = 0
    error: str | None = None


class ScanEngine:
    """Holds the loaded models. Create once, reuse for every scan --
    a cold ONNX session is most of the latency budget."""

    def __init__(self, detector: Detector, embedder: Embedder,
                 antispoof: PassiveAntiSpoof | None = None,
                 mesh: FaceMesh | None = None):
        self.detector = detector
        self.embedder = embedder
        self.antispoof = antispoof or PassiveAntiSpoof(None)
        self.mesh = mesh if mesh is not None else FaceMesh()
        self.preview_cfg = _ep.PreviewConfig()

    # ---- the main loop -------------------------------------------------
    def scan(self, camera: Camera, cfg: ScanConfig,
             emit: Emit | None = None,
             ir_camera: Camera | None = None) -> ScanResult:
        emit = emit or (lambda _m: None)
        res = ScanResult(model_id=self.embedder.model_id)
        state = res.liveness

        blink = BlinkDetector()
        planar = PlanarityTracker()
        challenge = ChallengeRunner() if cfg.use_challenge else None
        challenge_started = False

        t0 = time.monotonic()
        announced_face = False
        usable_seen = 0
        _preview_state = None       # lazily created by the preview hook

        try:
            for _ts, frame in camera.frames(cfg.timeout_ms / 1000.0):
                res.frames += 1
                face = self.detector.largest(frame)

                # --- ENROLL PREVIEW INTEGRATION HOOK ------------------
                # The only place a frame leaves this process. Guarded by
                # cfg.preview, which the worker sets solely from an
                # enrollment request; a normal unlock scan never enters
                # this block. Emitted events are {"ev":"preview","jpeg":
                # <base64>} and {"ev":"pose_status","status":...}, both
                # small enough to stay under the socket's 64 KB recv.
                if cfg.preview:
                    if _preview_state is None:
                        _preview_state = _ep.PreviewState()
                    _pq = _ep.assess_for_preview(frame, face, cfg.quality)
                    for _ev in _ep.preview_events(frame, face, _pq,
                                                  _preview_state,
                                                  self.preview_cfg):
                        emit(_ev)
                # --- END ENROLL PREVIEW INTEGRATION HOOK --------------

                if face is None:
                    continue
                if not announced_face:
                    announced_face = True
                    emit({"ev": "face_found"})

                q = assess(frame, face, cfg.quality)
                if not q.ok:
                    state.notes["last_quality"] = q.reason
                    continue
                usable_seen += 1
                res.usable = usable_seen

                self._collect_deny_cues(frame, face, state, ir_camera)
                dense = self.mesh(frame) if self.mesh.available else None
                self._collect_confirm_cues(dense, face, planar, blink, state)

                if challenge is not None and dense is not None:
                    if not challenge_started:
                        c = challenge.start(dense.yaw_signed, dense.pitch, blink.blinks)
                        challenge_started = True
                        emit({"ev": "challenge", "prompt": c.prompt})
                    elif challenge.update(dense.yaw_signed, dense.pitch, blink.blinks):
                        state.add_confirm("challenge")

                if usable_seen % cfg.embed_every == 0 and \
                        len(res.embeddings) < cfg.max_embeddings:
                    try:
                        crop = align(frame, face.landmarks, self.embedder.size)
                        res.embeddings.append(self.embedder(crop))
                    except ValueError:
                        pass

                emit({"ev": "progress",
                      "p": min(1.0, len(res.embeddings) / float(cfg.min_embeddings))})

                if self._enough(res, state, cfg):
                    break
        except CameraError as e:
            res.error = f"camera_unavailable: {e}"
        except Exception as e:                      # never let the worker die mid-auth
            res.error = f"worker_error: {type(e).__name__}"

        state.attention_ok = (blink.eyes_open if self.mesh.available
                              else True)            # no mesh: cannot judge attention
        if not self.mesh.available:
            state.notes["mesh"] = "unavailable"
        res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        if not res.embeddings and res.error is None:
            res.error = "no_usable_face"
        return res

    # ---- cue collection ------------------------------------------------
    def _collect_deny_cues(self, frame, face: Face, state: LivenessState,
                           ir_camera: Camera | None) -> None:
        cues = screen_report(frame, face)
        state.add_deny(*cues.any_deny)
        state.notes["glare_frac"] = round(cues.glare_frac, 4)
        state.notes["moire"] = round(cues.moire_energy, 4)

        if self.antispoof.available and self.antispoof.denies(frame, face.box):
            state.add_deny("antispoof_model")

        if ir_camera is not None:
            for _ts, ir in ir_camera.frames(0.15):
                rep = analyse_ir_face(ir, face.box)
                if rep.screen_dark:
                    state.add_deny("ir_screen_dark")
                elif rep.skin_response_ok:
                    state.add_confirm("ir_skin")
                break

    def _collect_confirm_cues(self, dense, face: Face,
                              planar: PlanarityTracker,
                              blink: BlinkDetector,
                              state: LivenessState) -> None:
        if dense is None:
            return
        blink.update(dense.left_eye6, dense.right_eye6)
        if blink.seen:
            state.add_confirm("blink")
        planar.update(dense.points, dense.yaw_signed)
        if planar.confirms:
            state.add_confirm("parallax")
        elif planar.denies:
            state.add_deny("planar_photo")

    def _enough(self, res: ScanResult, state: LivenessState,
                cfg: ScanConfig) -> bool:
        """Stop early only when nothing more can change the outcome."""
        if state.deny:
            return True                                    # a veto is final
        if len(res.embeddings) < max(cfg.min_embeddings, 1):
            return False
        if cfg.strictness is Strictness.HEAVY and not state.confirm:
            return False                                   # keep looking for a blink
        return len(res.embeddings) >= cfg.max_embeddings or \
            len(res.embeddings) >= cfg.min_embeddings


def build_engine(model_dir: Path | None = None, manifest: Path | None = None,
                 verify: bool = True) -> ScanEngine:
    from . import models as M

    kw = {}
    if model_dir:
        kw["model_dir"] = model_dir
    if manifest:
        kw["manifest"] = manifest

    det_path, _ = M.resolve("detector", verify=verify, **kw)
    emb_path, emb_spec = M.resolve("recognizer", verify=verify, **kw)
    try:
        spoof_path, _ = M.resolve("antispoof", verify=verify, **kw)
    except M.ModelError:
        spoof_path = None

    return ScanEngine(
        detector=Detector(det_path),
        embedder=Embedder(emb_path, model_id=emb_spec.model_id),
        antispoof=PassiveAntiSpoof(spoof_path),
    )
