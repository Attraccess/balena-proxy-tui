"""A terminal dashboard; discovery and process supervision stay outside the UI."""

from contextlib import ExitStack
import os
import queue
import select
import sys
import termios
import threading
import time
import tty
import webbrowser

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .fleets import read_key


def clean(value):
    # Container names, paths and upstreams are device data, never terminal code.
    return "".join(c for c in str(value) if c.isprintable())


def text(value, style=""):
    return Text(clean(value), style=style, overflow="ellipsis", no_wrap=True)


def link(url):
    return text(url, Style(color="bright_cyan", underline=True, link=clean(url)))


def route_target(route):
    if route.target:
        return route.target.label
    if route.kind == "redirection_host":
        return "Redirect → " + (route.redirect or "configured destination")
    if route.kind == "dead_host":
        return "404 response"
    return "Default proxy host" if route.kind == "extra" else "Upstream not configured"


def route_status(route, preview=False):
    if not route.enabled:
        return "Disabled", "yellow"
    if route.host_port is None:
        return "Unpublished", "yellow"
    return {"redirection_host": "Redirect", "dead_host": "404"}.get(route.kind, "Configured" if preview else "Ready"), "cyan" if preview else "green"


def routes_table(routes, selected=None, offset=0, wide=True, preview=False):
    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), header_style="bold dim",
                  show_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("URL", ratio=3, no_wrap=True)
    if wide:
        table.add_column("Via proxy", ratio=1, no_wrap=True)
    table.add_column("Container / destination", ratio=2, no_wrap=True)
    table.add_column("State", width=11, no_wrap=True)
    for index, route in enumerate(routes, offset):
        state, style = route_status(route, preview)
        cells = [text("›" if index == selected else "", "bold bright_cyan"),
                 link(route.url) if route.url else text(route.domain + route.path, "dim")]
        if wide:
            cells.append(text(f"{route.proxy_service}:{route.proxy_port}"))
        cells.extend([text(route_target(route)), text(state, style)])
        table.add_row(*cells, style="on grey15" if index == selected else None)
    return table


def ports_table(bindings, selected=None, offset=0, wide=True):
    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), header_style="bold dim",
                  show_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("Local TCP address", ratio=2, no_wrap=True)
    table.add_column("Container : port", ratio=2, no_wrap=True)
    table.add_column("Stream destination", ratio=2, no_wrap=True)
    if wide:
        table.add_column("Network", width=10, no_wrap=True)
    for index, binding in enumerate(bindings, offset):
        cells = [text("›" if index == selected else "", "bold bright_cyan"),
                 text(f"localhost:{binding.host_port}", "bright_cyan"),
                 text(f"{binding.service}:{binding.container_port}"),
                 text(binding.target.label if binding.target else "—", "dim" if not binding.target else "")]
        if wide:
            cells.append(text("host" if binding.network == "host" else "published", "dim"))
        table.add_row(*cells, style="on grey15" if index == selected else None)
    return table


def print_discovery(found, console=None):
    """Keep full URLs and mappings readable in dry runs, pipes and --no-tui."""
    console = console or Console(highlight=False)
    console.print(text("Domains: " + (", ".join(found.domains) or "none")))
    console.print(text("Device TCP ports: " + ", ".join(map(str, found.ports))))
    if found.routes:
        console.print("\n[bold cyan]PROXY URLS[/bold cyan]")
        # One route per line avoids truncating URLs when stdout is a pipe.
        for route in found.routes:
            row = Text()
            row.append_text(link(route.url) if route.url else text(route.domain + route.path))
            row.append(f" → {clean(route.proxy_service)}:{route.proxy_port} → {clean(route_target(route))}")
            if route.host_port is not None:
                row.append(f"  (local :{route.host_port})", "dim")
            state, style = route_status(route)
            if state != "Ready":
                row.append(f"  [{state}]", style)
            console.print(row, soft_wrap=True)
    if found.bindings:
        console.print("\n[bold cyan]TCP PORTS[/bold cyan]")
        for binding in found.bindings:
            row = Text(f"localhost:{binding.host_port} → {clean(binding.service)}:{binding.container_port}")
            if binding.target:
                row.append(" → " + clean(binding.target.label))
            row.append("  (host network)" if binding.network == "host" else "  (published)", "dim")
            console.print(row, soft_wrap=True)
    for warning in found.warnings:
        console.print(Text("Warning: " + clean(warning), style="yellow"), soft_wrap=True)


class Dashboard:
    def __init__(self, found, uuid, fleet, console=None):
        self.found, self.uuid, self.fleet = found, uuid, fleet
        self.console = console or Console(highlight=False)
        self.tab = 0 if found.routes else 1
        self.selected = [0, 0, 0]
        self.started = time.monotonic()
        self.message = "Select a URL and press Enter, or click a link."
        self._stack = None
        self._last_frame = None
        self._browser_results = queue.SimpleQueue()
        self._opening_browser = False
        self.content_height = None
        self.preview = False

    @property
    def items(self):
        return (self.found.routes, self.found.bindings, self.found.warnings)[self.tab]

    @property
    def page_size(self):
        if self.content_height is not None:
            return max(1, self.content_height - (10 if self.console.size.height >= 22 else 7))
        return max(1, self.console.size.height - (17 if self.console.size.height >= 22 else 11))

    def render(self, show_header=True, show_footer=True):
        width, height = self.console.size
        compact = height < 22
        elapsed = int(time.monotonic() - self.started)
        title = Text("BALENA PROXY TUI", style="bold bright_cyan")
        title.append(f"    ● CONNECTED   {elapsed // 60:02d}:{elapsed % 60:02d}", "green")
        device = Text(f"{clean(self.fleet)}  ·  {self.uuid}", overflow="ellipsis", no_wrap=True)
        overview = Text(f"{len(self.found.domains)} domains   {len(self.found.ports)} TCP ports   hosts entries active", style="dim")
        header = Group(title, device) if compact else Panel(Group(title, device, overview), border_style="cyan", box=box.ROUNDED)
        tabs = Text()
        for index, (name, count) in enumerate((("Domains", len(self.found.routes)), ("Ports", len(self.found.bindings)), ("Notices", len(self.found.warnings)))):
            tabs.append(f"  {name} ({count})  ", "bold black on cyan" if index == self.tab else "dim")
        count, selected = len(self.items), self.selected[self.tab]
        visible = self.page_size
        start = max(0, min(selected - visible // 2, count - visible))
        page = self.items[start:start + visible]
        if self.tab == 0:
            body = routes_table(page, selected, start, width >= 100, self.preview) if page else text("No proxy domains configured.", "dim")
        elif self.tab == 1:
            body = ports_table(page, selected, start, width >= 90) if page else text("No TCP bindings.", "dim")
        else:
            table = Table(box=None, expand=True, show_header=False, padding=(0, 1))
            table.add_column(width=1, no_wrap=True)
            table.add_column(no_wrap=True)
            for index, warning in enumerate(page, start):
                table.add_row(text("›" if index == selected else "", "bold yellow"), text(warning, "yellow"),
                              style="on grey15" if index == selected else None)
            body = table if page else text("No notices. All discovered TCP ports are forwarded.", "green")
        position = text(f"  {selected + 1 if count else 0}/{count}" + ("  ·  ↑/↓ to scroll" if count > visible else ""), "dim")
        detail = self.detail(compact)
        footer = Text("↑/↓ j/k select   Tab view   Enter/o open   q / Ctrl+C stop & clean up", style="dim", no_wrap=True, overflow="ellipsis")
        return Group(*([header] if show_header else []), tabs, "", body, position, detail,
                     *([text(self.message, "dim"), footer] if show_footer else []))

    def detail(self, compact=False):
        if not self.items:
            return text("")
        item = self.items[self.selected[self.tab]]
        lines = []
        if self.tab == 0:
            lines.append(link(item.url) if item.url else text(item.domain + item.path, "yellow"))
            lines.append(text(f"localhost:{item.host_port or 'unpublished'} → {item.proxy_service}:{item.proxy_port} → {route_target(item)}"))
            if not compact and item.target:
                lines.append(text(f"Configured upstream: {item.target.scheme}://{item.target.host}:{item.target.port}" +
                                  ("  ·  container " + item.target.service if item.target.service else "  ·  external / unresolved container"), "dim"))
        elif self.tab == 1:
            lines.append(text(f"localhost:{item.host_port} → device:{item.host_port} → {item.service}:{item.container_port}", "bright_cyan"))
            lines.append(text("Stream → " + item.target.label if item.target else "TCP binding; use the service's protocol with this local port."))
            if not compact:
                lines.append(text(f"Container {item.container_id[:12]}  ·  {'host network' if item.network == 'host' else 'published port'}", "dim"))
        else:
            return Panel(Text(clean(item), style="yellow", overflow="fold"), border_style="yellow", title="Notice")
        return Group(*lines) if compact else Panel(Group(*lines), box=box.ROUNDED, border_style="grey37", title="Selected mapping")

    def handle_key(self, key):
        if key in (b"", b"q", b"Q", b"\x03", b"\x04", b"\x1b"):
            raise KeyboardInterrupt
        if key in (b"\t", b"\x1b[C", b"\x1bOC", b"l"):
            self.tab = (self.tab + 1) % 3
        elif key in (b"\x1b[Z", b"\x1b[D", b"\x1bOD", b"h"):
            self.tab = (self.tab - 1) % 3
        elif key in (b"1", b"2", b"3"):
            self.tab = int(key) - 1
        elif key in (b"\x1b[A", b"\x1bOA", b"k", b"\x1b[B", b"\x1bOB", b"j", b"\x1b[5~", b"\x1b[6~"):
            delta = -1 if key in (b"\x1b[A", b"\x1bOA", b"k") else 1
            if key in (b"\x1b[5~", b"\x1b[6~"):
                delta = -self.page_size if key == b"\x1b[5~" else self.page_size
            self.selected[self.tab] = max(0, min(self.selected[self.tab] + delta, len(self.items) - 1))
        elif key in (b"\r", b"\n", b"o"):
            if self.tab != 0 or not self.items or not self.items[self.selected[0]].url:
                self.message = "Select a published domain URL in the Domains view to open it."
            else:
                url = self.items[self.selected[0]].url
                if not self._opening_browser:
                    self._opening_browser = True
                    self.message = "Opening " + url

                    def open_browser():
                        try:
                            message = "Opened " + url if webbrowser.open(url) else "Browser unavailable; click or copy the selected URL."
                        except (webbrowser.Error, OSError):
                            message = "Browser unavailable; click or copy the selected URL."
                        self._browser_results.put(message)

                    # A slow desktop/browser launch must not pause the helper's
                    # heartbeat or prevent q/Ctrl+C from starting cleanup.
                    threading.Thread(target=open_browser, daemon=True).start()

    def collect_browser_result(self):
        try:
            self.message = self._browser_results.get_nowait()
            self._opening_browser = False
        except queue.Empty:
            pass

    def refresh(self):
        self.collect_browser_result()
        fd = sys.stdin.fileno()
        if select.select([fd], [], [], 0)[0]:
            self.handle_key(read_key(fd))
        frame = (self.console.size, int(time.monotonic() - self.started), self.tab,
                 tuple(self.selected), self.message)
        if frame != self._last_frame:
            self.live.update(self.render(), refresh=True)
            self._last_frame = frame

    def __enter__(self):
        stack = self._stack = ExitStack()
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        stack.callback(termios.tcsetattr, fd, termios.TCSADRAIN, saved)
        try:
            tty.setcbreak(fd)
            self.live = stack.enter_context(Live(self.render(), console=self.console, screen=True,
                                                auto_refresh=False, redirect_stdout=False,
                                                redirect_stderr=False))
        except BaseException:
            stack.close()
            raise
        return self

    def __exit__(self, *args):
        return self._stack.__exit__(*args)


def interactive_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
