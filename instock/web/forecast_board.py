#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime

import pymysql
import pymysql.cursors

import instock.core.forecast_accuracy as forecast_accuracy
import instock.core.forecast_plan as forecast_plan
import instock.lib.database as mdb

FORMULA1_SQL = """
    SELECT `code` FROM `cn_stock_selection`
    WHERE `date` = %s
      AND `deal_amount` > 200000000
      AND `pe9` > 0 AND `pe9` < 25
      AND `pbnewmrq` > 0 AND `pbnewmrq` < 5
      AND `roe_weight` > 12
      AND `ycpeg` > 0 AND `ycpeg` < 1
      AND `netprofit_yoy_ratio` > 10
      AND `deduct_netprofit_growthrate` > 10
      AND `toi_yoy_ratio` > 0
      AND `debt_asset_ratio` < 55
      AND `change_rate` >= -2 AND `change_rate` <= 4
      AND `name` NOT LIKE '%%ST%%'
"""

# 巴菲特选股1。表里没有历年 ROE、历年毛利率、有息负债和折现估值。
# 长期稳定用净利润 3 年复合增长仍为正；财务安全用总负债代替有息负债；
# 估值用每股自由现金流按 8% 永续折现，现价要低于该估值的 70%。
FORMULA2_SQL = """
    SELECT `code` FROM `cn_stock_selection`
    WHERE `date` = %s
      AND `name` NOT LIKE '%%ST%%'
      AND `roe_weight` > 15
      AND `netprofit_growthrate_3y` > 0
      AND `sale_gpr` > 40
      AND `per_fcfe` > 0
      AND `basic_eps` > 0
      AND `new_price` > 0
      AND (`per_netcash_operate` - `per_fcfe`) / `basic_eps` < 0.5
      AND `parent_netprofit` > 0
      AND `bvps` > 0
      AND `total_shares` > 0
      AND `debt_asset_ratio` >= 0
      AND `debt_asset_ratio` < 100
      AND (`bvps` * `total_shares` * `debt_asset_ratio` / (100 - `debt_asset_ratio`)) / `parent_netprofit` < 4
      AND `per_fcfe` / `new_price` > (0.08 / 0.7)
"""


def ensure_tables():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS `cn_stock_attention` (
                  `datetime` datetime NULL DEFAULT NULL,
                  `code` varchar(6) NOT NULL,
                  PRIMARY KEY (`code`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS `cn_stock_holding` (
                  `code` varchar(6) NOT NULL,
                  `shares` int NOT NULL DEFAULT 0,
                  `cost` decimal(12,3) NULL DEFAULT NULL,
                  PRIMARY KEY (`code`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS `cn_forecast_setting` (
                  `id` tinyint NOT NULL,
                  `capital` decimal(16,2) NOT NULL DEFAULT 100000,
                  `risk_pct` decimal(6,2) NOT NULL DEFAULT 1.00,
                  PRIMARY KEY (`id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                "INSERT IGNORE INTO `cn_forecast_setting` (`id`, `capital`, `risk_pct`) VALUES (1, 100000, 1)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS `cn_forecast_scheme` (
                  `id` tinyint NOT NULL,
                  `band_mult` decimal(8,4) NOT NULL,
                  `trend_gap` decimal(8,4) NOT NULL,
                  `ma_align` tinyint NOT NULL,
                  PRIMARY KEY (`id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            default = forecast_plan.default_scheme()
            cur.execute(
                "INSERT IGNORE INTO `cn_forecast_scheme` (`id`, `band_mult`, `trend_gap`, `ma_align`) "
                "VALUES (1, %s, %s, %s)",
                (default["band_mult"], default["trend_gap"], default["ma_align"]),
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS `cn_forecast_tune_log` (
                  `id` int NOT NULL AUTO_INCREMENT,
                  `created_at` datetime NOT NULL,
                  `sample_count` int NOT NULL,
                  `hit_before` int NOT NULL,
                  `hit_after` int NOT NULL,
                  `accuracy_before` decimal(6,2) NOT NULL,
                  `accuracy_after` decimal(6,2) NOT NULL,
                  `adopted` tinyint NOT NULL,
                  `band_mult_before` decimal(8,4) NOT NULL,
                  `band_mult_after` decimal(8,4) NOT NULL,
                  `trend_gap_before` decimal(8,4) NOT NULL,
                  `trend_gap_after` decimal(8,4) NOT NULL,
                  `ma_align_before` tinyint NOT NULL,
                  `ma_align_after` tinyint NOT NULL,
                  `reason` varchar(1000) NOT NULL,
                  PRIMARY KEY (`id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
    finally:
        conn.close()


def latest_selection_date():
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


def load_setting():
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT `capital`, `risk_pct` FROM `cn_forecast_setting` WHERE `id` = 1")
            row = cur.fetchone()
            return {"capital": float(row["capital"]), "risk_pct": float(row["risk_pct"])}
    finally:
        conn.close()


def save_setting(capital, risk_pct):
    capital = float(capital)
    risk_pct = float(risk_pct)
    if capital <= 0 or capital > 100000000000:
        raise ValueError("总资金要大于 0")
    if risk_pct <= 0 or risk_pct > 20:
        raise ValueError("单笔风险请填 0 到 20 之间的百分比")
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `cn_forecast_setting` SET `capital` = %s, `risk_pct` = %s WHERE `id` = 1",
                (capital, risk_pct),
            )
    finally:
        conn.close()


def add_code(code):
    code = _clean_code(code)
    if code is None:
        raise ValueError("请输入 6 位股票代码")
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `cn_stock_attention` (`datetime`, `code`) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE `datetime` = VALUES(`datetime`)",
                (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), code),
            )
    finally:
        conn.close()
    return code


def remove_code(code):
    code = _clean_code(code)
    if code is None:
        return
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM `cn_stock_attention` WHERE `code` = %s", (code,))
    finally:
        conn.close()


def save_holding(code, shares, cost):
    code = _clean_code(code)
    if code is None:
        raise ValueError("股票代码不正确")
    shares = forecast_plan._lot(shares)
    cost_value = None
    if str(cost).strip() != "":
        cost_value = round(float(cost), 3)
        if cost_value <= 0:
            raise ValueError("成本价要大于 0")
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `cn_stock_holding` (`code`, `shares`, `cost`) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE `shares` = VALUES(`shares`), `cost` = VALUES(`cost`)",
                (code, shares, cost_value),
            )
    finally:
        conn.close()


def import_formula1(asof_date):
    return _import_codes(FORMULA1_SQL, asof_date)


def import_formula2(asof_date):
    return _import_codes(FORMULA2_SQL, asof_date)


def _import_codes(sql, asof_date):
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (asof_date,))
            codes = [row["code"] for row in cur.fetchall()]
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for code in codes:
                cur.execute(
                    "INSERT INTO `cn_stock_attention` (`datetime`, `code`) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE `datetime` = `datetime`",
                    (now, code),
                )
            return len(codes)
    finally:
        conn.close()


def load_scheme():
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT `band_mult`, `trend_gap`, `ma_align` FROM `cn_forecast_scheme` WHERE `id` = 1"
            )
            row = cur.fetchone()
            if not row:
                return forecast_plan.default_scheme()
            return forecast_plan.normalize_scheme(row)
    finally:
        conn.close()


def save_scheme(scheme):
    scheme = forecast_plan.normalize_scheme(scheme)
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `cn_forecast_scheme` SET `band_mult` = %s, `trend_gap` = %s, `ma_align` = %s WHERE `id` = 1",
                (scheme["band_mult"], scheme["trend_gap"], scheme["ma_align"]),
            )
    finally:
        conn.close()
    return scheme


def load_tune_logs(limit=20):
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT `created_at`, `accuracy_before`, `accuracy_after`, `adopted`, `reason` "
                "FROM `cn_forecast_tune_log` ORDER BY `id` DESC LIMIT %s",
                (int(limit),),
            )
            logs = []
            for row in cur.fetchall():
                created = row["created_at"]
                if hasattr(created, "strftime"):
                    created = created.strftime("%Y-%m-%d %H:%M")
                logs.append({
                    "created_at": str(created),
                    "accuracy_before": f"{float(row['accuracy_before']):.0f}%",
                    "accuracy_after": f"{float(row['accuracy_after']):.0f}%",
                    "adopted": "已采用" if int(row["adopted"]) else "未采用",
                    "reason": row["reason"],
                })
            return logs
    finally:
        conn.close()


def optimize_scheme():
    """按当前最大的偏差改一档方案。准确率提高才采用，无论是否采用都写入记录表。"""
    scheme = load_scheme()
    asof = latest_selection_date()
    codes = _attention_codes()
    before = forecast_accuracy.review(codes, asof, scheme)
    if not before["ready"]:
        raise ValueError(before["summary"])
    if before["total"] < 8:
        raise ValueError("样本不到 8 次，先不改方案。")
    if before["rate"] >= 70:
        raise ValueError(f"准确率是 {before['rate_text']}，已经不低于 70%，这次不改方案。")
    proposal = forecast_accuracy.propose(before)
    if proposal is None:
        raise ValueError("现有偏差没有还能调的项目，方案保持不变。")
    nxt = proposal["scheme"]
    if _rejected_before(scheme, nxt):
        raise ValueError("上次按这个方向改过，准确率没有提高，先保持现在的方案。")
    after = forecast_accuracy.review(codes, asof, nxt)
    adopted = bool(
        after["ready"]
        and after["total"] >= before["total"]
        and after["rate"] > before["rate"]
    )
    reason = (
        f"{proposal['why']}。{proposal['change']}。"
        f"优化前 {before['rate_text']}，试算后 {after['rate_text']}。"
    )
    if adopted:
        reason += "准确率提高了，已改用新方案。"
        save_scheme(nxt)
    else:
        reason += "准确率没有提高，方案保持不变。"
    if len(reason) > 1000:
        reason = reason[:1000]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO `cn_forecast_tune_log` (
                  `created_at`, `sample_count`, `hit_before`, `hit_after`,
                  `accuracy_before`, `accuracy_after`, `adopted`,
                  `band_mult_before`, `band_mult_after`,
                  `trend_gap_before`, `trend_gap_after`,
                  `ma_align_before`, `ma_align_after`, `reason`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    before["total"],
                    before["hits"],
                    after["hits"],
                    round(before["rate"], 2),
                    round(after["rate"], 2),
                    1 if adopted else 0,
                    scheme["band_mult"],
                    nxt["band_mult"],
                    scheme["trend_gap"],
                    nxt["trend_gap"],
                    scheme["ma_align"],
                    nxt["ma_align"],
                    reason,
                ),
            )
    finally:
        conn.close()
    return reason


def _attention_codes():
    ensure_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT `code` FROM `cn_stock_attention` ORDER BY `code`")
            return [row["code"] for row in cur.fetchall()]
    finally:
        conn.close()


def _rejected_before(before, after):
    before = forecast_plan.normalize_scheme(before)
    after = forecast_plan.normalize_scheme(after)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT `id` FROM `cn_forecast_tune_log`
                WHERE `adopted` = 0
                  AND `band_mult_before` = %s AND `band_mult_after` = %s
                  AND `trend_gap_before` = %s AND `trend_gap_after` = %s
                  AND `ma_align_before` = %s AND `ma_align_after` = %s
                LIMIT 1
                """,
                (
                    before["band_mult"], after["band_mult"],
                    before["trend_gap"], after["trend_gap"],
                    before["ma_align"], after["ma_align"],
                ),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def build_board():
    ensure_tables()
    setting = load_setting()
    scheme = load_scheme()
    asof = latest_selection_date()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.`code`, s.`name`, s.`volume`, h.`shares`, h.`cost`
                FROM `cn_stock_attention` a
                LEFT JOIN `cn_stock_selection` s
                  ON s.`code` COLLATE utf8mb4_general_ci = a.`code` AND s.`date` = %s
                LEFT JOIN `cn_stock_holding` h
                  ON h.`code` COLLATE utf8mb4_general_ci = a.`code`
                """,
                (asof,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    plans = []
    for row in rows:
        code = row["code"]
        name = row["name"] or ""
        shares = int(row["shares"] or 0)
        cost = float(row["cost"]) if row["cost"] is not None else ""
        volume = int(row["volume"] or 0)
        item = {
            "code": code,
            "name": name,
            "shares": shares,
            "cost": cost,
            "volume": volume,
            "volume_text": _volume_text(volume),
            "headline": "",
            "ready": False,
            "message": "",
        }
        data = None
        if asof:
            data = forecast_plan.load_cached_hist(code, asof)
        if data is not None and item["volume"] <= 0 and "volume" in data.columns and len(data.index) > 0:
            try:
                latest = float(data["volume"].iloc[-1])
            except (TypeError, ValueError):
                latest = 0
            if latest == latest and latest > 0:
                item["volume"] = int(latest)
                item["volume_text"] = _volume_text(item["volume"])
        if data is None:
            item["message"] = "没有本地日 K，暂时不能预判。先打开这只股票的 K 线页，或等日 K 缓存补齐后再看。"
            item["headline"] = item["message"]
            plans.append(item)
            continue
        plan = forecast_plan.build_plan(
            data,
            shares,
            float(cost) if cost != "" else 0,
            setting["capital"],
            setting["risk_pct"],
            scheme,
        )
        if plan is None:
            item["message"] = "日 K 不足以计算均线和波动。"
            item["headline"] = item["message"]
            plans.append(item)
            continue
        item.update(plan)
        item["ready"] = True
        item["headline"] = _headline(item)
        plans.append(item)
    plans.sort(key=lambda item: (-int(item.get("volume") or 0), item["code"]))
    return {"asof": asof, "setting": setting, "plans": plans}


def build_accuracy():
    ensure_tables()
    scheme = load_scheme()
    asof = latest_selection_date()
    review = forecast_accuracy.review(_attention_codes(), asof, scheme)
    return {"asof": asof, "review": review, "tune_logs": load_tune_logs()}


def _volume_text(volume):
    volume = int(volume or 0)
    if volume <= 0:
        return "成交量暂无"
    if volume >= 100000000:
        text = f"{volume / 100000000:.2f}亿"
    elif volume >= 10000:
        text = f"{volume / 10000:.2f}万"
    else:
        text = str(volume)
    return f"成交量 {text}手"


def _headline(item):
    trend = item.get("trend")
    holding = int(item.get("holding") or 0)
    if trend == "下降":
        if holding > 0:
            return "现在不买。已有持仓，建议按现价卖掉。"
        return "现在不买，等重新站上均线。"
    if trend == "震荡":
        return "方向没定，不追现价。"
    entry = item.get("entry_when") or ""
    if "不追" in entry or "偏热" in entry:
        return "上升，但先不要追高。"
    return "上升，可以按计划买。"


def _clean_code(code):
    text = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(text) != 6:
        return None
    return text


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
