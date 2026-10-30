from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.image_library import EmojiLibrary
from core.state import StateStore

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class ImageLibraryTest(unittest.TestCase):
    def test_index_and_tag_choice(self):
        lib = EmojiLibrary(FIXTURES)
        self.assertEqual(lib.count(), 3)
        self.assertEqual(lib.choose("今天开心").id, "happy")
        self.assertEqual(lib.choose("我有点难过").id, "sad")
        self.assertEqual(lib.choose("猫猫得意").id, "cat")
        self.assertIsNone(lib.choose("完全无关的句子"))

    def test_recent_prefers_other_but_does_not_drop_match(self):
        lib = EmojiLibrary(FIXTURES)
        self.assertEqual(lib.choose("开心", recent=["happy"]).id, "happy")
        self.assertEqual(lib.choose("猫", recent=["cat", "sad"]).id, "cat")

    def test_config_fallback(self):
        root = os.path.join(FIXTURES, "config_only")
        lib = EmojiLibrary(root,
                           config_path=os.path.join(root, "image_config.json"))
        self.assertEqual(lib.count(), 1)
        self.assertEqual(lib.choose("害羞").id, "a")


class StateTest(unittest.TestCase):
    def test_read_existing_state_and_migrate_legacy_window(self):
        state = StateStore(os.path.join(FIXTURES, "state"))
        self.assertEqual(state.get("last_private_umo"), "aiocqhttp:private:1")
        self.assertEqual(state.get("last_message"), "hello")
        self.assertEqual(state.get("daily_count"), 2)
        self.assertEqual(state.recent_emoji(), ["a", "b"])
        windows = state.windows()
        self.assertEqual(len(windows), 1)
        self.assertTrue(windows["aiocqhttp:private:1"]["last_active_at"])

    def test_multiple_windows_are_independent(self):
        state = StateStore(os.path.join(FIXTURES, "state_multi"))
        windows = state.windows()
        self.assertEqual(set(windows), {
            "aiocqhttp:private:111",
            "aiocqhttp:private:222",
        })
        self.assertEqual(windows["aiocqhttp:private:111"]["daily_count"], 1)
        self.assertEqual(windows["aiocqhttp:private:222"]["daily_count"], 0)

    def test_note_private_adds_new_window_in_memory(self):
        state = StateStore(os.path.join(FIXTURES, "state"))
        state.save = lambda: None
        state.note_private("aiocqhttp:private:333", "333", "新窗口")
        self.assertIn("aiocqhttp:private:333", state.windows())
        self.assertEqual(
            state.windows()["aiocqhttp:private:333"]["last_message"], "新窗口")


if __name__ == "__main__":
    unittest.main()
