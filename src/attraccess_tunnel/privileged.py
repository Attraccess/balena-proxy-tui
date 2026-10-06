"""Isolated local helper: loopback TCP relays and recoverable hosts-file edits.

This file also runs directly under `python -I`, so it imports only the stdlib.
The balena CLI and its credentials never run in this privileged process.
"""

import base64
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import sys
import threading
import time
import uuid


HOSTS = Path("/etc/hosts")
LOCK = Path("/var/run/attraccess-balena-tunnel.lock")
TAG = re.compile(r"# attraccess-balena-tunnel\[([0-9a-f]{32})\]:(saved|alias|entry|begin|end)(?: (.*))?$")


def _join(lines):
    # Preserve a missing final newline, but separate any subsequent user additions.
    return "".join(line if line.endswith(("\n", "\r")) or i == len(lines) - 1 else line + "\n"
                   for i, line in enumerate(lines))


def restore_hosts(text, session=None):
    lines = []
    for line in text.splitlines(keepends=True):
        match = TAG.search(line.rstrip("\r\n"))
        if not match or (session is not None and match[1] != session):
            lines.append(line)
            continue
        kind, payload = match[2], match[3]
        if kind == "saved":
            if not payload:
                raise ValueError("A hosts recovery record is incomplete; refusing to overwrite it.")
            original = base64.b64decode(payload, validate=True).decode("utf-8", "surrogateescape")
            lines.append(original)
        elif kind == "alias":
            # Generated alias lines are recoverable from their saved original.
            # Keep edits to this line if the user changed it during the session.
            if not payload:
                raise ValueError("A hosts alias recovery record is incomplete.")
            expected = base64.b64decode(payload, validate=True).decode("utf-8", "surrogateescape")
            actual = line[:match.start()].rstrip()
            if actual != expected.rstrip():
                lines.append(actual + ("\n" if line.endswith("\n") else ""))
        elif kind == "entry":
            # Leave additional aliases/IP changes made by the user intact.
            expected = "127.0.0.1 " + (payload or "")
            actual = line[:match.start()].rstrip()
            if actual != expected:
                lines.append(actual + ("\n" if line.endswith("\n") else ""))
    return _join(lines)


def patch_hosts(text, domains, session):
    if not re.fullmatch(r"[0-9a-f]{32}", session):
        raise ValueError("Invalid hosts session ID")
    domains = set(domains)
    prefix = f"# attraccess-balena-tunnel[{session}]:"
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = []
    for line in text.splitlines(keepends=True):
        body, separator, comment = line.rstrip("\r\n").partition("#")
        fields = body.split()
        conflicting = len(fields) > 1 and any(name.lower().rstrip(".") in domains for name in fields[1:])
        if conflicting or not line.endswith(("\n", "\r")):
            encoded = base64.b64encode(line.encode("utf-8", "surrogateescape")).decode()
            lines.append(prefix + "saved " + encoded + newline)
            if conflicting:
                remaining = [name for name in fields[1:] if name.lower().rstrip(".") not in domains]
                alias = fields[0] + " " + " ".join(remaining) if remaining else ""
                if alias and separator:
                    alias += " #" + comment
            else:
                alias = line.rstrip("\r\n")
            if alias:
                encoded_alias = base64.b64encode(alias.encode("utf-8", "surrogateescape")).decode()
                lines.append(alias + " " + prefix + "alias " + encoded_alias + newline)
        else:
            lines.append(line)
    lines.append(prefix + "begin" + newline)
    for domain in sorted(domains):
        lines.append(f"127.0.0.1 {domain} {prefix}entry {domain}" + newline)
    lines.append(prefix + "end" + newline)
    return "".join(lines)


def update_hosts(transform, path=HOSTS):
    # Keep the hosts inode, permissions and ownership (also works with bind mounts).
    with path.open("r+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        before = handle.read()
        after = transform(before.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
        if before != after:
            handle.seek(0)
            handle.write(after)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())


def validate_plan(plan):
    if set(plan) != {"domains", "mappings", "tunnel_pid"}:
        raise ValueError("Invalid helper plan")
    if not isinstance(plan["domains"], list) or not isinstance(plan["mappings"], list):
        raise ValueError("Invalid helper plan")
    if plan["tunnel_pid"] is not None and (type(plan["tunnel_pid"]) is not int or plan["tunnel_pid"] <= 1):
        raise ValueError("Invalid tunnel process ID")
    for domain in plan["domains"]:
        if not isinstance(domain, str) or len(domain) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        ):
            raise ValueError("Invalid hostname in helper plan")
    ports = set()
    for pair in plan["mappings"]:
        if not isinstance(pair, list) or len(pair) != 2 or any(type(p) is not int for p in pair):
            raise ValueError("Invalid port mapping in helper plan")
        local, upstream = pair
        if not 1 <= local <= 65535 or not 1024 <= upstream <= 65535 or local in ports:
            raise ValueError("Invalid or duplicate port mapping in helper plan")
        ports.add(local)
    if ports.intersection(pair[1] for pair in plan["mappings"]):
        raise ValueError("Relay and upstream ports overlap")


class Relays:
    def __init__(self, mappings):
        self.listeners = {}
        self.connections = set()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        try:
            for local, upstream in mappings:
                for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                    try:
                        listener = socket.socket(family)
                    except OSError as exc:
                        if family == socket.AF_INET6 and exc.errno == errno.EAFNOSUPPORT:
                            continue
                        raise
                    try:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        if family == socket.AF_INET6:
                            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                        listener.bind((address, local))
                        listener.listen(128)
                    except OSError as exc:
                        listener.close()
                        if family == socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                            continue
                        raise RuntimeError(f"Cannot bind {address}:{local}: {exc.strerror}. Stop the local service using that port.") from exc
                    self.listeners[listener] = upstream
        except BaseException:
            self.close()
            raise

    def serve(self):
        while not self.stopping.is_set():
            ready, _, _ = select.select(list(self.listeners), [], [], 0.2)
            for listener in ready:
                client, _ = listener.accept()
                with self.lock:
                    self.connections.add(client)
                threading.Thread(target=self.forward, args=(client, self.listeners[listener]), daemon=True).start()

    def forward(self, client, port):
        upstream = None
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
            upstream.settimeout(None)
            with self.lock:
                if self.stopping.is_set():
                    return
                self.connections.add(upstream)
            peers = {client: upstream, upstream: client}
            # A read EOF closes only the other side's write direction. Responses
            # still reach clients that half-close their request stream.
            while peers and not self.stopping.is_set():
                ready, _, _ = select.select(list(peers), [], [], 0.5)
                for source in ready:
                    data = source.recv(65536)
                    if data:
                        peers[source].sendall(data)
                    else:
                        peers[source].shutdown(socket.SHUT_WR)
                        del peers[source]
        except OSError:
            pass
        finally:
            with self.lock:
                for sock in (client, upstream):
                    if sock is not None:
                        self.connections.discard(sock)
                        sock.close()

    def close(self):
        self.stopping.set()
        for listener in self.listeners:
            listener.close()
        with self.lock:
            for sock in self.connections:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            self.connections.clear()


def helper(plan, hosts_path=HOSTS, lock_path=LOCK, control=None):
    validate_plan(plan)
    session = uuid.uuid4().hex
    control = sys.stdin if control is None else control
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Attraccess tunnel is active. Stop it before starting a new session.") from exc
        update_hosts(restore_hosts, hosts_path)
        relays = Relays(plan["mappings"])
        installed = False
        try:
            if plan["domains"]:
                # Set before writing so a failed/partial install also gets rolled back.
                installed = True
                update_hosts(lambda text: patch_hosts(text, plan["domains"], session), hosts_path)
            print(json.dumps({"ready": True}), flush=True)
            heartbeat = [time.monotonic()]

            def watch_parent():
                for line in control:
                    if line.strip() != "ping":
                        break
                    heartbeat[0] = time.monotonic()
                relays.stopping.set()

            def watch_heartbeat():
                while not relays.stopping.wait(1):
                    if time.monotonic() - heartbeat[0] > 10:
                        relays.stopping.set()

            threading.Thread(target=watch_parent, daemon=True).start()
            threading.Thread(target=watch_heartbeat, daemon=True).start()
            relays.serve()
        finally:
            relays.close()
            try:
                if installed:
                    update_hosts(lambda text: restore_hosts(text, session), hosts_path)
            finally:
                # The helper outlives a killed parent and also stops its detached
                # balena process group, preventing orphaned high-port listeners.
                if plan["tunnel_pid"] is not None:
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.killpg(plan["tunnel_pid"], sig)
                        except ProcessLookupError:
                            break
                        except PermissionError:
                            # macOS returns EPERM when SIGTERM has left a group
                            # containing only an unreaped zombie. A permission
                            # failure on the initial signal is still an error.
                            if sig == signal.SIGKILL:
                                break
                            raise
                        if sig == signal.SIGTERM:
                            time.sleep(0.1)


def main():
    def interrupted(_signum, _frame):
        # The sudo-launched helper shares the user's terminal. A hangup or Ctrl+C
        # must restore hosts, and repeated signals must not interrupt restoration.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt

    try:
        if os.geteuid() != 0:
            raise RuntimeError("This helper must be run by the CLI through sudo.")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, interrupted)
        plan_line = sys.stdin.readline(1048576)
        helper(json.loads(plan_line))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
