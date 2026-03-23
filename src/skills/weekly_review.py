# -*- coding: utf-8 -*-
"""
Skill: weekly.review
每周一触发：上周情绪简要总结 + 安抚/调整建议 + 本周待办提醒。
"""
import sys
import json
from datetime import datetime, timezone, timedelta


BEIJING_TZ = timezone(timedelta(hours=8))

# 用于在 execute → _ai_weekly_summary 之间传递 report_style
_report_style_cache = {"current": ""}


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def execute(params, state, ctx):
    """
    生成周总结 + 待办提醒。

    params:
        date: str — 可选，指定日期 YYYY-MM-DD，默认今天
    """
    today = datetime.now(BEIJING_TZ).date()
    # 计算上周的日期范围（上周一到上周日）
    days_since_monday = today.weekday()  # 0=Mon
    last_monday = today - timedelta(days=days_since_monday + 7)
    last_sunday = last_monday + timedelta(days=6)
    dates = [(last_monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    period_str = f"{last_monday.strftime('%m-%d')} ~ {last_sunday.strftime('%m-%d')}"

    _log(f"[weekly.review] 生成周报: {period_str}")

    # 1. 收集上周情绪数据
    mood_scores = []
    for entry in state.get("mood_scores", []):
        if entry.get("date") in dates:
            mood_scores.append(entry)

    # 2. 读取待办
    todo_content = ctx.IO.read_text(ctx.todo_file) or ""
    pending_todos = []
    for line in todo_content.split("\n"):
        line = line.strip()
        if line.startswith("- [ ]"):
            pending_todos.append(line[5:].strip())

    # 3. AI 生成周总结
    summary = _ai_weekly_summary(mood_scores, period_str, pending_todos)

    if summary:
        return {"success": True, "reply": summary}
    else:
        # 降级：手动构建
        return {"success": True, "reply": _fallback_summary(mood_scores, period_str, pending_todos)}


def _ai_weekly_summary(mood_scores, period_str, pending_todos):
    """调用 AI 生成周总结"""
    try:
        from brain import call_llm

        # 构建数据
        mood_text = ""
        if mood_scores:
            mood_lines = []
            for s in sorted(mood_scores, key=lambda x: x.get("date", "")):
                events = "、".join(s.get("events", [])) if s.get("events") else ""
                mood_lines.append(f"- {s.get('date','')}: {s.get('score','?')}/10 {s.get('label','')} {events}")
            mood_text = "\n".join(mood_lines)
        else:
            mood_text = "上周没有情绪记录"

        todo_text = ""
        if pending_todos:
            todo_text = "\n".join(f"- {t}" for t in pending_todos[:10])
        else:
            todo_text = "目前没有待办"

        prompt = f"""你是用户的 AI 管家。请根据以下数据生成一段简洁的周总结（直接输出文本，不要 JSON）。

上周（{period_str}）情绪记录：
{mood_text}

本周待办：
{todo_text}

要求：
1. 简要总结上周情绪状态（1-2句话）
2. 如果情绪偏低，给一句温暖的安抚；如果情绪不错，给一句鼓励
3. 列出本周待办提醒（如果有）
4. 语气简洁务实，像一个靠谱的管家，不要啰嗦
5. 用 emoji 分段，总字数控制在 200 字以内
6. 不要用"亲""宝"等称呼"""

        response = call_llm([
            {"role": "user", "content": prompt}
        ], model_tier="main", max_tokens=400, temperature=0.5)

        return response
    except Exception as e:
        _log(f"[weekly.review] AI 生成失败: {e}")
        return None


def _fallback_summary(mood_scores, period_str, pending_todos):
    """降级：手动构建周总结"""
    lines = [f"📅 周总结（{period_str}）\n"]

    if mood_scores:
        scores = [s.get("score", 5) for s in mood_scores if s.get("score") is not None]
        if scores:
            avg = sum(scores) / len(scores)
            lines.append(f"📊 上周情绪均分：{avg:.1f}/10（{len(scores)} 天记录）")
            if avg < 5:
                lines.append("上周看起来有点辛苦，这周对自己好一点~")
            elif avg >= 7:
                lines.append("上周状态不错，继续保持！")
            else:
                lines.append("上周状态还行，稳步前进~")
            lines.append("")
    else:
        lines.append("上周没有情绪记录\n")

    if pending_todos:
        lines.append(f"📌 本周待办（{len(pending_todos)} 项）：")
        for i, todo in enumerate(pending_todos[:5], 1):
            lines.append(f"  {i}. {todo}")
        if len(pending_todos) > 5:
            lines.append(f"  ...还有 {len(pending_todos) - 5} 项")
    else:
        lines.append("📌 本周暂无待办")

    return "\n".join(lines)


# Skill 热加载注册表
SKILL_REGISTRY = {
    "weekly.review": execute,
}
