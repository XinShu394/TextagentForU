# -*- coding: utf-8 -*-
"""
Skill: daily.generate
每日待办提醒：读取用户待办列表，生成当天和近期的待办提醒。
支持 report_style 个性化风格（有 style 时走 AI 生成，无则纯规则）。
"""
import sys
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def execute(params, state, ctx):
    """
    生成今日待办提醒。

    params:
        date: str — 可选，指定日期 YYYY-MM-DD，默认今天
    """
    date_str = (params.get("date") or "").strip()
    if not date_str:
        date_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    _log(f"[daily.generate] 生成 {date_str} 待办提醒")

    # 1. 读取待办列表
    todo_content = ctx.IO.read_text(ctx.todo_file) or ""

    if not todo_content.strip():
        _log("[daily.generate] 无待办事项")
        return {"success": True, "reply": f"📋 {date_str} 待办提醒\n\n目前没有待办事项，今天自由安排~"}

    # 2. 解析待办
    pending_todos = []
    done_todos = []
    for line in todo_content.split("\n"):
        line = line.strip()
        if line.startswith("- [ ]"):
            pending_todos.append(line[5:].strip())
        elif line.startswith("- [x]") or line.startswith("- [X]"):
            done_todos.append(line[5:].strip())

    # 3. 检查用户报告风格偏好
    report_style = ctx.get_report_style() if hasattr(ctx, "get_report_style") else ""

    if report_style:
        # 有个性化风格 → 走 AI 生成
        _log(f"[daily.generate] 使用 AI 生成（report_style={report_style[:30]}）")
        ai_reply = _ai_daily_report(date_str, pending_todos, done_todos, report_style)
        if ai_reply:
            return {"success": True, "reply": ai_reply}
        _log("[daily.generate] AI 生成失败，降级到纯规则")

    # 4. 纯规则构建提醒消息（无 style 或 AI 降级）
    lines = [f"📋 今日待办提醒 ({date_str})\n"]

    if pending_todos:
        lines.append(f"📌 待完成 ({len(pending_todos)} 项)：")
        for i, todo in enumerate(pending_todos, 1):
            lines.append(f"  {i}. {todo}")
        lines.append("")

    if done_todos:
        lines.append(f"✅ 已完成 ({len(done_todos)} 项)")
        lines.append("")

    if not pending_todos:
        lines.append("🎉 所有待办都完成了，今天继续加油！")

    reply = "\n".join(lines)

    return {"success": True, "reply": reply}


def _ai_daily_report(date_str, pending_todos, done_todos, report_style):
    """根据 report_style 用 AI 生成个性化日报"""
    try:
        from brain import call_llm

        pending_text = "\n".join(f"- {t}" for t in pending_todos[:15]) if pending_todos else "无"
        done_text = "\n".join(f"- {t}" for t in done_todos[:10]) if done_todos else "无"

        prompt = f"""你是用户的 AI 管家。请根据以下数据生成一段今日待办提醒（直接输出文本，不要 JSON）。

日期：{date_str}

待完成（{len(pending_todos)} 项）：
{pending_text}

已完成（{len(done_todos)} 项）：
{done_text}

用户对报告风格的偏好：
{report_style}

请严格按照用户偏好的风格来生成报告。如果用户偏好简洁，就精简输出；如果用户偏好温暖鼓励，就增加情感元素。
总字数控制在 300 字以内，不要用"亲""宝"等称呼。"""

        response = call_llm([
            {"role": "user", "content": prompt}
        ], model_tier="main", max_tokens=400, temperature=0.5)

        return response
    except Exception as e:
        _log(f"[daily.generate] AI 生成失败: {e}")
        return None


# Skill 热加载注册表（O-010）
SKILL_REGISTRY = {
    "daily.generate": execute,
}
