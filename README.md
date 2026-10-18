# Neoheberg 自动续期（GitHub Actions）

用 GitHub Actions 定时登录 [Neoheberg](https://dash.neoheberg.fr/login) 面板，剩余天数不足时自动续期，并在容器停止时自动开机。支持多账号、代理降级、Telegram 推送和仓库保活。

> 本项目与 Neoheberg 官方无关。登录与续期流程参考了公开仓库 [yousb66/neoheberg-renew](https://github.com/yousb66/neoheberg-renew) 的实现。使用前请确认符合 Neoheberg 的服务条款，风险自负。

## 功能

- 无头浏览器登录（两步式表单 + Cap.js 验证码），读取到期日，剩余天数 ≤ 阈值（默认 14 天）才续期
- 容器已停止时自动开机
- **多账号**：每个账号独立浏览器和 cookie，一个失败不影响其他账号
- **代理**：全局代理 + 账号专用代理（**专用代理支持节点分享链接**），专用代理失效或被 Cloudflare 拦截时自动降级到全局代理
- **Telegram 推送**：每次运行的汇总报告
- **仓库保活**：每次运行更新 `last_check.txt`，避免 GitHub 60 天无活动后停用定时任务
- 每周运行一次，也可手动触发

## 文件说明

```
.
├── renew.py                      # 主脚本：登录、续期、开机、多账号、代理降级、TG 推送
├── setup_proxy.py                # 把节点分享链接转成本地 SOCKS5 代理（sing-box）
├── last_check.txt                # 自动生成：上次检测时间（保活用）
└── .github/workflows/renew.yml   # 定时任务
```

## 快速开始

1. 新建一个仓库（**建议私有**，把 `renew.py`、`setup_proxy.py`、`.github/workflows/renew.yml` 放进去。
2. 进入 **Settings → Actions → General → Workflow permissions**，选择 **Read and write permissions**（保活要往仓库提交文件）。
3. 进入 **Settings → Secrets and variables → Actions**，添加 Secrets（见下表，最少需要账号信息）。
4. 打开 **Actions** 页面，选择 `Neoheberg Renew` → **Run workflow**，手动运行一次。
5. 看日志确认成功。之后每周一自动运行。

### Secrets

| 名称 | 必填 | 说明 |
|---|---|---|
| `NEO_USER` / `NEO_PASSWORD` / `NEO_VMID` | 单账号必填 | 账号、密码、服务实例 ID（纯数字，不要加引号或前缀） |
| `NEO_ACCOUNTS` | 多账号必填 | 多账号配置，格式见下文，可与上面三项同时存在 |
| `NODE_LINK` | 可选 | 全局代理：节点分享链接（单个），经 sing-box 转成本地 SOCKS5 |
| `PROXY_SERVER` | 可选 | 全局代理：现成的代理地址；`NODE_LINK` 优先，它不可用时改用这个 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 可选 | Telegram 推送 |

### Variables（可选，留空用默认值）

| 名称 | 默认 | 说明 |
|---|---|---|
| `RENEW_DAYS` | `14` | 剩余天数小于等于该值才续期 |
| `ACCOUNT_GAP` | `30` | 账号之间的等待秒数（另加 0~15 秒随机） |
| `TG_NOTIFY` | `always` | `always` 每次都推送；`change` 有续期、失败或代理降级才推送；`fail` 仅失败时推送 |

> `NEO_VMID` 是面板里该服务的实例 ID，登录面板后可在服务列表或服务详情页的 URL 中找到，具体位置以面板为准。

## 多账号配置

`NEO_ACCOUNTS` 推荐用 JSON，密码里有任何特殊字符都不受影响：

```json
[
  {"user": "账号1", "password": "密码1", "vmid": "12345"},
  {"user": "账号2", "password": "密码2", "vmid": "67890", "name": "备用号", "proxy": "vless://uuid@host:443?type=tcp&security=reality&...#节点名"}
]
```

也可以每行一个 `账号|密码|vmid`（密码里可以含 `|`，`#` 开头为注释；这种格式不能配置专用代理）：

```
账号1|密码1|12345
账号2|密码2|67890
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `user` | 是 | 账号（邮箱或用户名） |
| `password` | 是 | 密码 |
| `vmid` | 是 | 服务实例 ID |
| `name` | 否 | 日志和推送里的别名，默认 `#1`、`#2` |
| `proxy` | 否 | 该账号专用代理：**节点分享链接**或代理地址，见下节 |
| `renew_days` | 否 | 该账号单独的续期阈值 |
| `type` | 否 | 服务类型，默认 `vps` |

## 代理

GitHub Actions 的机房 IP 信誉较低，容易被 Cloudflare 的托管质询（Turnstile）拦截，表现为登录页变成「Vérification de sécurité en cours」。解决办法是使用干净的固定出口 IP（住宅 / 家宽 IP 最稳）。

### 配置方式

| 层级 | 配置位置 | 可填内容 |
|---|---|---|
| 全局代理（所有账号共用的后备） | Secret `NODE_LINK` | 节点分享链接 |
| | Secret `PROXY_SERVER` | 现成的代理地址，`NODE_LINK` 不可用时改用它 |
| 账号专用代理 | `NEO_ACCOUNTS` 里该账号的 `proxy` 字段 | 节点分享链接，或代理地址 |

`proxy`（以及 `NODE_LINK`）支持的写法：

| 写法 | 示例 | 处理方式 |
|---|---|---|
| 节点分享链接 | `vless://…`、`vmess://…`、`trojan://…`、`ss://…`、`hysteria2://…`（`hy2://…`）、`tuic://…` | 经 sing-box 转成本地 SOCKS5 入口 |
| 带账号密码的 socks5 | `socks5://user:pass@host:1080` | Chromium 不支持带认证的 SOCKS5，同样交给 sing-box 转换 |
| HTTP / HTTPS 代理 | `http://user:pass@host:8080` | 直接使用 |
| 不带认证的 socks5 | `socks5://host:1080` | 直接使用 |

说明：

- 全局的 `NODE_LINK` / `PROXY_SERVER` 只取第一行。
- 账号专用的节点链接会在任务开始时统一启动（本地端口从 2080 起），同一条链接被多个账号使用时只启动一次。
- 每个节点启动后会单独做连通性自检，不可用的节点按「失效」处理（见下节）。

## Telegram 推送

1. 在 [@BotFather](https://t.me/BotFather) 创建机器人，拿到 Token，填入 Secret `TG_BOT_TOKEN`。
2. **先给机器人发一条消息**，否则它无法主动联系你。
3. 获取你的 Chat ID（如通过 [@userinfobot](https://t.me/userinfobot)），填入 Secret `TG_CHAT_ID`。
4. 需要调整推送频率时，设置 Variable `TG_NOTIFY`。

推送内容：每个账号的结果（续期成功 / 无需续期 / 失败原因）、是否发生代理降级、运行日志链接。报告里只用账号别名，不含账号、密码、实例 ID、代理地址和 IP。workflow 在启动阶段（如依赖安装）就失败时，也会有一条兜底提醒。推送本身失败只记一条警告，不影响续期结果。

## 运行时间与手动触发

- 定时：每周一 UTC 02:00（北京时间 10:00），在 `renew.yml` 的 `cron` 里修改。每次只检测，剩余天数大于阈值就不续期。
- 手动：**Actions → Neoheberg Renew → Run workflow**。勾选 `force_renew` 会忽略剩余天数，强制续期一次。
- 阈值与频率的关系：每周运行一次，最迟会晚 7 天才续期，所以 `RENEW_DAYS` 不要小于 7，默认 14 比较稳妥。
