"""高风险动作的边界：主人意图判定、安装包名校验、删除走回收站。

为什么单独一个模块：这三件事都"错一次就出事"，必须能被单元测试覆盖；
放在 main.py 里要拖 AstrBot 运行时，测试跑不动。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

# ---------------------------------------------------------------- 同意判定

_QUESTION_MARKS = ("?", "？", "吗", "么", "呢")

#: 明确拒绝
_NEGATIVE = (
    "不要", "不用", "不必", "不需要", "不行", "不可以", "不能", "不好",
    "不想", "不买", "别买", "别了", "先别", "算了", "拒绝", "取消",
    "没空", "改天", "以后再说", "再说", "回头", "等下", "等等",
    "个鬼", "才怪", "你个头",
    "no", "nope", "cancel", "don't", "dont", "not now", "stop",
)

#: 假设/条件句一律不当成同意
_HYPOTHETICAL = ("如果", "要是", "假如", "万一", "假设", "若是", "if ")

#: 正面词。刻意不加裸"去""买"：'我去洗澡了' 不该被当成同意。
_POSITIVE = (
    "可以", "行", "同意", "批准", "买吧", "去吧", "好的", "好", "嗯",
    "没问题", "准了", "可以买", "就这个", "就它",
    "ok", "okay", "yes", "sure", "go ahead", "approve", "allow",
)

#: 正面词前面几个字里出现这些字，就当否定（"不好""别买了""没同意"）
_NEG_CHARS = "不没别甭勿莫"


def is_approval(text: str) -> bool:
    """当前这条主人消息是不是"明确同意"。

    顺序：空 → 反问 → 假设 → 否定 → 正面词（带否定前缀窗口）。
    **模糊一律不算同意**：宁可多问一句，也不要误执行安装/删除/付款。
    """
    value = " ".join(str(text or "").lower().split())
    if not value:
        return False

    # 问句：'可以吗''你确定吗''能买吗？'
    if any(mark in value for mark in ("?", "？", "吗", "么", "呢")):
        return False

    # 假设/条件句：'如果便宜就买'
    for word in _HYPOTHETICAL:
        if value.startswith(word):
            return False

    # 否定优先：'可以，但是先别买' 里同时有正面词和否定词
    for word in _NEGATIVE:
        if word in value:
            return False

    # 正面词：前面 3 个字里带否定字就不算（'不好''没同意''别了吧'）
    for word in _POSITIVE:
        start = 0
        while True:
            index = value.find(word, start)
            if index < 0:
                break
            window = value[max(0, index - 3):index]
            if not any(char in window for char in _NEG_CHARS):
                return True
            start = index + 1
    return False


# ---------------------------------------------------------------- 安装包名

#: 只接受 PyPI 风格的包名，可带版本约束；拒绝任何以 - 开头的东西
_PACKAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,80}"
    r"(?:\s*(?:==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9._+*]+)?$"
)


def validate_package(name: str) -> str:
    """校验 pip 安装目标，返回去空白后的包名。

    `pip install <用户/模型给的字符串>` 会把 `--index-url http://…` 这种当成参数，
    等于让模型能改安装源（供应链投毒）。这里只放行包名 + 版本约束，
    调用方再加 `--` 分隔，双保险。
    """
    value = " ".join(str(name or "").split())
    if not value:
        raise ValueError("安装目标为空")
    if value.startswith("-"):
        raise ValueError("安装目标不能以 - 开头")
    if not _PACKAGE_RE.match(value):
        raise ValueError(f"包名不合法：{value}")
    return value


# ---------------------------------------------------------------- 删除


def _protected(path: Path) -> bool:
    """系统目录 / 太浅的路径一律拒绝删除。

    只挡"删了会出事"的位置：盘符根、Windows、Program Files、ProgramData、
    用户主目录本身。**不**拦用户目录下的东西（桌面、文档、下载都能正常删），
    否则就没法用了。
    """
    if len(path.parts) <= 2:                      # C:\ 或 C:\Windows
        return True
    if path == Path(path.anchor):
        return True
    roots: list[Path] = []
    for name in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)",
                 "ProgramData"):
        value = os.environ.get(name)
        if value:
            try:
                roots.append(Path(value).resolve())
            except OSError:
                continue
    try:
        roots.append(Path.home().resolve())
    except OSError:
        pass

    for root in roots:
        if path == root:
            return True
        try:
            path.relative_to(root)
        except ValueError:
            continue
        # 在 Windows / Program Files / ProgramData 里面：整棵都挡
        if root.name.lower() in ("windows", "system32", "program files",
                                 "program files (x86)", "programdata"):
            return True
    return False


def trash_target(data_dir: str, target: str, stamp: str | None = None) -> str:
    """把要删的东西移到插件数据目录下的 `trash/<时间>/`，返回新路径。

    为什么不是直接删：模型把路径听错一个字母就没了。移到回收目录后
    跟主人说一句就能拿回来；要真删，等清理的时候由人来做。
    """
    raw = str(target or "").strip()
    if not raw:
        raise ValueError("删除路径为空")
    path = Path(raw).expanduser().resolve()
    if path == Path(path.anchor) or len(path.parts) <= 1:
        raise ValueError("不能删除磁盘根目录")
    if _protected(path):
        raise ValueError("拒绝删除系统目录")
    if not path.exists():
        raise FileNotFoundError(f"路径不存在：{path}")

    moment = stamp or time.strftime("%Y%m%d-%H%M%S")
    folder = Path(data_dir) / "trash" / moment
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / path.name
    index = 1
    while dest.exists():
        dest = folder / f"{path.name}-{index}"
        index += 1
    try:
        os.replace(path, dest)              # 同盘：瞬间完成
    except OSError:
        shutil.move(str(path), str(dest))   # 跨盘：复制后删
    return str(dest)
