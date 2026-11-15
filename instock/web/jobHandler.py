#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from abc import ABC

import instock.core.eastmoney_fetcher as eastmoney_fetcher
import instock.web.base as webBase
import instock.web.job_manager as job_manager


class JobPageHandler(webBase.BaseHandler, ABC):
    def get(self, page):
        template = "job_fetch.html" if page == "fetch" else "job_schedule.html"
        self.render(template, leftMenu=webBase.GetLeftMenu(self.request.uri))


class JobStateHandler(webBase.BaseHandler, ABC):
    def get(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write({"ok": True, "data": job_manager.state()})


class JobRunHandler(webBase.BaseHandler, ABC):
    def post(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        try:
            payload = json.loads(self.request.body.decode("utf-8") or "{}")
            keys = payload.get("jobs") or []
            ordered = job_manager.enqueue(keys, "manual")
        except RuntimeError as exc:
            self.set_status(409)
            self.write({"ok": False, "message": str(exc)})
            return
        except (ValueError, json.JSONDecodeError) as exc:
            self.set_status(400)
            self.write({"ok": False, "message": str(exc)})
            return
        self.write({"ok": True, "message": "已开始按顺序拉取", "jobs": ordered})


class JobStopHandler(webBase.BaseHandler, ABC):
    def post(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        job_manager.stop_jobs()
        self.write({"ok": True, "message": "已发送停止"})


class EastMoneyProbeHandler(webBase.BaseHandler, ABC):
    def get(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        try:
            result = eastmoney_fetcher.eastmoney_fetcher().probe()
        except Exception:
            self.set_status(500)
            self.write({"ok": False, "message": "检查没有完成"})
            return
        self.write(result)


class JobScheduleSaveHandler(webBase.BaseHandler, ABC):
    def post(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        try:
            payload = json.loads(self.request.body.decode("utf-8") or "{}")
            job_manager.save_schedules(payload.get("items") or [])
        except (ValueError, json.JSONDecodeError) as exc:
            self.set_status(400)
            self.write({"ok": False, "message": str(exc)})
            return
        self.write({"ok": True, "message": "自动同步设置已保存"})
