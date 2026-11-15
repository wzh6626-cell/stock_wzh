#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta

import pymysql
import pymysql.cursors

import instock.lib.database as mdb

INSTOCK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.path.join(INSTOCK_DIR, "job")
LOG_DIR = os.path.join(INSTOCK_DIR, "log", "job_runs")

JOBS = (
    {"key": "selection", "name": "综合选股", "script": "selection_data_daily_job.py",
     "desc": "抓取东方财富综合选股，对应左侧「综合选股」。"},
    {"key": "basic", "name": "每日股票与ETF", "script": "basic_data_daily_job.py",
     "desc": "抓取每日股票行情和 ETF。行情列表接口不稳定时可能失败。"},
    {"key": "other", "name": "龙虎榜、分红、资金流向、涨停", "script": "basic_data_other_daily_job.py",
     "desc": "龙虎榜、分红配送、资金流向、早盘抢筹、涨停原因，并生成基本面选股。"},
    {"key": "after_close", "name": "大宗交易与尾盘抢筹", "script": "basic_data_after_close_daily_job.py",
     "desc": "收盘后数据：大宗交易、尾盘抢筹。"},
    {"key": "indicators", "name": "股票指标", "script": "indicators_data_daily_job.py",
     "desc": "按日 K 线计算技术指标，并筛选指标买入、卖出。耗时较长。"},
    {"key": "kline", "name": "K线形态", "script": "klinepattern_data_daily_job.py",
     "desc": "识别 K 线形态。需先有日 K 线，耗时较长。"},
    {"key": "strategy", "name": "策略选股", "script": "strategy_data_daily_job.py",
     "desc": "运行内置选股策略。需先有日 K 线。"},
)

_JOB_MAP = {item["key"]: item for item in JOBS}
_lock = threading.Lock()
_proc = None
_cancel = False
_worker_running = False
_scheduler_started = False


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


def ensure_tables():
    os.makedirs(LOG_DIR, exist_ok=True)
    schedule_sql = """
        CREATE TABLE IF NOT EXISTS `cn_job_schedule` (
          `job_key` varchar(64) NOT NULL,
          `enabled` tinyint NOT NULL DEFAULT 0,
          `run_time` char(5) NOT NULL DEFAULT '17:30',
          `weekdays_only` tinyint NOT NULL DEFAULT 1,
          `updated_at` datetime NULL DEFAULT NULL,
          PRIMARY KEY (`job_key`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    run_sql = """
        CREATE TABLE IF NOT EXISTS `cn_job_run` (
          `id` bigint NOT NULL AUTO_INCREMENT,
          `job_key` varchar(64) NOT NULL,
          `trigger_type` varchar(16) NOT NULL,
          `status` varchar(16) NOT NULL,
          `message` varchar(1000) NULL DEFAULT NULL,
          `started_at` datetime NOT NULL,
          `finished_at` datetime NULL DEFAULT NULL,
          PRIMARY KEY (`id`),
          KEY `idx_job_started` (`job_key`, `started_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with _connect() as conn:
        with conn.cursor() as db:
            db.execute(schedule_sql)
            db.execute(run_sql)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in JOBS:
                db.execute(
                    """INSERT IGNORE INTO `cn_job_schedule`
                       (`job_key`, `enabled`, `run_time`, `weekdays_only`, `updated_at`)
                       VALUES (%s, 0, '17:30', 1, %s)""",
                    (item["key"], now),
                )


def _fmt(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value


def list_schedules():
    ensure_tables()
    with _connect() as conn:
        with conn.cursor() as db:
            db.execute("SELECT job_key, enabled, run_time, weekdays_only, updated_at FROM `cn_job_schedule`")
            rows = db.fetchall()
    by_key = {row["job_key"]: row for row in rows}
    result = []
    for item in JOBS:
        row = by_key.get(item["key"], {})
        result.append({
            "key": item["key"],
            "name": item["name"],
            "desc": item["desc"],
            "enabled": int(row.get("enabled") or 0),
            "run_time": row.get("run_time") or "17:30",
            "weekdays_only": int(row.get("weekdays_only") if row.get("weekdays_only") is not None else 1),
            "updated_at": _fmt(row.get("updated_at")),
        })
    return result


def save_schedules(items):
    ensure_tables()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        with conn.cursor() as db:
            for item in items:
                key = item["job_key"]
                if key not in _JOB_MAP:
                    raise ValueError(f"未知任务：{key}")
                run_time = _normalize_time(item.get("run_time") or "17:30")
                enabled = 1 if item.get("enabled") else 0
                weekdays_only = 1 if item.get("weekdays_only", True) else 0
                db.execute(
                    """INSERT INTO `cn_job_schedule`
                       (`job_key`, `enabled`, `run_time`, `weekdays_only`, `updated_at`)
                       VALUES (%s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                       `enabled`=VALUES(`enabled`),
                       `run_time`=VALUES(`run_time`),
                       `weekdays_only`=VALUES(`weekdays_only`),
                       `updated_at`=VALUES(`updated_at`)""",
                    (key, enabled, run_time, weekdays_only, now),
                )


def _normalize_time(value):
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError("执行时间格式应为 HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("执行时间超出范围")
    return f"{hour:02d}:{minute:02d}"


def recent_runs(limit=30):
    ensure_tables()
    with _connect() as conn:
        with conn.cursor() as db:
            db.execute(
                """SELECT id, job_key, trigger_type, status, message, started_at, finished_at
                   FROM `cn_job_run` ORDER BY id DESC LIMIT %s""",
                (int(limit),),
            )
            rows = db.fetchall()
    for row in rows:
        row["name"] = _JOB_MAP.get(row["job_key"], {}).get("name", row["job_key"])
        row["started_at"] = _fmt(row["started_at"])
        row["finished_at"] = _fmt(row["finished_at"])
    return rows


def latest_by_job():
    runs = recent_runs(200)
    found = {}
    for row in runs:
        found.setdefault(row["job_key"], row)
    return found


def state():
    schedules = list_schedules()
    latest = latest_by_job()
    runs = recent_runs(20)
    with _lock:
        running = _worker_running
        current = None
        if _proc is not None:
            current = getattr(_proc, "job_key", None)
    for item in schedules:
        last = latest.get(item["key"])
        item["last_status"] = last["status"] if last else ""
        item["last_message"] = last["message"] if last else ""
        item["last_started_at"] = last["started_at"] if last else ""
    return {
        "jobs": schedules,
        "runs": runs,
        "running": running,
        "current_job": current,
    }


def enqueue(keys, trigger_type):
    global _worker_running
    if not keys:
        raise ValueError("请至少选择一个任务")
    ordered = [item["key"] for item in JOBS if item["key"] in set(keys)]
    unknown = [key for key in keys if key not in _JOB_MAP]
    if unknown:
        raise ValueError("未知任务：" + ",".join(unknown))
    ensure_tables()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        if _worker_running:
            raise RuntimeError("已有任务正在执行，请等待结束后再拉取")
        with _connect() as conn:
            with conn.cursor() as db:
                for key in ordered:
                    db.execute(
                        """INSERT INTO `cn_job_run`
                           (`job_key`, `trigger_type`, `status`, `message`, `started_at`)
                           VALUES (%s, %s, 'queued', %s, %s)""",
                        (key, trigger_type, "等待执行", now),
                    )
        _worker_running = True
        threading.Thread(target=_worker, name="instock-job", daemon=True).start()
    return ordered


def stop_jobs():
    global _cancel, _proc
    with _lock:
        running = _worker_running
        _cancel = running
        proc = _proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not running:
        _mark_queued_cancelled()


def _mark_queued_cancelled():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        with conn.cursor() as db:
            db.execute(
                """UPDATE `cn_job_run` SET `status`='failed', `message`=%s, `finished_at`=%s
                   WHERE `status`='queued'""",
                ("已取消", now),
            )


def _worker():
    global _worker_running, _cancel
    try:
        while True:
            with _lock:
                if _cancel:
                    _cancel = False
                    _mark_queued_cancelled()
                    break
            row = _next_queued()
            if row is None:
                break
            try:
                _execute(row)
            except Exception as exc:
                finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with _connect() as conn:
                    with conn.cursor() as db:
                        db.execute(
                            """UPDATE `cn_job_run` SET `status`='failed', `message`=%s, `finished_at`=%s
                               WHERE `id`=%s AND `status` IN ('queued', 'running')""",
                            (str(exc)[:1000], finished, row["id"]),
                        )
    finally:
        with _lock:
            _cancel = False
            _worker_running = False
            pending = _next_queued()
            if pending is not None:
                _worker_running = True
                threading.Thread(target=_worker, name="instock-job", daemon=True).start()


def _next_queued():
    with _connect() as conn:
        with conn.cursor() as db:
            db.execute(
                """SELECT id, job_key, trigger_type FROM `cn_job_run`
                   WHERE `status`='queued' ORDER BY id ASC LIMIT 1"""
            )
            return db.fetchone()


def _execute(row):
    global _proc
    job = _JOB_MAP[row["job_key"]]
    script = os.path.join(JOB_DIR, job["script"])
    log_path = os.path.join(LOG_DIR, f"{row['id']}.log")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        with conn.cursor() as db:
            db.execute(
                "UPDATE `cn_job_run` SET `status`='running', `message`=%s, `started_at`=%s WHERE `id`=%s",
                ("正在执行", now, row["id"]),
            )
    env = os.environ.copy()
    project_root = os.path.dirname(INSTOCK_DIR)
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-u", script],
            cwd=JOB_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )
        proc.job_key = row["job_key"]
        with _lock:
            _proc = proc
        code = proc.wait()
        with _lock:
            if _proc is proc:
                _proc = None
            cancelled = _cancel
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tail = _tail(log_path)
    if cancelled:
        status, message = "failed", "已手动停止"
    elif code == 0:
        status, message = "success", "完成"
    else:
        status = "failed"
        message = f"退出码 {code}"
        if tail:
            message = f"{message}：{tail}"
    with _connect() as conn:
        with conn.cursor() as db:
            db.execute(
                "UPDATE `cn_job_run` SET `status`=%s, `message`=%s, `finished_at`=%s WHERE `id`=%s",
                (status, message[:1000], finished, row["id"]),
            )


def _tail(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()[-2000:]
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1][:300]


def _scheduler_loop():
    while True:
        try:
            _scheduler_tick()
        except Exception as exc:
            print(f"自动同步检查失败：{exc}")
        threading.Event().wait(30)


def _scheduler_tick():
    now = datetime.now()
    due = []
    for item in list_schedules():
        if not item["enabled"]:
            continue
        if item["weekdays_only"] and now.weekday() >= 5:
            continue
        hour, minute = [int(part) for part in item["run_time"].split(":")]
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < slot:
            continue
        day_end = datetime(now.year, now.month, now.day) + timedelta(days=1)
        with _connect() as conn:
            with conn.cursor() as db:
                db.execute(
                    """SELECT COUNT(*) AS n FROM `cn_job_run`
                       WHERE `job_key`=%s AND `trigger_type`='auto'
                       AND `started_at` >= %s AND `started_at` < %s""",
                    (item["key"], slot.strftime("%Y-%m-%d %H:%M:%S"), day_end.strftime("%Y-%m-%d %H:%M:%S")),
                )
                count = db.fetchone()["n"]
        if count:
            continue
        due.append(item["key"])
    if not due:
        return
    try:
        enqueue(due, "auto")
    except RuntimeError:
        return


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    ensure_tables()
    threading.Thread(target=_scheduler_loop, name="instock-scheduler", daemon=True).start()
