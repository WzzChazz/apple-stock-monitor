#!/usr/bin/env python3
"""Apple Store 门店取货自动下单（只给自己用：你自己的一个账户、只下一单，付款由你本人完成）

测试模式（你自己在终端里跑，会弹出浏览器窗口；一律停在「立即下单」之前，不会下单）：
  python3 autoorder.py dryrun MDGD4PA/A R448                     # 用连接线走一遍完整取货流程
  python3 autoorder.py dryrun MK2T4CH/A R448 --until pickup      # iPhone 只看门店列表
  python3 autoorder.py dryrun MK2T4CH/A R448 --until location    # 测试把位置固定成北京
每一步的截图、页面元素、日志都存在 runs/<时间>/。
结账时 Apple 要求登录的话，脚本会停下来等你在窗口里亲手登录（含验证码、隐私同意）。

常驻抢购（monitor.py 发现有货会写 trigger.json，这里看到就抢；北京 6 家店、iPhone Duo 任意规格）：
  python3 autoorder.py serve                                     # 演练：走到「立即下单」前停住
  python3 autoorder.py serve --live                              # 正式：真下单，成功一次就写 ordered.lock 不再下第二单
  python3 autoorder.py serve --parts MDGD4PA/A --selftest R448   # 用连接线假装王府井有货，演练一遍就退出
每次抢完不管成没成都会推送结果和各步耗时（慢了一步 / 卡在哪一步），记在 attempts.jsonl；没抢成但信号还新鲜会马上再试一次。
站位放的型号默认按 events.jsonl 挑最近 7 天补货最多的。看补货规律和抢购记录：python3 stats.py

取货人等信息写在同目录 .env（登录后账户里已有的会直接用，这里只用来补空着的项）：
  ORDER_LAST_NAME / ORDER_FIRST_NAME   取货人姓、名（要和到店出示的证件一致）
  ORDER_EMAIL / ORDER_PHONE            邮箱、手机号
  ORDER_PAYMENT                        付款方式：支付宝 / 微信（测试时留空默认用支付宝走到最后一步）

其它：
  python3 autoorder.py login   弹窗手动登录 Apple 账户（登录态存在 .apple-profile/，不要发给别人）
  python3 autoorder.py check   看登录 cookie 还在不在
"""
import argparse
import asyncio
import collections
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".apple-profile"
RUNS = HERE / "runs"
TRIGGER = HERE / "trigger.json"    # monitor.py 发现有货时写入：{"ts": 时间戳, "avail": [[门店, 型号], ...]}
LOCK = HERE / "ordered.lock"       # 下单成功后写入，之后拒绝再跑，保证只下一单
EVENTS = HERE / "events.jsonl"     # monitor.py 记的有货/售罄，用来挑站位型号
ATTEMPTS = HERE / "attempts.jsonl"  # 每次抢购的结果和各步耗时，stats.py 汇总
REPOSITION_EVERY = 15 * 60         # 结账会话约 20 分钟就过期（实测，刷新页面续不上），提前重新进一次结账
BASE = "https://www.apple.com.cn"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
STORES = {"R448": "王府井", "R320": "三里屯", "R388": "西单大悦城",
          "R479": "华贸购物中心", "R645": "朝阳大悦城", "R792": "北京荟聚"}
PART_NAMES = {"MK2M4CH/A": "256GB 星光白色", "MK2N4CH/A": "256GB 夜空色",   # iPhone Duo 全部 8 个规格
              "MK2P4CH/A": "512GB 星光白色", "MK2Q4CH/A": "512GB 夜空色",
              "MK2T4CH/A": "1TB 星光白色", "MK2U4CH/A": "1TB 夜空色",
              "MK2V4CH/A": "2TB 星光白色", "MK2W4CH/A": "2TB 夜空色"}
# 没有 events.jsonl 记录时站位放的型号（10-23 北京补货 40 次里它占 11 次，最多，也最便宜）；
# 有记录就自动挑最近 7 天补货最多的，见 most_restocked()
DEFAULT_PREFER = "MK2M4CH/A"
STEP_NAMES = {"Fulfillment": "取货方式", "PickupContact": "取货人", "Billing": "付款方式", "Review": "检查订单"}
# 测试模式下碰到这些字样的按钮一律不点
SUBMIT_WORDS = ("提交订单", "立即下单", "下单", "确认支付", "立即付款", "去付款", "Place")

DUMP_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const lab = e => (e.labels && e.labels[0] ? e.labels[0].innerText : '') || e.getAttribute('aria-label') || e.placeholder || '';
  const inputs = [...document.querySelectorAll('input, select, textarea')].filter(e => e.type !== 'hidden').map(e => ({
    tag: e.tagName, type: e.type, name: e.name, value: (e.value || '').slice(0, 40), id: e.id, checked: e.checked,
    disabled: e.disabled, required: e.required, label: lab(e).replace(/\\s+/g, ' ').slice(0, 80), vis: vis(e),
    autom: e.getAttribute('data-autom') || ''}));
  const buttons = [...document.querySelectorAll('button, a, [role=button]')].filter(vis).map(e => ({
    tag: e.tagName, text: (e.innerText || e.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').slice(0, 60),
    disabled: e.disabled || e.getAttribute('aria-disabled') === 'true', id: e.id,
    autom: e.getAttribute('data-autom') || ''})).filter(b => b.tag === 'BUTTON' || (b.autom && !b.autom.includes('footer') && !b.autom.startsWith('gn_')));
  return {url: location.href, title: document.title, inputs, buttons};
}"""


def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


async def open_profile(pw, headless):
    PROFILE.mkdir(mode=0o700, exist_ok=True)
    return await pw.chromium.launch_persistent_context(
        str(PROFILE), headless=headless, locale="zh-CN", user_agent=UA,
        viewport={"width": 1280, "height": 900}, args=["--disable-blink-features=AutomationControlled"])


class Stop(Exception):
    """测试按 --until 提前结束"""


async def wait_until(check, timeout, every=0.05):
    """每 every 秒查一次 check()，一变真就返回 True，超时返回 False（代替「先等几秒再看」）"""
    end = time.time() + timeout
    while not await check():
        if time.time() > end:
            return False
        await asyncio.sleep(every)
    return True


class Run:
    """一次下单流程：日志、每一步的截图和页面元素都存到 runs/<时间>/"""

    def __init__(self, page, ctx, dry, until="review"):
        self.page, self.ctx, self.dry, self.until = page, ctx, dry, until
        self.alert = None     # 常驻模式下设成发微信的函数
        self.bag_part = None  # 当前站位时购物袋里放的型号
        self.dir = RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.t0 = time.time()
        self.n = 0
        self.inflight = {}    # 页面上还没回来的 Apple 接口请求 -> 发出时间（watch_checkout_api 记）
        self.api_at = 0.0     # 最近一次有接口请求发出或回来的时间
        self.sent = collections.deque(maxlen=200)  # 最近发出的接口请求 (时间, URL)，press() 用来确认点上了
        self.saving = set()   # 后台还没存完的页面
        self.t_hit = None     # 开抢时间，下面各步耗时从这里算
        self.marks = []       # [(走到哪一步, 开抢后秒数), ...]
        self.pick = None      # 这次抢的门店

    def log(self, msg):
        line = f"[{time.time() - self.t0:6.1f}s] {msg}"
        print(line, flush=True)
        with (self.dir / "run.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def dump(self, name):
        self.n += 1
        stem = self.dir / f"{self.n:02d}_{name}"
        url = self.page.url
        try:  # 截图最慢放最后：后台存时页面可能已经往下走了，元素和文字先落盘才和这一刻对得上
            Path(f"{stem}.json").write_text(json.dumps(await self.page.evaluate(DUMP_JS), ensure_ascii=False, indent=1))
            Path(f"{stem}.txt").write_text(await self.page.evaluate("() => document.body.innerText"))
            await self.page.screenshot(path=f"{stem}.png", caret="initial")
        except Exception as e:
            self.log(f"保存 {name} 失败：{e}")
        self.log(f"已保存 {stem.name}  {url}")

    def snap(self, name):
        """抢购路上用：后台存一份当前页面，不耽误下一步（每一步都干等截图，几步下来就是好几秒）"""
        task = asyncio.ensure_future(self.dump(name))
        self.saving.add(task)
        task.add_done_callback(self.saving.discard)

    async def flush(self):
        """等后台存的页面都写完"""
        if self.saving:
            await asyncio.wait(self.saving, timeout=15)

    def mark(self, what):
        """记下抢购走到了哪一步（开抢后第几秒），没抢成时一眼看出慢在哪、卡在哪"""
        if self.t_hit:
            self.marks.append((what, round(time.time() - self.t_hit, 1)))

    def timeline(self):
        return " → ".join(f"{w} {s}s" for w, s in self.marks) or "还没开始"

    async def moved_on(self, before, since, timeout):
        """点完「继续」后等页面离开 before 这个 URL。接口都回来 3 秒了还停在原地，多半是被 Apple 拒了
        （刚好没货、表单报错之类），返回 False，不再干等到超时"""
        end = time.time() + timeout
        while self.page.url == before:
            busy = any(t >= since - 1 for t in self.inflight.values())
            if time.time() > end or (not busy and time.time() - max(self.api_at, since) > 3):
                return False
            await asyncio.sleep(0.1)
        return True

    async def settle(self, quiet=0.3, timeout=5):
        """代替固定干等：等页面发出去的 Apple 接口请求都回来、再安静 quiet 秒。刚点完按钮请求可能还没发出去，
        所以至少等 quiet 秒；开始等之前 3 秒就已经挂着的请求（长连接之类）不算"""
        t0 = time.time()
        while True:
            busy = [r for r, t in self.inflight.items() if t > t0 - 3]
            if not busy and time.time() - max(self.api_at, t0) >= quiet:
                return
            if time.time() - t0 > timeout:
                self.log(f"等了 {timeout}s 接口还没回完，先往下走：{[r.url.split('apple.com.cn', 1)[-1][:80] for r in busy]}")
                return
            await asyncio.sleep(0.05)

    async def click(self, locator, what):
        await locator.wait_for(state="visible", timeout=15000)
        text = (await locator.inner_text(timeout=3000)).strip()
        if self.dry and any(w in text for w in SUBMIT_WORDS):
            raise RuntimeError(f"测试模式不点「{text}」")
        for _ in range(150):  # 按钮可能还在加载（灰色转圈），等它能点
            if await locator.is_enabled():
                break
            await self.page.wait_for_timeout(100)
        # 滚到屏幕中间再点：滚到顶部会被吸顶的「结账」标题栏挡住（实测点省份标签时被挡超时）
        await locator.evaluate("e => e.scrollIntoView({block: 'center'})")
        try:
            # 很多按钮一点就跳页，不在原页面上干等（会因按钮消失而超时），跳转由调用方自己等
            await locator.click(timeout=5000, no_wait_after=True)
        except Exception as e:
            if "intercepts pointer events" not in str(e):
                raise
            await locator.dispatch_event("click")  # 还被别的元素挡着，就直接派发点击事件
        self.log(f"点击 {what}「{text[:30]}」")


def is_api(r):
    """页面发给 Apple 商店的接口请求（不算图片、脚本这些静态资源）"""
    u = r.url
    return "/shop/" in u and (r.resource_type in ("xhr", "fetch") or "/shop/checkout" in u or "/shop/bagx" in u)


def is_search(url):
    """「按位置查各店库存」的接口"""
    return "storeLocator" in url and "_a=search" in url


async def press(run, locator, what, expect, shown=None, every=0.5, tries=10):
    """点一个会发 Apple 接口的按钮，并确认页面真有反应：发出了 expect(url) 匹配的请求、页面跳走了，或者 shown 出现了。
    页面刚打开时脚本可能还没接管按钮，点了没反应，就隔 every 秒再点；按钮变灰/不见了说明在处理，就不再点"""
    page = run.page
    for _ in range(tries):
        t, url = time.time(), page.url
        await run.click(locator, what)

        async def took(t=t, url=url):
            return (page.url != url or any(ts >= t and expect(u) for ts, u in run.sent)
                    or bool(shown and await shown.first.is_visible()))

        if await wait_until(took, every):
            return True
        try:
            if not await locator.is_visible() or not await locator.is_enabled(timeout=1000):
                return True
        except Exception:
            return True
        run.log(f"点「{what}」{every:g} 秒没反应（页面可能还没准备好），再点一次")
    run.log(f"「{what}」点了 {tries} 次都没看到反应，接着等")
    return False


async def wait_shield(run):
    for _ in range(30):
        if any(c["name"] == "shld_bt_ck" for c in await run.ctx.cookies()):
            break
        await run.page.wait_for_timeout(1000)
    await run.page.wait_for_timeout(3000)  # cookie 出现时校验还没收尾，马上请求会 541（monitor 里实测过）


async def bag_lines(page):
    """等购物袋页渲染完（出现结账按钮或「没有商品」），返回商品行数"""
    done = page.locator('[data-autom="checkout"]').or_(page.get_by_text("你的购物袋中没有商品"))
    await done.first.wait_for(timeout=20000)
    return await page.locator('[data-autom="bag-item-remove-button"]').count()


async def prepare_bag(run, part):
    """清空购物袋，只放目标这一件"""
    page = run.page
    await page.goto(f"{BASE}/shop/bag", wait_until="domcontentloaded", timeout=60000)
    await wait_shield(run)
    for _ in range(10):
        n = await bag_lines(page)
        if not n:
            break
        await page.locator('[data-autom="bag-item-remove-button"]').first.click(timeout=10000)
        for _ in range(100):  # 等这一行真的移除掉
            await page.wait_for_timeout(100)
            if await page.locator('[data-autom="bag-item-remove-button"]').count() < n:
                break
    else:
        raise RuntimeError("购物袋清不空")
    run.log("购物袋已清空")
    # 加购物袋接口要带 atbtoken：先请求 beacon/atb 拿 cookie as_atb，取最后一段
    await page.evaluate("async () => { await fetch('/shop/beacon/atb'); }")
    atb = next((c["value"] for c in await run.ctx.cookies() if c["name"] == "as_atb"), "")
    res = await page.evaluate("""async ([part, tok]) => {
      const r = await fetch('/shop/bag/add?product=' + encodeURIComponent(part) + '&atbtoken=' + tok);
      return await r.text();
    }""", [part, atb.split("|")[-1]])
    data = json.loads(res)
    if not data.get("addedToBag"):
        raise RuntimeError(f"加入购物袋失败：{data.get('responseCode')} {data.get('message')}")
    await page.goto(f"{BASE}/shop/bag", wait_until="domcontentloaded", timeout=60000)
    n = await bag_lines(page)
    qty = await page.locator('[data-autom="item-quantity-dropdown"]').evaluate_all("es => es.map(e => e.value)")
    if n != 1 or qty != ["1"]:
        raise RuntimeError(f"购物袋里应只有 1 件，实际 {n} 行、数量 {qty}")
    run.log(f"已加入购物袋：{part}（核对过只有这 1 件）")
    if run.until == "bag":
        await run.dump("bag")
        raise Stop("bag")


async def enter_checkout(run):
    page = run.page
    run.snap("bag")
    # 购物袋页刚打开时页面脚本可能还没接管「结账」按钮，press 确认真点上了，没点上就再点
    await press(run, page.locator('[data-autom="checkout"]').first, "结账",
                lambda u: "/shop/bagx" in u or "/shop/checkout" in u)
    # 不等整页加载完（图片、统计脚本），后面要点的按钮各自会等、会确认
    await page.wait_for_url(lambda u: "signIn" in u or "/checkout" in u, wait_until="domcontentloaded", timeout=30000)
    if "signIn" in page.url:
        await run.dump("signin")
        await page.bring_to_front()
        if run.alert:
            run.alert("⚠️ 自动下单需要你重新登录", "Apple 结账要求登录，请到弹出的窗口里登录（最多等 10 分钟）")
        run.log(">>> 请在弹出的窗口里登录你的 Apple 账户（含验证码）；如有隐私同意，也请亲手勾选后继续。最多等 10 分钟")
        await page.wait_for_url(lambda u: "signIn" not in u, timeout=600_000)
        run.log("已登录")
    run.log("进入结账页")


def step_of(url):
    m = re.search(r"[?&]_s=([A-Za-z]+)", url)
    return m.group(1) if m else ""


async def click_continue(run, automs=()):
    page = run.page

    def to_apple(u):
        return "/shop/checkoutx" in u

    for _ in range(150):  # 新一步刚渲染时按钮可能还没出来，最多等 15 秒
        for a in (*automs, "continue-button", "continue-button-label", "continue-button-review"):
            b = page.locator(f'[data-autom="{a}"]')
            if await b.count() and await b.first.is_visible():
                await press(run, b.first, a, to_apple, every=1, tries=3)
                return
        await page.wait_for_timeout(100)
    b = page.get_by_role("button", name=re.compile("^继续"))
    if await b.count():
        await press(run, b.first, "继续", to_apple, every=1, tries=3)
        return
    raise RuntimeError("找不到「继续」按钮")


async def select_store(run, store_no):
    """点门店卡片上「Apple 王府井」这几个字；列表里没有就先展开「显示更多取货地点」"""
    page = run.page
    card = page.get_by_text(re.compile(rf"^Apple\s+{re.escape(STORES[store_no])}$")).filter(visible=True)
    more = page.get_by_text(re.compile("显示更多取货地点|显示更多零售店")).filter(visible=True)
    # 列表按 Apple 猜的定位城市排序（实测有时猜成沈阳），北京的店可能折叠在「显示更多取货地点」里
    for _ in range(3):
        if await card.count() or not await more.count():
            break
        await run.click(more.first, "显示更多取货地点")
        await run.settle(timeout=15)  # 展开可能要再查一次门店
    if not await card.count():
        raise RuntimeError(f"门店列表里找不到「Apple {STORES[store_no]}」")
    await run.click(card.first, "门店")

    async def picked():  # 选中了，或者选完列表收起来、单选框没了（接口回来后下面再核对一次）
        return await page.evaluate(RADIO_JS, store_no) is not False

    if not await wait_until(picked, 5):
        raise RuntimeError(f"点了「Apple {STORES[store_no]}」但没选中")
    await run.settle()  # 选店会发接口，回来后「继续」才能点；等它回来就走，不再固定干等几秒
    state = await page.evaluate(RADIO_JS, store_no)
    if state is False:
        raise RuntimeError(f"选了「Apple {STORES[store_no]}」，接口回来后又没选中了")
    run.log(f"选择门店：{store_no} {STORES[store_no]}（单选框{'已核对选中' if state else '未找到，靠截图确认'}）")


# 门店单选框：true=选中 false=没选中 null=页面上没有这个单选框
RADIO_JS = """sn => { const e = document.querySelector(`input[type=radio][value="${sn}"]`); return e ? e.checked : null; }"""


STORE_STATES_JS = """() => [...document.querySelectorAll('input[type=radio]')].filter(e => /^R\\d+$/.test(e.value)).map(e => {
  const card = e.closest('li') || e.parentElement;
  return [e.value, e.checked ? '选中' : '', e.disabled ? '不可选' : '', (card ? card.innerText : '').replace(/\\s+/g, ' ').slice(0, 50)];
})"""


def search_done(page, timeout=20000):
    """等「按位置查各店库存」的接口完整返回（实测约 10 秒）"""
    return asyncio.ensure_future(page.wait_for_event(
        "requestfinished", predicate=lambda r: is_search(r.url), timeout=timeout))


ANY_ENABLED_JS = """want => [...document.querySelectorAll('input[type=radio]')].some(e => want.includes(e.value) && !e.disabled)"""


async def read_states(run, want=()):
    """库存查询回来后读门店状态。want 是要抢的店：其中一家变成可选就马上读，不干等；
    1 秒内都没变（多半真没货），或者没给 want（平时站位），就照旧等 1 秒让列表渲染完再读"""
    page = run.page
    if want:
        try:
            await page.wait_for_function(ANY_ENABLED_JS, arg=list(want), polling=50, timeout=1000)
        except Exception:
            pass
    else:
        await page.wait_for_timeout(1000)
    return await page.evaluate(STORE_STATES_JS)


async def ensure_beijing(run, states, force=False, want=()):
    """浏览器走系统代理时，Apple 按出口 IP 猜位置，实测约 1/5 会猜到外地（嘉兴、沈阳、广州），
    列表里就没有北京的店。排第一的不是北京的店，就把位置手动设成「北京 东城区」，并勾选保存位置。
    抢购时要的店（want）已经在列表里能选，就先不管位置，省下再查一遍库存的约 10 秒"""
    if not force and states and (states[0][0] in STORES or any(s in want and not off for s, _, off, _ in states)):
        return states
    page = run.page
    run.log(f"门店列表不在北京（第一家是 {states[0][3] if states else '空'}），把位置设成北京 东城区")
    await run.click(page.locator('[data-autom="fulfillment-pickup-store-search-button"]').first, "位置")
    save = page.get_by_label("保存我的位置信息以便日后使用")
    if await save.count() and not await save.first.is_checked():
        try:
            await save.first.check(timeout=3000)
        except Exception:
            await page.get_by_text("保存我的位置信息以便日后使用").first.click()
        run.log("勾选：保存我的位置信息")
    state_tab = page.locator('[data-autom="address-field-provinceselector-state-tab"]').first
    if await state_tab.get_attribute("aria-selected") != "true":  # 已经停在省份列表就不用点
        await run.click(state_tab, "省份标签")
    await run.click(page.locator("button", has_text=re.compile(r"^\s*北京\s*$")).first, "北京")
    search = search_done(page)
    await run.click(page.locator("button", has_text=re.compile(r"^\s*东城区\s*$")).first, "东城区")
    try:
        await search
    except Exception:
        run.log("选完东城区没等到门店库存查询")
    states = await read_states(run, want)
    loc = await page.locator('[data-autom="fulfillment-pickup-store-search-button"]').first.inner_text()
    run.log(f"位置现在是「{loc.strip()}」，门店列表：{states}")
    return states


async def open_pickup(run, want=(), dump=True):
    """点「我要取货」，等各店库存查询回来，返回门店状态 [[编号, 选中, 不可选, 卡片文字], ...]；want 见 read_states"""
    page = run.page
    # 点完会自动查一遍各店库存（实测约 10 秒），等它回来，列表里的可取货状态才准
    search = search_done(page, timeout=30000)
    # 「我要取货」是 <button>，但 ARIA 角色不是 button，按角色找不到，按标签+文字找。
    # 刚进结账页时页面脚本可能还没接管按钮，press 确认查库存的请求发出去了，没发就再点
    await press(run, page.locator("button", has_text="我要取货").first, "我要取货", is_search,
                shown=page.locator('[data-autom="fulfillment-pickup-store-search-button"]'))
    try:
        await search
    except Exception:
        run.log("没等到门店库存查询，继续")
    states = await read_states(run, want)
    run.log(f"门店列表：{states}")
    states = await ensure_beijing(run, states, want=want)
    if dump:
        run.snap("pickup")
    return states


async def refresh_pickup(run, want=(), dump=True):
    """没货的店是灰的点不了，要让 Apple 重新查一遍各店库存。
    先试刷新页面（实测只触发 1 次查库存，约 10 秒）；不行再切「为我送货」再切回来（3 个排队请求，约 30 秒）"""
    page = run.page
    await run.flush()  # 刚站好位就来信号时，上一张还在后台存，等它存完再刷新（平时早存完了，不耽误）
    search = search_done(page, timeout=15000)
    await page.reload(wait_until="domcontentloaded")
    try:
        await search
        states = await read_states(run, want)
        if states:
            run.log(f"刷新页面后门店列表：{states}")
            states = await ensure_beijing(run, states, want=want)
            if dump:
                run.snap("pickup")
            return states
    except Exception:
        pass
    run.log("刷新页面没触发查库存，改用切换送货/取货")
    await run.click(page.locator("button", has_text="为我送货").first, "为我送货")
    await page.wait_for_timeout(1500)
    return await open_pickup(run, want, dump)


async def do_fulfillment(run, store_no):
    await open_pickup(run, {store_no})
    if run.until == "pickup":
        raise Stop("pickup")
    if run.until == "location":  # 测试「固定北京」：不管当前在哪，都强制走一遍设位置
        await ensure_beijing(run, await run.page.evaluate(STORE_STATES_JS), force=True)
        await run.dump("location")
        raise Stop("location")
    await finish_fulfillment(run, store_no)


async def finish_fulfillment(run, store_no):
    """选门店 →（取货时间）→ 继续填写取货详情"""
    page = run.page
    await select_store(run, store_no)
    run.snap("store")
    # 取货时间：选第一个能选的（实测目前没有这一项）
    sel = page.locator('select[name*="imeSlot"]')
    if await sel.count():
        opts = await sel.first.locator("option").evaluate_all(
            "os => os.filter(o => o.value && !o.disabled).map(o => [o.value, o.text])")
        if opts:
            await sel.first.select_option(opts[0][0])
            run.log(f"取货时间：{opts[0][1]}")
            await run.settle()
    else:
        slot = page.locator('input[type="radio"][name*="imeSlot"]:not([disabled])')
        if await slot.count():
            await slot.first.check(force=True)
            run.log("取货时间：选了第一个")
            await run.settle()
        else:
            run.log("没找到取货时间选项（可能不需要选）")
    await click_continue(run, ("fulfillment-continue-button",))


async def do_contact(run):
    """登录后取货人信息多半已从账户带出：已有的不动，空着的才用 .env 补"""
    page = run.page
    # 等表单渲染出来（继续按钮出现）再检查，免得字段还没出来就判断成空
    await page.locator('[data-autom="continue-button-label"]').first.wait_for(state="visible", timeout=15000)
    vals = {"lastName": "ORDER_LAST_NAME", "firstName": "ORDER_FIRST_NAME",
            "emailAddress": "ORDER_EMAIL", "Phone": "ORDER_PHONE"}
    filled = []
    for key, env in vals.items():
        inp = page.locator(f'input[name*="{key}"]:visible')
        val = os.environ.get(env, "").strip()
        if await inp.count() and not await inp.first.input_value() and val:
            await inp.first.fill(val)
            filled.append(key)
    run.log(f"用 .env 补填：{filled or '无（页面上已有）'}")
    required = page.locator("input[required]:visible")
    blanks = "es => es.filter(e => !e.value).map(e => (e.labels && e.labels[0] ? e.labels[0].innerText : e.name).trim())"
    empty = await required.evaluate_all(blanks)
    if empty:  # 账户里的信息可能还在往表单里填，稍等再判断，免得误报

        async def all_filled():
            return not await required.evaluate_all(blanks)

        await wait_until(all_filled, 2)
        empty = await required.evaluate_all(blanks)
    run.snap("contact")
    if empty:
        raise RuntimeError(f"还有必填项空着：{empty}，请在 .env 里补上对应信息")
    await click_continue(run)


PAYMENT_CODES = {"支付宝": "ALIPAY", "微信": "WECHAT", "微信支付": "WECHAT", "信用卡": "CREDIT"}


async def do_billing(run):
    page = run.page
    want = os.environ.get("ORDER_PAYMENT", "").strip()
    if not want and run.dry:
        want = "支付宝"
        run.log("没设 ORDER_PAYMENT，测试先用支付宝走到最后一步（测试不会付款）")
    code = PAYMENT_CODES.get(want, want.upper())
    await page.locator('input[name$="selectBillingOption"]').first.wait_for(state="attached", timeout=15000)
    opt = page.locator(f'input[name$="selectBillingOption"][value="{code}"]')
    if not await opt.count():
        have = await page.locator('input[name$="selectBillingOption"]').evaluate_all("es => es.map(e => e.value)")
        raise RuntimeError(f"付款方式里没有「{want}」，可选：{have}")
    # 单选框本身是隐藏的，直接点不生效（实测），要点它的 label
    oid = await opt.first.get_attribute("id")
    label = page.locator(f'label[for="{oid}"]').or_(page.locator(f'[id="{oid}_label"]'))
    await run.click(label.first, f"付款方式 {want}")
    if not await wait_until(opt.first.is_checked, 5):
        raise RuntimeError(f"点了「{want}」但没选中")
    await run.settle()  # 选付款方式也会发接口，等它回来再继续
    if not await opt.first.is_checked():
        raise RuntimeError(f"选了「{want}」，接口回来后又没选中了")
    run.log(f"选择付款方式：{want}（{code}，已核对选中）")
    run.snap("billing")
    await click_continue(run)
    return True


async def checkout_steps(run, store_no):
    """从当前结账步骤一路往下走。返回 review（测试停在立即下单前）/ placed（已下单）/ failed / None"""
    page = run.page
    seen = []
    for _ in range(10):
        # 进新一步时等页面上的接口请求回来就走，不固定干等；各步自己还会等要点的元素出现
        await page.wait_for_load_state("domcontentloaded")
        await run.settle()
        step = step_of(page.url) or "unknown"
        run.mark(STEP_NAMES.get(step, step))
        run.snap(step)
        if seen.count(step) >= 2:
            raise RuntimeError(f"卡在「{step}」这一步没往下走")
        seen.append(step)
        before, t = page.url, time.time()
        if step.startswith("Fulfillment"):
            await do_fulfillment(run, store_no)
        elif "Contact" in step:
            await do_contact(run)
        elif step.startswith("Billing"):
            if not await do_billing(run):
                return
        elif step.startswith("Review"):
            place = page.locator('[data-autom="continue-button-placeOrder"]')
            if run.dry:
                run.log(f"已到最后一步，提交按钮：{await place.all_inner_texts()}。测试模式到此为止，没有提交订单")
                return "review"
            await run.click(place.first, "立即下单")
            run.mark("点了立即下单")
            try:  # 下单成功会离开 Review（去付款/订单确认页）；还停在 Review 多半是没货了或报错
                await page.wait_for_url(lambda u: "_s=Review" not in u, timeout=120_000)
            except Exception:
                await run.dump("place_failed")
                return "failed"
            run.mark("已下单")
            await page.wait_for_timeout(3000)
            await run.dump("placed")
            return "placed"
        else:
            run.log(f"没见过的步骤「{step}」，先停下")
            return
        if not await run.moved_on(before, t, 45):  # 被拒了就马上回到上面再看一眼这一步，第二次还不行就报卡住
            run.log(f"「{step}」点了继续，接口都回来了还没往下走")


def watch_checkout_api(run):
    """把结账相关接口的耗时记进日志，看时间花在 Apple 服务器还是脚本自己；
    同时记下页面上还没回来的接口请求，给 run.settle() 用"""
    starts = {}

    def begin(r):
        if is_api(r):
            run.inflight[r] = run.api_at = time.time()
            run.sent.append((run.api_at, r.url))
        if "/shop/checkout" in r.url or "/shop/bagx" in r.url:
            starts[r] = time.time()

    def end(r):
        if run.inflight.pop(r, None) is not None:
            run.api_at = time.time()
        if r in starts:
            path = r.url.split("apple.com.cn", 1)[-1]
            run.log(f"  接口 {r.method} {path[:100]} 用时 {time.time() - starts.pop(r):.1f}s")

    run.page.on("request", begin)
    run.page.on("requestfinished", end)
    run.page.on("requestfailed", end)


async def cmd_dryrun(part, store_no, until):
    load_env()
    async with async_playwright() as pw:
        ctx = await open_profile(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        run = Run(page, ctx, dry=True, until=until)
        watch_checkout_api(run)
        run.log(f"测试模式：{part} @ {store_no} {STORES.get(store_no, '')}，走到 {until} 为止（不会提交订单）")
        run.t_hit = time.time()
        try:
            await prepare_bag(run, part)
            run.mark("购物袋")
            await enter_checkout(run)
            await checkout_steps(run, store_no)
        except Stop as s:
            run.log(f"按 --until {s} 停下")
        except Exception:
            run.log("出错：\n" + traceback.format_exc())
            await run.dump("error")
        await run.flush()
        run.log(f"各步耗时：{run.timeline()}")
        run.log(f"结束，记录在 {run.dir}")
        try:
            await asyncio.get_running_loop().run_in_executor(None, input, "看完按回车关闭浏览器…")
        except EOFError:
            pass
        await ctx.close()


def pname(part):
    return PART_NAMES.get(part, part)


async def position(run, part, want=()):
    """站位：购物袋里只放这一个型号，进结账，停在取货门店列表。返回门店状态；want 见 read_states"""
    await prepare_bag(run, part)
    run.bag_part = part
    await enter_checkout(run)
    try:  # 等跳到取货这一步就走（中间可能还转一下），不再固定等 1.5 秒；点「我要取货」时 press 会确认点上了
        await run.page.wait_for_url(lambda u: step_of(u) == "Fulfillment", wait_until="domcontentloaded", timeout=20000)
    except Exception:
        raise RuntimeError(f"结账页不是取货那一步：{run.page.url}") from None
    states = await open_pickup(run, want)
    run.log(f"已站位（购物袋里是 {pname(part)}）：停在取货门店列表，等监控报有货")
    return states


async def attempt(run, stores, hits):
    """收到有货 hits=[(门店, 型号), ...]：先抢购物袋里现成的型号（只需刷新，约 10 秒），
    不是的话换型号重新站位（约 25 秒）；某个型号刷新后没了就试下一个。返回 (结果, 型号)，
    结果是 placed / review / failed / gone。各步耗时记在 run.marks"""
    page = run.page
    run.t_hit, run.marks, run.pick = time.time(), [], None
    parts = list(dict.fromkeys(p for _, p in hits))
    parts.sort(key=lambda p: p != run.bag_part)  # 购物袋里现成的排最前
    for part in parts:
        if part == run.bag_part and step_of(page.url) == "Fulfillment":
            states = await refresh_pickup(run, stores)
        else:  # 换型号，或者结账会话过期了（跳到 session_expired 页）
            run.log(f"换成 {pname(part)} 重新站位")
            run.mark(f"换成{pname(part)}")
            states = await position(run, part, stores)
        run.mark("门店列表")
        enabled = [s for s, _, disabled, _ in states if not disabled and s in stores]
        pick = next((s for s, p in hits if p == part and s in enabled), enabled[0] if enabled else None)
        if not pick:
            run.log(f"{pname(part)} 刷新后这几家店都没货了，慢了一步")
            continue
        run.pick = pick
        run.log(f"抢 {pname(part)} @ {pick} {STORES.get(pick, '')}（开抢后 {time.time() - run.t_hit:.1f}s 门店列表就绪）")
        before, t = page.url, time.time()
        await finish_fulfillment(run, pick)
        run.mark("选好店")
        if not await run.moved_on(before, t, 60):  # 这一步 Apple 要十几秒
            run.log("选好店点了继续，接口回来了还停在取货这一步：多半是这家店刚被别人抢走，或者页面报错了")
            run.snap("fulfillment_stuck")
            return "failed", part
        return (await checkout_steps(run, pick) or "failed"), part
    return "gone", None


def read_trigger():
    try:
        return json.loads(TRIGGER.read_text())
    except Exception:
        return None


def record_attempt(**kw):
    """每次抢购的结果和各步耗时追加到 attempts.jsonl，stats.py 汇总"""
    with ATTEMPTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **kw}, ensure_ascii=False) + "\n")


def most_restocked(cands, days=7):
    """events.jsonl 里最近 days 天 cands 中补货次数最多的型号，返回 (型号, 次数, 总次数)；没记录返回 None"""
    if not EVENTS.exists():
        return None
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    n = collections.Counter()
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "in_stock" and e.get("ts", "") >= since and e.get("part") in cands:
            n[e["part"]] += 1
    if not n:
        return None
    part, cnt = n.most_common(1)[0]
    return part, cnt, sum(n.values())


async def cmd_serve(parts, prefer, stores, live, selftest):
    """parts=None 表示 iPhone Duo 全部规格都要；prefer 是站位时购物袋里放的型号"""
    load_env()
    from monitor import notify  # 复用监控的 Mac 通知 + 微信推送
    if LOCK.exists():
        print(f"已经下过单（{LOCK.read_text().strip()}），不再运行。确实还要再抢就删掉 {LOCK}")
        return
    if live and not os.environ.get("ORDER_PAYMENT", "").strip():
        print("正式模式要先在 .env 里设好 ORDER_PAYMENT")
        return
    alerted = {"err": False}

    def alert_once(title, body):
        if not alerted["err"]:
            notify(title, body)
            alerted["err"] = True

    async with async_playwright() as pw:
        ctx = await open_profile(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        run = Run(page, ctx, dry=not live)
        run.alert = alert_once
        watch_checkout_api(run)
        want = "iPhone Duo 全部规格" if parts is None else "、".join(pname(p) for p in parts)
        run.log(f"常驻{'【正式，会真下单】' if live else '【演练，停在立即下单前】'}：{want}，"
                f"站位放 {pname(prefer)}，门店 {stores}")

        async def grab(hits):
            """抢一次，返回 (结果, 型号, 出错信息)；出错不往外抛，结果记成 error"""
            try:
                result, part = await attempt(run, stores, hits)
                return result, part, None
            except Exception:
                run.log("出错：\n" + traceback.format_exc())
                await run.dump("error")
                return "error", None, traceback.format_exc().splitlines()[-1][:150]

        def report(hits, age, result, part, err, again, retrying):
            """抢完不管成没成都推送一条：结果、各步耗时、卡在哪，同时记进 attempts.jsonl"""
            line = run.timeline()
            run.log(f"结果：{result}（{line}）" + (f"，{err}" if err else ""))
            record_attempt(live=live, signal_age=round(age, 1), hits=hits, retry=retrying, result=result,
                           part=part, store=run.pick, marks=run.marks, error=err)
            where = STORES.get(run.pick, "")
            if result == "placed":
                notify("🎉 已下单！马上去付款",
                       f"iPhone Duo {pname(part)} @ {where} 已提交订单，请尽快在窗口或 Apple Store App 订单里用"
                       f"{os.environ.get('ORDER_PAYMENT')}付款（{line}）", page.url, urgent=True)
            elif result == "review":
                notify("演练：已走到立即下单前", f"{pname(part)} @ {where}：{line}（演练没有下单）")
            elif result == "gone":
                what = "、".join(f"{STORES.get(s, s)}·{pname(p)}" for s, p in hits)
                notify("😢 慢了一步", f"{what}：信号是 {age:.0f}s 前的，刷新门店列表时已经没货了（{line}）")
            else:
                stuck = run.marks[-1][0] if run.marks else "开头"
                notify("⚠️ 没抢成", f"{pname(part or hits[0][1])}：卡在「{stuck}」{'，' + err if err else ''}（{line}）"
                       + ("，信号还新鲜，马上再试一次" if again else ""))

        last_ts, positioned, next_refresh, placed = time.time(), False, 0.0, False
        pending, is_selftest = None, bool(selftest)
        while True:
            try:
                if not positioned:
                    await position(run, prefer)
                    positioned, next_refresh, alerted["err"] = True, time.time() + REPOSITION_EVERY, False
                    if selftest:  # 自测：站好位后假装监控报了 selftest 这家店有货
                        pending, selftest = {"ts": time.time(), "avail": [[selftest, prefer]]}, None
                trig, pending = pending or read_trigger(), None
                if trig and trig.get("ts", 0) > last_ts:
                    last_ts = trig["ts"]
                    hits = [(s, p) for s, p in trig.get("avail", [])
                            if s in stores and (p in parts if parts else p in PART_NAMES)]
                    if hits and time.time() - last_ts < 120:
                        age, retrying = time.time() - last_ts, bool(trig.get("retry"))
                        run.log(f"监控报有货：{[(s, pname(p)) for s, p in hits]}（信号是 {age:.1f}s 前的），"
                                + ("上次没抢成，再试一次" if retrying else "开抢"))
                        result, part, err = await grab(hits)
                        if result == "placed":  # 先写锁再通知，保证只下一单
                            LOCK.write_text(f"{datetime.now():%F %T} {part} {page.url}\n")
                        # 没抢成（不算「慢了一步」，那是真没货了）而信号还新鲜，就马上再试一次，只重试一次
                        again = result in ("failed", "error") and not retrying and time.time() - last_ts < 90
                        report(hits, age, result, part, err, again, retrying)
                        if result == "placed":
                            placed = True
                            break
                        if again:  # 不用先回首选型号站位，attempt 自己会按要抢的型号刷新或重新站位
                            pending, last_ts = dict(trig, retry=True), last_ts - 0.001
                            continue
                        if is_selftest:
                            break
                        # 购物袋换成了别的型号、离开了取货页、或者出过错，都回到首选型号重新站位
                        positioned = (result not in ("failed", "error") and step_of(page.url) == "Fulfillment"
                                      and run.bag_part == prefer)
                        continue
                if time.time() > next_refresh:  # 赶在结账会话过期前重新站位
                    positioned = False
                    continue
            except Exception:
                run.log("出错：\n" + traceback.format_exc())
                await run.dump("error")
                alert_once("⚠️ 自动下单脚本出错，正在重试", traceback.format_exc().splitlines()[-1][:120])
                positioned = False
                await asyncio.sleep(10)
            await asyncio.sleep(0.1)  # 只是读本地的 trigger.json，勤看几次不碰 Apple
        if placed:
            run.log("已下单，窗口保持打开。付完款后按 Ctrl+C 结束")
            await asyncio.Event().wait()
        await run.flush()
        await ctx.close()


async def cmd_login():
    async with async_playwright() as pw:
        ctx = await open_profile(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{BASE}/shop/bag", wait_until="domcontentloaded")
        print("请在弹出的窗口里点「登录」，登录你的 Apple 账户（勾选保持登录）；登录好后直接关掉窗口。", flush=True)
        await ctx.wait_for_event("close", timeout=0)
    print("窗口已关闭，登录态已保存。", flush=True)


async def cmd_check():
    async with async_playwright() as pw:
        ctx = await open_profile(pw, headless=True)
        print("cookies:", sorted({c["name"] for c in await ctx.cookies()}), flush=True)
        await ctx.close()


def main():
    ap = argparse.ArgumentParser(description="Apple Store 门店取货自动下单")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dryrun", help="测试：走完整流程，停在提交订单前")
    d.add_argument("part", help="型号编号，如 MDGD4PA/A（测试用连接线）或 MK2T4CH/A")
    d.add_argument("store", choices=sorted(STORES), help="门店编号，如 R448（王府井）")
    d.add_argument("--until", choices=["bag", "pickup", "location", "review"], default="review",
                   help="走到哪一步停：bag=加完购物袋，pickup=打开取货门店列表，location=再强制把位置设成北京，review=检查订单页（默认）")
    s = sub.add_parser("serve", help="常驻：站好位等监控报有货，有货就抢（不加 --live 只演练）")
    s.add_argument("--parts", default="ALL", help="接受的型号，逗号分隔；默认 ALL = iPhone Duo 全部 8 个规格，哪个有货抢哪个")
    s.add_argument("--prefer", help="站位时购物袋里放的型号；默认按 events.jsonl 挑最近 7 天补货最多的（只在 --parts 里挑）；"
                                    f"没记录时用 --parts 的第一个，没给 --parts 用 {DEFAULT_PREFER}（{PART_NAMES[DEFAULT_PREFER]}）")
    s.add_argument("--stores", default=",".join(STORES), help="接受的门店编号，逗号分隔，默认北京 6 家")
    s.add_argument("--live", action="store_true", help="正式模式：真的点「立即下单」")
    s.add_argument("--selftest", choices=sorted(STORES), help="演练自测：站好位后假装这家店有货，走一遍就退出")
    sub.add_parser("login")
    sub.add_parser("check")
    args = ap.parse_args()
    if args.cmd == "dryrun":
        asyncio.run(cmd_dryrun(args.part, args.store, args.until))
    elif args.cmd == "serve":
        if args.live and args.selftest:
            ap.error("--selftest 只能在演练模式用")
        stores = [x.strip() for x in args.stores.split(",") if x.strip()]
        if bad := [x for x in stores if x not in STORES]:
            ap.error(f"不认识的门店编号：{bad}")
        parts = None if args.parts.upper() == "ALL" else [x.strip().upper() for x in args.parts.split(",") if x.strip()]
        cands = parts or list(PART_NAMES)
        if args.prefer:
            prefer = args.prefer.upper()
        elif best := most_restocked(cands):  # 有货的型号正好在购物袋里只要刷新，换型号要多花十几秒
            prefer = best[0]
            print(f"站位放 {pname(prefer)}：最近 7 天补货 {best[1]}/{best[2]} 次，最多（按 events.jsonl 挑的，可用 --prefer 指定）")
        else:
            prefer = parts[0] if parts else DEFAULT_PREFER
        if parts and prefer not in parts:
            ap.error("--prefer 必须是 --parts 里的一个")
        asyncio.run(cmd_serve(parts, prefer, stores, args.live, args.selftest))
    else:
        asyncio.run({"login": cmd_login, "check": cmd_check}[args.cmd]())


if __name__ == "__main__":
    main()
