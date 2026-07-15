import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wgsm_import import (
    WgsmImportError,
    _sources_from_zip,
    _validate_public_https_url,
    import_wgsm_plugin,
    inspect_wgsm_source,
)


STEAM_PLUGIN = r'''
namespace WindowsGSM.Plugins {
public class NicheGame : SteamCMDAgent {
    public Plugin Plugin = new Plugin {
        name = "WindowsGSM.NicheGame",
        author = "Example Author",
        description = "Example plugin",
        version = "1.2.3",
        url = "https://github.com/example/plugin",
        color = "#ffffff"
    };
    public override bool loginAnonymous => true;
    public override string AppId => "123456";
    public override string StartPath => @"Server\Binaries\NicheServer.exe";
    public string FullName = "Niche Dedicated Server";
    public string ServerName = "Niche Test";
    public string Port = "7777";
    public string QueryPort = "27015";
    public string Maxplayers = "24";
    public string Additional = "-log -NoGui";
    public object QueryMethod = new A2S();
    public async Task<Process> Start() {
        string param = $" {_serverData.ServerParam} ";
        param += $"-port={_serverData.ServerPort} ";
        param += $"-query={_serverData.ServerQueryPort} ";
        param += $"-players={_serverData.ServerMaxPlayer} ";
        return null;
    }
    public async Task Stop(Process p) { }
}}
'''


CLASSIC_PLUGIN = r'''
namespace WindowsGSM.Plugins {
public class CustomGame {
    public Plugin Plugin = new Plugin { name = "WindowsGSM.CustomGame", version = "1.0" };
    public string StartPath = "custom-server.exe";
    public string Additional = "--headless";
    public async Task<Process> Install() { return null; }
}}
'''


class WindowsGsmImportTests(unittest.TestCase):
    def test_inspects_documented_steam_fields_and_dynamic_parameters(self):
        report = inspect_wgsm_source(STEAM_PLUGIN)
        self.assertEqual("steam_ready", report["compatibility"])
        self.assertEqual("123456", report["fields"]["app_id"])
        self.assertEqual(r"Server\Binaries\NicheServer.exe", report["fields"]["start_path"])
        self.assertEqual(
            ["-log", "-NoGui", "-port=7777", "-query=27015", "-players=24"],
            report["parameters"],
        )

    def test_creates_dgsm_template_without_copying_or_executing_csharp(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as templates_dir:
            source = Path(source_dir) / "NicheGame.cs"
            source.write_text(STEAM_PLUGIN, encoding="utf-8")
            report = import_wgsm_plugin(str(source), templates_dir)
            template = Path(templates_dir) / str(report["template_name"])
            config = json.loads((template / "config.json").read_text(encoding="utf-8"))
            self.assertEqual("123456", config["app_id"])
            self.assertTrue(config["auto_update"])
            self.assertTrue((template / "windowsgsm_import.json").is_file())
            self.assertFalse((template / "NicheGame.cs").exists())
            self.assertFalse((template / "install.py").exists())

    def test_classic_plugin_is_a_blocked_review_template(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as templates_dir:
            source = Path(source_dir) / "CustomGame.cs"
            source.write_text(CLASSIC_PLUGIN, encoding="utf-8")
            report = import_wgsm_plugin(str(source), templates_dir)
            template = Path(templates_dir) / str(report["template_name"])
            config = json.loads((template / "config.json").read_text(encoding="utf-8"))
            self.assertEqual("review_required", report["compatibility"])
            self.assertTrue(config["app_id"].startswith("wgsm_"))
            self.assertFalse(config["auto_update"])
            self.assertIn("not executed", (template / "install.py").read_text(encoding="utf-8"))

    def test_reads_plugin_zip_and_rejects_local_source_for_remote_surfaces(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as templates_dir:
            archive = Path(source_dir) / "plugin.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("NicheGame.cs/NicheGame.cs", STEAM_PLUGIN)
                handle.writestr("Other.cs", "public class Other {}")
            report = import_wgsm_plugin(str(archive), templates_dir)
            self.assertEqual("steam_ready", report["compatibility"])
            with self.assertRaises(WgsmImportError):
                import_wgsm_plugin(str(archive), templates_dir, allow_local=False)

    def test_normalizes_safe_steam_beta_arguments(self):
        source = STEAM_PLUGIN.replace('AppId => "123456"', 'AppId => "123456 -beta experimental +quit"')
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as templates_dir:
            plugin = Path(source_dir) / "BetaGame.cs"
            plugin.write_text(source, encoding="utf-8")
            report = import_wgsm_plugin(str(plugin), templates_dir, template_name="BetaGame")
            template = Path(templates_dir) / str(report["template_name"])
            config = json.loads((template / "config.json").read_text(encoding="utf-8"))
            self.assertEqual("123456", config["app_id"])
            self.assertEqual(["-beta", "experimental"], config["steam_update_args"])
            self.assertEqual("steam_ready", report["compatibility"])

    def test_rejects_archive_source_flood(self):
        with tempfile.TemporaryFile() as stream:
            with zipfile.ZipFile(stream, "w") as archive:
                for index in range(257):
                    archive.writestr(f"Plugin{index}.cs", "public class Plugin {}")
            stream.seek(0)
            with self.assertRaises(WgsmImportError):
                _sources_from_zip(stream.read())

    def test_rejects_remote_hosts_resolving_to_private_addresses(self):
        private_result = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch("wgsm_import.socket.getaddrinfo", return_value=private_result):
            with self.assertRaises(WgsmImportError):
                _validate_public_https_url("https://localhost/plugin.cs")


if __name__ == "__main__":
    unittest.main()
