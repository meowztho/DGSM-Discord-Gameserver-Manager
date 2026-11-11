"""Exercise control ordering without importing/starting the Discord application."""
import __future__
import ast
import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtime_status import drain_task


def source_functions(filename, names, namespace):
    source = Path(__file__).resolve().parents[1] / "src" / filename
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace


class PanelOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.panels = []
        self.context = types.ModuleType("context")
        self.context.safe_get_ip = lambda: "test.invalid"
        self.context_patch = patch.dict(sys.modules, {"context": self.context})
        self.context_patch.start()

        async def purge(limit):
            self.events.append("purge")
            self.panels.clear()
            await asyncio.sleep(0)

        async def post(channel, address, user=None):
            self.events.append("post")
            self.panels.append("panel")

        self.channel = types.SimpleNamespace(id=123, purge=purge)
        self.scope = source_functions("ui.py", {"clean_channel", "refresh_status_panel"}, {
            "asyncio": asyncio, "logging": logging, "_PANEL_LOCKS": {},
            "drain_task": drain_task, "update_status_message": post,
            "_notify_desktop_ui_refresh": lambda: None,
        })

    async def asyncTearDown(self):
        self.context_patch.stop()

    async def test_concurrent_refresh_keeps_purge_post_pairs_and_one_panel(self):
        refresh = self.scope["refresh_status_panel"]
        # Different Discord channel objects can describe the same channel ID.
        other = types.SimpleNamespace(id=123, purge=self.channel.purge)
        await asyncio.gather(refresh(self.channel), refresh(other))
        self.assertEqual(["purge", "post", "purge", "post"], self.events)
        self.assertEqual(["panel"], self.panels)

    async def test_failed_purge_does_not_append_a_duplicate(self):
        async def failed_purge(limit):
            raise RuntimeError("simulated Discord failure")
        self.panels.append("existing")
        self.channel.purge = failed_purge
        with self.assertLogs(level="ERROR"):
            await self.scope["refresh_status_panel"](self.channel)
        self.assertEqual(["existing"], self.panels)
        self.assertNotIn("post", self.events)

    async def test_timeout_cannot_interrupt_between_purge_and_post(self):
        posting = asyncio.Event()
        finish = asyncio.Event()

        async def delayed_post(channel, address, user=None):
            posting.set()
            await finish.wait()
            self.panels.append("panel")

        self.scope["update_status_message"] = delayed_post
        task = asyncio.create_task(self.scope["refresh_status_panel"](self.channel))
        await posting.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(["panel"], self.panels)


class StartupOrderingTests(unittest.IsolatedAsyncioTestCase):
    def scope(self, events):
        async def recovery(): events.append("recovery")
        async def autostart(): events.append("autostart")
        async def refresh(channel, user=None): events.append("panel")
        def desktop(*args, **kwargs): events.append("desktop"); return False
        def web(*args, **kwargs): events.append("web"); return False
        return source_functions("Main.py", {"on_ready", "_run_ui_only", "daily_update"}, {
            "asyncio": asyncio, "logging": logging,
            "_desktop_ui_started": False, "_web_ui_started": False, "_monitor_task": None,
            "ensure_steamcmd_available": lambda: (True, "simulated"),
            "init_db": lambda: events.append("db"), "cleanup_old_logs": lambda days: None,
            "get_log_retention_days": lambda: 7,
            "load_server_paths": lambda: events.append("paths"),
            "load_server_configs": lambda: events.append("config"),
            "recover_running_servers": recovery, "auto_start_servers": autostart,
            "_ensure_monitor_started": lambda: events.append("monitor"),
            "refresh_status_panel": refresh, "start_desktop_ui": desktop, "start_web_ui": web,
            "is_desktop_ui_started": lambda: False, "is_web_ui_started": lambda: False,
            "bot": types.SimpleNamespace(get_channel=lambda _: object(), user="fixture"),
            "CHANNEL": 123, "write_action_log": lambda *args: None,
        })

    async def test_discord_start_order_and_daily_use_shared_panel_path(self):
        events = []
        scope = self.scope(events)
        daily = scope["daily_update"]
        scope["daily_update"] = types.SimpleNamespace(is_running=lambda: False, start=lambda: events.append("daily"))
        await scope["on_ready"]()
        self.assertEqual(["db", "paths", "config", "recovery", "autostart", "monitor", "panel", "desktop", "web", "daily"], events)
        await daily()
        self.assertEqual("panel", events[-1])

    async def test_ui_only_composes_monitor_after_recovery_and_autostart(self):
        events = []
        scope = self.scope(events)
        with self.assertLogs(level="ERROR"):
            await scope["_run_ui_only"]()
        self.assertEqual(["db", "paths", "config", "recovery", "autostart", "monitor", "desktop", "web"], events)

    async def test_monitor_is_not_duplicated_on_reconnect(self):
        stop = asyncio.Event()
        async def monitor(): await stop.wait()
        scope = source_functions("Main.py", {"_ensure_monitor_started"}, {
            "asyncio": asyncio, "_monitor_task": None, "monitor_servers": monitor,
        })
        scope["_ensure_monitor_started"]()
        first = scope["_monitor_task"]
        scope["_ensure_monitor_started"]()
        self.assertIs(first, scope["_monitor_task"])
        stop.set()
        await first
