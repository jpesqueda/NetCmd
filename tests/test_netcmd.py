"""Offline unit tests; no network access or Netmiko required."""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netcmd


class ParserTests(unittest.TestCase):
    def test_interactive_chain(self):
        steps = netcmd.parse_steps(list(enumerate("""@timeout 60
copy running-config startup-config
@expect Destination filename
@send [Enter]
@timeout 240
install all nxos bootflash:image.bin
@expect continue.*installation
@send y
@wait-disconnect 300
@reconnect 900
""".splitlines(), 1)), "test")
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].timeout, 60)
        self.assertEqual(steps[0].interactions, [("Destination filename", "[Enter]")])
        self.assertEqual(steps[1].wait_disconnect, 300)
        self.assertEqual(steps[1].reconnect, 900)

    def test_missing_response(self):
        with self.assertRaises(netcmd.InputError):
            netcmd.parse_steps([(1, "show version"), (2, "@expect question")], "bad")

    def test_multifile_duplicates(self):
        with TemporaryDirectory() as temp:
            f = Path(temp)/"multi.txt"
            f.write_text("HostName:SW1\nDeviceType:cisco_ios\nshow ver\n!\nHostName:SW2\nshow clock\n!\nHostName:sw1\nshow mod\n", encoding="utf-8")
            jobs = netcmd.load_multifile(str(f))
            self.assertEqual(len(jobs), 2)
            self.assertEqual([s.command for s in jobs[0].steps], ["show ver", "show mod"])
            self.assertEqual(jobs[0].device_type, "cisco_ios")

    def test_outside_host_rejected(self):
        with TemporaryDirectory() as temp:
            f = Path(temp)/"multi.txt"
            f.write_text("show version\nHostName:sw1\nshow version\n", encoding="utf-8")
            with self.assertRaises(netcmd.InputError):
                netcmd.load_multifile(str(f))

    def test_filename(self):
        class Args:
            prefix="pre"; suffix="checks"; timestamp=True
        self.assertEqual(netcmd.result_filename("SW01", Args(), "20261112_090000"), "pre_SW01_checks_20261112_090000.txt")

    def test_wr(self):
        self.assertIsNone(netcmd.classify_command("wr", "Building configuration...\n[OK]\n"))
        self.assertIsNotNone(netcmd.classify_command("wr", "% Error: Not Enough space"))
        self.assertIsNotNone(netcmd.classify_command("wr", "Building configuration..."))


class FakeSSH:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
    def write_channel(self, value):
        self.sent.append(value)
    def read_channel(self):
        return self.chunks.pop(0) if self.chunks else ""


class SSHFlowTests(unittest.TestCase):
    def test_two_prompts_one_command(self):
        conn = FakeSSH(["copy file\nDestination filename [x]?", "Overwrite file? [y/n]", "Done\nSW01#"])
        step = netcmd.CommandStep("copy run bootflash:x", 1, interactions=[("Destination filename", "[Enter]"), ("Overwrite file", "y")])
        class Args: pass
        output, conn2 = netcmd.run_command(conn, step, 5, "SW01", "cisco_nxos", "admin", "fake", Args())
        self.assertIs(conn, conn2)
        self.assertEqual(conn.sent, ["copy run bootflash:x\n", "\n", "y\n"])
        self.assertIn("Overwrite file", output)


if __name__ == "__main__":
    unittest.main()
