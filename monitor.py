#!/usr/bin/env python3
"""Apple Store 零售店取货库存监控（apple.com.cn）

原理：购买页弹窗用的 /shop/fulfillment-messages 有 Apple Shield 反爬（要浏览器过工作量证明，
每个会话只给几十次额度，被拒后整个 IP 冷却半小时以上，挂机根本扛不住）。老接口
/shop/retail/pickup-message 和 /shop/delivery-message 没有这层防护，纯 HTTP 就能拿到同样的
「全部型号 × 附近门店」取货状态和送货日期，所以直接请求它们，不开浏览器。

用法：
  python3 monitor.py                                    # 监控 iPhone Duo 全部型号，北京
  python3 monitor.py --want "512GB 星光白色" --want 1TB  # 只对这些型号报警（引号内空格=且，多个 --want=或）
  python3 monitor.py --once                             # 查一次打印结果就退出
  python3 monitor.py --test-notify                      # 测试通知通道
  caffeinate -i python3 monitor.py ...                  # 挂机时防止 Mac 睡眠

手机推送（可选，环境变量）：
  微信（直连微信公众平台测试号，不经第三方）：
    WX_APPID / WX_SECRET    测试号页面的 appID / appsecret
    WX_OPENID               关注测试号后「用户列表」里你的微信号（openid）
    WX_TEMPLATE_ID          新增的模板消息 ID，模板内容填：{{title.DATA}} {{content.DATA}}
  BARK_KEY=xxx              iOS Bark App 的 key（或完整推送地址）

库存变化记录在同目录 events.jsonl，可以用来摸补货规律；发现新货时写 trigger.json 给 autoorder.py serve。
"""
import argparse
import collections
import gzip
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BASE = "https://www.apple.com.cn"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
PARTS_PER_CALL = 8         # 实测 8 个型号一次查没问题
# 实测 Apple 按 IP 做 60 分钟滑动窗口限流，约 75 次请求就 541（换接口也一样，被拒时的重试也算），
# 触发后罚约 30 分钟。自己卡在 70 次；万一还是被拒，每次自动再降 5 次，免得反复撞线
HOURLY_BUDGET = 70
DELIVERY_EVERY = 30 * 60   # 送货日期变得慢，省着点查
EVENTS_LOG = Path(__file__).with_name("events.jsonl")
TRIGGER = Path(__file__).with_name("trigger.json")  # 有货信号，给 autoorder.py serve 用


def log(msg):
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def record(event, **kw):
    with EVENTS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw},
                           ensure_ascii=False) + "\n")


# ---------------- 通知 ----------------

def notify(title, body, url=None, urgent=False, open_url=False):
    log(f"🔔 {title}\n{body}")
    try:
        subprocess.run(["osascript", "-e", "on run argv",
                        "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
                        "-e", "end run", title, body], timeout=5, capture_output=True)
        if urgent:  # 连响几声，免得错过
            subprocess.Popen(["sh", "-c", "for i in 1 2 3 4 5; do afplay /System/Library/Sounds/Glass.aiff; done"])
            if open_url and url:
                subprocess.run(["open", url])
    except Exception as e:
        log(f"本机通知失败：{e}")
    _push_wechat(title, body, url)
    _push_bark(title, body, url, urgent)


def _push_bark(title, body, url, urgent):
    key = os.environ.get("BARK_KEY", "").strip()
    if not key:
        return
    base = key.rstrip("/") if key.startswith("http") else f"https://api.day.app/{key}"
    q = {"group": "Apple库存"}
    if url:
        q["url"] = url
    if urgent:
        q.update(level="timeSensitive", call="1")  # call=1：铃声重复响 30 秒
    path = "/".join(urllib.parse.quote(s, safe="") for s in (title, body))
    try:
        resp = json.loads(urllib.request.urlopen(f"{base}/{path}?{urllib.parse.urlencode(q)}", timeout=10).read())
        if resp.get("code") != 200:
            log(f"Bark 推送失败：{resp}")
    except Exception as e:
        log(f"Bark 推送失败：{e}")


WX_KEYS = ("WX_APPID", "WX_SECRET", "WX_OPENID", "WX_TEMPLATE_ID")
_wx_token = {"value": "", "exp": 0.0}


def _wx_access_token(force=False):
    if force or time.time() > _wx_token["exp"]:
        q = urllib.parse.urlencode({"grant_type": "client_credential",
                                    "appid": os.environ["WX_APPID"], "secret": os.environ["WX_SECRET"]})
        resp = json.loads(urllib.request.urlopen(f"https://api.weixin.qq.com/cgi-bin/token?{q}", timeout=10).read())
        if "access_token" not in resp:
            raise RuntimeError(f"获取 access_token 失败：{resp}")
        _wx_token.update(value=resp["access_token"], exp=time.time() + resp["expires_in"] - 300)
    return _wx_token["value"]


def _wx_send(msg, force_token=False):
    req = urllib.request.Request(
        f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={_wx_access_token(force_token)}",
        data=json.dumps(msg, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def _push_wechat(title, body, url):
    cfg = {k: os.environ.get(k, "").strip() for k in WX_KEYS}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        if len(missing) < len(cfg):  # 配了一半，多半是 export 写错了
            log(f"微信推送跳过：缺 {' '.join(missing)}")
        return
    # 微信卡片上 title 字段不显示，标题并进 content
    msg = {"touser": cfg["WX_OPENID"], "template_id": cfg["WX_TEMPLATE_ID"],
           "data": {"title": {"value": title}, "content": {"value": f"{title}\n{body}"}}}
    if url:
        msg["url"] = url
    try:
        resp = _wx_send(msg)
        if resp.get("errcode") in (40001, 42001):  # access_token 失效/过期，刷新后重试一次
            resp = _wx_send(msg, force_token=True)
        if resp.get("errcode"):
            log(f"微信推送失败：{resp}")
        else:
            log("微信推送成功")
    except Exception as e:
        log(f"微信推送失败：{e}")


# ---------------- 抓取 ----------------

@dataclass
class Snapshot:
    titles: dict = field(default_factory=dict)    # part -> 中文型号名
    delivery: dict = field(default_factory=dict)  # part -> 送货文案，如「预计 1 月 22 日发货」
    stores: dict = field(default_factory=dict)    # storeNumber -> {name, city, km}
    avail: dict = field(default_factory=dict)     # (storeNumber, part) -> 取货文案


def parse_pickup(stores, snap):
    for s in stores:
        sn = s["storeNumber"]
        snap.stores[sn] = {"name": s["storeName"], "city": s.get("city", ""), "km": s.get("storedistance") or 0}
        for part, pa in s["partsAvailability"].items():
            title = pa.get("messageTypes", {}).get("regular", {}).get("storePickupProductTitle")
            if title:
                snap.titles[part] = title
            if pa.get("pickupDisplay") == "available":
                snap.avail[(sn, part)] = pa.get("pickupSearchQuote") or "可取货"


def parse_delivery(dm, delivery):
    # deliveryMessage 里混着 geoLocated 之类的非型号键
    for part, v in dm.items():
        msgs = v.get("regular", {}).get("deliveryOptionMessages") if isinstance(v, dict) else None
        if msgs:
            delivery[part] = msgs[0].get("displayName", "")


class AppleClient:
    def __init__(self, product, location):
        self.location = location
        self.buy_url = f"{BASE}/shop/buy-iphone/{product}"
        self.products = {}  # part -> 英文名（购买页里的型号列表）
        self.delivery = {}  # 上次查到的送货文案，没到时间就沿用
        self.delivery_at = 0.0
        self.sent = collections.deque()  # 最近 60 分钟内每次请求的时间
        self.budget = HOURLY_BUDGET      # 每小时请求上限，被 541 后自动调低

    def gap(self, min_interval):
        """把每小时额度均匀铺开：每轮 1 次取货请求，另有每 DELIVERY_EVERY 1 次送货请求，留 1 次余量"""
        return max(min_interval, 3600 / (self.budget - 3600 / DELIVERY_EVERY - 1))

    def _get(self, url):
        while self.sent and time.time() - self.sent[0] > 3600:
            self.sent.popleft()
        if len(self.sent) >= self.budget:
            wait = 3600 - (time.time() - self.sent[0]) + 1
            log(f"60 分钟内已请求 {len(self.sent)} 次，到自设上限了，等 {wait:.0f}s")
            time.sleep(wait)
        self.sent.append(time.time())
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":  # 购买页走 CDN，不要也给 gzip
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}") from None

    def _api(self, path, parts):
        q = [("pl", "true"), ("location", self.location)] + [(f"parts.{i}", p) for i, p in enumerate(parts)]
        body = json.loads(self._get(f"{BASE}{path}?{urllib.parse.urlencode(q)}"))["body"]
        if body.get("errorMessage"):  # 比如位置写错：「请输入有效的省/市名称或邮政编码。」
            raise RuntimeError(body["errorMessage"])
        return body

    def snapshot(self):
        if not self.products:
            m = re.search(r'"products":(\[\{"sku".*?\}\])', self._get(self.buy_url))
            if not m:
                raise RuntimeError(f"购买页里没找到型号列表：{self.buy_url}")
            self.products = {p["partNumber"]: p["name"] for p in json.loads(m.group(1))}
            log(f"购买页共 {len(self.products)} 个型号")
        parts = list(self.products)
        chunks = [parts[i:i + PARTS_PER_CALL] for i in range(0, len(parts), PARTS_PER_CALL)]
        snap = Snapshot()
        for chunk in chunks:
            parse_pickup(self._api("/shop/retail/pickup-message", chunk)["stores"], snap)
        if time.time() >= self.delivery_at:
            try:
                delivery = {}
                for chunk in chunks:
                    parse_delivery(self._api("/shop/delivery-message", chunk)["content"]["deliveryMessage"], delivery)
                self.delivery, self.delivery_at = delivery, time.time() + DELIVERY_EVERY
            except Exception as e:  # 送货日期是次要信息，查不到不耽误取货监控
                log(f"送货日期没查到（{e}），沿用上次")
                self.delivery_at = time.time() + 60
        snap.delivery = dict(self.delivery)
        return snap


# ---------------- 比对 & 报警 ----------------

def matches(text, wants):
    text = text.lower()
    return not wants or any(all(k.lower() in text for k in w.split()) for w in wants)


class Monitor:
    def __init__(self, args, buy_url):
        self.args, self.buy_url = args, buy_url
        self.prev_avail = {}     # (storeNumber, part) -> {title, store, km, quote}
        self.prev_delivery = {}  # part -> 送货文案
        self.rounds = 0

    def process(self, snap, en_names):
        a = self.args
        self.rounds += 1
        parts = [p for p in snap.titles if matches(f"{snap.titles[p]} {en_names.get(p, '')}", a.want)]
        stores = {sn: s for sn, s in snap.stores.items() if not a.max_km or s["km"] <= a.max_km}
        avail = {}
        for (sn, part), quote in snap.avail.items():
            if part in parts and sn in stores:
                avail[(sn, part)] = {"title": snap.titles[part], "store": stores[sn]["name"],
                                     "km": stores[sn]["km"], "quote": quote}

        new = [k for k in avail if k not in self.prev_avail]
        if new:
            tmp = TRIGGER.with_suffix(".tmp")  # 先写信号再发通知，让下单脚本最快开抢
            tmp.write_text(json.dumps({"ts": time.time(), "avail": [list(k) for k in avail]}))
            tmp.replace(TRIGGER)
            for k in new:
                record("in_stock", part=k[1], store_no=k[0], **avail[k])
            # 有新货的规格，列出它当前所有有货门店（Apple 不给台数，只有有/无）
            lines = []
            for p in dict.fromkeys(k[1] for k in new):
                xs = sorted((x for (sn, pp), x in avail.items() if pp == p), key=lambda x: x["km"])
                quotes = {x["quote"] for x in xs}
                when = f"，{quotes.pop()}" if len(quotes) == 1 else ""
                lines.append(f"{snap.titles[p]}：{'、'.join(x['store'] for x in xs)}（{len(xs)} 家店{when}）")
            # 链接直达第一个有货型号的配置页（颜色、容量已选好），手动抢少点两步
            first = new[0][1]
            notify(f"🍎 有货了！{len(lines)} 个规格", "\n".join(lines),
                   f"{self.buy_url}/{first.lower()}", urgent=True, open_url=a.open)
        for k in self.prev_avail.keys() - avail.keys():
            x = self.prev_avail[k]
            log(f"已售罄：{x['title']} @ {x['store']}")
            record("sold_out", part=k[1], store_no=k[0], **x)
        self.prev_avail = avail

        changes = []
        for p in parts:
            q, old = snap.delivery.get(p), self.prev_delivery.get(p)
            if q and old and q != old:
                changes.append(f"{snap.titles[p]}：{old} → {q}")
                record("delivery_change", part=p, title=snap.titles[p], old=old, new=q)
            if q:
                self.prev_delivery[p] = q
        if changes:
            notify("📦 送货日期变了", "\n".join(changes), self.buy_url)

        if a.once:
            print(f"\n位置「{a.location}」附近 {len(stores)} 家店："
                  + "、".join(f"{s['name']}({s['km']:g}km)" for s in stores.values()))
            for p in parts:
                got = [x["store"] for (sn, pp), x in avail.items() if pp == p]
                print(f"  {snap.titles[p]}\n    送货：{snap.delivery.get(p, '-')}"
                      f"　取货：{'、'.join(got) if got else '均无货'}")
            return
        quotes = " / ".join(sorted({snap.delivery[p] for p in parts if p in snap.delivery})) or "-"
        detail = ("　有货：" + "、".join(f"{x['store']}·{x['title']}" for x in avail.values())) if avail else ""
        log(f"#{self.rounds} 门店有货 {len(avail)}/{len(parts) * len(stores)} | 送货：{quotes}{detail}")


def run(args):
    client = AppleClient(args.product, args.location)
    mon = Monitor(args, client.buy_url)
    log(f"开始监控 {client.buy_url}，位置「{args.location}」，"
        f"报警型号：{' | '.join(args.want) or '全部'}，间隔 ~{client.gap(args.interval):.0f}s")
    fails = 0
    while True:
        try:
            snap = client.snapshot()
        except Exception as e:
            if args.once:
                raise
            fails += 1
            if "541" in str(e) and client.budget > 45:
                client.budget -= 5
                log(f"被 Apple 限流了，每小时请求上限降到 {client.budget} 次")
            # 541 是撞了限流，密集重试只会把窗口填得更满，直接隔 10 分钟
            wait = 600 if "541" in str(e) else min(10 * 3 ** (fails - 1), 600)
            log(f"查询失败（{e}），{wait}s 后重试")
            if fails == 4:
                notify("⚠️ 库存监控连续失败", f"已连续失败 {fails} 次：{e}")
            time.sleep(wait)
            continue
        if fails >= 4:
            notify("✅ 库存监控已恢复", f"之前连续失败 {fails} 次，现在正常了")
        elif mon.rounds == 0 and not args.once:  # 重启后报个平安，免得微信里最后一条还是「失败」
            notify("✅ 库存监控已启动",
                   f"{len(client.products)} 个规格 × {len(snap.stores)} 家店，约 {client.gap(args.interval):.0f} 秒查一次")
        fails = 0
        mon.process(snap, client.products)
        if args.once:
            break
        time.sleep(client.gap(args.interval) * random.uniform(0.85, 1.15))


def main():
    ap = argparse.ArgumentParser(description="Apple Store 零售店取货库存监控")
    ap.add_argument("--product", default="iphone-duo", help="购买页路径 /shop/buy-iphone/<product>，默认 iphone-duo")
    ap.add_argument("--location", default="北京 北京 东城区", help="查询位置（省 市 区 或邮编），默认北京东城区")
    ap.add_argument("--want", action="append", default=[], help='只对匹配的型号报警，如 "512GB 星光白色"；可多次指定')
    ap.add_argument("--max-km", type=float, default=0, help="只看这个距离内的门店，0=不限（北京默认会带上天津 3 家店）")
    ap.add_argument("--interval", type=float, default=0,
                    help="最短轮询间隔秒数（±15%% 随机抖动）；默认 0 = 按每小时请求上限自动均匀铺开（约 54 秒）")
    ap.add_argument("--open", action="store_true", help="有货时自动用默认浏览器打开购买页")
    ap.add_argument("--once", action="store_true", help="查一次打印结果后退出")
    ap.add_argument("--test-notify", action="store_true", help="发一条测试通知后退出")
    args = ap.parse_args()
    # 不走 macOS 系统代理（Clash 127.0.0.1:7897）：代理重启时会 Connection refused，Apple 中国站和微信接口直连就行
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    if args.test_notify:
        wx_missing = [k for k in WX_KEYS if not os.environ.get(k, "").strip()]
        log("微信配置：" + (f"缺 {' '.join(wx_missing)}" if wx_missing else "四项都已设置"))
        notify("🍎 测试通知", "Apple 库存监控通知通道正常", f"{BASE}/shop/buy-iphone/{args.product}", urgent=True)
        return
    try:
        run(args)
    except KeyboardInterrupt:
        log("已停止")


if __name__ == "__main__":
    main()
