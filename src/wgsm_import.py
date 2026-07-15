"""Static WindowsGSM plugin importer.

WindowsGSM plugins are C# programs. DGSM never compiles or executes them. This
module reads the documented declarative fields and creates a DGSM template plus
an audit report. Steam plugins can use DGSM's existing SteamCMD path; custom
installers remain blocked until a native DGSM install.py is supplied.
"""

from __future__ import annotations

import hashlib
import ipaddress
import io
import json
import os
import re
import shlex
import socket
import ssl
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from template_utils import build_template_config, normalize_steam_update_args, write_template_files


MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_COUNT = 256
MAX_TOTAL_SOURCE_BYTES = 16 * 1024 * 1024
MAX_PARAMETER_COUNT = 256
MAX_PARAMETER_LENGTH = 4096
REPORT_FILE = "windowsgsm_import.json"


class WgsmImportError(ValueError):
    pass


_CS_STRING_PATTERN = re.compile(
    r'(?:(?:\$@|@\$|\$|@)?"(?:\\.|""|[^"\\])*")',
    re.DOTALL,
)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _validate_public_https_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise WgsmImportError("Only HTTPS plugin URLs are accepted")
    if parsed.username or parsed.password or not parsed.hostname:
        raise WgsmImportError("Plugin URL must not contain credentials")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise WgsmImportError(f"Could not resolve plugin host: {parsed.hostname}") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise WgsmImportError("Plugin URLs must resolve to public internet addresses")
    return parsed


def _download_https(url: str) -> Tuple[bytes, str]:
    _validate_public_https_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "DGSM-WindowsGSM-Importer/1.0"})
    with urllib.request.urlopen(request, timeout=45, context=_ssl_context()) as response:
        final_url = str(response.geturl() or url)
        _validate_public_https_url(final_url)
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_DOWNLOAD_BYTES:
            raise WgsmImportError("Plugin download is larger than 25 MB")
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise WgsmImportError("Plugin download is larger than 25 MB")
    return data, final_url


def _github_source(url: str) -> Tuple[bytes, str]:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise WgsmImportError("GitHub URL must contain owner and repository")
    owner, repository = parts[0], parts[1].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
        raise WgsmImportError("GitHub owner or repository contains unsupported characters")
    if len(parts) >= 5 and parts[2] == "blob":
        branch = parts[3]
        path = "/".join(parts[4:])
        raw = f"https://raw.githubusercontent.com/{owner}/{repository}/{branch}/{path}"
        return _download_https(raw)

    api_url = f"https://api.github.com/repos/{owner}/{repository}"
    metadata_raw, _ = _download_https(api_url)
    try:
        metadata = json.loads(metadata_raw.decode("utf-8"))
        branch = str(metadata.get("default_branch", "") or "main")
    except Exception as exc:
        raise WgsmImportError(f"Could not read GitHub repository metadata: {exc}") from exc
    archive = f"https://github.com/{owner}/{repository}/archive/refs/heads/{urllib.parse.quote(branch, safe='')}.zip"
    return _download_https(archive)


def _decode_source(data: bytes, label: str) -> str:
    if len(data) > MAX_SOURCE_BYTES:
        raise WgsmImportError(f"C# source is larger than 2 MB: {label}")
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise WgsmImportError(f"Could not decode C# source: {label}")


def _source_score(text: str) -> int:
    return (
        (50 if "new Plugin" in text else 0)
        + (35 if re.search(r"\bAppId\b\s*(?:=>|=)", text) else 0)
        + (35 if re.search(r"\bStartPath\b\s*(?:=>|=)", text) else 0)
        + (20 if "SteamCMDAgent" in text else 0)
        + (10 if "WindowsGSM.Plugins" in text else 0)
    )


def _pick_source(candidates: List[Tuple[str, bytes]], source_reference: str) -> Tuple[str, str]:
    parsed: List[Tuple[int, str, str]] = []
    for label, raw in candidates:
        try:
            text = _decode_source(raw, label)
        except WgsmImportError:
            continue
        parsed.append((_source_score(text), label, text))
    if not parsed:
        raise WgsmImportError("No readable .cs plugin source found")
    parsed.sort(key=lambda item: (item[0], -len(item[1])), reverse=True)
    score, label, text = parsed[0]
    if score < 35:
        raise WgsmImportError("No WindowsGSM plugin declarations found in the C# sources")
    reference = f"{source_reference}#{label}" if label else source_reference
    return text, reference


def _sources_from_zip(data: bytes) -> List[Tuple[str, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            candidates: List[Tuple[str, bytes]] = []
            total_size = 0
            for info in archive.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".cs"):
                    continue
                if info.file_size > MAX_SOURCE_BYTES:
                    continue
                total_size += info.file_size
                if len(candidates) >= MAX_SOURCE_COUNT or total_size > MAX_TOTAL_SOURCE_BYTES:
                    raise WgsmImportError("Plugin archive contains too many or too large C# sources")
                candidates.append((info.filename, archive.read(info)))
            return candidates
    except zipfile.BadZipFile as exc:
        raise WgsmImportError("Plugin archive is not a valid ZIP file") from exc


def load_wgsm_source(source: str, *, allow_local: bool = True) -> Tuple[str, str]:
    value = str(source or "").strip().strip('"')
    if not value:
        raise WgsmImportError("Plugin source is required")

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() in {"http", "https"}:
        if parsed.scheme.lower() != "https":
            raise WgsmImportError("Only HTTPS plugin URLs are accepted")
        if parsed.netloc.lower() == "github.com":
            data, reference = _github_source(value)
        else:
            data, reference = _download_https(value)
        candidates = _sources_from_zip(data) if zipfile.is_zipfile(io.BytesIO(data)) else [(Path(parsed.path).name, data)]
        return _pick_source(candidates, reference)

    if not allow_local:
        raise WgsmImportError("Discord and Web UI imports require an HTTPS URL")

    path = Path(os.path.expanduser(value)).resolve()
    if not path.exists():
        raise WgsmImportError(f"Plugin source does not exist: {path}")
    if path.is_dir():
        candidates = []
        total_size = 0
        for item in path.rglob("*.cs"):
            if item.is_file() and item.stat().st_size <= MAX_SOURCE_BYTES:
                total_size += item.stat().st_size
                if len(candidates) >= MAX_SOURCE_COUNT or total_size > MAX_TOTAL_SOURCE_BYTES:
                    raise WgsmImportError("Plugin folder contains too many or too large C# sources")
                candidates.append((str(item.relative_to(path)), item.read_bytes()))
        return _pick_source(candidates, str(path))
    data = path.read_bytes()
    candidates = _sources_from_zip(data) if path.suffix.lower() == ".zip" else [(path.name, data)]
    return _pick_source(candidates, str(path))


def _strip_csharp_comments(source: str) -> str:
    out: List[str] = []
    i = 0
    state = "normal"
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if state == "normal":
            if ch == "/" and nxt == "/":
                out.extend("  ")
                i += 2
                state = "line"
                continue
            if ch == "/" and nxt == "*":
                out.extend("  ")
                i += 2
                state = "block"
                continue
            if ch == '"':
                state = "verbatim" if i > 0 and source[i - 1] == "@" else "string"
            elif ch == "'":
                state = "char"
            out.append(ch)
            i += 1
            continue
        if state == "line":
            if ch in "\r\n":
                out.append(ch)
                state = "normal"
            else:
                out.append(" ")
            i += 1
            continue
        if state == "block":
            if ch == "*" and nxt == "/":
                out.extend("  ")
                i += 2
                state = "normal"
            else:
                out.append(ch if ch in "\r\n" else " ")
                i += 1
            continue
        out.append(ch)
        if state == "verbatim":
            if ch == '"' and nxt == '"':
                out.append(nxt)
                i += 2
                continue
            if ch == '"':
                state = "normal"
        elif state in {"string", "char"}:
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if (state == "string" and ch == '"') or (state == "char" and ch == "'"):
                state = "normal"
        i += 1
    return "".join(out)


def _decode_csharp_string(token: str) -> str:
    quote = token.find('"')
    if quote < 0 or not token.endswith('"'):
        return ""
    prefix = token[:quote]
    body = token[quote + 1 : -1]
    if "@" in prefix:
        return body.replace('""', '"')
    out: List[str] = []
    i = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "\\": "\\", '"': '"', "'": "'"}
    while i < len(body):
        if body[i] != "\\" or i + 1 >= len(body):
            out.append(body[i])
            i += 1
            continue
        code = body[i + 1]
        if code in escapes:
            out.append(escapes[code])
            i += 2
            continue
        if code == "u" and i + 5 < len(body):
            try:
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        out.extend(("\\", code))
        i += 2
    return "".join(out)


def _assignment_rhs(text: str, field: str) -> str:
    match = re.search(rf"\b{re.escape(field)}\b\s*(?:=>|=)\s*(.*?);", text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _string_field(text: str, field: str) -> str:
    rhs = _assignment_rhs(text, field)
    match = _CS_STRING_PATTERN.search(rhs)
    return _decode_csharp_string(match.group(0)) if match else ""


def _bool_field(text: str, field: str, default: bool = False) -> bool:
    rhs = _assignment_rhs(text, field)
    match = re.search(r"\b(true|false)\b", rhs, re.IGNORECASE)
    return match.group(1).lower() == "true" if match else default


def _plugin_metadata(text: str) -> Dict[str, str]:
    match = re.search(r"new\s+Plugin\s*\{(.*?)\}\s*;", text, re.DOTALL)
    block = match.group(1) if match else ""
    return {
        key: _string_field(block, key)
        for key in ("name", "author", "description", "version", "url", "color")
    }


def _split_parameters(value: str) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        return [item for item in shlex.split(text, posix=True) if item]
    except ValueError:
        return [item for item in text.split() if item]


def _dynamic_parameters(text: str, defaults: Dict[str, str], additional: str, warnings: List[str]) -> List[str]:
    pattern = re.compile(
        rf"\b(?:param|args|arguments)\s*\+?=\s*(?P<literal>{_CS_STRING_PATTERN.pattern})\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    chunks: List[str] = []
    unresolved: set[str] = set()
    values = {
        "ServerParam": additional,
        "ServerPort": defaults.get("port", ""),
        "ServerQueryPort": defaults.get("query_port", ""),
        "ServerMaxPlayer": defaults.get("max_players", ""),
        "ServerName": defaults.get("server_name", ""),
        "ServerMap": defaults.get("default_map", ""),
    }

    for match in pattern.finditer(text):
        token = match.group("literal")
        rendered = _decode_csharp_string(token)

        def replace_field(field_match: re.Match[str]) -> str:
            field = field_match.group(1)
            value = values.get(field)
            if value is None or (not value and field != "ServerParam"):
                unresolved.add(field)
                return f"__DGSM_UNSET_{field}__"
            return value

        rendered = re.sub(r"\{\s*_serverData\.(\w+)(?:[^}]*)\}", replace_field, rendered)
        chunks.append(rendered)

    if not chunks:
        return _split_parameters(additional)

    parameters = _split_parameters(" ".join(chunks))
    filtered = [
        item
        for item in parameters[:MAX_PARAMETER_COUNT]
        if "__DGSM_UNSET_" not in item and len(item) <= MAX_PARAMETER_LENGTH
    ]
    if len(parameters) > MAX_PARAMETER_COUNT or any(len(item) > MAX_PARAMETER_LENGTH for item in parameters):
        warnings.append("Skipped excessive WindowsGSM start parameters")
    if unresolved:
        warnings.append("Skipped dynamic WindowsGSM values without a safe DGSM default: " + ", ".join(sorted(unresolved)))
    return filtered


def inspect_wgsm_source(source: str, source_reference: str = "inline") -> Dict[str, object]:
    clean = _strip_csharp_comments(source)
    metadata = _plugin_metadata(clean)
    class_match = re.search(r"\bclass\s+(\w+)\s*(?::\s*([^\{]+))?", clean)
    class_name = class_match.group(1) if class_match else "WindowsGSMPlugin"
    bases = class_match.group(2) if class_match and class_match.group(2) else ""
    inherits_steam = "SteamCMDAgent" in bases

    app_id_raw = _string_field(clean, "AppId")
    app_id_parts = _split_parameters(app_id_raw)
    app_id = app_id_parts[0] if app_id_parts and app_id_parts[0].isdigit() else app_id_raw
    steam_update_args = normalize_steam_update_args(app_id_parts[1:])
    fields: Dict[str, object] = {
        "class_name": class_name,
        "app_id": app_id,
        "app_id_raw": app_id_raw,
        "steam_update_args": steam_update_args,
        "start_path": _string_field(clean, "StartPath"),
        "full_name": _string_field(clean, "FullName"),
        "additional": _string_field(clean, "Additional"),
        "server_name": _string_field(clean, "ServerName"),
        "port": _string_field(clean, "Port"),
        "query_port": _string_field(clean, "QueryPort"),
        "default_map": _string_field(clean, "Defaultmap"),
        "max_players": _string_field(clean, "Maxplayers"),
        "login_anonymous": _bool_field(clean, "loginAnonymous", True),
        "inherits_steamcmd": inherits_steam,
    }
    query_match = re.search(r"\bQueryMethod\b\s*(?:=>|=)\s*new\s+(\w+)", clean)
    fields["query_method"] = query_match.group(1) if query_match else ""

    warnings: List[str] = []
    app_id = str(fields["app_id"] or "").strip()
    start_path = str(fields["start_path"] or "").strip()
    is_steam = bool(inherits_steam and app_id.isdigit())
    if not start_path:
        warnings.append("StartPath was not a static string and must be configured before use")
    if inherits_steam and not app_id.isdigit():
        warnings.append("SteamCMDAgent plugin has no static numeric AppId")
    if app_id_parts[1:] and steam_update_args != app_id_parts[1:]:
        warnings.append("Unsupported SteamCMD AppId options were discarded; only -beta and -betapassword are allowed")
    if is_steam and not bool(fields["login_anonymous"]):
        warnings.append("SteamCMD login is not anonymous; credentials must be added to the DGSM template")
    if str(fields["query_method"] or ""):
        warnings.append(f"WindowsGSM query method {fields['query_method']} is recorded but not executed by the importer")
    if re.search(r"\b(?:File\.Write|File\.Copy|WebClient|HttpClient|DownloadFile|ZipFile|JObject)\b", clean):
        warnings.append("Plugin contains custom file, configuration, or download logic that was not translated")
    if re.search(r"\bTask\s+Stop\s*\(", clean):
        warnings.append("WindowsGSM Stop() is not imported; DGSM keeps its existing process stop logic")

    defaults = {
        key: str(fields.get(key, "") or "")
        for key in ("server_name", "port", "query_port", "default_map", "max_players")
    }
    parameters = _dynamic_parameters(clean, defaults, str(fields["additional"] or ""), warnings)
    deduplicated: List[str] = []
    for item in parameters:
        if item not in deduplicated:
            deduplicated.append(item)

    compatibility = "steam_ready" if is_steam and start_path else "review_required"
    if not is_steam:
        warnings.append("Custom/non-Steam Install() and Update() methods require a native DGSM install.py adapter")

    return {
        "format_version": 1,
        "source": source_reference,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "plugin": metadata,
        "fields": fields,
        "parameters": deduplicated,
        "compatibility": compatibility,
        "warnings": warnings,
    }


def _safe_template_name(value: str) -> str:
    name = re.sub(r"^WindowsGSM[._ -]*", "", str(value or "").strip(), flags=re.IGNORECASE)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip(" ._-")
    if not name:
        name = "Plugin"
    return f"WGSM_{name}"[:80]


def _review_guard(plugin_name: str) -> str:
    return f'''"""Generated review guard for an imported WindowsGSM plugin."""


def install(serverfiles, ctx):
    raise RuntimeError(
        "{plugin_name}: WindowsGSM custom C# Install/Update logic was not executed. "
        "Add and review a native DGSM install.py before installing this template."
    )
'''


def import_wgsm_plugin(
    source: str,
    templates_dir: str,
    *,
    template_name: str = "",
    allow_local: bool = True,
) -> Dict[str, object]:
    source_text, source_reference = load_wgsm_source(source, allow_local=allow_local)
    report = inspect_wgsm_source(source_text, source_reference)
    metadata = report["plugin"] if isinstance(report.get("plugin"), dict) else {}
    fields = report["fields"] if isinstance(report.get("fields"), dict) else {}
    base_name = template_name or str(metadata.get("name") or fields.get("full_name") or fields.get("class_name") or "")
    final_name = _safe_template_name(base_name)

    app_id = str(fields.get("app_id", "") or "").strip()
    steam_ready = report.get("compatibility") == "steam_ready"
    if not steam_ready:
        slug = re.sub(r"[^a-z0-9]+", "_", final_name.lower()).strip("_")
        app_id = f"wgsm_{slug or 'plugin'}"

    template_cfg = build_template_config(
        app_id=app_id,
        executable=str(fields.get("start_path", "") or ""),
        parameters=[str(item) for item in report.get("parameters", [])],
        auto_start=False,
        auto_update=steam_ready,
        auto_restart=True,
        stop_time="05:00",
        restart_after_stop=False,
        steam_update_args=fields.get("steam_update_args"),
    )

    root = Path(templates_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / final_name).resolve()
    if destination.parent != root:
        raise WgsmImportError("Invalid template destination")
    if destination.exists():
        raise WgsmImportError(f"Template already exists: {final_name}")

    report["imported_at"] = datetime.now(timezone.utc).isoformat()
    report["template_name"] = final_name
    report["template_config"] = template_cfg

    temp_dir = Path(tempfile.mkdtemp(prefix=".wgsm-import-", dir=str(root)))
    try:
        write_template_files(str(temp_dir), template_cfg)
        with open(temp_dir / REPORT_FILE, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        if not steam_ready:
            with open(temp_dir / "install.py", "w", encoding="utf-8") as handle:
                handle.write(_review_guard(final_name))
        os.replace(temp_dir, destination)
    except Exception:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return report


def format_import_summary(report: Dict[str, object]) -> str:
    fields = report.get("fields") if isinstance(report.get("fields"), dict) else {}
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    status = str(report.get("compatibility", "review_required"))
    app_id = str(fields.get("app_id", "") or "-")
    executable = str(fields.get("start_path", "") or "-")
    suffix = f" | {len(warnings)} warning(s)" if warnings else ""
    return f"{report.get('template_name', 'Template')} | {status} | AppId {app_id} | {executable}{suffix}"
