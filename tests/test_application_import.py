"""Load the real adapter graph without logging in or starting any service."""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class ApplicationImportTests(unittest.TestCase):
    def test_original_context_first_bootstrap_registers_commands(self):
        # run_isolated.py supplies an empty runtime directory and blocks network,
        # process control and writes outside that temporary source copy.
        context = importlib.import_module("context")
        commands = importlib.import_module("commands")
        with patch("signal.signal"):
            main = importlib.import_module("Main")
        self.assertIs(main.bot, context.bot)
        names = {command.name for command in context.bot.pending_application_commands}
        self.assertTrue({"addserver", "removeserver", "createbackup", "restorebackup", "updateserver", "cli"} <= names)
        self.assertIs(commands.server_service, importlib.import_module("server_service"))
        self.assertIsNone(main._monitor_task)
        self.assertFalse(main._desktop_ui_started)
        self.assertFalse(main._web_ui_started)
