import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from template_utils import parse_parameters, format_parameters, normalize_server_settings


class ParameterTests(unittest.TestCase):
    def test_editable_text_roundtrips_argument_boundaries(self):
        cases = [
            ["-name", "My Server"],
            ["--config", r"C:\Game Files\settings.ini"],
            ["", "a'b", 'a"b', "back\\slash", "#literal", "  spaced  "],
            [],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(arguments, parse_parameters(format_parameters(arguments)))
                self.assertEqual(arguments, normalize_server_settings({"parameters": arguments})["parameters"])

    def test_unquoted_windows_path_preserves_backslashes(self):
        self.assertEqual(["--config", r"C:\Game\settings.ini"], parse_parameters(r"--config C:\Game\settings.ini"))

    def test_unclosed_quotes_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_parameters('"unfinished')
