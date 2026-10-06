import io
import threading
import time
import unittest
from unittest.mock import patch

from rich.console import Console

from attraccess_tunnel.discovery import Destination, Discovery, PortBinding, Route
from attraccess_tunnel.tui import Dashboard, print_discovery


def sample_discovery():
    routes = [Route(f"app{index}.example", "https", 443, "proxy", 443, "proxy_host", True,
                    Destination("app-alias", 3000, "attraccess", "http")) for index in range(20)]
    bindings = [PortBinding(2883, 1883, "proxy", "a" * 64, "bridge",
                            Destination("rabbitmq", 1883, "rabbitmq")),
                PortBinding(5380, 5380, "config-ui", "b" * 64, "host")]
    warnings = ["Skipping UDP proxy stream 1883; balena tunnel supports TCP only."]
    return Discovery([r.domain for r in routes], [443, 2883, 5380], warnings, routes, bindings)


class DashboardTests(unittest.TestCase):
    def console(self, width=120, height=32):
        return Console(file=io.StringIO(), width=width, height=height, force_terminal=True,
                       color_system="truecolor", record=True)

    def test_render_contains_terminal_links_and_container_mapping(self):
        console = self.console()
        dashboard = Dashboard(sample_discovery(), "f" * 32, "org/fleet", console)
        console.print(dashboard.render())
        output = console.export_text()
        self.assertIn("https://app0.example", output)
        self.assertIn("proxy:443", output)
        self.assertIn("attraccess:3000", output)
        self.assertIn("http://app-alias:3000", output)
        self.assertIn("\x1b]8;", console.file.getvalue())
        self.assertIn("https://app0.example\x1b\\", console.file.getvalue())
        dashboard.handle_key(b"\t")
        console.print(dashboard.render())
        output = console.export_text()
        self.assertIn("localhost:2883", output)
        self.assertIn("proxy:1883", output)
        self.assertIn("rabbitmq:1883", output)

    def test_navigation_scrolls_keeps_each_view_position_and_opens_selected_url(self):
        dashboard = Dashboard(sample_discovery(), "f" * 32, "org/fleet", self.console())
        dashboard.handle_key(b"j")
        dashboard.handle_key(b"\x1b[6~")
        selected = dashboard.selected[0]
        self.assertGreater(selected, 1)
        dashboard.handle_key(b"\t")
        dashboard.handle_key(b"j")
        dashboard.handle_key(b"\x1b[Z")
        self.assertEqual(dashboard.selected[0], selected)
        launched = threading.Event()
        with patch("attraccess_tunnel.tui.webbrowser.open", side_effect=lambda url: launched.set() or True) as opened:
            dashboard.handle_key(b"\r")
            self.assertTrue(launched.wait(1))
        opened.assert_called_once_with(f"https://app{selected}.example")
        with self.assertRaises(KeyboardInterrupt):
            dashboard.handle_key(b"q")

    def test_slow_browser_launch_does_not_block_navigation_or_cleanup(self):
        dashboard = Dashboard(sample_discovery(), "f" * 32, "org/fleet", self.console())
        launched, finish = threading.Event(), threading.Event()

        def browser(url):
            launched.set()
            finish.wait(3)
            return True

        with patch("attraccess_tunnel.tui.webbrowser.open", side_effect=browser):
            try:
                start = time.monotonic()
                dashboard.handle_key(b"\r")
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertTrue(launched.wait(1))
                dashboard.handle_key(b"j")
                self.assertEqual(dashboard.selected[0], 1)
                with self.assertRaises(KeyboardInterrupt):
                    dashboard.handle_key(b"q")
            finally:
                finish.set()
                self.assertIn("Opened", dashboard._browser_results.get(timeout=1))

    def test_resizes_without_hiding_controls_or_selected_mapping(self):
        for width, height in ((120, 32), (80, 24), (60, 16)):
            for tab in range(3):
                with self.subTest(size=(width, height), tab=tab):
                    console = self.console(width, height)
                    dashboard = Dashboard(sample_discovery(), "f" * 32, "org/fleet", console)
                    dashboard.tab = tab
                    console.print(dashboard.render())
                    output = console.export_text()
                    self.assertLessEqual(len(output.splitlines()), height, output)
                    self.assertTrue(all(len(line) <= width for line in output.splitlines()))
                    self.assertIn("Ctrl+C", output)

    def test_pipe_output_preserves_full_urls_and_rejects_terminal_controls_in_device_data(self):
        found = sample_discovery()
        found.routes[0] = Route("a" * 63 + "." + "b" * 63 + ".example", "https", 443, "proxy", 443,
                                "proxy_host", True, Destination("app", 3000, "[bold]app\x1b]0;owned\x07"))
        console = Console(file=io.StringIO(), width=30, force_terminal=False)
        print_discovery(found, console)
        output = console.file.getvalue()
        self.assertIn(found.routes[0].url, output)
        self.assertIn("[bold]app", output)  # Literal data, never Rich markup.
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)


if __name__ == "__main__":
    unittest.main()
