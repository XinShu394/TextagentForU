# -*- coding: utf-8 -*-
"""
TextAgent 消息网关
职责：接收企微消息 → 下载媒体/ASR → 构造 payload → 交给 brain.process()
不做任何业务判断，所有逻辑由大脑决定。
"""
# 加载 .env 文件（Lite 模式 / 本地开发）
import os
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass  # 未安装 python-dotenv 时跳过

from flask import Flask, request
import json
import time
import sys
import hashlib
import base64
import requests
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

from config import (
    CORP_ID, CORP_SECRET, AGENT_ID,
    WEWORK_TOKEN, ENCODING_AES_KEY,
    TENCENT_APPID, TENCENT_SECRET_ID, TENCENT_SECRET_KEY,
    MSG_CACHE_EXPIRE_SECONDS,
    WEATHER_API_KEY, WEATHER_CITY,
    SCHEDULER_TICK_MINUTES, SCHEDULER_DEFAULT_WAKE, SCHEDULER_DEFAULT_SLEEP,
    SCHEDULER_WEEKEND_SHIFT, SCHEDULER_PUSH_MAX_DAILY, SCHEDULER_MIN_PUSH_GAP,
    SERVER_PORT,
)
from time_utils import (
    parse_hhmm, format_minutes, add_minutes as _add_minutes_v9,
    safe_parse_hhmm, now_as_minutes,
)
from user_context import (
    get_or_create_user, get_all_active_users,
    increment_message_count, is_user_suspended,
    DATA_DIR, SYSTEM_DIR, DAILY_MESSAGE_LIMIT
)

# 异步处理端点的公网 URL（SCF 部署后填入，用于企微 5 秒超时的异步转发）
PROCESS_ENDPOINT_URL = os.environ.get("PROCESS_ENDPOINT_URL", "http://127.0.0.1:9000/process")
from wework_crypto import WXBizMsgCrypt
import brain
import channel_router

app = Flask(__name__)
_start_time = time.time()  # 记录启动时间，用于 /health uptime

# 过滤 Web 页面/API 读请求的 HTTP 访问日志，只保留业务日志和错误
import logging
class _QuietWebFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # 过滤健康检查
        if '"GET / ' in msg or '"GET /health' in msg:
            return False
        # 过滤 Web 静态页面、API 读请求、favicon
        if '"GET /web/' in msg or '"GET /api/' in msg or 'favicon' in msg:
            return False
        # 过滤 auth verify（前端每次页面加载都会调）
        if '"POST /api/auth/verify' in msg:
            return False
        # 过滤外部扫描探测（SSH probes, security.txt, robots.txt 等）
        if any(x in msg for x in ['SSH-2.0', 'security.txt', 'robots.txt',
                                    '.well-known', 'MGLNDD', 'boaform',
                                    'SCRIPT_FILENAME', 'mstshash']):
            return False
        # 过滤 bad request（非 HTTP 的网络扫描包）
        if 'code 400' in msg or 'code 505' in msg:
            return False
        return True
logging.getLogger('werkzeug').addFilter(_QuietWebFilter())
# 降低 werkzeug 日志级别，去掉 WARNING 横幅
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# 注册 Web 路由 Blueprint
from web_routes import web_bp, api_bp
app.register_blueprint(web_bp, url_prefix="/web")
app.register_blueprint(api_bp, url_prefix="/api")


# ============ Request ID 线程本地存储 ============
_request_local = threading.local()


def _get_request_id():
    """获取当前线程的 Request ID"""
    return getattr(_request_local, "request_id", None)


def _set_request_id(rid=None):
    """设置当前线程的 Request ID，不传则自动生成短 ID"""
    _request_local.request_id = rid or uuid.uuid4().hex[:8]
    return _request_local.request_id


_BEIJING_TZ = timezone(timedelta(hours=8))


def _log(msg):
    ts = datetime.now(_BEIJING_TZ).strftime("%H:%M:%S")
    rid = _get_request_id()
    if rid:
        print(f"{ts} [{rid}] {msg}", file=sys.stderr, flush=True)
    else:
        print(f"{ts} {msg}", file=sys.stderr, flush=True)


# ============ 企微 access_token 缓存 ============
_wework_token_cache = {"token": None, "expire_time": 0}


def get_wework_access_token():
    now = time.time()
    if _wework_token_cache["token"] and _wework_token_cache["expire_time"] > now:
        return _wework_token_cache["token"]
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={CORP_ID}&corpsecret={CORP_SECRET}"
    resp = requests.get(url, timeout=10)
    result = resp.json()
    if result.get("errcode") == 0:
        _wework_token_cache["token"] = result["access_token"]
        _wework_token_cache["expire_time"] = now + result["expires_in"] - 200
        return result["access_token"]
    _log(f"[企微] token 获取失败: {result}")
    return None


# ============ 消息发送 ============

def send_wework_message(user_id, content):
    """发送企业微信文本消息"""
    token = get_wework_access_token()
    if not token:
        return False
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    data = {
        "touser": user_id,
        "msgtype": "text",
        "agentid": AGENT_ID,
        "text": {"content": content}
    }
    resp = requests.post(url, json=data, timeout=10)
    result = resp.json()
    ok = result.get("errcode") == 0
    if not ok:
        _log(f"[回复] 发送失败: {result}")
    return ok


# ============ 消息去重（带大小限制） ============
_MSG_CACHE_MAX_SIZE = 2000
_processed_msg_cache = {}


def is_duplicate_msg(msg_id):
    if not msg_id:
        return False
    now = time.time()
    # 清理过期
    expired = [k for k, v in _processed_msg_cache.items() if v < now]
    for k in expired:
        del _processed_msg_cache[k]
    # 防止内存泄漏：超过上限时清除最早的一批
    if len(_processed_msg_cache) >= _MSG_CACHE_MAX_SIZE:
        oldest = sorted(_processed_msg_cache.items(), key=lambda x: x[1])[:_MSG_CACHE_MAX_SIZE // 4]
        for k, _ in oldest:
            del _processed_msg_cache[k]
        _log(f"[去重] 缓存超限，清理 {len(oldest)} 条旧记录")
    if msg_id in _processed_msg_cache:
        _log(f"[去重] 跳过: {msg_id}")
        return True
    _processed_msg_cache[msg_id] = now + MSG_CACHE_EXPIRE_SECONDS
    return False


# ============ 媒体下载 ============

def download_wework_media(media_id):
    """从企微下载临时素材，返回 (bytes, content_type) 或 (None, None)"""
    token = get_wework_access_token()
    if not token:
        return None, None
    url = f"https://qyapi.weixin.qq.com/cgi-bin/media/get?access_token={token}&media_id={media_id}"
    resp = requests.get(url, timeout=30)
    content_type = resp.headers.get("Content-Type", "")
    if "application/json" in content_type or "text/plain" in content_type:
        _log(f"[素材] 下载失败: {resp.text[:200]}")
        return None, None
    _log(f"[素材] 下载成功: size={len(resp.content)}, type={content_type}")
    return resp.content, content_type


# ============ 附件上传 ============

BEIJING_TZ = timezone(timedelta(hours=8))


def generate_attachment_name(msg_type, ext):
    ts = datetime.now(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{msg_type}.{ext}"


def upload_attachment(data, msg_type, ext, ctx, content_type="application/octet-stream"):
    """上传附件到用户 attachments 目录，返回完整路径或 None"""
    filename = generate_attachment_name(msg_type, ext)
    file_path = f"{ctx.attachments_path}/{filename}"
    ok = ctx.IO.upload_binary(file_path, data, content_type)
    return file_path if ok else None


# ============ ASR 语音识别 ============

def recognize_voice(audio_data, voice_format="amr"):
    """腾讯云录音文件识别极速版，降级到一句话识别"""
    import hmac

    if not TENCENT_APPID:
        _log("[ASR] 未配置 APPID，降级到一句话识别")
        return _recognize_voice_sentence(audio_data)

    try:
        timestamp = int(time.time())
        params = {
            "convert_num_mode": 1,
            "engine_type": "16k_zh",
            "filter_dirty": 0,
            "filter_modal": 0,
            "filter_punc": 0,
            "first_channel_only": 1,
            "secretid": TENCENT_SECRET_ID,
            "speaker_diarization": 0,
            "timestamp": timestamp,
            "voice_format": voice_format,
            "word_info": 0,
        }
        query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sign_str = f"POSTasr.cloud.tencent.com/asr/flash/v1/{TENCENT_APPID}?{query_str}"
        signature = base64.b64encode(
            hmac.new(TENCENT_SECRET_KEY.encode('utf-8'),
                     sign_str.encode('utf-8'), hashlib.sha1).digest()
        ).decode('utf-8')

        url = f"https://asr.cloud.tencent.com/asr/flash/v1/{TENCENT_APPID}?{query_str}"
        headers = {
            "Host": "asr.cloud.tencent.com",
            "Authorization": signature,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(audio_data)),
        }
        resp = requests.post(url, headers=headers, data=audio_data, timeout=30)
        result = resp.json()
        _log(f"[ASR极速版] code={result.get('code')}")

        if result.get("code") != 0:
            _log(f"[ASR极速版] 失败: {result.get('message')}")
            return _recognize_voice_sentence(audio_data)

        flash_result = result.get("flash_result", [])
        if flash_result:
            text = flash_result[0].get("text", "")
            _log(f"[ASR极速版] 识别: {text[:80]}")
            return text if text else None
        return None
    except Exception as e:
        _log(f"[ASR极速版] 异常: {e}")
        return _recognize_voice_sentence(audio_data)


def _recognize_voice_sentence(audio_data):
    """降级：腾讯云一句话识别"""
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.asr.v20190614 import asr_client, models

        cred = credential.Credential(TENCENT_SECRET_ID, TENCENT_SECRET_KEY)
        httpProfile = HttpProfile()
        httpProfile.endpoint = "asr.tencentcloudapi.com"
        httpProfile.reqTimeout = 30
        clientProfile = ClientProfile()
        clientProfile.httpProfile = httpProfile

        client = asr_client.AsrClient(cred, "", clientProfile)
        req = models.SentenceRecognitionRequest()
        req.EngSerViceType = "16k_zh"
        req.SourceType = 1
        req.VoiceFormat = "amr"
        req.Data = base64.b64encode(audio_data).decode('utf-8')
        req.DataLen = len(audio_data)

        resp = client.SentenceRecognition(req)
        _log(f"[ASR一句话] 成功: {resp.Result[:50] if resp.Result else 'empty'}")
        return resp.Result
    except Exception as e:
        _log(f"[ASR一句话] 失败: {e}")
        return None


# ============ XML 消息解析 ============

def parse_wechat_message(xml_data):
    """解析企微 XML 消息（含菜单 click 事件）"""
    root = ET.fromstring(xml_data)
    msg_type = root.find('MsgType').text
    from_user = root.find('FromUserName').text
    result = {'msg_type': msg_type, 'from_user': from_user}

    msg_id = root.find('MsgId')
    if msg_id is not None:
        result['msg_id'] = msg_id.text

    if msg_type == 'text':
        result['content'] = root.find('Content').text
    elif msg_type == 'image':
        media_id = root.find('MediaId')
        if media_id is not None:
            result['media_id'] = media_id.text
    elif msg_type == 'voice':
        media_id = root.find('MediaId')
        fmt = root.find('Format')
        if media_id is not None:
            result['media_id'] = media_id.text
        if fmt is not None:
            result['format'] = fmt.text
    elif msg_type == 'video':
        media_id = root.find('MediaId')
        if media_id is not None:
            result['media_id'] = media_id.text
    elif msg_type == 'link':
        for tag in ('Title', 'Description', 'Url'):
            node = root.find(tag)
            if node is not None:
                result[tag.lower()] = node.text
    elif msg_type == 'event':
        # 菜单点击事件
        event_node = root.find('Event')
        event_key_node = root.find('EventKey')
        if event_node is not None:
            result['event'] = event_node.text.upper()  # CLICK / VIEW 等
        if event_key_node is not None:
            result['event_key'] = event_key_node.text

    return result


# ============ F1: 链接内容抓取 ============

import re

_URL_PATTERN = re.compile(
    r'https?://[^\s<>"\')\]]+',
    re.IGNORECASE
)


def _extract_url(text):
    """
    从文本中提取 URL。
    仅当文本主体是 URL 时才提取（纯 URL 或 URL + 少量描述文字）。
    避免对正常聊天中偶尔出现的 URL 做不必要的抓取。
    """
    text = text.strip()
    match = _URL_PATTERN.search(text)
    if not match:
        return None
    url = match.group(0)
    # 只有当 URL 占文本大部分时才抓取（纯 URL 或 URL + 简短描述）
    non_url_text = text.replace(url, "").strip()
    if len(non_url_text) <= 30:
        return url
    return None

def _fetch_link_content(url):
    """
    F1: 抓取链接正文内容，失败返回空字符串（优雅降级）。
    支持微信公众号文章、普通网页。截断到 2000 字符。
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        }
        resp = requests.get(url, headers=headers, timeout=5,
                           allow_redirects=True, verify=True)
        resp.encoding = resp.apparent_encoding or 'utf-8'

        if resp.status_code != 200:
            _log(f"[链接抓取] HTTP {resp.status_code}: {url[:80]}")
            return ""

        content_type = resp.headers.get('Content-Type', '')
        if 'text/html' not in content_type and 'text/plain' not in content_type:
            _log(f"[链接抓取] 非网页内容({content_type}): {url[:80]}")
            return ""

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 移除无用标签
        for tag in soup.find_all(['script', 'style', 'nav', 'header',
                                   'footer', 'aside', 'iframe']):
            tag.decompose()

        # 优先取 article 标签（通用）或微信文章专用结构
        article = (soup.find('article')
                   or soup.find('div', class_='rich_media_content')
                   or soup.find('body'))

        if not article:
            _log(f"[链接抓取] 无法提取正文: {url[:80]}")
            return ""

        text = article.get_text(separator='\n', strip=True)
        result = text[:2000] if text else ""
        _log(f"[链接抓取] 成功: {len(result)} 字符, url={url[:80]}")
        return result
    except Exception as e:
        _log(f"[链接抓取] 异常({e}): {url[:80]}")
        return ""


# ============ 消息 → Payload 转换（网关核心） ============

def build_payload(msg, ctx):
    """
    将企微原始消息转换为 TextAgent payload。
    处理媒体下载、附件上传、ASR，但不做任何业务判断。
    返回 payload dict。
    """
    msg_type = msg['msg_type']
    user_id = msg.get('from_user', '')
    payload = {"user_id": user_id}

    if msg_type == 'text':
        content = msg.get('content', '')
        if content.startswith('/help') or content.startswith('帮助'):
            # 帮助命令直接在网关层处理
            return None, 'TextAgent 🤖\n\n发送任何内容，我会帮你记录到 Obsidian。\n支持：文字、图片、语音、视频、链接\n\n打卡相关：说"打卡"开始每日复盘'
        payload["type"] = "text"
        payload["text"] = content
        # F1: 检测纯 URL 文本，自动抓取网页正文
        url = _extract_url(content)
        if url:
            page_content = _fetch_link_content(url)
            if page_content:
                payload["page_content"] = page_content
                payload["detected_url"] = url
        return payload, None

    elif msg_type == 'image':
        media_id = msg.get('media_id', '')
        if not media_id:
            return None, "无法获取图片"
        data, content_type = download_wework_media(media_id)
        if not data:
            return None, "图片下载失败"
        ext = "jpg"
        if "png" in (content_type or ""):
            ext = "png"
        elif "gif" in (content_type or ""):
            ext = "gif"
        attachment = upload_attachment(data, "img", ext, ctx, content_type or "image/jpeg")
        if not attachment:
            return None, "图片上传失败"
        payload["type"] = "image"
        payload["attachment"] = attachment
        # 将图片 base64 传给 brain，用于千问 VL 图像理解
        payload["image_base64"] = base64.b64encode(data).decode("utf-8")
        return payload, None

    elif msg_type == 'voice':
        media_id = msg.get('media_id', '')
        audio_format = msg.get('format', 'amr')
        if not media_id:
            return None, "无法获取语音"
        data, content_type = download_wework_media(media_id)
        ext = audio_format.lower() if audio_format else "amr"
        if not data:
            return None, "语音下载失败"
        attachment = upload_attachment(data, "voice", ext, ctx, content_type or f"audio/{ext}")
        recognized_text = recognize_voice(data, voice_format=ext) or ""
        payload["type"] = "voice"
        payload["text"] = recognized_text
        payload["attachment"] = attachment or ""
        return payload, None

    elif msg_type == 'video':
        media_id = msg.get('media_id', '')
        if not media_id:
            return None, "无法获取视频"
        data, content_type = download_wework_media(media_id)
        if not data:
            return None, "视频下载失败"
        size_mb = len(data) / (1024 * 1024)
        _log(f"[视频] 大小={size_mb:.1f}MB")
        attachment = upload_attachment(data, "video", "mp4", ctx, content_type or "video/mp4")
        if not attachment:
            return None, "视频上传失败"
        payload["type"] = "video"
        payload["attachment"] = attachment
        return payload, None

    elif msg_type == 'link':
        payload["type"] = "link"
        payload["title"] = msg.get('title', '链接')
        payload["url"] = msg.get('url', '')
        payload["description"] = msg.get('description', '')[:200]
        # F1: 抓取网页正文内容
        if payload["url"]:
            payload["content"] = _fetch_link_content(payload["url"])
        return payload, None

    else:
        return None, f"暂不支持该消息类型: {msg_type}"


# ============ 自定义菜单 ============

# 菜单 click 事件处理：点击「查看功能」时发送的功能介绍
_FEATURE_INTRO = (
    "🤖 我是你的 AI 生活助手，以下是我的全部功能：\n\n"
    "📝 【记录】发任何内容（文字/图片/语音/视频/链接），我帮你自动归档\n"
    "✅ 【待办】说「提醒我…」或「帮我记个待办」，到时间自动提醒\n"
    "📰 【日报】说「今日日报」，我帮你总结今天的记录\n"
    "🔄 【打卡】说「打卡」，开始每日复盘\n"
    "🌤️ 【天气】说「天气」，查看今日天气\n"
    "📌 【随记】发「记 + 内容」，随时记录灵感想法，支持 #标签\n"
    "🎨 【风格】说「日报简洁一点」等，定制报告输出风格\n"
    "⚙️ 【设置】说「叫我XX」改昵称 / 说「改性格」自定义 AI 风格\n"
    "📊 【数据】说「导出数据」查看你的所有记录\n\n"
    "💡 有任何问题，直接发消息给我就好~"
)

# Markdown 版功能介绍（用于发送富文本消息）
_FEATURE_INTRO_MARKDOWN = """**🤖 我是你的 AI 生活助手**

以下是我的全部功能：

> 📝 **记录** — 发任何内容，我帮你自动归档
> ✅ **待办** — 说「提醒我…」到时间自动提醒
> 📰 **日报** — 说「今日日报」总结今天记录
> 🔄 **打卡** — 说「打卡」开始每日复盘
> 🌤️ **天气** — 说「天气」查看今日天气
> 📌 **随记** — 发「记 + 内容」记录灵感，支持 #标签
> 🎨 **风格** — 说「日报简洁一点」定制报告风格
> ⚙️ **设置** — 说「叫我XX」改昵称
> 📊 **数据** — 说「导出数据」查看记录

<font color="info">💡 有任何问题，直接发消息给我就好~</font>"""

def create_wework_menu():
    """
    通过企微 API 创建/更新自定义菜单。
    启动时自动调用，也可通过 POST /admin/menu 手动触发。
    两个按钮都是 click 类型：
    - 📊 数据总览 → 动态生成带 token 的链接发给用户
    - 🔍 查看功能 → 发送功能介绍文本
    """
    token = get_wework_access_token()
    if not token:
        _log("[菜单] 无法获取 access_token，跳过菜单创建")
        return False

    menu_data = {
        "button": [
            {
                "type": "click",
                "name": "📊 数据总览",
                "key": "MENU_WEB_LINK",
            },
            {
                "type": "click",
                "name": "🔍 查看功能",
                "key": "MENU_FEATURES",
            },
        ]
    }

    url = f"https://qyapi.weixin.qq.com/cgi-bin/menu/create?access_token={token}&agentid={AGENT_ID}"
    try:
        resp = requests.post(url, json=menu_data, timeout=10)
        result = resp.json()
        if result.get("errcode") == 0:
            _log("[菜单] ✅ 自定义菜单创建成功")
            return True
        else:
            _log(f"[菜单] ❌ 创建失败: {result}")
            return False
    except Exception as e:
        _log(f"[菜单] 创建异常: {e}")
        return False


def get_wework_menu():
    """获取当前自定义菜单配置"""
    token = get_wework_access_token()
    if not token:
        return None
    url = f"https://qyapi.weixin.qq.com/cgi-bin/menu/get?access_token={token}&agentid={AGENT_ID}"
    try:
        resp = requests.get(url, timeout=10)
        return resp.json()
    except Exception as e:
        _log(f"[菜单] 获取异常: {e}")
        return None


def delete_wework_menu():
    """删除自定义菜单"""
    token = get_wework_access_token()
    if not token:
        return False
    url = f"https://qyapi.weixin.qq.com/cgi-bin/menu/delete?access_token={token}&agentid={AGENT_ID}"
    try:
        resp = requests.get(url, timeout=10)
        result = resp.json()
        return result.get("errcode") == 0
    except Exception as e:
        _log(f"[菜单] 删除异常: {e}")
        return False


# ============ 消息处理主流程 ============

def handle_message(msg, user_id):
    """
    网关主处理流程：
    1. 获取 UserContext（自动注册新用户）
    2. 消息限额检查
    3. 构造 payload（含媒体处理）
    4. 交给 brain.process()
    5. 发送回复
    """
    t0 = time.time()
    msg_type = msg.get('msg_type', '')
    _log(f"[handle_message] === 开始处理 user={user_id}, msg_type={msg_type} ===")

    # event 类型：菜单点击事件
    if msg_type == 'event':
        event = msg.get('event', '')
        event_key = msg.get('event_key', '')
        _log(f"[handle_message] event={event}, key={event_key}, user={user_id}")

        if event == 'CLICK':
            if event_key == 'MENU_FEATURES':
                # 「查看功能」按钮 → 发送纯文本功能介绍（兼容微信端）
                channel_router.send_message(user_id, _FEATURE_INTRO)
                return
            elif event_key == 'MENU_WEB_LINK':
                # 「数据总览」按钮 → 转为"给我查看链接"，复用 web.token skill
                msg = {'msg_type': 'text', 'from_user': user_id, 'content': '给我查看链接'}
                msg_type = 'text'
                _log(f"[handle_message] 菜单事件 MENU_WEB_LINK → 转为「给我查看链接」")
                # 不 return，继续走下面的正常文本处理流程
            else:
                _log(f"[handle_message] 未知菜单 key: {event_key}")
                return
        else:
            # 其他 event（关注、进入应用等）静默忽略
            _log(f"[handle_message] 忽略 event: {event}/{event_key}")
            return

    try:
        # 检查用户是否被挂起
        if is_user_suspended(user_id):
            _log(f"[handle_message] 用户 {user_id} 已被挂起，拒绝处理")
            channel_router.send_message(user_id, "你的账号已被暂停使用，如有疑问请联系管理员。")
            return

        # 获取/创建用户上下文
        ctx, is_new = get_or_create_user(user_id)
        _log(f"[handle_message] 用户上下文已获取: is_new={is_new}, base_dir={ctx.base_dir}")

        # 新用户欢迎消息
        if is_new:
            _log(f"[handle_message] 新用户 {user_id}，发送欢迎消息")
            welcome_text = (
                "嗨～我是你的 AI 生活助手 🤖\n\n"
                "住在企业微信里，随时为你服务。\n\n"
                "先认识一下吧，你希望我怎么称呼你？\n"
                "（直接说「叫我XX」就好~）"
            )
            channel_router.send_message(user_id, welcome_text)
            # 通知管理员有新用户注册
            from config import ADMIN_USER_ID
            if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
                try:
                    total = len(get_all_active_users())
                    channel_router.send_alert(
                        f"📢 新用户注册\n\nuser_id: {user_id}\n当前活跃用户数: {total}")
                except Exception as e:
                    _log(f"[handle_message] 新用户通知管理员失败: {e}")
            return

        # ============ 新用户引导流程（onboarding） ============
        config = ctx.get_user_config()
        onboarding = config.get("onboarding_step", 0)

        if onboarding > 0 and msg_type == 'text':
            content = msg.get('content', '').strip()
            _log(f"[onboarding] step={onboarding}, user={user_id}, content={content[:50]}")

            # 任何阶段说"跳过"都结束引导
            if content in ('跳过', '算了', 'skip'):
                config["onboarding_step"] = 0
                ctx.save_user_config(config)
                channel_router.send_message(user_id, "没问题！有什么想法随时发给我就好～")
                return

            if onboarding == 1:
                # 等昵称 — 用模型提取，能处理各种自然表达
                from brain import call_llm
                extract_prompt = (
                    "用户在设置昵称，请从下面这句话中提取出用户希望被称呼的昵称。\n"
                    "只返回昵称本身，不要任何解释、引号或标点。\n"
                    "如果无法识别，返回空。\n\n"
                    f"用户说：{content}"
                )
                nickname = call_llm(
                    [{"role": "user", "content": extract_prompt}],
                    model_tier="flash", max_tokens=20, temperature=0
                )
                nickname = (nickname or "").strip().strip('"\'""''')
                if not nickname:
                    channel_router.send_message(user_id, "没听清名字呢，再说一次？直接打名字就行~")
                    return

                # 保存昵称
                config["nickname"] = nickname
                config["onboarding_step"] = 2  # 进入等 AI 名字阶段
                ctx.save_user_config(config)

                from user_context import update_user_nickname
                update_user_nickname(user_id, nickname)

                reply = (
                    f"好的{nickname}！以后就这么叫你啦～\n\n"
                    f"那你想给我起个什么名字呢？\n"
                    f"（直接说名字就好，或者说「跳过」用默认名字 TextAgent~）"
                )
                channel_router.send_message(user_id, reply)
                return

            elif onboarding == 2:
                # 等 AI 名字 — 用模型提取
                from brain import call_llm
                extract_prompt = (
                    "用户在给 AI 起名字，请从下面这句话中提取出用户想给 AI 起的名字。\n"
                    "只返回名字本身，不要任何解释、引号或标点。\n"
                    "如果无法识别，返回空。\n\n"
                    f"用户说：{content}"
                )
                ai_name = call_llm(
                    [{"role": "user", "content": extract_prompt}],
                    model_tier="flash", max_tokens=20, temperature=0
                )
                ai_name = (ai_name or "").strip().strip('"\'""''')
                if not ai_name:
                    ai_name = "TextAgent"  # 无法识别时保持默认

                config["ai_name"] = ai_name
                config["onboarding_step"] = 3  # 进入等笔记阶段
                ctx.save_user_config(config)

                nickname = config.get("nickname", "")
                reply = (
                    f"好呀，以后我就叫「{ai_name}」啦～ 很高兴认识你{nickname}！\n\n"
                    f"来试试我的核心功能吧 👇\n"
                    f"随便发句话给我，比如：\n"
                    f"「今天天气真好，心情不错」\n\n"
                    f"💡 小贴士：\n"
                    f"• 发「记 + 内容」随时记录灵感和想法\n"
                    f"• 说「日报简洁一点」可以定制报告风格"
                )
                channel_router.send_message(user_id, reply)
                return

            elif onboarding == 3:
                # 等第一条笔记 — 正常处理但加引导提示
                config["onboarding_step"] = 4
                ctx.save_user_config(config)
                # 不 return，继续走正常 brain 流程

            elif onboarding == 4:
                # 第四步或之后的消息 — 结束引导，正常处理
                config["onboarding_step"] = 0
                ctx.save_user_config(config)
                # 不 return，继续正常流程

        elif onboarding > 0 and msg_type != 'text':
            # 非文本消息（图片/语音等）在引导阶段也推进
            if onboarding == 1:
                channel_router.send_message(user_id, "先告诉我你的名字吧～直接打名字就行~")
                return
            elif onboarding == 2:
                # 等 AI 名字阶段收到非文本，跳过起名，保持默认
                config["onboarding_step"] = 3
                ctx.save_user_config(config)
                channel_router.send_message(user_id, "没关系，你可以之后再给我起名字～先来试试核心功能吧 👇\n随便发句话给我~")
                return
            else:
                # step 3/4 收到非文本，也正常处理并推进
                config["onboarding_step"] = 0
                ctx.save_user_config(config)

        # 引导阶段也正常计数
        # 消息计数 + 限额检查
        count, over_limit = increment_message_count(user_id)
        _log(f"[handle_message] 消息计数: count={count}, limit={DAILY_MESSAGE_LIMIT}, over={over_limit}")
        if over_limit:
            channel_router.send_message(user_id, f"今日消息已达上限（{DAILY_MESSAGE_LIMIT} 条），明天再来吧~")
            return

        payload, quick_reply = build_payload(msg, ctx)
        _log(f"[handle_message] payload构建完成: type={payload.get('type') if payload else 'None'}, "
             f"quick_reply={'有' if quick_reply else '无'}")

        # 帮助命令或媒体处理失败
        if payload is None:
            if quick_reply and user_id:
                channel_router.send_message(user_id, quick_reply)
            return

        # 交给大脑（传入发送回调，实现先回复后保存）
        def _send_reply(text):
            if user_id:
                channel_router.send_message(user_id, text)

        _log(f"[handle_message] 交给 brain.process(), payload_type={payload.get('type')}")
        result = brain.process(payload, send_fn=_send_reply, ctx=ctx)
        reply = result.get("reply") if result else None
        already_sent = result.get("already_sent", False) if result else False
        _log(f"[handle_message] brain 返回: reply={'有' if reply else '无'}({len(reply) if reply else 0}字), "
             f"already_sent={already_sent}")

        # 如果 brain 已经通过 send_fn 发送，不再重复发送
        if reply and user_id and not already_sent:
            channel_router.send_message(user_id, reply)

        # 引导阶段追加提示
        config_now = ctx.get_user_config()
        ob_step = config_now.get("onboarding_step", 0)
        nickname = config_now.get("nickname") or ""

        if ob_step == 4:
            # 刚完成第一条笔记（step 3→4），追加待办引导
            time.sleep(0.5)
            guide = (
                f"✨ 看，你的第一条记录已经保存好了！\n\n"
                f"再试试待办功能？直接说：\n"
                f"「帮我添加待办 明天买咖啡」"
            )
            channel_router.send_message(user_id, guide)

        elif ob_step == 0 and onboarding == 4:
            # 刚完成引导（step 4→0）— 生成 Web 链接一并发出
            time.sleep(0.5)

            # 自动生成查看链接
            from user_context import generate_token
            token = generate_token(user_id)
            import os as _os
            domain = _os.environ.get("WEB_DOMAIN", "127.0.0.1:9000")
            # IP 地址用 http，有域名才用 https
            _is_ip = all(part.isdigit() for part in domain.split(":")[0].split("."))
            scheme = "http" if _is_ip or "127.0.0.1" in domain or "localhost" in domain else "https"
            web_url = f"{scheme}://{domain}/web/login?token={token}"

            final = (
                f"🎉 太棒了{nickname}！你已经掌握了核心用法：\n\n"
                f"💬 发消息 → 自动记笔记\n"
                f"✅ 说「添加待办」→ 管理任务\n"
                f"📊 每晚自动生成日报\n"
                f"🌙 晚上 9 点会邀请你打卡复盘\n\n"
                f"📱 你还可以在浏览器里查看所有数据：\n"
                f"{web_url}\n\n"
                f"链接 24 小时有效，过期了跟我说「给我查看链接」就行～\n\n"
                f"还有更多玩法慢慢发现，有什么想法随时告诉我！"
            )
            channel_router.send_message(user_id, final)

        _log(f"[handle_message] === 处理完成 user={user_id}, 耗时={time.time()-t0:.1f}s ===")

    except Exception as e:
        _log(f"[handle_message] === 处理异常 user={user_id}, 耗时={time.time()-t0:.1f}s ===")
        _log(f"[handle_message] 异常: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)
        if user_id:
            channel_router.send_message(user_id, "处理消息时出错了，请稍后重试")


# ============ 加解密器 ============
wx_crypt = WXBizMsgCrypt(WEWORK_TOKEN, ENCODING_AES_KEY, CORP_ID)


# ============ Flask 路由 ============

@app.route('/wework', methods=['GET', 'POST'])
def wework():
    """企业微信入口"""
    if request.method == 'GET':
        msg_signature = request.args.get('msg_signature', '')
        timestamp = request.args.get('timestamp', '')
        nonce = request.args.get('nonce', '')
        echostr = request.args.get('echostr', '')
        _log(f"[企微验证] 收到GET请求: msg_signature={msg_signature}, timestamp={timestamp}, nonce={nonce}, echostr={echostr[:20]}...")
        reply = wx_crypt.verify_url(msg_signature, timestamp, nonce, echostr)
        if reply:
            _log(f"[企微验证] 验证成功，返回: {reply}")
            # 企微要求返回纯文本格式的解密后 echostr
            from flask import make_response
            resp = make_response(str(reply))
            resp.headers['Content-Type'] = 'text/plain'
            return resp
        else:
            _log(f"[企微验证] 验证失败")
            return "verify failed", 403

    if request.method == 'POST':
        try:
            xml_data = request.data.decode('utf-8')

            msg_signature = request.args.get('msg_signature', '')
            timestamp = request.args.get('timestamp', '')
            nonce = request.args.get('nonce', '')

            # 解密
            root = ET.fromstring(xml_data)
            encrypt_node = root.find('Encrypt')
            if encrypt_node is not None:
                decrypted_xml = wx_crypt.decrypt_msg(
                    msg_signature, timestamp, nonce, encrypt_node.text)
                if not decrypted_xml:
                    _log("[企微] 解密失败")
                    return "success"
                msg = parse_wechat_message(decrypted_xml)
            else:
                msg = parse_wechat_message(xml_data)

            user_id = msg.get('from_user', '')
            msg_id = msg.get('msg_id', '')
            _log(f"[企微] user={user_id}, type={msg['msg_type']}, id={msg_id}")

            # 消息去重
            if msg_id and is_duplicate_msg(msg_id):
                return "success"

            # 异步处理：通过公网 URL 调用自己的 /process 端点
            # 这会触发一个全新的 SCF 请求，不受企微 5 秒超时影响
            payload_data = json.dumps({
                "msg": msg,
                "user_id": user_id
            }, ensure_ascii=False)

            def fire_and_forget():
                try:
                    resp = requests.post(
                        PROCESS_ENDPOINT_URL,
                        data=payload_data.encode('utf-8'),
                        headers={"Content-Type": "application/json"},
                        timeout=300  # 等完整响应，日报等重任务可能需要更久
                    )
                    _log(f"[触发] /process 返回: {resp.status_code}")
                except Exception as e:
                    _log(f"[触发] /process 调用异常: {e}")

            t = threading.Thread(target=fire_and_forget)
            t.start()

            # 等一小段时间确保请求已发出（TCP 握手完成）
            time.sleep(0.3)

            _log(f"[企微] 已触发 /process，立即返回 success")
            return "success"

        except Exception as e:
            _log(f"[企微] 错误: {e}")
            import traceback
            traceback.print_exc(file=sys.stderr)
            return "success"

    return "success"


@app.route('/process', methods=['POST'])
def process_endpoint():
    """内部异步处理端点：接收消息并调用 brain 处理"""
    try:
        rid = _set_request_id()
        data = request.get_json(force=True)
        msg = data.get("msg", {})
        user_id = data.get("user_id", "")
        _log(f"[/process] 开始处理 type={msg.get('msg_type')}, user={user_id}")
        handle_message(msg, user_id)
        _log(f"[/process] 处理完成")
        return "ok"
    except Exception as e:
        _log(f"[/process] 异常: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)
        return "error"


@app.route('/admin/menu', methods=['GET', 'POST', 'DELETE'])
def admin_menu():
    """
    菜单管理端点：
    GET    → 查看当前菜单配置
    POST   → 创建/更新菜单
    DELETE → 删除菜单
    """
    if request.method == 'GET':
        result = get_wework_menu()
        return json.dumps(result or {"error": "获取失败"}, ensure_ascii=False)
    elif request.method == 'POST':
        ok = create_wework_menu()
        return json.dumps({"ok": ok, "message": "菜单已同步" if ok else "同步失败"}, ensure_ascii=False)
    elif request.method == 'DELETE':
        ok = delete_wework_menu()
        return json.dumps({"ok": ok, "message": "菜单已删除" if ok else "删除失败"}, ensure_ascii=False)


@app.route('/system', methods=['POST'])
def system_endpoint():
    """系统端点：定时器/手动触发的 system action（支持多用户遍历）"""
    try:
        rid = _set_request_id()
        data = request.get_json(force=True)
        action = data.get("action", "")
        target_user = data.get("user_id", "")
        _log(f"[/system] action={action}, user={target_user or 'all'}")

        if action == "refresh_cache":
            from memory import invalidate_all_caches
            from user_context import cleanup_expired_tokens
            invalidate_all_caches()
            removed = cleanup_expired_tokens()
            if removed > 0:
                _log(f"[/system] refresh_cache: 清理过期令牌 {removed} 个")
            return json.dumps({"ok": True, "action": "refresh_cache", "tokens_cleaned": removed})

        # 待办提醒专用心跳：每 1 分钟检查所有用户的到期待办
        if action == "todo_remind_tick":
            from skills.todo_manage import check_todos
            from memory import write_state_and_update_cache
            user_ids = [target_user] if target_user else get_all_active_users()
            total_sent = 0
            now = datetime.now(BEIJING_TZ)
            now_str = now.strftime("%H:%M")
            today_str = now.strftime("%Y-%m-%d")
            now_full = now.strftime("%Y-%m-%d %H:%M")
            _log(f"[todo_remind_tick] 开始检查, 用户数: {len(user_ids)}, now={now_str}")
            for uid in user_ids:
                try:
                    ctx, _ = get_or_create_user(uid)
                    # 【修复】绕过缓存直接读文件，确保获取最新 state（含刚添加的待办）
                    state = ctx.IO.read_json(ctx.state_file) or {}
                    todos = state.get("todos", [])
                    if not todos:
                        continue  # 无待办的用户直接跳过

                    # ── 智能预检：快速判断本轮是否有可能触发的待办 ──
                    # 只扫描 state 中的字段（纯内存操作，不读文件），
                    # 如果没有任何待办可能在本分钟触发，跳过 check_todos（省去 Todo.md IO）
                    has_potential = False
                    for t in todos:
                        # 已推送过的循环待办跳过
                        if t.get("recur") and t.get("last_notified") == today_str:
                            continue
                        # 循环待办：有 remind_at 的看时间是否到了，无 remind_at 且今天未推的直接标记
                        if t.get("recur"):
                            remind_at = t.get("remind_at", "")
                            if remind_at and len(remind_at) <= 5:
                                if now_str >= remind_at:
                                    has_potential = True
                                    break
                            elif not t.get("last_notified") or t["last_notified"] != today_str:
                                has_potential = True
                                break
                            continue
                        # 一次性定时提醒：未推送且临近到期
                        remind_at = t.get("remind_at", "")
                        if remind_at and len(remind_at) > 5 and not t.get("last_notified"):
                            # 粗筛：只有同天或 pre_notified 未做的才需要详查
                            if remind_at[:10] == today_str or not t.get("pre_notified"):
                                has_potential = True
                                break
                            continue
                        # 【修复】一次性待办 remind_at 为纯 HH:MM（格式异常），
                        # 也标记为有潜力，让 check_todos 中的运行时兜底逻辑去修复和处理
                        if remind_at and len(remind_at) <= 5 and not t.get("recur"):
                            has_potential = True
                            break
                        # 截止日期提醒
                        due_date = t.get("due_date", "")
                        if due_date:
                            if (due_date == today_str and t.get("last_notified") != today_str) or \
                               (due_date < today_str and not t.get("overdue_notified")):
                                has_potential = True
                                break

                    if not has_potential:
                        continue  # 本轮无需检查，快速跳过

                    _log(f"[todo_remind_tick] 检查用户 {uid}, 待办数: {len(todos)}")
                    result = check_todos(state, ctx=ctx, todo_file=ctx.todo_file)
                    messages = result.get("messages", [])
                    state_updates = result.get("state_updates", {})
                    if messages:
                        combined = "📋 待办提醒\n\n" + "\n".join(messages)
                        channel_router.send_message(uid, combined)
                        total_sent += len(messages)
                        _log(f"[todo_remind_tick] 推送 {len(messages)} 条提醒给 {uid}")
                    if state_updates:
                        for k, v in state_updates.items():
                            state[k] = v
                        write_state_and_update_cache(state, ctx)
                except Exception as e:
                    _log(f"[todo_remind_tick] 用户 {uid} 失败: {e}")
            _log(f"[todo_remind_tick] 检查完成, 共推送 {total_sent} 条")
            return json.dumps({"ok": True, "action": "todo_remind_tick", "sent": total_sent})

        # V9: 智能调度引擎（daily_init / scheduler_tick 遍历所有用户）
        if action in ("daily_init", "scheduler_tick"):
            user_ids = [target_user] if target_user else get_all_active_users()
            results = []
            for uid in user_ids:
                try:
                    ctx, _ = get_or_create_user(uid)
                    if action == "daily_init":
                        r = _daily_init(uid, ctx)
                    else:
                        r = _scheduler_tick(uid, ctx)
                    results.append({"user_id": uid, **r})
                except Exception as e:
                    _log(f"[/system] V9 {action} 用户 {uid} 失败: {e}")
                    results.append({"user_id": uid, "ok": False, "error": str(e)})
            return json.dumps({"ok": True, "action": action, "results": results}, ensure_ascii=False)

        # 如果指定了 user_id，只处理该用户；否则遍历所有活跃用户
        if target_user:
            user_ids = [target_user]
        else:
            user_ids = get_all_active_users()
            _log(f"[/system] 遍历 {len(user_ids)} 个活跃用户")

        total_results = []

        for uid in user_ids:
            try:
                ctx, _ = get_or_create_user(uid)
                result = _run_system_action_for_user(action, data, uid, ctx)
                total_results.append({"user_id": uid, **result})
            except Exception as e:
                _log(f"[/system] 用户 {uid} 执行 {action} 失败: {e}")
                total_results.append({"user_id": uid, "ok": False, "error": str(e)})

            # 多用户遍历时随机延迟，避免 API 限流
            if len(user_ids) > 1:
                import random
                time.sleep(random.uniform(1, 3))

        _log(f"[/system] {action} 完成, 共处理 {len(total_results)} 个用户")
        return json.dumps({"ok": True, "action": action, "results": total_results},
                          ensure_ascii=False)

    except Exception as e:
        _log(f"[/system] 异常: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)
        return json.dumps({"ok": False, "error": str(e)})


def _run_system_action_for_user(action, data, uid, ctx):
    """为单个用户执行系统动作，返回结果 dict（V9: 增加时间戳日志）"""
    from memory import read_state_cached, write_state_and_update_cache
    scheduled_at = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    _log(f"[system_action] 开始执行: action={action}, user={uid}, scheduled_at={scheduled_at}")
    t0 = time.time()

    if action == "todo_remind":
        from skills.todo_manage import check_todos
        # 【修复】绕过缓存直接读文件，与 todo_remind_tick 保持一致
        state = ctx.IO.read_json(ctx.state_file) or {}
        result = check_todos(state, ctx=ctx, todo_file=ctx.todo_file)
        messages = result.get("messages", [])
        state_updates = result.get("state_updates", {})
        _log(f"[system_action] todo_remind: {len(messages)} 条提醒, {len(state_updates)} 个状态更新")
        if messages:
            combined = "📋 待办提醒\n\n" + "\n".join(messages)
            channel_router.send_message(uid, combined)
        if state_updates:
            for k, v in state_updates.items():
                state[k] = v
            write_state_and_update_cache(state, ctx)
        _log(f"[system_action] todo_remind 完成, user={uid}, 耗时={time.time()-t0:.1f}s")
        return {"ok": True, "sent": len(messages)}

    if action in ("morning_report", "evening_checkin", "daily_report"):
        context = {}
        try:
            todo_content = ctx.IO.read_text(ctx.todo_file)
            if todo_content:
                context["todo"] = todo_content[:2000]
            quick_notes = ctx.IO.read_text(ctx.quick_notes_file)
            if quick_notes:
                context["quick_notes"] = quick_notes[:1000]
        except Exception as e:
            _log(f"[/system] [{uid}] 读取上下文失败（不影响主流程）: {e}")

        if action == "morning_report":
            try:
                context["time_capsule"] = _build_time_capsule(ctx)
            except Exception as e:
                _log(f"[/system] [{uid}] 时间胶囊读取失败: {e}")

            try:
                weather = _build_weather_context()
                if weather:
                    context["weather"] = weather
            except Exception as e:
                _log(f"[/system] [{uid}] 天气获取失败: {e}")

            try:
                from skills.decision_track import get_due_decisions
                _state = read_state_cached(ctx) or {}
                due_decisions = get_due_decisions(_state)
                if due_decisions:
                    context["due_decisions"] = due_decisions
            except Exception as e:
                _log(f"[/system] [{uid}] 到期决策读取失败: {e}")

        if action in ("morning_report", "evening_checkin"):
            try:
                context["nudge"] = _build_nudge_context(ctx)
            except Exception as e:
                _log(f"[/system] [{uid}] nudge 上下文读取失败: {e}")

        if action == "evening_checkin":
            try:
                _state = read_state_cached(ctx) or {}
                daily_top3 = _state.get("daily_top3", {})
                today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
                if daily_top3 and daily_top3.get("date") == today_str:
                    context["daily_top3"] = daily_top3
            except Exception as e:
                _log(f"[/system] [{uid}] daily_top3 读取失败: {e}")

        payload = {
            "type": "system",
            "action": action,
            "user_id": uid,
            "context": context
        }
        _log(f"[system_action] {action}: 调用 brain.process(), context_keys={list(context.keys())}")
        result = brain.process(payload, ctx=ctx)
        reply = result.get("reply") if result else None
        if reply:
            delivered_at = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
            _log(f"[system_action] {action}: 发送回复给 {uid}, len={len(reply)}, delivered_at={delivered_at}")
            channel_router.send_message(uid, reply)
        _log(f"[system_action] {action} 完成, user={uid}, has_reply={bool(reply)}, "
             f"耗时={time.time()-t0:.1f}s, scheduled_at={scheduled_at}")
        return {"ok": True, "has_reply": bool(reply)}

    if action == "weekly_review":
        from skills.weekly_review import execute as weekly_execute
        state = read_state_cached(ctx) or {}
        _log(f"[system_action] weekly_review: 开始生成周回顾, user={uid}")
        result = weekly_execute(data, state, ctx)
        write_state_and_update_cache(state, ctx)
        reply = result.get("reply") if result else None
        if reply:
            channel_router.send_message(uid, reply)
        _log(f"[system_action] weekly_review 完成, user={uid}, has_reply={bool(reply)}, 耗时={time.time()-t0:.1f}s")
        return {"ok": True, "has_reply": bool(reply)}

    if action == "nudge_check":
        messages = _run_nudge_check(ctx)
        _log(f"[system_action] nudge_check: {len(messages)} 条推送, user={uid}")
        for msg in messages:
            channel_router.send_message(uid, msg)
        _log(f"[system_action] nudge_check 完成, user={uid}, 耗时={time.time()-t0:.1f}s")
        return {"ok": True, "sent": len(messages)}

    if action == "monthly_review":
        from skills.monthly_review import execute as monthly_execute
        state = read_state_cached(ctx) or {}
        _log(f"[system_action] monthly_review: 开始生成月度回顾, user={uid}")
        result = monthly_execute(data, state, ctx)
        write_state_and_update_cache(state, ctx)
        reply = result.get("reply") if result else None
        if reply:
            channel_router.send_message(uid, reply)
        _log(f"[system_action] monthly_review 完成, user={uid}, has_reply={bool(reply)}, 耗时={time.time()-t0:.1f}s")
        return {"ok": True, "has_reply": bool(reply)}

    if action == "companion_check":
        message = _run_companion_check(ctx)
        _log(f"[system_action] companion_check 完成, user={uid}, has_message={bool(message)}, 耗时={time.time()-t0:.1f}s")
        if message:
            channel_router.send_message(uid, message)
        return {"ok": True, "sent": 1 if message else 0}

    if action == "finance_monthly_report":
        # 仅对 admin 用户触发（finance 功能需要 iCost 数据）
        user_cfg = ctx.get_user_config() if hasattr(ctx, "get_user_config") else {}
        if user_cfg.get("role") != "admin":
            _log(f"[system_action] finance_monthly_report: 非 admin 用户，跳过, user={uid}")
            return {"ok": True, "skipped": True, "reason": "not_admin"}
        from skills.finance_report import execute as finance_execute
        state = read_state_cached(ctx) or {}
        _log(f"[system_action] finance_monthly_report: 开始生成财务月报, user={uid}")
        result = finance_execute({}, state, ctx)
        write_state_and_update_cache(state, ctx)
        reply = result.get("reply") if result else None
        if reply:
            channel_router.send_message(uid, reply)
        _log(f"[system_action] finance_monthly_report 完成, user={uid}, has_reply={bool(reply)}, 耗时={time.time()-t0:.1f}s")
        return {"ok": True, "has_reply": bool(reply)}

    _log(f"[system_action] 未知 action: {action}")
    return {"ok": False, "error": f"unknown action: {action}"}


@app.route('/', methods=['GET'])
def health():
    """健康检查（基础 — 用于负载均衡探活）"""
    return "TextAgent is alive"


@app.route('/health', methods=['GET'])
def health_detail():
    """深度健康检查 — 返回各依赖组件状态"""
    import shutil
    checks = {}
    overall = True

    # 1. DeepSeek API Key 是否配置
    from config import DEEPSEEK_API_KEY, QWEN_API_KEY
    checks["deepseek_key"] = bool(DEEPSEEK_API_KEY)
    checks["qwen_key"] = bool(QWEN_API_KEY)
    if not DEEPSEEK_API_KEY:
        overall = False

    # 2. 企微 token 是否可获取
    try:
        token = get_wework_access_token()
        checks["wework_token"] = bool(token)
        if not token:
            overall = False
    except Exception:
        checks["wework_token"] = False
        overall = False

    # 3. 磁盘空间（/root 分区）
    try:
        usage = shutil.disk_usage("/root")
        free_gb = usage.free / (1024 ** 3)
        checks["disk_free_gb"] = round(free_gb, 1)
        if free_gb < 1:
            overall = False
            checks["disk_warning"] = "磁盘空间不足 1GB"
    except Exception:
        checks["disk_free_gb"] = -1

    # 4. Cron 调度是否活跃（通过 cron 日志最后更新时间判断）
    try:
        cron_log = "/var/log/textagent_cron.log"
        if os.path.exists(cron_log):
            last_mod = os.path.getmtime(cron_log)
            age_seconds = int(time.time() - last_mod)
            checks["cron_last_update_seconds_ago"] = age_seconds
            # todo_remind_tick 每分钟执行，超过 5 分钟未更新说明 cron 可能停了
            if age_seconds > 300:
                checks["cron_warning"] = f"cron 日志 {age_seconds}s 未更新，请检查 crontab"
                checks["scheduler"] = False
            else:
                checks["scheduler"] = True
        else:
            checks["scheduler"] = False
            checks["cron_warning"] = "cron 日志文件不存在，crontab 可能未配置"
    except Exception:
        checks["scheduler"] = False

    # 5. 活跃用户数
    try:
        active_count = len(get_all_active_users())
        checks["active_users"] = active_count
    except Exception:
        checks["active_users"] = -1

    # 6. 日志文件大小
    try:
        from config import LOG_FILE_TEXTAGENTFORU
        if os.path.exists(LOG_FILE_TEXTAGENTFORU):
            log_size_mb = os.path.getsize(LOG_FILE_TEXTAGENTFORU) / (1024 * 1024)
            checks["log_size_mb"] = round(log_size_mb, 1)
            if log_size_mb > 100:
                checks["log_warning"] = "日志文件超过 100MB"
        else:
            checks["log_size_mb"] = 0
    except Exception:
        checks["log_size_mb"] = -1

    # 7. 消息去重缓存大小
    checks["msg_cache_size"] = len(_processed_msg_cache)

    # 8. 启动时间
    checks["uptime_s"] = int(time.time() - _start_time)

    status_code = 200 if overall else 503
    return json.dumps({
        "status": "healthy" if overall else "degraded",
        "checks": checks,
        "timestamp": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False), status_code, {"Content-Type": "application/json"}


# ============ 时间胶囊辅助函数 ============

def _build_time_capsule(ctx):
    """
    F3: 读取历史同日的笔记，供 morning_report 注入。
    返回 dict: {"7d_ago": {...}, "30d_ago": {...}, "365d_ago": {...}}
    """
    from concurrent.futures import ThreadPoolExecutor

    today = datetime.now(BEIJING_TZ).date()
    offsets = {
        "7d_ago": 7,
        "30d_ago": 30,
        "365d_ago": 365,
    }

    capsule = {}
    files_to_read = {}

    for key, days in offsets.items():
        past_date = today - timedelta(days=days)
        date_str = past_date.strftime("%Y-%m-%d")
        files_to_read[f"{key}_daily"] = (date_str, f"{ctx.daily_notes_dir}/{date_str}.md")

    # 也需要从 Quick-Notes 中提取历史日期条目
    files_to_read["quick_notes"] = (None, ctx.quick_notes_file)

    # 并发读取
    results = {}
    try:
        from brain import _executor
        executor = _executor
    except ImportError:
        executor = ThreadPoolExecutor(max_workers=4)

    futures = {k: executor.submit(ctx.IO.read_text, v[1]) for k, v in files_to_read.items()}

    for k, fut in futures.items():
        try:
            results[k] = fut.result(timeout=15) or ""
        except Exception:
            results[k] = ""

    qn_text = results.get("quick_notes", "")

    for key, days in offsets.items():
        past_date = today - timedelta(days=days)
        date_str = past_date.strftime("%Y-%m-%d")

        daily_content = results.get(f"{key}_daily", "")
        # 从 Quick-Notes 提取该日期条目
        qn_entries = _extract_date_entries_for_capsule(qn_text, date_str)

        content_parts = []
        if qn_entries:
            content_parts.append(qn_entries[:500])
        if daily_content:
            # 只取日报总结部分，不取原始记录
            if "## 📊 今日总结" in daily_content:
                summary_section = daily_content.split("## 📊 今日总结")[1]
                end_idx = summary_section.find("\n## ")
                if end_idx >= 0:
                    summary_section = summary_section[:end_idx]
                content_parts.append(summary_section.strip()[:500])

        if content_parts:
            capsule[key] = {
                "date": date_str,
                "notes": "\n\n".join(content_parts)[:800]
            }
        else:
            capsule[key] = None

    return capsule


def _extract_date_entries_for_capsule(text, date_str):
    """从 Quick-Notes 中提取指定日期的条目（时间胶囊用）"""
    if not text:
        return ""
    entries = []
    sections = text.split("\n## ")
    for section in sections[1:]:
        first_line = section.split("\n")[0].strip()
        if first_line.startswith(date_str):
            # 只取内容，不取时间戳头
            body = "\n".join(section.split("\n")[1:]).strip()
            if body and body != "---":
                entries.append(body)
    return "\n".join(entries[:5])  # 最多 5 条


# ============ F5: 轻推系统辅助函数 ============

def _build_nudge_context(ctx):
    """
    F5: 构建 nudge 上下文信号，注入 morning_report / evening_checkin 的 context。
    读取 state 中的 nudge_state + mood_scores，返回 dict。
    """
    from memory import read_state_cached
    state = read_state_cached(ctx) or {}

    nudge = state.get("nudge_state", {})
    mood_scores = state.get("mood_scores", [])

    # 昨天的情绪评分
    today = datetime.now(BEIJING_TZ).date()
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_mood = None
    for s in mood_scores:
        if s.get("date") == yesterday_str:
            yesterday_mood = {"score": s.get("score"), "label": s.get("label", "")}
            break

    # 连续记录天数
    streak = nudge.get("streak", 0)

    # 距上次消息的小时数
    last_msg_date = nudge.get("last_message_date", "")
    hours_since_last = None
    if last_msg_date:
        try:
            last_dt = datetime.strptime(last_msg_date, "%Y-%m-%d")
            last_dt = last_dt.replace(tzinfo=BEIJING_TZ)
            now = datetime.now(BEIJING_TZ)
            hours_since_last = round((now - last_dt).total_seconds() / 3600, 1)
        except Exception:
            pass

    # 需要跟进的人（距上次提到超过 7 天 + 之前有负面情绪记录）
    people_to_follow = []
    people_last = nudge.get("people_last_mentioned", {})
    for name, last_date_str in people_last.items():
        try:
            last_d = datetime.strptime(last_date_str, "%Y-%m-%d").date()
            if (today - last_d).days >= 7:
                people_to_follow.append(name)
        except Exception:
            pass

    # 打卡统计
    checkin_stats = state.get("checkin_stats", {})

    return {
        "yesterday_mood": yesterday_mood,
        "streak": streak,
        "last_message_hours_ago": hours_since_last,
        "people_to_follow_up": people_to_follow,
        "checkin_streak": checkin_stats.get("streak", 0),
    }


def _run_nudge_check(ctx):
    """
    F5: 独立轻推检测（每天 14:00 执行）— 纯规则引擎，不走 LLM。
    返回要推送的消息列表。
    """
    from memory import read_state_cached
    state = read_state_cached(ctx) or {}

    nudge = state.get("nudge_state", {})
    mood_scores = state.get("mood_scores", [])
    messages = []

    today = datetime.now(BEIJING_TZ).date()
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # 场景1: 沉默检测 — 今天 14:00 之前无消息
    last_msg_date = nudge.get("last_message_date", "")
    if last_msg_date != today_str:
        messages.append("今天很安静呀，是忙还是累了？随时可以来聊两句~")

    # 场景2: 情绪跟进 — 昨天 mood_score ≤ 4
    for s in mood_scores:
        if s.get("date") == yesterday_str and s.get("score") is not None:
            if s["score"] <= 4:
                label = s.get("label", "")
                hint = f"（{label}）" if label else ""
                messages.append(f"昨天好像有点低落{hint}，今天好点了吗？")
            break

    # 场景3: 连续记录鼓励
    streak = nudge.get("streak", 0)
    if streak > 0 and streak % 7 == 0:
        messages.append(f"你已经连续记录 {streak} 天了！这个习惯太棒了 ✨")
    elif streak == 3:
        messages.append("连续记录 3 天了~坚持下去，会看到很棒的变化！")

    return messages


# ============ F2: 主动陪伴系统 ============

def _parse_companion_datetime(time_str):
    """解析 nudge_state 中的时间字符串，返回 datetime 或 None"""
    if not time_str:
        return None
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=BEIJING_TZ)
    except Exception:
        return None


def _run_companion_check(ctx):
    """
    F2: 每 2 小时执行一次的智能陪伴检查。
    核心原则: 有事才发，没事 return None 静默跳过。
    返回: 消息文本 或 None
    """
    from memory import read_state_cached, write_state_and_update_cache
    from config import (COMPANION_SILENT_HOURS, COMPANION_INTERVAL_HOURS,
                        COMPANION_MAX_DAILY, COMPANION_RECENT_HOURS)

    state = read_state_cached(ctx) or {}
    nudge = state.get("nudge_state", {})
    now = datetime.now(BEIJING_TZ)

    # ── 防骚扰层 ──

    # 安静时间双保险（cron 已排除 0-7，代码再兜底）
    if now.hour < 8:
        _log(f"[Companion] 安静时间({now.hour}:00), 跳过")
        return None

    # 最近 N 小时内有过互动 → 不需要主动关怀
    last_msg_time = _parse_companion_datetime(nudge.get("last_message_time"))
    if last_msg_time and (now - last_msg_time).total_seconds() < COMPANION_RECENT_HOURS * 3600:
        _log(f"[Companion] 近期有互动({nudge.get('last_message_time')}), 跳过")
        return None

    # 上次陪伴推送距今不足 N 小时 → 跳过
    last_companion = _parse_companion_datetime(nudge.get("last_companion_time"))
    if last_companion and (now - last_companion).total_seconds() < COMPANION_INTERVAL_HOURS * 3600:
        _log(f"[Companion] 推送间隔不足({nudge.get('last_companion_time')}), 跳过")
        return None

    # 今天已推送 ≥ N 次 → 停止
    companion_count = nudge.get("companion_count_today", 0)
    if companion_count >= COMPANION_MAX_DAILY:
        _log(f"[Companion] 今日已推送{companion_count}次, 达到上限, 跳过")
        return None

    # ── 信号收集 ──
    signals = []

    # 信号 1: 长时间沉默（超过 N 小时没消息）
    if last_msg_time:
        silent_hours = (now - last_msg_time).total_seconds() / 3600
        if silent_hours > COMPANION_SILENT_HOURS:
            signals.append({
                "type": "silence",
                "detail": f"已经 {silent_hours:.0f} 小时没消息"
            })

    # 信号 2: 待办提醒
    pending_todos = _check_pending_todos(ctx)
    if pending_todos:
        signals.append({
            "type": "todo_reminder",
            "detail": f"有 {len(pending_todos)} 个待办未完成",
            "items": pending_todos[:3]
        })

    # 信号 3: 情绪跟进（昨天情绪低落且今天还没跟进）
    yesterday_mood = nudge.get("yesterday_mood_score")
    mood_followed = nudge.get("mood_followed_today", False)
    if yesterday_mood and int(yesterday_mood) <= 4 and not mood_followed:
        signals.append({
            "type": "mood_followup",
            "detail": f"昨天情绪评分 {yesterday_mood}/10"
        })

    # ── 决策: 没有信号就静默 ──
    if not signals:
        _log(f"[Companion] 无触发信号, 静默跳过")
        return None

    _log(f"[Companion] 触发信号: {json.dumps(signals, ensure_ascii=False)[:200]}")

    # ── 收集上下文，生成关怀消息 ──
    context = _build_companion_context(state, ctx)
    message = _generate_companion_message(signals, context, state)

    if message:
        # 更新计数器
        nudge["last_companion_time"] = now.strftime("%Y-%m-%d %H:%M")
        nudge["companion_count_today"] = companion_count + 1
        if any(s["type"] == "mood_followup" for s in signals):
            nudge["mood_followed_today"] = True
        state["nudge_state"] = nudge
        write_state_and_update_cache(state, ctx)
        _log(f"[Companion] 消息已生成, 计数={companion_count + 1}")

    return message


def _build_companion_context(state, ctx):
    """
    F2: 为陪伴消息收集丰富上下文（memory + 速记 + 待办 + 近期对话）。
    并发读取，控制总耗时。
    """
    from concurrent.futures import ThreadPoolExecutor

    context = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            "memory": executor.submit(ctx.IO.read_text, ctx.memory_file),
            "quick_notes": executor.submit(ctx.IO.read_text, ctx.quick_notes_file),
            "todo": executor.submit(ctx.IO.read_text, ctx.todo_file),
        }
        for key, future in futures.items():
            try:
                content = future.result(timeout=5)
                if content:
                    if key == "quick_notes":
                        lines = content.strip().split('\n')
                        recent = lines[-20:] if len(lines) > 20 else lines
                        context[key] = '\n'.join(recent)
                    else:
                        context[key] = content
            except Exception as e:
                _log(f"[Companion] 读取 {key} 失败: {e}")

    # 近期对话（从 state 中取）
    recent_msgs = state.get("recent_messages", [])
    if recent_msgs:
        context["recent_messages"] = recent_msgs[-5:]

    return context


def _generate_companion_message(signals, context, state):
    """
    F2: 基于信号 + 上下文，调 Qwen Flash 生成自然的关怀消息。
    注入 soul + memory + 近期速记，让消息更有温度和个性。
    """
    import prompts as _prompts

    # 组装 system prompt
    system_parts = []

    # 1. Soul（人设）— 从 prompts 模块取
    system_parts.append(f"## 你的人设\n{_prompts.SOUL}")

    # 2. Memory（长期记忆）
    memory = context.get("memory", "")
    if memory:
        system_parts.append(f"## 你对用户的了解\n{memory}")

    # 3. 任务指令 — 从 prompts 模块取
    system_parts.append(_prompts.COMPANION_TASK)

    system_prompt = '\n\n'.join(system_parts)

    # 组装 user message
    user_parts = []

    # 触发信号
    signal_text = json.dumps(signals, ensure_ascii=False)
    user_parts.append(f"**触发信号**: {signal_text}")

    # 近期速记
    quick_notes = context.get("quick_notes", "")
    if quick_notes:
        user_parts.append(f"**近期速记**:\n{quick_notes}")

    # 待办列表
    todo = context.get("todo", "")
    if todo:
        user_parts.append(f"**待办清单**:\n{todo}")

    # 近期对话
    recent_msgs = context.get("recent_messages", [])
    if recent_msgs:
        msg_text = '\n'.join([f"- {m.get('role','')}: {m.get('text','')[:80]}"
                              for m in recent_msgs])
        user_parts.append(f"**最近对话**:\n{msg_text}")

    # 当前时间
    now = datetime.now(BEIJING_TZ)
    period = "上午" if now.hour < 12 else ("下午" if now.hour < 18 else "晚上")
    user_parts.append(f"**当前时间**: {now.strftime('%Y-%m-%d %H:%M')} {period}")

    user_message = '\n\n'.join(user_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]

    _log(f"[Companion] 调用 Flash 生成关怀消息, signals={len(signals)}")
    return brain.call_llm(messages, model_tier="flash", max_tokens=200,
                          temperature=0.7)


def _check_pending_todos(ctx):
    """F2: 从 Todo.md 读取未完成待办"""
    try:
        todo_content = ctx.IO.read_text(ctx.todo_file)
        if not todo_content:
            return []
        pending = []
        for line in todo_content.split('\n'):
            line = line.strip()
            if line.startswith('- [ ]'):
                pending.append(line[5:].strip())
        return pending
    except Exception as e:
        _log(f"[Companion] 读取待办失败: {e}")
        return []


# ============ V3-F13: 天气信息流辅助函数 ============

def _build_weather_context():
    """
    V3-F13: 获取天气信息，供 morning_report 注入。
    使用心知天气 API（免费版），返回 dict 或空 dict。
    """
    if not WEATHER_API_KEY:
        return {}
    try:
        resp = requests.get(
            "https://api.seniverse.com/v3/weather/daily.json",
            params={
                "key": WEATHER_API_KEY,
                "location": WEATHER_CITY,
                "language": "zh-Hans",
                "unit": "c",
                "start": 0,
                "days": 1
            },
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()["results"][0]["daily"][0]
            weather = {
                "city": WEATHER_CITY,
                "weather_day": data.get("text_day", ""),
                "weather_night": data.get("text_night", ""),
                "high": data.get("high", ""),
                "low": data.get("low", ""),
            }
            _log(f"[Weather] {WEATHER_CITY}: {weather['weather_day']} {weather['low']}~{weather['high']}°C")
            return weather
        else:
            _log(f"[Weather] API 返回非 200: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        _log(f"[Weather] 获取天气失败: {e}")
    return {}


# ============ V9: 智能调度引擎（精确性改造） ============

def _add_minutes(time_str, minutes):
    """给 HH:MM 格式的时间加减分钟数，返回 HH:MM（V9: 委托 time_utils）"""
    return _add_minutes_v9(time_str, minutes)


def _generate_daily_intents(state):
    """V9: 基于用户节奏画像动态生成当天触达意图队列"""
    sched = state.get("scheduler", {})
    rhythm = sched.get("user_rhythm", {})
    now = datetime.now(BEIJING_TZ)
    is_weekend = now.weekday() >= 5

    wake_time = rhythm.get("avg_wake_time", SCHEDULER_DEFAULT_WAKE)
    sleep_time = rhythm.get("avg_sleep_time", SCHEDULER_DEFAULT_SLEEP)

    if is_weekend:
        shift = rhythm.get("weekend_shift", SCHEDULER_WEEKEND_SHIFT)
        wake_time = _add_minutes(wake_time, shift)

    intents = [
        {
            "type": "morning_report",
            "earliest": wake_time,
            "latest": _add_minutes(wake_time, 150),
            "ideal": _add_minutes(wake_time, 30),
            "priority": "normal",
            "status": "pending"
        },
        # todo_remind 已改为独立的 1 分钟心跳驱动（todo_remind_tick），不再走意图队列
        {
            "type": "companion",
            "earliest": _add_minutes(wake_time, 120),
            "latest": _add_minutes(sleep_time, -60),
            "ideal": None,
            "priority": "low",
            "max_times": 2,
            "sent_count": 0,
            "conditions": {"silent_hours": 4},
            "status": "pending"
        },
        {
            "type": "nudge_check",
            "earliest": "13:00",
            "latest": "15:00",
            "ideal": "14:00",
            "priority": "low",
            "status": "pending"
        },
        {
            "type": "evening_checkin",
            "earliest": _add_minutes(sleep_time, -120),
            "latest": _add_minutes(sleep_time, -30),
            "ideal": _add_minutes(sleep_time, -90),
            "priority": "normal",
            "status": "pending"
        },
        {
            "type": "daily_report",
            "earliest": _add_minutes(sleep_time, -90),
            "latest": _add_minutes(sleep_time, -15),
            "ideal": _add_minutes(sleep_time, -60),
            "priority": "normal",
            "status": "pending"
        },
    ]

    _log(f"[V9] 生成每日意图: wake={wake_time}, sleep={sleep_time}, "
         f"weekend={is_weekend}, intents={len(intents)}")
    return intents


def _daily_init(uid, ctx):
    """V9: 每日初始化（多用户版）— 生成当天意图队列 + 重置计数器"""
    from memory import write_state_and_update_cache
    # 绕过缓存直接读文件，防止缓存中旧的 _init_date 导致重复初始化
    state = ctx.IO.read_json(ctx.state_file) or {}
    sched = state.setdefault("scheduler", {})
    now = datetime.now(BEIJING_TZ)
    today_str = now.strftime("%Y-%m-%d")

    if sched.get("_init_date") == today_str:
        _log(f"[V9][{uid}] daily_init 今天已执行，跳过")
        return {"skipped": True, "date": today_str}

    # 额外防重复：仅当 _init_date 是今天时，检查意图队列是否已在执行中
    # （跨天场景下旧意图应被新意图覆盖，不阻止初始化）
    existing_intents = sched.get("intents", [])
    old_init_date = sched.get("_init_date", "")
    if old_init_date == today_str and existing_intents and any(i.get("status") not in ("pending", None) for i in existing_intents):
        _log(f"[V9][{uid}] daily_init 检测到已有执行中/已完成的意图队列，跳过覆盖")
        return {"skipped": True, "reason": "intents_already_active"}

    intents = _generate_daily_intents(state)

    # 过期意图标记 skipped（容器重启等场景）
    # 【修复】对 morning_report 增加补发窗口：即使超过 latest，
    # 在 latest+120min 内仍保留为 pending（允许延迟补发），避免晨报因服务延迟启动而永久跳过
    now_min = now.hour * 60 + now.minute
    for intent in intents:
        latest = intent.get("latest", "23:59")
        latest_min = safe_parse_hhmm(latest, -1)
        if latest_min < 0:
            continue
        if now_min > latest_min:
            intent_type = intent.get("type", "")
            # morning_report 和 daily_report 允许延迟补发窗口（120分钟）
            if intent_type in ("morning_report", "daily_report"):
                grace_min = latest_min + 120
                if now_min <= grace_min:
                    # 在补发窗口内：调整 latest 为当前时间+5分钟，让下一个 tick 立即触发
                    new_latest = format_minutes(now_min + 5)
                    intent["latest"] = new_latest
                    intent["ideal"] = now.strftime("%H:%M")  # 立即可触发
                    intent["_trigger_reason"] = f"延迟补发（原 latest={latest}, 现={new_latest}）"
                    _log(f"[V9][{uid}] 意图 {intent_type} 延迟补发: latest {latest} → {new_latest}")
                    continue  # 保持 pending 状态
            intent["status"] = "skipped"
            intent["_skip_reason"] = f"初始化时已过期（now={now.strftime('%H:%M')} > latest={latest}）"
            _log(f"[V9][{uid}] 意图 {intent['type']} 已过期，标记 skipped")

    sched["intents"] = intents
    sched["_init_date"] = today_str
    sched["_push_count_today"] = 0
    sched["_last_push_time"] = None

    state["scheduler"] = sched
    write_state_and_update_cache(state, ctx)

    _log(f"[V9][{uid}] daily_init 完成: {len(intents)} 个意图已生成")
    return {"date": today_str, "intents_count": len(intents)}


def _check_push_limits(sched, now_min):
    """V9: 检查推送限制（每日上限 + 最小间隔）

    Returns:
        (can_push: bool, reason: str or None)
    """
    push_count = sched.get("_push_count_today", 0)
    if push_count >= SCHEDULER_PUSH_MAX_DAILY:
        return False, "daily_limit"

    last_push = sched.get("_last_push_time")
    if last_push:
        last_min = safe_parse_hhmm(last_push, -1)
        if last_min >= 0 and now_min - last_min < SCHEDULER_MIN_PUSH_GAP:
            return False, "min_gap"

    return True, None


def _revive_skipped_intents(intents, now_min, now, uid):
    """V9: 对 skipped 的 morning_report/daily_report 做二次补发

    场景：daily_init 在超过 120 分钟宽限窗口后才执行（如服务器中午重启），
    导致 morning_report 被标记为 skipped。这里在 tick 中做二次补救：
    如果 latest+180min 内且今天从未推送过该类型，重置为 pending。
    """
    for intent in intents:
        if intent.get("status") != "skipped":
            continue
        intent_type = intent.get("type", "")
        if intent_type not in ("morning_report", "daily_report"):
            continue
        # 检查是否已推送过（sent 状态说明已成功执行）
        if any(i.get("type") == intent_type and i.get("status") == "sent" for i in intents):
            continue
        # 计算 latest 的宽限边界（latest + 180 分钟）
        original_latest = intent.get("_original_latest") or intent.get("latest", "23:59")
        lat_min = safe_parse_hhmm(original_latest, -1)
        if lat_min < 0:
            continue
        grace_deadline = lat_min + 180  # 3 小时宽限
        if now_min <= grace_deadline:
            # 重置为 pending，调整时间窗口让 _rule_evaluate 立即放行
            intent["status"] = "pending"
            intent["_original_latest"] = original_latest
            intent["latest"] = format_minutes(now_min + 5)
            intent["ideal"] = now.strftime("%H:%M")
            intent["_trigger_reason"] = f"tick 二次补发（原 skipped, latest={original_latest}）"
            _log(f"[V9][{uid}] {intent_type} 被补发: skipped → pending (grace_deadline={grace_deadline}min)")


def _scheduler_tick(uid, ctx):
    """V9: 每 5 分钟心跳（多用户版）— 检查到期意图并执行

    V9 改进：
    - 心跳从 30 分钟缩短到 5 分钟，大幅降低推送延迟
    - 时间计算统一使用 time_utils
    - 拆分 _check_push_limits / _revive_skipped_intents 职责更清晰
    - 消除 _execute_intent 的 HTTP 回环，直接函数调用
    """
    from memory import write_state_and_update_cache
    # 绕过缓存读最新 state，防止缓存中旧的意图状态导致重复执行
    state = ctx.IO.read_json(ctx.state_file) or {}
    sched = state.setdefault("scheduler", {})
    now = datetime.now(BEIJING_TZ)
    now_str = now.strftime("%H:%M")
    now_full = now.strftime("%Y-%m-%d %H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")

    # 兜底初始化
    if sched.get("_init_date") != today_str:
        _log(f"[V9][{uid}] tick 检测到未初始化，触发 daily_init")
        _daily_init(uid, ctx)
        # 重新读取（daily_init 已写入文件）
        state = ctx.IO.read_json(ctx.state_file) or {}
        sched = state.get("scheduler", {})

    intents = sched.get("intents", [])
    now_min = now.hour * 60 + now.minute

    # 二次补发 skipped 的重要意图
    _revive_skipped_intents(intents, now_min, now, uid)

    pending = [i for i in intents if i.get("status") == "pending"]

    if not pending:
        return {"evaluated": 0, "executed": 0}

    # 检查推送限制
    can_push, limit_reason = _check_push_limits(sched, now_min)
    if not can_push:
        _log(f"[V9][{uid}] tick: 推送受限 ({limit_reason})")
        return {"evaluated": len(pending), "executed": 0, "reason": limit_reason}

    # 规则评估
    ready = []
    for intent in pending:
        action = _rule_evaluate(intent, state, now)
        if action == "send":
            ready.append(intent)
        elif action == "skip":
            intent["status"] = "skipped"
            intent["_skip_reason"] = "rule_skip"

    if not ready:
        write_state_and_update_cache(state, ctx)
        return {"evaluated": len(pending), "executed": 0}

    if len(ready) > 1:
        ready = _try_merge_intents(ready)

    # 执行到期意图
    push_count = sched.get("_push_count_today", 0)
    executed = 0
    for intent in ready:
        if push_count + executed >= SCHEDULER_PUSH_MAX_DAILY:
            break
        try:
            intent["_tick_at"] = now_full  # P1-改造6: 记录 tick 评估时间
            _execute_intent(intent, uid, ctx=ctx)
            intent["status"] = "sent"
            intent["_sent_at"] = now_str
            executed += 1
        except Exception as e:
            _log(f"[V9][{uid}] 意图执行失败 {intent['type']}: {e}")
            intent["_error"] = str(e)

    sched["_push_count_today"] = push_count + executed
    if executed > 0:
        sched["_last_push_time"] = now_str

    # 重新读取最新 state，避免覆盖子 action（如 todo_remind）的 state 更新
    if executed > 0:
        fresh_state = ctx.IO.read_json(ctx.state_file) or {}
        fresh_sched = fresh_state.setdefault("scheduler", {})
        fresh_sched["intents"] = sched["intents"]
        fresh_sched["_push_count_today"] = sched["_push_count_today"]
        fresh_sched["_last_push_time"] = sched.get("_last_push_time")
        write_state_and_update_cache(fresh_state, ctx)
    else:
        write_state_and_update_cache(state, ctx)
    _log(f"[V9][{uid}] tick 完成: 评估 {len(pending)}, 执行 {executed}")
    return {"evaluated": len(pending), "executed": executed}


def _rule_evaluate(intent, state, now):
    """V9 Layer 1: 规则引擎 — 返回 "send" | "skip" | "wait"

    V9: 使用 safe_parse_hhmm 替代散落的 split(":") 时间解析。
    """
    intent_type = intent.get("type", "")
    now_min = now.hour * 60 + now.minute

    earliest = intent.get("earliest", "00:00")
    latest = intent.get("latest", "23:59")
    ideal = intent.get("ideal")

    earliest_min = safe_parse_hhmm(earliest, -1)
    latest_min = safe_parse_hhmm(latest, 1440)
    ideal_min = safe_parse_hhmm(ideal, -1) if ideal else -1

    if earliest_min < 0:
        return "wait"  # 时间格式异常，不触发

    if now_min < earliest_min:
        return "wait"

    if now_min >= latest_min:
        intent["_trigger_reason"] = "兜底触发（已到 latest）"
        return "send"

    sched = state.get("scheduler", {})
    rhythm = sched.get("user_rhythm", {})
    avg_wake = rhythm.get("avg_wake_time", SCHEDULER_DEFAULT_WAKE)
    wake_min = safe_parse_hhmm(avg_wake, 0)
    if now_min < wake_min:
        return "wait"

    if intent_type in ("companion", "nudge_check"):
        nudge = state.get("nudge_state", {})
        last_msg = nudge.get("last_message_time", "")
        if last_msg:
            try:
                last_dt = datetime.strptime(last_msg, "%Y-%m-%d %H:%M")
                last_dt = last_dt.replace(tzinfo=BEIJING_TZ)
                if (now - last_dt).total_seconds() < 1800:
                    return "wait"
            except Exception:
                pass

    if intent_type == "companion":
        conditions = intent.get("conditions", {})
        silent_hours = conditions.get("silent_hours", 4)
        nudge = state.get("nudge_state", {})
        last_msg = nudge.get("last_message_time", "")
        if last_msg:
            try:
                last_dt = datetime.strptime(last_msg, "%Y-%m-%d %H:%M")
                last_dt = last_dt.replace(tzinfo=BEIJING_TZ)
                hours_silent = (now - last_dt).total_seconds() / 3600
                if hours_silent < silent_hours:
                    return "wait"
            except Exception:
                pass

    max_times = intent.get("max_times")
    if max_times and intent.get("sent_count", 0) >= max_times:
        return "skip"

    if ideal_min >= 0 and now_min >= ideal_min:
        intent["_trigger_reason"] = "到达 ideal 时间"
        return "send"

    if ideal_min < 0:
        if intent_type == "companion":
            intent["_trigger_reason"] = "沉默条件满足"
            return "send"
        return "wait"

    return "wait"


_MERGEABLE = {
    ("evening_checkin", "daily_report"),
    # todo_remind 已独立为 todo_remind_tick（1分钟心跳），不再走意图队列，移除合并规则
}


def _try_merge_intents(intents):
    """V9: 尝试合并相近的意图"""
    types = set(i["type"] for i in intents)
    consumed = set()
    for pair in _MERGEABLE:
        if pair[0] in types and pair[1] in types:
            consumed.add(pair[1])

    merged = []
    for intent in intents:
        if intent["type"] in consumed:
            intent["status"] = "merged"
            _log(f"[V9] 意图合并: {intent['type']} 被合并")
        else:
            merged.append(intent)
    return merged


def _execute_intent(intent, user_id=None, ctx=None):
    """V9: 分发执行一个到期意图 — 直接函数调用（消除 HTTP 回环）

    V8 中通过 POST /system 回调自己，导致：
    1. 一个 tick 占用两个 Flask 线程（排队阻塞风险）
    2. HTTP 往返额外延迟 + timeout 风险
    V9 改为直接调用 _run_system_action_for_user()，省去一次 HTTP 回环。
    """
    intent_type = intent.get("type", "")
    scheduled_at = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    _log(f"[V9] 执行意图: {intent_type}, user={user_id}, "
         f"reason={intent.get('_trigger_reason', 'N/A')}, scheduled_at={scheduled_at}")

    action_map = {
        "morning_report": "morning_report",
        # todo_remind 已独立为 todo_remind_tick，不再由意图队列驱动
        "companion": "companion_check",
        "nudge_check": "nudge_check",
        "evening_checkin": "evening_checkin",
        "daily_report": "daily_report",
    }

    action = action_map.get(intent_type)
    if not action:
        _log(f"[V9] 未知意图类型: {intent_type}")
        return

    try:
        # V9: 直接函数调用，不再通过 HTTP 回环
        if ctx and user_id:
            result = _run_system_action_for_user(action, {}, user_id, ctx)
            delivered_at = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
            intent["_scheduled_at"] = scheduled_at
            intent["_delivered_at"] = delivered_at
            _log(f"[V9] 意图执行完成 {intent_type}: user={user_id}, "
                 f"result={result}, delivered_at={delivered_at}")
        else:
            # 降级：无 ctx 时仍走 HTTP（兼容旧调用路径）
            _log(f"[V9] 降级: {intent_type} 无 ctx, 回退 HTTP 调用")
            payload = {"action": action}
            if user_id:
                payload["user_id"] = user_id
            requests.post(
                f"http://127.0.0.1:{SERVER_PORT}/system",
                json=payload,
                timeout=120
            )
    except Exception as e:
        _log(f"[V9] 意图执行失败 {intent_type}: {e}")
        raise


# ============ 启动兜底：首次 daily_init（去重 + 延迟） ============

# 标记文件路径（每天只执行一次启动兜底 daily_init）
_DAILY_INIT_MARKER = os.path.join(
    os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")),
    "_textagent_system", ".daily_init_marker"
)


def _startup_daily_init():
    """启动时兜底触发一次 daily_init（带去重 + 延迟）

    去重逻辑：用 marker 文件记录上次执行日期，同一天内重启不重复执行。
    延迟逻辑：等待 15 秒让 Flask 完全就绪后再触发。
    """
    time.sleep(15)  # 等 Flask 就绪

    # ── 去重：检查今天是否已执行过 ──
    today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    try:
        if os.path.exists(_DAILY_INIT_MARKER):
            with open(_DAILY_INIT_MARKER, "r") as f:
                last_date = f.read().strip()
            if last_date == today_str:
                _log(f"[Startup] daily_init 今天已执行过({last_date})，跳过")
                return
    except Exception:
        pass  # marker 读取失败不影响执行

    # ── 执行 daily_init ──
    try:
        _log("[Startup] 兜底触发 daily_init ...")
        url = f"http://127.0.0.1:{SERVER_PORT}/system"
        resp = requests.post(url, json={"action": "daily_init"}, timeout=600)
        if resp.status_code == 200:
            _log("[Startup] daily_init 兜底执行成功")
            # 写入 marker
            os.makedirs(os.path.dirname(_DAILY_INIT_MARKER), exist_ok=True)
            with open(_DAILY_INIT_MARKER, "w") as f:
                f.write(today_str)
        else:
            _log(f"[Startup] daily_init 兜底异常: HTTP {resp.status_code}")
    except Exception as e:
        _log(f"[Startup] daily_init 兜底失败: {e}")


# ============ 启动初始化 ============

def _init_system_dirs():
    """启动时确保系统级目录存在"""
    os.makedirs(SYSTEM_DIR, exist_ok=True)
    _log(f"[Init] 系统目录已就绪: {SYSTEM_DIR}")


_app_initialized = False  # 防止 gunicorn 多 worker fork 时重复初始化

def _do_init():
    """执行一次性启动初始化（渠道注册、兜底 daily_init 等）"""
    global _app_initialized
    if _app_initialized:
        return
    _app_initialized = True

    _init_system_dirs()

    # ── 渠道注册 ──
    _log(f"[Init] 渠道: wework")
    channel_router.register_channel("wework", send_wework_message)
    _log("[Init] 自定义菜单同步已禁用")

    # ── 兜底 daily_init（异步，带去重+延迟）──
    threading.Thread(target=_startup_daily_init, daemon=True).start()
    _log("[Init] 调度模式: ECS crontab 外部驱动（已移除 APScheduler）")


def create_app():
    """Flask 应用工厂 — 供 gunicorn 调用。

    用法:
        gunicorn "app:create_app()"
        # 或
        gunicorn --factory app:create_app

    注意: 由于当前架构大量使用模块级全局变量（_msg_cache, _wework_token 等），
    app 对象在模块加载时已创建。此函数仅负责触发初始化逻辑并返回已有的 app。
    Phase 3 模块化拆分后将改为真正的工厂模式。
    """
    _do_init()
    return app


if __name__ == '__main__':
    _do_init()
    app.run(host='0.0.0.0', port=SERVER_PORT, threaded=True)
