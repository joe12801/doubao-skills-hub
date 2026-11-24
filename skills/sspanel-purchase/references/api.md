# SSPanel-UIM + V免签 接口参考

实测站点：`panel.994938.xyz`（面板，HTTPS）+ `pay.994938.xyz`（V免签收款网关，HTTP）。
面板前端为 Vue + jQuery + Material Design，支付为 VmqPay 面对面收款。

## 1. 登录

```
POST {panel}/auth/login
Content-Type: application/json

{"email":"<邮箱>","passwd":"<密码>","code":"","remember_me":false}
```

成功：`{"ret":1,"msg":"登录成功"}`；失败 `ret=0`，`msg` 为中文原因。
响应 `Set-Cookie` 写入 5 个 cookie：`uid` / `email` / `key` / `ip` / `expire_in`。
后续请求只需带 `uid` + `key` + `email`。会话探测：`GET {panel}/user`，未登录 302 到 `/auth/login`。

> 换账号必须用干净会话：否则旧 cookie 让 `/auth/login` 直接 302 到 `/user`，
> 返回 HTML 而非 JSON，脚本会误报“返回非 JSON”。脚本 `login` 已用 fresh 会话处理。

## 2. 账户信息

```
GET {panel}/user
```

关键结构（HTML，正则抽取）：

```html
<div class="nodename">余额</div> ... <div class="nodetype"> 20.00 CNY </div>
<div class="nodename">在线设备数</div> ... <div class="nodetype"><dd>0 / 2</dd></div>
```

- 等级：`帐号等级` 卡片值形如 `VIP 2`
- 等级到期：`等级到期时间 2026-12-25 03:10:43`
- 账户到期：`账户有效时间：2027-01-01`（到期自动删号）

> 注意：账户到期与等级/套餐到期可能不一致（后台手改日期时年份字段可能错位）。
> 节点可用性以「等级到期时间」为准；汇报时两个日期都列出。

## 3. 商店套餐

```
GET {panel}/user/shop
```

每个商品是一个 `<div class="shop-gridarea">` 区块：

```html
<div class="shop-name"><span>包月20</span></div>
<div class="card-tag tag-gold">VIP 2</div>
<div class="card-tag tag-orange">¥ 20.00</div>
<div class="card-tag tag-cyan">9999 G</div>
<div class="card-tag tag-blue">30 天</div>
<a ... onClick="buy('1',0)">购买</a>     <!-- 第一个参数即 shop id -->
```

## 4. 充值下单（V免签）

```
GET {panel}/user/payment/purchase/vmqpay?price=20&type=1
```

- `type`：`1`=微信，`2`=支付宝
- `price`：元，最多两位小数

面板 302 到网关：

```
Location: http://pay.994938.xyz/createOrder?payId=<面板单号>&type=1&price=20&sign=<签名>&param=<uid>&isHtml=1&notifyUrl=...&returnUrl=...
```

该地址返回 JS 跳转（不是 302，curl `-L` 不会跟随）：

```html
<script>window.location.href = 'payPage/pay.html?orderId=202611250311219266'</script>
```

**`orderId` 从这段 HTML 正则取**，是后续查单的唯一凭据。订单号时间戳可能与本机 `date` 有
时钟错位，有效期以网关 `getOrder` 返回的 `timeOut/date` 为准，不要拿本机时钟判断是否过期。

### 取订单详情

```
POST {gateway}/getOrder      (form: orderId=<orderId>)
```

```json
{"code":1,"msg":"成功","data":{
  "payId":"1795547274","orderId":"202611250311219266","payType":1,
  "price":20,"reallyPrice":20.01,
  "payUrl":"wxp://f2f0...","isAuto":1,"state":0,"timeOut":"5","date":1795547301
}}
```

- `payUrl`：`wxp://` 开头的微信面对面收款码链接
- `reallyPrice`：**用户实际应付金额**，可能比 `price` 多 0.01~0.02 的对账尾数
- `timeOut`：有效分钟数（此处 5）；`state`：`-1` 已过期，`0` 待支付，`1` 已支付
- 实测：尾数可能不计入余额（应付 20.01，到账 20.00）。判余额是否够买套餐时按套餐价比对，
  不要要求余额精确等于 `reallyPrice`。

### 出二维码

```
GET {gateway}/enQrcode?url=<urlencode(payUrl)>
```

返回 200×200 PNG。该小图不适合直接在手机端聊天里发，需脚本 `--poster` 放大成竖版海报。

### 查支付状态

```
POST {gateway}/checkOrder     (form: orderId=<orderId>)
```

- 已支付：`{"code":1,"data":"<returnUrl>"}`
- 未支付：`{"code":-1,"msg":"订单未支付"}`
- 已过期：`{"code":-1,"msg":"订单已过期"}`

> 实测该接口可能滞后（面板余额已到账仍返回未支付），**以 `GET /user` 余额为最终依据**。

## 5. 购买套餐

```
POST {panel}/user/buy
Content-Type: application/x-www-form-urlencoded
X-Requested-With: XMLHttpRequest

coupon=&shop=1&autorenew=0&disableothers=1
```

- `shop`：套餐 id；`autorenew`：`1` 开自动续费，`0` 关；`disableothers`：`1` 关闭其他套餐自动续费
- `coupon`：优惠码，可空

返回 `{"ret":1,"msg":"购买成功"}`；`ret=0` 时 `msg` 为业务原因：

| msg | 含义 |
|---|---|
| 余额不足 | 先充值 |
| 您购买的含有自动重置系统的套餐还未过期，无法购买新套餐 | 已有生效套餐，需等过期或走续费 |
| 优惠码无效 | coupon 错误 |

## 6. 订阅地址

```
GET {panel}/user   → 正则取 /link/<token>
```

协议专用订阅用 `?sub=N`：1=SSR、2=SS、3=V2Ray、4=Trojan；`clash=1` Clash；
`list=shadowrocket` Shadowrocket。

订阅内容多为 base64 列表，需先解码再识别 `trojan://` / `vmess://` 等节点。

> **active 判定坑**：节点名里的「剩余流量：9.76TB」「过期时间：2026-12-25」是正常元信息行，
> 不要见到“过期”两字就判定占位失效。只有出现字面 **「账户已过期」** 才是未生效占位节点。

## 7. 浏览器兜底交互链路

商店页 `购买` 是 `javascript:void(0)`，走三层弹窗：
1. 点 `购买` → 弹「您有优惠码吗？」→ 空着点「确定」
2. 弹「订单确认」：商品名 / 优惠额度 / 总金额 / 开关「关闭旧套餐自动续费」→「确定」
3. 弹「购买成功」→「知道了」→ 约 1.2 秒后回到 `/user/shop`

登录态注入浏览器：

```bash
python3 panel.py cookies --browser-cmd
# agent-browser cookies set "uid" "819" --domain panel.994938.xyz --path /
```

## 8. 常见异常

| 现象 | 说明 |
|---|---|
| `chrome-error://chromewebdata/` | 浏览器打开 http 网关被拦截；改用 curl/脚本 |
| `请传入商户订单号` | 访问 `payPage/pay.html` 路径错误，正确是 `{gateway}/payPage/pay.html?orderId=` |
| `云端订单编号不存在` | `getOrder` 传了面板单号（payId）而非 `orderId` |
| TLS 握手超时 | 站点网络抖动，重试即可（脚本内置 3 次指数退避） |
| 导入后节点叫「账户已过期，请续费后使用」 | 账号到期时间早于当前时间；`info` 看 `account_expire`/`level_expire`，链接本身不用换 |
