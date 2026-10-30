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
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools

from .core import memory_bridge
from .core.image_library import EmojiLibrary
from .core.state import StateStore

PLUGIN_NAME = "astrbot_plugin_self_thinking"
DISPLAY_NAME = "自主思维"
VERSION = "1.0.1"

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

    # ------------------------------------------------------------ 表情工具

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

        windows = self._windows()
        ordered = sorted(
            windows.items(),
            key=lambda pair: float(pair[1].get("last_active_at") or 0),
            reverse=True,
        )
        lines = [
            "自主思维状态",
            f"开关：{'开' if self._enabled() else '关'}",
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
            "用法：/自主思维 now｜on｜off｜emoji 开心｜reload",
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
