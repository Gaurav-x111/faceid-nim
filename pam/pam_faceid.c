/* pam_faceid.so -- ask faceid-nimd whether this user's face is present.
 *
 * This module does no AI, opens no camera and links no ML library. It
 * connects to a Unix socket, reads lines and returns a PAM code. If
 * anything at all goes wrong it returns PAM_AUTHINFO_UNAVAIL so the
 * next module in the stack (your password) runs.
 *
 * Install it as:
 *     auth sufficient pam_faceid.so
 * Never `required`. With `sufficient`, PAM_SUCCESS ends the stack
 * successfully and everything else falls through to the password.
 *
 * Build:
 *     gcc -shared -fPIC -Wall -Wextra -Werror -o pam_faceid.so \
 *         pam_faceid.c -lpam
 */
#define _GNU_SOURCE
#define PAM_SM_AUTH

#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <security/pam_modules.h>
#include <security/pam_ext.h>

#include "protocol.h"

static long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static int connect_daemon(const char *path)
{
    struct sockaddr_un addr;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;

    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof(addr.sun_path)) {
        close(fd);
        return -1;
    }
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

/* Minimal scan of a one-line JSON reply. We look only for the "kind"
 * and "text" fields; a full JSON parser in a PAM module is a liability
 * we do not need. */
static void parse_reply(const char *line, struct faceid_reply *out)
{
    const char *k = strstr(line, "\"kind\"");
    const char *t;

    out->kind = FID_PROTO;
    out->text[0] = '\0';

    if (!k)
        return;
    k = strchr(k + 6, '"');
    if (!k)
        return;
    k++;

    if (!strncmp(k, "ok\"", 3))            out->kind = FID_OK;
    else if (!strncmp(k, "denied\"", 7))   out->kind = FID_DENIED;
    else if (!strncmp(k, "unavail\"", 8))  out->kind = FID_UNAVAIL;
    else if (!strncmp(k, "info\"", 5))     out->kind = FID_INFO;

    t = strstr(line, "\"text\"");
    if (t) {
        t = strchr(t + 6, '"');
        if (t) {
            const char *end = strchr(++t, '"');
            if (end && (size_t)(end - t) < sizeof(out->text)) {
                memcpy(out->text, t, (size_t)(end - t));
                out->text[end - t] = '\0';
            }
        }
    }
}

static int read_reply(int fd, char *buf, size_t bufsz, size_t *len,
                      struct faceid_reply *out, long deadline)
{
    for (;;) {
        char *nl = memchr(buf, '\n', *len);
        if (nl) {
            size_t linelen = (size_t)(nl - buf);
            char line[FACEID_MAX_LINE];
            if (linelen >= sizeof(line))
                linelen = sizeof(line) - 1;
            memcpy(line, buf, linelen);
            line[linelen] = '\0';
            /* shift the remainder down */
            memmove(buf, nl + 1, *len - linelen - 1);
            *len -= linelen + 1;
            parse_reply(line, out);
            return 0;
        }

        long remaining = deadline - now_ms();
        if (remaining <= 0)
            return -1;
        {
            struct pollfd p = { .fd = fd, .events = POLLIN };
            int r = poll(&p, 1, (int)remaining);
            if (r <= 0)
                return -1;
        }
        if (*len >= bufsz - 1)
            return -1;                       /* peer is flooding us */
        {
            ssize_t n = read(fd, buf + *len, bufsz - 1 - *len);
            if (n <= 0)
                return -1;
            *len += (size_t)n;
        }
    }
}

/* Escape the few characters that would break our one-line JSON. Users
 * and services cannot normally contain them, but a PAM module must not
 * assume that. */
static int json_escape(const char *in, char *out, size_t outsz)
{
    size_t o = 0;
    for (size_t i = 0; in[i]; i++) {
        unsigned char c = (unsigned char)in[i];
        if (c < 0x20 || c == '"' || c == '\\')
            return -1;
        if (o + 1 >= outsz)
            return -1;
        out[o++] = (char)c;
    }
    out[o] = '\0';
    return 0;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags,
                                   int argc, const char **argv)
{
    const char *user = NULL;
    const char *service = NULL;
    const char *sock = FACEID_SOCKET;
    int quiet = 0;
    long timeout_ms = FACEID_TIMEOUT_MS;
    int fd, rc = PAM_AUTHINFO_UNAVAIL;
    char req[512], euser[256], eservice[256], buf[4096];
    size_t len = 0;
    long deadline;

    (void)flags;

    for (int i = 0; i < argc; i++) {
        if (!strncmp(argv[i], "socket=", 7))
            sock = argv[i] + 7;
        else if (!strncmp(argv[i], "timeout=", 8))
            timeout_ms = atol(argv[i] + 8);
        else if (!strcmp(argv[i], "quiet"))
            quiet = 1;
    }
    if (timeout_ms < 500 || timeout_ms > 60000)
        timeout_ms = FACEID_TIMEOUT_MS;

    if (pam_get_user(pamh, &user, NULL) != PAM_SUCCESS || !user || !*user)
        return PAM_AUTHINFO_UNAVAIL;
    if (pam_get_item(pamh, PAM_SERVICE, (const void **)&service) != PAM_SUCCESS
        || !service)
        service = "unknown";

    if (json_escape(user, euser, sizeof(euser)) < 0 ||
        json_escape(service, eservice, sizeof(eservice)) < 0)
        return PAM_AUTHINFO_UNAVAIL;

    fd = connect_daemon(sock);
    if (fd < 0) {
        /* Daemon down. Say nothing and let the password prompt appear:
         * a broken face-unlock must be invisible, not fatal. */
        pam_syslog(pamh, LOG_DEBUG, "faceid daemon unreachable at %s", sock);
        return PAM_AUTHINFO_UNAVAIL;
    }

    if (snprintf(req, sizeof(req),
                 "{\"op\":\"auth\",\"user\":\"%s\",\"service\":\"%s\"}\n",
                 euser, eservice) >= (int)sizeof(req)) {
        close(fd);
        return PAM_AUTHINFO_UNAVAIL;
    }
    if (write(fd, req, strlen(req)) < 0) {
        close(fd);
        return PAM_AUTHINFO_UNAVAIL;
    }

    deadline = now_ms() + timeout_ms;
    for (;;) {
        struct faceid_reply r;
        if (read_reply(fd, buf, sizeof(buf), &len, &r, deadline) < 0) {
            rc = PAM_AUTHINFO_UNAVAIL;       /* timeout: fall through */
            break;
        }
        if (r.kind == FID_INFO) {
            if (!quiet && r.text[0])
                pam_info(pamh, "%s", r.text);
            continue;
        }
        if (r.kind == FID_OK) {
            rc = PAM_SUCCESS;
            break;
        }
        if (r.kind == FID_DENIED) {
            if (!quiet && r.text[0])
                pam_info(pamh, "%s", r.text);
            rc = PAM_AUTH_ERR;
            break;
        }
        rc = PAM_AUTHINFO_UNAVAIL;
        break;
    }

    close(fd);
    pam_syslog(pamh, LOG_INFO, "faceid result for %s (%s): %d", user, service, rc);
    return rc;
}

PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags,
                              int argc, const char **argv)
{
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_acct_mgmt(pam_handle_t *pamh, int flags,
                                int argc, const char **argv)
{
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_IGNORE;
}
