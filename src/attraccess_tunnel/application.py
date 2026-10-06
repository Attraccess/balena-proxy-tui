"""One fullscreen window for selection, setup, authentication and local access."""

from contextlib import ExitStack
from functools import lru_cache
import os
import re
import select
import signal
import sys
import termios
import time
import tty

import pyte
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .fleets import read_key
from .processes import BalenaTerminal, free_ports
from .tui import Dashboard, clean, text


def cancel():
    # Keyboard cancellation needs the same protection as a terminal signal:
    # subsequent Ctrl+C must not interrupt hosts-file restoration.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    raise KeyboardInterrupt


def color(value):
    if value == "default":
        return None
    if re.fullmatch(r"[0-9a-f]{6}", value):
        return "#" + value
    return value.replace("brown", "yellow").replace("bright", "bright_")


@lru_cache(maxsize=128)
def terminal_style(fg, bg, bold, italics, underscore, reverse):
    return Style(color=color(fg), bgcolor=color(bg), bold=bold, italic=italics,
                 underline=underscore, reverse=reverse)


class TerminalPane:
    """Interpret the native picker's cursor updates inside our window only."""
    def __init__(self, columns, rows):
        self.reply = lambda data: None
        self.screen = pyte.Screen(columns, rows)
        self.screen.write_process_input = lambda data: self.reply(data.encode())
        self.stream = pyte.ByteStream(self.screen)

    def resize(self, columns, rows):
        self.screen.resize(lines=rows, columns=columns)

    def feed(self, data):
        self.stream.feed(data)

    def render(self):
        lines = []
        for row in range(self.screen.lines):
            line = Text(no_wrap=True, overflow="crop")
            for column in range(self.screen.columns):
                char = self.screen.buffer[row][column]
                line.append(clean(char.data), terminal_style(char.fg, char.bg, char.bold,
                                                            char.italics, char.underscore, char.reverse))
            line.rstrip()
            lines.append(line)
        return Group(*lines)


class Application:
    STEPS = (("fleet", "Fleet"), ("device", "Device"), ("discover", "Discover"),
             ("tunnel", "Tunnel"), ("access", "Local access"))

    def __init__(self, console=None, recovery=False):
        self.console = console or Console(highlight=False)
        self.recovery = recovery
        self.fleet = ""
        self.uuid = ""
        self.stage = None
        self.status = "STARTING"
        self.title = "Preparing local access"
        self.description = "Checking your balena CLI and terminal…"
        self.view = "loading"
        self.spinner = Spinner("dots", style="bright_cyan")
        self.dashboard = None
        self.native = None
        self.fleets = []
        self.query = ""
        self.searching = False
        self.selected = 0
        self.password_length = 0
        self._last_render = 0
        self._output_lost = False

    @property
    def body_height(self):
        return max(3, self.console.size.height - (9 if self.console.size.height >= 22 else 7))

    @property
    def terminal_size(self):
        return max(20, self.console.size.width - 4), max(3, self.body_height - 2)

    @property
    def filtered_fleets(self):
        return [f for f in self.fleets if self.query.lower() in
                (str(f.get("app_name", "")) + " " + f["slug"]).lower()]

    def loading(self, title, description="", stage=None):
        self.title, self.description, self.stage = title, description, stage
        self.view = "loading"
        self.status = "CLEANING UP" if stage == "cleanup" else "SETTING UP"
        self.refresh(force=True)

    def render(self):
        compact = self.console.size.height < 22
        layout = Layout()
        layout.split_column(Layout(name="header", size=4 if compact else 5),
                            Layout(name="steps", size=1 if compact else 2),
                            Layout(name="body"), Layout(name="footer", size=2))
        heading = Table.grid(expand=True)
        heading.add_column(ratio=1)
        heading.add_column(justify="right")
        heading.add_row(Text("BALENA PROXY TUI", style="bold bright_cyan"),
                        text("● " + self.status, "red" if self.view == "error" else "green" if self.view in ("dashboard", "done") else "cyan"))
        context = text("  ·  ".join(filter(None, (self.fleet, self.uuid))) or "Temporary access to your balena fleet", "dim")
        header = Group(heading, context) if compact else Group(heading, "", context)
        layout["header"].update(Panel(header, box=box.ROUNDED, border_style="cyan"))
        steps = Text()
        current = next((i for i, s in enumerate(self.STEPS) if s[0] == self.stage), -1)
        if self.recovery:
            steps.append("  Restore local access", "bold cyan")
        else:
            for index, (_, name) in enumerate(self.STEPS):
                complete = index < current or self.stage in ("running", "cleanup")
                steps.append(f"  {'✓' if complete else index + 1} {name}  ",
                             "bold cyan" if index == current else "green" if complete else "dim")
        layout["steps"].update(steps)
        if self.view == "fleet":
            body = self.fleet_menu()
        elif self.view == "native":
            self.native.resize(*self.terminal_size)
            body = Panel(self.native.render(), title="Select a device", border_style="cyan",
                         height=self.body_height, box=box.ROUNDED)
        elif self.view == "dashboard":
            self.dashboard.content_height = self.body_height
            body = self.dashboard.render(show_header=False, show_footer=False)
        else:
            body = self.dialog()
        layout["body"].update(body)
        layout["footer"].update(self.footer())
        return layout

    def dialog(self):
        title = Text(clean(self.title), style="bold red" if self.view == "error" else "bold bright_cyan")
        if self.view == "loading":
            title = Table.grid(padding=(0, 1))
            title.add_row(self.spinner, text(self.title, "bold bright_cyan"))
        description = Text(clean(self.description), style="dim", overflow="fold")
        contents = [title, "", description]
        if self.view == "password":
            contents.extend(["", text("Password  " + "●" * self.password_length + "▏", "bold cyan")])
        if self.view == "error":
            contents.extend(["", Text("Enter to close", style="bold")])
        panel = Panel(Group(*contents), box=box.ROUNDED,
                      border_style="red" if self.view == "error" else "cyan",
                      width=min(76, max(20, self.console.size.width - 4)),
                      padding=(0, 1) if self.console.size.height < 22 else (1, 2))
        return Align.center(panel, vertical="middle")

    def footer(self):
        if self.view == "fleet":
            controls = "↑/↓ j/k choose   Enter continue   / search   Esc / Ctrl+C cancel"
            message = "Choose a fleet to browse its online devices."
        elif self.view == "native":
            controls = "↑/↓ choose   Enter continue   Esc / Ctrl+C cancel"
            message = "Your balena CLI's device picker; keyboard input goes directly to balena."
        elif self.view == "password":
            controls = "Enter authorize   Esc/Ctrl+C cancel   Backspace edit   Ctrl+U clear"
            message = "Password input is hidden and is sent only to sudo."
        elif self.view == "dashboard":
            controls = "↑/↓ j/k select   Tab view   Enter/o open   q / Ctrl+C stop & clean up"
            message = self.dashboard.message
        elif self.view == "error":
            controls, message = "Enter / q / Esc close", "Close this window to return to your terminal."
        elif self.stage == "cleanup" or self.view == "done":
            controls, message = "", "Restoring your local machine before closing…" if self.view != "done" else "Session closed."
        else:
            controls, message = "q / Esc / Ctrl+C cancel", ""
        return Group(text(message, "dim"), text(controls, "dim"))

    def fleet_menu(self):
        fleets = self.filtered_fleets
        visible = max(1, self.body_height - 8)
        start = max(0, min(self.selected - visible // 2, len(fleets) - visible))
        table = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False, header_style="bold dim")
        table.add_column("", width=1)
        table.add_column("Fleet", ratio=2, no_wrap=True)
        table.add_column("Organization / slug", ratio=2, no_wrap=True)
        if self.console.size.width >= 90:
            table.add_column("Device type", ratio=1, no_wrap=True)
        for index in range(start, start + min(visible, len(fleets))):
            fleet = fleets[index]
            cells = [text("›" if index == self.selected else "", "bold cyan"),
                     text(fleet.get("app_name") or fleet["slug"]), text(fleet["slug"], "dim")]
            if self.console.size.width >= 90:
                cells.append(text(fleet.get("device_type", "—"), "dim"))
            table.add_row(*cells, style="on grey15" if index == self.selected else None)
        search = Text("Search: " + clean(self.query) + ("▏" if self.searching else ""),
                      style="cyan" if self.searching else "dim")
        body = Group(search, "", table if fleets else text("No fleets match this search.", "yellow"),
                     text(f"{len(fleets)} fleets  ·  {self.selected + 1 if fleets else 0}/{len(fleets)}", "dim"))
        return Panel(body, title="Select a fleet", box=box.ROUNDED, border_style="cyan", height=self.body_height)

    def refresh(self, force=False):
        if self._output_lost:
            return
        now = time.monotonic()
        if force or now - self._last_render >= 0.08:
            try:
                self.live.update(self.render(), refresh=True)
            except OSError:
                # A closed terminal must not interrupt resource restoration.
                self._output_lost = True
            self._last_render = now

    def read(self, timeout=0):
        fd = sys.stdin.fileno()
        return read_key(fd) if select.select([fd], [], [], timeout)[0] else None

    def tick(self):
        if self.read() in (b"", b"q", b"Q", b"\x1b", b"\x03", b"\x04"):
            cancel()
        self.refresh()

    def animate(self):
        # Teardown deliberately doesn't accept more cancellation requests.
        self.refresh()

    def choose_fleet(self, fleets):
        self.fleets, self.selected, self.query = fleets, 0, ""
        self.view, self.stage, self.status = "fleet", "fleet", "CHOOSE FLEET"
        while True:
            self.refresh()
            key = self.read(0.05)
            if key is None:
                continue
            choices = self.filtered_fleets
            if key in (b"\r", b"\n") and choices:
                self.fleet = choices[self.selected]["slug"]
                return self.fleet
            if self.searching:
                if key == b"\x1b":
                    self.searching, self.query, self.selected = False, "", 0
                elif key in (b"\x7f", b"\x08"):
                    self.query, self.selected = self.query[:-1], 0
                elif len(key) == 1 and 32 <= key[0] < 127:
                    self.query += key.decode()
                    self.selected = 0
                elif key in (b"\x03", b"\x04", b""):
                    cancel()
            elif key in (b"q", b"Q", b"\x1b", b"\x03", b"\x04", b""):
                cancel()
            elif key == b"/":
                self.searching = True
            if key in (b"\x1b[A", b"\x1bOA") or (key == b"k" and not self.searching):
                self.selected = (self.selected - 1) % max(1, len(choices))
            elif key in (b"\x1b[B", b"\x1bOB") or (key == b"j" and not self.searching):
                self.selected = (self.selected + 1) % max(1, len(choices))
            self.refresh(force=True)

    def choose_device(self, balena, target):
        self.loading("Loading devices", "Opening balena's online-device picker…", "device")
        self.native = TerminalPane(*self.terminal_size)
        self.view, self.status = "native", "CHOOSE DEVICE"
        port = free_ports(1)[0]
        with BalenaTerminal([balena, "device", "tunnel", target, "-p", f"22222:127.0.0.1:{port}"],
                            display=False, on_output=self.native.feed, size=lambda: self.terminal_size,
                            cleanup_tick=self.animate) as child:
            self.native.reply = lambda data: os.write(child.master, data)
            while True:
                child.pump(timeout=0.04)
                match = re.search(r"Opening a tunnel to ([0-9a-f]{32})\.\.\.", child.text, re.I)
                if match:
                    self.uuid = match[1].lower()
                    self.loading("Device selected", "Preparing to discover its local services…", "discover")
                    return self.uuid
                if child.process.poll() is not None or "Waiting for connections..." in child.text:
                    raise RuntimeError("balena did not select a device. Check login, fleet name and device connectivity.")
                key = self.read()
                if key in (b"", b"\x1b", b"\x03", b"\x04"):
                    cancel()
                if key is not None:
                    os.write(child.master, key)
                self.refresh()

    def password(self, prompt, notice=""):
        self.view, self.status, self.title = "password", "AUTHORIZATION", "Authorize local access"
        self.description = ((clean(notice).strip() + "\n\n") if notice else "") + (clean(prompt).strip() or "Enter your sudo password.")
        secret = bytearray()
        try:
            while True:
                self.password_length = len(secret.decode("utf-8", "ignore"))
                self.refresh()
                key = self.read(0.05)
                if key in (b"\r", b"\n"):
                    return bytes(secret)
                if key in (b"", b"\x1b", b"\x03", b"\x04"):
                    cancel()
                if key in (b"\x08", b"\x7f"):
                    secret[:] = secret.decode("utf-8", "ignore")[:-1].encode()
                elif key == b"\x15":
                    secret.clear()
                elif key and len(key) == 1 and key[0] >= 32:
                    secret.extend(key)
        finally:
            secret[:] = b"\0" * len(secret)
            self.password_length = 0

    def show_discovery(self, found, preview=False):
        self.dashboard = Dashboard(found, self.uuid, self.fleet or "Device", self.console)
        self.dashboard.preview = preview
        self.view, self.stage = "dashboard", "discover" if preview else "running"
        self.status = "PREVIEW · NO LOCAL ACCESS" if preview else "CONNECTED"
        if preview:
            self.dashboard.message = "Discovery preview. Hosts entries and local forwarding are inactive."
        self.refresh(force=True)

    def dashboard_tick(self):
        self.dashboard.collect_browser_result()
        key = self.read()
        if key is not None:
            try:
                self.dashboard.handle_key(key)
            except KeyboardInterrupt:
                cancel()
        self.refresh()

    def error(self, message):
        self.view, self.status = "error", "ERROR"
        self.title, self.description = "Unable to complete local access", str(message)
        self.refresh(force=True)
        while True:
            key = self.read(0.08)
            if key in (b"", b"q", b"Q", b"\x1b", b"\x03", b"\x04", b"\r", b"\n"):
                return
            self.refresh()

    def finish(self):
        self.view, self.status = "done", "CLOSED"
        self.title = "Session closed"
        self.description = "Local hosts entries and forwarding cleaned up."
        self.refresh(force=True)

    def __enter__(self):
        self._stack = ExitStack()
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        self._stack.callback(termios.tcsetattr, fd, termios.TCSADRAIN, saved)
        try:
            tty.setcbreak(fd)
            self.live = self._stack.enter_context(Live(self.render(), console=self.console, screen=True,
                                                      auto_refresh=False, redirect_stdout=False,
                                                      redirect_stderr=False))
        except BaseException:
            self._stack.close()
            raise
        return self

    def __exit__(self, *args):
        try:
            self._stack.__exit__(*args)
        except (OSError, termios.error):
            pass
        finally:
            try:
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            except (OSError, termios.error):
                pass
