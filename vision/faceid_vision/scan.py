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
from .liveness.screen import MOIRE_THRESHOLD, screen_report
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
    # Active camera mode from the request: "rgb" | "ir" | "both" |
    # "hybrid". "both" and its explicit alias "hybrid" run recognition on
    # the RGB primary and spoof cues on the IR camera. The worker gates
    # RGB-specific screen cues (glare/bezel/moire) on mode: those FFT-
    # and contrast-based checks are tuned for webcam texture statistics
    # and false-positive on IR sensor noise. IR screens are detected by
    # analyse_ir_face()'s "screen_dark" cue instead.
    mode: str = "rgb"
    # Moire is a soft cue, not an instant veto. A single transition
    # frame (exposure/AGC swing, motion blur) must not kill an
    # otherwise genuine auth; a screen or print floods *every* frame.
    # A moire deny is only emitted once it has been seen in at least
    # `moire_required` of the last `moire_frames` usable frames.
    moire_frames: int = 5
    moire_required: int = 3
    # Enrollment preview. Off for every unlock scan: a preview frame
    # must only ever exist for a session the enrolling app owns.
    preview: bool = False

    def __post_init__(self):
        self.max_embeddings = max(1, int(self.max_embeddings))
        self.min_embeddings = max(1, int(self.min_embeddings))
        self.embed_every = max(1, int(self.embed_every or 1))
        self.moire_frames = max(1, int(self.moire_frames or 5))
        self.moire_required = max(1, min(int(self.moire_required or 3),
                                        self.moire_frames))


@dataclass
class ScanResult:
    embeddings: list[np.ndarray] = field(default_factory=list)
    liveness: LivenessState = field(default_factory=LivenessState)
    model_id: str = ""
    frames: int = 0
    usable: int = 0
    elapsed_ms: int = 0
    error: str | None = None


class _MoireWindow:
    """Bounded rolling window of per-frame moire observations.

    Tracks the number of positive hits in the last ``n`` usable frames,
    so one flickering frame cannot veto an authentication while a real
    replay (positive every frame) still accumulates."""

    def __init__(self, n: int):
        self.n = max(1, n)
        self.hits = 0
        self._q: list[bool] = []

    def push(self, hit: bool) -> None:
        self._q.append(hit)
        if hit:
            self.hits += 1
        if len(self._q) > self.n:
            drop = self._q.pop(0)
            if drop:
                self.hits -= 1

    def __len__(self) -> int:
        return len(self._q)

    @property
    def hits_in_window(self) -> int:
        return self.hits


class ScanEngine:
    """Holds the loaded models. Create once, reuse for every scan --
    a cold ONNX session is most of the latency budget."""

    def __init__(self, detector: Detector, embedder: Embedder,
                 antispoof: PassiveAntiSpoof | None = None,
                 mesh: FaceMesh | None = None,
                 landmarks: FaceMesh | None = None):
        self.detector = detector
        self.embedder = embedder
        self.antispoof = antispoof or PassiveAntiSpoof(None)
        # `mesh` is the historical name and is still what tests inject;
        # `landmarks` is what build_engine passes. One object either way.
        self.mesh = mesh if mesh is not None else (
            landmarks if landmarks is not None else FaceMesh())
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
        moire_win = _MoireWindow(cfg.moire_frames)

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

                self._collect_deny_cues(frame, face, state, cfg, ir_camera, moire_win)
                dense = self.mesh(frame) if self.mesh.available else None
                self._collect_confirm_cues(dense, face, planar, blink, state)

                if challenge is not None and dense is not None:
                    if not challenge_started:
                        c = challenge.start(dense.yaw_signed, dense.pitch, blink.blinks)
                        challenge_started = True
                        emit({"ev": "challenge", "prompt": c.prompt})
                    elif challenge.update(dense.yaw_signed, dense.pitch, blink.blinks):
                        state.add_confirm("challenge")

                if usable_seen % max(1, cfg.embed_every) == 0 and \
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

        state.attention_ok = True
        if self.mesh.available:
            # blink.history empty = no dense observations (dark/IR/fast
            # motion): unknown, not closed. Only a measured closed eye
            # may veto when require_attention is on.
            state.attention_ok = blink.eyes_open if blink.history else True
        if not self.mesh.available:
            # Say *why*, not just "unavailable". A liveness backend that
            # is missing and one that is broken need different fixes, and
            # the difference used to be invisible.
            state.notes["mesh"] = "unavailable"
            state.notes["mesh_backend"] = getattr(self.mesh, "backend", "none")
            state.notes["mesh_reason"] = getattr(self.mesh, "reason", "")
        res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        if not res.embeddings and res.error is None:
            res.error = "no_usable_face"
        return res

    # ---- cue collection ------------------------------------------------
    def _collect_deny_cues(self, frame, face: Face, state: LivenessState,
                           cfg: ScanConfig,
                           ir_camera: Camera | None,
                           moire_win: _MoireWindow | None = None) -> None:
        if cfg.mode != "ir":
            cues = screen_report(frame, face)
            # Glare and device-frame are strong, geometry/contrast based
            # and stay hard denies. Moire is a soft cue resolved over
            # the rolling window below.
            state.add_deny(*(c for c in cues.any_deny if c != "moire"))
            state.notes["glare_frac"] = round(cues.glare_frac, 4)
            state.notes["moire"] = round(cues.moire_energy, 4)
            state.notes["moire_threshold"] = MOIRE_THRESHOLD
            if moire_win is not None:
                moire_win.push(cues.moire)
                state.notes["moire_window"] = {
                    "frames": len(moire_win),
                    "hits": moire_win.hits_in_window,
                    "required": cfg.moire_required,
                }
                if moire_win.hits_in_window >= cfg.moire_required:
                    state.add_deny("moire")
        else:
            # Screens and prints emit almost no infrared; glare/bezel/
            # moire are RGB-specific attack surfaces whose thresholds
            # are tuned for webcam texture and false-positive on IR
            # sensor noise (fixed-pattern noise, different gain). The
            # IR-native screen check is analyse_ir_face()'s
            # "screen_dark" cue below, not the RGB FFT/contrast gates.
            state.notes["glare_frac"] = None
            state.notes["moire"] = None

        if self.antispoof.available and self.antispoof.denies(frame, face.box):
            state.add_deny("antispoof_model")

        # IR analysis is allowed only when the configured mode asks for
        # it. cfg.mode is the sole authority: the mere presence of an
        # opened IR camera, or a single-channel (GREY) negotiated frame,
        # must never promote an "rgb"/"hybrid"-rgb scan to IR analysis.
        # IR-primary native cue: a resolved auto->IR (or plain ir) scan
        # has no secondary IR camera — the primary IS the IR sensor — so
        # run analyse_ir_face on the primary frame directly (box already
        # in primary pixels, no rescale). Otherwise a resolved-IR scan
        # would carry zero spoof cues.
        if cfg.mode == "ir" and ir_camera is None:
            try:
                rep = analyse_ir_face(frame, face.box)
                if rep.screen_dark:
                    state.add_deny("ir_screen_dark")
                elif rep.skin_response_ok:
                    state.add_confirm("ir_skin")
            except Exception:
                pass
        if cfg.mode in ("ir", "both", "hybrid") and ir_camera is not None:
            try:
                for _ts, ir in ir_camera.frames(0.15):
                    # Rescale RGB box -> IR pixels (sensors differ in
                    # resolution/FOV, e.g. 640x480 vs 640x360).
                    rh = frame.shape[0] / max(1, ir.shape[0])
                    rw = frame.shape[1] / max(1, ir.shape[1])
                    # face.box is (x,y,w,h) in RGB pixels; map to IR.
                    bx, by, bw, bh = face.box
                    scaled = (int(bx / rw), int(by / rh),
                              int(bw / rw), int(bh / rh))
                    rep = analyse_ir_face(ir, scaled)
                    if rep.screen_dark:
                        state.add_deny("ir_screen_dark")
                    elif rep.skin_response_ok:
                        state.add_confirm("ir_skin")
                    break
            except Exception:
                pass  # IR is secondary: never veto a good RGB scan

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
        # min reached (and heavy confirm satisfied): enough for voting.
        # max_embeddings only caps collection in the loop above.
        return True


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

    # Both of these are optional features, and both are resolved through
    # resolve_optional so "not installed" is a logged, reported, degraded
    # state rather than a silent False. Recognition never depends on
    # either one.
    spoof = M.resolve_optional("antispoof", verify=verify, **kw)
    spoof_path = spoof[0] if spoof else None
    marks = M.resolve_optional("landmarks", verify=verify, **kw)
    landmark_path = marks[0] if marks else None

    return ScanEngine(
        detector=Detector(det_path),
        embedder=Embedder(emb_path, model_id=emb_spec.model_id),
        antispoof=PassiveAntiSpoof(spoof_path),
        landmarks=FaceMesh(model_path=landmark_path),
    )
