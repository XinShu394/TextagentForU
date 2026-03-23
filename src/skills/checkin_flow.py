# -*- coding: utf-8 -*-
"""
Skill: checkin.*
每日打卡流程：单问题"今天感觉怎么样？做了什么？"，AI 自动提取事件+情绪标签。

打卡状态字段（存在 .ai-life-state.json 中，由 brain.py 管理读写）：
    checkin_pending: bool
    checkin_sent_at: str (YYYY-MM-DD HH:MM)
    checkin_date: str (YYYY-MM-DD)
"""
import json
import sys
from datetime import datetime, timezone, timedelta


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


BEIJING_TZ = timezone(timedelta(hours=8))

CHECKIN_QUESTION = "今天感觉怎么样？做了什么？"


# ============ Skill 入口函数 ============

def start(params, state, ctx):
    """
    启动打卡流程。
    由 LLM 触发（system 消息 evening_checkin 或用户主动说"打卡"）。
    """
    if state.get("checkin_pending"):
        return {
            "success": True,
            "reply": f"打卡已在进行中~\n\n{CHECKIN_QUESTION}"
        }

    now = datetime.now(BEIJING_TZ)
    return {
        "success": True,
        "reply": f"🌙 {CHECKIN_QUESTION}",
        "state_updates": {
            "checkin_pending": True,
            "checkin_sent_at": now.strftime("%Y-%m-%d %H:%M"),
            "checkin_date": now.strftime("%Y-%m-%d")
        }
    }


def answer(params, state, ctx):
    """
    处理打卡回答。用户自由回答后，AI 自动提取事件+情绪标签。

    params:
        answer: str — 用户的回答内容
    """
    if not state.get("checkin_pending"):
        return {"success": False, "reply": "当前没有进行中的打卡"}

    answer_text = params.get("answer", "").strip()
    if not answer_text:
        return {"success": True, "reply": "回答不能为空哦~"}

    checkin_date = state.get("checkin_date",
                             datetime.now(BEIJING_TZ).strftime("%Y-%m-%d"))

    # 调用 Flash 模型提取事件和情绪标签
    tags = _extract_tags(answer_text)

    events = tags.get("events", [])
    mood = tags.get("mood", "")
    mood_score = tags.get("mood_score", 5)

    # 构建 Markdown
    lines = ["## 每日记录\n"]
    lines.append(f"**原话**: {answer_text}\n")
    if events:
        lines.append(f"**事件**: {', '.join(events)}")
    if mood:
        lines.append(f"**情绪**: {mood}")
    lines.append(f"**状态评分**: {mood_score}/10")
    lines.append("")

    checkin_content = "\n".join(lines)

    # 写入 Daily Note
    daily_note_path = f"{ctx.daily_notes_dir}/{checkin_date}.md"
    _write_to_daily_note(ctx, daily_note_path, checkin_date, checkin_content)

    # 记录 mood_scores
    scores = state.get("mood_scores", [])[:]
    scores = [s for s in scores if s.get("date") != checkin_date]
    scores.append({
        "date": checkin_date,
        "score": mood_score,
        "label": mood,
        "events": events,
        "source": "checkin"
    })

    # 更新打卡统计
    stats = state.get("checkin_stats", {"total": 0, "streak": 0, "last_checkin_date": ""}).copy()
    stats["total"] = stats.get("total", 0) + 1
    last_date = stats.get("last_checkin_date", "")
    if last_date:
        try:
            last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
            today_dt = datetime.strptime(checkin_date, "%Y-%m-%d").date()
            if (today_dt - last_dt).days == 1:
                stats["streak"] = stats.get("streak", 0) + 1
            elif (today_dt - last_dt).days > 1:
                stats["streak"] = 1
        except Exception:
            stats["streak"] = 1
    else:
        stats["streak"] = 1
    stats["last_checkin_date"] = checkin_date

    # 构建回复
    events_str = "、".join(events) if events else "记录完成"
    mood_str = f"  情绪: {mood}" if mood else ""
    reply = f"✅ 今日打卡完成！{mood_str}\n📌 {events_str}\n已保存到 {checkin_date}.md"

    return {
        "success": True,
        "reply": reply,
        "state_updates": {
            "checkin_pending": False,
            "checkin_date": "",
            "checkin_sent_at": "",
            "mood_scores": scores,
            "checkin_stats": stats
        }
    }


def cancel(params, state, ctx):
    """取消当前打卡。"""
    if not state.get("checkin_pending"):
        return {"success": True, "reply": "当前没有打卡在进行哦"}

    _log("[checkin] 取消打卡")
    return {
        "success": True,
        "reply": "❌ 已取消今日打卡",
        "state_updates": {
            "checkin_pending": False,
            "checkin_date": "",
            "checkin_sent_at": ""
        }
    }


def finish(state, ctx=None, timeout=False):
    """
    打卡超时处理（由 brain.py 超时检测调用）。
    """
    checkin_date = state.get("checkin_date",
                             datetime.now(BEIJING_TZ).strftime("%Y-%m-%d"))
    state["checkin_pending"] = False
    state["checkin_date"] = ""
    state["checkin_sent_at"] = ""

    if timeout:
        return f"⏰ 打卡超时，下次有空再来~"
    return "打卡已结束"


# ============ AI 标签提取 ============

def _extract_tags(text):
    """
    调用 Flash 模型从用户自由回答中提取事件和情绪标签。
    返回: {"events": [...], "mood": "...", "mood_score": int}
    """
    try:
        from brain import call_llm

        prompt = f"""从以下用户打卡内容中提取信息，返回严格 JSON（不要 markdown 标记）：

用户说："{text}"

返回格式：
{{"events": ["事件1", "事件2"], "mood": "2-4字情绪标签", "mood_score": 7}}

规则：
- events: 用户今天做了什么，提取为简短事件标签（2-8字）
- mood: 用户的情绪状态（如"累"、"开心"、"焦虑"、"平静"等）
- mood_score: 1-10 评分，基于内容判断（1=极差, 5=一般, 10=极好）
- 如果内容不包含明确事件，events 可以为空数组
- 如果内容不包含明确情绪，mood 填"平静"，mood_score 填 5"""

        response = call_llm([
            {"role": "user", "content": prompt}
        ], model_tier="flash", max_tokens=200, temperature=0)

        if response:
            text_resp = response.strip()
            if text_resp.startswith("```"):
                lines = text_resp.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                text_resp = "\n".join(lines).strip()
            try:
                return json.loads(text_resp)
            except Exception:
                start = text_resp.find("{")
                end = text_resp.rfind("}")
                if start >= 0 and end > start:
                    try:
                        return json.loads(text_resp[start:end + 1])
                    except Exception:
                        pass
    except Exception as e:
        _log(f"[checkin] 标签提取失败: {e}")

    # 降级：返回默认值
    return {"events": [], "mood": "平静", "mood_score": 5}


# ============ 打卡完成：写入 Daily Note ============

def _write_to_daily_note(ctx, file_path, date_str, checkin_content):
    """将打卡内容写入 Daily Note（替换或追加 ## 每日记录 section）"""
    existing = ctx.IO.read_text(file_path)

    if existing is None:
        _log(f"[checkin] 无法读取 Daily Note，尝试创建: {file_path}")
        existing = ""

    if existing:
        if "## 每日记录" in existing:
            parts = existing.split("## 每日记录")
            before = parts[0]
            after_parts = parts[1].split("\n## ", 1)
            after = "\n## " + after_parts[1] if len(after_parts) > 1 else ""
            new_content = before + checkin_content + after
        else:
            new_content = existing.rstrip() + "\n\n" + checkin_content
    else:
        new_content = f"# {date_str}\n\n{checkin_content}"

    ok = ctx.IO.write_text(file_path, new_content)
    if ok:
        _log(f"[checkin] 已写入 Daily Note: {file_path}")
    else:
        _log(f"[checkin] 写入 Daily Note 失败: {file_path}")
    return ok


# Skill 热加载注册表（O-010）
SKILL_REGISTRY = {
    "checkin.start": start,
    "checkin.answer": answer,
    "checkin.cancel": cancel,
}
