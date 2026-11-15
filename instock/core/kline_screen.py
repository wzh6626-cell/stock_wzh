#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import threading

import numpy as np
import pandas as pd

import instock.core.stockfetch as stf
import instock.lib.trade_time as trd

_LOCK = threading.Lock()


def screen_payload(asof):
    """Rank local daily K: 120-day return percentile and one-year volatility."""
    metrics = _metrics(asof)
    returns = sorted(item["ret"] for item in metrics if item["ret"] is not None)
    vols = sorted(item["vol"] for item in metrics if item["vol"] is not None)
    rps = []
    return_count = len(returns)
    if return_count > 1:
        for item in metrics:
            if item["ret"] is None or not item["above"]:
                continue
            weaker = _below(returns, item["ret"])
            if 100.0 * weaker / (return_count - 1) > 85:
                rps.append(item["code"])
    low_vol = []
    vol_count = len(vols)
    if vol_count > 1:
        cutoff = int(vol_count * 0.30)
        order = sorted(
            (item for item in metrics if item["vol"] is not None),
            key=lambda item: (item["vol"], item["code"]),
        )
        low_vol = [item["code"] for item in order[:cutoff]]
    return {
        "ok": True,
        "ranked": return_count,
        "vol_ranked": vol_count,
        "rps": rps,
        "low_vol": low_vol,
    }


def _metrics(asof):
    date_start, _ = trd.get_trade_hist_interval(asof)
    cache_dir = os.path.join(stf.stock_hist_cache_path, date_start[0:6], date_start)
    path = os.path.join(cache_dir, "_screen_metrics.json")
    with _LOCK:
        if _needs_build(cache_dir, path):
            _build(cache_dir, path)
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    rows = []
    for code, item in raw.items():
        rows.append({
            "code": str(code),
            "ret": item.get("ret"),
            "above": bool(item.get("above")),
            "vol": item.get("vol"),
        })
    return rows


def _needs_build(cache_dir, path):
    if not os.path.isdir(cache_dir) or not os.path.isfile(path):
        return os.path.isdir(cache_dir)
    stamp = os.path.getmtime(path)
    for name in os.listdir(cache_dir):
        if not name.endswith("qfq.gzip.pickle"):
            continue
        if os.path.getmtime(os.path.join(cache_dir, name)) > stamp:
            return True
    return False


def _build(cache_dir, path):
    payload = {}
    if not os.path.isdir(cache_dir):
        return
    for name in os.listdir(cache_dir):
        if not name.endswith("qfq.gzip.pickle"):
            continue
        code = name[:6]
        item = _one(os.path.join(cache_dir, name))
        if item is not None:
            payload[code] = item
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, path)


def _one(path):
    try:
        frame = pd.read_pickle(path, compression="gzip")
    except Exception:
        return None
    if frame is None or "close" not in frame.columns or len(frame.index) < 121:
        return None
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype="float64")
    result = {"ret": None, "above": 0, "vol": None}
    window = close[-121:]
    if np.isfinite(window).all() and window[0] > 0 and window[-1] > 0:
        result["ret"] = round(float(window[-1] / window[0] - 1.0), 6)
        ma50 = close[-50:]
        if np.isfinite(ma50).all() and float(ma50.mean()) > 0:
            result["above"] = 1 if float(window[-1]) > float(ma50.mean()) else 0
    year = close[-251:]
    if year.size == 251 and np.isfinite(year).all() and np.all(year[:-1] > 0):
        change = year[1:] / year[:-1] - 1.0
        result["vol"] = round(float(np.std(change)), 6)
    if result["ret"] is None and result["vol"] is None:
        return None
    return result


def _below(ordered, value):
    low = 0
    high = len(ordered)
    while low < high:
        mid = (low + high) // 2
        if ordered[mid] < value:
            low = mid + 1
        else:
            high = mid
    return low
