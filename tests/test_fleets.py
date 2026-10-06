import json
from pathlib import Path
import sys
import tempfile
import unittest

from attraccess_tunnel.fleets import fleet_label, list_fleets


class FleetTests(unittest.TestCase):
    def balena(self, directory, output, status=0):
        script = Path(directory) / "balena"
        script.write_text(f"#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ['fleet','list','--json']\nprint({output!r})\nsys.exit({status})\n")
        script.chmod(0o755)
        return str(script)

    def test_accepts_balena_json_and_double_encoded_json(self):
        fleets = [{"app_name": "Test fleet", "slug": "myorg/myfleet", "device_type": "raspberrypi4-64"}]
        for output in (json.dumps(fleets), json.dumps(json.dumps(fleets))):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(list_fleets(self.balena(directory, output), 5), fleets)

    def test_empty_invalid_and_failed_lists_have_clear_errors(self):
        for output, status, message in (
            ("[]", 0, "No accessible balena fleets"),
            ("not JSON", 0, "invalid fleet list"),
            (json.dumps([{"slug": "--bad"}]), 0, "invalid fleet list"),
            ("Not logged in", 1, "Not logged in"),
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, message):
                    list_fleets(self.balena(directory, output, status), 5)

    def test_cloud_fleet_names_cannot_emit_terminal_controls(self):
        label = fleet_label({"app_name": "Test\x1b[31m\nfleet", "slug": "org/fleet"})
        self.assertNotIn("\x1b", label)
        self.assertNotIn("\n", label)


if __name__ == "__main__":
    unittest.main()
