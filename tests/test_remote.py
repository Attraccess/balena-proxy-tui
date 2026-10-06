from pathlib import Path
import sys
import tempfile
import unittest

from attraccess_tunnel.processes import remote_command


class RemoteCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.balena = Path(self.directory.name) / "balena"
        # Exercise the actual submitted shell script. Model a gateway that
        # returns success even when the device's command fails.
        self.balena.write_text(
            f"#!{sys.executable}\n"
            "import subprocess,sys\n"
            "assert sys.argv[1:3] == ['device', 'ssh']\n"
            "subprocess.run(['/bin/sh'], input=sys.stdin.read(), text=True)\n"
            "sys.exit(0)\n"
        )
        self.balena.chmod(0o755)

    def test_remote_failure_preserves_diagnostic_despite_successful_gateway_exit(self):
        command = "sh -c 'printf \"template parsing error: ExposedPorts missing\\n\" >&2; exit 7'"
        with self.assertRaisesRegex(RuntimeError, "template parsing error: ExposedPorts missing"):
            remote_command(str(self.balena), "f" * 32, command, 5)

    def test_multiline_command_preserves_output(self):
        output = remote_command(str(self.balena), "f" * 32, "printf '%s\\n' 'first'\nprintf '%s\\n' 'second'", 5)
        self.assertEqual(output.strip(), "first\nsecond")

    def test_missing_completion_status_rejects_partial_output(self):
        self.balena.write_text(f"#!{sys.executable}\nprint('partial output')\n")
        with self.assertRaisesRegex(RuntimeError, "missing remote completion marker"):
            remote_command(str(self.balena), "f" * 32, "printf done", 5)


if __name__ == "__main__":
    unittest.main()
