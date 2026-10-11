# faceid-nim Recognition Test Report

**Date:** 2026-09-29
**Environment:** Ubuntu 24.04, amd64, CPU-only (no GPU)
**Models:** Not installed locally (fetched during `.deb` install)

## Tests Run

| Test | Result |
|------|--------|
| Rust daemon unit tests | 28 passed |
| Python vision + app tests | 147 passed, 1 skipped |
| JS extension syntax check | Valid |
| Shell script syntax check | Valid |
| Clippy (Rust lint) | Clean |

## Recognition Pipeline

The full pipeline (YuNet detect → ArcFace align → SFace embed → cosine match)
requires the ONNX models and a camera. Neither is available in this
environment. The models are downloaded during `.deb` installation and
the daemon requires root to run.

## What Was Verified

- All Rust daemon logic (voting, template store, audit, lockout)
- All Python vision code (camera discovery, liveness cues, alignment)
- All app code (settings, enrollment wizard, animation system)
- All packaging scripts (installer, diagnostics, IR emitter)

## What Was NOT Verified

- Real face detection and embedding (needs camera + models)
- Real unlock latency (needs daemon + camera)
- FAR/FRR numbers (needs genuine + impostor datasets)
- Spoof rejection rate (needs printed photos / phone screens)
- IR liveness (needs IR camera)

## Next Steps for Real Testing

1. Install the `.deb` on a machine with a webcam
2. Enroll a face (9-pose wizard, ~20 seconds)
3. Run `sudo faceid-nim status` to verify models loaded
4. Lock and unlock to test recognition
5. Measure latency with `sudo faceid-nim audit 20`
6. For FAR/FRR: collect genuine + impostor datasets, run `eval/far_frr.py`
