"""Interactive fleet selection using the installed balena CLI's fleet list."""

import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import tty

from .processes import ANSI, communicate, stop_process


def list_fleets(balena, timeout, tick=None):
    process = subprocess.Popen(
        [balena, "fleet", "list", "--json"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        output, errors = communicate(process, timeout, tick=tick)
        if process.returncode:
            detail = errors.decode("utf-8", "replace").strip() or output.decode("utf-8", "replace").strip()
            raise RuntimeError("Could not list balena fleets: " + detail)
        try:
            fleets = json.loads(ANSI.sub("", output.decode("utf-8")))
            # Some oclif versions serialize the command's JSON string again.
            if isinstance(fleets, str):
                fleets = json.loads(fleets)
            if not isinstance(fleets, list) or not all(
                isinstance(fleet, dict) and isinstance(fleet.get("slug"), str) and
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", fleet["slug"])
                for fleet in fleets
            ):
                raise ValueError("invalid fleet list")
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("balena fleet list --json returned an invalid fleet list.") from exc
        if not fleets:
            raise RuntimeError("No accessible balena fleets found. Check your balena login.")
        return fleets
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Listing balena fleets timed out after {timeout:g}s.") from exc
    finally:
        stop_process(process)


def fleet_label(fleet):
    label = f"{fleet.get('app_name') or fleet['slug']} ({fleet['slug']})"
    if fleet.get("device_type"):
        label += f" [{fleet['device_type']}]"
    # Names come from the cloud; prevent them from writing terminal controls.
    return "".join(character if character.isprintable() else " " for character in label)


def read_key(fd):
    key = os.read(fd, 1)
    if key == b"\x1b":
        # Handle cursor/application keys; a bare Escape cancels the menu.
        while len(key) < 8 and select.select([fd], [], [], 0.1)[0]:
            next_byte = os.read(fd, 1)
            if not next_byte:
                break
            key += next_byte
            if len(key) >= 3 and (key[-1:].isalpha() or key.endswith(b"~")):
                break
    return key


def choose_fleet(fleets):
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    selected, rows = 0, 0

    def clear():
        if rows:
            sys.stdout.write(f"\x1b[{rows}F\x1b[J")

    def render():
        nonlocal rows
        clear()
        size = shutil.get_terminal_size()
        visible = min(len(fleets), 7, max(1, size.lines - 3))
        start = max(0, min(selected - visible // 2, len(fleets) - visible))
        lines = ["? Select a fleet (Up/Down or j/k, Enter to select)"]
        for index in range(start, start + visible):
            lines.append(("❯ " if index == selected else "  ") + fleet_label(fleets[index]))
        if len(fleets) > visible:
            lines.append(f"  {selected + 1} of {len(fleets)}")
        sys.stdout.write("\n".join(line[:max(1, size.columns - 1)] for line in lines) + "\n")
        sys.stdout.flush()
        rows = len(lines)

    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?25l")
        render()
        while True:
            key = read_key(fd)
            if key in (b"\r", b"\n"):
                clear()
                print("? Select a fleet " + fleet_label(fleets[selected]), flush=True)
                return fleets[selected]["slug"]
            if key in (b"", b"\x03", b"\x04", b"\x1b"):
                raise KeyboardInterrupt
            if key in (b"\x1b[A", b"\x1bOA", b"k"):
                selected = (selected - 1) % len(fleets)
            elif key in (b"\x1b[B", b"\x1bOB", b"j"):
                selected = (selected + 1) % len(fleets)
            else:
                continue
            render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


def select_fleet(balena, timeout):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Interactive fleet selection requires a terminal. Supply a fleet name/slug or device UUID.")
    print("Loading balena fleets…", flush=True)
    return choose_fleet(list_fleets(balena, timeout))
