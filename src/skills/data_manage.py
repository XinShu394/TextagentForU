# -*- coding: utf-8 -*-
"""
Skill: data.*
用户数据管理：导出和销毁
"""
import sys
import json
import os
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _now_str():
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def export_user_data(params, state, ctx):
    """
    导出用户所有数据，以结构化文本形式返回。
    便于用户迁移数据到其他 AI 平台。
    
    导出范围:
    - 用户配置信息（昵称、AI名、偏好等）
    - 长期记忆（memory.md）
    - 快速笔记（Quick-Notes.md）
    - 待办事项（state 中的 todos）
    - 各类归档笔记（读书、影视、情感、生活趣事等）
    - 日报记录
    """
    try:
        _log(f"[data.export] 开始导出用户数据: {ctx.user_id}")
        
        parts = []
        parts.append("# 📦 TextAgent 数据导出")
        parts.append(f"导出时间: {_now_str()}")
        parts.append(f"用户 ID: {ctx.user_id}")
        parts.append("")
        
        # 1. 用户配置
        config = ctx.get_user_config()
        parts.append("## 👤 用户配置")
        parts.append(f"- 昵称: {config.get('nickname', '未设置')}")
        parts.append(f"- AI 名字: {config.get('ai_name', 'TextAgent')}")
        parts.append(f"- 存储模式: {config.get('storage_mode', 'local')}")
        
        # 偏好设置
        prefs = config.get('preferences', {})
        if prefs:
            parts.append("- 偏好设置:")
            for k, v in prefs.items():
                parts.append(f"  - {k}: {v}")
        
        # 用户信息
        info = config.get('info', {})
        if info:
            parts.append("- 个人信息:")
            for k, v in info.items():
                parts.append(f"  - {k}: {v}")
        parts.append("")
        
        # 2. 长期记忆
        parts.append("## 🧠 长期记忆")
        memory_content = ctx.IO.read_text(ctx.memory_file)
        if memory_content:
            # 截断过长内容
            if len(memory_content) > 10000:
                memory_content = memory_content[:10000] + "\n\n...(内容过长，已截断)..."
            parts.append(memory_content)
        else:
            parts.append("(暂无长期记忆)")
        parts.append("")
        
        # 3. 快速笔记
        parts.append("## 📝 快速笔记")
        notes_content = ctx.IO.read_text(ctx.quick_notes_file)
        if notes_content:
            if len(notes_content) > 15000:
                notes_content = notes_content[:15000] + "\n\n...(内容过长，已截断)..."
            parts.append(notes_content)
        else:
            parts.append("(暂无快速笔记)")
        parts.append("")
        
        # 4. 待办事项
        parts.append("## ✅ 待办事项")
        todos = state.get("todos", [])
        if todos:
            parts.append("### 进行中")
            for i, t in enumerate(todos, 1):
                content = t.get("content", "")
                recur = t.get("recur", "")
                remind = t.get("remind_at", "")
                due = t.get("due_date", "")
                
                line = f"{i}. {content}"
                if recur:
                    line += f" 🔁 {recur}"
                if remind:
                    line += f" ⏰ {remind}"
                if due:
                    line += f" 📅 {due}"
                parts.append(line)
        
        # 从 Todo.md 获取已完成的待办
        todo_content = ctx.IO.read_text(ctx.todo_file)
        if todo_content and "## 已完成" in todo_content:
            done_section = todo_content.split("## 已完成")[1]
            done_lines = [l for l in done_section.split("\n") if l.strip().startswith("- [x]")]
            if done_lines:
                parts.append("\n### 已完成")
                for line in done_lines[:20]:  # 最多显示 20 条
                    parts.append(line)
                if len(done_lines) > 20:
                    parts.append(f"...(共 {len(done_lines)} 条)")
        
        if not todos and not (todo_content and "- [x]" in todo_content):
            parts.append("(暂无待办事项)")
        parts.append("")
        
        # 5. 归档笔记
        parts.append("## 📂 归档笔记")
        
        # 遍历 02-Notes 目录
        notes_dir = os.path.join(ctx.user_dir, "02-Notes") if ctx.storage_mode == "local" else "02-Notes"
        
        categories = [
            ("读书笔记", "📚"),
            ("影视笔记", "🎬"),
            ("工作笔记", "💼"),
            ("情感日记", "💭"),
            ("生活趣事", "🎉"),
            ("语音日记", "🎤"),
        ]
        
        total_chars = len("\n".join(parts))
        max_total_chars = 50000  # 总导出限制
        
        for cat_name, cat_emoji in categories:
            if total_chars > max_total_chars:
                parts.append(f"\n(内容已达上限，剩余分类已省略)")
                break
            
            cat_dir = os.path.join(notes_dir, cat_name) if ctx.storage_mode == "local" else f"{notes_dir}/{cat_name}"
            
            try:
                if ctx.storage_mode == "local":
                    if os.path.exists(cat_dir):
                        files = [f for f in os.listdir(cat_dir) if f.endswith('.md')]
                    else:
                        files = []
                else:
                    # OneDrive 模式
                    files = ctx.IO.list_files(cat_dir) or []
                
                if files:
                    parts.append(f"\n### {cat_emoji} {cat_name}")
                    for fname in files[:10]:  # 每类最多 10 个文件
                        fpath = os.path.join(cat_dir, fname) if ctx.storage_mode == "local" else f"{cat_dir}/{fname}"
                        content = ctx.IO.read_text(fpath)
                        if content:
                            if len(content) > 3000:
                                content = content[:3000] + "\n...(已截断)"
                            parts.append(f"\n#### {fname}")
                            parts.append(content)
                            total_chars += len(content)
                            if total_chars > max_total_chars:
                                break
                    
                    if len(files) > 10:
                        parts.append(f"...(该分类共 {len(files)} 个文件)")
            except Exception as e:
                _log(f"[data.export] 读取 {cat_name} 失败: {e}")
        
        parts.append("")
        
        # 6. 日报（最近 7 天）
        parts.append("## 📊 近期日报")
        daily_dir = os.path.join(ctx.user_dir, "01-Daily") if ctx.storage_mode == "local" else "01-Daily"
        
        try:
            if ctx.storage_mode == "local":
                if os.path.exists(daily_dir):
                    daily_files = sorted([f for f in os.listdir(daily_dir) if f.endswith('.md')], reverse=True)[:7]
                else:
                    daily_files = []
            else:
                daily_files = sorted(ctx.IO.list_files(daily_dir) or [], reverse=True)[:7]
            
            if daily_files:
                for fname in daily_files:
                    fpath = os.path.join(daily_dir, fname) if ctx.storage_mode == "local" else f"{daily_dir}/{fname}"
                    content = ctx.IO.read_text(fpath)
                    if content:
                        if len(content) > 2000:
                            content = content[:2000] + "\n...(已截断)"
                        parts.append(f"\n### {fname}")
                        parts.append(content)
            else:
                parts.append("(暂无日报记录)")
        except Exception as e:
            _log(f"[data.export] 读取日报失败: {e}")
            parts.append("(读取日报失败)")
        
        parts.append("")
        parts.append("---")
        parts.append("导出完成。如需完整数据备份，请联系管理员。")
        
        result_text = "\n".join(parts)
        _log(f"[data.export] 导出完成: {len(result_text)} 字符")
        
        return {
            "success": True,
            "reply": result_text
        }
        
    except Exception as e:
        _log(f"[data.export] 导出失败: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {
            "success": False,
            "reply": f"数据导出失败: {str(e)}"
        }


def destroy_user_data(params, state, ctx):
    """
    销毁用户所有数据。需要二次确认机制。
    
    params:
        confirm: bool - 用户是否已确认销毁
    
    流程:
    1. 第一次调用（无 confirm）：设置 pending_destroy，提示用户确认
    2. 用户确认后再次调用（confirm=true）：执行实际删除
    """
    from config import ADMIN_USER_ID
    
    # 安全检查：管理员账号不允许自我销毁
    if ctx.user_id == ADMIN_USER_ID:
        return {
            "success": False,
            "reply": "管理员账号不支持自我销毁，请通过其他方式管理数据。"
        }
    
    confirm = params.get("confirm", False)
    pending = state.get("pending_destroy", {})
    
    if not confirm:
        # 第一次调用：设置待确认状态
        _log(f"[data.destroy] 用户请求销毁数据: {ctx.user_id}")
        
        state["pending_destroy"] = {
            "pending": True,
            "requested_at": _now_str(),
        }
        
        return {
            "success": True,
            "reply": (
                "⚠️ 你确定要销毁所有数据吗？\n\n"
                "这将永久删除：\n"
                "- 你的所有笔记和待办\n"
                "- 长期记忆和日报\n"
                "- 用户配置和偏好\n\n"
                "此操作**不可恢复**！\n\n"
                "如果确定要继续，请回复「确认销毁」或「确认删除」。\n"
                "5 分钟内有效，超时需重新申请。"
            ),
            "state_updates": {"pending_destroy": state["pending_destroy"]}
        }
    
    # 第二次调用：检查确认状态
    if not pending.get("pending"):
        return {
            "success": False,
            "reply": "没有待处理的销毁请求。如需销毁数据，请先说「删除我的所有数据」。"
        }
    
    # 检查是否超时（5 分钟）
    requested_at = pending.get("requested_at", "")
    if requested_at:
        try:
            req_time = datetime.strptime(requested_at, "%Y-%m-%d %H:%M:%S")
            req_time = req_time.replace(tzinfo=BEIJING_TZ)
            now = datetime.now(BEIJING_TZ)
            if (now - req_time).total_seconds() > 300:
                state["pending_destroy"] = {}
                return {
                    "success": False,
                    "reply": "确认超时，请重新发起销毁请求。",
                    "state_updates": {"pending_destroy": {}}
                }
        except Exception as e:
            _log(f"[data.destroy] 时间解析失败: {e}")
    
    # 执行销毁
    _log(f"[data.destroy] 开始销毁用户数据: {ctx.user_id}")
    
    try:
        from user_context import delete_user
        
        # 记录到审计日志
        _log(f"[data.destroy] 审计日志: user_id={ctx.user_id}, action=destroy, time={_now_str()}")
        
        # 执行删除
        success = delete_user(ctx.user_id)
        
        if success:
            _log(f"[data.destroy] 用户数据已销毁: {ctx.user_id}")
            
            # 清除状态
            state.clear()
            
            return {
                "success": True,
                "reply": (
                    "👋 你的所有数据已被销毁。\n\n"
                    "感谢你使用 TextAgent，希望我曾给你带来帮助。\n"
                    "如果未来想要回来，随时欢迎~\n\n"
                    "再见，保重！💙"
                )
            }
        else:
            return {
                "success": False,
                "reply": "数据销毁过程中出现问题，请联系管理员处理。"
            }
            
    except Exception as e:
        _log(f"[data.destroy] 销毁失败: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {
            "success": False,
            "reply": f"数据销毁失败: {str(e)}，请联系管理员。"
        }


# Skill 热加载注册表
SKILL_REGISTRY = {
    "data.export": export_user_data,
    "data.destroy": destroy_user_data,
}
