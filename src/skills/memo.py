# -*- coding: utf-8 -*-
"""
随记系统（Memo）
用户发送"记 + 内容"即可主动保存随记，AI 自动提取标签。
支持按标签/时间查看和关键词搜索。

存储路径：02-Notes/随记/{YYYY-MM-DD}.md
标签格式：Obsidian 原生 #tag
"""
import re
import sys
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


# ============ memo.save ============

def save(params, state, ctx):
    """
    保存随记。

    params:
        content: str — 随记内容
        tags: list[str] — 可选，用户手动指定的标签
    """
    content = (params.get("content") or "").strip()
    if not content:
        return {"success": False, "reply": "没有内容可以记录哦~"}

    # 1. 提取标签：优先手动 > AI 提取
    manual_tags = params.get("tags") or []
    if not manual_tags:
        manual_tags = _extract_manual_tags(content)

    if manual_tags:
        tags = manual_tags
        # 从内容中移除手动标签，保留纯文本
        clean_content = _remove_tags_from_content(content)
    else:
        # AI 提取标签
        tags = _ai_extract_tags(content)
        clean_content = content

    if not clean_content.strip():
        clean_content = content  # 防止全部是标签导致内容为空

    # 2. 构建条目
    now = datetime.now(BEIJING_TZ)
    time_str = now.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")
    full_time = now.strftime("%Y-%m-%d %H:%M")

    tags_str = " ".join(f"#{t}" for t in tags) if tags else ""
    title = f"### {time_str} {tags_str}".strip()

    entry_parts = [title, clean_content, f"*— {full_time}*", "", "---", ""]
    entry = "\n".join(entry_parts)

    # 3. 写入文件
    file_path = f"{ctx.memo_notes_dir}/{date_str}.md"
    existing = ctx.IO.read_text(file_path)
    if existing is None:
        return {"success": False, "reply": "保存失败，稍后再试~"}

    if not existing.strip():
        existing = f"# 📝 随记 — {date_str}\n\n---\n"

    new_content = existing.rstrip() + "\n\n" + entry
    ok = ctx.IO.write_text(file_path, new_content)

    if ok:
        tags_display = " ".join(f"#{t}" for t in tags) if tags else "无标签"
        _log(f"[memo.save] 已保存: tags={tags_display}, content={clean_content[:50]}...")
        return {
            "success": True,
            "reply": f"已记下 ✏️ {tags_display}",
        }
    else:
        return {"success": False, "reply": "保存失败，稍后再试~"}


def _extract_manual_tags(content):
    """从内容中提取用户手动写的 #tag"""
    tags = re.findall(r'#([\u4e00-\u9fff\w]+)', content)
    return tags[:5]  # 最多 5 个


def _remove_tags_from_content(content):
    """从内容中移除 #tag，保留纯文本"""
    cleaned = re.sub(r'#[\u4e00-\u9fff\w]+\s*', '', content).strip()
    return cleaned


def _ai_extract_tags(content):
    """调用 Flash 模型提取标签"""
    try:
        from brain import call_llm
        prompt = (
            "从以下随记内容中提取 1-3 个分类标签（2-4字中文短词）。\n"
            '返回 JSON: {"tags": ["标签1", "标签2"]}\n'
            "只返回 JSON，不要其他内容。\n\n"
            f"内容：{content[:500]}"
        )
        response = call_llm(
            [{"role": "user", "content": prompt}],
            model_tier="flash", max_tokens=50, temperature=0
        )
        if response:
            import json
            # 容错解析
            response = response.strip()
            if response.startswith("```"):
                lines = response.split("\n")
                response = "\n".join(lines[1:-1])
            result = json.loads(response)
            tags = result.get("tags", [])
            return [str(t).strip() for t in tags if t][:3]
    except Exception as e:
        _log(f"[memo.save] AI 标签提取失败: {e}")
    return []


# ============ memo.list ============

def list_memos(params, state, ctx):
    """
    查看随记列表。

    params:
        tag: str — 可选，按标签筛选
        days: int — 可选，查看最近几天（默认 7）
    """
    tag_filter = (params.get("tag") or "").strip().lstrip("#")
    days = int(params.get("days", 7))

    now = datetime.now(BEIJING_TZ)
    entries = []

    for i in range(days):
        date = now - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        file_path = f"{ctx.memo_notes_dir}/{date_str}.md"
        content = ctx.IO.read_text(file_path)
        if not content or not content.strip():
            continue

        # 解析条目
        for block in content.split("\n---"):
            block = block.strip()
            if not block or block.startswith("# "):
                continue

            # 检查标签筛选
            if tag_filter and f"#{tag_filter}" not in block:
                continue

            # 提取第一行作为标题
            lines = block.split("\n")
            title_line = ""
            body = ""
            for line in lines:
                if line.startswith("### "):
                    title_line = line[4:].strip()
                elif not line.startswith("*—") and line.strip():
                    if not body:
                        body = line.strip()

            if title_line or body:
                preview = body[:60] + ("..." if len(body) > 60 else "") if body else ""
                entries.append(f"📝 {date_str} {title_line}\n   {preview}" if preview else f"📝 {date_str} {title_line}")

    if not entries:
        if tag_filter:
            return {"success": True, "reply": f"最近 {days} 天没有 #{tag_filter} 相关的随记~"}
        return {"success": True, "reply": f"最近 {days} 天没有随记~"}

    header = f"📝 最近 {days} 天的随记"
    if tag_filter:
        header += f"（#{tag_filter}）"
    header += f"（共 {len(entries)} 条）\n"

    reply = header + "\n".join(entries[:20])
    if len(entries) > 20:
        reply += f"\n\n...还有 {len(entries) - 20} 条"

    return {"success": True, "reply": reply}


# ============ memo.search ============

def search_memos(params, state, ctx):
    """
    搜索随记。

    params:
        keyword: str — 搜索关键词
        days: int — 可选，搜索最近几天（默认 30）
    """
    keyword = (params.get("keyword") or "").strip()
    if not keyword:
        return {"success": False, "reply": "请告诉我要搜索什么关键词~"}

    days = int(params.get("days", 30))
    now = datetime.now(BEIJING_TZ)
    results = []

    for i in range(days):
        date = now - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        file_path = f"{ctx.memo_notes_dir}/{date_str}.md"
        content = ctx.IO.read_text(file_path)
        if not content or not content.strip():
            continue

        for block in content.split("\n---"):
            block = block.strip()
            if not block or block.startswith("# "):
                continue

            if keyword.lower() in block.lower():
                # 提取摘要
                lines = [l for l in block.split("\n")
                         if l.strip() and not l.startswith("*—") and not l.startswith("### ")]
                summary = lines[0][:80] if lines else block[:80]

                # 提取标题行中的标签和时间
                title_line = ""
                for l in block.split("\n"):
                    if l.startswith("### "):
                        title_line = l[4:].strip()
                        break

                results.append(f"🔍 {date_str} {title_line}\n   {summary}")

    if not results:
        return {"success": True, "reply": f"最近 {days} 天的随记中没有找到「{keyword}」相关内容~"}

    header = f"🔍 搜索「{keyword}」（共 {len(results)} 条匹配）\n"
    reply = header + "\n".join(results[:15])
    if len(results) > 15:
        reply += f"\n\n...还有 {len(results) - 15} 条"

    return {"success": True, "reply": reply}


# ============ Skill 注册 ============

SKILL_REGISTRY = {
    "memo.save": save,
    "memo.list": list_memos,
    "memo.search": search_memos,
}
