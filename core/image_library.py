"""本地表情库：读取智能图片对话已经打好的标签，不做任何图片分析。

图片由智能图片对话负责收集、打标签；那个插件平时可以不开。
本模块只读它的 image_index.json / 配置里的 image_tags，按标签和语义
打分选一张，供模型通过 send_emoji 工具主动决定要不要发。
"""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_TERM_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
_SPLIT_RE = re.compile(r"[\s,，、;；|/]+")


def _clean(value) -> str:
    return str(value or "").strip()


def _dedupe(values: Iterable[str]) -> list[str]:
    out = []
    seen = set()
    for value in values:
        text = _clean(value)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _terms(value: str) -> set[str]:
    text = _clean(value).lower()
    out: set[str] = set()
    for chunk in _TERM_RE.findall(text):
        out.add(chunk)
        if len(chunk) > 2 and re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            for i in range(len(chunk) - 1):
                out.add(chunk[i:i + 2])
    return out


@dataclass
class EmojiItem:
    id: str
    path: str
    tags: list[str] = field(default_factory=list)
    source: str = "index"

    def label(self) -> str:
        return "、".join(self.tags[:5]) or self.id


class EmojiLibrary:
    """从智能图片对话的数据目录读取表情；不依赖那个插件正在运行。"""

    def __init__(self, data_dir: str, index_path: str = "",
                 config_path: str = "", recent_limit: int = 30):
        self.data_dir = str(data_dir or "")
        self.index_path = str(index_path or "")
        self.config_path = str(config_path or "")
        self.recent_limit = max(1, int(recent_limit or 30))
        self._items: list[EmojiItem] = []
        self._stamp: tuple = ()
        self._lock = None

    # ------------------------------------------------------------ 读取

    def _default_index(self) -> str:
        return self.index_path or os.path.join(self.data_dir, "image_index.json")

    def _default_config(self) -> str:
        return self.config_path

    def _read_json(self, path: str):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return None

    def _resolve(self, rel_path: str) -> str:
        rel = _clean(rel_path).replace("\\", "/")
        if not rel:
            return ""
        if os.path.isabs(rel):
            return os.path.normpath(rel)
        return os.path.normpath(os.path.join(self.data_dir, rel))

    def _from_index(self, data) -> list[EmojiItem]:
        items = []
        images = data.get("images") if isinstance(data, dict) else None
        if isinstance(images, dict):
            rows = images.values()
        elif isinstance(images, list):
            rows = images
        else:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rel = _clean(row.get("rel_path") or row.get("image_path")
                         or row.get("path"))
            path = self._resolve(rel)
            if not path:
                continue
            tags = _dedupe(row.get("tags") or row.get("manual_tags")
                           or row.get("auto_tags") or [])
            if not tags:
                tags = _dedupe(row.get("auto_tags") or [])
            item_id = _clean(row.get("id") or row.get("filename")
                             or os.path.basename(path))
            items.append(EmojiItem(item_id, path, tags, "index"))
        return items

    def _from_config(self, data) -> list[EmojiItem]:
        rows = data.get("image_tags") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rel = _clean(row.get("image_path") or row.get("rel_path")
                         or row.get("path"))
            path = self._resolve(rel)
            if not path:
                continue
            tags = _dedupe(row.get("tags") or row.get("auto_tags")
                           or row.get("manual_tags") or [])
            item_id = _clean(row.get("__template_key") or row.get("id")
                             or os.path.basename(path))
            items.append(EmojiItem(item_id, path, tags, "config"))
        return items

    def load(self, force: bool = False) -> list[EmojiItem]:
        index = self._default_index()
        config = self._default_config()
        stamp = []
        for path in (index, config):
            try:
                stat = os.stat(path)
                stamp.append((path, stat.st_mtime_ns, stat.st_size))
            except OSError:
                stamp.append((path, 0, 0))
        stamp_key = tuple(stamp)
        if not force and self._items and stamp_key == self._stamp:
            return self._items

        items: list[EmojiItem] = []
        data = self._read_json(index) if index else None
        if data is not None:
            items = self._from_index(data)
        if not items and config:
            data = self._read_json(config)
            if data is not None:
                items = self._from_config(data)

        # 同 id / 同路径去重；后出现的配置标签优先保留
        merged: dict[str, EmojiItem] = {}
        for item in items:
            key = item.id or item.path
            if key not in merged:
                merged[key] = item
                continue
            old = merged[key]
            old.tags = _dedupe([*old.tags, *item.tags])
        self._items = list(merged.values())
        self._stamp = stamp_key
        return self._items

    def count(self) -> int:
        return len(self.load())

    # ------------------------------------------------------------ 选图

    def choose(self, query: str = "", tags: str = "", recent: Iterable[str] = (),
               allow_any: bool = False,
               rng: random.Random | None = None) -> EmojiItem | None:
        items = self.load()
        if not items:
            return None
        query_text = " ".join(x for x in (_clean(query), _clean(tags)) if x)
        q_terms = _terms(query_text)
        recent_set = {_clean(x) for x in recent if _clean(x)}
        candidates = [x for x in items if x.id not in recent_set]
        if not candidates:
            candidates = list(items)

        def collect(pool: list[EmojiItem]) -> list[tuple[float, EmojiItem]]:
            scored_local = []
            for item in pool:
                score = 0.0
                label = " ".join(item.tags).lower()
                if query_text and label:
                    if query_text.lower() in label:
                        score += 3.0
                    for tag in item.tags:
                        tag_text = _clean(tag).lower()
                        if not tag_text:
                            continue
                        tag_terms = _terms(tag_text)
                        if tag_text and tag_text in query_text.lower():
                            score += 5.0
                        overlap = len(tag_terms & q_terms)
                        if overlap:
                            score += overlap * 2.0
                if score > 0:
                    scored_local.append((score, item))
            return scored_local

        scored = collect(candidates)
        if not scored and recent_set:
            # 只有最近用过的图匹配时，宁可重复一张，也不要完全放弃表达。
            scored = collect(list(items))
        # 没有任何命中就不乱发；模型可以继续用文字。
        if not scored:
            if not allow_any:
                return None
            scored = [(0.01, item) for item in candidates]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[0][0]
        best = [item for score, item in scored if score >= top - 0.001]
        picker = rng or random
        return picker.choice(best)

    def remember(self, recent: list[str], item: EmojiItem) -> list[str]:
        out = [x for x in recent if x != item.id]
        out.append(item.id)
        return out[-self.recent_limit:]
