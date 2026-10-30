from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.autonomy import WebReader, Workspace, WorkspaceError

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class WorkspaceTest(unittest.TestCase):
    def test_path_guard(self):
        ws = Workspace(FIXTURES)
        self.assertTrue(str(ws.path("image_index.json")).endswith("image_index.json"))
        with self.assertRaises(WorkspaceError):
            ws.path("../outside.txt")
        with self.assertRaises(WorkspaceError):
            ws.path("")


class WebReaderTest(unittest.TestCase):
    def test_url_validation(self):
        self.assertEqual(WebReader.validate("https://example.com/a"),
                         "https://example.com/a")
        for url in ("file:///etc/passwd", "http://127.0.0.1/x",
                    "http://localhost/x", "ftp://example.com"):
            with self.assertRaises(ValueError):
                WebReader.validate(url)


if __name__ == "__main__":
    unittest.main()
