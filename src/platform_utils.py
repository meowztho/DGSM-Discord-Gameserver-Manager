import os
import sys
from typing import List, Literal

PlatformKind = Literal["windows", "linux", "other"]


def runtime_platform() -> PlatformKind:
    """Returns a normalized runtime platform identifier."""
    plat = (sys.platform or "").lower()
    if os.name == "nt" or plat.startswith("win"):
        return "windows"
    if plat.startswith("linux"):
        return "linux"
    return "other"


def is_windows() -> bool:
    return runtime_platform() == "windows"


def is_linux() -> bool:
    return runtime_platform() == "linux"


def runtime_platform_label() -> str:
    kind = runtime_platform()
    if kind == "windows":
        return "Windows"
    if kind == "linux":
        return "Linux"
    return sys.platform or os.name


def normalize_user_path(value: str) -> str:
    """
    Normalizes user-provided paths so both slash styles work on all platforms.
    """
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    normalized = text.replace("\\", os.sep).replace("/", os.sep)
    return os.path.normpath(normalized)


def executable_path_variants(value: str) -> List[str]:
    """
    Returns OS-specific fallback candidates for a configured executable path.
    """
    normalized = normalize_user_path(value)
    if not normalized:
        return []

    directory, filename = os.path.split(normalized)
    stem, ext = os.path.splitext(filename)
    ext_low = ext.lower()
    names: List[str] = []

    def _add(name: str) -> None:
        if name and name not in names:
            names.append(name)

    _add(filename)

    if is_linux():
        if ext_low == ".exe":
            _add(stem)
            _add(f"{stem}.x86_64")
            _add(f"{stem}.sh")
        elif ext_low in (".bat", ".cmd"):
            _add(stem)
            _add(f"{stem}.sh")
        elif ext_low == ".sh":
            _add(stem)
            _add(f"{stem}.x86_64")
        elif not ext_low:
            _add(f"{stem}.x86_64")
            _add(f"{stem}.sh")
    elif is_windows():
        if ext_low in ("", ".sh", ".x86_64"):
            _add(f"{stem}.exe")
            _add(f"{stem}.bat")
            _add(f"{stem}.cmd")

    if not directory:
        return names
    return [os.path.join(directory, name) for name in names]
