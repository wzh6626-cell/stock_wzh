#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import math
import os

import numpy as np
import pandas as pd
import talib as tl

import instock.core.stockfetch as stf
import instock.lib.trade_time as trd


def load_cached_hist(code, asof_date):
    """只读本地日 K 缓存，不访问行情接口。"""
    date_start, _ = trd.get_trade_hist_interval(asof_date)
    cache_file = os.path.join(
        stf.stock_hist_cache_path, date_start[0:6], date_start, f"{code}qfq.gzip.pickle"
    )
    if not os.path.isfile(cache_file):
        return None
    data = pd.read_pickle(cache_file, compression="gzip")
    if data is None or len(data.index) == 0:
        return None
    data = data.copy()
    data["date"] = data["date"].map(_date_text)
    asof = str(asof_date)[:10]
    data = data.loc[data["date"] <= asof]
    if len(data.index) < 60:
        return None
    return data


def default_scheme():
    return {"band_mult": round(math.sqrt(5), 4), "trend_gap": 0.0, "ma_align": 1}


def normalize_scheme(scheme):
    base = default_scheme()
    if not scheme:
        return base
    try:
        base["band_mult"] = float(scheme.get("band_mult", base["band_mult"]))
        base["trend_gap"] = float(scheme.get("trend_gap", base["trend_gap"]))
        base["ma_align"] = 1 if int(scheme.get("ma_align", 1)) else 0
    except (TypeError, ValueError):
        return default_scheme()
    if not math.isfinite(base["band_mult"]) or base["band_mult"] <= 0:
        base["band_mult"] = default_scheme()["band_mult"]
    if not math.isfinite(base["trend_gap"]):
        base["trend_gap"] = 0.0
    return base


def market_state(data, scheme=None):
    """用截至最后一根 K 的数据判断趋势和一周价格区间。"""
    scheme = normalize_scheme(scheme)
    close = data["close"].astype(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    if len(close.index) < 60:
        return None
    atr_series = tl.ATR(high.values, low.values, close.values, timeperiod=14)
    rsi_series = tl.RSI(close.values, timeperiod=14)
    ma10_series = tl.MA(close.values, timeperiod=10)
    ma20_series = tl.MA(close.values, timeperiod=20)
    ma60_series = tl.MA(close.values, timeperiod=60)
    last = float(close.iloc[-1])
    atr = float(atr_series[-1])
    rsi = float(rsi_series[-1])
    ma10 = float(ma10_series[-1])
    ma20 = float(ma20_series[-1])
    ma60 = float(ma60_series[-1])
    if not all(np.isfinite(v) for v in (last, atr, rsi, ma10, ma20, ma60)) or atr <= 0:
        return None
    band = atr * scheme["band_mult"]
    up_line = ma20 * (1 + scheme["trend_gap"])
    down_line = ma20 * (1 - scheme["trend_gap"])
    if scheme["ma_align"]:
        up = last > up_line and ma20 >= ma60
        down = last < down_line and ma20 <= ma60
    else:
        up = last > up_line
        down = last < down_line
    if up:
        trend = "上升"
    elif down:
        trend = "下降"
    else:
        trend = "震荡"
    return {
        "trend": trend,
        "last": last,
        "atr": atr,
        "rsi": rsi,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "high20": float(high.tail(20).max()),
        "low20": float(low.tail(20).min()),
        "upper": _price(last + band),
        "lower": _price(max(last - band, 0.01)),
    }


def build_plan(data, holding_shares, cost, capital, risk_pct, scheme=None):
    state = market_state(data, scheme)
    if state is None:
        return None
    last = state["last"]
    atr = state["atr"]
    rsi = state["rsi"]
    ma10 = state["ma10"]
    ma20 = state["ma20"]
    ma60 = state["ma60"]
    high20 = state["high20"]
    low20 = state["low20"]
    upper = state["upper"]
    lower = state["lower"]
    trend = state["trend"]

    extended = rsi >= 75 or last > ma10 * 1.03
    hot = rsi >= 80 or last >= high20 * 0.995
    stop_distance = max(2 * atr, last * 0.03)
    buy_shares = _position_shares(capital, risk_pct, last, stop_distance)
    half_budget = _lot(buy_shares / 2)

    holding = _lot(holding_shares)
    gap_text = _gap_text(atr, last, stop_distance)
    hot_why = _hot_why(rsi, last, high20, hot)

    if trend == "下降":
        entry_price = _price(ma20)
        entry_shares = buy_shares
        entry_amount = _money(entry_price, entry_shares)
        fallback = _price(max(last - stop_distance, 0.01))
        entry_parts = _down_entry(last, ma20, ma60, entry_price, entry_shares, entry_amount)
        if holding > 0:
            exit_parts = [
                (
                    f"趋势已经向下，建议按现价 {_t(last)} 附近把持有的 {holding} 股全部卖掉，不要等反弹。"
                    f"{_pnl_text(holding, cost, last)}"
                ),
                (
                    f"继续拿着的话，价格更容易往 {lower} 这一侧靠。"
                    f"已经有的利润可能吐回去，亏损也可能变大。"
                ),
            ]
            exit_when = "".join(exit_parts)
            exit_shares = holding
            stop_when = (
                f"上面的建议是现在就卖。如果今天没成交，或者你还想再确认一天，最后离场价放在 {_t(fallback)}。"
                f"它比现价低 {_t(stop_distance)} 元。{gap_text}"
                f"价格跌到 {_t(fallback)}，把 {holding} 股全部卖掉，不再等。"
            )
            stop_shares = holding
        else:
            exit_when = "当前没有填写持仓，没有股票要卖。上面的买入条件满足、并且真的买进之后，再按买进当天的目标价和止损价处理。"
            exit_shares = 0
            stop_when = (
                f"现在没有持仓，不用设止损卖出。"
                f"以后如果按上面的条件买进，买入价往下 {_price(stop_distance)} 元就是必须卖掉的位置。{gap_text}"
            )
            stop_shares = 0
    elif trend == "震荡":
        raw_entry = max(low20, last - atr)
        entry_price = _price(raw_entry)
        target_price = _price(min(high20, last + atr))
        stop_price = _price(max(entry_price - stop_distance, 0.01))
        entry_shares = half_budget
        entry_amount = _money(entry_price, entry_shares)
        entry_parts = _range_entry(last, low20, atr, entry_price, entry_shares, entry_amount)
        target_why = (
            f"卖出参考价 {target_price} 取最近 20 个交易日高点 {_price(high20)} 和现价加一个每日波动 {_price(last + atr)} 里更近的那个。"
            f"震荡里不把目标定得太远。"
        )
        exit_when, exit_shares, stop_when, stop_shares = _exit_for_position(
            holding, cost, last, hot, last < ma20, target_price, stop_price, buy_shares, gap_text, hot_why, target_why
        )
    else:
        raw_entry = min(last, ma10)
        entry_price = _price(raw_entry)
        target_price = _price(max(high20, last + 2 * atr))
        stop_price = _price(max(entry_price - stop_distance, 0.01))
        entry_shares = buy_shares
        entry_amount = _money(entry_price, entry_shares)
        entry_parts = _up_entry(last, ma10, rsi, extended, entry_price, entry_shares, entry_amount)
        target_why = (
            f"卖出参考价 {target_price} 取最近 20 个交易日高点 {_price(high20)} 和现价加两个每日波动 {_price(last + 2 * atr)} 里更高的那个。"
        )
        exit_when, exit_shares, stop_when, stop_shares = _exit_for_position(
            holding, cost, last, hot, False, target_price, stop_price, buy_shares, gap_text, hot_why, target_why
        )

    if trend != "下降" or holding <= 0:
        exit_parts = [exit_when]
    entry_when = "".join(entry_parts)
    shares_text = _shares_text(
        capital, risk_pct, last, stop_distance, buy_shares, entry_shares, trend == "震荡"
    )
    outlook = _outlook(last, atr, lower, upper, trend)
    sections = [
        {
            "title": "现在处在什么位置",
            "paragraphs": [
                _position_text(last, ma10, ma20, ma60, trend),
                _atr_text(atr),
                _rsi_text(rsi),
            ],
        },
        {"title": "后面大约一周可能在哪晃", "paragraphs": [outlook]},
        {"title": "什么时候买、买多少", "paragraphs": _paras(*entry_parts, shares_text)},
        {"title": "什么时候卖、卖多少", "paragraphs": exit_parts},
        {"title": "跌到哪里必须卖", "paragraphs": [stop_when]},
    ]

    return {
        "trend": trend,
        "price": _price(last),
        "ma10": _price(ma10),
        "ma20": _price(ma20),
        "ma60": _price(ma60),
        "atr": _price(atr),
        "rsi": round(rsi, 1),
        "upper": upper,
        "lower": lower,
        "outlook": outlook,
        "sections": sections,
        "entry_when": entry_when,
        "entry_shares": int(entry_shares),
        "exit_when": exit_when,
        "exit_shares": int(exit_shares),
        "stop_when": stop_when,
        "stop_shares": int(stop_shares),
        "holding": int(holding),
    }


def _down_entry(last, ma20, ma60, entry_price, shares, amount):
    parts = [
        (
            f"现在不买。现价 {_t(last)} 还在 20 日均线 {_t(ma20)} 下面，"
            f"20 日均线也还在 60 日均线 {_t(ma60)} 下面。"
            f"近一个月的平均价低于近一个季度，下跌结构没变。"
        ),
        (
            f"可以重新考虑买入，要同时看到两件事。"
            f"第一，某一天的收盘价高于当天的 20 日均线。站上指的是收盘价，不是盘中瞬间摸到。"
            f"第二，20 日均线不再低于 60 日均线。"
            f"按今天的数据，这两条线在 {_t(ma20)} 和 {_t(ma60)}，以后会随新的收盘价变化，不要把今天的数字当成固定买点。"
        ),
    ]
    if shares <= 0:
        parts.append("就算以后两个条件都满足，按当前资金和风险也买不了 1 手。")
    else:
        parts.append(
            f"两个条件都满足后，买 {shares} 股。金额按今天的 20 日均线 {_t(entry_price)} 估算，大约 {amount} 元。"
        )
    return parts


def _range_entry(last, low20, atr, entry_price, shares, amount):
    parts = ["现在涨跌方向还没分开，不追现价。"]
    if entry_price <= _price(low20) + 0.02:
        parts.append(f"买入参考价 {_t(entry_price)} 在最近 20 个交易日低点 {_t(low20)} 一带。")
    else:
        parts.append(
            f"最近 20 个交易日低点是 {_t(low20)}，离现价偏远。"
            f"所以不要求跌到那个低点才买，买入参考价取现价往下一个每日波动，也就是 {_t(entry_price)}。"
        )
    if shares <= 0:
        parts.append("按当前资金和风险，这次连 1 手都买不了。")
    else:
        parts.append(
            f"等价格靠近 {_t(entry_price)} 再买 {shares} 股，大约 {amount} 元。"
            f"现价 {_t(last)} 已经高于这个价，所以现在不追。"
        )
    return parts


def _up_entry(last, ma10, rsi, extended, entry_price, shares, amount):
    if extended:
        reasons = []
        if rsi >= 75:
            reasons.append(f"RSI {round(rsi, 1)} 已经不低于 75，短线偏热")
        if last > ma10 * 1.03:
            reasons.append(f"现价 {_t(last)} 比 10 日均线 {_t(ma10)} 高出 3% 以上")
        why = "，".join(reasons) if reasons else "现价离开短期均线偏远"
        parts = [f"大方向仍是上升，但 {why}。现在追进去，容易买在短线高点。"]
        if shares <= 0:
            parts.append("就算价格回到均线附近，按当前资金和风险也买不了 1 手。")
        else:
            parts.append(
                f"等价格回到 {_t(entry_price)} 附近再买 {shares} 股，大约 {amount} 元。"
                f"这个价是现价和 10 日均线里更低的那个。"
            )
        return parts
    top = _price(entry_price * 1.01)
    if abs(last - ma10) < 0.02:
        where = f"现价和 10 日均线 {_t(ma10)} 几乎在一起。"
    elif last < ma10:
        where = f"现价已经低于 10 日均线 {_t(ma10)}，买入区间放在现价附近，不抬高去追。"
    else:
        where = f"10 日均线 {_t(ma10)} 在现价下面，等价格回到均线附近再买。"
    if shares <= 0:
        order = "按当前资金和风险，这次连 1 手都买不了。"
    else:
        order = f"在这个区间买 {shares} 股，大约 {amount} 元。"
    return [
        "趋势是上升，现价也没有离开 10 日均线太远，可以按计划买。",
        f"{where}买入区间是 {_t(entry_price)} 到 {_t(top)}，上沿只比下沿多 1%，避免追高。{order}",
    ]


def _exit_for_position(holding, cost, last, hot, broken, target_price, stop_price, buy_shares, gap_text, hot_why, target_why):
    pnl = _pnl_text(holding, cost, last)
    if holding <= 0:
        if buy_shares <= 0:
            return (
                "当前没有填写持仓，也还没有可执行的买入股数，所以暂时没有卖出计划。",
                0,
                "没有持仓，也不设止损卖出。",
                0,
            )
        half = _lot(buy_shares / 2)
        if half <= 0:
            half = buy_shares
        return (
            f"当前没有填写持仓。如果按上面的计划买进，涨到 {target_price} 时先卖出一半 {half} 股，把一部分差价落袋，剩下的继续拿。{target_why}",
            half,
            f"买进之后如果跌破 {stop_price}，把还没卖的仓位全部卖掉。{gap_text}",
            buy_shares,
        )
    half = _lot(holding / 2)
    if half <= 0:
        half = holding
    rest = holding - half
    if hot:
        if rest <= 0:
            stop_when = f"这次卖出的就是全部 {holding} 股。如果没卖成，跌破 {stop_price} 也要全部卖掉。{gap_text}"
        else:
            stop_when = f"剩下的 {rest} 股，如果跌破 {stop_price}，再全部卖掉。{gap_text}"
        return (
            f"{hot_why}建议按现价附近先卖出 {half} 股，先把仓位降下来。{pnl}",
            half,
            stop_when,
            rest if rest > 0 else holding,
        )
    if broken:
        return (
            f"现价已经在 20 日均线下面。震荡行情里跌破这条线，就先把持有的 {holding} 股全部卖掉，不在均线下面继续拿。{pnl}",
            holding,
            f"如果当天没有卖成，之后跌破 {stop_price} 也要把 {holding} 股全部卖掉，不要再等反弹。{gap_text}",
            holding,
        )
    if rest <= 0:
        sell_text = f"涨到 {target_price} 时卖出全部 {holding} 股。"
    else:
        sell_text = f"涨到 {target_price} 时先卖出 {half} 股，剩下的 {rest} 股继续拿。"
    return (
        f"趋势还在，不用现在清仓。{sell_text}{target_why}{pnl}",
        half if rest > 0 else holding,
        f"如果价格跌破 {stop_price}，说明这次走势失败，把 {holding} 股全部卖掉。{gap_text}",
        holding,
    )


def _position_text(last, ma10, ma20, ma60, trend):
    if ma20 > ma60 + 0.005:
        relation = f"20 日均线 {_t(ma20)} 高于 60 日均线 {_t(ma60)}，近一个月的价格重心在近一个季度上面。"
    elif ma20 < ma60 - 0.005:
        relation = f"20 日均线 {_t(ma20)} 低于 60 日均线 {_t(ma60)}，近一个月的价格重心在近一个季度下面。"
    else:
        relation = f"20 日均线 {_t(ma20)} 和 60 日均线 {_t(ma60)} 几乎重合。"
    why = {
        "上升": "现价在 20 日均线上面，而且 20 日均线不低于 60 日均线，所以记为上升。",
        "下降": "现价在 20 日均线下面，而且 20 日均线不高于 60 日均线，所以记为下降。",
        "震荡": "现价和 20 日、60 日均线的上下关系不一致，涨跌还没分开，所以记为震荡。",
    }[trend]
    return (
        f"现价 {_t(last)}。"
        f"{_vs('10 日均线', last, ma10)}，{_vs('20 日均线', last, ma20)}，{_vs('60 日均线', last, ma60)}。"
        f"均线就是最近若干个交易日收盘价的平均数：10 日看近两周，20 日看近一个月，60 日看近一个季度。"
        f"{relation}{why}"
    )


def _vs(name, price, line):
    gap = price - line
    if gap >= 0.01:
        return f"比 {name} {_t(line)} 高 {_t(gap)} 元"
    if gap <= -0.01:
        return f"比 {name} {_t(line)} 低 {_t(abs(gap))} 元"
    return f"和 {name} {_t(line)} 基本持平"


def _atr_text(atr):
    return (
        f"14 日波动是 {_t(atr)} 元。这是最近 14 个交易日里，每天最高价和最低价相差多少的平均数，"
        f"用来表示这只股票平时一个交易日大约上下晃 {_t(atr)} 元。"
        f"它不表示明天会涨还是会跌。波动越大，止损就要留得更宽，同样的风险金能买的股数就越少。"
    )


def _rsi_text(rsi):
    value = round(float(rsi), 1)
    shown = f"{value:.1f}"
    if value >= 80:
        judge = "已经到偏热区间。最近涨的幅度远大于跌的幅度，短线容易回落，不适合追。"
    elif value >= 70:
        judge = "偏高。最近涨得多，继续追高容易买在短线高点。"
    elif value <= 20:
        judge = "已经到偏冷区间。最近跌的幅度远大于涨的幅度。短线有反弹可能，但下跌趋势里不能只看这个分数就买。"
    elif value <= 30:
        judge = "偏低。最近跌得多，短线有反弹可能。仍要等价格重新站上均线，不能只因为分数低就抄底。"
    elif value < 45:
        judge = "在中间偏弱的一侧。最近跌的力气比涨的力气大，但还没到 30 以下那种跌过头，不能只因为这个分数去抄底。"
    elif value > 55:
        judge = "在中间偏强的一侧。最近涨的力气比跌的力气大，但还没到 70 以上那种涨过头。"
    else:
        judge = "在中间。最近涨和跌的力气差不多，单看这个分数判断不了方向。"
    return (
        f"RSI 是 {shown}。RSI 全称是相对强弱指标，把最近 14 个交易日的上涨幅度和下跌幅度放在一起比较，得到 0 到 100 的分数。"
        f"可以把它理解成最近是涨的力气大，还是跌的力气大：50 左右表示两边差不多，70 以上表示偏热，30 以下表示偏冷。"
        f"这个分数{judge}"
    )


def _outlook(last, atr, lower, upper, trend):
    if trend == "上升":
        lean = f"当前记为上升，后面这几天价格更常靠近上沿 {upper}；下沿 {lower} 多半是回调时才会碰到。"
    elif trend == "下降":
        lean = f"当前记为下降，后面这几天价格更常靠近下沿 {lower}；上沿 {upper} 要等跌势缓和才容易碰到。"
    else:
        lean = f"当前记为震荡，价格在 {lower} 和 {upper} 之间来回的可能更大，不会只贴着一边走。"
    return (
        f"现价 {_t(last)}，最近每天大约晃 {_t(atr)} 元。"
        f"把一天的晃动放大约 2.2 倍，换成往后大约 5 个交易日（一周左右）的活动范围，是 {lower} 到 {upper}。"
        f"{lean}"
        f"这是按近期波动估出来的范围，不是预测价格一定会到达某一边。"
    )


def _gap_text(atr, last, stop_distance):
    return (
        f"{_t(stop_distance)} 元取的是两倍每日波动（{_t(2 * atr)} 元）和现价的 3%（{_t(last * 0.03)} 元）里更大的一个，"
        f"用来躲开一天之内的正常上下晃动。"
    )


def _hot_why(rsi, last, high20, hot):
    if not hot:
        return ""
    parts = []
    if rsi >= 80:
        parts.append(f"RSI {round(float(rsi), 1)} 已经到 80 以上，短线偏热")
    if last >= high20 * 0.995:
        parts.append(f"现价 {_price(last)} 已经贴着近 20 日最高价 {_price(high20)}")
    if not parts:
        parts.append("短线偏热")
    return "，".join(parts) + "。"


def _shares_text(capital, risk_pct, price, stop_distance, full_shares, actual_shares, half):
    if capital <= 0 or risk_pct <= 0 or price <= 0 or stop_distance <= 0:
        return ""
    risk_money = capital * risk_pct / 100.0
    cap_money = capital * 0.2
    by_risk = int(risk_money / stop_distance / 100) * 100
    by_cap = int(cap_money / price / 100) * 100
    shown_distance = _price(stop_distance)
    approx = int(round(risk_money / shown_distance)) if shown_distance else 0
    if full_shares <= 0:
        return (
            f"股数这样来的：总资金 {_num(capital)} 元，单笔风险 {_num(risk_pct)}%，这笔交易最多允许亏约 {_num(risk_money)} 元。"
            f"止损距离大约 {_t(shown_distance)} 元，买 1 手 100 股就可能亏过这笔钱，所以买入股数是 0。"
            f"把总资金调高，或把单笔风险调大，再点“保存资金”重算。"
        )
    text = (
        f"股数这样来的：总资金 {_num(capital)} 元，单笔风险 {_num(risk_pct)}%，这笔最多亏约 {_num(risk_money)} 元。"
        f"允许亏损 {_num(risk_money)} 元除以止损距离 {_t(shown_distance)} 元，大约 {approx} 股，按 100 股一手取整是 {by_risk} 股。"
        f"另外单只股票最多用总资金的 20%，约 {_num(cap_money)} 元，按现价 {_t(price)} 大约 {by_cap} 股。"
        f"两个限制取更小的，完整仓位是 {full_shares} 股。"
    )
    if half:
        text += f"这次方向没定，只买一半，再按 100 股取整，所以是 {actual_shares} 股。"
    return text


def _pnl_text(holding, cost, last):
    if holding <= 0:
        return ""
    if not cost:
        return f"你填了持有 {holding} 股，还没有成本价，所以这里不算浮盈浮亏。补上成本价后再算。"
    diff = float(last) - float(cost)
    money = int(round(diff * holding))
    if money > 0:
        return f"你持有 {holding} 股，成本 {_t(cost)}，按现价算大约浮盈 {money} 元。"
    if money < 0:
        return f"你持有 {holding} 股，成本 {_t(cost)}，按现价算大约浮亏 {abs(money)} 元。"
    return f"你持有 {holding} 股，成本 {_t(cost)}，现价和成本基本持平。"


def _paras(*parts):
    return [part for part in parts if part]


def _num(value):
    number = float(value)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _position_shares(capital, risk_pct, price, stop_distance):
    if capital <= 0 or risk_pct <= 0 or price <= 0 or stop_distance <= 0:
        return 0
    risk_money = capital * risk_pct / 100.0
    by_risk = int(risk_money / stop_distance / 100) * 100
    by_cap = int((capital * 0.2) / price / 100) * 100
    shares = min(by_risk, by_cap)
    return max(shares, 0)


def _lot(shares):
    try:
        shares = int(float(shares))
    except (TypeError, ValueError):
        return 0
    if shares <= 0:
        return 0
    return shares // 100 * 100


def _price(value):
    return round(float(value) + 1e-8, 2)


def _t(value):
    return f"{_price(value):.2f}"


def _money(price, shares):
    return int(round(float(price) * int(shares)))


def _date_text(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]
