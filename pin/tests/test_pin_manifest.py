"""Machine paths must not leak into a gate's input."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

PIN = Path(__file__).resolve().parents[1] / "PIN.json"


def walk(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (index,))
    else:
        yield path, value


class PinManifestTest(unittest.TestCase):
    def setUp(self):
        self.pin = json.loads(PIN.read_text())

    def test_no_machine_path_outside_informational(self):
        leaks = [
            (path, value)
            for path, value in walk(self.pin)
            if isinstance(value, str)
            and value.startswith("/")
            and "informational" not in path
        ]
        self.assertEqual(leaks, [])

    def test_every_deviation_has_a_checked_patch(self):
        deviations = self.pin["official_base"]["deviations"]
        self.assertEqual(len(deviations), 3)
        for dev in deviations:
            self.assertIn("patch", dev, dev["path"])
            self.assertIn("patch_sha256", dev, dev["path"])
            patch = PIN.parent / dev["patch"]
            data = patch.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),
                             dev["patch_sha256"], dev["path"])
            self.assertIn(("b/" + dev["path"]).encode(), data, dev["path"])

    def test_the_content_map_is_still_there(self):
        files = self.pin["official_base"]["files"]
        self.assertEqual(len(files), 12)
        self.assertTrue(all(len(sha) == 64 for sha in files.values()))


if __name__ == "__main__":
    unittest.main()
