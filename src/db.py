# -*- coding: utf-8 -*-
"""
TextAgentForU SQLite 存储层（Phase 1）

设计原则：
  1. WAL 模式 — 支持并发读 + 串行写，单文件部署友好
  2. 渐进迁移 — 提供与 user_context.py 相同的函数签名，逐步替换
  3. Markdown 保留 — Quick-Notes / Todo.md / memory.md 继续用文件
  4. 向后兼容 — 老数据自动迁移，降级开关可回退到文件存储

使用方式：
  from db import db
  user = db.get_or_create_user("ChenTian")
  db.increment_message_count("ChenTian")
"""
import os
import sys
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

_BEIJING_TZ = timezone(timedelta(hours=8))


def _log(msg):
    ts = datetime.now(_BEIJING_TZ).strftime("%H:%M:%S")
    print(f"{ts} [DB] {msg}", file=sys.stderr, flush=True)


# ============ 数据库路径 ============

_project_root = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_project_root, "data"))
SYSTEM_DIR = os.path.join(DATA_DIR, "_textagent_system")
DB_PATH = os.path.join(SYSTEM_DIR, "textagent.db")

# 不活跃天数阈值
INACTIVE_DAYS_THRESHOLD = int(os.environ.get("INACTIVE_DAYS_THRESHOLD", "7"))
# 每日消息上限
DAILY_MESSAGE_LIMIT = int(os.environ.get("DAILY_MESSAGE_LIMIT", "50"))


# ============ 连接管理 ============

class Database:
    """SQLite 连接管理器 — 线程安全，WAL 模式

    每个线程维护独立连接（SQLite 的连接不能跨线程共享）。
    使用 WAL 模式实现并发读 + 串行写。
    """

    def __init__(self, db_path: str = None):
        self._db_path = db_path or DB_PATH
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")   # 10 秒等锁
            conn.execute("PRAGMA synchronous=NORMAL")    # WAL 模式下安全且快
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self):
        """事务上下文管理器 — 自动 commit/rollback"""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def execute(self, sql: str, params=None):
        """执行单条 SQL（自动提交）"""
        conn = self._get_conn()
        cursor = conn.execute(sql, params or ())
        conn.commit()
        return cursor

    def executemany(self, sql: str, params_list):
        """批量执行"""
        conn = self._get_conn()
        cursor = conn.executemany(sql, params_list)
        conn.commit()
        return cursor

    def fetchone(self, sql: str, params=None):
        """查询单行"""
        conn = self._get_conn()
        return conn.execute(sql, params or ()).fetchone()

    def fetchall(self, sql: str, params=None):
        """查询多行"""
        conn = self._get_conn()
        return conn.execute(sql, params or ()).fetchall()

    def ensure_tables(self):
        """创建所有表（幂等）"""
        with self._init_lock:
            if self._initialized:
                return
            conn = self._get_conn()
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            self._initialized = True
            _log(f"数据库初始化完成: {self._db_path}")

    def close(self):
        """关闭当前线程的连接"""
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None


# ============ 表结构定义 ============

_SCHEMA_SQL = """
-- 1. 用户注册表（替代 users.json）
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    nickname TEXT DEFAULT '',
    status TEXT DEFAULT 'active',     -- active / suspended
    role TEXT DEFAULT 'user',
    channel TEXT DEFAULT 'wework',
    created_at TEXT NOT NULL,
    last_active TEXT NOT NULL,
    message_count_today INTEGER DEFAULT 0,
    message_count_date TEXT DEFAULT '',
    total_messages INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active);

-- 2. Web 令牌（替代 tokens.json）
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expire_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_tokens_expire ON tokens(expire_at);

-- 3. 邀请码（替代 invite_codes.json）
CREATE TABLE IF NOT EXISTS invite_codes (
    code TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    created_by TEXT DEFAULT 'admin',
    used INTEGER DEFAULT 0,           -- 0=未使用, 1=已使用
    used_by TEXT DEFAULT '',
    used_at TEXT DEFAULT ''
);

-- 4. 公告（替代 announcements.json）
CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 5. 用户反馈（替代 feedbacks.json）
CREATE TABLE IF NOT EXISTS feedbacks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reply TEXT DEFAULT '',
    replied_at TEXT DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_feedbacks_user ON feedbacks(user_id);

-- 6. 决策日志（替代 decisions.jsonl — 追加写入 → INSERT）
CREATE TABLE IF NOT EXISTS decision_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    input_type TEXT DEFAULT '',
    action TEXT DEFAULT '',
    skill TEXT DEFAULT '',
    has_memory_updates INTEGER DEFAULT 0,
    has_reply INTEGER DEFAULT 0,
    elapsed_s REAL,
    request_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_decisions_user_ts ON decision_logs(user_id, ts);

-- 7. LLM 用量日志（替代 usage_log.jsonl）
CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id TEXT NOT NULL,
    model_tier TEXT DEFAULT '',
    model TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    latency_s REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_logs(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_logs(ts);

-- 8. 审计日志
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    user_id TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    success INTEGER DEFAULT 1,
    details TEXT DEFAULT '{}'
);

-- 版本管理（用于未来迁移）
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT DEFAULT ''
);
INSERT OR IGNORE INTO schema_version (version, applied_at, description)
VALUES (1, datetime('now'), 'Initial schema: users, tokens, invite_codes, announcements, feedbacks, decision_logs, usage_logs');
"""


# ============ 辅助函数 ============

def _now_str() -> str:
    return datetime.now(_BEIJING_TZ).isoformat(timespec="seconds")


def _today_str() -> str:
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d")


# ============ 用户管理 API（替代 user_context.py 的 users.json 操作）============

class UserManager:
    """用户注册表管理 — 直接替换 user_context.py 中的 _read_registry/_write_registry"""

    def __init__(self, database: Database):
        self.db = database

    def get_or_create_user(self, user_id: str) -> tuple:
        """获取或创建用户。返回 (user_data: dict, is_new: bool)"""
        row = self.db.fetchone(
            "SELECT * FROM users WHERE user_id = ?", (user_id,))

        if row is None:
            # 新用户
            now = _now_str()
            today = _today_str()
            self.db.execute(
                """INSERT INTO users (user_id, nickname, status, role, channel,
                   created_at, last_active, message_count_today, message_count_date, total_messages)
                   VALUES (?, '', 'active', 'user', 'wework', ?, ?, 0, ?, 0)""",
                (user_id, now, now, today))
            _log(f"新用户注册: {user_id}")
            return self._row_to_dict_default(user_id, now, today), True
        else:
            # 更新活跃时间 + 重置跨天计数
            today = _today_str()
            if dict(row).get("message_count_date") != today:
                self.db.execute(
                    """UPDATE users SET last_active = ?, message_count_today = 0,
                       message_count_date = ? WHERE user_id = ?""",
                    (_now_str(), today, user_id))
            else:
                self.db.execute(
                    "UPDATE users SET last_active = ? WHERE user_id = ?",
                    (_now_str(), user_id))
            return dict(row), False

    def increment_message_count(self, user_id: str) -> tuple:
        """增加今日消息计数。返回 (current_count, is_over_limit)"""
        today = _today_str()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT message_count_today, message_count_date FROM users WHERE user_id = ?",
                (user_id,)).fetchone()
            if not row:
                _log(f"increment_message_count: 用户 {user_id} 不存在")
                return 0, False

            count_today = dict(row).get("message_count_today", 0)
            count_date = dict(row).get("message_count_date", "")

            if count_date != today:
                count_today = 0

            count_today += 1
            conn.execute(
                """UPDATE users SET message_count_today = ?,
                   message_count_date = ?, total_messages = total_messages + 1
                   WHERE user_id = ?""",
                (count_today, today, user_id))

        return count_today, count_today > DAILY_MESSAGE_LIMIT

    def get_all_active_users(self) -> list:
        """获取所有活跃用户 ID（定时任务用）"""
        threshold = datetime.now(_BEIJING_TZ) - timedelta(days=INACTIVE_DAYS_THRESHOLD)
        threshold_str = threshold.isoformat(timespec="seconds")
        rows = self.db.fetchall(
            """SELECT user_id FROM users
               WHERE status = 'active' AND last_active >= ?""",
            (threshold_str,))
        return [dict(r)["user_id"] for r in rows]

    def get_all_users(self) -> dict:
        """获取所有用户数据（管理员用）"""
        rows = self.db.fetchall("SELECT * FROM users")
        return {dict(r)["user_id"]: dict(r) for r in rows}

    def update_user_status(self, user_id: str, status: str):
        self.db.execute(
            "UPDATE users SET status = ? WHERE user_id = ?",
            (status, user_id))

    def update_user_nickname(self, user_id: str, nickname: str):
        self.db.execute(
            "UPDATE users SET nickname = ? WHERE user_id = ?",
            (nickname, user_id))

    def is_user_suspended(self, user_id: str) -> bool:
        row = self.db.fetchone(
            "SELECT status FROM users WHERE user_id = ?", (user_id,))
        return dict(row).get("status") == "suspended" if row else False

    def delete_user(self, user_id: str) -> bool:
        """从数据库中删除用户（级联删除令牌等）"""
        try:
            self.db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            _log(f"已从数据库删除用户: {user_id}")
            return True
        except Exception as e:
            _log(f"删除用户失败: {e}")
            return False

    @staticmethod
    def _row_to_dict_default(user_id, now, today):
        return {
            "user_id": user_id,
            "nickname": "",
            "status": "active",
            "role": "user",
            "channel": "wework",
            "created_at": now,
            "last_active": now,
            "message_count_today": 0,
            "message_count_date": today,
            "total_messages": 0,
        }


# ============ 令牌管理 API（替代 user_context.py 的 tokens.json 操作）============

class TokenManager:
    def __init__(self, database: Database):
        self.db = database

    def generate_token(self, user_id: str, expire_hours: int = 24) -> str:
        from config import WEB_TOKEN_EXPIRE_HOURS
        if expire_hours == 24:
            expire_hours = WEB_TOKEN_EXPIRE_HOURS

        token = str(uuid.uuid4())
        now = datetime.now(_BEIJING_TZ)
        expire_at = now + timedelta(hours=expire_hours)

        self.db.execute(
            "INSERT INTO tokens (token, user_id, created_at, expire_at) VALUES (?, ?, ?, ?)",
            (token, user_id,
             now.isoformat(timespec="seconds"),
             expire_at.isoformat(timespec="seconds")))

        _log(f"生成令牌: user={user_id}, token={token[:8]}..., "
             f"expire={expire_at.isoformat(timespec='seconds')}")
        return token

    def verify_token(self, token: str) -> dict:
        if not token:
            return {"valid": False}

        row = self.db.fetchone(
            "SELECT user_id, expire_at FROM tokens WHERE token = ?", (token,))
        if not row:
            return {"valid": False}

        data = dict(row)
        try:
            expire_at = datetime.fromisoformat(data["expire_at"])
            now = datetime.now(_BEIJING_TZ)
            if now > expire_at:
                _log(f"令牌已过期: {token[:8]}..., expire_at={data['expire_at']}")
                return {"valid": False, "expired": True}
        except (ValueError, KeyError):
            return {"valid": False}

        return {"valid": True, "user_id": data["user_id"]}

    def cleanup_expired_tokens(self) -> int:
        now = _now_str()
        cursor = self.db.execute(
            "DELETE FROM tokens WHERE expire_at < ?", (now,))
        removed = cursor.rowcount
        if removed > 0:
            _log(f"清理过期令牌: {removed} 个")
        return removed

    def delete_user_tokens(self, user_id: str):
        self.db.execute(
            "DELETE FROM tokens WHERE user_id = ?", (user_id,))


# ============ 邀请码管理 ============

class InviteCodeManager:
    def __init__(self, database: Database):
        self.db = database

    def create_invite_code(self, created_by: str = "admin") -> str:
        import random
        import string
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        self.db.execute(
            "INSERT INTO invite_codes (code, created_at, created_by) VALUES (?, ?, ?)",
            (code, _now_str(), created_by))
        _log(f"生成邀请码: {code}")
        return code

    def get_all_invite_codes(self) -> list:
        rows = self.db.fetchall("SELECT * FROM invite_codes ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def use_invite_code(self, code: str, user_id: str) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT used FROM invite_codes WHERE code = ?", (code,)).fetchone()
            if not row or dict(row).get("used"):
                return False
            conn.execute(
                "UPDATE invite_codes SET used = 1, used_by = ?, used_at = ? WHERE code = ?",
                (user_id, _now_str(), code))
        _log(f"邀请码 {code} 被 {user_id} 使用")
        return True

    def delete_invite_code(self, code: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM invite_codes WHERE code = ?", (code,))
        return cursor.rowcount > 0


# ============ 公告管理 ============

class AnnouncementManager:
    def __init__(self, database: Database):
        self.db = database

    def create_announcement(self, title: str, content: str) -> dict:
        ann_id = str(uuid.uuid4())[:8]
        now = _now_str()
        self.db.execute(
            "INSERT INTO announcements (id, title, content, created_at) VALUES (?, ?, ?, ?)",
            (ann_id, title, content, now))
        return {"id": ann_id, "title": title, "content": content, "created_at": now}

    def get_announcements(self) -> list:
        rows = self.db.fetchall("SELECT * FROM announcements ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def delete_announcement(self, ann_id: str) -> bool:
        cursor = self.db.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
        return cursor.rowcount > 0


# ============ 反馈管理 ============

class FeedbackManager:
    def __init__(self, database: Database):
        self.db = database

    def create_feedback(self, user_id: str, content: str) -> dict:
        fb_id = str(uuid.uuid4())[:8]
        now = _now_str()
        self.db.execute(
            "INSERT INTO feedbacks (id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (fb_id, user_id, content, now))
        return {"id": fb_id, "user_id": user_id, "content": content,
                "created_at": now, "reply": "", "replied_at": ""}

    def get_feedbacks(self) -> list:
        rows = self.db.fetchall("SELECT * FROM feedbacks ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def reply_feedback(self, fb_id: str, reply: str) -> bool:
        cursor = self.db.execute(
            "UPDATE feedbacks SET reply = ?, replied_at = ? WHERE id = ?",
            (reply, _now_str(), fb_id))
        return cursor.rowcount > 0


# ============ 决策日志 ============

class DecisionLogManager:
    def __init__(self, database: Database):
        self.db = database

    def write_decision_log(self, user_id: str, ts: str, input_type: str = "",
                           action: str = "", skill: str = "",
                           has_memory_updates: bool = False,
                           has_reply: bool = False, elapsed_s: float = None,
                           request_id: str = ""):
        self.db.execute(
            """INSERT INTO decision_logs
               (user_id, ts, input_type, action, skill, has_memory_updates, has_reply, elapsed_s, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, ts, input_type, action, skill,
             1 if has_memory_updates else 0,
             1 if has_reply else 0,
             elapsed_s, request_id))

    def get_recent_decisions(self, user_id: str = None, limit: int = 50) -> list:
        if user_id:
            rows = self.db.fetchall(
                "SELECT * FROM decision_logs WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
                (user_id, limit))
        else:
            rows = self.db.fetchall(
                "SELECT * FROM decision_logs ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


# ============ LLM 用量日志 ============

class UsageLogManager:
    def __init__(self, database: Database):
        self.db = database

    def log_usage(self, user_id: str, model_tier: str, model: str,
                  prompt_tokens: int, completion_tokens: int,
                  total_tokens: int, latency_s: float):
        ts = datetime.now(_BEIJING_TZ).isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO usage_logs
               (ts, user_id, model_tier, model, prompt_tokens, completion_tokens, total_tokens, latency_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, user_id, model_tier, model,
             prompt_tokens, completion_tokens, total_tokens,
             round(latency_s, 1)))

    def get_monthly_cost(self, month_str: str = None) -> float:
        """计算指定月份的 API 成本（元）"""
        if not month_str:
            month_str = datetime.now(_BEIJING_TZ).strftime("%Y-%m")

        rows = self.db.fetchall(
            """SELECT model, SUM(prompt_tokens) as pt, SUM(completion_tokens) as ct
               FROM usage_logs WHERE ts LIKE ? GROUP BY model""",
            (f"{month_str}%",))

        total_cost = 0.0
        for row in rows:
            r = dict(row)
            m = (r.get("model") or "").lower()
            pt = r.get("pt") or 0
            ct = r.get("ct") or 0
            if "deepseek" in m:
                total_cost += pt / 1e6 * 2 + ct / 1e6 * 8
            elif "vl" in m:
                total_cost += pt / 1e6 * 3 + ct / 1e6 * 9
        return total_cost

    def get_today_stats(self) -> dict:
        """获取今日用量统计"""
        today = _today_str()
        rows = self.db.fetchall(
            """SELECT model_tier, COUNT(*) as calls,
                      SUM(prompt_tokens) as pt, SUM(completion_tokens) as ct,
                      AVG(latency_s) as avg_latency
               FROM usage_logs WHERE ts LIKE ? GROUP BY model_tier""",
            (f"{today}%",))
        return {dict(r)["model_tier"]: dict(r) for r in rows}


# ============ 审计日志 ============

class AuditLogManager:
    def __init__(self, database: Database):
        self.db = database

    def log_audit(self, action: str, user_id: str = "",
                  success: bool = True, details: dict = None):
        self.db.execute(
            "INSERT INTO audit_logs (action, user_id, timestamp, success, details) VALUES (?, ?, ?, ?, ?)",
            (action, user_id, _now_str(),
             1 if success else 0,
             json.dumps(details or {}, ensure_ascii=False)))


# ============ 数据迁移：从 JSON 文件导入到 SQLite ============

class DataMigrator:
    """从旧的 JSON 文件存储迁移数据到 SQLite"""

    def __init__(self, database: Database):
        self.db = database

    def migrate_all(self):
        """执行全部迁移（幂等，可重复执行）"""
        _log("开始数据迁移...")
        self._migrate_users()
        self._migrate_tokens()
        self._migrate_invite_codes()
        self._migrate_announcements()
        self._migrate_feedbacks()
        self._migrate_usage_log()
        _log("数据迁移完成")

    def _migrate_users(self):
        users_file = os.path.join(SYSTEM_DIR, "users.json")
        if not os.path.exists(users_file):
            return
        try:
            with open(users_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            users = data.get("users", {})
            migrated = 0
            for uid, info in users.items():
                existing = self.db.fetchone(
                    "SELECT user_id FROM users WHERE user_id = ?", (uid,))
                if existing:
                    continue
                self.db.execute(
                    """INSERT INTO users
                       (user_id, nickname, status, role, channel,
                        created_at, last_active, message_count_today,
                        message_count_date, total_messages)
                       VALUES (?, ?, ?, 'user', 'wework', ?, ?, ?, ?, ?)""",
                    (uid,
                     info.get("nickname", ""),
                     info.get("status", "active"),
                     info.get("created_at", _now_str()),
                     info.get("last_active", _now_str()),
                     info.get("message_count_today", 0),
                     info.get("message_count_date", ""),
                     info.get("total_messages", 0)))
                migrated += 1
            if migrated:
                _log(f"迁移用户: {migrated} 个")
        except Exception as e:
            _log(f"用户迁移失败: {e}")

    def _migrate_tokens(self):
        tokens_file = os.path.join(SYSTEM_DIR, "tokens.json")
        if not os.path.exists(tokens_file):
            return
        try:
            with open(tokens_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            tokens = data.get("tokens", {})
            migrated = 0
            for token_str, info in tokens.items():
                existing = self.db.fetchone(
                    "SELECT token FROM tokens WHERE token = ?", (token_str,))
                if existing:
                    continue
                self.db.execute(
                    "INSERT INTO tokens (token, user_id, created_at, expire_at) VALUES (?, ?, ?, ?)",
                    (token_str,
                     info.get("user_id", ""),
                     info.get("created_at", _now_str()),
                     info.get("expire_at", _now_str())))
                migrated += 1
            if migrated:
                _log(f"迁移令牌: {migrated} 个")
        except Exception as e:
            _log(f"令牌迁移失败: {e}")

    def _migrate_invite_codes(self):
        codes_file = os.path.join(SYSTEM_DIR, "invite_codes.json")
        if not os.path.exists(codes_file):
            return
        try:
            with open(codes_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            codes = data.get("codes", [])
            migrated = 0
            for c in codes:
                code = c.get("code", "")
                if not code:
                    continue
                existing = self.db.fetchone(
                    "SELECT code FROM invite_codes WHERE code = ?", (code,))
                if existing:
                    continue
                self.db.execute(
                    """INSERT INTO invite_codes
                       (code, created_at, created_by, used, used_by, used_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (code,
                     c.get("created_at", _now_str()),
                     c.get("created_by", "admin"),
                     1 if c.get("used") else 0,
                     c.get("used_by", ""),
                     c.get("used_at", "")))
                migrated += 1
            if migrated:
                _log(f"迁移邀请码: {migrated} 个")
        except Exception as e:
            _log(f"邀请码迁移失败: {e}")

    def _migrate_announcements(self):
        ann_file = os.path.join(SYSTEM_DIR, "announcements.json")
        if not os.path.exists(ann_file):
            return
        try:
            with open(ann_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            anns = data.get("announcements", [])
            migrated = 0
            for a in anns:
                ann_id = a.get("id", "")
                if not ann_id:
                    continue
                existing = self.db.fetchone(
                    "SELECT id FROM announcements WHERE id = ?", (ann_id,))
                if existing:
                    continue
                self.db.execute(
                    "INSERT INTO announcements (id, title, content, created_at) VALUES (?, ?, ?, ?)",
                    (ann_id, a.get("title", ""), a.get("content", ""),
                     a.get("created_at", _now_str())))
                migrated += 1
            if migrated:
                _log(f"迁移公告: {migrated} 个")
        except Exception as e:
            _log(f"公告迁移失败: {e}")

    def _migrate_feedbacks(self):
        fb_file = os.path.join(SYSTEM_DIR, "feedbacks.json")
        if not os.path.exists(fb_file):
            return
        try:
            with open(fb_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            fbs = data.get("feedbacks", [])
            migrated = 0
            for fb in fbs:
                fb_id = fb.get("id", "")
                if not fb_id:
                    continue
                existing = self.db.fetchone(
                    "SELECT id FROM feedbacks WHERE id = ?", (fb_id,))
                if existing:
                    continue
                self.db.execute(
                    """INSERT INTO feedbacks
                       (id, user_id, content, created_at, reply, replied_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (fb_id, fb.get("user_id", ""), fb.get("content", ""),
                     fb.get("created_at", _now_str()),
                     fb.get("reply", ""), fb.get("replied_at", "")))
                migrated += 1
            if migrated:
                _log(f"迁移反馈: {migrated} 个")
        except Exception as e:
            _log(f"反馈迁移失败: {e}")

    def _migrate_usage_log(self):
        """迁移 usage_log.jsonl → usage_logs 表"""
        usage_file = os.path.join(SYSTEM_DIR, "usage_log.jsonl")
        if not os.path.exists(usage_file):
            return
        try:
            # 检查是否已迁移过（看表里有无数据）
            row = self.db.fetchone("SELECT COUNT(*) as cnt FROM usage_logs")
            if row and dict(row).get("cnt", 0) > 0:
                return  # 已有数据，跳过

            migrated = 0
            batch = []
            with open(usage_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        batch.append((
                            e.get("ts", ""),
                            e.get("user_id", ""),
                            e.get("model_tier", ""),
                            e.get("model", ""),
                            e.get("prompt_tokens", 0),
                            e.get("completion_tokens", 0),
                            e.get("total_tokens", 0),
                            e.get("latency_s", 0),
                        ))
                        if len(batch) >= 500:
                            self.db.executemany(
                                """INSERT INTO usage_logs
                                   (ts, user_id, model_tier, model, prompt_tokens,
                                    completion_tokens, total_tokens, latency_s)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                batch)
                            migrated += len(batch)
                            batch = []
                    except (json.JSONDecodeError, KeyError):
                        continue
            if batch:
                self.db.executemany(
                    """INSERT INTO usage_logs
                       (ts, user_id, model_tier, model, prompt_tokens,
                        completion_tokens, total_tokens, latency_s)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    batch)
                migrated += len(batch)
            if migrated:
                _log(f"迁移用量日志: {migrated} 条")
        except Exception as e:
            _log(f"用量日志迁移失败: {e}")


# ============ 全局单例 ============

# 降级开关：设置环境变量 TEXTAGENT_DB_DISABLE=1 可回退到文件存储
_DB_DISABLED = os.environ.get("TEXTAGENT_DB_DISABLE", "").lower() in ("1", "true", "yes")

if _DB_DISABLED:
    _log("⚠️ SQLite 已通过 TEXTAGENT_DB_DISABLE 禁用，使用文件存储")
    db = None
    users = None
    tokens = None
    invite_codes = None
    announcements = None
    feedbacks = None
    decision_logs = None
    usage_logs = None
    audit_logs = None
    migrator = None
else:
    db = Database()
    db.ensure_tables()

    users = UserManager(db)
    tokens = TokenManager(db)
    invite_codes = InviteCodeManager(db)
    announcements = AnnouncementManager(db)
    feedbacks = FeedbackManager(db)
    decision_logs = DecisionLogManager(db)
    usage_logs = UsageLogManager(db)
    audit_logs = AuditLogManager(db)
    migrator = DataMigrator(db)

    # 首次启动自动迁移
    try:
        migrator.migrate_all()
    except Exception as e:
        _log(f"自动迁移失败（不影响运行）: {e}")
