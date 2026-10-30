"""插件自己的小状态：所有私聊窗口、主动频率、最近表情。

1.0.1 起不再要求“绑定一个窗口”。每个私聊窗口各自记录活跃时间、
主动间隔和每日次数，启用后自动覆盖所有出现过的私聊窗口。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time


def _new_window(user_id: str = "", last_active: float = 0.0,
                text: str = "") -> dict:
    return {
        "user_id": str(user_id or ""),
        "last_active_at": float(last_active or 0.0),
        "last_message": str(text or "")[:1000],
        "last_proactive_at": 0.0,
        "last_proactive_attempt_at": 0.0,
        "daily_date": "",
        "daily_count": 0,
        "last_job_id": "",
    }


def _default_state() -> dict:
    return {
        "windows": {},
        # 旧版本字段保留一轮，便于从 1.0.0 状态自动迁移
        "last_private_umo": "",
        "last_user_id": "",
        "last_active_at": 0.0,
        "last_proactive_at": 0.0,
        "last_proactive_attempt_at": 0.0,
        "last_message": "",
        "last_job_id": "",
        "last_error": "",
        "daily_date": "",
        "daily_count": 0,
        "recent_emoji_ids": [],
        "manual_off": False,
    }


class StateStore:
    def __init__(self, root: str):
        self.root = str(root)
        os.makedirs(self.root, exist_ok=True)
        self.path = os.path.join(self.root, "state.json")
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError, TypeError):
            raw = {}
        data = _default_state()
        if isinstance(raw, dict):
            for key in data:
                if key in raw:
                    data[key] = raw[key]

        windows = data.get("windows")
        if not isinstance(windows, dict):
            windows = {}
        clean: dict[str, dict] = {}
        for umo, value in windows.items():
            if not isinstance(value, dict) or not str(umo).strip():
                continue
            item = _new_window(
                value.get("user_id"),
                value.get("last_active_at"),
                value.get("last_message"),
            )
            for key in item:
                if key in value:
                    item[key] = value[key]
            clean[str(umo)] = item
        data["windows"] = clean

        # 1.0.0 只存了一个最后窗口；升级时自动变成多窗口里的一项。
        legacy = str(data.get("last_private_umo") or "").strip()
        if legacy and legacy not in clean:
            item = _new_window(
                data.get("last_user_id"),
                data.get("last_active_at"),
                data.get("last_message"),
            )
            item["last_proactive_at"] = data.get("last_proactive_at") or 0.0
            item["last_proactive_attempt_at"] = (
                data.get("last_proactive_attempt_at") or 0.0)
            item["daily_date"] = data.get("daily_date") or ""
            item["daily_count"] = data.get("daily_count") or 0
            item["last_job_id"] = data.get("last_job_id") or ""
            clean[legacy] = item
        return data

    def save(self) -> None:
        with self._lock:
            os.makedirs(self.root, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json",
                                       dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2,
                              sort_keys=True)
                os.replace(tmp, self.path)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError:
                    pass

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value

    def update(self, **values) -> None:
        self._data.update(values)

    def snapshot(self) -> dict:
        return dict(self._data)

    # ------------------------------------------------------------ 窗口

    def windows(self) -> dict[str, dict]:
        value = self._data.get("windows")
        if not isinstance(value, dict):
            return {}
        return {str(k): dict(v) for k, v in value.items()
                if isinstance(v, dict)}

    def window(self, umo: str) -> dict | None:
        value = self.windows().get(str(umo or ""))
        return value

    def note_private(self, umo: str, user_id: str, text: str = "") -> None:
        umo = str(umo or "").strip()
        if not umo:
            return
        now = time.time()
        windows = self._data.setdefault("windows", {})
        if not isinstance(windows, dict):
            windows = {}
            self._data["windows"] = windows
        old = windows.get(umo)
        item = old if isinstance(old, dict) else _new_window(user_id)
        windows[umo] = item
        item["user_id"] = str(user_id or item.get("user_id") or "")
        item["last_active_at"] = now
        item["last_message"] = " ".join(str(text or "").split())[:1000]

        # 旧字段同步保留，方便外部工具/旧版本读取。
        self._data.update({
            "last_private_umo": umo,
            "last_user_id": str(user_id or ""),
            "last_active_at": now,
            "last_message": item["last_message"],
        })
        self.save()

    def note_trigger(self, umo: str, job_id: str = "") -> None:
        umo = str(umo or "").strip()
        windows = self._data.setdefault("windows", {})
        if not isinstance(windows, dict):
            return
        item = windows.get(umo)
        if not isinstance(item, dict):
            return
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if item.get("daily_date") != day:
            item["daily_date"] = day
            item["daily_count"] = 0
        item["last_proactive_attempt_at"] = now
        item["daily_count"] = int(item.get("daily_count") or 0) + 1
        if job_id:
            item["last_job_id"] = str(job_id)
        self._data["last_proactive_attempt_at"] = now
        self._data["daily_date"] = day
        self._data["daily_count"] = item["daily_count"]
        self.save()

    def note_sent_attempt(self, umo: str) -> None:
        # 任务已经交给主 Agent，是否真正发送由模型决定。
        windows = self._data.setdefault("windows", {})
        item = windows.get(str(umo or "")) if isinstance(windows, dict) else None
        if isinstance(item, dict):
            item["last_proactive_at"] = time.time()
            self._data["last_proactive_at"] = item["last_proactive_at"]
            self.save()

    def note_error(self, message: str) -> None:
        self._data["last_error"] = str(message or "")[:1000]
        self.save()

    def recent_emoji(self) -> list[str]:
        value = self._data.get("recent_emoji_ids")
        return [str(x) for x in value] if isinstance(value, list) else []

    def note_emoji(self, item_id: str, limit: int = 30) -> None:
        values = [x for x in self.recent_emoji() if x != item_id]
        values.append(str(item_id))
        self._data["recent_emoji_ids"] = values[-max(1, int(limit)):]
        self.save()
