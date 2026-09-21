"""faceid-nim vision worker.

Unprivileged process that owns the camera. It turns frames into
embeddings + liveness cues and hands them to the daemon over a Unix
socket. It never sees stored templates and never makes the unlock
decision.
"""

__version__ = "0.1.0"
