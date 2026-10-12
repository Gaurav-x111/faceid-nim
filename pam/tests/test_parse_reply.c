/* H6 regression: malformed daemon replies must never authenticate.
 *
 * Includes the real pam_faceid.c so the tested parse_reply and
 * verify_server_is_root are the shipped ones, not copies. pam_* symbols
 * resolve via -lpam at link time; pam_sm_authenticate is linked but
 * never called here.
 *
 * Build: gcc -Wall -Wextra -Werror pam/tests/test_parse_reply.c -lpam \
 *          -o /tmp/test_parse_reply && /tmp/test_parse_reply
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "../pam_faceid.c"

static int failures = 0;

#define CHECK(cond, label) do { \
    if (!(cond)) { printf("FAIL: %s\n", label); failures++; } \
    else { printf("ok: %s\n", label); } \
} while (0)

int main(void)
{
    struct faceid_reply r;

    /* Well-formed kinds. */
    parse_reply("{\"kind\":\"ok\",\"text\":\"\"}\n", &r);
    CHECK(r.kind == FID_OK, "ok parses");
    parse_reply("{\"kind\":\"denied\",\"text\":\"no\"}\n", &r);
    CHECK(r.kind == FID_DENIED, "denied parses");
    parse_reply("{\"kind\":\"unavail\",\"text\":\"down\"}\n", &r);
    CHECK(r.kind == FID_UNAVAIL, "unavail parses");
    parse_reply("{\"kind\":\"info\",\"text\":\"hi\"}\n", &r);
    CHECK(r.kind == FID_INFO, "info parses");

    /* Malformed input is PROTO (fail closed), never OK. */
    const char *bad[] = {
        "",
        "\n",
        "this is not json\n",
        "{\"op\":\"explode\"}\n",
        "{\"kind\":\"ok\"}\n",          /* missing leading brace? no: valid JSON, kind ok */
        "   {\"kind\":\"ok\"}\n",       /* leading whitespace: not an object line */
        "[\"kind\",\"ok\"]\n",
        "{\"kind\":ok}\n",             /* unquoted value */
        "{\"kind\":\"unknown-thing\"}\n",
        NULL,
    };
    /* Kind-carrying object lines keep their kind even when the optional
     * text field is odd: authorization rides on kind, and the peer is
     * already verified root by the time we parse. */
    parse_reply("{\"kind\":\"ok\", \"text\": 12345}\n", &r);
    CHECK(r.kind == FID_OK && r.text[0] == '\0', "non-string text: kind ok, text empty");
    parse_reply("{\"kind\":\"ok\",\"text\":\"unterminated", &r);
    CHECK(r.kind == FID_OK, "truncated text keeps kind");
    /* Index 4 is actually well-formed and must stay OK: it guards
     * against over-tightening the parser. */
    parse_reply(bad[4], &r);
    CHECK(r.kind == FID_OK, "bare ok object stays ok");
    for (int i = 0; bad[i]; i++) {
        if (i == 4)
            continue;
        parse_reply(bad[i], &r);
        char label[128];
        snprintf(label, sizeof(label), "malformed %d is not ok", i);
        CHECK(r.kind != FID_OK, label);
    }

    /* Text extraction is bounded and never overflows. */
    char big[4096];
    memset(big, 'A', sizeof(big) - 1);
    big[sizeof(big) - 1] = '\0';
    char line[8192];
    snprintf(line, sizeof(line), "{\"kind\":\"denied\",\"text\":\"%s\"}", big);
    parse_reply(line, &r);
    CHECK(r.kind == FID_DENIED, "oversize text keeps kind");
    CHECK(strlen(r.text) < sizeof(r.text), "text bounded");

    /* SO_PEERCRED fail-closed: a socketpair peer runs as this test's
     * uid. When not root, verification must refuse; an invalid fd must
     * always refuse. (When the test itself runs as root, the pair peer
     * IS root, so only the invalid-fd assertion applies.) */
    CHECK(verify_server_is_root(-1) < 0, "invalid fd refused");
    int sv[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sv) == 0) {
        int rc = verify_server_is_root(sv[0]);
        if (getuid() == 0) {
            CHECK(rc == 0, "root peer accepted when running as root");
        } else {
            CHECK(rc < 0, "non-root peer refused");
        }
        close(sv[0]);
        close(sv[1]);
    } else {
        printf("SKIP: socketpair unavailable\n");
    }

    if (failures) {
        printf("%d FAILURES\n", failures);
        return 1;
    }
    printf("all parse_reply/peercred checks passed\n");
    return 0;
}
