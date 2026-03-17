# -*- coding: utf-8 -*-
"""
渠道路由器 — 根据用户所属渠道分发消息。

设计：
- 维护 {channel_name: send_function} 注册表
- send_message(user_id, text) 查用户 channel 自动路由
- 支持运行时注册新渠道
- 兼容老用户（默认 wework）
"""
import os
import sys
import json
from datetime import datetime, timezone, timedelta

_BEIJING_TZ = timezone(timedelta(hours=8))

def _log(msg):
    ts = datetime.now(_BEIJING_TZ).strftime("%H:%M:%S")
    print(f"{ts} [ChannelRouter] {msg}", file=sys.stderr, flush=True)


# ============ 渠道注册表 ============
_channels = {}   # {"wework": send_fn}


def register_channel(name, send_fn):
    """注册一个发送渠道"""
    _channels[name] = send_fn
    _log(f"渠道已注册: {name}")


# ============ 用户渠道缓存 ============
_user_channel_cache = {}   # {user_id: "wework"}


def get_user_channel(user_id):
    """获取用户所属渠道（带内存缓存）"""
    if user_id in _user_channel_cache:
        return _user_channel_cache[user_id]

    # 尝试读 user_config.json
    try:
        from user_context import DATA_DIR
        config_file = os.path.join(DATA_DIR, "users", user_id, "_Karvis", "user_config.json")
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            channel = config.get("channel", "wework")
            _user_channel_cache[user_id] = channel
            return channel
    except Exception:
        pass

    # 默认企微（兼容老用户）
    _user_channel_cache[user_id] = "wework"
    return "wework"


def set_user_channel(user_id, channel):
    """设置用户渠道（缓存 + 写入 config 由调用方负责）"""
    _user_channel_cache[user_id] = channel


def clear_user_channel_cache(user_id=None):
    """清除渠道缓存"""
    if user_id:
        _user_channel_cache.pop(user_id, None)
    else:
        _user_channel_cache.clear()


# ============ 统一发送 ============

def send_message(user_id, text, msg_type="text", **kwargs):
    """
    统一发送入口 — 根据用户 channel 自动路由
    
    Args:
        user_id: 用户 ID
        text: 消息内容
        msg_type: 消息类型 ("text", "markdown", "textcard", "button_card")
        **kwargs: 额外参数（如 url, buttons 等）
    """
    channel = get_user_channel(user_id)
    
    # 企业微信渠道支持富文本
    if channel == "wework" and msg_type != "text":
        try:
            from wework_rich_message import (
                send_markdown, send_textcard, send_button_card, smart_send
            )
            
            if msg_type == "markdown":
                return send_markdown(user_id, text)
            elif msg_type == "textcard":
                url = kwargs.get("url", "")
                btntxt = kwargs.get("btntxt", "详情")
                title = kwargs.get("title", "消息")
                return send_textcard(user_id, title, text, url, btntxt)
            elif msg_type == "button_card":
                buttons = kwargs.get("buttons", [])
                title = kwargs.get("title", "请选择")
                result = send_button_card(user_id, title, text, buttons)
                return result.get("ok", False)
            elif msg_type == "smart":
                url = kwargs.get("url")
                buttons = kwargs.get("buttons")
                return smart_send(user_id, text, url=url, buttons=buttons)
        except ImportError as e:
            _log(f"导入富文本模块失败: {e}，回退到纯文本")
    
    # 默认纯文本发送
    fn = _channels.get(channel)
    if fn:
        return fn(user_id, text)
    _log(f"未知渠道 {channel} for user {user_id}, 已注册渠道: {list(_channels.keys())}")
    return False


def send_rich_message(user_id, content, url=None, buttons=None, title=None):
    """
    发送富文本消息的便捷方法
    
    根据参数自动选择最佳消息类型：
    - 有 buttons → 按钮卡片
    - 有 url → 文本卡片
    - 有 Markdown 格式 → Markdown 消息
    - 其他 → 纯文本
    
    Args:
        user_id: 用户 ID
        content: 消息内容
        url: 可选，跳转链接
        buttons: 可选，按钮列表 [{"key": "...", "name": "..."}, ...]
        title: 可选，卡片标题
    """
    channel = get_user_channel(user_id)
    
    if channel == "wework":
        try:
            from wework_rich_message import smart_send, send_textcard, send_button_card
            
            if buttons:
                result = send_button_card(user_id, title or "请选择", content, buttons)
                return result.get("ok", False)
            elif url:
                return send_textcard(user_id, title or "查看详情", content, url)
            else:
                return smart_send(user_id, content)
        except ImportError:
            pass
    
    # 回退到纯文本
    return send_message(user_id, content)


def send_alert(text):
    """管理员告警 — 推送到所有活跃渠道的管理员"""
    results = []
    # 企微管理员
    from config import ADMIN_USER_ID
    if ADMIN_USER_ID and "wework" in _channels:
        try:
            ok = _channels["wework"](ADMIN_USER_ID, text)
            results.append(("wework", ok))
        except Exception as e:
            _log(f"企微告警发送失败: {e}")
            results.append(("wework", False))

    return results
