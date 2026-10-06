import io
import unittest

from rich.console import Console

from attraccess_tunnel.application import Application, TerminalPane
from test_tui import sample_discovery


class ApplicationTests(unittest.TestCase):
    def test_terminal_pane_interprets_cursor_updates_without_emitting_child_controls(self):
        pane = TerminalPane(40, 5)
        responses = []
        pane.reply = responses.append
        pane.feed(b'? Select a device\r\n  old device\r\x1b[2K\x1b[36m> Demo (0123456)\x1b[0m\x1b[6n')
        console = Console(file=io.StringIO(), force_terminal=False)
        console.print(pane.render())
        output = console.file.getvalue()
        self.assertIn("Select a device", output)
        self.assertIn("Demo (0123456)", output)
        self.assertNotIn("old device", output)
        self.assertNotIn("\x1b", output)
        self.assertEqual(len(responses), 1)
        self.assertTrue(responses[0].startswith(b"\x1b["))
        pane.resize(30, 4)
        self.assertEqual((pane.screen.columns, pane.screen.lines), (30, 4))

    def test_every_stage_keeps_the_same_header_and_footer_inside_the_terminal(self):
        for width, height in ((120, 32), (80, 24), (60, 16)):
            for view in ("loading", "fleet", "native", "password", "dashboard", "error"):
                with self.subTest(size=(width, height), view=view):
                    console = Console(file=io.StringIO(), width=width, height=height, record=True,
                                      force_terminal=True, color_system="truecolor")
                    app = Application(console)
                    app.fleet, app.uuid = "org/fleet", "f" * 32
                    app.fleets = [{"app_name": "First fleet", "slug": "org/first"},
                                  {"app_name": "Second fleet", "slug": "org/second"}]
                    app.native = TerminalPane(*app.terminal_size)
                    app.native.feed(b'? Select a device\r\n> Demo (0123456)')
                    # show_discovery normally refreshes a live screen. Here only
                    # the render model is needed to check every viewport size.
                    from attraccess_tunnel.tui import Dashboard
                    app.dashboard = Dashboard(sample_discovery(), app.uuid, app.fleet, console)
                    app.view = view
                    app.description = "A status message long enough to wrap in a narrow dialog without hiding the controls."
                    app.password_length = 5
                    console.print(app.render())
                    output = console.export_text()
                    self.assertEqual(len(output.splitlines()), height)
                    self.assertIn("BALENA PROXY TUI", output)
                    self.assertTrue(all(len(line) <= width for line in output.splitlines()))
                    self.assertTrue("Ctrl+C" in output or "Enter / q / Esc" in output)
                    if view == "password":
                        self.assertIn("●●●●●", output)


if __name__ == "__main__":
    unittest.main()
