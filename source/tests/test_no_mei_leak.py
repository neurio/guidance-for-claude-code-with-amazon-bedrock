# ABOUTME: Tests that prove PyInstaller --onedir and Nuitka --standalone do not
# ABOUTME: leak MEI-style tmp extraction directories under abnormal termination.
"""Regression tests for the _MEI tmp-dir leak fix.

Background
==========
PyInstaller ``--onefile`` and Nuitka ``--onefile`` both work by unpacking a
compressed archive of the Python interpreter + deps into a per-invocation
temporary directory at startup. Cleanup relies on an ``atexit`` hook. When
the process dies abnormally (SIGKILL, hard timeout, OOM, crash) the tmp
directory is left behind. Over time this fills ``/tmp`` — we hit that
condition in production with the shipped ``credential-process`` binary and
had ``/`` reach 100% full from 12,000+ leaked ``/tmp/_MEI*`` directories.

The fix is to switch both binaries to non-``--onefile`` modes:
- PyInstaller ``--onedir`` — the launcher runs directly from its install
  location; there is no runtime extraction at all.
- Nuitka ``--standalone`` (without ``--onefile``) — same idea.

These tests build tiny stand-in binaries (a Python script that imports a
few common deps, prints ``READY``, then sleeps) in both modes and prove:

- The ``--onefile`` build DOES leak a tmp directory when SIGKILLed.
  This is the "RED" test that proves the bug exists on the current
  distribution path.
- The ``--onedir`` build DOES NOT create any tmp directory in the first
  place, so there is nothing to leak.
  This is the "GREEN" test that proves the fix works.

All tests are marked ``slow`` because each one invokes a real PyInstaller
or Nuitka build (~10-20s). They do not run in the default ``pytest``
invocation; opt in with ``pytest -m slow``.

The tests skip cleanly when the corresponding build tool is not installed.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Marker the stub binary prints on stdout once it has finished importing
# its dependencies and is ready to be killed. Reading this from stdout
# guarantees any runtime tmp extraction has already happened, which makes
# the "did it leak?" assertion deterministic.
_STUB_READY_MARKER = "READY"

# How long to wait for the stub binary to print READY before giving up.
_STUB_READY_TIMEOUT_S = 30.0

# How long to wait after SIGKILL for the process to be reaped.
_PROCESS_REAP_TIMEOUT_S = 5.0

# PyInstaller onefile leaks directories with this prefix under the OS tmp dir.
_PYINSTALLER_LEAK_PREFIX = "_MEI"

# Nuitka onefile leaks directories with this prefix under the OS tmp dir.
# Nuitka's default ``--onefile-tempdir-spec`` is
# ``{TEMP}/onefile_{PID}_{TIME}``.
_NUITKA_LEAK_PREFIX = "onefile_"

# Body of the stub Python program that both build backends compile. It
# imports a handful of dependencies (to force a non-trivial extraction
# under onefile mode), prints the ready marker, then sleeps long enough
# for the test to send SIGKILL.
_STUB_SOURCE = """\
import sys
import time
# Import a few things to make the onefile bundle non-trivial so the MEI
# extraction is unambiguous. These are stdlib-only so they don't add
# dependencies to the test suite.
import json
import hashlib
import base64
import urllib.parse
_ = json.dumps({"h": hashlib.sha256(b"x").hexdigest(),
                "b": base64.b64encode(b"x").decode(),
                "u": urllib.parse.quote("x")})
print("READY", flush=True)
# Sleep long enough for the test to kill us. If the test does not kill
# within this window, exit cleanly so the test framework doesn't hang.
time.sleep(60)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_root() -> Path:
    """Return the OS temp directory where PyInstaller / Nuitka leak dirs land."""
    return Path(tempfile.gettempdir())


def _list_leaks(prefix: str) -> set[Path]:
    """Return the set of tmp directories currently starting with ``prefix``."""
    return {p for p in _tmp_root().iterdir() if p.name.startswith(prefix) and p.is_dir()}


def _wait_for_ready(proc: subprocess.Popen) -> None:
    """Block until the stub prints ``READY`` or the timeout expires."""
    deadline = time.monotonic() + _STUB_READY_TIMEOUT_S
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            # EOF before READY — process crashed during extraction. That is
            # itself a test failure because it means the build is broken.
            raise AssertionError(
                f"stub binary exited before printing READY (returncode="
                f"{proc.poll()!r})"
            )
        if line.strip() == _STUB_READY_MARKER:
            return
    raise AssertionError(
        f"stub binary did not print READY within {_STUB_READY_TIMEOUT_S}s"
    )


def _sigkill_and_reap(proc: subprocess.Popen) -> None:
    """Send SIGKILL (or Windows equivalent) and wait for the process to exit.

    SIGKILL is the deterministic killer because it cannot be trapped by
    the PyInstaller bootloader or Nuitka's runtime — it guarantees the
    ``atexit`` cleanup path does NOT run, which is exactly the leak
    scenario we're reproducing.
    """
    if platform.system() == "Windows":
        # Windows has no SIGKILL. TerminateProcess is the closest analogue
        # and similarly bypasses any userland cleanup handlers.
        proc.kill()
    else:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=_PROCESS_REAP_TIMEOUT_S)


def _write_stub(target_dir: Path) -> Path:
    """Write the stub Python source file into ``target_dir`` and return its path."""
    stub_path = target_dir / "stub.py"
    stub_path.write_text(_STUB_SOURCE, encoding="utf-8")
    return stub_path


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------


def _pyinstaller_available() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        return False


def _nuitka_available() -> bool:
    try:
        import nuitka  # noqa: F401
        return True
    except ImportError:
        return False


# Substring uniquely present in Nuitka's error when static libpython headers
# (the Debian ``python3-dev`` package or equivalent) are not installed. When
# we see this we skip rather than fail, because it's a local dev-environment
# gap that doesn't reflect a real regression in what we're testing.
_NUITKA_MISSING_PYTHON_DEV_HINT = "Automatic detection of static libpython failed"


def _skip_if_nuitka_prereq_missing(build_error: AssertionError) -> None:
    """Convert a well-known Nuitka prerequisite error into a pytest skip.

    Called from inside the ``except AssertionError`` handler in the Nuitka
    tests. Any Nuitka build failure whose message contains the known
    prerequisite hint is treated as a skip; anything else re-raises so
    that genuine build regressions still surface as failures.
    """
    if _NUITKA_MISSING_PYTHON_DEV_HINT in str(build_error):
        pytest.skip(
            "Nuitka requires python3-dev (or the equivalent Python "
            "development headers package) to be installed. Install it "
            "and re-run to exercise the Nuitka build path."
        )
    raise build_error


def _build_pyinstaller_stub(build_dir: Path, mode: str) -> Path:
    """Build the stub with PyInstaller in ``onefile`` or ``onedir`` mode.

    Returns the path to the launcher binary the test should invoke.
    """
    assert mode in {"onefile", "onedir"}, mode
    stub_src = _write_stub(build_dir)
    dist_dir = build_dir / "dist"
    work_dir = build_dir / "build"
    spec_dir = build_dir / "spec"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        f"--{mode}",
        "--clean",
        "--noconfirm",
        "--name=stub",
        f"--distpath={dist_dir}",
        f"--workpath={work_dir}",
        f"--specpath={spec_dir}",
        "--log-level=WARN",
        str(stub_src),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=build_dir)
    if result.returncode != 0:
        raise AssertionError(
            "PyInstaller build failed:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    if mode == "onefile":
        # PyInstaller writes a single-file executable at dist/stub[.exe].
        exe_name = "stub.exe" if platform.system() == "Windows" else "stub"
        return dist_dir / exe_name

    # onedir: PyInstaller 6.x defaults to ``dist/stub/stub[.exe]`` with
    # bundled internals under ``dist/stub/_internal/``.
    exe_name = "stub.exe" if platform.system() == "Windows" else "stub"
    return dist_dir / "stub" / exe_name


def _build_nuitka_stub(build_dir: Path, mode: str) -> Path:
    """Build the stub with Nuitka in ``onefile`` or ``standalone`` mode.

    Returns the path to the launcher binary the test should invoke.
    """
    assert mode in {"onefile", "standalone"}, mode
    stub_src = _write_stub(build_dir)
    output_dir = build_dir / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--assume-yes-for-downloads",
        "--remove-output",
        "--quiet",
        f"--output-dir={output_dir}",
        "--output-filename=stub",
    ]
    if mode == "onefile":
        cmd.extend(["--onefile", "--standalone"])
    else:
        cmd.append("--standalone")
    cmd.append(str(stub_src))

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=build_dir)
    if result.returncode != 0:
        raise AssertionError(
            "Nuitka build failed:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    exe_name = "stub.exe" if platform.system() == "Windows" else "stub"
    if mode == "onefile":
        # Onefile output: ``{output_dir}/stub[.exe]``.
        return output_dir / exe_name

    # Standalone: ``{output_dir}/stub.dist/stub[.exe]``.
    return output_dir / "stub.dist" / exe_name


# ---------------------------------------------------------------------------
# Core assertions
# ---------------------------------------------------------------------------


def _assert_leaks_on_sigkill(binary: Path, prefix: str) -> None:
    """Assert that killing ``binary`` mid-execution leaks a tmp dir with ``prefix``.

    This is the RED assertion — it MUST hold for the current onefile
    binaries. If a future PyInstaller / Nuitka release changes the
    extraction behavior such that leaks no longer occur (e.g. they add a
    signal handler for SIGKILL — which is impossible, but hypothetically
    if the semantics change) this test would fail and we'd want to
    revisit whether the fix is still needed.
    """
    before = _list_leaks(prefix)
    proc = subprocess.Popen(
        [str(binary)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_ready(proc)
        # At this point the onefile bootloader has finished extracting;
        # the tmp dir must exist right now.
        mid = _list_leaks(prefix)
        created_during = mid - before
        assert len(created_during) >= 1, (
            f"expected onefile binary to have created at least one "
            f"{prefix}* tmp dir by READY time; found new={created_during}"
        )
        _sigkill_and_reap(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=_PROCESS_REAP_TIMEOUT_S)

    after = _list_leaks(prefix)
    leaked = after - before
    try:
        assert len(leaked) >= 1, (
            f"expected onefile binary to leak at least one {prefix}* tmp "
            f"dir after SIGKILL; found none (before={before}, after={after})"
        )
    finally:
        # Never leave the test's own leaks behind, even on failure.
        for d in leaked:
            shutil.rmtree(d, ignore_errors=True)


def _assert_no_leaks_ever(binary: Path, prefix: str) -> None:
    """Assert ``binary`` never creates a tmp dir with ``prefix`` at any point.

    Stronger than "cleans up on exit": for onedir / standalone, no
    extraction ever happens, so no tmp dir is ever created — not even
    during normal execution.
    """
    before = _list_leaks(prefix)
    proc = subprocess.Popen(
        [str(binary)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_ready(proc)
        # Mid-execution snapshot: the whole point of onedir is that
        # nothing gets extracted, so this must equal ``before``.
        mid = _list_leaks(prefix)
        created_during = mid - before
        assert not created_during, (
            f"onedir/standalone binary should NEVER create a {prefix}* "
            f"tmp dir, but did during execution: {created_during}"
        )
        _sigkill_and_reap(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=_PROCESS_REAP_TIMEOUT_S)

    after = _list_leaks(prefix)
    leaked = after - before
    try:
        assert not leaked, (
            f"onedir/standalone binary must not leak {prefix}* tmp dirs "
            f"after SIGKILL; found: {leaked}"
        )
    finally:
        # Defensive: if the assertion above is wrong, still clean up.
        for d in leaked:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.slow


@pytest.mark.skipif(
    not _pyinstaller_available(),
    reason="PyInstaller not installed",
)
class TestPyInstaller:
    """PyInstaller onefile leaks; PyInstaller onedir does not."""

    def test_onefile_leaks_mei_on_sigkill(self, tmp_path: Path) -> None:
        """RED: proves the current onefile build leaks a _MEI* dir on SIGKILL."""
        binary = _build_pyinstaller_stub(tmp_path, mode="onefile")
        _assert_leaks_on_sigkill(binary, _PYINSTALLER_LEAK_PREFIX)

    def test_onedir_never_creates_mei(self, tmp_path: Path) -> None:
        """GREEN: proves the onedir build does not create any _MEI* dir at all."""
        binary = _build_pyinstaller_stub(tmp_path, mode="onedir")
        _assert_no_leaks_ever(binary, _PYINSTALLER_LEAK_PREFIX)


@pytest.mark.skipif(
    not _nuitka_available(),
    reason="Nuitka not installed",
)
class TestNuitka:
    """Nuitka onefile leaks; Nuitka standalone does not."""

    def test_onefile_leaks_on_sigkill(self, tmp_path: Path) -> None:
        """RED: proves the current onefile build leaks an onefile_* dir on SIGKILL."""
        try:
            binary = _build_nuitka_stub(tmp_path, mode="onefile")
        except AssertionError as exc:
            _skip_if_nuitka_prereq_missing(exc)
            raise  # unreachable — helper either skips or re-raises
        _assert_leaks_on_sigkill(binary, _NUITKA_LEAK_PREFIX)

    def test_standalone_never_creates_tmp_dir(self, tmp_path: Path) -> None:
        """GREEN: proves the standalone build does not create any onefile_* dir."""
        try:
            binary = _build_nuitka_stub(tmp_path, mode="standalone")
        except AssertionError as exc:
            _skip_if_nuitka_prereq_missing(exc)
            raise  # unreachable — helper either skips or re-raises
        _assert_no_leaks_ever(binary, _NUITKA_LEAK_PREFIX)
