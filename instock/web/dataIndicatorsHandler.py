#!/usr/local/bin/python3
# -*- coding: utf-8 -*-

import logging
import os
from abc import ABC

from tornado import gen
from tornado.ioloop import IOLoop

import instock.core.kline.visualization as vis
import instock.core.kline_screen as kline_screen
import instock.core.stockfetch as stf
import instock.lib.trade_time as trd
import instock.web.base as webBase
import instock.web.forecast_board as board

__author__ = 'myh '
__date__ = '2023/3/10 '


# 获得页面数据。
class GetDataIndicatorsHandler(webBase.BaseHandler, ABC):
    @gen.coroutine
    def get(self):
        code = self.get_argument("code", default=None, strip=False)
        date = self.get_argument("date", default=None, strip=False)
        name = self.get_argument("name", default=None, strip=False)
        comp_list = []
        notice = None
        try:
            if code is None or date is None:
                notice = "缺少股票代码或日期，无法打开行情图。"
            elif code.startswith(('1', '5')):
                stock = stf.fetch_etf_hist((date, code))
            else:
                stock = stf.fetch_stock_hist((date, code))
            if notice is None and stock is None:
                notice = f"{code} 没有读到日 K 线，行情图无法显示。请先在「拉取数据」中完成股票指标或 K 线相关抓取。"
            elif notice is None:
                pk = vis.get_plot_kline(code, stock, date, name)
                if pk is None:
                    notice = f"{code} 行情图生成失败，请查看 instock/log/stock_web.log。"
                else:
                    comp_list.append(pk)
        except Exception as e:
            logging.error(f"dataIndicatorsHandler.GetDataIndicatorsHandler处理异常：{e}")
            notice = "行情图生成失败，请查看 instock/log/stock_web.log。"

        self.render("stock_indicators.html", comp_list=comp_list, notice=notice,
                    leftMenu=webBase.GetLeftMenu(self.request.uri))


# 关注股票。
class SaveCollectHandler(webBase.BaseHandler, ABC):
    @gen.coroutine
    def get(self):
        import datetime
        import instock.core.tablestructure as tbs
        code = self.get_argument("code", default=None, strip=False)
        otype = self.get_argument("otype", default=None, strip=False)
        try:
            table_name = tbs.TABLE_CN_STOCK_ATTENTION['name']
            if otype == '1':
                # sql = f"DELETE FROM `{table_name}` WHERE `code` = '{code}'"
                sql = f"DELETE FROM `{table_name}` WHERE `code` = %s"
                self.db.query(sql,code)
            else:
                added_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                sql = f"INSERT INTO `{table_name}`(`datetime`, `code`) VALUES (%s, %s) ON DUPLICATE KEY UPDATE `datetime` = %s"
                self.db.query(sql, added_at, code, added_at)
        except Exception as e:
            err = {"error": str(e)}
            # logging.info(err)
            # self.write(err)
            # return
        self.write("{\"data\":[{}]}")


class SelectionScreenHandler(webBase.BaseHandler, ABC):
    @gen.coroutine
    def get(self):
        asof = self.get_argument("date", default="", strip=False).strip()[:10]
        if len(asof) != 10:
            asof = board.latest_selection_date() or ""
        if len(asof) != 10:
            self.set_status(400)
            self.write({"ok": False, "message": "没有数据日，不能计算相对强弱"})
            return
        try:
            result = yield IOLoop.current().run_in_executor(None, kline_screen.screen_payload, asof)
        except Exception as exc:
            logging.error(f"dataIndicatorsHandler.SelectionScreenHandler处理异常：{exc}")
            self.set_status(500)
            self.write({"ok": False, "message": "相对强弱没有算完"})
            return
        self.write(result)


class FetchDailyKHandler(webBase.BaseHandler, ABC):
    @gen.coroutine
    def get(self):
        code = "".join(ch for ch in self.get_argument("code", default="") if ch.isdigit())
        if len(code) != 6:
            self.set_status(400)
            self.write({"ok": False, "message": "股票代码要是 6 位数字"})
            return
        asof = self.get_argument("date", default="", strip=False).strip()[:10]
        if len(asof) != 10:
            asof = board.latest_selection_date() or ""
        if len(asof) != 10:
            self.set_status(400)
            self.write({"ok": False, "code": code, "message": "没有数据日，不能确定日K区间"})
            return
        try:
            result = yield IOLoop.current().run_in_executor(None, _fetch_daily, code, asof)
        except Exception as exc:
            logging.error(f"dataIndicatorsHandler.FetchDailyKHandler处理异常：{exc}")
            self.set_status(500)
            self.write({"ok": False, "code": code, "message": "拉取没有完成"})
            return
        if not result["ok"]:
            self.set_status(502)
        self.write(result)


def _fetch_daily(code, asof):
    date_start, _ = trd.get_trade_hist_interval(asof)
    cache_file = os.path.join(
        stf.stock_hist_cache_path, date_start[0:6], date_start, f"{code}qfq.gzip.pickle"
    )
    existed = os.path.isfile(cache_file)
    data = stf.stock_hist_cache(code, date_start, None, True, "qfq")
    if data is None or len(data.index) == 0:
        return {"ok": False, "code": code, "message": f"{code} 这次没有拉到日K"}
    first = str(data.iloc[0]["date"])[:10]
    last = str(data.iloc[-1]["date"])[:10]
    bars = int(len(data.index))
    if existed:
        message = f"本地已有 {bars} 根日K，从 {first} 到 {last}"
    else:
        message = f"已保存 {bars} 根日K，从 {first} 到 {last}"
    return {
        "ok": True,
        "code": code,
        "bars": bars,
        "start": first,
        "end": last,
        "cached": existed,
        "message": message,
    }