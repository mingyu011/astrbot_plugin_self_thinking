"""自主行动的底层工具：专用工作区、审计、待确认动作、只读网页、屏幕截图。

所有高风险操作都不在这里直接执行；这里只负责“记录 + 请求确认”。
真正执行只发生在主人确认之后，并且仍然受工作区和动作类型限制。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import httpx
except Exception:  # pragma: no cover - 测试环境可能没有
    httpx = None

try:
    from PIL import ImageGrab
except Exception:  # pragma: no cover
    ImageGrab = None

_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|svg|canvas)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")
_URL_HOST_BLOCK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class WorkspaceError(ValueError):
    pass


class Workspace:
    """只允许访问 root 内部；路径逃逸一律拒绝。"""

    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _inside(self, relative: str) -> Path:
        raw = str(relative or "").strip().replace("\\", "/")
        if not raw:
            raise WorkspaceError("文件名不能为空")
        candidate = Path(raw)
        if candidate.is_absolute():
            candidate = candidate
        else:
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise WorkspaceError("只能访问专用工作区内的文件") from exc
        return resolved

    def path(self, relative: str) -> Path:
        return self._inside(relative)

    def write(self, relative: str, text: str, append: bool = False) -> Path:
        path = self._inside(relative)
        if path == self.root:
            raise WorkspaceError("不能写工作区根目录")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(text or "")
        if len(content) > 200_000:
            raise WorkspaceError("单次写入超过 200000 字")
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8", newline="\n") as f:
            f.write(content)
        return path

    def read(self, relative: str, max_chars: int = 20000) -> tuple[Path, str]:
        path = self._inside(relative)
        if not path.is_file():
            raise WorkspaceError("文件不存在")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(max(1, min(int(max_chars or 20000), 200000)))
        return path, text

    def list(self, limit: int = 100) -> list[str]:
        out = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                try:
                    out.append(str(path.relative_to(self.root)))
                except ValueError:
                    continue
            if len(out) >= max(1, int(limit or 100)):
                break
        return out

    def delete(self, relative: str) -> Path:
        path = self._inside(relative)
        if path == self.root:
            raise WorkspaceError("不能删除工作区根目录")
        if path.is_dir():
            raise WorkspaceError("只允许删除文件，不允许递归删除目录")
        if not path.is_file():
            raise WorkspaceError("文件不存在")
        path.unlink()
        return path

    def screens_dir(self) -> Path:
        path = self.root / "screens"
        path.mkdir(parents=True, exist_ok=True)
        return path


class AuditLog:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "audit.jsonl"
        self._lock = threading.Lock()

    def write(self, action: str, detail: Any = None, result: str = "",
              status: str = "ok") -> None:
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": str(action or ""),
            "status": str(status or ""),
            "detail": detail if isinstance(detail, (dict, list, str, int,
                                                    float, bool, type(None)))
            else str(detail),
            "result": str(result or "")[:2000],
        }
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def recent(self, limit: int = 20) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-max(1, int(limit or 20)):]:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out


class PendingStore:
    """高风险动作确认队列。确认前绝不执行。

    1.2.0 起不再给主人推送“确认/拒绝按钮”编号；这里只保存模型自己的
    意图，由模型用自然语言向主人请求同意。主人自然回复后，模型再调用
    self_execute_action；执行时用当前这条主人消息判断是否真的同意。
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "pending_actions.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".pending-", suffix=".json",
                                   dir=str(self.root))
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

    def add(self, action: str, target: str, details: str,
            requested_by: str = "", session: str = "") -> dict:
        with self._lock:
            action_id = f"act-{int(time.time())}-{secrets.token_hex(3)}"
            item = {
                "id": action_id,
                "action": str(action or ""),
                "target": str(target or ""),
                "details": str(details or ""),
                "requested_by": str(requested_by or ""),
                "session": str(session or ""),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "pending",
            }
            self._data[action_id] = item
            self._save()
            return dict(item)

    def get(self, action_id: str) -> dict | None:
        with self._lock:
            item = self._data.get(str(action_id or ""))
            return dict(item) if isinstance(item, dict) else None

    def pop(self, action_id: str) -> dict | None:
        with self._lock:
            item = self._data.pop(str(action_id or ""), None)
            if item is not None:
                self._save()
            return dict(item) if isinstance(item, dict) else None

    def latest(self, session: str = "") -> dict | None:
        with self._lock:
            items = [dict(v) for v in self._data.values()
                     if isinstance(v, dict) and v.get("status") == "pending"
                     and (not session or str(v.get("session") or "") == session)]
        if not items:
            return None
        items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return items[0]

    def pending(self, limit: int = 20) -> list[dict]:
        with self._lock:
            items = [dict(v) for v in self._data.values()
                     if isinstance(v, dict) and v.get("status") == "pending"]
        items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return items[:max(1, int(limit or 20))]


class WebReader:
    """只读 HTTP：不发 POST、不登录、不带 cookie、不执行 JS。"""

    def __init__(self, max_bytes: int = 1_500_000, timeout: float = 20.0):
        self.max_bytes = max(10_000, int(max_bytes or 1_500_000))
        self.timeout = max(5.0, float(timeout or 20.0))

    @staticmethod
    def validate(url: str) -> str:
        url = str(url or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("只允许 http/https 网页")
        host = parts.hostname.lower()
        if host in _URL_HOST_BLOCK:
            raise ValueError("不允许访问本机地址")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and (ip.is_private or ip.is_loopback
                               or ip.is_reserved):
            raise ValueError("不允许访问内网地址")
        return url

    @staticmethod
    def _text(raw: str) -> tuple[str, str]:
        title = ""
        match = re.search(r"<title[^>]*>(.*?)</title>", raw,
                          re.IGNORECASE | re.DOTALL)
        if match:
            title = html.unescape(_TAG_RE.sub("", match.group(1))).strip()
        text = _SCRIPT_RE.sub(" ", raw)
        text = _TAG_RE.sub(" ", text)
        text = html.unescape(text)
        text = _SPACE_RE.sub(" ", text)
        text = _BLANK_RE.sub("\n\n", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return title, "\n".join(lines)

    async def fetch(self, url: str, max_chars: int = 8000) -> dict:
        if httpx is None:
            raise RuntimeError("缺少 httpx，无法读取网页")
        url = self.validate(url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,text/plain",
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout, headers=headers
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            raw = response.text[:self.max_bytes]
            content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            text = raw.strip()
            title = ""
        else:
            title, text = self._text(raw)
        limit = max(500, min(int(max_chars or 8000), 30000))
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n……（网页正文已截断）"
        return {"url": url, "title": title, "text": text}


class BrowserHistory:
    """读取本机浏览器最近历史，使用现有账户数据，不创建独立 profile。

    只读 SQLite 的 History/places 数据库；不读 Cookie、密码、支付信息。
    Chrome/Edge 数据库被浏览器占用时先复制一份再查。
    """

    def __init__(self, workspace: Workspace, extra_paths=()):
        self.workspace = workspace
        self.extra_paths = [str(x) for x in (extra_paths or ()) if str(x).strip()]

    @staticmethod
    def _candidate_paths() -> list[tuple[str, Path]]:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        roaming = Path(os.environ.get("APPDATA", ""))
        candidates: list[tuple[str, Path]] = []
        if str(local):
            for label, rel in (
                ("Chrome", "Google/Chrome/User Data"),
                ("Edge", "Microsoft/Edge/User Data"),
                ("Brave", "BraveSoftware/Brave-Browser/User Data"),
                ("Chromium", "Chromium/User Data"),
            ):
                root = local / rel
                if root.is_dir():
                    for profile in sorted(root.glob("*/History")):
                        candidates.append((label, profile))
        if str(roaming):
            ff = roaming / "Mozilla/Firefox/Profiles"
            if ff.is_dir():
                for profile in sorted(ff.glob("*/places.sqlite")):
                    candidates.append(("Firefox", profile))
        return candidates

    def paths(self) -> list[tuple[str, Path]]:
        found = self._candidate_paths()
        for raw in self.extra_paths:
            path = Path(raw).expanduser()
            if path.is_file():
                found.append((path.parent.name or "custom", path))
        return found

    def _copy_db(self, source: Path) -> Path:
        folder = Path(tempfile.mkdtemp(prefix=".history-", dir=str(self.workspace.root)))
        target = folder / source.name
        try:
            shutil.copy2(source, target)
            for suffix in ("-wal", "-shm"):
                side = Path(str(source) + suffix)
                if side.is_file():
                    shutil.copy2(side, str(target) + suffix)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return target

    @staticmethod
    def _query_chromium(path: Path, limit: int) -> list[dict]:
        conn = sqlite3.connect(str(path), timeout=3)
        try:
            rows = conn.execute(
                "SELECT u.url, u.title, "
                "datetime(v.visit_time/1000000 - 11644473600, "
                "'unixepoch', 'localtime') "
                "FROM visits v JOIN urls u ON u.id=v.url "
                "ORDER BY v.visit_time DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [{"url": str(r[0] or ""), "title": str(r[1] or ""),
                 "time": str(r[2] or "")} for r in rows if r[0]]

    @staticmethod
    def _query_firefox(path: Path, limit: int) -> list[dict]:
        conn = sqlite3.connect(str(path), timeout=3)
        try:
            rows = conn.execute(
                "SELECT url, title, datetime(last_visit_date/1000000, "
                "'unixepoch', 'localtime') FROM moz_places "
                "WHERE last_visit_date IS NOT NULL "
                "ORDER BY last_visit_date DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [{"url": str(r[0] or ""), "title": str(r[1] or ""),
                 "time": str(r[2] or "")} for r in rows if r[0]]

    def recent(self, limit: int = 30) -> list[dict]:
        limit = max(1, min(int(limit or 30), 100))
        out: list[dict] = []
        seen = set()
        for label, source in self.paths():
            if not source.is_file():
                continue
            copied = None
            try:
                copied = self._copy_db(source)
                if source.name == "places.sqlite":
                    rows = self._query_firefox(copied, limit)
                else:
                    rows = self._query_chromium(copied, limit)
            except Exception:
                continue
            finally:
                if copied is not None:
                    shutil.rmtree(copied.parent, ignore_errors=True)
            for row in rows:
                url = row["url"]
                if url in seen:
                    continue
                seen.add(url)
                row["browser"] = label
                out.append(row)
                if len(out) >= limit:
                    return out
        return out


class ScreenCapture:
    """Pillow 截屏；失败时返回明确原因，不假装看到了。"""

    def __init__(self, workspace: Workspace, all_screens: bool = True):
        self.workspace = workspace
        self.all_screens = bool(all_screens)

    def capture(self) -> Path:
        if ImageGrab is None:
            raise RuntimeError("Pillow 的 ImageGrab 不可用")
        folder = self.workspace.screens_dir()
        name = time.strftime("screen-%Y%m%d-%H%M%S.png")
        path = folder / name
        try:
            image = ImageGrab.grab(all_screens=self.all_screens)
            image.save(path, "PNG")
        except Exception as exc:
            raise RuntimeError(f"截屏失败：{exc}") from exc
        return path
