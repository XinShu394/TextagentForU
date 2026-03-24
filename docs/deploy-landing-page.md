# TextAgentForU 宣传页部署指南

## 概述

宣传页面通过 **GitHub Pages** 托管，绑定自定义域名 `textagent.cn`，DNS 由 **Cloudflare** 管理。

## 架构

```
用户访问 textagent.cn
       ↓
Cloudflare DNS（DNS only 模式）
       ↓
GitHub Pages 服务器（185.199.108/109/110/111.153）
       ↓
GitHub 仓库：XinShu394/Textagentweb (main 分支)
       ↓
返回 index.html 宣传页面
```

## 仓库信息

| 项目 | 详情 |
|------|------|
| **GitHub 仓库** | https://github.com/XinShu394/Textagentweb |
| **分支** | main |
| **本地目录** | `TextAgentForU/landing/` |
| **域名** | textagent.cn |
| **DNS 托管** | Cloudflare |
| **域名注册** | 阿里云 |

## 仓库文件结构

```
Textagentweb/
├── CNAME              # GitHub Pages 自定义域名配置（内容：textagent.cn）
├── LICENSE            # MIT 许可证
├── index.html         # 宣传页面主文件
└── qrcode_wework.png  # 企业微信二维码（⚠️ 有效期7天，需定期更换）
```

## 部署步骤记录

### 1. GitHub 仓库设置

1. 在 GitHub 创建公开仓库：`XinShu394/Textagentweb`
2. 在本地 `TextAgentForU/landing/` 目录初始化 Git 仓库
3. 添加文件并推送：
   ```bash
   cd TextAgentForU/landing
   git init
   git remote add origin https://github.com/XinShu394/Textagentweb.git
   git add index.html qrcode_wework.png CNAME
   git commit -m "Initial commit: TextAgentForU landing page for GitHub Pages"
   git branch -M main
   git push -u origin main
   ```

### 2. GitHub Pages 设置

1. 打开 https://github.com/XinShu394/Textagentweb/settings/pages
2. **Source**：Deploy from a branch
3. **Branch**：main / `/ (root)`
4. **Custom domain**：填入 `textagent.cn`，等待 DNS check successful
5. 勾选 **Enforce HTTPS**（需等 GitHub 自动颁发 SSL 证书后才可勾选）

### 3. Cloudflare DNS 配置

在 Cloudflare 的 `textagent.cn` DNS 管理页面：

**删除旧记录：**
- 删除原有的 Tunnel 记录（karvis）
- 删除原有的 A 记录（www → <YOUR_SERVER_IP>）

**添加新记录（全部设为 DNS only ☁️，不开 Proxied）：**

| Type  | Name | Content           | Proxy status |
|-------|------|-------------------|-------------|
| A     | @    | 185.199.108.153   | DNS only    |
| A     | @    | 185.199.109.153   | DNS only    |
| A     | @    | 185.199.110.153   | DNS only    |
| A     | @    | 185.199.111.153   | DNS only    |
| CNAME | www  | xinshu394.github.io | DNS only  |

> ⚠️ **必须设为 DNS only**（灰色云朵），不能开 Proxied（橙色云朵），否则 GitHub Pages 无法验证域名和颁发 SSL 证书。

### 4. 验证

DNS 生效后（通常几分钟）：
- 访问 http://textagent.cn 确认页面正常显示
- 访问 https://textagent.cn 确认 HTTPS 正常
- 在 GitHub Pages 设置页勾选 Enforce HTTPS

## 日常维护

### 更新页面内容

```bash
cd TextAgentForU/landing
# 编辑 index.html 或 textagent-foryou.html
# 如果编辑的是 textagent-foryou.html，需要同步覆盖 index.html：
cp textagent-foryou.html index.html

git add -A
git commit -m "Update landing page content"
git push
```

GitHub Pages 会在推送后 1-2 分钟内自动更新。

### 更换企业微信二维码

当前二维码有效期 7 天（到期日期见二维码上水印），更换步骤：
1. 从企业微信后台获取新的二维码图片
2. 保存为 `qrcode_wework.png`，放入 `landing/` 目录
3. 推送到 GitHub：
   ```bash
   cd TextAgentForU/landing
   git add qrcode_wework.png
   git commit -m "Update WeChat Work QR code"
   git push
   ```

## 注意事项

- `index.html` 是 GitHub Pages 的入口文件，`textagent-foryou.html` 是开发用的源文件
- 每次修改 `textagent-foryou.html` 后记得同步到 `index.html`
- 企业微信二维码有效期 7 天，需定期更换
- 页面数据已与项目实际代码同步：39 项技能、20 个功能模块
