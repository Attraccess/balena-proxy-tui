import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from attraccess_tunnel.privileged import patch_hosts
from attraccess_tunnel.processes import BalenaTerminal, free_ports, select_device, wait_for_tunnel


SOURCE = str(Path(__file__).resolve().parents[1] / "src")
PYTHONPATH = os.pathsep.join(filter(None, (SOURCE, os.environ.get("PYTHONPATH", ""))))
UUID = "f" * 32


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hosts = Path(self.directory.name) / "hosts"
        self.lock = Path(self.directory.name) / "lock"
        self.original = "127.0.0.1 localhost\n10.0.0.1 app.example alias\n"
        self.hosts.write_text(self.original)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream and not stream.closed:
                    stream.close()

    def start_helper(self, mappings=(), tunnel_pid=None):
        code = (
            "import json,sys; from pathlib import Path; from attraccess_tunnel.privileged import helper; "
            "helper(json.loads(sys.stdin.readline()), Path(sys.argv[1]), Path(sys.argv[2]))"
        )
        child = subprocess.Popen([sys.executable, "-c", code, str(self.hosts), str(self.lock)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env={**os.environ, "PYTHONPATH": PYTHONPATH})
        self.children.append(child)
        child.stdin.write(json.dumps({"domains": ["app.example"], "mappings": list(mappings), "tunnel_pid": tunnel_pid}) + "\n")
        child.stdin.flush()
        self.assertTrue(select.select([child.stdout], [], [], 5)[0], "helper did not respond")
        return child

    def test_eof_restores_hosts_and_terminates_owned_tunnel(self):
        tunnel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(100)"], start_new_session=True)
        self.children.append(tunnel)
        helper = self.start_helper(tunnel_pid=tunnel.pid)
        self.assertTrue(json.loads(helper.stdout.readline())["ready"])
        self.hosts.write_text(self.hosts.read_text() + "10.0.0.2 added.example\n")
        helper.stdin.close()
        self.assertEqual(helper.wait(timeout=5), 0, helper.stderr.read())
        self.assertEqual(self.hosts.read_text(), self.original + "10.0.0.2 added.example\n")
        tunnel.wait(timeout=5)
        self.assertNotEqual(tunnel.returncode, 0)

    def test_stale_hosts_recovered_on_start(self):
        self.hosts.write_text(patch_hosts(self.original, ["old.example"], "b" * 32))
        child = self.start_helper()
        self.assertTrue(json.loads(child.stdout.readline())["ready"])
        self.assertNotIn("old.example", self.hosts.read_text())
        child.stdin.close()
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual(self.hosts.read_text(), self.original)

    def test_active_session_lock_prevents_other_session(self):
        first = self.start_helper()
        self.assertTrue(json.loads(first.stdout.readline())["ready"])
        during = self.hosts.read_text()
        second = self.start_helper()
        self.assertEqual(second.stdout.read(), "")
        self.assertNotEqual(second.wait(timeout=5), 0)
        self.assertIn("Another Attraccess tunnel is active", second.stderr.read())
        self.assertEqual(self.hosts.read_text(), during)
        first.stdin.close()
        self.assertEqual(first.wait(timeout=5), 0)

    def test_tcp_relay_preserves_response_after_request_half_close(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen()
        upstream = server.getsockname()[1]
        local = free_ports(1)[0]

        def reply_after_eof():
            connection, _ = server.accept()
            with connection:
                chunks = []
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                connection.sendall(b"response:" + b"".join(chunks))

        thread = threading.Thread(target=reply_after_eof, daemon=True)
        thread.start()
        child = self.start_helper([[local, upstream]])
        self.assertTrue(json.loads(child.stdout.readline())["ready"])
        with socket.create_connection(("127.0.0.1", local), timeout=3) as client:
            client.sendall(b"request")
            client.shutdown(socket.SHUT_WR)
            response = bytearray()
            while chunk := client.recv(4096):
                response.extend(chunk)
        self.assertEqual(response, b"response:request")
        child.stdin.close()
        self.assertEqual(child.wait(timeout=5), 0, child.stderr.read())
        self.assertEqual(self.hosts.read_text(), self.original)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", local), timeout=0.2)

    def test_busy_local_port_leaves_hosts_unchanged(self):
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        child = self.start_helper([[occupied.getsockname()[1], free_ports(1)[0]]])
        self.assertEqual(child.stdout.read(), "")
        self.assertNotEqual(child.wait(timeout=5), 0)
        self.assertIn("Cannot bind", child.stderr.read())
        self.assertEqual(self.hosts.read_text(), self.original)

    def test_selection_uses_balena_terminal_and_pins_selected_uuid(self):
        fake = Path(self.directory.name) / "balena"
        fake.write_text(f"#!{sys.executable}\nimport os,time\nassert all(os.isatty(fd) for fd in (0,1,2))\nprint('Opening a tunnel to {UUID}...', flush=True)\ntime.sleep(100)\n")
        fake.chmod(0o755)
        self.assertEqual(select_device(str(fake), "org/fleet"), UUID)

    def test_partial_balena_startup_is_rejected(self):
        code = f"print(' - tunnelling 127.0.0.1:30000 to {UUID}:80', flush=True); print('Waiting for connections...', flush=True); import time; time.sleep(100)"
        with BalenaTerminal([sys.executable, "-c", code], display=False) as child:
            with self.assertRaisesRegex(RuntimeError, "every port"):
                wait_for_tunnel(child, [(80, 30000), (443, 30001)], 3)

    def test_changed_selection_status_fails_instead_of_hanging(self):
        fake = Path(self.directory.name) / "balena"
        fake.write_text(f"#!{sys.executable}\nimport time\nprint('Waiting for connections...', flush=True)\ntime.sleep(100)\n")
        fake.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "expects balena device tunnel"):
            select_device(str(fake), "org/fleet")


if __name__ == "__main__":
    unittest.main()
