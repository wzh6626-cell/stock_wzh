#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pathlib import Path
import time
import random
from instock.core.singleton_proxy import proxys

__author__ = 'myh '
__date__ = '2025/12/31 '

# push2 行情接口被断开后，短时间内不再连打，避免把临时封锁拖得更久。
_push_down_until = 0.0


def push_is_down():
    return time.time() < _push_down_until


def note_push_down(seconds=15 * 60):
    global _push_down_until
    _push_down_until = time.time() + seconds


def note_push_up():
    global _push_down_until
    _push_down_until = 0.0


def _looks_disconnected(exc):
    text = str(exc)
    return "RemoteDisconnected" in text or "Connection aborted" in text or "closed abruptly" in text


class eastmoney_fetcher:
    """
    东方财富网数据获取器
    封装了Cookie管理、会话管理和请求发送功能
    """

    def __init__(self):
        """初始化获取器"""
        self.base_dir = os.path.dirname(os.path.dirname(__file__))
        self.session = self._create_session()
        self.proxies = proxys().get_proxies()

    def _get_cookie(self):
        """
        获取东方财富网的Cookie
        优先级：环境变量 > 文件 > 默认Cookie
        """
        # 1. 尝试从环境变量获取
        cookie = os.environ.get('EAST_MONEY_COOKIE')
        if cookie:
            # print("环境变量中的Cookie: 已设置")
            return cookie

        # 2. 尝试从文件获取
        cookie_file = Path(os.path.join(self.base_dir, 'config', 'eastmoney_cookie.txt'))
        if cookie_file.exists():
            with open(cookie_file, 'r') as f:
                cookie = f.read().strip()
            if cookie:
                # print("文件中的Cookie: 已设置")
                return cookie

        # 3. 默认Cookie（可能过期，仅作为备选）
        return 'st_si=78948464251292; st_psi=20260205091253851-119144370567-1089607836; st_pvi=07789985376191; st_sp=2026-02-05%2009%3A11%3A13; st_inirUrl=https%3A%2F%2Fxuangu.eastmoney.com%2FResult; st_sn=12; st_asi=20260205091253851-119144370567-1089607836-webznxg.dbssk.qxg-1'

    def _create_session(self):
        """创建并配置会话"""
        session = requests.Session()
        # 未配置项目代理时不使用系统代理。本机 127.0.0.1:7890 对行情接口会返回 502。
        session.trust_env = False

        # 配置连接池
        # 连接被断开时不要在这里立刻连打。外层按秒级等待后再重试。
        retry_strategy = Retry(
            total=0,
            connect=0,
            read=0,
            status=0,
            redirect=0,
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=50,  # 增加连接池大小
            pool_maxsize=50  # 增加连接池最大大小
        )

        # 为http和https请求添加适配器
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # 设置请求头
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://quote.eastmoney.com/',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        session.headers.update(headers)
        _apply_cookie(session, self._get_cookie())
        return session

    def make_request(self, url, params=None, retry=5, timeout=15):
        """
        发送请求
        :param url: 请求URL
        :param params: 请求参数
        :param retry: 重试次数
        :param timeout: 超时时间
        :return: 响应对象
        """
        if _is_push_url(url) and push_is_down():
            raise requests.exceptions.ConnectionError("东财行情接口刚才已被断开，先不再连打")
        for i in range(retry):
            try:
                response = self.session.get(
                    url,
                    proxies=self.proxies,
                    params=params,
                    timeout=timeout
                )
                response.raise_for_status()  # 检查HTTP错误
                body = response.content.lstrip()
                if not body or body[:1] not in (b"{", b"[", b"j", b"J"):
                    raise requests.exceptions.RequestException(
                        f"unexpected body status={response.status_code} body={body[:80]!r}"
                    )
                if _is_push_url(url):
                    note_push_up()
                return response
            except requests.exceptions.RequestException as e:
                print(f"请求错误: {e}, 第 {i + 1}/{retry} 次重试")
                self.session.close()
                self.session = self._create_session()
                # 对方直接掐连接时，继续重试只会把封锁延长。
                if _is_push_url(url) and _looks_disconnected(e):
                    note_push_down()
                    raise
                if i < retry - 1:
                    time.sleep(min(30, 3 * (2 ** i)) + random.uniform(0, 1))
                else:
                    raise

    def make_post_request(self, url, data=None, json=None, params=None, retry=3, timeout=60):
        """
        发送POST请求
        :param url: 请求URL
        :param data: 请求数据（表单形式）
        :param json: 请求数据（JSON形式）
        :param params: URL参数
        :param retry: 重试次数
        :param timeout: 超时时间
        :return: 响应对象
        """
        for i in range(retry):
            try:
                response = self.session.post(
                    url,
                    proxies=self.proxies,
                    params=params,
                    data=data,
                    json=json,
                    timeout=timeout
                )
                response.raise_for_status()  # 检查HTTP错误
                return response
            except requests.exceptions.RequestException as e:
                print(f"请求错误: {e}, 第 {i + 1}/{retry} 次重试")
                if i < retry - 1:
                    # 随机延迟后重试
                    time.sleep(random.uniform(1, 3))
                else:
                    raise

    def probe(self):
        """各发一次请求，确认拉取任务用到的东财接口现在能不能通。"""
        items = [
            self._probe_one(
                "行情网页",
                "https://quote.eastmoney.com/sh600000.html",
                None,
                _page_ok,
            ),
            self._probe_one(
                "行情列表",
                "http://82.push2.eastmoney.com/api/qt/clist/get",
                {
                    "pn": "1",
                    "pz": "5",
                    "po": "1",
                    "np": "1",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f12",
                    "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
                    "fields": "f12,f14,f2",
                },
                _spot_ok,
            ),
            self._probe_one(
                "日K",
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                {
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56",
                    "ut": "7eea3edcaed734bea9cbfc24409ed989",
                    "klt": "101",
                    "fqt": "1",
                    "secid": "1.600000",
                    "beg": "20261101",
                    "end": "20261115",
                },
                _kline_ok,
            ),
        ]
        return {"ok": all(item["ok"] for item in items), "items": items}

    def _probe_one(self, name, url, params, accept):
        started = time.time()
        try:
            response = self.session.get(url, proxies=self.proxies, params=params, timeout=8)
            response.raise_for_status()
            message = accept(response)
            if _is_push_url(url):
                note_push_up()
            return _probe_result(name, True, message, started)
        except Exception as exc:
            if _is_push_url(url) and _looks_disconnected(exc):
                note_push_down()
            return _probe_result(name, False, _probe_error(exc), started)

    def update_cookie(self, new_cookie):
        """
        更新Cookie
        :param new_cookie: 新的Cookie值
        """
        _apply_cookie(self.session, new_cookie)


def _probe_result(name, ok, message, started):
    return {
        "name": name,
        "ok": ok,
        "message": message,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _apply_cookie(session, cookie):
    session.cookies.clear()
    for part in (cookie or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            session.cookies.set(name, value.strip())


def _is_push_url(url):
    return "push2" in (url or "")


def _spot_ok(response):
    payload = response.json()
    rows = ((payload or {}).get("data") or {}).get("diff") or []
    total = ((payload or {}).get("data") or {}).get("total") or 0
    if not rows:
        raise ValueError("没有返回股票列表")
    return f"合计 {total} 只"


def _kline_ok(response):
    payload = response.json()
    klines = ((payload or {}).get("data") or {}).get("klines") or []
    if not klines:
        raise ValueError("没有返回日K")
    return f"浦发银行返回 {len(klines)} 根日K"


def _page_ok(response):
    if "东方财富" not in response.text:
        raise ValueError("页面内容不对")
    return "页面可以打开"


def _probe_error(exc):
    text = str(exc)
    if "RemoteDisconnected" in text or "Connection aborted" in text or "closed abruptly" in text:
        return "连接被对方断开。行情网页如果正常，就是 push 行情接口把这个地址拦住了"
    if "timed out" in text or "Timeout" in text:
        return "8 秒内没有返回"
    if "502" in text or "503" in text or "504" in text:
        return "接口返回了服务不可用"
    if "没有返回" in text or "页面内容不对" in text:
        return text
    return "请求失败"
