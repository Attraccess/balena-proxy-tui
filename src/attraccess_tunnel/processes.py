"""Run balena with its own terminal, preserving the native device picker."""

import errno
import fcntl
import os
import pty
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
import uuid as uuid_module


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


def communicate(process, timeout, request=None, tick=None):
    """Keep the UI responsive while subprocess pipes are drained concurrently."""
    if tick is None:
        return process.communicate(request, timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        tick()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.communicate(request, timeout=min(0.08, remaining))
        except subprocess.TimeoutExpired:
            request = None  # communicate() retains input/output across retries.


def stop_process(process, tick=None):
    """Terminate the entire child group, including ssh spawned by balena."""
    # Reap an exited leader first: macOS refuses signals to zombie-only groups.
    process.poll()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            if tick:
                tick()
            try:
                process.wait(timeout=0.08)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is not None:
            break
    process.wait()


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sockets.append(sock)
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class BalenaTerminal:
    def __init__(self, command, display=True, on_output=None, size=None, cleanup_tick=None):
        self.master, slave = pty.openpty()
        self.display = display
        self.on_output = on_output
        self.size = size
        self.cleanup_tick = cleanup_tick
        self.output = ""
        try:
            self.resize()
            self.process = subprocess.Popen(
                command, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)

    def resize(self):
        if self.size:
            columns, rows = self.size()
            size = struct.pack("HHHH", rows, columns, 0, 0)
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, size)
        elif sys.stdin.isatty():
            size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, size)

    def pump(self, timeout=0.2, interactive=False):
        self.resize()
        descriptors = [self.master]
        if interactive and sys.stdin.isatty():
            descriptors.append(sys.stdin.fileno())
        ready, _, _ = select.select(descriptors, [], [], timeout)
        if self.master in ready:
            try:
                data = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b""
            if data:
                if self.on_output:
                    self.on_output(data)
                if self.display:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                self.output = (self.output + data.decode("utf-8", "replace"))[-131072:]
        if interactive and sys.stdin.isatty() and sys.stdin.fileno() in ready:
            data = os.read(sys.stdin.fileno(), 4096)
            if b"\x03" in data or not data:
                raise KeyboardInterrupt
            os.write(self.master, data)

    @property
    def text(self):
        return ANSI.sub("", self.output)

    def close(self):
        stop_process(self.process, self.cleanup_tick)
        os.close(self.master)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def select_device(balena, target):
    port = free_ports(1)[0]
    saved_terminal = None
    with BalenaTerminal([balena, "device", "tunnel", target, "-p", f"22222:127.0.0.1:{port}"]) as child:
        try:
            if sys.stdin.isatty():
                saved_terminal = termios.tcgetattr(sys.stdin.fileno())
                tty.setraw(sys.stdin.fileno())
            while True:
                child.pump(interactive=True)
                match = re.search(r"Opening a tunnel to ([0-9a-f]{32})\.\.\.", child.text, re.I)
                if match:
                    return match.group(1).lower()
                if child.process.poll() is not None or "Waiting for connections..." in child.text:
                    raise RuntimeError(
                        "balena did not select a device. Check login, fleet name, and device connectivity. "
                        "This tool expects balena device tunnel to print 'Opening a tunnel to <UUID>...'."
                    )
        finally:
            if saved_terminal is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_terminal)
                sys.stdout.buffer.write(b"\x1b[?25h")
                sys.stdout.buffer.flush()


def remote_command(balena, uuid, command, timeout, tick=None):
    # The gateway's SSH exit status may not reflect the host command's status.
    # Frame it explicitly; a trailing plain `exit` would mask the failure.
    marker = "__ATTRACCESS_COMMAND_" + uuid_module.uuid4().hex + "__"
    script = command + f'\nstatus=$?\nprintf "\\n{marker}%s\\n" "$status"\nexit "$status"'
    request = "sh -c " + shlex.quote(script) + "\nexit $?\n"
    process = subprocess.Popen(
        [balena, "device", "ssh", uuid], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        output, errors = communicate(process, timeout, request.encode(), tick)
        text = ANSI.sub("", output.decode("utf-8", "replace"))
        status = re.search(r"^" + re.escape(marker) + r"(\d+)\r?$", text, re.M)
        if process.returncode or status is None or int(status[1]) != 0:
            detail = ANSI.sub("", errors.decode("utf-8", "replace")).strip()
            reason = f"remote exit {status[1]}" if status else "missing remote completion marker"
            if process.returncode:
                reason += f", SSH exit {process.returncode}"
            raise RuntimeError(f"balena SSH command failed ({reason})" + (": " + detail if detail else "."))
        return text[:status.start()]
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Device discovery timed out after {timeout:g}s. Check balena SSH access.") from exc
    finally:
        stop_process(process)


def wait_for_tunnel(child, mappings, timeout, tick=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tick:
            tick()
        child.pump(timeout=0.08 if tick else 0.2)
        bound = {
            (int(local), int(remote)) for local, remote in re.findall(
                r" - tunnelling 127\.0\.0\.1:(\d+) to [0-9a-f]+:(\d+)", child.text,
            )
        }
        if "Waiting for connections..." in child.text:
            missing = [(local, remote) for remote, local in mappings if (local, remote) not in bound]
            if missing:
                raise RuntimeError(f"balena failed to open every port: {missing}")
            return
        if child.process.poll() is not None:
            raise RuntimeError("balena tunnel exited before opening its ports.")
    raise RuntimeError(f"balena tunnel startup timed out after {timeout:g}s.")
