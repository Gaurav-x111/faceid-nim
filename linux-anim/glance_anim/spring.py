"""Spring + easing helpers matching SwiftUI semantics used in Glance.

SwiftUI `.spring(response, dampingFraction)` is an under/damped harmonic
oscillator. We map it to stiffness/damping the standard way:

    omega0 = 2*pi / response
    k = omega0^2
    c = 2 * zeta * omega0        (zeta = dampingFraction)

and integrate with semi-implicit Euler at 60/120Hz — stable for our values.
"""
import math


def stiffness(response: float) -> float:
    omega0 = 2.0 * math.pi / max(response, 1e-6)
    return omega0 * omega0


def damping_coef(response: float, zeta: float) -> float:
    omega0 = 2.0 * math.pi / max(response, 1e-6)
    return 2.0 * zeta * omega0


class Spring1D:
    """Scalar spring toward a target. Set target, then call step(dt)."""

    def __init__(self, x: float = 0.0, response: float = 0.45, zeta: float = 0.7):
        self.x = x
        self.v = 0.0
        self.target = x
        self.response = response
        self.zeta = zeta

    def set_target(self, target: float, response: float, zeta: float):
        self.target = target
        self.response = response
        self.zeta = zeta

    def step(self, dt: float) -> float:
        dt = min(max(dt, 0.0), 0.05)  # clamp tab-switch jumps
        k = stiffness(self.response)
        c = damping_coef(self.response, self.zeta)
        # semi-implicit Euler
        acc = -k * (self.x - self.target) - c * self.v
        self.v += acc * dt
        self.x += self.v * dt
        return self.x

    def settled(self, eps: float = 0.001) -> bool:
        return abs(self.x - self.target) < eps and abs(self.v) < eps


def ease_out_cubic(t: float) -> float:
    """Matches `.easeOut(duration:)` slide in NotchOverlayView.slideAnimation."""
    t = min(max(t, 0.0), 1.0)
    return 1.0 - (1.0 - t) ** 3


class TimedLerp:
    """0->1 progress over `duration` with ease_out_cubic, for the slide axis."""

    def __init__(self):
        self.t0: float | None = None
        self.frm: float = 0.0
        self.to: float = 1.0
        self.duration: float = 0.25

    def start(self, now: float, frm: float, to: float, duration: float):
        self.t0 = now
        self.frm = frm
        self.to = to
        self.duration = max(duration, 1e-6)

    def value(self, now: float) -> tuple[float, bool]:
        if self.t0 is None:
            return self.to, True
        t = (now - self.t0) / self.duration
        if t >= 1.0:
            return self.to, True
        return self.frm + (self.to - self.frm) * ease_out_cubic(t), False
