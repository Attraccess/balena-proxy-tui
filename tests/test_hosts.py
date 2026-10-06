from pathlib import Path
import tempfile
import unittest

from attraccess_tunnel.privileged import patch_hosts, restore_hosts, update_hosts, validate_plan


SESSION = "a" * 32


class HostsTests(unittest.TestCase):
    def test_restores_exact_original_and_keeps_other_aliases_active(self):
        for original in (
            "127.0.0.1 localhost\n192.168.1.10 app.example other # existing\n::1 app.example other6\n",
            "127.0.0.1 localhost\r\n192.168.1.10 APP.EXAMPLE. other # existing\r\n",
            "127.0.0.1 localhost\n# comment without final newline",
            "192.168.1.10 app.example other",
            "",
        ):
            with self.subTest(original=original):
                patched = patch_hosts(original, ["app.example", "grafana.example"], SESSION)
                self.assertIn("127.0.0.1 app.example", patched)
                if " other " in original:
                    self.assertIn("192.168.1.10 other # existing", patched)
                self.assertEqual(restore_hosts(patched, SESSION), original)

    def test_preserves_unrelated_changes_during_session(self):
        original = "127.0.0.1 localhost\n10.0.0.1 app.example keep\n"
        patched = patch_hosts(original, ["app.example"], SESSION)
        changed = patched.replace("127.0.0.1 localhost", "127.0.0.1 localhost newalias")
        changed += "10.0.0.2 another.example\n"
        restored = restore_hosts(changed, SESSION)
        self.assertEqual(restored, original.replace("localhost", "localhost newalias") + "10.0.0.2 another.example\n")

    def test_preserves_edits_to_generated_alias_line(self):
        patched = patch_hosts("10.0.0.1 app.example keep\n", ["app.example"], SESSION)
        changed = patched.replace("10.0.0.1 keep #", "10.0.0.1 keep added #")
        self.assertIn("10.0.0.1 keep added\n", restore_hosts(changed, SESSION))

    def test_stale_records_recover_without_a_separate_journal(self):
        original = "127.0.0.1 localhost\n10.0.0.1 app.example\n"
        patched = patch_hosts(original, ["app.example"], SESSION)
        self.assertEqual(restore_hosts(patched), original)
        self.assertEqual(restore_hosts(patched, "b" * 32), patched)

    def test_rejects_corrupt_recovery_data_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts"
            content = f"# attraccess-balena-tunnel[{SESSION}]:saved !invalid\n"
            path.write_text(content)
            with self.assertRaises(ValueError):
                update_hosts(restore_hosts, path)
            self.assertEqual(path.read_text(), content)

    def test_writes_keep_file_mode_and_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts"
            path.write_text("127.0.0.1 localhost\n")
            path.chmod(0o640)
            before = path.stat()
            update_hosts(lambda text: patch_hosts(text, ["app.example"], SESSION), path)
            after = path.stat()
            self.assertEqual(before.st_ino, after.st_ino)
            self.assertEqual(before.st_mode, after.st_mode)

    def test_helper_rejects_injection_and_port_overlap(self):
        for plan in (
            {"domains": ["evil\n127.0.0.1 other"], "mappings": [], "tunnel_pid": None},
            {"domains": [], "mappings": [[80, 80]], "tunnel_pid": None},
            {"domains": [], "mappings": [[80, 2000], [2000, 3000]], "tunnel_pid": None},
            {"domains": [], "mappings": [[80, 2000], [80, 3000]], "tunnel_pid": None},
            {"domains": [], "mappings": [], "tunnel_pid": 1},
        ):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                validate_plan(plan)


if __name__ == "__main__":
    unittest.main()
