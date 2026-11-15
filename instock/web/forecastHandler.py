#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from abc import ABC
from urllib.parse import quote

import instock.web.base as webBase
import instock.web.forecast_board as board


class ForecastPageHandler(webBase.BaseHandler, ABC):
    def get(self):
        notice = self.get_argument("notice", default="", strip=False)
        open_code = "".join(ch for ch in self.get_argument("code", default="") if ch.isdigit())
        data = board.build_board()
        self.render(
            "forecast.html",
            leftMenu=webBase.GetLeftMenu(self.request.uri),
            notice=notice,
            asof=data["asof"],
            setting=data["setting"],
            plans=data["plans"],
            open_code=open_code,
        )

    def post(self):
        action = self.get_argument("action", default="", strip=False)
        notice = ""
        open_code = ""
        try:
            if action == "add":
                open_code = board.add_code(self.get_argument("code", default=""))
                notice = f"已加入自选 {open_code}"
            elif action == "remove":
                board.remove_code(self.get_argument("code", default=""))
                notice = "已从自选移除"
            elif action == "import_formula1":
                asof = board.latest_selection_date()
                count = board.import_formula1(asof) if asof else 0
                notice = f"已把公式一的 {count} 只股票加入自选"
            elif action == "import_formula2":
                asof = board.latest_selection_date()
                count = board.import_formula2(asof) if asof else 0
                notice = f"已把巴菲特选股1的 {count} 只股票加入自选"
            elif action == "setting":
                board.save_setting(
                    self.get_argument("capital", default="100000"),
                    self.get_argument("risk_pct", default="1"),
                )
                notice = "资金和风险设置已保存，下面的股数已按新设置重算"
            elif action == "holding":
                open_code = self.get_argument("code", default="")
                board.save_holding(
                    open_code,
                    self.get_argument("shares", default="0"),
                    self.get_argument("cost", default=""),
                )
                notice = "持仓已保存，卖出股数按这笔持仓重算"
            else:
                notice = "没有识别到操作"
        except ValueError as exc:
            notice = str(exc)
        target = "/instock/forecast?notice=" + quote(notice)
        if open_code:
            target += "&code=" + quote(open_code)
        self.redirect(target)


class ForecastAccuracyHandler(webBase.BaseHandler, ABC):
    def get(self):
        notice = self.get_argument("notice", default="", strip=False)
        data = board.build_accuracy()
        self.render(
            "forecast_accuracy.html",
            leftMenu=webBase.GetLeftMenu(self.request.uri),
            notice=notice,
            asof=data["asof"],
            review=data["review"],
            tune_logs=data["tune_logs"],
        )

    def post(self):
        try:
            notice = board.optimize_scheme()
        except ValueError as exc:
            notice = str(exc)
        self.redirect("/instock/forecast/accuracy?notice=" + quote(notice))
