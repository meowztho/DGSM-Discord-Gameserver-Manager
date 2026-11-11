"""Administrative regressions. Run via tests/run_isolated.py, never a live install."""
import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
import zipfile

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import config_store
import paths
import runtime_status
from template_utils import format_parameters


def load_service():
    manager = types.ModuleType("server_manager")
    manager.ensure_server_executable_hint = mock.Mock()
    manager.is_server_running = mock.Mock(return_value=False)
    manager.start_server = mock.AsyncMock(return_value=True)
    manager.stop_server = mock.AsyncMock(return_value=True)
    steam = types.ModuleType("steam_integration")
    steam.run_update = mock.AsyncMock(return_value=(True, "Installed"))
    database = types.ModuleType("db")
    database.write_action_log = mock.Mock()
    spec = importlib.util.spec_from_file_location("_service_under_test", SRC / "server_service.py")
    service = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = service
    with mock.patch.dict(sys.modules, {"server_manager": manager, "steam_integration": steam, "db": database}):
        spec.loader.exec_module(service)
    return service


def adapter_functions(filename, names, namespace):
    """Execute the actual adapter bodies without registering Discord or starting Qt."""
    tree = ast.parse((SRC / filename).read_text(encoding="utf-8"))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            body.append(node)
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, filename, "exec"), namespace)
    return namespace


class ServerServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.service = load_service()
        log_patch = mock.patch.object(self.service.logging, "exception")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        for module, name, value in (
            (self.service, "BASE_DIR", str(self.base)),
            (self.service, "PLUGIN_TEMPLATES_DIR", str(self.base / "templates")),
            (paths, "BASE_DIR", str(self.base)),
            (config_store, "CONFIG_PATH", str(self.base / "server_config.json")),
            (config_store, "CONFIG_BACKUP_PATH", str(self.base / "server_config.json.bak")),
            (config_store, "CONFIG_CACHE", None),
            (config_store, "CONFIG_LAST_MODIFIED", 0),
        ):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        runtime_status._STATUS.clear()
        paths.SERVER_PATHS.clear()
        paths.SERVER_CONFIGS.clear()
        config_store.save_config({"server_paths": {}})

    def server(self, name="S", directory=None, settings=None):
        files = Path(directory or self.base / "servers" / name / "serverfiles")
        files.mkdir(parents=True, exist_ok=True)
        (files / "server_settings.json").write_text(json.dumps(settings or {}), encoding="utf-8")
        (files / "world.dat").write_bytes(b"ORIGINAL WORLD")
        cfg = config_store.load_config()
        cfg["server_paths"][name] = {"app_id": "1", "install_dir": str(files)}
        config_store.save_config(cfg)
        paths.load_server_paths()
        paths.load_server_configs()
        return files

    def archive(self, filename="backup.zip", entries=None):
        directory = self.base / "steam" / "backup"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in (entries or {"serverfiles/world.dat": b"RESTORED WORLD"}).items():
                archive.writestr(name, content)
        return path

    async def template(self, credentials=False):
        result = await self.service.create_template(
            "Game", "1", "game.exe", '-name "My Server"',
            username="user" if credentials else "", password="secret" if credentials else "",
        )
        self.assertTrue(result.ok, result.message)

    async def test_invalid_zip_does_not_change_original(self):
        files = self.server()
        archive = self.archive()
        archive.write_bytes(b"not a ZIP")
        result = await self.service.restore_backup("S", archive.name, True)
        self.assertFalse(result.ok)
        self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")
        self.assertEqual(list(files.parent.parent.glob(".*restore*")), [])

    async def test_bad_crc_does_not_change_original(self):
        files = self.server()
        archive = self.archive(entries={"serverfiles/world.dat": b"CRC_CONTENT"})
        data = bytearray(archive.read_bytes())
        location = data.index(b"CRC_CONTENT")
        data[location] ^= 1
        archive.write_bytes(data)
        result = await self.service.restore_backup("S", archive.name, True)
        self.assertFalse(result.ok)
        self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")

    async def test_path_traversal_and_archive_symlinks_rejected_before_commit(self):
        files = self.server()
        for name in ("../escape", "serverfiles/../../escape", "C:/escape", "\\\\host\\share\\escape"):
            archive = self.archive(entries={name: b"bad"})
            result = await self.service.restore_backup("S", archive.name, True)
            self.assertFalse(result.ok, name)
            self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")
        archive = self.archive()
        with zipfile.ZipFile(archive, "w") as handle:
            info = zipfile.ZipInfo("serverfiles/link")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            handle.writestr(info, "outside")
        result = await self.service.restore_backup("S", archive.name, True)
        self.assertFalse(result.ok)
        self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")

    async def test_restore_replaces_only_after_validation_and_keeps_recovery_copy(self):
        files = self.server()
        archive = self.archive()
        result = await self.service.restore_backup("S", archive.name, True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual((files / "world.dat").read_bytes(), b"RESTORED WORLD")
        original = list(files.parent.parent.glob(".S.restore-original-*"))
        self.assertEqual(len(original), 1)
        self.assertEqual((original[0] / "serverfiles" / "world.dat").read_bytes(), b"ORIGINAL WORLD")
        self.assertIn(str(original[0]), result.message)

    async def test_locked_original_is_not_modified(self):
        files = self.server()
        archive = self.archive()
        original_replace = os.replace
        def replace(source, destination):
            if Path(source) == files.parent:
                raise PermissionError("locked original")
            return original_replace(source, destination)
        with mock.patch.object(self.service.os, "replace", side_effect=replace):
            result = await self.service.restore_backup("S", archive.name, True)
        self.assertFalse(result.ok)
        self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")

    async def test_commit_failure_rolls_original_back(self):
        files = self.server()
        archive = self.archive()
        original_replace = os.replace
        def replace(source, destination):
            if ".restore-stage-" in Path(source).name:
                raise PermissionError("commit blocked")
            return original_replace(source, destination)
        with mock.patch.object(self.service.os, "replace", side_effect=replace):
            result = await self.service.restore_backup("S", archive.name, True)
        self.assertFalse(result.ok)
        self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")

    async def test_rollback_failure_reports_preserved_original_path(self):
        files = self.server()
        archive = self.archive()
        original_replace = os.replace
        def replace(source, destination):
            if ".restore-stage-" in Path(source).name or ".restore-original-" in Path(source).name:
                raise PermissionError("destination blocked")
            return original_replace(source, destination)
        with mock.patch.object(self.service.os, "replace", side_effect=replace):
            result = await self.service.restore_backup("S", archive.name, True)
        self.assertFalse(result.ok)
        original = list(files.parent.parent.glob(".S.restore-original-*"))
        self.assertEqual(len(original), 1)
        self.assertEqual((original[0] / "serverfiles" / "world.dat").read_bytes(), b"ORIGINAL WORLD")
        self.assertIn(str(original[0]), result.message)

    async def test_shared_ancestor_and_alias_paths_reject_restore(self):
        files = self.server()
        archive = self.archive()
        self.server("Parent", files.parent.parent)
        result = await self.service.restore_backup("S", archive.name, True)
        self.assertFalse(result.ok)
        self.assertIn("overlaps", result.message)
        self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")

    async def test_resolved_alias_rejects_restore_and_keeps_remove_folder(self):
        files = self.server()
        cfg = config_store.load_config()
        cfg["server_paths"]["Alias"] = {"app_id": "1", "install_dir": str(files.parent / ".." / "S" / "serverfiles")}
        config_store.save_config(cfg)
        archive = self.archive()
        result = await self.service.restore_backup("S", archive.name, True)
        self.assertFalse(result.ok)
        result = await self.service.remove_server("S", False)
        self.assertTrue(result.ok, result.message)
        self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")

    async def test_unsafe_base_root_rejected_without_file_operations(self):
        self.server(directory=self.base / "serverfiles")
        with mock.patch.object(self.service, "_directory_backup") as backup:
            result = await self.service.remove_server("S")
        self.assertFalse(result.ok)
        backup.assert_not_called()
        self.assertTrue((self.base / "serverfiles" / "world.dat").exists())

    async def test_protected_subdirectories_are_not_valid_server_roots(self):
        for directory in (self.base / "templates" / "Game", self.base / "steam" / "backup" / "subdir"):
            with self.assertRaises(ValueError):
                self.service._safe_root(directory)

    async def test_install_failure_contains_shared_diagnostics(self):
        await self.template()
        self.service.run_update.return_value = (False, "fixture failure")
        with mock.patch.object(self.service.shutil, "disk_usage", return_value=types.SimpleNamespace(free=2 * 1024 ** 3)):
            result = await self.service.add_server("Diagnostic", "Game")
        self.assertFalse(result.ok)
        for text in ("fixture failure", "App ID:", "Install directory:", "serverfiles", "Free space: 2.00 GB"):
            self.assertIn(text, result.message)

    async def test_adapters_share_install_fallback_and_preserve_discord_progress_order(self):
        await self.template(credentials=True)
        attempts = {}
        async def update(name):
            attempts.setdefault(name, []).append(bool(config_store.load_config()["server_paths"][name].get("username")))
            return (False, "Account besitzt keine Lizenz") if len(attempts[name]) == 1 else (True, "OK")
        self.service.run_update.side_effect = update
        events = []
        async def defer(*args, **kwargs):
            events.append("defer")
        async def send(ctx, message, **kwargs):
            events.append(message)
        async def refresh(*args, **kwargs):
            events.append(("refresh", runtime_status.get_operation_status("Discord")[0]))
        command = adapter_functions("commands.py", {"add_server", "_admin_progress", "_admin_reply"}, {
            "server_service": self.service, "safe_defer": defer, "safe_send": send,
            "refresh_main_panel": refresh, "_notify_desktop_ui_refresh": mock.Mock(), "logging": self.service.logging,
        })
        desktop = adapter_functions("desktop_ui.py", {"_add_server_action", "_admin_result", "_admin_progress"}, {
            "server_service": self.service, "_feedback": mock.Mock(), "_safe_refresh_discord_panel": mock.AsyncMock(),
            "notify_external_refresh": mock.Mock(),
        })
        web = adapter_functions("web_ui.py", {"_handle_add_server"}, {"dui": types.SimpleNamespace(**desktop)})
        await command["add_server"](None, "Discord", "Game", None)
        self.assertTrue((await desktop["_add_server_action"]("Desktop", "Game"))[0])
        self.assertTrue((await web["_handle_add_server"]({"name": "Web", "template_name": "Game"}))[0])
        self.assertEqual(attempts, {"Discord": [True, False], "Desktop": [True, False], "Web": [True, False]})
        self.assertEqual(events[0], "defer")
        self.assertIn("Starte Installation", events[1])
        self.assertIn(("refresh", "busy"), events)
        self.assertEqual(events[-2], ("refresh", None))
        self.assertTrue(events[-1].startswith("✅"))

    async def test_adapters_return_same_restore_error_without_mutation(self):
        files = self.server()
        archive = self.archive()
        archive.write_bytes(b"invalid archive")
        messages = []
        async def send(ctx, message, **kwargs):
            messages.append(message)
        command = adapter_functions("commands.py", {"restore_backup", "_admin_progress", "_admin_reply"}, {
            "server_service": self.service, "safe_defer": mock.AsyncMock(), "safe_send": send,
            "refresh_main_panel": mock.AsyncMock(), "_notify_desktop_ui_refresh": mock.Mock(), "logging": self.service.logging,
        })
        desktop = adapter_functions("desktop_ui.py", {"_restore_backup_action", "_admin_result", "_admin_progress"}, {
            "server_service": self.service, "_feedback": mock.Mock(), "_safe_refresh_discord_panel": mock.AsyncMock(),
            "notify_external_refresh": mock.Mock(),
        })
        web = adapter_functions("web_ui.py", {"_handle_action"}, {"dui": types.SimpleNamespace(**desktop)})
        await command["restore_backup"](None, "S", archive.name, True)
        desktop_result = await desktop["_restore_backup_action"]("S", archive.name, True)
        web_result = await web["_handle_action"]({"name": "S", "action": "restore", "backup_file": archive.name, "overwrite": True})
        self.assertFalse(desktop_result[0])
        self.assertEqual(desktop_result, web_result)
        self.assertEqual(messages[-1], "❌ " + desktop_result[1])
        self.assertEqual((files / "world.dat").read_bytes(), b"ORIGINAL WORLD")

    async def test_discord_numeric_app_id_is_persisted_as_canonical_text(self):
        self.server()
        messages = []
        async def send(ctx, message, **kwargs):
            messages.append(message)
        command = adapter_functions("commands.py", {"update_server", "_admin_progress", "_admin_reply"}, {
            "server_service": self.service, "safe_defer": mock.AsyncMock(), "safe_send": send,
            "refresh_main_panel": mock.AsyncMock(), "_notify_desktop_ui_refresh": mock.Mock(), "logging": self.service.logging,
        })
        await command["update_server"](None, "S", "config", "app_id", "2394010")
        persisted = json.loads(Path(config_store.CONFIG_PATH).read_text(encoding="utf-8"))
        self.assertEqual(persisted["server_paths"]["S"]["app_id"], "2394010")
        self.assertTrue(messages[-1].startswith("✅"), messages[-1])

    async def test_app_id_keeps_string_identifiers_and_rejects_boolean(self):
        self.server()
        result = await self.service.update_server_config("S", {"app_id": "minecraft_vanilla"})
        self.assertTrue(result.ok, result.message)
        result = await self.service.update_server_config("S", {"app_id": True})
        self.assertFalse(result.ok)
        self.assertEqual(config_store.load_config()["server_paths"]["S"]["app_id"], "minecraft_vanilla")

    async def test_remove_aborts_when_stop_fails(self):
        files = self.server()
        self.service.is_server_running.return_value = True
        self.service.stop_server.return_value = False
        result = await self.service.remove_server("S", False)
        self.assertFalse(result.ok)
        self.assertTrue((files / "world.dat").exists())
        self.assertIn("S", config_store.load_config()["server_paths"])

    async def test_remove_aborts_when_required_backup_fails(self):
        files = self.server()
        with mock.patch.object(self.service, "_directory_backup", side_effect=OSError("disk full")):
            result = await self.service.remove_server("S")
        self.assertFalse(result.ok)
        self.assertTrue((files / "world.dat").exists())
        self.assertIn("S", config_store.load_config()["server_paths"])

    async def test_remove_shared_layout_keeps_folder_and_other_entry(self):
        files = self.server()
        self.server("Alias", files)
        result = await self.service.remove_server("S", False)
        self.assertTrue(result.ok, result.message)
        self.assertTrue((files / "world.dat").exists())
        self.assertNotIn("S", config_store.load_config()["server_paths"])
        self.assertIn("Alias", config_store.load_config()["server_paths"])

    async def test_remove_readonly_files_keeps_existing_windows_behavior(self):
        files = self.server()
        (files / "world.dat").chmod(0o444)
        result = await self.service.remove_server("S", False)
        self.assertTrue(result.ok, result.message)
        self.assertFalse(files.parent.exists())
        self.assertNotIn("S", config_store.load_config()["server_paths"])

    async def test_add_anonymous_fallback_is_shared_and_credentials_removed(self):
        await self.template(credentials=True)
        credentialed = []
        async def update(name):
            credentialed.append(bool(config_store.load_config()["server_paths"][name].get("username")))
            return (False, "Account besitzt keine Lizenz") if len(credentialed) == 1 else (True, "OK")
        self.service.run_update.side_effect = update
        result = await self.service.add_server("S", "Game")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(credentialed, [True, False])
        self.service.start_server.assert_awaited_once_with("S")

    async def test_add_can_install_without_start_and_preserves_argument_boundaries(self):
        await self.template()
        result = await self.service.add_server("S", "Game", start_after_install=False)
        self.assertTrue(result.ok, result.message)
        self.service.start_server.assert_not_awaited()
        self.assertEqual(paths.SERVER_CONFIGS["S"]["parameters"], ["-name", "My Server"])

    async def test_add_rejects_existing_unregistered_folder(self):
        await self.template()
        root = paths.server_root("1", instance_id="S")
        root.mkdir(parents=True)
        (root / "valuable.dat").write_bytes(b"DO NOT OVERWRITE")
        result = await self.service.add_server("S", "Game")
        self.assertFalse(result.ok)
        self.assertEqual((root / "valuable.dat").read_bytes(), b"DO NOT OVERWRITE")
        self.service.run_update.assert_not_awaited()

    async def test_settings_roundtrip_preserves_arguments_and_unrelated_fields(self):
        arguments = ["-name", "My Server", r"C:\Game Data\world", ""]
        files = self.server(settings={"parameters": arguments, "rest_api": {"enabled": True}, "custom": 17})
        result = await self.service.update_server_settings("S", {"parameters": format_parameters(arguments)})
        self.assertTrue(result.ok, result.message)
        settings = json.loads((files / "server_settings.json").read_text())
        self.assertEqual(settings["parameters"], arguments)
        self.assertEqual(settings["custom"], 17)
        self.assertEqual(settings["rest_api"], {"enabled": True})

    async def test_invalid_settings_leave_original_untouched(self):
        files = self.server(settings={"stop_time": "05:00"})
        original = (files / "server_settings.json").read_bytes()
        for patch in ({"stop_time": "25:00"}, {"auto_restart": "false"}, {"parameters": '"unterminated'}):
            result = await self.service.update_server_settings("S", patch)
            self.assertFalse(result.ok)
            self.assertEqual((files / "server_settings.json").read_bytes(), original)

    async def test_same_task_nested_owner_allowed_but_second_action_rejected(self):
        files = self.server()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def progress(label):
            async with runtime_status.server_operation("S"):
                entered.set()
                await release.wait()
        first = asyncio.create_task(self.service.create_backup("S", progress=progress))
        await entered.wait()
        second = await self.service.remove_server("S", False)
        self.assertFalse(second.ok)
        self.assertTrue((files / "world.dat").exists())
        release.set()
        self.assertTrue((await first).ok)

    async def test_cancelled_filesystem_worker_keeps_gate_until_drained(self):
        files = self.server()
        entered = threading.Event()
        release = threading.Event()
        def backup(*args):
            entered.set()
            release.wait(timeout=5)
            return "completed.zip"
        with mock.patch.object(self.service, "_directory_backup", side_effect=backup):
            task = asyncio.create_task(self.service.create_backup("S"))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            blocked = await self.service.remove_server("S", False)
            self.assertFalse(blocked.ok)
            self.assertTrue((files / "world.dat").exists())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        async with runtime_status.server_operation("S"):
            self.assertNotEqual(runtime_status.get_operation_status("S")[0], "busy")


if __name__ == "__main__":
    unittest.main()
