#!/usr/bin/env python3
"""补货规律和抢购记录：python3 stats.py [--days 7]

读同目录 monitor.py 记的 events.jsonl（有货 / 售罄）和 autoorder.py 记的 attempts.jsonl（每次抢购的结果和各步耗时），
回答几个问题：哪个型号、哪家店、几点补货多；货一般能挂多久；自动下单没抢到，是慢了还是卡在哪一步。
"""
import argparse
import collections
import json
import statistics
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ONE_ROUND = 75   # 监控约 54 秒查一次（±15% 抖动），有货挂不到这么久 = 下一次查就没了
RESULT_NAMES = {"placed": "已下单", "review": "演练走通", "gone": "慢了一步", "failed": "卡住", "error": "出错"}


def load(name, since):
    f = HERE / name
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            e["t"] = datetime.fromisoformat(e["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if e["t"] >= since:
            out.append(e)
    return sorted(out, key=lambda e: e["t"])


def width(s):
    """终端里的显示宽度（中文占两格）"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def table(rows, total):
    if not rows:
        return
    w = max(width(label) for label, _ in rows)
    top = max(n for _, n in rows) or 1
    for label, n in rows:
        print(f"  {label}{' ' * (w - width(label))}  {n:4d}  {n * 100 / total:3.0f}%  {'█' * round(n * 24 / top)}")


def durations(events):
    """每次有货挂了多久（秒）：同一家店同一型号，从 in_stock 到之后第一次 sold_out"""
    start, out = {}, []
    for e in events:
        k = (e.get("store_no"), e.get("part"))
        if e.get("event") == "in_stock":
            start.setdefault(k, e["t"])
        elif e.get("event") == "sold_out" and k in start:
            d = (e["t"] - start.pop(k)).total_seconds()
            if d < 6 * 3600:  # 再长多半是中间监控停过，不算
                out.append(d)
    return out


def mark(a, what):
    return dict(map(tuple, a.get("marks") or [])).get(what)


def main():
    ap = argparse.ArgumentParser(description="补货规律和抢购记录")
    ap.add_argument("--days", type=float, default=7, help="看最近几天，默认 7")
    args = ap.parse_args()
    since = datetime.now() - timedelta(days=args.days)
    events, atts = load("events.jsonl", since), load("attempts.jsonl", since)
    ins = [e for e in events if e.get("event") == "in_stock"]
    names = {e["part"]: e.get("title") or e["part"] for e in ins if e.get("part")}
    tips = []

    span = f"（{ins[0]['t']:%m-%d %H:%M} ~ {ins[-1]['t']:%m-%d %H:%M}）" if ins else ""
    print(f"最近 {args.days:g} 天补货（监控看到有货）{len(ins)} 次{span}")
    if ins:
        by_part = collections.Counter(e["part"] for e in ins)
        print("\n按型号")
        table([(names[p], n) for p, n in by_part.most_common()], len(ins))
        print("\n按门店")
        table(collections.Counter(e.get("store") or e.get("store_no") or "?" for e in ins).most_common(), len(ins))
        hours = collections.Counter(e["t"].hour for e in ins)
        print("\n按几点")
        table([(f"{h:02d} 点", hours[h]) for h in range(min(hours), max(hours) + 1)], len(ins))
        mins = collections.Counter(e["t"].minute // 5 for e in ins)
        print("\n按每小时的第几分钟（5 分钟一格；如果某几格明显多，说明补货有固定的时间点）")
        table([(f"{m * 5:02d}-{m * 5 + 4:02d} 分", mins[m]) for m in range(12)], len(ins))

        part, n = by_part.most_common(1)[0]
        tips.append(f"站位放「{names[part]}」：补货最多，占 {n * 100 / len(ins):.0f}%（autoorder.py serve 默认就按这个挑）")
        m, k = mins.most_common(1)[0]
        if len(ins) >= 24 and k >= 2.5 * len(ins) / 12:
            tips.append(f"补货明显集中在每小时的 {m * 5:02d}-{m * 5 + 4:02d} 分（{k} 次，平均一格 {len(ins) / 12:.0f} 次），"
                        "把这份输出发我，可以让监控把查询集中到这几分钟（每小时总次数不变）")

    ds = durations(events)
    if ds:
        print("\n货挂了多久（同一家店同一型号，从监控看到有货到看到没货；监控约 1 分钟查一次）")
        buckets = [("下一次查就没了（约 1 分钟内）", -1, ONE_ROUND), ("1~2.5 分钟", ONE_ROUND, 150),
                   ("2.5~5 分钟", 150, 300), ("5~15 分钟", 300, 900), ("15 分钟以上", 900, 1e9)]
        table([(label, sum(lo < d <= hi for d in ds)) for label, lo, hi in buckets], len(ds))
        quick = sum(d <= ONE_ROUND for d in ds) / len(ds)
        print(f"  中位数 {statistics.median(ds) / 60:.1f} 分钟")
        if quick >= 0.5:
            tips.append(f"{quick:.0%} 的货下一次查询前就卖完了：监控受 Apple 每 IP 每小时约 75 次的限制，约 1 分钟才查一次，"
                        "这部分货发现时多半已经没了，能抢到的主要是挂得久的那些")

    if atts:
        print(f"\n自动下单（attempts.jsonl）{len(atts)} 次")
        by = collections.Counter(a.get("result") for a in atts)
        for r, n in by.most_common():
            sub = [a for a in atts if a.get("result") == r]
            extra = ""
            if r == "gone":
                ages = [a["signal_age"] for a in sub if a.get("signal_age") is not None]
                lists = [x for a in sub if (x := mark(a, "门店列表")) is not None]
                if ages and lists:
                    extra = (f"：收到信号时中位已过 {statistics.median(ages):.0f}s，"
                             f"开抢后门店列表刷新出来中位 {statistics.median(lists):.1f}s")
            elif r in ("failed", "error"):
                stuck = collections.Counter((a["marks"][-1][0] if a.get("marks") else "开头") for a in sub)
                extra = "：卡在" + "、".join(f"「{k}」×{v}" for k, v in stuck.most_common())
                errs = collections.Counter(a["error"] for a in sub if a.get("error"))
                if errs:
                    extra += "；报错 " + "、".join(f"{k}×{v}" for k, v in errs.most_common(3))
            elif r in ("review", "placed"):
                ts = [x for a in sub if (x := mark(a, "检查订单")) is not None]
                if ts:
                    extra = f"：开抢到「立即下单」前中位 {statistics.median(ts):.1f}s"
            print(f"  {RESULT_NAMES.get(r, r)} {n} 次{extra}")
        bad = by["failed"] + by["error"]
        if bad:
            tips.append(f"有 {bad} 次卡住/出错：把 runs/ 里对应时间那次的 run.log 发我，看卡在哪")
        if by["gone"] > len(atts) / 2:
            tips.append("多数是「慢了一步」：流程本身走通了，差在发现得晚 + Apple 刷新门店列表要 10 秒左右")

    if not ins and not atts:
        print("还没有记录：monitor.py 跑一阵会写 events.jsonl，autoorder.py serve 抢过会写 attempts.jsonl")
    if tips:
        print("\n建议")
        for t in tips:
            print(f"  · {t}")


if __name__ == "__main__":
    main()
