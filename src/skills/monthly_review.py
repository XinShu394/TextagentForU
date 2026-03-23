# -*- coding: utf-8 -*-
"""
Skill: monthly.review
每月1日触发：上月情绪简要总结 + 安抚/调整建议 + 本月待办提醒。
"""
import sys
import calendar
from datetime import datetime, timezone, timedelta


BEIJING_TZ = timezone(timedelta(hours=8))

# 用于在 execute → _ai_monthly_summary 之间传递 report_style
_report_style_cache = {"current": ""}


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def execute(params, state, ctx):
    """
    生成月度总结 + 待办提醒。

    params:
        month: str — 可选，YYYY-MM 格式，默认上月
    """
    now = datetime.now(BEIJING_TZ)
    month_str = (params.get("month") or "").strip()

    if not month_str:
        # 默认回顾上月
        first_of_this_month = now.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        month_str = last_month_end.strftime("%Y-%m")

    try:
        year, month = int(month_str[:4]), int(month_str[5:7])
    except (ValueError, IndexError):
        return {"success": False, "reply": f"月份格式错误：{month_str}"}

    _, days_in_month = calendar.monthrange(year, month)
    dates = [f"{year}-{month:02d}-{d:02d}" for d in range(1, days_in_month + 1)]
    period_str = f"{year}年{month}月"

    _log(f"[monthly.review] 生成月报: {period_str}")

    # 1. 收集上月情绪数据
    mood_scores = []
    for entry in state.get("mood_scores", []):
        if entry.get("date", "")[:7] == month_str:
            mood_scores.append(entry)

    # 2. 读取待办
    todo_content = ctx.IO.read_text(ctx.todo_file) or ""
    pending_todos = []
    for line in todo_content.split("\n"):
        line = line.strip()
        if line.startswith("- [ ]"):
            pending_todos.append(line[5:].strip())

    # 3. 打卡统计
    checkin_stats = state.get("checkin_stats", {})

    # 4. AI 生成月度总结
    summary = _ai_monthly_summary(mood_scores, period_str, pending_todos, checkin_stats)

    if summary:
        return {"success": True, "reply": summary}
    else:
        return {"success": True, "reply": _fallback_summary(mood_scores, period_str, pending_todos, checkin_stats)}


def _ai_monthly_summary(mood_scores, period_str, pending_todos, checkin_stats):
    """调用 AI 生成月度总结"""
    try:
        from brain import call_llm

        mood_text = ""
        if mood_scores:
            mood_lines = []
            for s in sorted(mood_scores, key=lambda x: x.get("date", "")):
                events = "、".join(s.get("events", [])) if s.get("events") else ""
                mood_lines.append(f"- {s.get('date','')}: {s.get('score','?')}/10 {s.get('label','')} {events}")
            mood_text = "\n".join(mood_lines)
        else:
            mood_text = "上月没有情绪记录"

        todo_text = ""
        if pending_todos:
            todo_text = "\n".join(f"- {t}" for t in pending_todos[:10])
        else:
            todo_text = "目前没有待办"

        checkin_text = ""
        if checkin_stats.get("total"):
            checkin_text = f"打卡 {checkin_stats.get('total', 0)} 次，当前连续 {checkin_stats.get('streak', 0)} 天"

        prompt = f"""你是用户的 AI 管家。请根据以下数据生成一段简洁的月度总结（直接输出文本，不要 JSON）。

{period_str} 情绪记录：
{mood_text}

打卡统计：{checkin_text or '无记录'}

本月待办：
{todo_text}

要求：
1. 简要总结上月情绪走势（2-3句话），指出高点和低点
2. 给出一句安抚或调整建议
3. 列出本月待办提醒（如果有）
4. 语气简洁务实，像一个靠谱的管家
5. 用 emoji 分段，总字数控制在 300 字以内
6. 不要用"亲""宝"等称呼"""

        response = call_llm([
            {"role": "user", "content": prompt}
        ], model_tier="main", max_tokens=500, temperature=0.5)

        return response
    except Exception as e:
        _log(f"[monthly.review] AI 生成失败: {e}")
        return None


def _fallback_summary(mood_scores, period_str, pending_todos, checkin_stats):
    """降级：手动构建月度总结"""
    lines = [f"📊 月度总结（{period_str}）\n"]

    if mood_scores:
        scores = [s.get("score", 5) for s in mood_scores if s.get("score") is not None]
        if scores:
            avg = sum(scores) / len(scores)
            highest = max(scores)
            lowest = min(scores)
            lines.append(f"📈 情绪均分：{avg:.1f}/10（{len(scores)} 天记录）")
            lines.append(f"   最高 {highest}/10 · 最低 {lowest}/10")
            if avg < 5:
                lines.append("上个月有些辛苦，这个月多照顾自己~")
            elif avg >= 7:
                lines.append("上个月状态不错，继续保持！")
            else:
                lines.append("上个月整体平稳，稳步向前~")
            lines.append("")
    else:
        lines.append("上月没有情绪记录\n")

    if checkin_stats.get("total"):
        lines.append(f"🔄 打卡：累计 {checkin_stats['total']} 次，连续 {checkin_stats.get('streak', 0)} 天")
        lines.append("")

    if pending_todos:
        lines.append(f"📌 本月待办（{len(pending_todos)} 项）：")
        for i, todo in enumerate(pending_todos[:5], 1):
            lines.append(f"  {i}. {todo}")
        if len(pending_todos) > 5:
            lines.append(f"  ...还有 {len(pending_todos) - 5} 项")
    else:
        lines.append("📌 本月暂无待办")

    return "\n".join(lines)


# Skill 热加载注册表
SKILL_REGISTRY = {
    "monthly.review": execute,
}
