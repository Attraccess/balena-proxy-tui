import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from attraccess_tunnel.cli import local_access


class SudoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.cache = root / "sudo-context"
        self.hosts = root / "hosts"
        self.original = "127.0.0.1 localhost\n192.0.2.1 app.example other\n"
        self.hosts.write_text(self.original)
        self.sudo = root / "sudo"
        # Model a session-scoped sudo timestamp. Validation is successful, but
        # a detached child has a different session and cannot use that timestamp.
        self.sudo.write_text(
            f"#!{sys.executable}\n"
            "import os,sys; from pathlib import Path\n"
            f"cache=Path({str(self.cache)!r})\n"
            "if sys.argv[1:] == ['-v']:\n"
            "    cache.write_text(str(os.getsid(0))); sys.exit(0)\n"
            "if sys.argv[1] == '-n':\n"
            "    if cache.read_text() != str(os.getsid(0)):\n"
            "        print('sudo: a password is required', file=sys.stderr); sys.exit(1)\n"
            "    os.execv(sys.argv[2], sys.argv[2:])\n"
            "sys.exit(2)\n"
        )
        self.sudo.chmod(0o755)
        self.helper = root / "helper.py"
        self.source = str(Path(__file__).resolve().parents[1] / "src")
        # Run the actual helper/handlers against temporary files, without root.
        self.helper.write_text(
            f"import sys; sys.path.insert(0, {self.source!r})\n"
            "import functools; from pathlib import Path\n"
            "from attraccess_tunnel import privileged\n"
            "privileged.os.geteuid=lambda:0\n"
            f"privileged.helper=functools.partial(privileged.helper, hosts_path=Path({str(self.hosts)!r}), lock_path=Path({str(root / 'lock')!r}))\n"
            "sys.exit(privileged.main())\n"
        )

    def test_authenticated_helper_keeps_validation_session_and_cleans_up(self):
        with patch.dict(os.environ, {"PATH": self.directory.name + os.pathsep + os.environ.get("PATH", "")}), \
             patch("attraccess_tunnel.cli.os.geteuid", return_value=501), \
             patch("attraccess_tunnel.cli.privileged.__file__", str(self.helper)):
            with local_access(["app.example"], [], 3) as helper:
                self.assertIsNone(helper.poll())
                self.assertIn("127.0.0.1 app.example", self.hosts.read_text())
                helper.stdin.write("ping\n")
                helper.stdin.flush()
        self.assertEqual(self.hosts.read_text(), self.original)

    def test_terminal_signals_restore_hosts_in_authenticated_helper(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig), \
                 patch.dict(os.environ, {"PATH": self.directory.name + os.pathsep + os.environ.get("PATH", "")}), \
                 patch("attraccess_tunnel.cli.os.geteuid", return_value=501), \
                 patch("attraccess_tunnel.cli.privileged.__file__", str(self.helper)):
                with local_access(["app.example"], [], 3) as helper:
                    self.assertIn("127.0.0.1 app.example", self.hosts.read_text())
                    helper.send_signal(sig)
                    self.assertEqual(helper.wait(timeout=3), 0)
                self.assertEqual(self.hosts.read_text(), self.original)

    def test_parent_death_still_restores_hosts_in_shared_session(self):
        driver = (
            f"import sys; sys.path.insert(0, {self.source!r})\n"
            "import json,time; from attraccess_tunnel import cli\n"
            "cli.os.geteuid=lambda:501\n"
            f"cli.privileged.__file__={str(self.helper)!r}\n"
            "with cli.local_access(['app.example'], [], 3) as helper:\n"
            "    print(json.dumps({'helper_pid':helper.pid}), flush=True)\n"
            "    time.sleep(100)\n"
        )
        child = subprocess.Popen([sys.executable, "-u", "-c", driver], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 env={**os.environ, "PATH": self.directory.name + os.pathsep + os.environ.get("PATH", "")})

        def close():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            deadline = time.monotonic() + 3
            while self.hosts.read_text() != self.original and time.monotonic() < deadline:
                time.sleep(0.05)

        self.addCleanup(close)
        output = []
        for line in child.stdout:
            output.append(line)
            if line.startswith('{"helper_pid":'):
                break
        else:
            self.fail("Helper did not start: " + "".join(output))
        self.assertIn("127.0.0.1 app.example", self.hosts.read_text())
        child.kill()
        child.wait(timeout=3)
        deadline = time.monotonic() + 3
        while self.hosts.read_text() != self.original and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.hosts.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
