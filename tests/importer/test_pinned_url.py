"""`--git-repo` accepts a URL; the pin is still checked byte for byte."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "src" / "gisel_anchor" / "importer" / "import_cxx.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("import_cxx_url_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FileUrlGitRepo(unittest.TestCase):
    def test_file_url_is_cloned_once_and_read_at_the_pin(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repo = d / "llvm"
            rel = "llvm/lib/Target/X86/X86LegalizerInfo.cpp"
            (repo / Path(rel).parent).mkdir(parents=True)
            (repo / rel).write_text("// pinned body\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b",
                            "-c", "user.name=t", "add", rel], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b",
                            "-c", "user.name=t", "commit", "-q", "-m", "pin"],
                           check=True)
            commit = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True).stdout.strip()
            digest = hashlib.sha256((repo / rel).read_bytes()).hexdigest()
            pin = d / "PIN.json"
            pin.write_text(json.dumps({
                "commit": commit,
                "sources": {"X86": {"path": rel, "sha256": digest}},
            }))
            cache = d / "cache"
            tool = _load_tool()
            with mock.patch.dict(os.environ,
                                 {"GISEL_ANCHOR_CACHE": str(cache)}):
                out = tool.pinned_source(
                    SimpleNamespace(name="X86"), pin, "file://%s" % repo)
                self.assertEqual(out.read_text(), "// pinned body\n")
                # Second call reuses the cache directory named by the commit.
                self.assertTrue((cache / commit).is_dir())
                out2 = tool.pinned_source(
                    SimpleNamespace(name="X86"), pin, "file://%s" % repo)
                self.assertEqual(out2.read_text(), "// pinned body\n")

    def test_a_sha_mismatch_still_fails(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repo = d / "llvm"
            rel = "llvm/lib/Target/X86/X86LegalizerInfo.cpp"
            (repo / Path(rel).parent).mkdir(parents=True)
            (repo / rel).write_text("// pinned body\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b",
                            "-c", "user.name=t", "add", rel], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b",
                            "-c", "user.name=t", "commit", "-q", "-m", "pin"],
                           check=True)
            commit = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True).stdout.strip()
            pin = d / "PIN.json"
            pin.write_text(json.dumps({
                "commit": commit,
                "sources": {"X86": {"path": rel, "sha256": "0" * 64}},
            }))
            tool = _load_tool()
            with mock.patch.dict(os.environ,
                                 {"GISEL_ANCHOR_CACHE": str(d / "cache")}):
                with self.assertRaises(SystemExit) as caught:
                    tool.pinned_source(
                        SimpleNamespace(name="X86"), pin, "file://%s" % repo)
            self.assertIn("pin says", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
