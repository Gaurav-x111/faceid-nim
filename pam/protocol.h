/* Line protocol shared between pam_faceid.so and faceid-nimd.
 *
 * Deliberately trivial: the PAM module runs inside every login, sudo
 * and screen unlock on the machine. Anything clever in here is a
 * chance to lock the user out of their own computer. */
#ifndef FACEID_PROTOCOL_H
#define FACEID_PROTOCOL_H

#define FACEID_SOCKET      "/run/faceid-nim/auth.sock"
#define FACEID_TIMEOUT_MS  8000
#define FACEID_MAX_LINE    1024

/* Prefixed FID_ rather than R_: R_OK is taken by unistd.h (access modes). */
enum faceid_reply_kind {
    FID_INFO = 0,    /* status text to show the user; keep reading */
    FID_OK,          /* authenticated                              */
    FID_DENIED,      /* not authenticated; fall through to password*/
    FID_UNAVAIL,     /* daemon/camera unusable; fall through       */
    FID_PROTO        /* malformed reply; treat as unavailable      */
};

struct faceid_reply {
    enum faceid_reply_kind kind;
    char text[FACEID_MAX_LINE];
};

#endif /* FACEID_PROTOCOL_H */
