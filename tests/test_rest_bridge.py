import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cli_commands
import desktop_ui
import rest_bridge


def _config() -> dict:
    return {
        "parameters": ["-AdminPassword=secret"],
        "rest_api": {
            "enabled": True,
            "base_url": "http://127.0.0.1:8212",
            "auth": {
                "type": "basic",
                "username": "admin",
                "password_from_parameter": "AdminPassword",
            },
            "actions": {
                "enabled": True,
                "commands": {
                    "announce": {
                        "method": "POST",
                        "path": "/v1/api/announce",
                        "arguments": [
                            {
                                "name": "message",
                                "required": True,
                                "consume_rest": True,
                                "max_length": 500,
                            }
                        ],
                    },
                    "save": {"method": "POST", "path": "/v1/api/save", "arguments": []},
                },
            },
        },
    }


class RestBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_actions_require_both_feature_flags(self):
        config = _config()
        config["rest_api"]["actions"]["enabled"] = False
        self.assertEqual([], rest_bridge.describe_rest_actions(config))
        config["rest_api"]["actions"]["enabled"] = True
        config["rest_api"]["enabled"] = False
        self.assertEqual([], rest_bridge.describe_rest_actions(config))

    def test_lifecycle_actions_and_paths_are_never_exposed(self):
        config = _config()
        config["rest_api"]["actions"]["commands"].update(
            {
                "shutdown": {"method": "POST", "path": "/v1/api/shutdown"},
                "hidden-stop": {"method": "POST", "path": "/v1/api/stop"},
                "nested-stop": {"method": "POST", "path": "/v1/api/stop/now"},
                "encoded-stop": {"method": "POST", "path": "/v1/api/%73top/now"},
                "external": {"method": "POST", "path": "https://example.com/action"},
            }
        )
        self.assertEqual(
            ["announce <message>", "save"],
            rest_bridge.describe_rest_actions(config),
        )

    async def test_executes_configured_action_with_bounded_body_and_auth(self):
        captured = {}

        def fake_http(url, headers, timeout, body):
            captured.update(url=url, headers=headers, timeout=timeout, body=body)
            return {"message": "announced"}

        with patch("rest_bridge._http_action", side_effect=fake_http):
            ok, message = await rest_bridge.execute_rest_action(
                "Palworld",
                _config(),
                "announce",
                ["Hello", "guild"],
                running=True,
            )

        self.assertTrue(ok)
        self.assertIn("announced", message)
        self.assertEqual("http://127.0.0.1:8212/v1/api/announce", captured["url"])
        self.assertEqual({"message": "Hello guild"}, captured["body"])
        self.assertTrue(captured["headers"]["Authorization"].startswith("Basic "))

    def test_snapshot_cache_signature_changes_with_credentials(self):
        first = _config()
        second = _config()
        second["parameters"] = ["-AdminPassword=changed"]
        self.assertNotEqual(
            rest_bridge._cache_config_signature(first["rest_api"], first),
            rest_bridge._cache_config_signature(second["rest_api"], second),
        )

    async def test_shared_cli_dispatches_api_actions(self):
        config = _config()
        with (
            patch("cli_commands._resolve_server", return_value=("Palworld", None)),
            patch("cli_commands._is_running", return_value=True),
            patch.dict(cli_commands.SERVER_CONFIGS, {"Palworld": config}, clear=True),
            patch("cli_commands.execute_rest_action", new=AsyncMock(return_value=(True, "Palworld: announced"))) as action,
            patch("cli_commands.write_action_log"),
        ):
            result = await cli_commands.execute_cli_command(
                'api Palworld announce "Hello guild"',
                source="test",
            )

        self.assertTrue(result.ok)
        self.assertTrue(result.refresh)
        action.assert_awaited_once_with(
            "Palworld",
            config,
            "announce",
            ["Hello guild"],
            running=True,
        )

    async def test_malformed_cli_quotes_are_rejected(self):
        with patch("cli_commands.write_action_log"):
            result = await cli_commands.execute_cli_command('api Palworld announce "unfinished', source="test")
        self.assertFalse(result.ok)
        self.assertIn("No command provided", result.message)

    def test_player_metric_uses_only_enabled_available_api_data(self):
        rows = [
            {
                "rest": {
                    "enabled": True,
                    "available": True,
                    "sections": {"metrics": {"currentplayernum": 3, "maxplayernum": 32}},
                }
            },
            {
                "rest": {
                    "enabled": False,
                    "available": True,
                    "sections": {"metrics": {"currentplayernum": 99, "maxplayernum": 99}},
                }
            },
        ]
        self.assertEqual("3/32 | 1 API", desktop_ui._api_player_metric(rows))

    def test_resource_process_scan_is_cached_for_two_seconds(self):
        desktop_ui._METRICS_CACHE = {}
        desktop_ui._METRICS_CACHE_TS = 0.0
        with (
            patch("desktop_ui._collect_server_process_metrics", return_value={}) as server_scan,
            patch(
                "desktop_ui._collect_bot_process_metrics",
                return_value={"cpu": 0.0, "rss": 0, "read_bps": 0.0, "write_bps": 0.0},
            ),
        ):
            desktop_ui._collect_system_metrics([])
            desktop_ui._collect_system_metrics([])
        server_scan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
