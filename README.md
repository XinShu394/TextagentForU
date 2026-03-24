# TextagentForU — 多用户 AI 生活助手

> 基于企业微信应用的 AI 生活助手，支持微信聊天置顶，支持多人共享一套部署，每人拥有独立的数据空间。
> 记速记、管待办、写日记、追情绪、养习惯、记账理财——通过对话完成一切。
>
> 内置 **管理员后台**（用户管理、LLM 用量监控）和 **用户 Web 端**（查看笔记、待办、日记、情绪等 14 个页面）。

---

## 功能一览

### 📝 日常记录
- **全类型消息存档**：文字、语音、图片、视频、链接 → 自动存到 Quick-Notes
- **智能分类归档**：自动归类到工作笔记 / 情感日记 / 生活趣事 / 碎碎念
- **随记系统（Memo）**：发送"记 + 内容"主动保存随记，AI 自动提取标签，支持按标签/关键词查看和搜索
- **链接解析**：分享链接自动抓取全文，回复基于内容理解
- **语音日记**：长语音（>30秒）自动整理为结构化日记
- **读书笔记**：书摘、感想、AI 总结、金句提炼
- **影视笔记**：影评、感想、自动填充影视信息

### ✅ 待办与习惯
- **待办管理**：自然语言增删查，支持截止日期和定时提醒，序号批量操作
- **删除 vs 完成分离**：`todo.delete` 直接删除 / `todo.done` 标记完成，不再混淆
- **意图确认（Clarify）**：意图不明确时主动向用户确认具体操作
- **待办心跳检测**：独立 1 分钟心跳，精准到分钟级的到期提醒推送
- **过期自动完成**：一次性提醒过期且已通知后，自动标注完成
- **每日 Top 3**：早报引导设定当天 3 件重要事，晚间追踪完成情况
- **微习惯实验**：基于行为模式自动提议微习惯，触发词检测，实验周期跟踪
- **决策复盘**：记录重要决策，N 天后自动提醒复盘结果

### 📊 复盘与洞察
- **每日打卡**：问题引导复盘，写入日记
- **情绪日记**：每天自动从消息中提取情绪脉络，生成分析
- **日报 / 周报 / 月报**：自动总结、情绪曲线、碎片连线、成长轨迹
- **报告风格定制**：自定义日报/周报/月报输出风格（简洁列表/情感陪伴/数据导向等），有风格时走 AI 生成
- **深度自问（Reflect）**：定期引导深度自我探索，90 天不重复
- **主题深潜**：跨时间线搜索全历史，生成深度分析报告

### 💰 财务管理（管理员功能）
- **账单导入**：支持 iCost App 导出的 xlsx 文件批量导入，自动去重
- **收支查询**：自然语言查询消费记录（按月/周/年/分类筛选）
- **财务快照**：资产/负债概览，支持多期对比和趋势分析
- **月度财报**：自动生成月度收支分析报告，含 AI 深度洞察

### 🤝 主动陪伴
- **V8 智能调度引擎**：意图队列 + 心跳驱动 + 规则引擎，每用户个性化推送时间
- **用户节奏自学习**：自动推算起床/入睡时间，滑动 7 天加权平均
- **智能关怀**：有事才发，没事不发（五层防骚扰）
- **沉默检测 / 情绪跟进 / 待办轻推**
- **时间胶囊**：早报中回顾 7天/30天/365天 前的记录

### 🛡️ 数据管理
- **数据导出**：一键汇总导出所有个人数据（配置、记忆、笔记、待办等）
- **数据销毁**：二次确认 + 5 分钟超时 + 管理员保护，彻底删除用户数据

### 🌐 Web 查看 & 管理
- **用户页面**（Token 鉴权）：概览、速记、待办、日记、笔记、情绪、记忆等
- **用户运营中心**（admin.html）：用户列表、概览卡片、挂起/激活
- **技术监控中心**（logs.html）：实时日志 + 统计监控（Token/延迟/成本/Prompt膨胀/技能热力图）+ 错误聚合（Sentry 风格）
- **企微告警推送**：慢请求连续检测 + 异常告警 + 冷却防风暴

### ⚙️ 个性化设置
- 对话中设置昵称、给 AI 起名、自定义 AI 性格
- 报告风格定制：自定义日报/周报/月报的输出风格
- Skill 开关：按需启用/禁用功能模块

---

## 核心架构

```
企业微信 → Flask 网关（解密/去重/ASR/异步转发）
    ↓
brain.process()
    ├── 加载 State + Memory（三级缓存，per-user 隔离）
    ├── 三层模型路由 → JSON 决策
    │   ├── Flash (Qwen Flash)        — 陪伴关怀、轻量生成、意图确认
    │   ├── Main  (DeepSeek V3.2)     — 日常路由、Skill 分发
    │   └── Think (DeepSeek V3.2 R1)  — 深度分析、主题深潜
    ├── V4 Flash 智能回复 → 二次生成自然语言
    ├── Agent Loop（最多 5 轮）→ 文件搜索/读取后再回答
    ├── Skill 分发 → 执行操作 → 写回数据
    └── Memory 更新 + 决策日志

V8 智能调度引擎（APScheduler 心跳驱动）
    ├── 待办心跳              （每 1 分钟，独立检查到期待办）
    ├── 智能调度心跳          （每 30 分钟，驱动以下意图）
    │   ├── 晨报              （用户节奏自适应时间）
    │   ├── 待办提醒          （意图队列触发）
    │   ├── 主动陪伴          （五层防骚扰）
    │   ├── 轻推检测          （沉默/情绪跟进）
    │   ├── 晚间签到          （打卡引导）
    │   └── 日报生成          （自适应时间）
    ├── 情绪日记 + 周期调度   （固定 cron）
    ├── 月度回顾              （每月 1 日）
    ├── 财务月报              （每月 8 日）
    └── 每日意图初始化        （05:00 生成意图队列 + 重置计数器）
```

---

## 完整 Skill 列表

共 **39 个 Skill**，分布在 20 个功能模块中：

| 分类 | Skill | 说明 |
|---|---|---|
| **笔记** | `note.save` | 保存到 Quick-Notes |
| | `classify.archive` | 智能归档（工作/情感/趣事/碎碎念） |
| **随记** | `memo.save` / `memo.list` / `memo.search` | 主动随记保存 + 标签自动提取 + 按标签查看 + 关键词搜索 |
| **打卡** | `checkin.start` / `checkin.answer` | 每日打卡复盘流程 |
| **待办** | `todo.add` / `todo.done` / `todo.delete` / `todo.list` / `todo.remind_cancel` | 待办管理（增/完成/删除/查/取消提醒，支持循环/截止日期/序号批量操作） |
| **复盘** | `daily.generate` / `weekly.review` / `monthly.review` | 日/周/月报（支持 report_style 个性化风格） |
| **深潜** | `deep.dive` | 跨时间线主题深度分析 |
| **决策** | `decision.record` / `decision.review` / `decision.list` | 决策记录 + 定期复盘 |
| **数据** | `data.export` / `data.destroy` | 数据导出（一键汇总）+ 数据销毁（二次确认） |
| **财务** 🔒 | `finance.import` / `finance.query` / `finance.snapshot` / `finance.monthly` | iCost 账单导入 / 收支查询 / 资产快照 / 月度财报 |
| **语音** | `voice.journal` | 长语音自动整理为结构化日记 |
| **动态引擎** | `dynamic` | 直接操作 state 字段（带安全白名单） |
| **Agent** | `internal.read` / `internal.search` / `internal.list` | 文件搜索/读取（Agent Loop 信息检索） |
| **设置** | `settings.nickname` / `settings.ai_name` / `settings.soul` / `settings.info` / `settings.skills` / `settings.report_style` | 个性化设置 + Skill 开关 + 报告风格定制 |
| **Web** | `web.token` | 生成 Web 查看链接 |

> 🔒 标记的 Skill 仅管理员可用（visibility: private）

---

## 快速开始

### 准备工作

1. **大模型 API Key**：推荐 [DeepSeek 官方](https://platform.deepseek.com/)（注册充值 10 元即可）；也支持 [腾讯云知识引擎 lkeap](https://console.cloud.tencent.com/lkeap) 或任何兼容 OpenAI API 的平台
2. **企业微信应用**：https://work.weixin.qq.com/ 注册企业 → 创建应用 → 记下 Corp ID / AgentId / Secret / Token / EncodingAESKey（[企微应用配置指南 →](docs/企微应用配置指南.md)）
3. **服务器**：腾讯云轻量 1C1G 即可，或本地电脑先体验

### 部署方式一：一键脚本

```bash
git clone https://github.com/XinShu394/TextagentForU.git
cd TextagentForU
chmod +x setup.sh
./setup.sh
```

### 部署方式二：Docker

```bash
git clone https://github.com/XinShu394/TextagentForU.git
cd TextagentForU
cp .env.example src/.env
nano src/.env          # 填入配置
cd deploy
docker-compose up -d
```

### 部署方式三：手动

```bash
cd TextagentForU/src
pip3 install -r requirements.txt
cp ../.env.example .env
nano .env              # 填入配置
python3 app.py
```

### 配置企微回调

启动后将公网地址 + `/wework` 填入企微后台「接收消息」的 URL。

---

## 环境变量

### 必填

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | 大模型 API 密钥（DeepSeek 官方或兼容平台） |
| `WEWORK_CORP_ID` | 企微企业 ID |
| `WEWORK_AGENT_ID` | 应用 AgentID |
| `WEWORK_CORP_SECRET` | 应用 Secret |
| `WEWORK_TOKEN` | 回调 Token |
| `WEWORK_ENCODING_AES_KEY` | 回调加密密钥 |
| `DEFAULT_USER_ID` | 管理员企微用户 ID |
| `ADMIN_TOKEN` | 管理后台密码 |

### 可选

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | API 地址（可替换为腾讯云 lkeap 等） |
| `DEEPSEEK_MODEL` | `deepseek-v3.2` | 主力模型（Tier 2/3: Main + Think） |
| `QWEN_API_KEY` | 空 | Qwen Flash API Key（Tier 1，不填则全走 DeepSeek） |
| `QWEN_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Qwen API 地址（阿里云百炼） |
| `QWEN_MODEL` | `qwen-flash` | Qwen 模型名 |
| `QWEN_VL_MODEL` | `qwen-vl-max` | 视觉模型（图片理解） |
| `DAILY_MESSAGE_LIMIT` | `50` | 每人每天消息上限 |
| `WEB_TOKEN_EXPIRE_HOURS` | `24` | Web 链接有效时长 |
| `WEB_DOMAIN` | `127.0.0.1:9000` | Web 访问域名（服务器必改） |
| `SERVER_PORT` | `9000` | 服务端口 |
| `WEATHER_CITY` | `北京` | 天气城市 |
| `ADMIN_USER_ID` | 空 | 管理员 user_id（告警推送，配置后启用企微告警） |
| `ALERT_SLOW_THRESHOLD` | `20` | 慢请求告警阈值（秒） |
| `ALERT_COOLDOWN_SECONDS` | `300` | 同类告警冷却时间（秒） |

---

## 项目结构

```
TextagentForU/
├── setup.sh                 # 一键安装脚本
├── .env.example             # 配置模板
├── src/                     # 核心代码
│   ├── app.py               # Flask 网关 + V8 智能调度引擎
│   ├── brain.py             # AI 大脑（三层模型路由 + Agent Loop）
│   ├── prompts.py           # Prompt 统一管理（V12 结构化 Skill 描述）
│   ├── config.py            # 配置管理（模型/调度/告警参数）
│   ├── user_context.py      # 多用户管理（UserContext 全链路贯穿）
│   ├── web_routes.py        # Web API + 页面路由
│   ├── memory.py            # 记忆系统（三级缓存：内存→/tmp→文件）
│   ├── skill_loader.py      # Skill 自动发现 V12（支持 visibility）
│   ├── channel_router.py    # 渠道路由器
│   ├── storage.py           # 存储抽象层
│   ├── local_io.py          # 本地文件读写
│   ├── wework_crypto.py     # 企微消息加解密
│   ├── finance_utils.py     # 财务工具
│   ├── requirements.txt     # Python 依赖
│   ├── skills/              # 20 个功能模块 → 39 个 Skill
│   └── web_static/          # Web 前端页面
├── deploy/                  # Docker + SCF 部署
├── landing/                 # 官网首页
├── docs/                    # 文档
└── assets/                  # 截图资源
```

---

## 数据结构

```
data/
├── _textagent_system/          # 系统数据
│   ├── users.json           # 用户注册表
│   ├── tokens.json          # Web 令牌
│   └── usage_log.jsonl      # LLM 用量日志
└── users/                   # 按用户隔离
    └── <user_id>/
        ├── 00-Inbox/        # 速记、待办、状态
        ├── 01-Daily/        # 日报、周报、月报
        ├── 02-Notes/        # 分类笔记 + 随记
        │   └── 随记/        # memo.save 按日期存储
        └── _TextAgent/         # 记忆、配置、日志
```

---

## 成本估算

以 2-3 人日常使用：

| 项目 | 月费用 |
|---|---|
| DeepSeek API | ¥10-30（Prompt 优化后降低 ~37%） |
| 服务器（1C1G） | ¥30-60 |
| 其他（Qwen/ASR） | ¥0-20 |
| **合计** | **¥40-110/月** |

> 通过 `DAILY_MESSAGE_LIMIT` + 管理员后台 LLM 用量面板 + 不活跃用户自动跳过控制成本。

---

## 官网

🔗 **https://textagent.top**

---

## License

[MIT](LICENSE)
