"""Let sudo own authentication while the app presents its askpass prompts."""

import json
import os
from pathlib import Path
import select
import shlex
import socket
import subprocess
import sys
import tempfile


ASKPASS = """
import json, os, socket, sys
with socket.socket(socket.AF_UNIX) as channel:
    channel.connect(os.environ['ATTRACCESS_SUDO_SOCKET'])
    channel.sendall(json.dumps(sys.argv[1] if len(sys.argv) > 1 else 'Password:').encode() + b'\\n')
    with channel.makefile('rb') as reply:
        password = reply.readline()
    if not password:
        sys.exit(1)
    sys.stdout.buffer.write(password)
"""


def authenticate(ui):
    ui.loading("Authorize local access", "sudo grants temporary access to your hosts file and local ports.", "access")
    # A private socket carries one prompt/reply at a time. Passwords are never
    # written to the helper files, command arguments, screen or application logs.
    # Keep Unix socket paths below macOS/Linux limits even with a long TMPDIR.
    with tempfile.TemporaryDirectory(prefix="attraccess-auth-", dir="/tmp") as directory:
        root = Path(directory)
        helper = root / "askpass.py"
        helper.write_text(ASKPASS)
        executable = root / "askpass"
        executable.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", str(helper)]) + ' "$@"\n')
        executable.chmod(0o700)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(root / "prompt.sock"))
            server.listen(1)
            process = subprocess.Popen(
                ["sudo", "-A", "-v"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "SUDO_ASKPASS": str(executable),
                     "ATTRACCESS_SUDO_SOCKET": str(root / "prompt.sock")},
                # Validation and the final sudo helper share the app's session.
                start_new_session=False,
            )
            messages = bytearray()
            streams = [process.stdout, process.stderr]
            try:
                while process.poll() is None:
                    ui.tick()
                    ready = select.select([*streams, server], [], [], 0.05)[0]
                    for source in ready:
                        if source is server:
                            with server.accept()[0] as channel:
                                prompt = bytearray()
                                channel.settimeout(1)
                                while b"\n" not in prompt and len(prompt) < 8192:
                                    chunk = channel.recv(1024)
                                    if not chunk:
                                        break
                                    prompt.extend(chunk)
                                title = json.loads(prompt.split(b"\n", 1)[0])
                                password = ui.password(title, messages.decode("utf-8", "replace"))
                                try:
                                    channel.sendall(password + b"\n")
                                finally:
                                    del password
                                ui.loading("Authorizing local access", "Validating your credentials with sudo…", "access")
                        else:
                            data = os.read(source.fileno(), 8192)
                            if data:
                                messages.extend(data)
                                messages = messages[-8192:]
                            else:
                                streams.remove(source)
                output, errors = process.communicate()
                if process.returncode:
                    detail = (messages + output + errors).decode("utf-8", "replace").strip()
                    raise RuntimeError("sudo authorization failed" + (": " + detail if detail else "."))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
                process.stderr.close()
