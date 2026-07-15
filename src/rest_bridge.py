from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse


_CACHE: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = {}


@dataclass(frozen=True)
class RestEndpoint:
    key: str
    method: str
    path: str


@dataclass(frozen=True)
class RestAction:
    key: str
    method: str
    path: str
    arguments: Tuple[Dict[str, Any], ...]


_ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ARGUMENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_BLOCKED_LIFECYCLE_ACTIONS = {
    "start",
    "stop",
    "shutdown",
    "restart",
    "update",
    "install",
    "remove",
    "delete",
    "force-stop",
    "force_stop",
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int, minimum: int = 0, maximum: int = 300) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, parsed))


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _clean(value: Any, limit: int = 120) -> str:
    text = " ".join(str("" if value is None else value).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _deep_get(data: Any, dotted_key: str) -> Any:
    current = data
    for part in str(dotted_key or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except Exception:
                return None
        else:
            return None
    return current


def _extract_password_from_parameters(parameters: Any, option_name: str) -> str:
    wanted = str(option_name or "").strip()
    if not wanted:
        return ""
    tokens = parameters if isinstance(parameters, list) else str(parameters or "").split()
    for raw in tokens:
        token = str(raw or "").strip()
        if not token:
            continue
        normalized = token[1:] if token.startswith("-") else token
        key, sep, value = normalized.partition("=")
        if sep and key.lower() == wanted.lstrip("-").lower():
            return value.strip()
    return ""


def _auth_header(rest_cfg: Dict[str, Any], server_cfg: Dict[str, Any]) -> Optional[str]:
    auth = rest_cfg.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    auth_type = _as_str(auth.get("type") or "basic").lower()
    if auth_type in {"", "none"}:
        return None
    if auth_type != "basic":
        return None

    username = _as_str(auth.get("username") or "admin")
    password = _as_str(auth.get("password"))
    if not password:
        password_from = _as_str(auth.get("password_from_parameter"))
        password = _extract_password_from_parameters(server_cfg.get("parameters", []), password_from)
    if not username or not password:
        return None

    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _configured_endpoints(rest_cfg: Dict[str, Any]) -> List[RestEndpoint]:
    raw_endpoints = rest_cfg.get("endpoints")
    if not isinstance(raw_endpoints, dict):
        return []

    wanted = rest_cfg.get("poll")
    if isinstance(wanted, list):
        keys = [_as_str(item) for item in wanted if _as_str(item)]
    else:
        keys = [_as_str(key) for key in raw_endpoints.keys()]

    out: List[RestEndpoint] = []
    for key in keys:
        raw = raw_endpoints.get(key)
        if isinstance(raw, str):
            method = "GET"
            path = raw
        elif isinstance(raw, dict):
            method = _as_str(raw.get("method") or "GET").upper()
            path = _as_str(raw.get("path"))
        else:
            continue
        if method != "GET" or not path:
            continue
        out.append(RestEndpoint(key=key, method=method, path=path))
    return out


def _configured_actions(rest_cfg: Dict[str, Any]) -> Dict[str, RestAction]:
    raw_actions = rest_cfg.get("actions")
    if not isinstance(raw_actions, dict) or not _as_bool(raw_actions.get("enabled"), False):
        return {}
    raw_commands = raw_actions.get("commands")
    if not isinstance(raw_commands, dict):
        return {}

    actions: Dict[str, RestAction] = {}
    for raw_key, raw in list(raw_commands.items())[:32]:
        key = _as_str(raw_key).lower()
        if not _ACTION_NAME_RE.fullmatch(key) or key in _BLOCKED_LIFECYCLE_ACTIONS or not isinstance(raw, dict):
            continue
        method = _as_str(raw.get("method") or "POST").upper()
        path = _as_str(raw.get("path"))
        parsed_path = urlparse(path)
        path_segments = [segment.lower() for segment in unquote(parsed_path.path).split("/") if segment]
        if method != "POST" or not path or parsed_path.scheme or parsed_path.netloc:
            continue
        if any(segment in _BLOCKED_LIFECYCLE_ACTIONS for segment in path_segments):
            continue
        raw_arguments = raw.get("arguments")
        arguments: List[Dict[str, Any]] = []
        if isinstance(raw_arguments, list):
            for item in raw_arguments[:8]:
                if not isinstance(item, dict):
                    continue
                name = _as_str(item.get("name")).lower()
                if not _ARGUMENT_NAME_RE.fullmatch(name):
                    continue
                arguments.append(dict(item, name=name))
        actions[key] = RestAction(key=key, method=method, path=path, arguments=tuple(arguments))
    return actions


def describe_rest_actions(server_cfg: Dict[str, Any]) -> List[str]:
    rest_cfg = server_cfg.get("rest_api")
    if not isinstance(rest_cfg, dict) or not _as_bool(rest_cfg.get("enabled"), False):
        return []
    descriptions: List[str] = []
    for key, action in _configured_actions(rest_cfg).items():
        parts = [key]
        for spec in action.arguments:
            name = str(spec.get("name", "arg"))
            required = _as_bool(spec.get("required"), True)
            parts.append(f"<{name}>" if required else f"[{name}]")
        descriptions.append(" ".join(parts))
    return descriptions


def _coerce_action_value(raw: str, spec: Dict[str, Any]) -> Any:
    value_type = _as_str(spec.get("type") or "string").lower()
    max_length = _as_int(spec.get("max_length"), 500, minimum=1, maximum=2000)
    value = str(raw or "")[:max_length]
    choices = spec.get("choices")
    if isinstance(choices, list) and choices:
        allowed = [str(item) for item in choices[:64]]
        if value not in allowed:
            raise ValueError(f"{spec.get('name')}: expected one of {', '.join(allowed[:8])}")
    if value_type == "integer":
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{spec.get('name')}: integer required") from exc
        minimum = _as_int(spec.get("minimum"), -2147483648, minimum=-2147483648, maximum=2147483647)
        maximum = _as_int(spec.get("maximum"), 2147483647, minimum=-2147483648, maximum=2147483647)
        if parsed < minimum or parsed > maximum:
            raise ValueError(f"{spec.get('name')}: must be between {minimum} and {maximum}")
        return parsed
    if value_type == "boolean":
        lowered = value.lower()
        if lowered not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"{spec.get('name')}: boolean required")
        return lowered in {"1", "true", "yes", "on"}
    return value


def _action_body(action: RestAction, values: List[str]) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    value_index = 0
    for spec in action.arguments:
        name = str(spec.get("name", ""))
        consume_rest = _as_bool(spec.get("consume_rest"), False)
        if consume_rest:
            raw = " ".join(values[value_index:])
            value_index = len(values)
        else:
            raw = values[value_index] if value_index < len(values) else ""
            if value_index < len(values):
                value_index += 1
        required = _as_bool(spec.get("required"), True)
        if not raw:
            if required:
                raise ValueError(f"Missing API argument: {name}")
            continue
        body[name] = _coerce_action_value(raw, spec)
    if value_index < len(values):
        raise ValueError("Too many API arguments")
    if not action.arguments and values:
        raise ValueError("This API command accepts no arguments")
    return body


def _http_get_json(url: str, headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read(1024 * 1024)
        if not body:
            return {}
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else {"value": data}


def _http_action(url: str, headers: Dict[str, str], timeout: int, body: Dict[str, Any]) -> Any:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    request_headers = dict(headers)
    if payload is not None:
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=payload, method="POST", headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(1024 * 1024)
        if not raw:
            return {}
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return _clean(text, 500)


def _action_result_message(action_name: str, payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("message", "status", "result", "error"):
            if payload.get(key) not in {None, ""}:
                return f"{action_name}: {_clean(payload.get(key), 300)}"
    if isinstance(payload, str) and payload:
        return f"{action_name}: {_clean(payload, 300)}"
    return f"{action_name}: API command completed"


def _clear_server_cache(server_name: str) -> None:
    for key in list(_CACHE.keys()):
        if key[0] == server_name:
            _CACHE.pop(key, None)


def _cache_config_signature(rest_cfg: Dict[str, Any], server_cfg: Dict[str, Any]) -> str:
    signature_data = {
        "endpoints": rest_cfg.get("endpoints"),
        "poll": rest_cfg.get("poll"),
        "display": rest_cfg.get("display"),
        "timeout_seconds": rest_cfg.get("timeout_seconds"),
        "auth_type": (rest_cfg.get("auth") or {}).get("type") if isinstance(rest_cfg.get("auth"), dict) else None,
        "auth_header": _auth_header(rest_cfg, server_cfg),
    }
    encoded = json.dumps(signature_data, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


async def execute_rest_action(
    server_name: str,
    server_cfg: Dict[str, Any],
    action_name: str,
    arguments: List[str],
    *,
    running: bool,
) -> Tuple[bool, str]:
    rest_cfg = server_cfg.get("rest_api")
    if not isinstance(rest_cfg, dict) or not _as_bool(rest_cfg.get("enabled"), False):
        return False, f"{server_name}: REST API is not enabled"
    if not running:
        return False, f"{server_name}: server is not running"
    actions = _configured_actions(rest_cfg)
    key = _as_str(action_name).lower()
    action = actions.get(key)
    if action is None:
        available = ", ".join(describe_rest_actions(server_cfg)) or "none"
        return False, f"{server_name}: API command not allowed ({available})"

    base_url = _as_str(rest_cfg.get("base_url")).rstrip("/")
    if not base_url:
        return False, f"{server_name}: REST API base_url missing"
    auth_cfg = rest_cfg.get("auth") if isinstance(rest_cfg.get("auth"), dict) else {}
    auth_type = _as_str(auth_cfg.get("type") or "basic").lower()
    auth = _auth_header(rest_cfg, server_cfg)
    if auth_type not in {"", "none"} and not auth:
        return False, f"{server_name}: REST API credentials missing"
    try:
        body = _action_body(action, [str(item) for item in arguments])
    except ValueError as exc:
        return False, f"{server_name}: {exc}"

    headers: Dict[str, str] = {"Accept": "application/json", "Connection": "close"}
    if auth:
        headers["Authorization"] = auth
    timeout = _as_int(rest_cfg.get("timeout_seconds"), 2, minimum=1, maximum=30)
    url = urljoin(base_url + "/", action.path.lstrip("/"))
    try:
        payload = await asyncio.to_thread(_http_action, url, headers, timeout, body)
    except urllib.error.HTTPError as exc:
        return False, f"{server_name}: {key} failed (HTTP {exc.code})"
    except urllib.error.URLError as exc:
        return False, f"{server_name}: {key} failed ({_clean(getattr(exc, 'reason', exc), 140)})"
    except Exception as exc:
        return False, f"{server_name}: {key} failed ({_clean(exc, 140)})"
    _clear_server_cache(server_name)
    return True, f"{server_name}: {_action_result_message(key, payload)}"


def _fetch_snapshot(server_name: str, server_cfg: Dict[str, Any], rest_cfg: Dict[str, Any]) -> Dict[str, Any]:
    base_url = _as_str(rest_cfg.get("base_url")).rstrip("/")
    endpoints = _configured_endpoints(rest_cfg)
    timeout = _as_int(rest_cfg.get("timeout_seconds"), 2, minimum=1, maximum=30)
    headers: Dict[str, str] = {"Accept": "application/json", "Connection": "close"}
    auth = _auth_header(rest_cfg, server_cfg)
    if auth:
        headers["Authorization"] = auth

    sections: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for endpoint in endpoints:
        try:
            url = urljoin(base_url + "/", endpoint.path.lstrip("/"))
            sections[endpoint.key] = _http_get_json(url, headers, timeout)
        except urllib.error.HTTPError as exc:
            errors[endpoint.key] = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            errors[endpoint.key] = _clean(getattr(exc, "reason", exc), 100)
            if not sections:
                break
        except Exception as exc:
            errors[endpoint.key] = _clean(exc, 100)
            if not sections:
                break

    available = bool(sections) and not errors
    status = "ok" if available else ("partial" if sections else "error")
    return {
        "configured": True,
        "enabled": True,
        "available": bool(sections),
        "status": status,
        "summary": _summary(rest_cfg, sections, errors),
        "sections": sections,
        "errors": errors,
        "updated_at": int(time.time()),
    }


def _summary(rest_cfg: Dict[str, Any], sections: Dict[str, Any], errors: Dict[str, str]) -> List[str]:
    display = rest_cfg.get("display")
    fields = []
    if isinstance(display, dict) and isinstance(display.get("ui_card"), list):
        fields = [_as_str(item) for item in display.get("ui_card", []) if _as_str(item)]

    lines: List[str] = []
    for field in fields[:8]:
        value = _deep_get(sections, field)
        if value is not None:
            lines.append(f"{field.split('.')[-1]}: {_clean(value, 80)}")

    if not lines:
        metrics = sections.get("metrics")
        if isinstance(metrics, dict):
            current = metrics.get("currentplayernum")
            maximum = metrics.get("maxplayernum")
            fps = metrics.get("serverfps")
            uptime = metrics.get("uptime")
            if current is not None and maximum is not None:
                lines.append(f"players: {current}/{maximum}")
            if fps is not None:
                lines.append(f"fps: {fps}")
            if uptime is not None:
                lines.append(f"uptime: {uptime}s")
        info = sections.get("info")
        if isinstance(info, dict):
            version = info.get("version")
            if version is not None:
                lines.append(f"version: {_clean(version, 80)}")
        players = sections.get("players")
        if isinstance(players, dict) and isinstance(players.get("players"), list):
            lines.append(f"online players: {len(players.get('players') or [])}")

    if not lines and errors:
        first_key = sorted(errors.keys())[0]
        lines.append(f"{first_key}: {errors[first_key]}")
    return lines[:8]


def empty_snapshot(configured: bool, enabled: bool, reason: str = "") -> Dict[str, Any]:
    return {
        "configured": bool(configured),
        "enabled": bool(enabled),
        "available": False,
        "status": "disabled" if configured and not enabled else "unconfigured",
        "summary": [],
        "sections": {},
        "errors": {"config": reason} if reason else {},
        "updated_at": None,
    }


async def collect_rest_snapshot(server_name: str, server_cfg: Dict[str, Any], running: bool) -> Dict[str, Any]:
    rest_cfg = server_cfg.get("rest_api")
    configured = isinstance(rest_cfg, dict)
    if not configured:
        return empty_snapshot(False, False)
    assert isinstance(rest_cfg, dict)

    enabled = _as_bool(rest_cfg.get("enabled"), False)
    if not enabled:
        return empty_snapshot(True, False)
    if not running and not _as_bool(rest_cfg.get("poll_when_stopped"), False):
        return empty_snapshot(True, True, "server is not running")

    base_url = _as_str(rest_cfg.get("base_url"))
    if not base_url:
        return empty_snapshot(True, True, "base_url missing")
    if not _configured_endpoints(rest_cfg):
        return empty_snapshot(True, True, "no GET endpoints configured")

    cache_seconds = _as_int(rest_cfg.get("cache_seconds"), 120, minimum=30, maximum=900)
    error_cache_seconds = _as_int(rest_cfg.get("error_cache_seconds"), 300, minimum=60, maximum=1800)
    cache_key = (server_name, base_url, _cache_config_signature(rest_cfg, server_cfg))
    cached = _CACHE.get(cache_key)
    now = time.time()
    if cached:
        cached_at, cached_snapshot = cached
        active_cache = error_cache_seconds if cached_snapshot.get("status") == "error" else cache_seconds
        if now - cached_at <= active_cache:
            return cached_snapshot

    snapshot = await asyncio.to_thread(_fetch_snapshot, server_name, server_cfg, rest_cfg)
    _CACHE[cache_key] = (time.time(), snapshot)
    return snapshot
