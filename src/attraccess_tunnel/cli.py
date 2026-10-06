import argparse
from contextlib import contextmanager, nullcontext
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import time

from .discovery import discover, hostname
from .fleets import list_fleets, select_fleet
from .processes import BalenaTerminal, free_ports, select_device, wait_for_tunnel
from .tui import interactive_terminal, print_discovery
from .application import Application
from .authentication import authenticate
from . import privileged


@contextmanager
def local_access(domains, mappings, timeout, tunnel_pid=None, ui=None):
    command = [sys.executable, "-I", str(Path(privileged.__file__).resolve())]
    use_sudo = os.geteuid() != 0
    if use_sudo:
        if ui:
            authenticate(ui)
        else:
            print("sudo is needed for temporary hosts entries and local listeners.", flush=True)
            subprocess.run(["sudo", "-v"], check=True)
        command = ["sudo", "-n", *command]
    if ui:
        ui.loading("Opening local access", "Configuring temporary hosts entries and local port listeners…", "access")
    # sudo's cached authorization is tied to the terminal/session used by -v.
    # Keep that session until sudo has authenticated and launched the helper.
    # A separate helper process still survives parent death via its EOF watcher.
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE if ui else None,
                               text=True, start_new_session=not use_sudo)
    ready = False
    try:
        process.stdin.write(json.dumps({"domains": domains, "mappings": mappings, "tunnel_pid": tunnel_pid}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        while not select.select([process.stdout], [], [], min(0.08, max(0, deadline - time.monotonic())))[0]:
            if ui:
                ui.tick()
            if time.monotonic() >= deadline:
                raise RuntimeError("Local listener setup timed out.")
        reply = process.stdout.readline()
        if not reply:
            raise RuntimeError("Local helper exited before setup completed.")
        status = json.loads(reply)
        if not status.get("ready"):
            raise RuntimeError(status.get("error", "Local listener setup failed."))
        ready = True
        yield process
    finally:
        if ui:
            ui.loading("Restoring local access", "Closing listeners and restoring your original hosts entries…", "cleanup")
        # EOF asks the independently supervised helper to close sockets and restore
        # hosts. It also cleans up after SIGKILL/crashes of this parent process.
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            deadline = time.monotonic() + 15
            while process.poll() is None:
                if ui:
                    ui.animate()
                if time.monotonic() >= deadline:
                    raise RuntimeError("Local helper is still cleaning up. Run 'balena-proxy-tui --cleanup' if hosts entries remain.")
                try:
                    process.wait(timeout=0.08)
                except subprocess.TimeoutExpired:
                    pass
            tail = process.stdout.read().strip()
            errors = process.stderr.read().strip() if process.stderr else ""
            if ready and (process.returncode or tail):
                raise RuntimeError(f"Local cleanup failed: {tail or errors or process.returncode}. Run balena-proxy-tui --cleanup.")
        finally:
            process.stdout.close()
            if process.stderr:
                process.stderr.close()


def parser():
    command = argparse.ArgumentParser(description="Access an Attraccess balena device locally until Ctrl+C.")
    command.add_argument("target", nargs="?", help="fleet name/slug or device UUID; omit to choose a fleet interactively")
    command.add_argument("--balena", default="balena", help="balena CLI executable (default: balena)")
    command.add_argument("--proxy-service", default="proxy", help="Nginx Proxy Manager balena service name")
    command.add_argument("--hostname", action="append", default=[], type=hostname, help="extra concrete hostname, e.g. for a wildcard proxy host; repeatable")
    command.add_argument("--dry-run", action="store_true", help="select and discover without local forwarding or hosts changes")
    command.add_argument("--no-tui", action="store_true", help="use traditional terminal prompts and print URLs/mappings")
    command.add_argument("--cleanup", action="store_true", help="recover stale hosts entries after an interrupted helper; no device needed")
    command.add_argument("--timeout", type=float, default=60, help="fleet loading, discovery and setup timeout in seconds (default: 60)")
    return command


def run(args, ui=None):
    if not sys.platform.startswith(("linux", "darwin")):
        raise RuntimeError("This CLI currently supports macOS and Linux.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise RuntimeError("--timeout must be a finite number greater than zero.")
    if args.cleanup:
        if ui:
            ui.loading("Recovering local access", "Restoring hosts entries from interrupted sessions…", "cleanup")
        with local_access([], [], args.timeout, ui=ui):
            pass
        if ui:
            ui.finish()
        else:
            print("Stale Attraccess hosts entries recovered.")
        return
    balena = shutil.which(args.balena)
    if not balena:
        raise RuntimeError("balena CLI is not installed. Install it and run 'balena login' first.")
    if args.target:
        target = args.target
        if ui and not re.fullmatch(r"[0-9a-fA-F]{7,32}", target):
            ui.fleet = target
    elif ui:
        ui.loading("Loading balena fleets", "Finding the fleets available through your balena account…", "fleet")
        target = ui.choose_fleet(list_fleets(balena, args.timeout, tick=ui.tick))
    else:
        target = select_fleet(balena, args.timeout)
    if not sys.stdin.isatty() and not re.fullmatch(r"[0-9a-fA-F]{7,32}", target):
        raise RuntimeError("Fleet selection requires an interactive terminal. For automation, supply a device UUID.")
    if ui:
        uuid = ui.choose_device(balena, target)
        ui.loading("Discovering device services", "Reading configured domains, container destinations and TCP ports…", "discover")
    else:
        print("Selecting a device with balena…", flush=True)
        uuid = select_device(balena, target)
        print(f"\nReading live proxy domains and ports from {uuid}…", flush=True)
    found = discover(balena, uuid, args.proxy_service, args.hostname, args.timeout, tick=ui.tick if ui else None)
    if not ui:
        print_discovery(found)
    if args.dry_run:
        if ui:
            ui.show_discovery(found, preview=True)
            while True:
                ui.dashboard_tick()
                time.sleep(0.05)
        return
    # Never choose an upstream that is also a public relay port.
    upstreams = free_ports(len(found.ports))
    while set(upstreams).intersection(found.ports):
        upstreams = free_ports(len(found.ports))
    mappings = list(zip(found.ports, upstreams))
    command = [balena, "device", "tunnel", uuid]
    for remote, upstream in mappings:
        command.extend(["-p", f"{remote}:127.0.0.1:{upstream}"])
    if ui:
        ui.loading("Opening device tunnel", f"Forwarding {len(found.ports)} TCP ports from the selected device…", "tunnel")
    with BalenaTerminal(command, display=not ui, cleanup_tick=ui.animate if ui else None) as tunnel:
        try:
            wait_for_tunnel(tunnel, mappings, args.timeout, tick=ui.tick if ui else None)
            with local_access(found.domains, mappings, args.timeout, tunnel.process.pid, ui) as helper:
                if ui:
                    ui.show_discovery(found)
                else:
                    print("\nReady. Proxy domains and localhost:<device-port> now reach the device. Ctrl+C to stop.", flush=True)
                last_heartbeat = 0
                while True:
                    tunnel.pump(timeout=0.05 if ui else 0.2)
                    if tunnel.process.poll() is not None:
                        raise RuntimeError("balena tunnel stopped; local access has been cleaned up.")
                    if helper.poll() is not None:
                        raise RuntimeError("Local listener helper stopped; closing the balena tunnel.")
                    if time.monotonic() - last_heartbeat >= 1:
                        helper.stdin.write("ping\n")
                        helper.stdin.flush()
                        last_heartbeat = time.monotonic()
                    if ui:
                        ui.dashboard_tick()
        finally:
            if ui:
                ui.loading("Closing device tunnel", "Releasing the balena tunnel and its local connections…", "cleanup")


def main():
    args = parser().parse_args()

    def interrupted(signum, _frame):
        # Once cleanup starts, repeated Ctrl+C must not interrupt it.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    ui = Application(recovery=args.cleanup) if not args.no_tui and interactive_terminal() else None
    exit_code = 0
    with ui if ui else nullcontext():
        try:
            run(args, ui)
        except KeyboardInterrupt:
            if ui:
                ui.finish()
            else:
                print("\nStopped. Local hosts entries and forwarding cleaned up.")
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            exit_code = 1
            if ui:
                try:
                    ui.error(exc)
                except KeyboardInterrupt:
                    pass
            else:
                print(f"Error: {exc}", file=sys.stderr)
    if exit_code:
        sys.exit(exit_code)
