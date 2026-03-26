# -*- coding: utf-8 -*-
"""
时间工具函数 — V9 调度精确性改造

集中管理 HH:MM 格式的时间解析、运算，消除 app.py 中散落的 split(":") 和
硬编码时间计算，降低 bug 风险。

设计原则：纯函数，不引入 class，保持简洁。
"""

# 一天总分钟数
_DAY_MINUTES = 24 * 60  # 1440


def parse_hhmm(time_str: str) -> int:
    """将 'HH:MM' 格式字符串解析为当天分钟数 (0~1439)。

    Args:
        time_str: 格式 "HH:MM"，如 "08:30"

    Returns:
        分钟数（int），如 510

    Raises:
        ValueError: 格式不合法时
    """
    if not time_str or not isinstance(time_str, str):
        raise ValueError(f"无效的时间字符串: {time_str!r}")
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式错误（需 HH:MM）: {time_str!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"时间超出范围: {time_str!r} (h={h}, m={m})")
    return h * 60 + m


def format_minutes(total_min: int) -> str:
    """将分钟数 (0~1439) 格式化为 'HH:MM' 字符串。

    超出 [0, 1439] 范围时自动 clamp。

    Args:
        total_min: 当天的分钟数

    Returns:
        格式化字符串，如 "08:30"
    """
    total_min = max(0, min(total_min, _DAY_MINUTES - 1))
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def add_minutes(time_str: str, delta: int) -> str:
    """给 'HH:MM' 时间加/减分钟数，返回 'HH:MM'。

    结果 clamp 到 [00:00, 23:59]。

    Args:
        time_str: 基准时间 "HH:MM"
        delta: 增减分钟数（正数为加，负数为减）

    Returns:
        计算后的 "HH:MM"
    """
    try:
        base = parse_hhmm(time_str)
        return format_minutes(base + delta)
    except (ValueError, TypeError):
        return time_str  # 降级：原值返回


def safe_parse_hhmm(time_str: str, default: int = 0) -> int:
    """安全解析 'HH:MM'，解析失败返回默认值。

    用于替换到处 try-except split(":") 的模式。

    Args:
        time_str: 时间字符串
        default: 解析失败时的默认值

    Returns:
        分钟数（int）
    """
    try:
        return parse_hhmm(time_str)
    except (ValueError, TypeError):
        return default


def minutes_between(time_a: str, time_b: str) -> int:
    """计算两个 HH:MM 时间之间的分钟差（b - a）。

    Args:
        time_a: 起始时间 "HH:MM"
        time_b: 结束时间 "HH:MM"

    Returns:
        分钟差（可为负数）
    """
    return safe_parse_hhmm(time_b) - safe_parse_hhmm(time_a)


def now_as_minutes(tz=None) -> int:
    """获取当前时间的分钟数表示。

    Args:
        tz: 时区（datetime.timezone），None 则用本地时间

    Returns:
        当前时刻的分钟数
    """
    from datetime import datetime
    now = datetime.now(tz)
    return now.hour * 60 + now.minute
