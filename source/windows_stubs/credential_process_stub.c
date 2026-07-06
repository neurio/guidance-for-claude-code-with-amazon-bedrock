/*
 * ABOUTME: Tiny Windows shim that forwards to a Nuitka-standalone
 * ABOUTME: launcher inside its sibling `*-dist/` directory.
 *
 * Why this exists
 * ---------------
 * The credential-process and otel-helper binaries used to be shipped
 * as PyInstaller / Nuitka `--onefile` executables. That mode extracts
 * the bundled archive to a per-invocation temp directory at startup
 * and cleans up via an atexit hook. Abnormal termination bypasses
 * that cleanup, so under credential_process workloads that get
 * killed by boto3 timeouts the temp directories accumulate. In
 * production this filled /tmp with 12,000+ directories.
 *
 * The fix switches to `--standalone` (Nuitka) / `--onedir`
 * (PyInstaller), which produces a directory tree that runs directly
 * from its install location. Nothing gets extracted at runtime, so
 * nothing can leak.
 *
 * On Linux and macOS the install script exposes the launcher through
 * a symlink so existing `~/.aws/config` entries pointing at
 * `~/claude-code-with-bedrock/credential-process` keep working with
 * zero user action.
 *
 * On Windows, boto3's credential_process runner uses
 * `subprocess.Popen(shell=False)` which does NOT resolve PATHEXT.
 * A bare `credential-process` string in `~/.aws/config` therefore
 * requires an exact filename with extension. Previously that was
 * `credential-process.exe` (the onefile binary). This shim provides
 * the same `.exe` path for backward compatibility: it is a real PE
 * executable that resolves its own directory and forwards all
 * arguments to the real launcher inside the sibling `*-dist/`
 * directory. Stdin, stdout, stderr are inherited via CreateProcess
 * with bInheritHandles=TRUE, so JSON credentials produced by the
 * real launcher flow through unmodified.
 *
 * The shim is invoked as e.g.
 *   credential-process.exe --profile MyProfile
 * and forwards to
 *   %~dp0credential-process-dist\credential-process-windows.exe --profile MyProfile
 *
 * Build
 * -----
 * Compiled at build time in CodeBuild via MinGW:
 *   x86_64-w64-mingw32-gcc credential_process_stub.c \
 *       -DTARGET_LAUNCHER=... -DTARGET_DIST_DIR=... \
 *       -o credential-process.exe -Os -s -municode
 * The `-Os -s` combination produces a ~30 KB stripped binary; no
 * runtime dependencies beyond the Windows API.
 *
 * The macros must be defined at compile time so the same source
 * compiles for both credential-process and otel-helper:
 *   TARGET_DIST_DIR    e.g. "credential-process-dist"
 *   TARGET_LAUNCHER    e.g. "credential-process-windows.exe"
 */

#include <windows.h>
#include <wchar.h>
#include <stdio.h>
#include <stdlib.h>

#ifndef TARGET_DIST_DIR
#error "TARGET_DIST_DIR must be defined at compile time"
#endif

#ifndef TARGET_LAUNCHER
#error "TARGET_LAUNCHER must be defined at compile time"
#endif

/*
 * Widen a compile-time ASCII string literal into a wide-char literal.
 * Two levels of macro expansion so the value of TARGET_DIST_DIR /
 * TARGET_LAUNCHER (not the literal token) gets prefixed with L.
 */
#define _WIDE(s) L##s
#define WIDE(s) _WIDE(s)

/* Upper bound on the fully-quoted forwarded command line. */
#define CMDLINE_MAX 32768

/*
 * Append a single argument to `dst`, wrapping it in double quotes and
 * escaping embedded backslashes and quotes per the CommandLineToArgvW
 * rules that CreateProcess consumers use. Returns 0 on overflow.
 */
static int append_quoted_arg(wchar_t *dst, size_t dst_capacity, const wchar_t *arg) {
    size_t used = wcslen(dst);
    if (used + 3 >= dst_capacity) {
        return 0;
    }
    if (used > 0) {
        dst[used++] = L' ';
    }
    dst[used++] = L'"';
    dst[used] = 0;

    /* Copy the arg, doubling any backslashes that precede a quote and
       escaping any embedded quotes. Most credential-process args are
       simple identifiers, but be defensive. */
    for (const wchar_t *p = arg; *p; p++) {
        size_t backslashes = 0;
        while (*p == L'\\') {
            backslashes++;
            p++;
        }
        if (*p == 0) {
            /* Trailing backslashes before the closing quote must be
               doubled so the closing quote is not escaped. */
            for (size_t i = 0; i < 2 * backslashes; i++) {
                if (used + 1 >= dst_capacity) return 0;
                dst[used++] = L'\\';
            }
            break;
        }
        if (*p == L'"') {
            /* Backslashes before an embedded quote are doubled, then
               the quote itself is escaped. */
            for (size_t i = 0; i < 2 * backslashes + 1; i++) {
                if (used + 1 >= dst_capacity) return 0;
                dst[used++] = L'\\';
            }
            if (used + 1 >= dst_capacity) return 0;
            dst[used++] = L'"';
        } else {
            for (size_t i = 0; i < backslashes; i++) {
                if (used + 1 >= dst_capacity) return 0;
                dst[used++] = L'\\';
            }
            if (used + 1 >= dst_capacity) return 0;
            dst[used++] = *p;
        }
    }

    if (used + 2 >= dst_capacity) return 0;
    dst[used++] = L'"';
    dst[used] = 0;
    return 1;
}

int wmain(int argc, wchar_t *argv[]) {
    /* Discover our own directory so we can locate the sibling dist tree. */
    wchar_t self_path[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, self_path, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) {
        fwprintf(stderr, L"stub: GetModuleFileNameW failed (err=%lu)\n", GetLastError());
        return 1;
    }
    /* Strip the trailing filename, keep the directory including its
       trailing separator. */
    wchar_t *last_sep = wcsrchr(self_path, L'\\');
    if (!last_sep) {
        fwprintf(stderr, L"stub: cannot parse own path %ls\n", self_path);
        return 1;
    }
    *(last_sep + 1) = 0;

    /* Build the full launcher path:
         <self_dir>\<TARGET_DIST_DIR>\<TARGET_LAUNCHER> */
    wchar_t launcher[MAX_PATH];
    int written = swprintf(launcher, MAX_PATH, L"%ls%ls\\%ls",
                           self_path, WIDE(TARGET_DIST_DIR), WIDE(TARGET_LAUNCHER));
    if (written < 0 || written >= MAX_PATH) {
        fwprintf(stderr, L"stub: launcher path too long\n");
        return 1;
    }

    /* Build the forwarded command line. First arg is the launcher
       path itself (CreateProcessW convention when lpApplicationName
       is set to the target). Then each of our argv[1..] gets appended
       verbatim (quoted). */
    wchar_t *cmdline = (wchar_t *)calloc(CMDLINE_MAX, sizeof(wchar_t));
    if (!cmdline) {
        fwprintf(stderr, L"stub: OOM\n");
        return 1;
    }
    if (!append_quoted_arg(cmdline, CMDLINE_MAX, launcher)) {
        fwprintf(stderr, L"stub: launcher path exceeds cmdline buffer\n");
        free(cmdline);
        return 1;
    }
    for (int i = 1; i < argc; i++) {
        if (!append_quoted_arg(cmdline, CMDLINE_MAX, argv[i])) {
            fwprintf(stderr, L"stub: forwarded arguments exceed cmdline buffer\n");
            free(cmdline);
            return 1;
        }
    }

    /* Inherit stdin/stdout/stderr so JSON credentials flow through
       unmodified and error messages surface to the caller. */
    STARTUPINFOW si = { 0 };
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    PROCESS_INFORMATION pi = { 0 };

    BOOL ok = CreateProcessW(launcher, cmdline, NULL, NULL,
                             TRUE /* bInheritHandles */,
                             0, NULL, NULL, &si, &pi);
    if (!ok) {
        DWORD err = GetLastError();
        fwprintf(stderr, L"stub: failed to launch %ls (err=%lu)\n", launcher, err);
        free(cmdline);
        return 1;
    }
    free(cmdline);

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD exit_code = 0;
    GetExitCodeProcess(pi.hProcess, &exit_code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)exit_code;
}
