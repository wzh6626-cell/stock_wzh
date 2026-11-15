#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""把过去的预判和后来的收盘价对上，算出准确率，并给出下一步怎么改方案。"""

import instock.core.forecast_plan as forecast_plan

MISS_TEXT = {
    "上升喊早了": "判成上升，但 5 个交易日之后收盘更低",
    "下降喊早了": "判成下降，但 5 个交易日之后收盘更高",
    "震荡没看住": "判成震荡，但这 5 天涨跌幅超过 2%",
    "方向喊重了": "判出了涨或跌，但这 5 天价格几乎没动",
    "一周区间太窄": "方向判断对了，但 5 日后收盘跑出了预判区间",
}


def review(codes, asof, scheme):
    scheme = forecast_plan.normalize_scheme(scheme)
    scored = _score(codes, asof, scheme)
    scored["scheme"] = scheme
    scored["scheme_text"] = scheme_text(scheme)
    scored["low"] = bool(scored["ready"] and scored["rate"] < 70)
    return scored


def propose(review_result):
    """按最多的一种偏差，只改方案里的一项。"""
    scheme = forecast_plan.normalize_scheme(review_result.get("scheme"))
    misses = review_result.get("misses") or {}
    if not misses:
        return None
    top = max(misses.items(), key=lambda item: item[1])[0]
    nxt = dict(scheme)
    if top == "一周区间太窄" and scheme["band_mult"] < 3.9:
        nxt["band_mult"] = round(scheme["band_mult"] + 0.25, 4)
        change = f"一周价格区间从每日波动的 {scheme['band_mult']:.2f} 倍放宽到 {nxt['band_mult']:.2f} 倍"
        why = "偏差主要是收盘价跑出区间，先把区间放宽一档"
    elif top in ("上升喊早了", "下降喊早了", "方向喊重了") and scheme["trend_gap"] < 0.03:
        nxt["trend_gap"] = round(scheme["trend_gap"] + 0.005, 4)
        change = (
            f"认定趋势所需的偏离，从现价离开 20 日均线 {scheme['trend_gap'] * 100:.1f}%"
            f"提高到 {nxt['trend_gap'] * 100:.1f}%"
        )
        why = "偏差主要是趋势喊早了，现价要离 20 日均线更远才认定上升或下降"
    elif top == "震荡没看住" and scheme["trend_gap"] > 0:
        nxt["trend_gap"] = round(max(0, scheme["trend_gap"] - 0.005), 4)
        change = (
            f"认定趋势所需的偏离，从现价离开 20 日均线 {scheme['trend_gap'] * 100:.1f}%"
            f"降到 {nxt['trend_gap'] * 100:.1f}%"
        )
        why = "很多日子被判成震荡，后面却走出了单边，把趋势门槛放宽一点"
    elif top == "震荡没看住" and scheme["ma_align"] == 1:
        nxt["ma_align"] = 0
        change = "上升和下降不再额外要求 20 日均线站在 60 日均线同一侧"
        why = "很多日子被判成震荡，后面却走出了单边。改成只看现价和 20 日均线的位置"
    else:
        return None
    return {"scheme": nxt, "change": change, "why": why, "top": top}


def scheme_text(scheme):
    scheme = forecast_plan.normalize_scheme(scheme)
    align = "20 日均线还要和 60 日均线同向" if scheme["ma_align"] else "只看现价是否离开 20 日均线"
    gap = scheme["trend_gap"] * 100
    if gap <= 0:
        gap_text = "现价一过 20 日均线就按趋势看"
    else:
        gap_text = f"现价要比 20 日均线至少偏离 {gap:.1f}% 才按趋势看"
    return f"一周区间是每日波动的 {scheme['band_mult']:.2f} 倍；{gap_text}；{align}。"


def _score(codes, asof, scheme):
    hits = 0
    total = 0
    misses = {}
    skipped = 0
    used = 0
    for code in codes:
        data = forecast_plan.load_cached_hist(code, asof) if asof else None
        indexes = _sample_indexes(0 if data is None else len(data.index))
        if data is None or not indexes:
            skipped += 1
            continue
        used += 1
        close = data["close"].astype(float)
        for index in indexes:
            state = forecast_plan.market_state(data.iloc[: index + 1], scheme)
            if state is None:
                continue
            future = float(close.iloc[index + 5])
            tag = _judge(state, future)
            total += 1
            if tag is None:
                hits += 1
            else:
                misses[tag] = misses.get(tag, 0) + 1
    if total <= 0:
        return {
            "ready": False,
            "low": False,
            "hits": 0,
            "total": 0,
            "rate": 0,
            "rate_text": "暂无",
            "summary": "还没有足够的日 K 可以回头核对。一只股票至少要有 65 根日 K，才能拿出 5 天后的收盘价来对。",
            "reasons": [],
            "misses": {},
            "used": used,
            "skipped": skipped,
        }
    rate = hits * 100.0 / total
    reasons = []
    for tag, count in sorted(misses.items(), key=lambda item: (-item[1], item[0])):
        reasons.append(f"{MISS_TEXT[tag]}，有 {count} 次。")
    summary = (
        f"用自选里 {used} 只股票最近约 60 个交易日的日 K，每隔 5 个交易日取一次当时的预判，"
        f"再和 5 个交易日之后的收盘价对比，一共 {total} 次，对了 {hits} 次。"
        f"一次算对，要同时满足两件：收盘方向和当时的趋势一致；收盘价还在当时给出的一周区间里。"
        f"震荡看这 5 天涨跌幅有没有超过 2%。"
    )
    if skipped:
        summary += f" 另有 {skipped} 只没有足够日 K，没有算进去。"
    if total < 8:
        summary += " 样本不到 8 次，这个百分比还不稳定。"
    return {
        "ready": True,
        "hits": hits,
        "total": total,
        "rate": rate,
        "rate_text": f"{round(rate)}%（{total} 次里对了 {hits} 次）",
        "summary": summary,
        "reasons": reasons,
        "misses": misses,
        "used": used,
        "skipped": skipped,
    }


def _sample_indexes(length):
    last_index = length - 1 - 5
    if last_index < 59:
        return []
    first_index = max(59, last_index - 55)
    return list(range(first_index, last_index + 1, 5))


def _judge(state, future):
    last = state["last"]
    trend = state["trend"]
    move = (future - last) / last if last else 0
    if trend == "上升":
        direction_ok = future > last
    elif trend == "下降":
        direction_ok = future < last
    else:
        direction_ok = abs(move) <= 0.02
    band_ok = state["lower"] <= future <= state["upper"]
    if direction_ok and band_ok:
        return None
    if trend == "上升" and future < last:
        return "上升喊早了"
    if trend == "下降" and future > last:
        return "下降喊早了"
    if trend == "震荡" and abs(move) > 0.02:
        return "震荡没看住"
    if not direction_ok:
        return "方向喊重了"
    return "一周区间太窄"
