# Security policy

## Reporting

Report privately, not as a public issue. Add a real contact address here
before publishing the repository.

## In scope

- Authentication bypass: unlocking as a user whose face is not enrolled.
- Presentation attacks that defeat the documented strictness levels.
- Privilege escalation through the daemon, the auth socket, or D-Bus.
- Template extraction by an unprivileged local user.

## Explicitly out of scope

These are documented limitations, not vulnerabilities:

- Video replay against an RGB-only camera in `light` mode. This is
  stated in the README as a known limit.
- Attacks by `root`. The daemon runs as root; the threat model does not
  defend against root.
- Twins and close look-alikes at a permissive threshold.
- Reading `/var/lib/faceid-nim` from an unencrypted disk that has been
  removed from the machine. Full-disk encryption is the baseline; TPM
  sealing is a planned upgrade.

## Design invariants

A change that breaks any of these is a security bug even if nothing
else is wrong:

1. Recognition and liveness are evaluated independently. A high
   similarity score must never soften a liveness failure.
2. Callers receive success or failure only — never a similarity score,
   never a per-cue breakdown. Scores let an attacker hill-climb.
3. The vision worker never receives templates.
4. The UI never receives frames, embeddings, or scores.
5. Frames are never written to disk.
6. A daemon or camera failure returns `PAM_AUTHINFO_UNAVAIL` (fall
   through to password), never success.
7. Every model file is checksum-pinned; an unverified model does not load.
8. Enrollment requires the account password via polkit.
