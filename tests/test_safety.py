from __future__ import annotations

import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import safety


class ApprovalTest(unittest.TestCase):
    def test_explicit_yes(self):
        for text in ("可以", "买吧", "去吧", "好的", "嗯", "行", "OK", "Yes",
                     "没问题", "同意", "就这个", "准了"):
            self.assertTrue(safety.is_approval(text), text)

    def test_question_is_not_approval(self):
        for text in ("可以吗", "可以吗？", "你确定吗", "能买么", "行不行呢"):
            self.assertFalse(safety.is_approval(text), text)

    def test_negation_wins(self):
        for text in ("不要", "不用了", "算了", "先别买", "不好", "不行",
                     "不可以", "我同意个鬼", "我在上班，回去再说",
                     "以后再说", "可以，但是先别买", "no", "nope",
                     "cancel", "not now"):
            self.assertFalse(safety.is_approval(text), text)

    def test_hypothetical_is_not_approval(self):
        for text in ("如果便宜就买", "要是明天有空就去吧", "假如可以的话"):
            self.assertFalse(safety.is_approval(text), text)

    def test_empty_and_noise(self):
        for text in ("", "   ", "在吗", "今天天气不错", "嗯？"):
            self.assertFalse(safety.is_approval(text), text)


class PackageTest(unittest.TestCase):
    def test_accepts_normal_names(self):
        for text in ("requests", "python-dotenv", "bge_m3",
                     "requests==2.31.0", "httpx>=0.27", "torch~=2.1.0"):
            self.assertEqual(safety.validate_package(text),
                             " ".join(text.split()))

    def test_rejects_option_injection(self):
        for text in ("--index-url http://evil", "-r req.txt",
                     "requests --upgrade", "--extra-index-url=http://x",
                     " ../evil", "包名", "", "   "):
            with self.assertRaises(ValueError, msg=text):
                safety.validate_package(text)


class TrashTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="st-trash-")
        self.data = os.path.join(self.root, "data")
        os.makedirs(self.data, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_moves_file_into_trash(self):
        target = os.path.join(self.root, "note.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("hello")
        moved = safety.trash_target(self.data, target, stamp="20260101-000000")
        self.assertFalse(os.path.exists(target), "原路径应该不在了")
        self.assertTrue(os.path.isfile(moved))
        self.assertIn("trash", moved)
        with open(moved, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello")

    def test_moves_directory_into_trash(self):
        folder = os.path.join(self.root, "old")
        os.makedirs(os.path.join(folder, "sub"))
        with open(os.path.join(folder, "sub", "a.txt"), "w") as f:
            f.write("x")
        moved = safety.trash_target(self.data, folder, stamp="20260101-000001")
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.isdir(moved))

    def test_refuses_root_and_system_dirs(self):
        for target in (os.path.abspath(os.sep), "C:\\Windows",
                       "C:\\Program Files"):
            with self.assertRaises(ValueError, msg=target):
                safety.trash_target(self.data, target)

    def test_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            safety.trash_target(self.data,
                                os.path.join(self.root, "nope.txt"))


if __name__ == "__main__":
    unittest.main()
