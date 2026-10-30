"""找 all_memory（全量记忆体）并取一份主动开口用的记忆摘要。

只走公开的插件注册表，不 import 对方模块；没装 / 没激活就静默降级，
不阻塞主动思维本身。
"""

from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger("self_thinking")

NAMES = {
    "astrbot_plugin_all_memory",
    "全量记忆体",
    "all_memory",
}
METHOD = "compose_proactive_context"


def _meta_matches(meta: Any) -> bool:
    if meta is None:
        return False
    values = set()
    for attr in ("name", "display_name", "root_dir_name", "module_path"):
        try:
            value = getattr(meta, attr, "")
        except Exception:
            value = ""
        if value:
            values.add(str(value))
    return bool(values & NAMES)


def find_all_memory(context: Any) -> Any:
    if context is None:
        return None
    candidates = []
    get_one = getattr(context, "get_registered_star", None)
    if callable(get_one):
        for name in NAMES:
            try:
                meta = get_one(name)
            except Exception:
                meta = None
            if meta is not None:
                candidates.append(meta)
    get_all = getattr(context, "get_all_stars", None)
    if callable(get_all):
        try:
            candidates.extend(list(get_all() or []))
        except Exception:
            pass
    seen = set()
    for meta in candidates:
        if meta is None or id(meta) in seen:
            continue
        seen.add(id(meta))
        if not _meta_matches(meta):
            continue
        if not bool(getattr(meta, "activated", True)):
            continue
        star = getattr(meta, "star_cls", None)
        if star is None:
            continue
        if callable(getattr(star, METHOD, None)):
            return star
    return None


async def compose(star: Any, session: str, user_id: str = "",
                  query: str = "", max_chars: int = 2400) -> str:
    if star is None:
        return ""
    fn = getattr(star, METHOD, None)
    if not callable(fn):
        return ""
    try:
        value = await fn(
            session=session,
            user_id=user_id,
            query=query,
            max_chars=max_chars,
        )
        return str(value or "").strip()
    except Exception as exc:
        logger.debug(f"[self_thinking] 记忆摘要不可用: {exc}")
        return ""
