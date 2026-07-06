# ABOUTME: End-to-end tests that build the real credential-process and
# ABOUTME: otel-helper binaries via PackageCommand and verify no MEI leak.
"""End-to-end regression tests for the shipped binaries.

These tests are the Layer 2 guardrail on top of the Layer 1 mechanism
tests in ``test_no_mei_leak.py``. Layer 1 proves the general property
"PyInstaller onefile leaks, onedir does not" using tiny stub binaries.
Layer 2 proves that the actual ``credential-process`` and ``otel-helper``
binaries produced by ``PackageCommand`` on the current OS use the
non-leaking build mode.

The tests invoke ``PackageCommand._build_*_pyinstaller`` (or, on Windows,
``_build_native_executable_nuitka``) directly instead of shelling out to
``poetry run ccwb package``. Direct invocation is faster, avoids the
config-loading ceremony that ``ccwb package`` needs, and produces
cleaner failure messages when something regresses.

Each binary has three sub-tests, ordered from cheapest to most
expensive so failures surface quickly:

1. ``test_<binary>_build_produces_directory_not_single_file``
   Fast structural check on the build artifact. Catches "someone
   deleted ``--onedir``" without needing to run anything.

2. ``test_<binary>_launcher_exists_inside_dist``
   Confirms the launcher binary is at the expected path inside the
   dist directory, which is what the install script will move.

3. ``test_<binary>_running_creates_no_mei_dir``
   Runs the real binary with a benign argument (``--version``),
   polls the tmp directory during execution, and asserts that no
   MEI-style extraction directory ever appears. Because PyInstaller
   / Nuitka clean up on normal exit, checking only after the process
   exits would miss the leak; we must poll while the binary is alive.

All tests are marked ``slow``. They are opt-in via ``pytest -m slow``.

Each test builds its own binary in its own ``tmp_path``. This is
intentional: separate builds produce clearer failure attribution at
the cost of doubling total build time. On Linux each PyInstaller build
is ~30-60s; on macOS similar; on Windows Nuitka is slower.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

from claude_code_with_bedrock.cli.commands.package import PackageCommand


# ---------------------------------------------------------------------------
# Constants (kept in sync with test_no_mei_leak.py; small enough to duplicate)
# ---------------------------------------------------------------------------

# Leak-directory prefix produced by PyInstaller ``--onefile`` bootloader.
_PYINSTALLER_LEAK_PREFIX = "_MEI"

# Leak-directory prefix produced by Nuitka ``--onefile`` runtime.
_NUITKA_LEAK_PREFIX = "onefile_"

# How long to wait for a binary to finish executing before giving up.
# The real binaries exit in <1s when passed ``--version``; 30s is a
# generous ceiling that also covers slow CI environments.
_BINARY_EXIT_TIMEOUT_S = 30.0

# Poll interval while watching for MEI dirs to appear during execution.
# Empirically, PyInstaller onefile extraction finishes within ~20-50ms
# on Linux; polling at 5ms gives us many chances to observe the dir
# before the process exits and atexit cleans it up.
_LEAK_POLL_INTERVAL_S = 0.005


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_root() -> Path:
    """OS temp dir where extraction dirs (if any) would land."""
    import tempfile
    return Path(tempfile.gettempdir())


def _list_leaks(prefix: str) -> set[Path]:
    """Set of tmp directories currently starting with ``prefix``."""
    return {
        p for p in _tmp_root().iterdir()
        if p.name.startswith(prefix) and p.is_dir()
    }


def _poetry_available() -> bool:
    """Whether ``poetry`` is on PATH.

    The build methods shell out to ``poetry run pyinstaller``; without
    Poetry installed and on PATH they cannot run.
    """
    return shutil.which("poetry") is not None


def _pyinstaller_available() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        return False


def _current_platform_uses_pyinstaller() -> bool:
    """True on Linux and macOS. False on Windows (uses Nuitka)."""
    return platform.system() in {"Linux", "Darwin"}


def _source_dir() -> Path:
    """Path to the ``source/`` directory containing pyproject.toml.

    The build methods invoke ``poetry run pyinstaller`` with ``cwd`` set
    to the directory containing ``pyproject.toml``. Locating this
    explicitly (rather than trusting the ambient cwd of the pytest run)
    makes the tests robust to being launched from any directory. This
    is a lesson we've learned before: tests that depend on ambient cwd
    are painful to debug when they fail on someone else's machine.
    """
    # This file lives at source/tests/test_real_binary_no_mei_leak.py.
    # Two ``.parent`` calls take us to source/.
    return Path(__file__).resolve().parent.parent


def _make_package_command_for_build() -> PackageCommand:
    """Instantiate ``PackageCommand`` and stub the option() lookup.

    ``self.option("build-verbose")`` inside the build methods will
    raise ``AttributeError`` on a bare ``PackageCommand()`` because
    Cleo hasn't wired up the IO context. Since we're calling internal
    build methods directly, we bypass Cleo entirely by replacing
    ``option`` on the instance.
    """
    cmd = PackageCommand()
    cmd.option = lambda name, default=None: False if name == "build-verbose" else default  # type: ignore[method-assign]
    return cmd


def _build_credential_process(build_output_dir: Path) -> Path:
    """Build the real ``credential-process`` binary for the current platform.

    Returns the launcher path that end users will invoke — for onedir
    this is a file inside the produced directory.
    """
    cmd = _make_package_command_for_build()
    source_dir = _source_dir()

    # PackageCommand._build_*_pyinstaller / _build_native_executable_nuitka
    # do ``cwd=source_dir`` internally when invoking poetry, so we don't
    # need to os.chdir. But some code paths rely on ``Path(__file__)``
    # resolving from the installed package location — that always works
    # regardless of cwd — so no additional setup is required beyond
    # option() stubbing above.

    system = platform.system()
    if system == "Linux":
        return cmd._build_linux_pyinstaller(build_output_dir)
    if system == "Darwin":
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
        return cmd._build_macos_pyinstaller(build_output_dir, arch)
    if system == "Windows":
        return cmd._build_native_executable_nuitka(build_output_dir, "windows")
    raise RuntimeError(f"unsupported platform for build: {system}")


def _build_otel_helper(build_output_dir: Path) -> Path:
    """Build the real ``otel-helper`` binary for the current platform.

    Returns the launcher path.
    """
    cmd = _make_package_command_for_build()

    system = platform.system()
    if system == "Linux":
        return cmd._build_otel_helper_pyinstaller(build_output_dir, "linux", None)
    if system == "Darwin":
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
        return cmd._build_otel_helper_pyinstaller(build_output_dir, "macos", arch)
    if system == "Windows":
        # otel-helper on Windows also builds via Nuitka; the method
        # signature mirrors credential-process. See package.py.
        return cmd._build_native_executable_nuitka(build_output_dir, "windows")
    raise RuntimeError(f"unsupported platform for build: {system}")


def _run_binary_and_watch_for_leaks(
    binary: Path,
    prefix: str,
    args: list[str] | None = None,
) -> tuple[int, set[Path]]:
    """Run ``binary`` and poll the tmp dir for new leak-prefixed directories.

    Returns ``(return_code, dirs_observed_during_execution)``.

    The polling loop is a busy-wait with a small sleep. Empirically,
    PyInstaller extraction finishes in <100ms on Linux, so polling
    every 5ms catches the dir with dozens of samples to spare even
    against a fast-exiting binary.

    ``dirs_observed_during_execution`` is the union of every new
    directory seen at any poll point while the process was alive.
    This is stronger than "check after exit" because atexit cleanup
    on a clean exit removes the dir before we could otherwise see it.
    """
    args = args or []
    before = _list_leaks(prefix)

    proc = subprocess.Popen(
        [str(binary), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    observed_during: set[Path] = set()
    deadline = time.monotonic() + _BINARY_EXIT_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            during = _list_leaks(prefix)
            observed_during |= (during - before)
            time.sleep(_LEAK_POLL_INTERVAL_S)
        else:
            # Process didn't exit within timeout. Kill it and record
            # anything else that appeared. This is defensive; ``--version``
            # should exit in well under a second.
            proc.kill()
            proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    # One last sweep before we return — the process may have created
    # the dir right at the end of its life.
    final_during = _list_leaks(prefix) - before
    observed_during |= final_during

    # Clean up anything the run leaked so we don't pollute /tmp for
    # subsequent tests or subsequent developers.
    after = _list_leaks(prefix)
    for d in after - before:
        shutil.rmtree(d, ignore_errors=True)

    return proc.returncode, observed_during


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------


_SKIP_UNSUPPORTED_PLATFORM = pytest.mark.skipif(
    platform.system() not in {"Linux", "Darwin", "Windows"},
    reason="unsupported test platform",
)


_SKIP_NO_POETRY = pytest.mark.skipif(
    not _poetry_available(),
    reason="poetry not on PATH; build methods invoke 'poetry run'",
)


_SKIP_NO_PYINSTALLER = pytest.mark.skipif(
    _current_platform_uses_pyinstaller() and not _pyinstaller_available(),
    reason="PyInstaller not installed",
)


def _leak_prefix_for_current_platform() -> str:
    """The tmp-dir prefix used by the current platform's build backend."""
    return (
        _PYINSTALLER_LEAK_PREFIX
        if _current_platform_uses_pyinstaller()
        else _NUITKA_LEAK_PREFIX
    )


# ---------------------------------------------------------------------------
# Shared assertion helpers
# ---------------------------------------------------------------------------


def _assert_artifact_is_directory(launcher_path: Path) -> None:
    """The build must produce a directory-shaped artifact, not a single file.

    For onedir / standalone builds, ``launcher_path`` is a file INSIDE a
    directory; the directory is the shipping unit. For onefile builds,
    ``launcher_path`` IS the shipping unit and its parent is the dist
    root that also contains other unrelated files.

    The invariant: ``launcher_path.parent`` must be a directory whose
    ONLY purpose is to hold this binary and its ``_internal`` (PyInstaller)
    or ``.dist`` (Nuitka) sibling. We check by asserting the parent
    directory name matches the launcher name (PyInstaller onedir puts
    ``dist/{name}/{name}``), OR the parent name ends in ``.dist``
    (Nuitka standalone puts ``{name}.dist/{name}``).
    """
    parent = launcher_path.parent
    launcher_name = launcher_path.name

    # Windows executables end in .exe; strip for comparison.
    launcher_stem = launcher_name[:-4] if launcher_name.endswith(".exe") else launcher_name

    is_pyinstaller_onedir = parent.name == launcher_stem
    is_nuitka_standalone = parent.name == f"{launcher_stem}.dist"

    assert is_pyinstaller_onedir or is_nuitka_standalone, (
        f"launcher at {launcher_path} is not inside a onedir/standalone "
        f"directory. parent={parent.name!r}, expected {launcher_stem!r} "
        f"(PyInstaller onedir) or {launcher_stem + '.dist'!r} "
        f"(Nuitka standalone). If parent is 'dist' or similar, the build "
        f"is still producing a single-file executable and the MEI leak "
        f"fix has regressed."
    )


def _assert_launcher_is_executable_file(launcher_path: Path) -> None:
    """The launcher file must exist and be executable (Unix) or an .exe (Windows)."""
    assert launcher_path.exists(), f"launcher not found: {launcher_path}"
    assert launcher_path.is_file(), f"launcher is not a file: {launcher_path}"
    if platform.system() != "Windows":
        mode = launcher_path.stat().st_mode
        assert mode & 0o111, (
            f"launcher is not executable: {launcher_path} (mode={oct(mode)})"
        )


def _assert_no_leak_during_run(binary: Path, args: list[str] | None = None) -> None:
    """Run the binary and assert no leak-prefixed tmp dir ever appears."""
    prefix = _leak_prefix_for_current_platform()
    returncode, observed = _run_binary_and_watch_for_leaks(
        binary, prefix, args=args,
    )
    assert not observed, (
        f"binary at {binary} created {len(observed)} leak-prefixed tmp "
        f"directory(s) during execution ({observed}). This means the "
        f"build is still using onefile / non-standalone mode. Extraction "
        f"directories left in /tmp will accumulate over time and can "
        f"fill the disk. Expected zero at every poll point."
    )
    # We don't assert on returncode: ``--version`` exits 0, but if a
    # future refactor changes the flag semantics, a non-zero exit
    # doesn't invalidate the leak assertion.


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


pytestmark = [
    pytest.mark.slow,
    _SKIP_UNSUPPORTED_PLATFORM,
    _SKIP_NO_POETRY,
    _SKIP_NO_PYINSTALLER,
]


class TestCredentialProcessBinary:
    """The shipped ``credential-process`` binary must not leak tmp dirs."""

    def test_credential_process_build_produces_directory_not_single_file(
        self, tmp_path: Path
    ) -> None:
        """Structural check: build output is a directory, not a bare executable."""
        launcher = _build_credential_process(tmp_path)
        _assert_artifact_is_directory(launcher)

    def test_credential_process_launcher_exists_inside_dist(
        self, tmp_path: Path
    ) -> None:
        """The launcher is present and executable inside the produced dist dir."""
        launcher = _build_credential_process(tmp_path)
        _assert_launcher_is_executable_file(launcher)

    def test_credential_process_running_creates_no_mei_dir(
        self, tmp_path: Path
    ) -> None:
        """Runtime check: no MEI/onefile dir appears at any moment during ``--version``."""
        launcher = _build_credential_process(tmp_path)
        _assert_no_leak_during_run(launcher, args=["--version"])


class TestOtelHelperBinary:
    """The shipped ``otel-helper`` binary must not leak tmp dirs."""

    def test_otel_helper_build_produces_directory_not_single_file(
        self, tmp_path: Path
    ) -> None:
        """Structural check: build output is a directory, not a bare executable."""
        launcher = _build_otel_helper(tmp_path)
        _assert_artifact_is_directory(launcher)

    def test_otel_helper_launcher_exists_inside_dist(
        self, tmp_path: Path
    ) -> None:
        """The launcher is present and executable inside the produced dist dir."""
        launcher = _build_otel_helper(tmp_path)
        _assert_launcher_is_executable_file(launcher)

    def test_otel_helper_running_creates_no_mei_dir(
        self, tmp_path: Path
    ) -> None:
        """Runtime check: no MEI/onefile dir appears at any moment during ``--help``.

        otel-helper doesn't have a ``--version`` flag, but it does
        respond to ``--help`` by exiting immediately. Any flag that
        causes a fast, clean exit works for this assertion because
        PyInstaller extraction happens before the user's code runs.
        """
        launcher = _build_otel_helper(tmp_path)
        _assert_no_leak_during_run(launcher, args=["--help"])
