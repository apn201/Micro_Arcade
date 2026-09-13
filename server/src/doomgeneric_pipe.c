/* doomgeneric backend: headless DOOM as a frame source.
 *
 * This is deliberately the dumbest possible backend. It does no scaling, no
 * encoding, no game logic and no networking beyond one local socket: it hands
 * raw 320x200 frames to whatever connects, and injects the key codes it gets
 * back. Everything interesting -- downscaling, tile diffing, encoding,
 * transports, input profiles -- lives in the Python service, where it is
 * shared with every other game source.
 *
 * Wire format (see docs/SOURCE_PROTOCOL.md):
 *   out:  "DGF1" | u16 w | u16 h | u8 bpp | u8 state | u32 len | pixels
 *   in:   u8 type | u8 pressed | u8 doom_key
 *
 * Build with server/build-source.sh (zig cc, gcc or clang; Windows or Linux).
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#ifdef _WIN32
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #include <windows.h>
  typedef SOCKET md_sock_t;
  #define MD_SOCK_INVALID INVALID_SOCKET
  #define md_sock_close   closesocket
#else
  #include <unistd.h>
  #include <errno.h>
  #include <netinet/in.h>
  #include <netinet/tcp.h>
  #include <arpa/inet.h>
  #include <sys/socket.h>
  #include <sys/select.h>
  #include <time.h>
  typedef int md_sock_t;
  #define MD_SOCK_INVALID (-1)
  #define md_sock_close   close
#endif

#include "doomgeneric.h"

/* Implemented in md_state.c, which owns the DOOM headers: Win32 and DOOM
   both typedef `boolean`, so the two cannot meet in one file. */
unsigned char md_game_state(void);

/* Game state, reported with every frame so the service's input profile can
   tell "in a menu" from "playing" from "dead" without knowing DOOM's guts. */
#define MD_STATE_LEVEL    0
#define MD_STATE_MENU     1
#define MD_STATE_DEAD     2
#define MD_STATE_NONLEVEL 3

#define MD_MSG_KEY  1
#define MD_MSG_QUIT 2

#define MD_KEYQ_SIZE 64

static md_sock_t s_sock = MD_SOCK_INVALID;
static char      s_svc_host[64] = "127.0.0.1";
static int       s_port = 0;
static uint32_t  s_start_ms;

static unsigned char s_keyq[MD_KEYQ_SIZE][2];   /* {pressed, key} */
static int s_keyq_head, s_keyq_tail;

/* --- platform shims ----------------------------------------------------- */

static uint32_t now_ms(void)
{
#ifdef _WIN32
    return (uint32_t)GetTickCount64();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
#endif
}

static void sleep_ms(uint32_t ms)
{
#ifdef _WIN32
    Sleep(ms);
#else
    usleep(ms * 1000);
#endif
}

#ifdef _WIN32
/* Without this, a fault inside DOOM looks exactly like a hang: Windows keeps
   the process alive spinning in exception dispatch and nothing is printed. */
static LONG WINAPI crash_filter(EXCEPTION_POINTERS *ep)
{
    fprintf(stderr, "doom-source: CRASH code=0x%08lx at %p\n",
            (unsigned long)ep->ExceptionRecord->ExceptionCode,
            ep->ExceptionRecord->ExceptionAddress);
    if (ep->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION)
        fprintf(stderr, "doom-source: access violation %s %p\n",
                ep->ExceptionRecord->ExceptionInformation[0] ? "writing" : "reading",
                (void *)ep->ExceptionRecord->ExceptionInformation[1]);
    fflush(stderr);
    _exit(9);
    return EXCEPTION_EXECUTE_HANDLER;
}
#endif

static int sock_startup(void)
{
#ifdef _WIN32
    WSADATA wsa;
    return WSAStartup(MAKEWORD(2, 2), &wsa) == 0 ? 0 : -1;
#else
    return 0;
#endif
}

/* --- socket ------------------------------------------------------------- */

static void fail(const char *what)
{
    fprintf(stderr, "doom-source: %s failed\n", what);
    exit(1);
}

static void connect_to_service(void)
{
    struct sockaddr_in addr;
    int one = 1;

    if (sock_startup() != 0) fail("winsock init");

    s_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (s_sock == MD_SOCK_INVALID) fail("socket");

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)s_port);
    if (inet_pton(AF_INET, s_svc_host, &addr.sin_addr) != 1) fail("bad host");

    if (connect(s_sock, (struct sockaddr *)&addr, sizeof(addr)) != 0)
        fail("connect");

    /* Frames are latency-critical and already large; never sit on one waiting
       for more data to coalesce. */
    setsockopt(s_sock, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof(one));

    fprintf(stderr, "doom-source: connected to %s:%d\n", s_svc_host, s_port);
}

static int send_all(const void *data, size_t len)
{
    const char *p = (const char *)data;

    while (len > 0) {
        int n = (int)send(s_sock, p, (int)len, 0);
        if (n <= 0) return -1;
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

/* Non-blocking drain of pending input. select() behaves the same on winsock
   and POSIX, which keeps this free of #ifdef. */
static void poll_input(void)
{
    for (;;) {
        fd_set rd;
        struct timeval tv;
        unsigned char msg[3];
        int n;

        FD_ZERO(&rd);
        FD_SET(s_sock, &rd);
        tv.tv_sec = 0;
        tv.tv_usec = 0;

        if (select((int)s_sock + 1, &rd, NULL, NULL, &tv) <= 0) return;

        n = (int)recv(s_sock, (char *)msg, sizeof(msg), 0);
        if (n <= 0) {
            fprintf(stderr, "doom-source: service closed the connection\n");
            exit(0);
        }
        if (n < 3) continue;

        if (msg[0] == MD_MSG_QUIT) {
            fprintf(stderr, "doom-source: quit requested\n");
            exit(0);
        }
        if (msg[0] != MD_MSG_KEY) continue;

        {
            int next = (s_keyq_head + 1) % MD_KEYQ_SIZE;
            if (next == s_keyq_tail) return;      /* full: drop the newest */
            s_keyq[s_keyq_head][0] = msg[1];
            s_keyq[s_keyq_head][1] = msg[2];
            s_keyq_head = next;
        }
    }
}

/* --- doomgeneric callbacks ---------------------------------------------- */

void DG_Init(void)
{
    s_start_ms = now_ms();
    connect_to_service();
}

void DG_DrawFrame(void)
{
    static int s_first = 1;
    unsigned char hdr[16];
    uint32_t len = (uint32_t)(DOOMGENERIC_RESX * DOOMGENERIC_RESY * 4);

    if (s_first) {
        s_first = 0;
        fprintf(stderr, "doom-source: rendering, %dx%d\n",
                DOOMGENERIC_RESX, DOOMGENERIC_RESY);
    }

    memcpy(hdr, "DGF1", 4);
    hdr[4] = (unsigned char)(DOOMGENERIC_RESX & 0xFF);
    hdr[5] = (unsigned char)(DOOMGENERIC_RESX >> 8);
    hdr[6] = (unsigned char)(DOOMGENERIC_RESY & 0xFF);
    hdr[7] = (unsigned char)(DOOMGENERIC_RESY >> 8);
    hdr[8] = 4;                       /* bytes per pixel, 0x00RRGGBB */
    hdr[9] = md_game_state();
    hdr[10] = 0;
    hdr[11] = 0;
    hdr[12] = (unsigned char)(len & 0xFF);
    hdr[13] = (unsigned char)((len >> 8) & 0xFF);
    hdr[14] = (unsigned char)((len >> 16) & 0xFF);
    hdr[15] = (unsigned char)((len >> 24) & 0xFF);

    if (send_all(hdr, sizeof(hdr)) != 0 ||
        send_all(DG_ScreenBuffer, len) != 0) {
        fprintf(stderr, "doom-source: service went away\n");
        exit(0);
    }

    poll_input();
}

void DG_SleepMs(uint32_t ms)
{
    sleep_ms(ms);
}

uint32_t DG_GetTicksMs(void)
{
    return now_ms() - s_start_ms;
}

int DG_GetKey(int *pressed, unsigned char *doomKey)
{
    if (s_keyq_tail == s_keyq_head) {
        poll_input();
        if (s_keyq_tail == s_keyq_head) return 0;
    }

    *pressed = s_keyq[s_keyq_tail][0];
    *doomKey = s_keyq[s_keyq_tail][1];
    s_keyq_tail = (s_keyq_tail + 1) % MD_KEYQ_SIZE;
    return 1;
}

void DG_SetWindowTitle(const char *title)
{
    (void)title;
}

/* --- entry point --------------------------------------------------------- */

int main(int argc, char **argv)
{
    char **doom_argv;
    int doom_argc = 0;
    int i;

    /* DOOM's startup log is block-buffered into a pipe otherwise, which makes
       a hung start look identical to a silent one. */
#ifdef _WIN32
    SetUnhandledExceptionFilter(crash_filter);
#endif
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    doom_argv = (char **)calloc((size_t)argc + 1, sizeof(char *));
    if (!doom_argv) return 1;
    doom_argv[doom_argc++] = argv[0];

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--connect") == 0 && i + 1 < argc) {
            char *sep;
            const char *val = argv[++i];
            sep = strchr((char *)val, ':');
            if (!sep) {
                fprintf(stderr, "doom-source: --connect wants HOST:PORT\n");
                return 1;
            }
            *sep = '\0';
            snprintf(s_svc_host, sizeof(s_svc_host), "%s", val);
            s_port = atoi(sep + 1);
        } else {
            doom_argv[doom_argc++] = argv[i];
        }
    }
    doom_argv[doom_argc] = NULL;

    if (s_port <= 0) {
        fprintf(stderr,
                "usage: %s --connect HOST:PORT [doom args...]\n"
                "  Frame source for the micro-doom service. Not meant to be\n"
                "  run by hand -- the service spawns it.\n", argv[0]);
        return 1;
    }

    doomgeneric_Create(doom_argc, doom_argv);

    for (;;)
        doomgeneric_Tick();

    return 0;
}
