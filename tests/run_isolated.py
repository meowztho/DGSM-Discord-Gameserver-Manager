"""Run tests against a disposable source copy, never the installed DGSM instance.

Usage: py -3 tests/run_isolated.py [test_file_pattern]
The child blocks external connections, process control, and writes outside its
temporary workspace. No credentials, runtime configs, PID caches or game data
are copied. The workspace is retained for inspecting failures.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def _run_child(pattern: str) -> int:
    root = Path(__file__).resolve().parents[1]
    allowed = os.path.normcase(os.path.realpath(root))

    def check_path(path):
        if isinstance(path, int):
            return
        resolved = os.path.normcase(os.path.realpath(os.fsdecode(path)))
        if os.path.commonpath([allowed, resolved]) != allowed:
            raise RuntimeError(f"Test attempted a write outside its workspace: {resolved}")

    def guard(event, args):
        if event in {"socket.connect", "socket.bind"}:
            caller = sys._getframe(1)
            if caller.f_code.co_name == "_fallback_socketpair" and caller.f_globals.get("__name__") == "socket":
                return  # Windows asyncio's private self-pipe.
            raise RuntimeError("Tests cannot connect to live services")
        if event in {"socket.getaddrinfo", "subprocess.Popen", "os.system", "os.kill", "os.startfile"}:
            raise RuntimeError(f"Tests cannot perform external side effects: {event}")
        if event == "open":
            path, mode, flags = args
            if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                check_path(path)
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"}:
            check_path(args[0])
        elif event in {"os.rename", "os.link"}:
            check_path(args[0])
            check_path(args[1])
        elif event == "os.symlink":
            check_path(args[1])
        elif event == "sqlite3.connect" and args[0] != ":memory:":
            check_path(args[0])

    sys.addaudithook(guard)
    import psutil

    def no_process_control(*args, **kwargs):
        raise RuntimeError("Tests must simulate game processes")

    for name in ("kill", "terminate", "send_signal", "suspend", "resume"):
        setattr(psutil.Process, name, no_process_control)
    psutil.process_iter = no_process_control
    sys.path.insert(0, str(root / "src"))
    import unittest
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return int(not result.wasSuccessful())


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        return _run_child(sys.argv[2])
    pattern = sys.argv[1] if len(sys.argv) > 1 else "test_*.py"
    source_root = Path(__file__).resolve().parents[1]
    workspace = Path(tempfile.mkdtemp(prefix="dgsm-tests-"))
    for folder in ("src", "tests"):
        # Runtime game installations can live below src/steam. Never recurse
        # into them or copy user plugin/game data into a test snapshot.
        for source in (source_root / folder).glob("*.py"):
            if source.is_symlink():
                raise RuntimeError(f"Refusing linked test/source file: {source}")
            target = workspace / source.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    temp_dir = workspace / "tmp"
    temp_dir.mkdir()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("DGSM_") or key in {"DISCORD_TOKEN", "DISCORD_CHANNEL", "DISCORD_GUILD", "DOMAIN", "ADMIN_CHANNEL", "ENCRYPTION_KEY", "PYTHONPATH", "STEAMCMD_PATH"}:
            env.pop(key, None)
    env.update(TMP=str(temp_dir), TEMP=str(temp_dir), TMPDIR=str(temp_dir), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    print(f"Isolated workspace: {workspace}", flush=True)
    result = subprocess.run(
        [sys.executable, "-B", str(workspace / "tests" / "run_isolated.py"), "--child", pattern],
        cwd=workspace, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    output = result.stdout + result.stderr
    (workspace / "test-results.txt").write_text(output, encoding="utf-8")
    print(output)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
