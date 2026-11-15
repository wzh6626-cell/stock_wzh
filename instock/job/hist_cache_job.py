#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os.path
import sys
import time

import pymysql
import pymysql.cursors

cpath_current = os.path.dirname(os.path.dirname(__file__))
cpath = os.path.abspath(os.path.join(cpath_current, os.pardir))
sys.path.append(cpath)

import instock.core.stockfetch as stf
import instock.lib.database as mdb
import instock.lib.trade_time as trd


def main():
    """按一只一只补日 K。已有缓存的跳过，中断后重新运行会从缺的继续。"""
    asof = _latest_selection_date()
    if not asof:
        print("没有选股日期，不能确定日 K 区间")
        return
    date_start, _ = trd.get_trade_hist_interval(asof)
    codes = _codes_missing_first(asof, date_start)
    missing = [code for code in codes if not os.path.isfile(_cache_file(code, date_start))]
    print(f"数据日 {asof}，区间起点 {date_start}，全部 {len(codes)}，还缺 {len(missing)}", flush=True)
    saved = 0
    skipped = 0
    fail_streak = 0
    for index, code in enumerate(missing, 1):
        ok = _fetch_one(code, date_start)
        if ok:
            saved += 1
            fail_streak = 0
            print(f"{index}/{len(missing)} {code} 已保存", flush=True)
        else:
            skipped += 1
            fail_streak += 1
            print(f"{index}/{len(missing)} {code} 这次没拉到，先跳过", flush=True)
            if fail_streak >= 5:
                print("连续失败，暂停 90 秒再继续", flush=True)
                time.sleep(90)
                fail_streak = 0
    print(f"结束。新保存 {saved}，仍缺 {skipped}。再运行一次会只补还缺的。", flush=True)


def _fetch_one(code, date_start):
    for attempt in range(4):
        data = stf.stock_hist_cache(code, date_start, None, True, "qfq")
        if data is not None and len(data.index) > 0 and os.path.isfile(_cache_file(code, date_start)):
            return True
        if attempt < 3:
            wait = 8 * (attempt + 1)
            print(f"{code} 第 {attempt + 1} 次失败，等 {wait} 秒", flush=True)
            time.sleep(wait)
    return False


def _codes_missing_first(asof, date_start):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT `code` FROM `cn_stock_attention` ORDER BY `code`")
            attention = [row["code"] for row in cur.fetchall()]
            cur.execute("SELECT DISTINCT `code` FROM `cn_stock_selection` WHERE `date` = %s", (asof,))
            universe = [row["code"] for row in cur.fetchall()]
    finally:
        conn.close()
    ordered = []
    seen = set()
    for code in attention + universe:
        if code and code not in seen:
            seen.add(code)
            ordered.append(code)
    ordered.sort(key=lambda code: (os.path.isfile(_cache_file(code, date_start)), code not in set(attention), code))
    return ordered


def _cache_file(code, date_start):
    return os.path.join(stf.stock_hist_cache_path, date_start[0:6], date_start, f"{code}qfq.gzip.pickle")


def _latest_selection_date():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(`date`) AS d FROM `cn_stock_selection`")
            row = cur.fetchone()
            if not row or row["d"] is None:
                return None
            value = row["d"]
            if hasattr(value, "strftime"):
                return value.strftime("%Y-%m-%d")
            return str(value)[:10]
    finally:
        conn.close()


def _connect():
    return pymysql.connect(
        host=mdb.db_host,
        user=mdb.db_user,
        password=mdb.db_password,
        database=mdb.db_database,
        port=mdb.db_port,
        charset=mdb.db_charset,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


if __name__ == "__main__":
    main()
