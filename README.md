# Neoheberg 自动续期（GitHub Actions）

用 GitHub Actions 定时登录 [Neoheberg](https://dash.neoheberg.fr/login) 面板，剩余天数不足时自动续期，并在容器停止时自动开机。支持多账号、代理降级、Telegram 推送和仓库保活。

> 本项目与 Neoheberg 官方无关。登录与续期流程参考了公开仓库 [yousb66/neoheberg-renew](https://github.com/yousb66/neoheberg-renew) 的实现。使用前请确认符合 Neoheberg 的服务条款，风险自负。

## 功能

- 无头浏览器登录（两步式表单 + Cap.js 验证码），读取到期日，剩余天数 ≤ 阈值（默认 14 天）才续期
- 容器已停止时自动开机
- **多账号**：每个账号独立浏览器和 cookie，一个失败不影响其他账号
- **代理**：全局代理（节点链接 / 现成代理）+ 账号专用代理，失效或被 Cloudflare 拦截时自动降级
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

1. 新建一个仓库（**建议私有**，原因见[安全建议](#安全建议)），把 `renew.py`、`setup_proxy.py`、`.github/workflows/renew.yml` 放进去。
2. 进入 **Settings → Actions → General → Workflow permissions**，选择 **Read and write permissions**（保活要往仓库提交文件）。
3. 进入 **Settings → Secrets and variables → Actions**，添加 Secrets（见下表，最少需要账号信息）。
4. 打开 **Actions** 页面，选择 `Neoheberg Renew` → **Run workflow**，手动运行一次。
5. 看日志确认成功。之后每周一自动运行。

### Secrets

| 名称 | 必填 | 说明 |
|---|---|---|
| `NEO_USER` / `NEO_PASSWORD` / `NEO_VMID` | 单账号必填 | 账号、密码、服务实例 ID（纯数字，不要加引号或前缀） |
| `NEO_ACCOUNTS` | 多账号必填 | 多账号配置，格式见下文，可与上面三项同时存在 |
| `NODE_LINK` | 可选 | 全局代理：节点分享链接，经 sing-box 转成本地 SOCKS5 |
| `PROXY_SERVER` | 可选 | 全局代理：现成的代理地址，`NODE_LINK` 优先 |
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
  {"user": "账号2", "password": "密码2", "vmid": "67890", "name": "备用号", "proxy": "http://user:pass@1.2.3.4:8080"}
]
```

也可以每行一个 `账号|密码|vmid`（密码里可以含 `|`，`#` 开头为注释）：

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
| `proxy` | 否 | 该账号专用代理，见下节 |
| `renew_days` | 否 | 该账号单独的续期阈值 |
| `type` | 否 | 服务类型，默认 `vps` |

说明：

- 旧的 `NEO_USER` 等单账号 Secret 若仍存在，会作为第一个账号与 `NEO_ACCOUNTS` 合并，相同的「账号 + 实例」自动去重。不想要就把旧 Secret 删掉。
- 一个账号对应一个服务。同一账号有多个服务时，写成多条并填不同的 `vmid`，每条会各自登录一次。
- 账号之间默认间隔 30~45 秒，防止站点限流。账号很多时注意 workflow 的 `timeout-minutes`（默认 60 分钟，每个账号约 1~2 分钟）。

## 代理

GitHub Actions 的机房 IP 信誉较低，容易被 Cloudflare 的托管质询（Turnstile）拦截，表现为登录页变成「Vérification de sécurité en cours」。解决办法是使用干净的固定出口 IP（住宅 / 家宽 IP 最稳）。

### 配置方式

| 方式 | 配置 | 说明 |
|---|---|---|
| 全局 · 节点链接 | Secret `NODE_LINK` | 支持 `vless` `vmess` `trojan` `ss` `hysteria2` `tuic` `socks5` `http`，由 sing-box 自动转成 `127.0.0.1:1080` |
| 全局 · 现成代理 | Secret `PROXY_SERVER` | 如 `http://user:pass@host:port`、`socks5://host:port` |
| 账号专用 | `NEO_ACCOUNTS` 里的 `proxy` 字段 | 只支持 `http(s)://`、`socks5://` 地址，不支持节点链接 |

注意：Chromium 不支持带账号密码的 SOCKS5。有认证的请用 HTTP 代理，或放进全局的 `NODE_LINK`。

### 降级规则（每个账号单独判断，绝不会悄悄改成直连）

| 情况 | 结果 |
|---|---|
| 没配任何代理 | 直连 |
| 有专用代理且可用 | 用专用代理 |
| 专用代理连不上，或被 Cloudflare 拦截 | 自动改用全局代理 |
| 没有专用代理，全局代理可用 | 用全局代理 |
| 全局代理失效（含 `NODE_LINK` 启动失败），账号有可用的专用代理 | 该账号继续 |
| 全局代理失效，账号没有专用代理 | 取消该账号，不直连 |
| 全局和专用都失效 / 都被拦截 | 取消该账号 |

另外，某个出口被 Cloudflare 拦截后，本次运行内不再让其他账号去撞这个出口。被取消的账号算失败，整次运行会标红，Telegram 也会通知。

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

## 仓库保活

GitHub 会在公开仓库 60 天没有活动后自动停用定时任务。每次运行结束后（无论成功失败），workflow 会把检测时间写入 `last_check.txt` 并提交，保证仓库每周都有新提交。只提交这一个文件，不会带上调试截图。

```
last_check: 2026-10-19 02:00:13 UTC
status: success
run: https://github.com/<用户名>/<仓库>/actions/runs/<编号>
```

前提是第 2 步的 **Read and write permissions** 已开启，否则推送会被拒绝（日志里会有提示）。建议第一个月后检查一次 Actions 页面，确认定时任务没有被停用。

## 日志说明与常见问题

| 日志 | 原因与处理 |
|---|---|
| `检测到 Cloudflare 质询页` / `被 Cloudflare 托管质询拦截` | 出口 IP 信誉低。配置 `NODE_LINK` / `PROXY_SERVER`，或改用自托管 runner |
| `站点提示: Identifiants invalides` | 账号或密码错误。Discord 注册的账号默认没有密码，需先用「Mot de passe oublié ?」设置。**脚本遇到该提示只会试一次**，反复输错可能被站点限流甚至锁号 |
| `密码=N位` | 脚本实际读到的密码长度，可与真实密码核对有没有多带引号、空格 |
| `登录表单未渲染` | 站点偶尔返回残缺页面，稍后手动重跑 |
| `验证码求解超时` | 网络或代理过慢，重跑 |
| `未取到 CSRF token` | 登录后页面结构异常，查看 Artifacts 里的截图 |
| `续期失败: …` | 先检查 `NEO_VMID` 是否正确 |
| `专用代理不可用，已自动改用全局代理` | 专用代理连不上，已降级，请修复它 |
| `全局代理不可用 … 已取消` | 节点或代理失效，未配专用代理的账号被取消 |
| `Telegram 推送失败: HTTP 401` / `HTTP 400` | 分别是 Token 无效 / Chat ID 无效或没给机器人发过消息 |
| `push 失败（已忽略）` | 没有开启 Read and write permissions |

运行失败时，Actions 页面底部的 **Artifacts → debug** 里有截图和页面 HTML（多账号时文件名带 `acc1_`、`acc2_` 前缀），可用于排查。

## 安全建议

- **使用私有仓库。** 日志里账号和密码会被屏蔽，但失败时上传的截图是登录页原貌，可能带有账号信息。
- 所有凭据只放在 Secrets 里，不要写进代码或提交到仓库。
- 站点对登录有限流，不要频繁手动触发，更不要反复用错误密码运行。
- 脚本不会在日志里输出密码，只输出位数。

## 工作原理

1. 打开登录页，先填账号并点「Continuer」，等密码框出现。
2. 触发 Cap.js 验证码（浏览器内工作量证明，约十几秒），等待 `cap-token` 生成。
3. 提交登录，成功后在已登录页面内用 `fetch` 读取到期日和 CSRF token。
4. 剩余天数不足时向 `/services/renew` 提交续期；再查询容器状态，已停止则下发开机。
5. 汇总结果，推送 Telegram，并更新 `last_check.txt`。

## 已知限制

- 本项目的登录流程在 Actions 中已验证到「账号密码校验」这一步；**续期与开机接口基于公开仓库的实现编写，首次运行后请以日志为准确认**。
- 站点改版（表单字段、接口路径、验证码方案）后脚本可能失效，需要按日志和截图调整。
- 没有实现自动点击 Cloudflare Turnstile，被拦截时的办法是更换出口 IP。
- 保活的有效性依赖 GitHub 对机器人提交的处理，建议偶尔确认定时任务仍在运行。
