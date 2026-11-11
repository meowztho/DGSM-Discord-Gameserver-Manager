"""Lifecycle regressions. Run via tests/run_isolated.py; all processes are fakes."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import custom_install
import pidcache
import runtime_status as status
import server_manager as manager
import steam_integration as steam


class FakeProcess:
    def __init__(self, pid=43210, created=123456.0, exe="", cwd="", running=True):
        self.pid, self.created, self.executable = pid, created, exe
        self.directory, self.running = cwd, running
        self.terminate = Mock(side_effect=self._stop)
        self.kill = Mock(side_effect=self._stop)

    def _stop(self):
        self.running = False

    def is_running(self):
        return self.running

    def create_time(self):
        return self.created

    def exe(self):
        return self.executable

    def cwd(self):
        return self.directory

    def cmdline(self):
        return [self.executable]


class LifecycleSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.proc = FakeProcess(exe=str(self.root / "server.exe"), cwd=str(self.root))
        for value in (status._STATUS, status._OPERATION_OWNERS, status._OPERATION_ROOTS, manager.server_processes,
                      manager.SERVER_PATHS, manager.SERVER_CONFIGS, manager._restart_history):
            self.stack.enter_context(patch.dict(value, {}, clear=True))
        self.stack.enter_context(patch.object(manager, "daily_stopped_servers", set()))
        self.config = {"server_paths": {}}
        self.stack.enter_context(patch.object(steam.paths, "load_config", return_value=self.config))
        manager.SERVER_PATHS["test"] = str(self.root)
        manager.SERVER_CONFIGS["test"] = {"auto_update": False}
        for obj, name in ((manager, "load_server_paths"), (manager, "load_server_configs"),
                          (steam.paths, "load_server_paths"), (steam.paths, "load_server_configs"),
                          (steam, "load_config"), (manager, "write_action_log"),
                          (manager, "save_pids"), (steam, "save_steam_session")):
            self.stack.enter_context(patch.object(obj, name, Mock()))
        self.stack.enter_context(patch.object(manager, "_list_server_related_processes", return_value=[]))
        self.stack.enter_context(patch.object(manager, "_server_command", return_value=[self.proc.executable]))
        self.stack.enter_context(patch.object(manager, "_resolve_executable", return_value=self.proc.executable))
        self.stack.enter_context(patch.object(manager.psutil, "Process", return_value=self.proc))
        self.spawn = self.stack.enter_context(patch.object(manager.subprocess, "Popen", return_value=self.proc))
        self.stack.enter_context(patch.object(steam, "restore_steam_session", return_value=False))
        self.stack.enter_context(patch.object(steam, "get_config_value", side_effect=lambda n, k: "audit" if k == "app_id" else None))

    async def test_nested_gate_and_child_task_rejection(self):
        async with status.server_operation("test"):
            async with status.server_operation("test"):
                async def child():
                    async with status.server_operation("test"):
                        self.fail("Child inherited lifecycle ownership")
                with self.assertRaises(status.ServerOperationBusy):
                    await asyncio.create_task(child())
            async with status.server_operation("other"):
                pass
        self.assertFalse(status._OPERATION_OWNERS)

    async def test_cancelled_owner_releases_gate(self):
        entered = asyncio.Event()
        async def owner():
            async with status.server_operation("test"):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(owner())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with status.server_operation("test"):
            pass

    async def test_running_server_rejects_both_update_entries(self):
        manager.server_processes["test"] = self.proc
        with patch.object(steam, "run_custom_install", new_callable=AsyncMock) as install:
            self.assertFalse((await steam.run_update("test"))[0])
            self.assertFalse((await steam.run_update_with_credentials("test", "user", "password"))[0])
        install.assert_not_awaited()
        self.spawn.assert_not_called()

    async def test_untracked_related_process_also_blocks_update(self):
        with patch.object(manager, "_list_server_related_processes", return_value=[self.proc]):
            self.assertFalse((await steam.run_update("test"))[0])

    async def test_update_blocks_start_stop_and_second_update(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def install(*args):
            entered.set()
            await release.wait()
            return True, "installed"
        with patch.object(steam, "run_custom_install", side_effect=install):
            task = asyncio.create_task(steam.run_update("test"))
            await entered.wait()
            try:
                self.assertFalse(await manager.start_server("test"))
                self.assertFalse(await manager.stop_server("test"))
                self.assertFalse((await steam.run_update("test"))[0])
                self.assertEqual("busy", status.get_operation_status("test")[0])
                self.spawn.assert_not_called()
            finally:
                release.set()
                self.assertTrue((await task)[0])

    async def test_alias_cannot_update_same_files_during_first_update(self):
        manager.SERVER_PATHS["alias"] = manager.SERVER_PATHS["test"]
        manager.SERVER_CONFIGS["alias"] = {"auto_update": False}
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def install(name, app, directory):
            calls.append((name, directory))
            if name == "test":
                entered.set()
                await release.wait()
            return True, "installed"
        with patch.object(steam, "run_custom_install", side_effect=install):
            first = asyncio.create_task(steam.run_update("test"))
            await entered.wait()
            try:
                self.assertFalse((await steam.run_update("alias"))[0])
                self.assertFalse(await manager.start_server("alias"))
                self.assertFalse(await manager.stop_server("alias"))
                self.assertEqual([("test", str(self.root))], calls)
            finally:
                release.set()
                await first

    async def _assert_alias_admission(self, *, blocked=True, after_enter=None):
        entered, release = asyncio.Event(), asyncio.Event()
        async def owner():
            async with status.server_operation("test"):
                entered.set()
                await release.wait()
        task = asyncio.create_task(owner())
        await entered.wait()
        try:
            if after_enter:
                after_enter()
            if blocked:
                with self.assertRaises(status.ServerOperationBusy):
                    async with status.server_operation("alias"):
                        self.fail("Overlapping filesystem roots were admitted")
            else:
                async with status.server_operation("alias"):
                    pass
        finally:
            release.set()
            await task
        self.assertFalse(status._OPERATION_ROOTS)

    async def test_parent_and_child_server_roots_exclude_each_other(self):
        parent = self.root / "common" / "serverfiles"
        child = self.root / "common" / "nested" / "serverfiles"
        for first, second in ((parent, child), (child, parent)):
            with self.subTest(first=str(first)):
                manager.SERVER_PATHS.update(test=str(first), alias=str(second))
                await self._assert_alias_admission()

    async def test_configured_aliases_are_blocked_before_runtime_map_reload(self):
        manager.SERVER_PATHS.clear()
        self.config["server_paths"] = {
            "test": {"app_id": "1", "instance_id": "shared"},
            "alias": {"app_id": "1", "instance_id": "shared"},
        }
        await self._assert_alias_admission()

    async def test_independent_sibling_instances_remain_admissible(self):
        manager.SERVER_PATHS.clear()
        self.config["server_paths"] = {
            "test": {"app_id": "1", "instance_id": "first"},
            "alias": {"app_id": "1", "instance_id": "second"},
        }
        await self._assert_alias_admission(blocked=False)

    async def test_name_admission_covers_entry_registered_before_copy_await(self):
        manager.SERVER_PATHS.clear()
        def register():
            self.config["server_paths"] = {
                "test": {"app_id": "1", "instance_id": "new"},
                "alias": {"app_id": "1", "instance_id": "new"},
            }
        await self._assert_alias_admission(after_enter=register)

    async def test_old_root_stays_reserved_after_config_and_runtime_path_change(self):
        manager.SERVER_PATHS.clear()
        self.config["server_paths"] = {
            "test": {"app_id": "1", "instance_id": "old"},
            "alias": {"app_id": "1", "instance_id": "old"},
        }
        def move_entry():
            self.config["server_paths"]["test"]["instance_id"] = "new"
        await self._assert_alias_admission(after_enter=move_entry)

    async def test_same_task_can_nest_aliases_without_deadlock(self):
        manager.SERVER_PATHS["alias"] = manager.SERVER_PATHS["test"]
        async with status.server_operation("test"):
            async with status.server_operation("alias"):
                async with status.server_operation("test"):
                    pass
            self.assertIn("test", status._OPERATION_ROOTS)
        self.assertFalse(status._OPERATION_ROOTS)

    async def test_realpath_symlink_alias_uses_same_scope(self):
        actual = self.root / "actual"
        actual.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Directory symlink unavailable: {exc}")
        manager.SERVER_PATHS.update(test=str(actual / "serverfiles"), alias=str(link / "serverfiles"))
        await self._assert_alias_admission()

    async def test_start_updates_in_same_task_before_spawn(self):
        manager.SERVER_CONFIGS["test"]["auto_update"] = True
        order = []
        async def install(*args):
            order.append("update")
            return True, "installed"
        self.spawn.side_effect = lambda *a, **kw: (order.append("spawn") or self.proc)
        with patch.object(steam, "run_custom_install", side_effect=install):
            self.assertTrue(await manager.start_server("test"))
        self.assertEqual(["update", "spawn"], order)

    async def test_failed_automatic_update_preserves_existing_start_policy(self):
        manager.SERVER_CONFIGS["test"]["auto_update"] = True
        with patch.object(steam, "run_custom_install", new=AsyncMock(return_value=(False, "failed"))):
            self.assertTrue(await manager.start_server("test"))
        self.spawn.assert_called_once()

    async def test_schedule_cannot_bypass_other_owner(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def owner():
            async with status.server_operation("test"):
                entered.set()
                await release.wait()
        task = asyncio.create_task(owner())
        await entered.wait()
        try:
            with patch.object(manager, "stop_server", new_callable=AsyncMock) as stop:
                await manager._scheduled_stop("test", {"restart_after_stop": True})
                stop.assert_not_awaited()
            self.assertNotIn("test", manager.daily_stopped_servers)
            self.spawn.assert_not_called()
        finally:
            release.set()
            await task

    async def test_failed_scheduled_stop_never_updates_or_restarts(self):
        manager.server_processes["test"] = self.proc
        with patch.object(manager, "stop_server", new=AsyncMock(return_value=False)), \
             patch.object(manager, "auto_update_if_enabled", new_callable=AsyncMock) as update:
            await manager._scheduled_stop("test", {"restart_after_stop": True})
            update.assert_not_awaited()
        self.assertNotIn("test", manager.daily_stopped_servers)

    async def test_scheduled_update_failure_preserves_restart_policy(self):
        with patch.object(manager, "is_server_running", return_value=True), \
             patch.object(manager, "stop_server", new=AsyncMock(return_value=True)), \
             patch.object(manager, "auto_update_if_enabled", new=AsyncMock(return_value=False)) as update, \
             patch.object(manager, "start_server", new=AsyncMock(return_value=True)) as start, \
             patch.object(asyncio, "sleep", new_callable=AsyncMock):
            await manager._scheduled_stop("test", {"restart_after_stop": True})
        update.assert_awaited_once_with("test")
        start.assert_awaited_once_with("test")
        self.assertIn("test", manager.daily_stopped_servers)

    async def test_restart_owns_entire_stop_start_sequence(self):
        order = []
        async def stop(name):
            order.append("stop")
            async def competitor():
                return await manager.start_server(name)
            self.assertFalse(await asyncio.create_task(competitor()))
            return True
        async def start(name, _auto=False):
            order.append("start")
            return True
        with patch.object(manager, "is_server_running", return_value=True), \
             patch.object(manager, "_stop_server", side_effect=stop), \
             patch.object(manager, "_start_server", side_effect=start):
            self.assertTrue(await manager.restart_server("test"))
        self.assertEqual(["stop", "start"], order)

    async def test_restart_does_not_start_after_failed_stop(self):
        manager.server_processes["test"] = self.proc
        with patch.object(manager, "_stop_server", new=AsyncMock(return_value=False)):
            self.assertFalse(await manager.restart_server("test"))
        self.spawn.assert_not_called()

    async def test_recovery_validates_new_identity_and_legacy_path(self):
        record = pidcache.process_identity(self.proc)
        for value, expected in ((record, True), (self.proc.pid, True),
                                ({**record, "created_at": self.proc.created + 1}, False),
                                ({"pid": self.proc.pid}, False), (True, False)):
            with self.subTest(record=value):
                manager.server_processes.clear()
                with patch.object(manager, "load_pids", return_value={"test": value}):
                    await manager.recover_running_servers()
                self.assertEqual(expected, "test" in manager.server_processes)
        manager.server_processes.clear()
        self.proc.executable = str(self.root.parent / "unrelated.exe")
        self.proc.directory = str(self.root.parent)
        with patch.object(manager, "load_pids", return_value={"test": self.proc.pid}):
            await manager.recover_running_servers()
        self.assertNotIn("test", manager.server_processes)
        with patch.object(manager, "load_pids", return_value={"test": pidcache.process_identity(self.proc)}):
            await manager.recover_running_servers()
        self.assertNotIn("test", manager.server_processes)

    async def test_legacy_pid_cannot_adopt_wrong_executable_in_same_directory(self):
        self.proc.executable = str(self.root / "unrelated.exe")
        with patch.object(manager, "load_pids", return_value={"test": self.proc.pid}):
            await manager.recover_running_servers()
        self.assertNotIn("test", manager.server_processes)

    async def test_legacy_interpreter_requires_instance_path_and_script(self):
        self.proc.executable = str(self.root.parent / "sh")
        script = str(self.root / "start.sh")
        with patch.object(manager, "_resolve_executable", return_value=script), \
             patch.object(self.proc, "cmdline", return_value=[self.proc.executable, script]), \
             patch.object(manager, "load_pids", return_value={"test": self.proc.pid}):
            await manager.recover_running_servers()
        self.assertIs(self.proc, manager.server_processes["test"])

    async def test_stop_never_recreates_reused_tracked_pid(self):
        self.proc.running = False
        manager.server_processes["test"] = self.proc
        replacement = FakeProcess(pid=self.proc.pid, created=self.proc.created + 1)
        with patch.object(manager.psutil, "Process", return_value=replacement) as lookup, \
             patch.object(manager, "is_windows", return_value=True), \
             patch.object(manager, "_terminate_tree_windows", new_callable=AsyncMock):
            self.assertTrue(await manager.stop_server("test"))
            lookup.assert_not_called()
        replacement.terminate.assert_not_called()
        replacement.kill.assert_not_called()

    async def test_taskkill_does_not_touch_expired_process_identity(self):
        self.proc.running = False
        with patch.object(manager.subprocess, "run") as run:
            await manager._win_taskkill(self.proc, force=True)
            run.assert_not_called()

    async def _check_thread_drain(self, timeout=False):
        entered, release = threading.Event(), threading.Event()
        def installer(path):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Fixture was not released")
            return "installed"
        with patch.dict(custom_install._INSTALLERS, {"audit": installer}), \
             patch.object(custom_install, "_DOWNLOAD_TIMEOUT", 0.01 if timeout else 30):
            task = asyncio.create_task(steam.run_update("test"))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(entered.is_set())
            if not timeout:
                task.cancel()
            await asyncio.sleep(0.04)
            try:
                self.assertFalse(task.done())
                self.assertFalse(await manager.start_server("test"))
                self.spawn.assert_not_called()
            finally:
                release.set()
            if timeout:
                self.assertFalse((await task)[0])
            else:
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertFalse(status._OPERATION_OWNERS)

    async def test_cancelled_custom_installer_retains_gate_until_thread_exits(self):
        await self._check_thread_drain()

    async def test_timed_out_custom_installer_retains_gate_until_thread_exits(self):
        await self._check_thread_drain(timeout=True)

    async def test_cancelled_steamcmd_kills_and_drains_before_gate_release(self):
        entered, release = asyncio.Event(), asyncio.Event()
        process = Mock(returncode=None)
        process.wait = AsyncMock(return_value=-1)
        async def communicate():
            entered.set()
            await release.wait()
            process.returncode = -1
            return b"", b""
        process.communicate = communicate
        async def run():
            async with status.server_operation("test"):
                await steam._run_steamcmd(["fixture"], str(self.root))
        with patch.object(asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)):
            task = asyncio.create_task(run())
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(await manager.start_server("test"))
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        self.assertFalse(status._OPERATION_OWNERS)

    async def test_pid_cache_roundtrip_and_malformed_root(self):
        cache = self.root / "pids.json"
        with patch.object(pidcache, "PID_CACHE", str(cache)):
            pidcache.save_pids({"test": self.proc})
            data = pidcache.load_pids()
            self.assertTrue(pidcache.matches_process_identity(self.proc, data["test"]))
            self.proc.created += 1
            self.assertFalse(pidcache.matches_process_identity(self.proc, data["test"]))
            cache.write_text(json.dumps([self.proc.pid]), encoding="utf-8")
            self.assertEqual({}, pidcache.load_pids())


if __name__ == "__main__":
    unittest.main()
