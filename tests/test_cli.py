import base64
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from attraccess_tunnel.processes import free_ports


SOURCE = str(Path(__file__).resolve().parents[1] / "src")
# Nix supplies dependencies through PYTHONPATH rather than a virtualenv's site.
PYTHONPATH = os.pathsep.join(filter(None, (SOURCE, os.environ.get("PYTHONPATH", ""))))
UUID = "f" * 32


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.hosts = root / "hosts"
        self.original = "127.0.0.1 localhost\n192.0.2.1 app.example other\n"
        self.hosts.write_text(self.original)
        self.port = free_ports(1)[0]
        self.log = root / "balena-calls.jsonl"
        self.balena = root / "balena"
        containers = [{"id": "a" * 64, "name": "proxy", "image": "balena-registry/release",
                       "labels": {"io.balena.service-name": "proxy"}, "network": "bridge",
                       "ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(self.port)}]}, "exposed": {}}]
        containers.append({"id": "b" * 64, "name": "attraccess", "image": "balena-registry/app",
                           "labels": {"io.balena.service-name": "attraccess"}, "network": "bridge",
                           "ports": {}, "exposed": {"3000/tcp": {}}})
        data = {"hosts": [{"names": ["app.example"], "type": "proxy_host", "enabled": True,
                           "certificate_id": 0, "forward_host": "attraccess", "forward_port": 3000,
                           "forward_scheme": "http"}], "streams": []}
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        script = f"""#!{sys.executable}
import json,os,re,signal,socket,sys,time
with open({str(self.log)!r}, 'a') as log:
    log.write(json.dumps({{'args':sys.argv[1:], 'pid':os.getpid()}}) + '\\n')
if sys.argv[1:3] == ['fleet','list']:
    if os.environ.get('FAKE_SLOW_STAGE') == 'fleet': time.sleep(20)
    print(json.dumps([{{'app_name':'First fleet', 'slug':'org/first', 'device_type':'raspberrypi4-64'}},
                      {{'app_name':'Second fleet', 'slug':'org/second', 'device_type':'raspberrypi4-64'}}]))
elif sys.argv[1:3] == ['device','tunnel']:
    assert all(os.isatty(fd) for fd in (0,1,2))
    if '/' in sys.argv[3]:
        import tty
        tty.setraw(0)
        print('Select a device on fleet ' + sys.argv[3], flush=True)
        while os.read(0, 1) not in (b'\\r', b'\\n'): pass
    print('Opening a tunnel to {UUID}...', flush=True)
    if os.environ.get('FAKE_SLOW_STAGE') == 'tunnel' and not any(item.startswith('22222:') for item in sys.argv): time.sleep(20)
    sockets=[]
    for index, item in enumerate(sys.argv):
        if item == '-p':
            remote,ip,local=sys.argv[index+1].split(':')
            listener=socket.socket(); listener.bind((ip,int(local))); listener.listen(); sockets.append(listener)
            print(f' - tunnelling {{ip}}:{{local}} to {UUID}:{{remote}}', flush=True)
    print('Waiting for connections...', flush=True)
    while True: time.sleep(1)
elif sys.argv[1:3] == ['device','ssh']:
    command=sys.stdin.read()
    if os.environ.get('FAKE_SLOW_STAGE') == 'discover': time.sleep(20)
    if os.environ.get('FAKE_FAIL_DISCOVERY'):
        marker = re.search(r'__ATTRACCESS_COMMAND_[0-9a-f]{{32}}__', command)
        print(marker.group(0) + '1'); print('simulated SSH failure', file=sys.stderr); sys.exit(1)
    if 'balena-engine inspect' in command:
        print('__ATTRACCESS_CONTAINERS__BEGIN')
        for row in {containers!r}: print(json.dumps(row))
        print('__ATTRACCESS_CONTAINERS__END')
    else: print('__ATTRACCESS_PROXY__{encoded}')
    marker = re.search(r'__ATTRACCESS_COMMAND_[0-9a-f]{{32}}__', command)
    if marker: print(marker.group(0) + '0')
else: sys.exit(2)
"""
        self.balena.write_text(script)
        self.balena.chmod(0o755)
        # Test the real CLI/control protocol with an isolated, unprivileged helper.
        # The wrapper replaces only sudo/root setup and the hosts/lock paths.
        wrapper = root / "helper.py"
        wrapper.write_text(
            f"import sys; sys.path.insert(0, {SOURCE!r})\n"
            "import json; from pathlib import Path; from attraccess_tunnel.privileged import helper\n"
            f"helper(json.loads(sys.stdin.readline()), Path({str(self.hosts)!r}), Path({str(root / 'lock')!r}))\n"
        )
        self.driver = (
            "from attraccess_tunnel import cli; "
            "cli.os.geteuid=lambda:0; "
            f"cli.privileged.__file__={str(wrapper)!r}; cli.main()"
        )

    def start(self, *extra):
        child = subprocess.Popen([sys.executable, "-u", "-c", self.driver,
                                  UUID, "--balena", str(self.balena), "--timeout", "5", *extra],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env={**os.environ, "PYTHONPATH": PYTHONPATH})
        self.addCleanup(self.close_child, child)
        return child

    def close_child(self, child):
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        child.stdout.close()

    def wait_ready(self, child):
        output = b""
        deadline = time.monotonic() + 5
        while b"Ready." not in output and time.monotonic() < deadline:
            if select.select([child.stdout], [], [], 0.2)[0]:
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    break
                output += chunk
        self.assertIn(b"Ready.", output, output.decode())

    def interactive_child(self, *extra, dry_run=True, env=None, driver=None):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before = termios.tcgetattr(slave)
        child = subprocess.Popen([sys.executable, "-u", "-c", driver or self.driver,
                                  "--balena", str(self.balena), "--timeout", "5",
                                  *(["--dry-run"] if dry_run else []), *extra],
                                 stdin=slave, stdout=slave, stderr=subprocess.STDOUT,
                                 env={**os.environ, "PYTHONPATH": PYTHONPATH, "TERM": "xterm-256color", **(env or {})})

        def close():
            if child.poll() is None:
                child.terminate()
            # Drain the PTY while exiting: writing the final frame/alternate
            # screen restoration must not block cleanup on a full PTY buffer.
            deadline = time.monotonic() + 5
            while child.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

        self.addCleanup(close)
        return child, master, slave, before

    def read_until(self, master, text):
        output = b""
        deadline = time.monotonic() + 5
        while text not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                output += os.read(master, 65536)
        self.assertTrue(text in output, f"Did not see {text!r}:\n" + output[-6000:].decode("utf-8", "replace"))
        return output

    def read_exit(self, child, master):
        output = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                output.extend(os.read(master, 65536))
            elif child.poll() is not None:
                break
        self.assertIsNotNone(child.poll(), output[-6000:].decode("utf-8", "replace"))
        return bytes(output)

    def assert_terminal_restored(self, slave, before):
        after = termios.tcgetattr(slave)
        before = list(before)
        # PENDIN is a transient kernel flag for retyping pending input on macOS.
        # Compare the actual input modes and controls rather than this flag.
        before[3] &= ~getattr(termios, "PENDIN", 0)
        after[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(after, before)

    def test_no_target_selects_fleet_then_device_with_keyboard(self):
        child, master, slave, before = self.interactive_child("--no-tui")
        self.read_until(master, b"Select a fleet")
        os.write(master, b"\x1b[B\r")
        self.read_until(master, b"Select a device on fleet org/second")
        os.write(master, b"\r")
        self.read_until(master, b"Domains: app.example")
        self.assertEqual(child.wait(timeout=5), 0)
        self.assert_terminal_restored(slave, before)
        self.assertEqual(self.hosts.read_text(), self.original)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls[0]["args"], ["fleet", "list", "--json"])
        self.assertEqual(calls[1]["args"][2], "org/second")
        self.assertTrue(all(call["args"][2] == UUID for call in calls[2:]))

    def test_fullscreen_starts_before_fleet_loading_and_remains_for_the_whole_flow(self):
        child, master, slave, before = self.interactive_child(dry_run=False)
        output = self.read_until(master, b"Choose a fleet to browse")
        self.assertTrue(output.startswith(b"\x1b[?1049h"))
        self.assertIn(b"Loading balena fleets", output)
        os.write(master, b"\x1b[B\r")
        output += self.read_until(master, b"Select a device on fleet org/second")
        os.write(master, b"\r")
        output += self.read_until(master, b"Ctrl+C stop & clean up")
        self.assertIn(b"Discovering device services", output)
        self.assertIn(b"Opening device tunnel", output)
        self.assertIn(b"Opening local access", output)
        self.assertIn(f"http://app.example:{self.port}".encode(), output)
        os.write(master, b"q")
        output += self.read_exit(child, master)
        self.assertIn(b"Restoring local access", output)
        self.assertIn(b"Closing device tunnel", output)
        self.assertEqual(output.count(b"\x1b[?1049h"), 1)
        self.assertEqual(output.count(b"\x1b[?1049l"), 1)
        self.assertEqual(child.returncode, 0)
        self.assert_terminal_restored(slave, before)
        self.assertEqual(self.hosts.read_text(), self.original)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls[1]["args"][2], "org/second")

    def test_fullscreen_loading_cancels_without_waiting_for_subprocess_timeout(self):
        for stage, target, title in (("fleet", None, b"Loading balena fleets"),
                                     ("discover", UUID, b"Discovering device services"),
                                     ("tunnel", UUID, b"Opening device tunnel")):
            with self.subTest(stage=stage):
                self.log.write_text("")
                child, master, slave, before = self.interactive_child(*([target] if target else []),
                    dry_run=False, env={"FAKE_SLOW_STAGE": stage})
                output = self.read_until(master, title)
                # Observe multiple frames while the operation is deliberately
                # slow, then cancel before its timeout expires.
                output += self.read_until(master, title)
                start = time.monotonic()
                os.write(master, b"q")
                output += self.read_exit(child, master)
                self.assertLess(time.monotonic() - start, 3)
                self.assertEqual(child.returncode, 0)
                self.assertEqual(output.count(b"\x1b[?1049h"), 1)
                self.assertEqual(output.count(b"\x1b[?1049l"), 1)
                self.assert_terminal_restored(slave, before)
                self.assertEqual(self.hosts.read_text(), self.original)

    def test_fullscreen_errors_stay_in_the_app_until_dismissed(self):
        child, master, slave, before = self.interactive_child(UUID, dry_run=False, env={"FAKE_FAIL_DISCOVERY": "1"})
        output = self.read_until(master, b"Enter to close")
        self.assertTrue(b"simulated SSH" in output and b"failure" in output)
        self.assertIsNone(child.poll())
        os.write(master, b"\r")
        output += self.read_exit(child, master)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(output.count(b"\x1b[?1049h"), 1)
        self.assertEqual(output.count(b"\x1b[?1049l"), 1)
        self.assert_terminal_restored(slave, before)
        self.assertEqual(self.hosts.read_text(), self.original)

    def test_fullscreen_dry_run_keeps_a_preview_open_without_hosts_changes(self):
        child, master, slave, before = self.interactive_child(UUID)
        output = self.read_until(master, b"Discovery preview.")
        self.assertIn(b"PREVIEW", output)
        self.assertIn(f"http://app.example:{self.port}".encode(), output)
        self.assertIsNone(child.poll())
        self.assertEqual(self.hosts.read_text(), self.original)
        os.write(master, b"q")
        self.read_exit(child, master)
        self.assertEqual(child.returncode, 0)
        self.assert_terminal_restored(slave, before)

    def test_fullscreen_sudo_askpass_is_masked_and_helper_reuses_authorization(self):
        root = Path(self.directory.name)
        sudo = root / "sudo"
        auth_context = root / "auth-context"
        # Authenticate through sudo's real askpass protocol, with a simulated
        # credential and per-session timestamp; never require a real password.
        sudo.write_text(
            f"#!{sys.executable}\n"
            "import json,os,subprocess,sys; from pathlib import Path\n"
            f"cache=Path({str(auth_context)!r})\n"
            "if sys.argv[1:] == ['-A','-v']:\n"
            "    rejected=subprocess.check_output([os.environ['SUDO_ASKPASS'],'Password:'])\n"
            "    if rejected != b'rejected-test-password\\n': sys.exit(1)\n"
            "    print('Sorry, try again.',file=sys.stderr,flush=True)\n"
            "    credential=subprocess.check_output([os.environ['SUDO_ASKPASS'],'Password:'])\n"
            "    if credential != b'simulated-secret\\n': sys.exit(1)\n"
            "    cache.write_text(json.dumps({'session':os.getsid(0),'askpass':os.environ['SUDO_ASKPASS']})); sys.exit(0)\n"
            "if sys.argv[1] == '-n':\n"
            "    if json.loads(cache.read_text())['session'] != os.getsid(0): sys.exit(1)\n"
            "    os.execv(sys.argv[2],sys.argv[2:])\n"
            "sys.exit(2)\n"
        )
        sudo.chmod(0o755)
        child, master, slave, before = self.interactive_child(UUID, dry_run=False,
            env={"PATH": self.directory.name + os.pathsep + os.environ.get("PATH", "")},
            driver=self.driver.replace("geteuid=lambda:0", "geteuid=lambda:501"))
        output = self.read_until(master, b"Password input is hidden")
        os.write(master, b"rejected-test-password\r")
        output += self.read_until(master, b"Sorry, try again.")
        os.write(master, b"wrong\x15simulated-secrex\x7ft")
        output += self.read_until(master, "●●●●●".encode())
        os.write(master, b"\r")
        output += self.read_until(master, b"Ctrl+C stop & clean up")
        self.assertIn("127.0.0.1 app.example", self.hosts.read_text())
        os.write(master, b"q")
        output += self.read_exit(child, master)
        self.assertEqual(child.returncode, 0)
        self.assertNotIn(b"simulated-secret", output)
        self.assertNotIn(b"rejected-test-password", output)
        self.assertNotIn(b"wrong", output)
        self.assertEqual(output.count(b"\x1b[?1049h"), 1)
        self.assertEqual(output.count(b"\x1b[?1049l"), 1)
        self.assert_terminal_restored(slave, before)
        self.assertEqual(self.hosts.read_text(), self.original)
        self.assertFalse(Path(json.loads(auth_context.read_text())["askpass"]).exists())

    def test_fullscreen_fleet_search_keeps_the_native_device_picker(self):
        child, master, slave, before = self.interactive_child(dry_run=True)
        output = self.read_until(master, b"Choose a fleet to browse")
        os.write(master, b"/second\r")
        output += self.read_until(master, b"Select a device on fleet org/second")
        os.write(master, b"\r")
        output += self.read_until(master, b"Discovery preview.")
        os.write(master, b"q")
        output += self.read_exit(child, master)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(output.count(b"\x1b[?1049h"), 1)
        self.assertEqual(output.count(b"\x1b[?1049l"), 1)
        self.assert_terminal_restored(slave, before)
        self.assertEqual(self.hosts.read_text(), self.original)

    def test_fullscreen_sudo_cancel_ends_authentication_without_hosts_changes(self):
        root = Path(self.directory.name)
        sudo = root / "sudo"
        sudo.write_text(f"#!{sys.executable}\nimport os,subprocess,sys\n"
                        "sys.exit(subprocess.call([os.environ['SUDO_ASKPASS'],'Password:']))\n")
        sudo.chmod(0o755)
        child, master, slave, before = self.interactive_child(UUID, dry_run=False,
            env={"PATH": self.directory.name + os.pathsep + os.environ.get("PATH", "")},
            driver=self.driver.replace("geteuid=lambda:0", "geteuid=lambda:501"))
        output = self.read_until(master, b"Password input is hidden")
        os.write(master, b"\x1b")
        output += self.read_exit(child, master)
        self.assertEqual(child.returncode, 0)
        self.assertNotIn(b"Traceback", output)
        self.assert_terminal_restored(slave, before)
        self.assertEqual(self.hosts.read_text(), self.original)

    def test_cancel_fleet_selection_restores_terminal_without_device_access(self):
        child, master, slave, before = self.interactive_child("--no-tui")
        self.read_until(master, b"Select a fleet")
        os.write(master, b"\x1b")
        self.assertEqual(child.wait(timeout=5), 0)
        self.assert_terminal_restored(slave, before)
        self.assertEqual(self.hosts.read_text(), self.original)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 1)

    def test_no_target_without_terminal_has_actionable_error(self):
        child = subprocess.run([sys.executable, "-c", self.driver, "--balena", str(self.balena)],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": PYTHONPATH}, timeout=5)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("Interactive fleet selection requires a terminal", child.stderr)
        self.assertFalse(self.log.exists())

    def test_dry_run_selects_discovers_and_never_edits_hosts(self):
        child = self.start("--dry-run")
        output = child.communicate(timeout=5)[0].decode()
        self.assertEqual(child.returncode, 0, output)
        self.assertIn("Domains: app.example", output)
        self.assertIn(f"Device TCP ports: {self.port}", output)
        self.assertIn(f"http://app.example:{self.port}", output)
        self.assertIn("proxy:80 → attraccess:3000", output)
        self.assertIn(f"localhost:{self.port} → proxy:80", output)
        self.assertEqual(self.hosts.read_text(), self.original)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call["args"][2] == UUID for call in calls))

    def test_dashboard_keyboard_quit_and_signals_restore_terminal_and_hosts(self):
        for exit_key in (b"q", signal.SIGTERM, signal.SIGHUP):
            with self.subTest(exit=exit_key):
                child, master, slave, before = self.interactive_child(UUID, dry_run=False)
                output = self.read_until(master, b"Ctrl+C stop & clean up")
                self.assertIn(b"\x1b[?1049h", output)  # Alternate screen, without changing the native menus.
                self.assertIn(f"http://app.example:{self.port}".encode(), output)
                self.assertIn(b"\x1b]8;", output)  # Terminal hyperlinks retain the full URL.
                self.assertIn("127.0.0.1 app.example", self.hosts.read_text())
                os.write(master, b"\t")
                self.read_until(master, b"Local TCP address")
                os.write(master, b"\t")
                self.read_until(master, b"No notices")
                if isinstance(exit_key, bytes):
                    os.write(master, exit_key)
                else:
                    child.send_signal(exit_key)
                output = self.read_exit(child, master)
                self.assertTrue(b"cleaned up." in output)
                self.assertEqual(child.returncode, 0)
                self.assert_terminal_restored(slave, before)
                self.assertEqual(self.hosts.read_text(), self.original)
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", self.port), timeout=0.2)

    def test_no_tui_prints_clickable_urls_without_alternate_screen(self):
        child, master, slave, before = self.interactive_child(UUID, "--no-tui", dry_run=False)
        output = self.read_until(master, b"Ready.")
        self.assertIn(f"http://app.example:{self.port}".encode(), output)
        self.assertNotIn(b"\x1b[?1049h", output)
        child.terminate()
        self.assertEqual(child.wait(timeout=5), 0)
        self.assert_terminal_restored(slave, before)
        self.assertEqual(self.hosts.read_text(), self.original)

    def test_signals_restore_hosts_and_release_all_forwarding_ports(self):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGKILL):
            with self.subTest(signal=sig):
                self.log.write_text("")
                child = self.start()
                self.wait_ready(child)
                self.assertIn("127.0.0.1 app.example", self.hosts.read_text())
                child.send_signal(sig)
                child.wait(timeout=5)
                if sig != signal.SIGKILL:
                    self.assertEqual(child.returncode, 0, child.stdout.read().decode())
                deadline = time.monotonic() + 5
                while self.hosts.read_text() != self.original and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(self.hosts.read_text(), self.original)
                calls = [json.loads(line) for line in self.log.read_text().splitlines()]
                tunnel_calls = [call for call in calls if call["args"][1] == "tunnel"]
                forwarded_ports = [int(call["args"][-1].split(":")[-1]) for call in tunnel_calls]
                for port in [self.port, *forwarded_ports]:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                                pass
                        except OSError:
                            break
                        time.sleep(0.02)
                    else:
                        self.fail(f"Port {port} is still open after parent termination.")


if __name__ == "__main__":
    unittest.main()
