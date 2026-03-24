# -*- coding: utf-8 -*-
"""
企业微信富文本消息模块

支持的消息类型：
1. Markdown 消息 - 支持粗体、链接、引用等格式
2. 文本卡片消息 (textcard) - 带标题、描述和"查看详情"按钮
3. 模板卡片消息 (template_card) - 支持多按钮交互

使用示例：
    from wework_rich_message import (
        send_markdown, send_textcard, send_button_card
    )
    
    # Markdown
    send_markdown(user_id, "**标题**\n> 引用内容\n[链接](https://example.com)")
    
    # 文本卡片
    send_textcard(user_id, "标题", "描述内容", "https://example.com")
    
    # 按钮卡片
    send_button_card(user_id, "选择操作", "请选择一个选项", [
        {"key": "btn1", "name": "选项一"},
        {"key": "btn2", "name": "选项二"},
    ])
"""

import sys
import requests
from datetime import datetime, timezone, timedelta

_BEIJING_TZ = timezone(timedelta(hours=8))


def _log(msg):
    ts = datetime.now(_BEIJING_TZ).strftime("%H:%M:%S")
    print(f"{ts} [WeworkRich] {msg}", file=sys.stderr, flush=True)


# ============ Token 获取（复用 app.py 的逻辑） ============

def _get_access_token():
    """获取企业微信 access_token"""
    try:
        from app import get_wework_access_token
        return get_wework_access_token()
    except ImportError:
        _log("无法导入 get_wework_access_token")
        return None


def _get_agent_id():
    """获取 Agent ID"""
    try:
        from config import AGENT_ID
        return AGENT_ID
    except ImportError:
        import os
        return os.getenv("AGENT_ID")


# ============ Markdown 消息 ============

def send_markdown(user_id: str, content: str) -> bool:
    """
    发送 Markdown 格式消息
    
    支持的格式：
    - 标题：# 标题  或  ## 二级标题
    - 粗体：**粗体**
    - 链接：[文字](URL)
    - 引用：> 引用内容
    - 代码：`代码`
    - 换行：\n
    - 字体颜色：<font color="info">绿色</font>
                <font color="warning">橙色</font>
                <font color="comment">灰色</font>
    
    Args:
        user_id: 用户 ID
        content: Markdown 内容
        
    Returns:
        bool: 是否发送成功
    """
    token = _get_access_token()
    if not token:
        _log("获取 access_token 失败")
        return False
    
    agent_id = _get_agent_id()
    
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    data = {
        "touser": user_id,
        "msgtype": "markdown",
        "agentid": agent_id,
        "markdown": {
            "content": content
        }
    }
    
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        ok = result.get("errcode") == 0
        if not ok:
            _log(f"Markdown消息发送失败: {result}")
        return ok
    except Exception as e:
        _log(f"Markdown消息发送异常: {e}")
        return False


# ============ 文本卡片消息 ============

def send_textcard(user_id: str, title: str, description: str, 
                  url: str, btntxt: str = "详情") -> bool:
    """
    发送文本卡片消息
    
    显示为带有标题、描述和底部按钮的卡片样式。
    
    Args:
        user_id: 用户 ID
        title: 卡片标题（最长128字节，超出自动截断）
        description: 卡片描述（最长512字节，超出自动截断）
        url: 点击"详情"跳转的链接
        btntxt: 按钮文字，默认"详情"（最长4个字）
        
    Returns:
        bool: 是否发送成功
    """
    token = _get_access_token()
    if not token:
        _log("获取 access_token 失败")
        return False
    
    agent_id = _get_agent_id()
    
    api_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    data = {
        "touser": user_id,
        "msgtype": "textcard",
        "agentid": agent_id,
        "textcard": {
            "title": title[:42],  # 约128字节
            "description": description[:170],  # 约512字节
            "url": url,
            "btntxt": btntxt[:4]
        }
    }
    
    try:
        resp = requests.post(api_url, json=data, timeout=10)
        result = resp.json()
        ok = result.get("errcode") == 0
        if not ok:
            _log(f"文本卡片发送失败: {result}")
        return ok
    except Exception as e:
        _log(f"文本卡片发送异常: {e}")
        return False


# ============ 模板卡片消息（按钮交互型） ============

def send_button_card(user_id: str, main_title: str, sub_title: str,
                     buttons: list, source: str = "TextAgent") -> dict:
    """
    发送按钮交互型模板卡片
    
    用户点击按钮后，会回调到服务器，可以获取用户选择。
    
    Args:
        user_id: 用户 ID
        main_title: 主标题
        sub_title: 副标题/描述
        buttons: 按钮列表，格式：[{"key": "btn_key", "name": "按钮名称"}, ...]
                 最多6个按钮
        source: 来源标识，显示在卡片顶部
        
    Returns:
        dict: {"ok": bool, "response_code": str} 
              response_code 用于后续更新卡片
    """
    token = _get_access_token()
    if not token:
        _log("获取 access_token 失败")
        return {"ok": False, "response_code": None}
    
    agent_id = _get_agent_id()
    
    # 构建按钮列表
    button_list = []
    for i, btn in enumerate(buttons[:6]):  # 最多6个
        button_list.append({
            "type": 1,  # 1=普通按钮
            "text": btn.get("name", f"按钮{i+1}"),
            "key": btn.get("key", f"btn_{i}")
        })
    
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    data = {
        "touser": user_id,
        "msgtype": "template_card",
        "agentid": agent_id,
        "template_card": {
            "card_type": "button_interaction",
            "source": {
                "desc": source
            },
            "main_title": {
                "title": main_title
            },
            "sub_title_text": sub_title,
            "button_list": button_list
        }
    }
    
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        ok = result.get("errcode") == 0
        response_code = result.get("response_code")
        if not ok:
            _log(f"按钮卡片发送失败: {result}")
        return {"ok": ok, "response_code": response_code}
    except Exception as e:
        _log(f"按钮卡片发送异常: {e}")
        return {"ok": False, "response_code": None}


def send_vote_card(user_id: str, main_title: str, 
                   options: list, source: str = "TextAgent") -> dict:
    """
    发送投票选择型模板卡片（单选）
    
    Args:
        user_id: 用户 ID
        main_title: 投票标题
        options: 选项列表，格式：[{"id": "opt_1", "text": "选项一"}, ...]
        source: 来源标识
        
    Returns:
        dict: {"ok": bool, "response_code": str}
    """
    token = _get_access_token()
    if not token:
        return {"ok": False, "response_code": None}
    
    agent_id = _get_agent_id()
    
    # 构建选项列表
    option_list = []
    for i, opt in enumerate(options[:20]):  # 最多20个选项
        option_list.append({
            "id": opt.get("id", f"opt_{i}"),
            "text": opt.get("text", f"选项{i+1}")
        })
    
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    data = {
        "touser": user_id,
        "msgtype": "template_card",
        "agentid": agent_id,
        "template_card": {
            "card_type": "vote_interaction",
            "source": {
                "desc": source
            },
            "main_title": {
                "title": main_title
            },
            "checkbox": {
                "question_key": "vote_question",
                "mode": 0,  # 0=单选，1=多选
                "option_list": option_list
            },
            "submit_button": {
                "text": "提交",
                "key": "vote_submit"
            }
        }
    }
    
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        ok = result.get("errcode") == 0
        if not ok:
            _log(f"投票卡片发送失败: {result}")
        return {"ok": ok, "response_code": result.get("response_code")}
    except Exception as e:
        _log(f"投票卡片发送异常: {e}")
        return {"ok": False, "response_code": None}


# ============ 更新模板卡片 ============

def update_card(response_code: str, user_id: str, 
                replace_text: str = "已处理") -> bool:
    """
    更新已发送的模板卡片（替换为文本）
    
    用户点击按钮后，可以调用此方法将卡片替换为确认文本。
    
    Args:
        response_code: 发送卡片时返回的 response_code
        user_id: 用户 ID
        replace_text: 替换后显示的文本
        
    Returns:
        bool: 是否更新成功
    """
    if not response_code:
        _log("response_code 为空，无法更新卡片")
        return False
    
    token = _get_access_token()
    if not token:
        return False
    
    agent_id = _get_agent_id()
    
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/update_template_card?access_token={token}"
    data = {
        "userids": [user_id],
        "agentid": agent_id,
        "response_code": response_code,
        "button": {
            "replace_name": replace_text
        }
    }
    
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        ok = result.get("errcode") == 0
        if not ok:
            _log(f"更新卡片失败: {result}")
        return ok
    except Exception as e:
        _log(f"更新卡片异常: {e}")
        return False


# ============ 便捷发送函数 ============

def send_link(user_id: str, text: str, url: str, link_text: str = "点击查看") -> bool:
    """
    发送带链接的消息（Markdown 格式）
    
    Args:
        user_id: 用户 ID
        text: 消息正文
        url: 链接地址
        link_text: 链接显示文字
        
    Returns:
        bool: 是否发送成功
    """
    content = f"{text}\n\n[{link_text}]({url})"
    return send_markdown(user_id, content)


def send_action_card(user_id: str, title: str, content: str,
                     actions: list) -> dict:
    """
    发送操作卡片（带多个跳转链接按钮）
    
    注意：这里使用的是文本通知型模板卡片 + action_menu
    
    Args:
        user_id: 用户 ID
        title: 卡片标题
        content: 卡片内容
        actions: 操作列表，格式：[{"text": "按钮文字", "url": "跳转链接"}, ...]
        
    Returns:
        dict: {"ok": bool, "response_code": str}
    """
    token = _get_access_token()
    if not token:
        return {"ok": False, "response_code": None}
    
    agent_id = _get_agent_id()
    
    # 构建 action_list
    action_list = []
    for i, action in enumerate(actions[:3]):  # 最多3个
        action_list.append({
            "text": action.get("text", f"操作{i+1}"),
            "key": f"action_{i}"
        })
    
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    data = {
        "touser": user_id,
        "msgtype": "template_card",
        "agentid": agent_id,
        "template_card": {
            "card_type": "text_notice",
            "source": {
                "desc": "TextAgent"
            },
            "main_title": {
                "title": title
            },
            "sub_title_text": content,
            "card_action": {
                "type": 1,
                "url": actions[0].get("url", "") if actions else ""
            },
            "button_list": [
                {
                    "text": action.get("text", "查看"),
                    "type": 1,
                    "key": f"action_{i}"
                }
                for i, action in enumerate(actions[:2])
            ] if actions else []
        }
    }
    
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        ok = result.get("errcode") == 0
        if not ok:
            _log(f"操作卡片发送失败: {result}")
        return {"ok": ok, "response_code": result.get("response_code")}
    except Exception as e:
        _log(f"操作卡片发送异常: {e}")
        return {"ok": False, "response_code": None}


# ============ 智能发送（自动选择最佳格式） ============

def smart_send(user_id: str, content: str, 
               url: str = None, buttons: list = None) -> bool:
    """
    智能发送消息 - 根据参数自动选择最佳消息格式
    
    Args:
        user_id: 用户 ID
        content: 消息内容
        url: 可选，如果提供则发送文本卡片
        buttons: 可选，如果提供则发送按钮卡片
        
    Returns:
        bool: 是否发送成功
    """
    # 有按钮 → 按钮卡片
    if buttons:
        result = send_button_card(user_id, "请选择", content, buttons)
        return result.get("ok", False)
    
    # 有链接 → 文本卡片
    if url:
        return send_textcard(user_id, "查看详情", content, url)
    
    # 检查内容是否包含 Markdown 格式
    md_markers = ["**", "[", "](", ">", "#", "<font", "`"]
    has_markdown = any(marker in content for marker in md_markers)
    
    if has_markdown:
        return send_markdown(user_id, content)
    
    # 默认发送纯文本（使用原有方法）
    try:
        from app import send_wework_message
        return send_wework_message(user_id, content)
    except ImportError:
        # 回退到 Markdown
        return send_markdown(user_id, content)
