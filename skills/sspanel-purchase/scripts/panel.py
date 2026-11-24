#!/usr/bin/env python3
"""
SSPanel-UIM 面板操作 CLI —— 登录 / 查余额 / 充值下单 / 出二维码海报 / 轮询到账 /
到账自动购套餐 / 取订阅地址。

设计原则：全程走 HTTP 接口，不依赖浏览器渲染。面板前端（Vue + jQuery）偶发页面卡死、
点击不触发，接口层则稳定可复现。

示例：
    python3 panel.py login --email a@b.com --password <密码> --json
    python3 panel.py info
    python3 panel.py plans
    python3 panel.py recharge --amount 20 --type weixin --qr ./qr_raw.png --poster ./qr_poster.png
    python3 panel.py status --order 202611250311219266
    python3 panel.py wait   --order 202611250311219266 --timeout 300
    python3 panel.py auto   --order 202611250311219266 --shop 1 --timeout 300
    python3 panel.py buy    --shop 1
    python3 panel.py sub    --protocol trojan
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

import requests

STATE_DIR = os.path.expanduser("~/.cache/sspanel-purchase")
SESSION_FILE = os.path.join(STATE_DIR, "session.json")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_PANEL = "https://panel.994938.xyz"
DEFAULT_GATEWAY = "http://pay.994938.xyz"

PAY_TYPES = {"weixin": "1", "wechat": "1", "alipay": "2", "1": "1", "2": "2"}

# 该站点常见 TLS 握手超时（实测 curl/requests 交替超时、偶发 6s+ 才连通），
# 因此所有请求统一走带重试的包装，默认 3 次、指数退避。
RETRIES = int(os.environ.get("SSPANEL_RETRIES", "3"))
TIMEOUT = float(os.environ.get("SSPANEL_TIMEOUT", "40"))

# 海报（二维码大图）中文字体候选，按优先级取第一个存在的。
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]


def rq(method, url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    last = None
    for i in range(RETRIES):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except Exception as e:  # 网络抖动/握手超时一律重试
            last = e
            if i < RETRIES - 1:
                time.sleep(2 * (i + 1))
    raise SystemExit(f"[FAIL] {method} {url} 重试 {RETRIES} 次仍失败：{type(last).__name__}: {last}")


# ---------------------------------------------------------------- 会话与配置
def load_state():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.chmod(SESSION_FILE, 0o600)


def get_session(args, fresh=False):
    """构造带 cookie 的会话。fresh=True 时不加载历史 cookie（换账号登录必须用）。

    base/gateway 优先级：命令行 > 环境变量 > 状态文件 > 默认值。
    """
    state = load_state()
    base = (args.base or os.environ.get("SSPANEL_BASE") or state.get("base") or DEFAULT_PANEL).rstrip("/")
    gateway = (
        args.gateway or os.environ.get("SSPANEL_GATEWAY") or state.get("gateway") or DEFAULT_GATEWAY
    ).rstrip("/")

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": base + "/user",
        }
    )
    if not fresh:
        for k, v in (state.get("cookies") or {}).items():
            s.cookies.set(k, v)
    return s, base, gateway


def persist(s, base, gateway):
    save_state(
        {
            "base": base,
            "gateway": gateway,
            "cookies": {c.name: c.value for c in s.cookies},
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


def need_login(s, base):
    """凭证失效时接口会 302 到登录页，这里用 /user 做一次探测。"""
    r = srq(s, "GET", base + "/user", allow_redirects=False)
    if r.status_code in (301, 302) and "auth/login" in r.headers.get("Location", ""):
        return False
    return r.status_code == 200


def srq(s, method, url, **kw):
    """带重试的 session 请求。"""
    kw.setdefault("timeout", TIMEOUT)
    last = None
    for i in range(RETRIES):
        try:
            r = s.request(method, url, **kw)
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except Exception as e:
            last = e
            if i < RETRIES - 1:
                time.sleep(2 * (i + 1))
    raise SystemExit(f"[FAIL] {method} {url} 重试 {RETRIES} 次仍失败：{type(last).__name__}: {last}")


def out(obj, as_json=False):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


def to_num(bal):
    """把 '20.00 CNY' / '0.00' 抽成 float；抽不到返回 0.0。"""
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(bal))
    return float(m.group(1)) if m else 0.0


# --------------------------------------------------------------------- 解析器
def node_values(html):
    """把 <div class='nodename'>X</div> ... <div class='nodetype'>Y</div> 抽成字典。"""
    result = {}
    for m in re.finditer(
        r'<div class="nodename">(.*?)</div>(.*?)(?=<div class="nodename">|<div class="user-info-bottom">)',
        html,
        re.S,
    ):
        name = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        body = m.group(2)
        val = re.search(r'<div class="nodetype">(.*?)</div>', body, re.S)
        text = re.sub(r"<[^>]+>", " ", val.group(1)) if val else ""
        result[name] = re.sub(r"\s+", " ", text).strip()
    return result


def parse_info(html):
    nodes = node_values(html)
    info = {}
    bal = nodes.get("余额", "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", bal)
    info["balance"] = m.group(1) + " CNY" if m else "unknown"
    info["level"] = nodes.get("帐号等级", "unknown")
    m = re.search(r"等级到期时间\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[ 0-9:]*)", html)
    info["level_expire"] = m.group(1).strip() if m else "unknown"
    m = re.search(r"账户有效时间[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", html)
    info["account_expire"] = m.group(1) if m else "unknown"
    info["devices"] = nodes.get("在线设备数", "unknown")
    m = re.search(r"今日已用</div>.*?<code>\s*([^<]+?)\s*</code>", html, re.S)
    if m:
        info["used_today"] = m.group(1)
    return info


def parse_plans(html):
    """解析商店卡片：优先取 shop-gridarea 区块，兼容单一卡片布局。"""
    plans = []
    blocks = re.split(r'<div class="shop-gridarea">', html)[1:] or [html]
    for b in blocks:
        bid = re.search(r"buy\('(\d+)'", b)
        if not bid:
            continue
        name = re.search(r'<div class="shop-name">\s*(?:<span>)?(.*?)(?:</span>)?\s*</div>', b, re.S)
        price = re.search(r"¥\s*([0-9]+(?:\.[0-9]+)?)", b)
        traffic = re.search(r'<div class="card-tag tag-cyan">\s*([0-9]+(?:\.[0-9]+)?)\s*G', b)
        days = re.search(r'<div class="card-tag tag-blue">\s*([0-9]+)\s*天', b)
        level = re.search(r'<div class="card-tag tag-gold">\s*(.*?)\s*</div>', b, re.S)
        plans.append(
            {
                "id": bid.group(1),
                "name": re.sub(r"<[^>]+>", "", name.group(1)).strip() if name else "?",
                "price": price.group(1) if price else "?",
                "traffic": traffic.group(1) + " G" if traffic else "?",
                "period": days.group(1) + " 天" if days else "?",
                "level": re.sub(r"<[^>]+>", "", level.group(1)).strip() if level else "?",
            }
        )
    return plans


# ------------------------------------------------------------ 二维码海报渲染
def _load_font(size):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def render_poster(raw_qr, out, amount, pay_type="微信", expire_min=5, tail_note=None):
    """把网关返回的 200×200 小二维码放大成带金额说明的竖版大图，便于手机端直接扫码。

    移动端聊天里小图/文件卡片常不显示，必须出一张字大图清的海报再走图片通道发送。
    """
    from PIL import Image, ImageDraw

    qr = Image.open(raw_qr).convert("RGB")
    W, H = 620, 820
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    f_title = _load_font(40)
    f_amount = _load_font(44)
    f_note = _load_font(24)
    f_tip = _load_font(22)

    title = f"{pay_type}扫码支付"
    tw = d.textlength(title, font=f_title)
    d.text(((W - tw) / 2, 60), title, font=f_title, fill="#111111")

    qr_size = 460
    qr = qr.resize((qr_size, qr_size), Image.NEAREST)  # 最近邻放大，保持码块锐利
    qx = (W - qr_size) // 2
    qy = 150
    img.paste(qr, (qx, qy))

    amount_text = f"应付 ¥{amount}"
    aw = d.textlength(amount_text, font=f_amount)
    d.text(((W - aw) / 2, qy + qr_size + 40), amount_text, font=f_amount, fill="#d81e06")

    if tail_note:
        nw = d.textlength(tail_note, font=f_note)
        d.text(((W - nw) / 2, qy + qr_size + 110), tail_note, font=f_note, fill="#555555")

    tip = f"二维码 {expire_min} 分钟内有效，过期请告知重新出码"
    pw = d.textlength(tip, font=f_tip)
    d.text(((W - pw) / 2, H - 70), tip, font=f_tip, fill="#888888")

    img.save(out, "PNG")
    return os.path.abspath(out)


# --------------------------------------------------------------------- 命令
def cmd_login(args):
    # 关键：换账号登录必须用干净会话，否则旧 cookie 会让 /auth/login 直接 302 到 /user，
    # 接口返回的是 HTML 页面而不是 {"ret":..} 的 JSON，脚本会误报“返回非 JSON”。
    s, base, gateway = get_session(args, fresh=True)
    payload = {
        "email": args.email,
        "passwd": args.password,
        "code": args.code or "",
        "remember_me": False,
    }
    r = srq(
        s,
        "POST",
        base + "/auth/login",
        json=payload,
        headers={"Origin": base, "Referer": base + "/auth/login"},
    )
    try:
        data = r.json()
    except Exception:
        hint = "（返回 HTML 通常是旧会话未清理，请确认已用 fresh 会话登录）"
        sys.exit(f"[FAIL] 登录接口返回非 JSON：HTTP {r.status_code} {r.text[:200]}{hint}")

    if data.get("ret") != 1:
        sys.exit(f"[FAIL] 登录失败：{data.get('msg')}")

    persist(s, base, gateway)
    info = {}
    try:
        info = parse_info(srq(s, "GET", base + "/user").text)
    except Exception:
        pass
    out({"login": "OK", "msg": data.get("msg"), **info}, args.json)


def cmd_info(args):
    s, base, gateway = get_session(args)
    if not need_login(s, base):
        sys.exit("[FAIL] 未登录或会话已过期，请先执行 login")
    out(parse_info(srq(s, "GET", base + "/user").text), args.json)


def cmd_plans(args):
    s, base, gateway = get_session(args)
    if not need_login(s, base):
        sys.exit("[FAIL] 未登录或会话已过期，请先执行 login")
    plans = parse_plans(srq(s, "GET", base + "/user/shop").text)
    if args.json:
        print(json.dumps(plans, ensure_ascii=False, indent=2))
    else:
        if not plans:
            print("（未解析到套餐，页面结构可能变化）")
        for p in plans:
            print(f"[id={p['id']}] {p['name']}  ¥{p['price']}  {p['traffic']} / {p['period']}  {p['level']}")


def _create_order(s, base, gateway, amount, pay_type):
    """下单：面板 302 → 网关 createOrder（返回 JS 跳转，内含 orderId）。"""
    r = s.get(
        base + "/user/payment/purchase/vmqpay",
        params={"price": amount, "type": pay_type},
        allow_redirects=False,
    )
    loc = r.headers.get("Location")
    if not loc:
        sys.exit(f"[FAIL] 未获得支付网关跳转，返回 HTTP {r.status_code}：{r.text[:200]}")

    g = rq("GET", loc, headers={"User-Agent": UA})
    m = re.search(r"orderId=(\d+)", g.text) or re.search(r"orderId=(\d+)", loc)
    if not m:
        sys.exit(f"[FAIL] 网关未返回订单号：{g.text[:300]}")
    order_id = m.group(1)

    det = rq("POST", gateway + "/getOrder", data={"orderId": order_id}).json()
    if det.get("code") != 1:
        sys.exit(f"[FAIL] 获取订单失败：{det.get('msg')}")
    d = det["data"]
    d["orderId"] = d.get("orderId") or order_id
    d["expire_seconds"] = int(d.get("timeOut", 5)) * 60
    return d


def cmd_recharge(args):
    s, base, gateway = get_session(args)
    if not need_login(s, base):
        sys.exit("[FAIL] 未登录或会话已过期，请先执行 login")

    pay_type = PAY_TYPES.get(str(args.type).lower())
    if not pay_type:
        sys.exit("[FAIL] --type 只支持 weixin / alipay")

    d = _create_order(s, base, gateway, args.amount, pay_type)
    really = d.get("reallyPrice")
    result = {
        "order_id": d["orderId"],
        "pay_id": d.get("payId"),
        "pay_type": "微信" if str(d.get("payType")) == "1" else "支付宝",
        "price": d.get("price"),
        "really_price": really,  # 用户实际需支付的金额（可能带对账尾数）
        "pay_url": d.get("payUrl"),
        "expire_seconds": d["expire_seconds"],
    }

    if args.qr:
        img = rq("GET", gateway + "/enQrcode", params={"url": d["payUrl"]}).content
        if len(img) < 200:
            q = urllib.parse.quote(d["payUrl"], safe="")
            img = rq("GET", gateway + "/enQrcode?url=" + q).content
        with open(args.qr, "wb") as f:
            f.write(img)
        result["qr_file"] = os.path.abspath(args.qr)
        result["qr_bytes"] = len(img)

    if args.poster:
        if not args.qr:
            sys.exit("[FAIL] --poster 需要同时用 --qr 落盘原始二维码")
        tail = None
        price_num = to_num(really)
        if abs(price_num - to_num(d.get("price"))) > 0.001:
            tail = f"含 {round(price_num - to_num(d.get('price')), 2):.2f} 元对账尾数，请按此金额支付"
        result["poster_file"] = render_poster(
            args.qr,
            args.poster,
            really,
            pay_type=result["pay_type"],
            expire_min=int(d.get("timeOut", 5)),
            tail_note=tail,
        )

    out(result, args.json)


def _check(gateway, order_id):
    r = rq("POST", gateway + "/checkOrder", data={"orderId": order_id})
    try:
        return r.json()
    except Exception:
        return {"code": -1, "msg": r.text[:200]}


def cmd_status(args):
    s, base, gateway = get_session(args)
    data = _check(gateway, args.order)
    paid = data.get("code") == 1
    res = {
        "paid": paid,
        "msg": data.get("msg") if not paid else "已支付",
        "redirect": data.get("data") if paid else None,
    }
    if need_login(s, base):
        res["balance"] = parse_info(srq(s, "GET", base + "/user").text)["balance"]
    out(res, args.json)


def cmd_wait(args):
    s, base, gateway = get_session(args)
    deadline = time.time() + args.timeout
    paid = False
    while time.time() < deadline:
        data = _check(gateway, args.order)
        if data.get("code") == 1:
            paid = True
            break
        # 兜底：checkOrder 可能滞后，余额已增加即视为到账。
        if need_login(s, base):
            cur = parse_info(srq(s, "GET", base + "/user").text)
            if args.baseline is not None and to_num(cur["balance"]) > args.baseline + 0.001:
                paid = True
                break
        time.sleep(args.interval)
    res = {"paid": paid, "order_id": args.order}
    if need_login(s, base):
        res["balance"] = parse_info(srq(s, "GET", base + "/user").text)["balance"]
    out(res, args.json)
    sys.exit(0 if paid else 2)


def _do_buy(s, base, args):
    before = parse_info(srq(s, "GET", base + "/user").text)
    r = srq(
        s,
        "POST",
        base + "/user/buy",
        data={
            "coupon": args.coupon or "",
            "shop": args.shop,
            "autorenew": 1 if args.autorenew else 0,
            "disableothers": 0 if args.keep_others else 1,
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": base + "/user/shop",
            "Origin": base,
        },
    )
    try:
        data = r.json()
    except Exception:
        sys.exit(f"[FAIL] 购买接口返回非 JSON：HTTP {r.status_code} {r.text[:300]}")

    res = {"success": data.get("ret") == 1, "msg": data.get("msg"), "balance_before": before["balance"]}
    if data.get("ret") == 1:
        after = parse_info(srq(s, "GET", base + "/user").text)
        res.update(
            {
                "balance_after": after["balance"],
                "level": after["level"],
                "level_expire": after["level_expire"],
                "account_expire": after["account_expire"],
            }
        )
    return res


def cmd_buy(args):
    s, base, gateway = get_session(args)
    if not need_login(s, base):
        sys.exit("[FAIL] 未登录或会话已过期，请先执行 login")
    res = _do_buy(s, base, args)
    out(res, args.json)
    sys.exit(0 if res["success"] else 1)


def collect_sub(s, base, protocol=None, raw=False):
    """取订阅地址并识别协议；抽成函数供 sub 与 auto 复用。"""
    html = srq(s, "GET", base + "/user").text
    tok = None
    m = re.search(r"/link/([A-Za-z0-9]{8,})", html)
    if m:
        tok = m.group(1)
    if not tok:
        return {"error": "页面中未找到订阅 token（套餐可能未生效或页面结构变化）"}

    variants = {
        "ssr": f"{base}/link/{tok}?sub=1",
        "ss": f"{base}/link/{tok}?sub=2",
        "vmess": f"{base}/link/{tok}?sub=3",
        "v2ray": f"{base}/link/{tok}?sub=3",
        "trojan": f"{base}/link/{tok}?sub=4",
        "clash": f"{base}/link/{tok}?clash=1",
        "shadowrocket": f"{base}/link/{tok}?list=shadowrocket",
    }

    if protocol and protocol.lower() in variants:
        url = variants[protocol.lower()]
    else:
        url = f"{base}/link/{tok}"

    info = {"url": url, "token": tok, "protocols": {}, "nodes": 0, "active": True}
    body = ""
    try:
        r = rq("GET", url, headers={"User-Agent": "v2rayN/6.31"})
        info["status"] = r.status_code
        body = r.text.strip()
    except SystemExit:
        info["status"] = "error"

    text = body
    if body and not body.startswith(("port:", "#", "proxies:")):
        try:
            import base64 as _b64
            text = _b64.b64decode(body + "=" * (-len(body) % 4)).decode("utf-8", "ignore")
        except Exception:
            text = body

    for proto in ("trojan", "vmess", "vless", "ssr", "ss"):
        n = len(re.findall(rf"^{proto}://", text, re.M))
        if n:
            info["protocols"][proto] = n
            info["nodes"] += n

    # 注意：节点名里的“过期时间：…”是元信息行，不是占位节点；
    # 只有出现字面“账户已过期”才判定为未生效。
    decoded_names = [urllib.parse.unquote(ln.split("#")[-1]) for ln in text.splitlines() if "#" in ln]
    if any("账户已过期" in n for n in decoded_names):
        info["active"] = False
    if raw:
        info["head"] = text[:200]

    info["all_urls"] = variants
    if protocol:
        info["matched"] = info["protocols"].get(protocol.lower(), 0)
    return info


def cmd_sub(args):
    s, base, gateway = get_session(args)
    if not need_login(s, base):
        sys.exit("[FAIL] 未登录或会话已过期，请先执行 login")
    out(collect_sub(s, base, args.protocol, raw=args.raw), args.json)


def cmd_auto(args):
    """后台续买：轮询到账（checkOrder 或余额增长）→ 买套餐 → 回读信息 → 取订阅。

    用于用户扫码后不再回来通知的场景；可放后台运行，结束后读输出文件/回传结果。
    前置：套餐 id 与价格已和用户确认；只买一次，不循环续费。
    """
    s, base, gateway = get_session(args)
    if not need_login(s, base):
        sys.exit("[FAIL] 未登录或会话已过期，请先执行 login")

    before = parse_info(srq(s, "GET", base + "/user").text)
    baseline = to_num(before["balance"])
    deadline = time.time() + args.timeout
    paid = False
    while time.time() < deadline:
        chk = _check(gateway, args.order)
        if chk.get("code") == 1:
            paid = True
            break
        cur = parse_info(srq(s, "GET", base + "/user").text)
        if to_num(cur["balance"]) > baseline + 0.001:
            paid = True
            break
        time.sleep(args.interval)

    res = {"order_id": args.order, "paid": paid, "balance_before": before["balance"]}
    if not paid:
        out(res, args.json)
        sys.exit(2)

    after = parse_info(srq(s, "GET", base + "/user").text)
    res["balance_after_wait"] = after["balance"]

    buy_args = args  # 复用 shop/coupon/autorenew/keep_others
    buy_res = _do_buy(s, base, buy_args)
    res["buy"] = buy_res
    if buy_res["success"]:
        res["sub"] = collect_sub(s, base, args.protocol or "trojan")
    out(res, args.json)
    sys.exit(0 if buy_res["success"] else 1)


def cmd_poster(args):
    """单独把已有原始二维码重排成海报大图。"""
    tail = args.tail_note
    out(
        {"poster_file": render_poster(
            args.qr, args.out, args.amount,
            pay_type=args.pay_type, expire_min=args.expire, tail_note=tail,
        )},
        args.json,
    )


def cmd_cookies(args):
    state = load_state()
    if not state.get("cookies"):
        sys.exit("[FAIL] 本地没有会话，请先 login")
    for k, v in state["cookies"].items():
        print(f"{k}={v}")
    if args.browser_cmd:
        for k, v in state["cookies"].items():
            print(f'agent-browser cookies set "{k}" "{v}" --domain {urllib.parse.urlparse(state["base"]).hostname} --path /')


# --------------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(description="SSPanel 面板充值/购买 CLI")
    ap.add_argument("--base", help=f"面板地址，默认 {DEFAULT_PANEL}")
    ap.add_argument("--gateway", help=f"支付网关地址，默认 {DEFAULT_GATEWAY}")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    # 全局 --json 同时挂到每个子命令，避免“--json 必须写在子命令前面”的位置限制。
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", parents=[json_parent], help="登录并保存会话")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--code", default="", help="两步验证码，未开启可忽略")
    p.set_defaults(func=cmd_login)

    sub.add_parser("info", parents=[json_parent], help="查看余额/等级/到期时间").set_defaults(func=cmd_info)
    sub.add_parser("plans", parents=[json_parent], help="列出商店套餐").set_defaults(func=cmd_plans)

    p = sub.add_parser("recharge", parents=[json_parent], help="创建充值订单，出原始二维码与海报大图")
    p.add_argument("--amount", required=True, type=float)
    p.add_argument("--type", default="weixin", help="weixin | alipay")
    p.add_argument("--qr", help="原始二维码 PNG 落盘路径")
    p.add_argument("--poster", help="海报大图（含金额说明）落盘路径；需同时给 --qr")
    p.set_defaults(func=cmd_recharge)

    p = sub.add_parser("status", parents=[json_parent], help="查询订单支付状态")
    p.add_argument("--order", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("wait", parents=[json_parent], help="轮询等待支付完成")
    p.add_argument("--order", required=True)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--baseline", type=float, default=None, help="轮询前余额（兜底按余额增长判定到账）")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("buy", parents=[json_parent], help="扣余额购买套餐")
    p.add_argument("--shop", required=True, help="套餐 id，可从 plans 获取")
    p.add_argument("--coupon", default="")
    p.add_argument("--autorenew", action="store_true", help="开启自动续费（默认关闭）")
    p.add_argument("--keep-others", action="store_true", help="保留其他套餐自动续费（默认关闭）")
    p.set_defaults(func=cmd_buy)

    p = sub.add_parser("auto", parents=[json_parent], help="轮询到账后自动购套餐并取订阅")
    p.add_argument("--order", required=True, help="充值订单号")
    p.add_argument("--shop", required=True, help="套餐 id")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--protocol", default="trojan", help="订阅协议，默认 trojan")
    p.add_argument("--coupon", default="")
    p.add_argument("--autorenew", action="store_true")
    p.add_argument("--keep-others", action="store_true")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("sub", parents=[json_parent], help="获取订阅地址（可指定协议）")
    p.add_argument("--protocol", help="trojan / vmess / ss / ssr / clash / shadowrocket")
    p.add_argument("--raw", action="store_true", help="附带响应前 200 字符便于核查")
    p.set_defaults(func=cmd_sub)

    p = sub.add_parser("poster", parents=[json_parent], help="把已有原始二维码重排成海报大图")
    p.add_argument("--qr", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--amount", required=True, help="应付金额，如 20.01")
    p.add_argument("--pay-type", default="微信")
    p.add_argument("--expire", type=int, default=5)
    p.add_argument("--tail-note", default=None)
    p.set_defaults(func=cmd_poster)

    p = sub.add_parser("cookies", parents=[json_parent], help="打印会话 cookie，可注入浏览器")
    p.add_argument("--browser-cmd", action="store_true", help="同时输出 agent-browser 注入命令")
    p.set_defaults(func=cmd_cookies)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
