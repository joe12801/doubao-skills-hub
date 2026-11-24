---
name: sspanel-purchase
display_name: "机场面板购套餐"
description: 在 SSPanel-UIM 机场/加速器面板完成登录、查余额与套餐、充值下单（微信/支付宝扫码出海报二维码）、后台实时轮询到账、到账后自动扣余额买/续套餐并返回订阅链接。当用户给出 panel.xxxx.xyz 面板地址并要求登录看余额、充 20 块、生成收款码给我扫、买个 20 元套餐、续费、取订阅链接时使用。内置纯 HTTP 脚本，可绕过前端卡死、二维码过期与扫码后无人通知的问题。
---

# 机场面板购套餐

> SSPanel-UIM 面板充值 / 套餐购买。把一条真实跑通的付费链路固化成脚本：
> **登录 → 查价 → 充值出码 → 后台监控到账 → 自动买套餐 → 复核 → 返订阅**。

## 依赖

- Python3，`requests`；出海报大图需 `Pillow`（`pip install pillow`）。
- 中文海报字体：优先 Noto Sans CJK（脚本内置候选路径，缺失时回退默认字体）。
- 会话 cookie 存 `~/.cache/sspanel-purchase/session.json`（权限 600）。

## 铁律（务必遵守）

1. **金额与套餐必须先和用户确认**：下单前把「套餐名 / 价格 / 流量 / 周期 / 支付方式 / 应付尾数」讲清楚；用户没明确授权金额或套餐时不要自行下单。
2. **全程走 HTTP 接口**，不要用浏览器点按钮：面板前端（Vue + jQuery）会间歇性卡死、点了不跳转，接口一直正常。
3. **出码后必须挂后台实时监控，不等用户回来报「付好了」**：检测到付款成功就自动续买套餐并取订阅（见「后台自动续买」）。
4. **到账以面板余额为准**，不要只信网关 `checkOrder`（实测会滞后：返回未支付但余额已到账）。
5. **二维码只有 5 分钟有效期**（`timeOut=5`），过期立即重新下单，不复用旧码。
6. 密码只从用户消息读取、通过命令行参数传入，**不要写进回复、日志或状态文件**。
7. 买完必须回读用户中心，用**等级 + 到期时间 + 余额**三项交叉验证再汇报；并把订阅地址一并返回，优先 Trojan。
8. **只买一次**：买完不要重复触发 `buy` / `auto`；余额为 0 时不要再挂购买监控。

## 脚本

```bash
cd <本技能目录>/scripts
python3 panel.py <command> [--json]   # --json 写在子命令前后都可；--base/--gateway 可换站点
```

| 命令 | 作用 | 关键输出 |
|---|---|---|
| `login --email E --password P` | 登录并持久化会话 | `login: OK`、余额、等级、到期 |
| `info` | 查当前账户 | `balance` / `level` / `level_expire` / `account_expire` |
| `plans` | 列商店套餐 | `id` / 名称 / 价格 / 流量 / 周期 / 等级 |
| `recharge --amount 20 --type weixin --qr raw.png --poster poster.png` | 下单 + 落原始码 + 渲染海报大图 | `order_id`、`really_price`、`pay_url`、`qr_file`、`poster_file` |
| `poster --qr raw.png --out poster.png --amount 20.01 [--tail-note ...]` | 把已有原始码重排成海报 | `poster_file` |
| `status --order ID` | 查订单是否支付 | `paid`、`balance` |
| `wait --order ID --timeout 300 [--baseline 0]` | 轮询直到支付成功（退出码 2=超时） | `paid`、`balance` |
| `auto --order ID --shop 1 --timeout 300 --protocol trojan` | **后台一条龙**：轮询到账 → 买套餐 → 回读 → 取订阅 | `paid`、`buy.success`、`level`、`sub.url` |
| `buy --shop 1` | 扣余额买套餐 | `success`、`msg`、买后等级与到期 |
| `sub --protocol trojan` | 取订阅地址 | `url`、`protocols`、`nodes`、`active`、`all_urls` |
| `cookies --browser-cmd` | 导出 cookie / 生成注入命令 | `uid=..` / `agent-browser cookies set ..` |

## 标准执行流程

### 第 0 步 接续会话

```bash
python3 panel.py info          # 有输出即已登录，可直接跳到第 2 步
python3 panel.py login --email <用户给的> --password <用户给的>
```

### 第 1 步 报价确认

先 `plans` 拉出套餐，挑出与预算匹配的（如 20 元档），把「套餐名 / 价格 / 流量 / 周期」和当前余额讲清楚，再下单。

### 第 2 步 充值下单 + 出海报

```bash
python3 panel.py recharge --amount 20 --type weixin \
  --qr ./qr_raw.png --poster ./微信收款码-20元.png
```

- `really_price` 是实际应付（可能比订单金额多 0.01~0.02 **对账尾数**），必须让用户按尾数金额付。
- 把海报大图按「收款码发送」一节的通道发给用户，告知金额、尾数、5 分钟有效期。

### 第 3 步 后台实时监控到账（强制，不要等用户通知）

出码后**立刻**把一条龙命令放后台运行（`run_in_background: true`）：

```bash
python3 panel.py auto --order <order_id> --shop <shop_id> --timeout 300 --protocol trojan
```

脚本内部每 `interval` 秒轮询一次：`checkOrder` 返回已支付，**或** 余额较下单前增长，即判定到账；随后自动 `buy` 该套餐、回读账户信息、拉取 Trojan 订阅，一次性输出 JSON。

- 主流程用后台任务输出（`TaskOutput` / 轮询文件）读取结果，**不要停下来等用户回「付好了」**。
- 后台超时（退出码 2）：再 `info` 查一次余额；余额已涨就手动 `buy --shop <id>`。
- 若 `buy` 返回「套餐还未过期」：说明已有生效套餐，只能续费或等过期，如实告知用户。

### 第 4 步 复核与汇报

`auto` 成功后已带回买后信息；必要时再 `info` 复核。汇报：到账金额 → 套餐名/价格/权益 → 买后等级与到期 → 余额变化 → **Trojan 订阅地址**。

### 第 5 步 返回订阅地址

```bash
python3 panel.py sub --protocol trojan
```

- `url` 即 Trojan 订阅链接，直接粘进客户端。
- `all_urls` 带齐其它协议：`sub=1` SSR、`sub=2` SS、`sub=3` V2Ray、`sub=4` Trojan、`clash=1` Clash、`list=shadowrocket`。
- `active:false` 且节点名出现字面「账户已过期」才是占位节点；「剩余流量 / 过期时间：…」是正常元信息，别误判。

## 收款码发送（本次踩坑，务必照做）

**失败路径（别再用）**：把网关 200×200 原始 PNG 直接用 `present_files` 发本地附件——移动端聊天里只出一个不可内联的文件卡片，**图片不显示，用户看不到二维码**。

**成功路径**：

1. 脚本 `recharge --poster` 生成 620×820 竖版海报（标题 + 放大码 + 红字金额 + 尾数说明 + 有效期提示）。
2. 先 `Read` 本地海报，确认中文不乱码、码块清晰可扫。
3. 用 `FileBatchUpload` 上传海报，拿到 CDN 直链。
4. 回复里用图片内联卡片展示，`refs` 同时带云端 URL 和本地路径（含 `env_id`）：

```
<RichMediaCard>{"refs":["<CDN 直链>",{"ref":"<海报绝对路径>","env_id":"<环境 id>"}]}</RichMediaCard>
```

- 网关小图（200×200）不要直接发，必须先放大成海报。
- 二维码 5 分钟过期；用户反馈扫不出/过期，重新 `recharge` 出码，不要复用旧码。

## 站点特性与坑（panel.994938.xyz 实测）

| 现象 | 原因 | 对策 |
|---|---|---|
| 登录页点「确认登录」不跳转 | AJAX 已 POST 但前端未处理 | 用 `login` 走接口，再 `cookies --browser-cmd` 注入浏览器 |
| 浏览器打开支付页报 `chrome-error://` | 网关是 **http**，被强制升级/拦截 | 用脚本或 curl 取码，别用浏览器打开网关 |
| 二维码扫出来金额不对 | `reallyPrice` 带随机对账尾数 | 让用户按海报上的金额付，别报订单原价 |
| 充值后 `checkOrder` 仍显示未支付 | 网关回调/状态滞后 | 以面板余额为准；`auto` 已做余额增长兜底 |
| 尾数没进余额（应付 20.01 到账 20.00） | 网关/面板对账口径 | 按套餐价比对余额是否充足，不要求余额等于 `reallyPrice` |
| 买套餐返回「含有自动重置系统的套餐还未过期」 | 已有未过期套餐 | 只能续费/等到期，向用户说明 |
| 请求随机 TLS 握手超时 | 站点网络抖动 | 脚本内置 3 次重试（`SSPANEL_RETRIES`/`SSPANEL_TIMEOUT` 可调） |
| 节点名含「过期时间」被判失效 | 把元信息行误当占位节点 | 仅字面「账户已过期」才算未生效 |
| 账户到期与等级到期差一年 | 后台手改日期年份错位 | 节点可用性以 `level_expire` 为准，两个日期都汇报 |
| 订单号时间戳与本机时钟错位 | 网关/本机时钟不一致 | 有效期以 `getOrder` 的 `timeOut/date` 为准 |

## 浏览器兜底（仅当接口不可用时）

```bash
python3 panel.py cookies --browser-cmd
agent-browser open https://panel.994938.xyz/user
agent-browser snapshot -i                   # 先取 ref 再点，别猜选择器
```

UI 购买链路三层弹窗：`购买` → 优惠码框「确定」→ `订单确认`（含「关闭旧套餐自动续费」开关）→「确定」→「购买成功」。

## 换站点

非该域名但同样是 SSPanel-UIM + V免签的面板：

```bash
python3 panel.py --base https://新面板 --gateway http://新支付网关 plans
```

或设环境变量 `SSPANEL_BASE` / `SSPANEL_GATEWAY`。接口路径见 [references/api.md](references/api.md)。
