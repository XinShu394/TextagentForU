# -*- coding: utf-8 -*-
"""
KarvisForAll 用户数据管理
提供数据导出和数据销毁功能。
"""
import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

_BEIJING_TZ = timezone(timedelta(hours=8))


def _log(msg):
    ts = datetime.now(_BEIJING_TZ).strftime("%H:%M:%S")
    print(f"{ts} {msg}", file=sys.stderr, flush=True)


# ============ 数据导出 ============

# 单文件最大读取字符数
_MAX_FILE_CHARS = 5000
# 总导出最大字符数
_MAX_TOTAL_CHARS = 50000


def _read_text_safe(io_backend, path, max_chars=_MAX_FILE_CHARS):
    """安全读取文本文件，超长截断"""
    try:
        content = io_backend.read_text(path)
        if content and len(content) > max_chars:
            content = content[:max_chars] + f"\n\n... (已截断，原文共 {len(content)} 字符)"
        return content or ""
    except Exception as e:
        _log(f"[DataManage] 读取文件失败 {path}: {e}")
        return ""


def _collect_notes_from_dir(io_backend, dir_path, label):
    """从笔记目录收集所有 .md 文件内容"""
    sections = []
    try:
        if not os.path.isdir(dir_path):
            return ""
        for filename in sorted(os.listdir(dir_path)):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(dir_path, filename)
            content = _read_text_safe(io_backend, filepath, max_chars=2000)
            if content:
                sections.append(f"### {filename}\n{content}")
    except Exception as e:
        _log(f"[DataManage] 遍历目录失败 {dir_path}: {e}")
    if sections:
        return f"\n## {label}\n\n" + "\n\n---\n\n".join(sections)
    return ""


def export_user_data(params, state, ctx):
    """
    导出用户所有数据，以结构化文本形式返回。
    """
    _log(f"[DataManage] 开始导出用户数据: {ctx.user_id}")

    parts = []
    total_chars = 0

    # 1. 用户配置信息
    config = ctx.get_user_config()
    config_text = (
        f"# 📋 用户数据导出\n\n"
        f"**导出时间**: {datetime.now(_BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"**用户 ID**: {ctx.user_id}\n\n"
        f"---\n\n"
        f"## 基本信息\n\n"
        f"- **昵称**: {config.get('nickname', '未设置')}\n"
        f"- **AI 名字**: {config.get('ai_name', 'Karvis')}\n"
        f"- **渠道**: {config.get('channel', 'wework')}\n"
        f"- **个人信息**: {json.dumps(config.get('info', {}), ensure_ascii=False)}\n"
        f"- **偏好设置**: {json.dumps(config.get('preferences', {}), ensure_ascii=False)}\n"
    )
    parts.append(config_text)
    total_chars += len(config_text)

    # 2. 长期记忆
    memory = _read_text_safe(ctx.IO, ctx.memory_file)
    if memory:
        section = f"\n## 长期记忆\n\n{memory}"
        parts.append(section)
        total_chars += len(section)

    # 3. 快速笔记
    quick_notes = _read_text_safe(ctx.IO, ctx.quick_notes_file)
    if quick_notes:
        section = f"\n## 快速笔记\n\n{quick_notes}"
        parts.append(section)
        total_chars += len(section)

    # 4. 待办事项
    todo = _read_text_safe(ctx.IO, ctx.todo_file)
    if todo:
        section = f"\n## 待办事项\n\n{todo}"
        parts.append(section)
        total_chars += len(section)

    # 5. 状态数据（待办提醒、习惯实验、决策追踪等）
    try:
        state_content = ctx.IO.read_text(ctx.state_file)
        if state_content:
            state_data = json.loads(state_content)
            # 提取关键信息
            state_parts = []
            if state_data.get("active_experiment"):
                state_parts.append(f"- **活跃实验**: {json.dumps(state_data['active_experiment'], ensure_ascii=False)}")
            if state_data.get("pending_decisions"):
                state_parts.append(f"- **待复盘决策**: {json.dumps(state_data['pending_decisions'], ensure_ascii=False)}")
            if state_data.get("mood_scores"):
                scores = state_data["mood_scores"][-30:]  # 最近30天
                state_parts.append(f"- **近期情绪评分**: {json.dumps(scores, ensure_ascii=False)}")
            if state_data.get("checkin_stats"):
                state_parts.append(f"- **打卡统计**: {json.dumps(state_data['checkin_stats'], ensure_ascii=False)}")
            if state_parts:
                section = "\n## 状态数据\n\n" + "\n".join(state_parts)
                parts.append(section)
                total_chars += len(section)
    except Exception as e:
        _log(f"[DataManage] 读取状态文件失败: {e}")

    # 6. 碎碎念
    misc = _read_text_safe(ctx.IO, ctx.misc_file)
    if misc:
        section = f"\n## 碎碎念\n\n{misc}"
        parts.append(section)
        total_chars += len(section)

    # 7. 各分类笔记（如果总量还不超限）
    if total_chars < _MAX_TOTAL_CHARS:
        note_dirs = [
            (ctx.book_notes_dir, "📖 读书笔记"),
            (ctx.media_notes_dir, "🎬 影视笔记"),
            (ctx.work_notes_dir, "💼 工作笔记"),
            (ctx.emotion_notes_dir, "💝 情感日记"),
            (ctx.fun_notes_dir, "😄 生活趣事"),
            (ctx.voice_journal_dir, "🎙️ 语音日记"),
        ]
        for dir_path, label in note_dirs:
            if total_chars >= _MAX_TOTAL_CHARS:
                parts.append(f"\n\n⚠️ 数据量较大，已达到单次导出上限（{_MAX_TOTAL_CHARS} 字符）。")
                break
            section = _collect_notes_from_dir(ctx.IO, dir_path, label)
            if section:
                parts.append(section)
                total_chars += len(section)

    # 8. 日报
    if total_chars < _MAX_TOTAL_CHARS:
        daily_section = _collect_notes_from_dir(ctx.IO, ctx.daily_notes_dir, "📅 日报记录")
        if daily_section:
            parts.append(daily_section)
            total_chars += len(daily_section)

    full_text = "\n".join(parts)
    _log(f"[DataManage] 数据导出完成: {ctx.user_id}, 总字符数={len(full_text)}")

    return {
        "success": True,
        "reply": full_text,
    }


# ============ 数据销毁 ============

# 确认超时时间（秒）
_DESTROY_CONFIRM_TIMEOUT = 300  # 5 分钟


def destroy_user_data(params, state, ctx):
    """
    销毁用户所有数据。需要二次确认。
    第一次调用：设置 pending_destroy 标记，提示用户确认。
    确认后再次调用（confirm=true）：执行实际删除。
    """
    confirm = params.get("confirm", False)
    nickname = ctx.get_nickname() or "朋友"

    # 管理员不允许自我销毁
    if ctx.is_admin:
        return {
            "success": False,
            "reply": "管理员账号不支持数据销毁操作，请通过后台管理。",
        }

    if not confirm:
        # 第一次调用 — 设置确认标记
        pending = {
            "pending": True,
            "requested_at": time.time(),
        }
        return {
            "success": True,
            "reply": (
                f"⚠️ {nickname}，你确定要销毁所有数据吗？\n\n"
                f"这将永久删除以下内容：\n"
                f"• 所有笔记和记录\n"
                f"• 待办事项\n"
                f"• 长期记忆\n"
                f"• 情绪日记和打卡记录\n"
                f"• 所有个人配置\n\n"
                f"此操作不可逆❗\n\n"
                f"如果确定，请在 5 分钟内回复「确认销毁」。\n"
                f"回复其他任何内容将取消操作。"
            ),
            "state_updates": {
                "pending_destroy": pending,
            },
        }
    else:
        # 确认销毁 — 检查是否超时
        pending = state.get("pending_destroy", {})
        if not pending.get("pending"):
            return {
                "success": False,
                "reply": "没有待确认的销毁请求。如果你想销毁数据，请先说「销毁我的数据」。",
            }

        requested_at = pending.get("requested_at", 0)
        if time.time() - requested_at > _DESTROY_CONFIRM_TIMEOUT:
            return {
                "success": False,
                "reply": "确认已超时（超过 5 分钟），请重新发起销毁请求。",
                "state_updates": {"pending_destroy": {}},
            }

        # 执行删除
        _log(f"[DataManage] ⚠️ 用户 {ctx.user_id} 确认销毁数据!")

        try:
            from user_context import delete_user
            success = delete_user(ctx.user_id)
            if success:
                _log(f"[DataManage] 用户 {ctx.user_id} 数据已销毁")
                return {
                    "success": True,
                    "reply": (
                        f"再见{nickname}，你的所有数据已被彻底删除。\n\n"
                        f"如果将来想回来，随时欢迎～ 🌟\n"
                        f"（下次发消息会作为新用户重新开始）"
                    ),
                }
            else:
                return {
                    "success": False,
                    "reply": "数据删除过程中遇到了问题，请联系管理员处理。",
                }
        except Exception as e:
            _log(f"[DataManage] 销毁用户数据异常: {e}")
            return {
                "success": False,
                "reply": "数据删除过程中遇到了问题，请联系管理员处理。",
            }


# ============ Skill 注册 ============

SKILL_REGISTRY = {
    "data.export": export_user_data,
    "data.destroy": destroy_user_data,
}
