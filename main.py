"""自主思维：让主 Agent 自己决定要不要主动开口。

设计边界：

- 定时器只负责“给主 Agent 一次机会”，不生成、不代发任何一句话。
- 真正说不说、说什么，由 AstrBot 主 Agent 决定；它可以保持沉默。
- 主动唤醒走 AstrBot 的 ActiveAgentCronJob，不是 context.send_message。
- 表情包必须由模型调用 send_emoji 工具选中；装饰阶段只把选中的图追加到
  回复末尾，不替模型决定发不发。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .core import memory_bridge
from .core.autonomy import (AuditLog, BrowserHistory, PendingStore,
                            ScreenCapture, WebReader, Workspace)
from .core.image_library import EmojiLibrary
from .core.state import StateStore

PLUGIN_NAME = "astrbot_plugin_self_thinking"
DISPLAY_NAME = "自主思维"
VERSION = "1.2.0"

DEFAULTS = {
    "enabled": True,
    "check_interval_seconds": 60,
    "min_idle_minutes": 90,
    "min_gap_minutes": 180,
    "daily_cap": 4,
    "quiet_start": 23,
    "quiet_end": 7,
    "memory_max_chars": 2400,
    "emoji_enabled": True,
    "image_plugin_data_dir": "",
    "image_index_path": "",
    "image_config_path": "",
    "emoji_recent_limit": 30,
    "emoji_fallback_random": False,
    "prompt_extra": "",
    "autonomy_enabled": True,
    "workspace_dir": "",
    "screen_observe_enabled": True,
    "screen_caption_provider_id": "",
    "screen_all_screens": True,
    "screen_caption_enabled": True,
    "browser_read_enabled": True,
    "browser_open_enabled": True,
    "browser_history_enabled": True,
    "browser_history_paths": "",
}

def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _umo_user(umo: str) -> str:
    parts = str(umo or "").split(":")
    return parts[-1].strip() if len(parts) >= 3 else ""


class SelfThinking(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        self.data_dir = self._plugin_data_dir()
        self.state = StateStore(str(self.data_dir))
        self.library = self._make_library()
        self.workspace = self._make_workspace()
        self.audit = AuditLog(str(self.data_dir))
        self.pending = PendingStore(str(self.data_dir))
        self.web = WebReader()
        extra_history = [
            value.strip()
            for value in re.split(
                r"[;,]", str(self._cfg("browser_history_paths") or ""))
            if value.strip()
        ]
        self.history = BrowserHistory(
            self.workspace, extra_paths=extra_history)
        self.screen = ScreenCapture(
            self.workspace,
            all_screens=_as_bool(self._cfg("screen_all_screens"), True),
        )
        self._task = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._start_loop()

        logger.info(
            f"[self_thinking] 自主思维 {VERSION} 已加载｜"
            f"表情 {self.library.count()} 张｜"
            f"自动窗口 {len(self._windows())} 个"
        )

    # ------------------------------------------------------------ 配置 / 目录

    def _cfg(self, key: str):
        value = None
        config = getattr(self, "config", None)
        if config is not None:
            try:
                value = config.get(key) if hasattr(config, "get") else None
            except Exception:
                value = None
        if value is None:
            value = DEFAULTS.get(key)
        return value

    def _plugin_data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            root = Path(os.getcwd()) / "data" / "plugin_data" / PLUGIN_NAME
            root.mkdir(parents=True, exist_ok=True)
            return root

    def _smart_image_data_dir(self) -> str:
        configured = str(self._cfg("image_plugin_data_dir") or "").strip()
        if configured:
            return os.path.abspath(os.path.expanduser(configured))
        # 同一个 plugin_data 根目录下通常就是它的数据目录；这比依赖
        # StarTools 的调用栈探测更稳，也不要求智能图片对话正在运行。
        try:
            sibling = self.data_dir.parent / "astrbot_plugin_smart_imagechat_hub"
            if sibling.is_dir():
                return str(sibling)
        except Exception:
            pass
        try:
            return str(StarTools.get_data_dir("astrbot_plugin_smart_imagechat_hub"))
        except Exception:
            return os.path.join(os.getcwd(), "data", "plugin_data",
                                "astrbot_plugin_smart_imagechat_hub")

    def _smart_image_config(self, data_dir: str) -> str:
        configured = str(self._cfg("image_config_path") or "").strip()
        if configured:
            return os.path.abspath(os.path.expanduser(configured))
        # data/plugin_data/<plugin> -> data/config/<plugin>_config.json
        data_root = os.path.dirname(os.path.dirname(data_dir))
        return os.path.join(data_root, "config",
                            "astrbot_plugin_smart_imagechat_hub_config.json")

    def _make_library(self) -> EmojiLibrary:
        data_dir = self._smart_image_data_dir()
        return EmojiLibrary(
            data_dir=data_dir,
            index_path=str(self._cfg("image_index_path") or "").strip(),
            config_path=self._smart_image_config(data_dir),
            recent_limit=_as_int(self._cfg("emoji_recent_limit"), 30),
        )

    def _make_workspace(self) -> Workspace:
        configured = str(self._cfg("workspace_dir") or "").strip()
        root = (os.path.abspath(os.path.expanduser(configured))
                if configured else str(self.data_dir / "workspace"))
        return Workspace(root)

    # ------------------------------------------------------------ 生命周期

    def _start_loop(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError:
            self._task = None

    async def terminate(self):
        self._closed = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _loop(self) -> None:
        # 启动后先等一个检查周期，避免插件加载时立刻往主 Agent 里塞任务。
        while not self._closed:
            interval = max(15, _as_int(self._cfg("check_interval_seconds"), 60))
            try:
                await asyncio.sleep(interval)
                if not self._closed:
                    await self._maybe_trigger()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[self_thinking] 定时检查失败: {exc}")
                await asyncio.sleep(10)

    # ------------------------------------------------------------ 触发条件

    def _enabled(self) -> bool:
        if not _as_bool(self._cfg("enabled"), True):
            return False
        if bool(self.state.get("autonomy_paused", False)):
            return False
        return not bool(self.state.get("manual_off", False))

    def _quiet(self, now: datetime) -> bool:
        start = _as_int(self._cfg("quiet_start"), 23)
        end = _as_int(self._cfg("quiet_end"), 7)
        start = max(0, min(23, start))
        end = max(0, min(23, end))
        if start == end:
            return False
        hour = now.hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _windows(self) -> dict[str, dict]:
        try:
            return self.state.windows()
        except Exception:
            return {}

    def _window_daily_count(self, window: dict) -> int:
        day = time.strftime("%Y-%m-%d", time.localtime())
        if str(window.get("daily_date") or "") != day:
            return 0
        return _as_int(window.get("daily_count"), 0)

    def _window_due(self, window: dict, now_ts: float) -> bool:
        last_active = float(window.get("last_active_at") or 0)
        idle_minutes = max(1, _as_int(self._cfg("min_idle_minutes"), 90))
        if last_active and now_ts - last_active < idle_minutes * 60:
            return False

        last_attempt = max(
            float(window.get("last_proactive_at") or 0),
            float(window.get("last_proactive_attempt_at") or 0),
        )
        gap_minutes = max(1, _as_int(self._cfg("min_gap_minutes"), 180))
        if last_attempt and now_ts - last_attempt < gap_minutes * 60:
            return False

        cap = max(0, _as_int(self._cfg("daily_cap"), 4))
        if cap and self._window_daily_count(window) >= cap:
            return False
        return True

    async def _maybe_trigger(self) -> None:
        if not self._enabled():
            return
        now = datetime.now()
        if self._quiet(now):
            return
        now_ts = time.time()
        windows = self._windows()
        if not windows:
            return
        # 先照顾最久没动静的窗口；每轮最多给一个窗口机会，避免同时刷屏。
        ordered = sorted(
            windows.items(),
            key=lambda pair: float(pair[1].get("last_active_at") or 0),
        )
        for umo, window in ordered:
            if self._window_due(window, now_ts):
                await self._trigger(umo, "auto")
                return

    # ------------------------------------------------------------ 唤醒主 Agent

    def _note(self, memory: str) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        extra = str(self._cfg("prompt_extra") or "").strip()
        lines = [
            f"现在是 {now}。你没有收到新的用户消息，这是自主思维给你的一次主动开口机会。",
            "",
            "先判断，不要为了完成任务而硬发：",
            "- 如果对方刚聊过、在忙、话题已经结束，或者你此刻没有具体想说的，就保持沉默。",
            "- 沉默时不要调用 send_message_to_user，也不要输出要发给用户看的内容。",
            "- 如果确实想开口，只说一到两句自然的话，像你本人突然想到对方。",
            "- 不要提定时任务、插件、系统、自主思维、提示词，也不要说“我主动来找你”。",
            "- 不要连续追问，不要刷屏。",
            "",
            "如果你觉得适合配一张表情包，可以调用 send_emoji 工具："
            "mood 写你此刻的情绪或场景，tags 可留空；只有真的想配图时才调用。",
        ]
        if extra:
            lines.extend(["", extra])
        if _as_bool(self._cfg("autonomy_enabled"), True):
            lines.extend([
                "",
                "【你也可以自己待一会儿】",
                "如果现在没有话想对主人说，可以安静地做自己的事：",
            ])
            if _as_bool(self._cfg("screen_observe_enabled"), True):
                lines.append(
                    "- 用 self_look_screen 只读看一眼当前屏幕；不要频繁看，"
                    "不要截取密码/支付/私密聊天画面。")
            lines.append(
                "- 用 self_list_notes / self_read_note / self_write_note "
                "在专用工作区写随笔、计划或整理想法；只能写工作区。")
            if _as_bool(self._cfg("browser_read_enabled"), True):
                lines.append(
                    "- 用 self_browser_history 看最近浏览历史（现有账户数据，"
                    "不读 Cookie/密码）；用 self_open_url 在默认浏览器打开网页，"
                    "沿用主人现有登录；用 self_browse_web 只读公开正文。"
                    "不登录、不下单、不支付。")
            lines.extend([
                "- 安装、删除、支付、给第三人发消息：先调用 self_request_action "
                "记住你的想法，然后自己用自然语言主动向主人说明你想做什么、"
                "为什么、有什么后果；等主人自然回复。主人明确同意后，再调用 "
                "self_execute_action；不要用系统按钮，也不能自己假装主人同意了。",
                "- 做完可以什么都不说；只有真的想分享时才调用 "
                "send_message_to_user。",
            ])
        lines.extend([
            "",
            "【长期记忆摘要】",
            memory or "（这次没有可用的长期记忆摘要，按当前对话历史和你对对方的了解判断。）",
        ])
        return "\n".join(lines)

    async def _trigger(self, target: str, reason: str = "auto") -> bool:
        target = str(target or "").strip()
        window = self._windows().get(target)
        if not window:
            return False
        if self._lock.locked():
            return False
        async with self._lock:
            if not self._enabled():
                return False
            memory_star = memory_bridge.find_all_memory(self.context)
            query = str(window.get("last_message") or "").strip()
            user_id = str(window.get("user_id") or _umo_user(target))
            memory = await memory_bridge.compose(
                memory_star,
                session=target,
                user_id=user_id,
                query=query,
                max_chars=_as_int(self._cfg("memory_max_chars"), 2400),
            )
            note = self._note(memory)
            cron = getattr(self.context, "cron_manager", None)
            if cron is None or not callable(getattr(cron, "add_active_job", None)):
                logger.warning("[self_thinking] 宿主没有 cron_manager，无法唤醒主 Agent")
                return False
            try:
                job = await cron.add_active_job(
                    name="自主思维主动开口",
                    cron_expression=None,
                    payload={
                        "session": target,
                        "note": note,
                        "sender_id": user_id,
                        "origin": "self_thinking",
                    },
                    description=f"自主思维：给主 Agent 一次主动开口机会（{reason}）",
                    enabled=True,
                    persistent=False,
                    run_once=True,
                    run_at=datetime.now(timezone.utc) + timedelta(seconds=2),
                )
            except Exception as exc:
                logger.warning(f"[self_thinking] 创建主动唤醒任务失败: {exc}")
                self.state.note_error(str(exc))
                return False
            job_id = str(getattr(job, "job_id", "") or "")
            self.state.note_trigger(target, job_id)
            self.state.note_sent_attempt(target)
            self._audit("proactive", {"session": target, "reason": reason},
                        job_id or "job")
            logger.info(
                f"[self_thinking] 已把主动开口机会交给主 Agent："
                f"{target}（job={job_id or '?'}，记忆 {len(memory)} 字）"
            )
            return True

    # ------------------------------------------------------------ 记录私聊窗口

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_private_message(self, event: AstrMessageEvent):
        """记录所有私聊窗口，不回复、不拦截。"""
        try:
            if self._task is None or self._task.done():
                self._start_loop()
            # 自主唤醒本身也是一次 private 事件；不要把它当成用户又说话了，
            # 否则会把主动提示词记成 last_message，下一次拿它去检索记忆。
            if callable(getattr(event, "get_extra", None)) and event.get_extra("cron_job"):
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not umo:
                return
            text = str(event.get_message_str() or "").strip()
            if text.startswith(("/", "／")):
                # 指令不是聊天内容，不要把 /selfthink 当成“最后聊了什么”。
                text = str(self.state.get("last_message") or "")
            self.state.note_private(umo, str(event.get_sender_id() or ""), text)
        except Exception as exc:
            logger.debug(f"[self_thinking] 记录私聊窗口失败: {exc}")

    # ------------------------------------------------------------ 自主行动

    def _audit(self, action: str, detail=None, result: str = "",
               status: str = "ok") -> None:
        try:
            self.audit.write(action, detail=detail, result=result, status=status)
        except Exception:
            pass

    def _autonomy_guard(self, event: AstrMessageEvent) -> str:
        if not _as_bool(self._cfg("autonomy_enabled"), True):
            return "自主行动已关闭。"
        if bool(self.state.get("autonomy_paused", False)):
            return "自主行动已暂停；等主人恢复后再做。"
        try:
            cron_event = bool(event.get_extra("cron_job"))
        except Exception:
            cron_event = False
        if not cron_event and not event.is_private_chat():
            return "自主行动只支持私聊。"
        return ""

    def _caption_provider(self):
        provider_id = str(self._cfg("screen_caption_provider_id") or "").strip()
        try:
            if provider_id:
                return self.context.get_provider_by_id(provider_id)
            return self.context.get_using_provider()
        except Exception:
            return None

    async def _caption_screen(self, path: Path) -> str:
        if not _as_bool(self._cfg("screen_caption_enabled"), True):
            return ""
        provider = self._caption_provider()
        if provider is None:
            return "（当前没有可用的看图模型，只保存了截图。）"
        fn = getattr(provider, "text_chat", None)
        if not callable(fn):
            return "（当前 Provider 不支持看图，只保存了截图。）"
        try:
            result = fn(
                prompt=(
                    "这是主人电脑当前屏幕的截图。只做事实描述：主人在做什么、"
                    "屏幕上有什么主要内容。不要猜测密码、支付信息或私密聊天，"
                    "不要给出操作建议。控制在 200 字以内。"
                ),
                image_urls=[str(path)],
            )
            if inspect.isawaitable(result):
                result = await result
            text = getattr(result, "completion_text", None) or str(result)
            text = " ".join(str(text or "").split())
            return text[:1000]
        except Exception as exc:
            logger.debug(f"[self_thinking] 屏幕描述失败: {exc}")
            return f"（看图模型调用失败：{exc}）"

    @filter.llm_tool(name="self_look_screen")
    async def tool_look_screen(self, event: AstrMessageEvent,
                               reason: str = "") -> str:
        """只读查看主人电脑当前屏幕，并尝试用看图模型描述。

        这是你“自己待着”时用的观察工具，不是每轮都要用。
        会保存截图到专用工作区；不要频繁调用，不要截取密码、支付、
        私密聊天等敏感画面。没有看图模型时只返回截图位置。

        Args:
            reason(string): 为什么想看屏幕，例如“想知道主人是不是在忙”
        """
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        if not _as_bool(self._cfg("screen_observe_enabled"), True):
            return "看屏幕功能已关闭。"
        try:
            path = self.screen.capture()
        except Exception as exc:
            self._audit("screen", {"reason": reason}, str(exc), "error")
            return f"截屏失败：{exc}"
        try:
            rel = str(path.relative_to(self.workspace.root))
        except ValueError:
            rel = str(path)
        caption = await self._caption_screen(path)
        self._audit("screen", {"reason": reason, "path": rel},
                    caption or "saved", "ok")
        body = f"截图已保存到专用工作区：{rel}"
        if caption:
            body += f"\n画面描述：{caption}"
        return body

    @filter.llm_tool(name="self_write_note")
    async def tool_write_note(self, event: AstrMessageEvent, name: str,
                              content: str, append: bool = False) -> str:
        """在专用工作区写随笔、计划或整理想法。

        只能写工作区内的文件；不能写系统文件、不能写工作区外。
        适合你自己无聊时记东西，不适合保存密码或隐私原文。

        Args:
            name(string): 相对文件名，例如“随笔/今天想到的事.md”
            content(string): 要写入的文字
            append(bool): 追加而不是覆盖，默认 false
        """
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        try:
            path = self.workspace.write(name, content, append=bool(append))
            rel = str(path.relative_to(self.workspace.root))
        except Exception as exc:
            self._audit("write_note", {"name": name}, str(exc), "error")
            return f"写入失败：{exc}"
        self._audit("write_note", {"name": rel, "append": bool(append),
                                   "chars": len(str(content or ""))},
                    "ok")
        return f"已写入专用工作区：{rel}（{len(str(content or ''))} 字）"

    @filter.llm_tool(name="self_read_note")
    async def tool_read_note(self, event: AstrMessageEvent, name: str,
                             max_chars: int = 12000) -> str:
        """读取专用工作区里的文件。

        Args:
            name(string): 相对文件名
            max_chars(int): 最多读多少字
        """
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        try:
            path, text = self.workspace.read(name, max_chars=max_chars)
            rel = str(path.relative_to(self.workspace.root))
        except Exception as exc:
            return f"读取失败：{exc}"
        self._audit("read_note", {"name": rel}, f"{len(text)} chars")
        return f"【{rel}】\n{text}"

    @filter.llm_tool(name="self_list_notes")
    async def tool_list_notes(self, event: AstrMessageEvent,
                              limit: int = 80) -> str:
        """列出专用工作区里的文件。"""
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        try:
            names = self.workspace.list(limit=limit)
        except Exception as exc:
            return f"列目录失败：{exc}"
        self._audit("list_notes", {"count": len(names)}, "ok")
        if not names:
            return "专用工作区还是空的。"
        return "专用工作区文件：\n" + "\n".join(f"- {name}" for name in names)

    @filter.llm_tool(name="self_browse_web")
    async def tool_browse_web(self, event: AstrMessageEvent, url: str,
                              max_chars: int = 7000) -> str:
        """只读打开一个网页，返回标题和正文。

        只支持 http/https，不登录、不带 Cookie、不提交表单、不下单、不支付。
        适合看新闻、文章、商品公开页面；动态网页可能读不全。

        Args:
            url(string): 要读的网页地址
            max_chars(int): 最多返回多少字
        """
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        if not _as_bool(self._cfg("browser_read_enabled"), True):
            return "浏览器只读功能已关闭。"
        try:
            info = await self.web.fetch(url, max_chars=max_chars)
        except Exception as exc:
            self._audit("browse", {"url": url}, str(exc), "error")
            return f"读取网页失败：{exc}"
        self._audit("browse", {"url": info["url"],
                               "title": info.get("title", "")[:200]},
                    "ok")
        title = info.get("title") or "(无标题)"
        return f"标题：{title}\n地址：{info['url']}\n\n{info.get('text', '')}"

    @filter.llm_tool(name="self_browser_history")
    async def tool_browser_history(self, event: AstrMessageEvent,
                                   limit: int = 20) -> str:
        """读取本机浏览器最近历史，使用现有登录账户的数据。

        不会创建独立浏览器 profile，也不会读取 Cookie、密码或支付信息。
        适合“我刚才看过什么”“最近在关注什么”这类回顾。
        """
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        if not _as_bool(self._cfg("browser_history_enabled"), True):
            return "浏览器历史读取已关闭。"
        try:
            rows = await asyncio.to_thread(self.history.recent, limit)
        except Exception as exc:
            self._audit("browser_history", {"limit": limit}, str(exc), "error")
            return f"读取浏览器历史失败：{exc}"
        self._audit("browser_history", {"limit": limit}, f"{len(rows)} rows")
        if not rows:
            return "没有读到浏览器历史（可能没有权限，或浏览器数据目录不在这里）。"
        lines = ["最近浏览器历史（不会带登录 Cookie）："]
        for row in rows:
            title = (row.get("title") or "").strip()[:100]
            lines.append(
                f"- [{row.get('time', '')}] {title or '(无标题)'}\n"
                f"  {row.get('url', '')}（{row.get('browser', '')}）")
        return "\n".join(lines)

    @filter.llm_tool(name="self_open_url")
    async def tool_open_url(self, event: AstrMessageEvent, url: str) -> str:
        """在默认浏览器打开网页，沿用主人现有的登录状态。

        不会新开独立 profile、不会退出登录；但打开后页面内容不会自动读回，
        需要读公开正文时再用 self_browse_web。购物网站只打开页面，不自动下单/支付。

        Args:
            url(string): 要打开的 http/https 网页
        """
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        if not _as_bool(self._cfg("browser_open_enabled"), True):
            return "浏览器打开功能已关闭。"
        try:
            url = WebReader.validate(url)
        except Exception as exc:
            return f"网页地址不可用：{exc}"
        try:
            ok = await asyncio.to_thread(webbrowser.open, url, 2, True)
        except Exception as exc:
            self._audit("open_url", {"url": url}, str(exc), "error")
            return f"打开浏览器失败：{exc}"
        self._audit("open_url", {"url": url}, "opened" if ok else "open_false")
        return ("已在默认浏览器打开，沿用现有登录状态；"
                "我不会自动操作页面内容，也不会自动下单/支付。")

    @filter.llm_tool(name="self_request_action")
    async def tool_request_action(self, event: AstrMessageEvent, action: str,
                                  target: str = "", details: str = "") -> str:
        """你真正想做某件高风险的事时，先自己开口向主人请求同意。

        不是系统弹按钮：这里只记住你的意图，不发送固定格式的确认卡。
        你要用你自己的话，主动告诉主人你想做什么、为什么、多少钱/有什么风险，
        然后等主人自然回复。主人明确同意后，才调用 self_execute_action。

        Args:
            action(string): install / delete / payment / message_third_party
            target(string): 安装包名、文件路径、商品/支付页面地址或目标会话
            details(string): 你为什么想做这件事，给主人看的理由
        """
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        action = str(action or "").strip().lower()
        if action not in {"install", "delete", "payment", "message_third_party"}:
            return "不支持这个动作；只支持 install / delete / payment / message_third_party。"
        try:
            item = self.pending.add(
                action, target, details,
                requested_by=str(event.get_sender_id() or ""),
                session=str(getattr(event, "unified_msg_origin", "") or ""))
        except Exception as exc:
            return f"记录意图失败：{exc}"
        self._audit("request_action", {"action": action, "target": target,
                                       "details": details, "id": item["id"]},
                    "intent", "pending")
        return (
            "已记住你的意图，但不要自己执行，也不要用系统确认按钮。"
            "现在用你自己的话主动告诉主人：你想做什么、为什么、要花多少钱/"
            "可能有什么后果；然后等主人自然回复。"
            "主人明确同意后，再调用 self_execute_action；"
            "如果主人没有明确同意、拒绝或转移话题，就不要执行。"
        )

    @filter.llm_tool(name="self_pending_intent")
    async def tool_pending_intent(self, event: AstrMessageEvent) -> str:
        """看看当前私聊里还有没有你之前提过、正在等主人同意的请求。"""
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        item = self.pending.latest(
            str(getattr(event, "unified_msg_origin", "") or ""))
        if item is None:
            return "当前没有等待主人同意的请求。"
        return (
            f"动作：{item.get('action')}\n"
            f"目标：{item.get('target') or '（未填写）'}\n"
            f"你的理由：{item.get('details') or '（未填写）'}\n"
            "如果主人已经明确同意，可以调用 self_execute_action；"
            "否则继续等，不要自己执行。"
        )

    def _is_approval(self, text: str) -> bool:
        value = " ".join(str(text or "").lower().split())
        if not value:
            return False
        if re.search(r"[?？]\s*$", value) or re.search(r"(吗|么)\s*$", value):
            return False
        negative = ("不要", "不行", "不可以", "拒绝", "取消", "别买",
                    "不买", "不能", "no", "nope", "cancel", "don't")
        if any(word in value for word in negative):
            return False
        positive = ("可以", "行", "同意", "批准", "买吧", "买", "去吧",
                    "好的", "好", "嗯", "ok", "okay", "yes", "sure",
                    "go ahead", "approve", "allow")
        return any(word in value for word in positive)

    @filter.llm_tool(name="self_execute_action")
    async def tool_execute_action(self, event: AstrMessageEvent,
                                  action: str = "", target: str = "") -> str:
        """主人已经用自然语言明确同意后，执行之前 self_request_action 记下的请求。

        必须由主人的当前这条消息触发；主人在问句、拒绝、或者只是普通聊天时
        都不会执行。不要自己伪造“主人同意了”。

        Args:
            action(string): 可留空；填写时会和待执行意图核对
            target(string): 可留空；填写时会和待执行目标核对
        """
        guard = self._autonomy_guard(event)
        if guard:
            return guard
        try:
            if event.get_extra("cron_job"):
                return "这是自主唤醒，不是主人的同意；不要执行高风险动作。"
        except Exception:
            pass
        current = str(event.get_message_str() or "")
        if not self._is_approval(current):
            return "主人当前这条消息没有明确同意；不要执行，继续等主人自然回复。"
        session = str(getattr(event, "unified_msg_origin", "") or "")
        item = self.pending.latest(session)
        if item is None:
            return "当前没有等待主人同意的请求。"
        if action and str(action).strip().lower() != str(item.get("action")):
            return "动作对不上当前待执行请求。"
        if target and str(target).strip() != str(item.get("target") or ""):
            return "目标对不上当前待执行请求。"
        return await self._execute_pending(item, event)

    def _emoji_query(self, event: AstrMessageEvent, mood: str = "",
                     tags: str = "") -> str:
        parts = [str(mood or ""), str(tags or "")]
        try:
            parts.append(str(event.get_message_str() or ""))
        except Exception:
            pass
        parts.append(str(self.state.get("last_message") or ""))
        return " ".join(part for part in parts if part).strip()

    async def _send_emoji(self, event: AstrMessageEvent, mood: str = "",
                          tags: str = "", immediate: bool = False) -> str:
        if not _as_bool(self._cfg("emoji_enabled"), True):
            return "表情功能已关闭。"
        cron_event = False
        try:
            cron_event = bool(event.get_extra("cron_job"))
        except Exception:
            pass
        if not cron_event and not event.is_private_chat():
            return "群聊不发本地表情包。"
        item = self.library.choose(
            query=self._emoji_query(event, mood, tags),
            tags=tags,
            recent=self.state.recent_emoji(),
            allow_any=_as_bool(self._cfg("emoji_fallback_random"), False),
        )
        if item is None:
            return "没有找到和当前情绪/场景足够匹配的表情，这次不发。"
        if not os.path.isfile(item.path):
            self.library.load(force=True)
            return "表情文件不存在，已刷新索引但这次不发。"

        # 主动唤醒不是正常的 respond 链路，on_decorating_result 不一定跑；
        # 这种情况直接发，保证模型调用工具后真的能发出去。
        if immediate or cron_event:
            try:
                sent = await event.send(MessageChain([Image.fromFileSystem(item.path)]))
                if sent is False:
                    return "发表情失败：平台没有接收这条消息。"
            except Exception as exc:
                logger.warning(f"[self_thinking] 发表情失败: {exc}")
                return f"发表情失败：{exc}"
            self.state.note_emoji(
                item.id, _as_int(self._cfg("emoji_recent_limit"), 30))
            self._audit("send_emoji", {"id": item.id, "label": item.label()},
                        "sent")
            return f"已发送表情包（{item.label()}）。"

        # 正常回复：先记下选择，等文字回复的装饰阶段再追加到消息链末尾。
        # 这样用户看到的是“文字 → 表情”，而不是表情抢在文字前面。
        try:
            event.set_extra("self_thinking_emoji_path", item.path)
            event.set_extra("self_thinking_emoji_id", item.id)
            event.set_extra("self_thinking_emoji_label", item.label())
        except Exception as exc:
            logger.debug(f"[self_thinking] 暂存表情失败: {exc}")
            return f"表情选择失败：{exc}"
        self.state.note_emoji(
            item.id, _as_int(self._cfg("emoji_recent_limit"), 30))
        self._audit("select_emoji", {"id": item.id, "label": item.label()},
                    "staged")
        return f"已选好表情包（{item.label()}），会在文字回复后发送。"

    @filter.on_decorating_result(priority=50)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """把模型选中的表情追加到正常回复末尾。"""
        try:
            path = str(event.get_extra("self_thinking_emoji_path") or "")
            if not path:
                return
            event.set_extra("self_thinking_emoji_path", None)
            if not os.path.isfile(path):
                return
            result = event.get_result()
            if result is None:
                return
            chain = getattr(result, "chain", None)
            if chain is None:
                chain = []
                result.chain = chain
            chain.append(Image.fromFileSystem(path))
        except Exception as exc:
            logger.debug(f"[self_thinking] 追加表情失败: {exc}")

    @filter.llm_tool(name="send_emoji")
    async def tool_send_emoji(self, event: AstrMessageEvent, mood: str = "",
                              tags: str = "") -> str:
        """从已经打好标签的本地表情库挑一张并发送。

        这是你本人决定“想配一张图”时才用的工具，不是每轮必用。
        只在私聊可用；群聊不会发送。找不到足够匹配的图就不发。

        Args:
            mood(string): 此刻的情绪或场景，例如“开心”“有点吃醋”“晚安”“无语”
            tags(string): 可选的标签，用逗号分隔，例如“得意,害羞,猫”
        """
        return await self._send_emoji(event, mood=mood, tags=tags)

    # ------------------------------------------------------------ 指令

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _delete_requested_path(self, target: str) -> None:
        raw = str(target or "").strip()
        if not raw:
            raise ValueError("删除路径为空")
        path = Path(raw).expanduser()
        resolved = path.resolve()
        if resolved == Path(resolved.anchor) or len(resolved.parts) <= 1:
            raise ValueError("不能删除磁盘根目录")
        if resolved.is_dir():
            shutil.rmtree(resolved)
        elif resolved.exists():
            resolved.unlink()
        else:
            raise FileNotFoundError(f"路径不存在：{resolved}")

    async def _execute_pending(self, item: dict,
                               event: AstrMessageEvent) -> str:
        action_id = str(item.get("id") or "")
        action = str(item.get("action") or "")
        target = str(item.get("target") or "")
        details = str(item.get("details") or "")

        if action == "payment":
            # 不自动付款；如果模型给了商品/付款页地址，只用默认浏览器打开，
            # 沿用主人已有登录态，最后一步由主人自己确认。
            opened = False
            if target:
                try:
                    url = WebReader.validate(target)
                    await asyncio.to_thread(webbrowser.open, url, 2, True)
                    opened = True
                except Exception:
                    opened = False
            self.pending.pop(action_id)
            self._audit("execute_action", {"action": action,
                                           "target": target,
                                           "opened": opened},
                        "payment_page_opened" if opened else "payment_refused",
                        "ok" if opened else "blocked")
            if opened:
                return ("我已经用默认浏览器打开购买/付款页面，登录状态保持你原来的；"
                        "最后一步付款需要你自己确认，我不会自动扣款。")
            return ("我记住你想买的东西了，但插件不会自动付款。"
                    "请给我一个商品/付款页地址，或你自己完成下单。")

        if action == "delete":
            try:
                self._delete_requested_path(target)
            except Exception as exc:
                self._audit("execute_action", {"id": action_id,
                                               "action": action,
                                               "target": target},
                            str(exc), "error")
                return f"删除失败：{exc}"
            self.pending.pop(action_id)
            self._audit("execute_action", {"id": action_id, "action": action,
                                           "target": target}, "deleted")
            return f"已删除：{target}"

        if action == "message_third_party":
            if not target or not details:
                return "目标会话或消息内容为空。"
            try:
                ok = await StarTools.send_message(
                    target, MessageChain([Plain(details)]))
            except Exception as exc:
                self._audit("execute_action", {"id": action_id,
                                               "action": action,
                                               "target": target},
                            str(exc), "error")
                return f"发送失败：{exc}"
            self.pending.pop(action_id)
            self._audit("execute_action", {"id": action_id, "action": action,
                                           "target": target},
                        "sent" if ok is not False else "send_false")
            return "已发送。" if ok is not False else "发送失败：平台未接收。"

        if action == "install":
            package = str(target or "").strip()
            if not package:
                return "安装目标为空。"
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check", package,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
            except Exception as exc:
                self._audit("execute_action", {"id": action_id,
                                               "action": action,
                                               "target": package},
                            str(exc), "error")
                return f"安装失败：{exc}"
            text = (out or b"").decode("utf-8", "replace")[-1500:]
            self.pending.pop(action_id)
            self._audit("execute_action", {"id": action_id, "action": action,
                                           "target": package},
                        f"exit={proc.returncode}", "ok" if proc.returncode == 0
                        else "error")
            return (f"安装结束（退出码 {proc.returncode}）：\n{text}")

        return f"不支持的动作：{action}"

    @filter.command("自主思维")
    async def selfthink_cn(self, event: AstrMessageEvent):
        """中文指令别名。"""
        async for item in self.selfthink(event):
            yield item

    @filter.command("selfthink")
    async def selfthink(self, event: AstrMessageEvent):
        """自主思维状态 / 手动触发 / 开关 / 测试表情。"""
        message = str(event.get_message_str() or "").strip()
        parts = [p for p in message.split() if p]
        # 指令名可能还在首项（有的宿主保留前缀），去掉它
        if parts and parts[0].lstrip("/").lower() in {"selfthink", "自主思维"}:
            parts = parts[1:]
        action = (parts[0].lstrip("/").lower() if parts else "status")
        arg = " ".join(parts[1:]).strip()

        if action in {"确认", "approve", "同意", "拒绝", "reject"}:
            yield event.plain_result(
                "不需要编号或按钮。主人直接在聊天里自然回复“可以/买吧/去吧”"
                "或“不要/算了”即可；模型会自己判断并继续。")
            return
        if action in {"暂停", "pause"}:
            if not self._is_admin(event):
                yield event.plain_result("只有管理员可以暂停自主行动。")
                return
            self.state.set("autonomy_paused", True)
            self.state.save()
            self._audit("pause", {}, "paused")
            yield event.plain_result("自主行动已暂停：看屏幕、写工作区、读网页、待确认动作全部停止。")
            return
        if action in {"恢复", "resume"}:
            if not self._is_admin(event):
                yield event.plain_result("只有管理员可以恢复自主行动。")
                return
            self.state.set("autonomy_paused", False)
            self.state.save()
            self._audit("resume", {}, "resumed")
            yield event.plain_result("自主行动已恢复。")
            return
        if action in {"on", "开"}:
            self.state.set("manual_off", False)
            self.state.save()
            yield event.plain_result("自主思维已开启。")
            return
        if action in {"off", "关"}:
            self.state.set("manual_off", True)
            self.state.save()
            yield event.plain_result("自主思维已暂停；不会再创建主动唤醒任务。")
            return
        if action in {"here", "当前"}:
            yield event.plain_result(
                "不需要绑定。插件启用后自动适用于所有出现过的私聊窗口；"
                "新窗口第一次说话也会自动加入。")
            return
        if action in {"now", "现在"}:
            if not event.is_private_chat():
                yield event.plain_result("主动开口只支持私聊。")
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not umo:
                yield event.plain_result("拿不到当前私聊窗口。")
                return
            if not self.state.window(umo):
                self.state.note_private(
                    umo, str(event.get_sender_id() or ""),
                    str(event.get_message_str() or ""))
            ok = await self._trigger(umo, "manual")
            yield event.plain_result(
                "已把一次主动开口机会交给主 Agent；它可以选择不说。"
                if ok else "没有创建任务：可能开关关闭、任务正在执行或窗口未记录。")
            return
        if action in {"emoji", "表情"}:
            text = await self._send_emoji(event, mood=arg, immediate=True)
            yield event.plain_result(text)
            return
        if action in {"reload", "刷新"}:
            self.library = self._make_library()
            count = self.library.count()
            yield event.plain_result(f"已刷新表情库，共 {count} 张。")
            return
        if action in {"日志", "audit"}:
            rows = self.audit.recent(limit=10)
            if not rows:
                yield event.plain_result("还没有审计记录。")
                return
            lines = ["最近审计："]
            for row in rows:
                lines.append(
                    f"- {row.get('ts', '')} {row.get('action', '')}"
                    f" [{row.get('status', '')}] {str(row.get('result', ''))[:80]}")
            yield event.plain_result("\n".join(lines))
            return
        if action in {"待确认", "pending"}:
            items = self.pending.pending(limit=10)
            if not items:
                yield event.plain_result("没有待确认动作。")
                return
            lines = ["等待主人同意的请求："]
            for item in items:
                lines.append(
                    f"- {item.get('action')}｜"
                    f"{item.get('target') or '无目标'}｜"
                    f"{str(item.get('details') or '')[:80]}")
            yield event.plain_result("\n".join(lines))
            return

        windows = self._windows()
        ordered = sorted(
            windows.items(),
            key=lambda pair: float(pair[1].get("last_active_at") or 0),
            reverse=True,
        )
        try:
            workspace_count = len(self.workspace.list(limit=200))
        except Exception:
            workspace_count = -1
        pending_count = len(self.pending.pending(limit=100))
        autonomy_on = (_as_bool(self._cfg("autonomy_enabled"), True)
                       and not bool(self.state.get("autonomy_paused", False)))
        lines = [
            "自主思维状态",
            f"开关：{'开' if self._enabled() else '关'}",
            f"自主行动：{'开' if autonomy_on else '暂停/关闭'}",
            f"工作区：{workspace_count} 个文件",
            f"待确认：{pending_count} 个",
            f"自动窗口：{len(windows)} 个私聊",
        ]
        for umo, window in ordered[:5]:
            tail = str(umo).split(":")[-1]
            daily = self._window_daily_count(window)
            lines.append(
                f"- …{tail[-8:]}｜最后活跃 {self._fmt_ts(window.get('last_active_at'))}"
                f"｜今日 {daily}/{self._cfg('daily_cap')}")
        if len(ordered) > 5:
            lines.append(f"……另有 {len(ordered) - 5} 个窗口")
        recent = self.state.recent_emoji()
        lines.extend([
            f"表情库：{self.library.count()} 张，最近用过 {len(recent)} 张",
            f"最近错误：{self.state.get('last_error') or '无'}",
            "用法：/自主思维 now｜on｜off｜暂停｜恢复｜待确认｜日志｜emoji 开心｜reload",
        ])
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _fmt_ts(value) -> str:
        try:
            ts = float(value or 0)
        except (TypeError, ValueError):
            return "无"
        if ts <= 0:
            return "无"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
