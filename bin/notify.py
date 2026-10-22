#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每天一条通知，把「AI 情报日报」送到你眼前。
点通知直接打开面板 —— 不点开也已经在锁屏/通知中心看到今天最重要的三条。

用法：
    python3 bin/notify.py           发一条今天的摘要通知
    python3 bin/notify.py --force   即使今天已发过也再发一次
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
OUT = BASE / "output"
STATE = BASE / "state"
STATE.mkdir(exist_ok=True)

PORTS = (8787,)


def latest_json():
    files = sorted(DATA.glob("20??-??-??.json"))
    return files[-1] if files else None


def is_fresh(path, hours=7):
    try:
        return time.time() - path.stat().st_mtime < hours * 3600
    except Exception:  # noqa: BLE001
        return False


def ensure_data():
    f = latest_json()
    today = datetime.now().strftime("%Y-%m-%d")
    if f and f.stem == today and is_fresh(f):
        return f
    # 数据太旧就地重建，别让通知指向过期内容
    subprocess.run([sys.executable, str(BASE / "bin" / "ai_daily.py"), "--quiet"],
                   cwd=str(BASE), check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return latest_json()


def point_url():
    """服务在跑就用局域网地址，否则退回本地文件。"""
    for port in PORTS:
        try:
            import socket
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return "http://127.0.0.1:%d/" % port
        except OSError:
            continue
    return "file://" + str(OUT / "index.html")


def top_lines(data, n=3):
    out = []
    for h in (data.get("headlines") or [])[:n]:
        t = h.get("title") or ""
        if len(t) > 46:
            t = t[:45] + "…"
        out.append("· " + t)
    return out


def send(title, message, url, subtitle=""):
    tn = "/opt/homebrew/bin/terminal-notifier"
    if Path(tn).exists():
        cmd = [tn, "-title", title, "-message", message, "-open", url,
               "-group", "ai-daily", "-sound", "Glass"]
        if subtitle:
            cmd += ["-subtitle", subtitle]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0, r.stderr.strip()
    # 没装 terminal-notifier 就退回系统通知（点了不会跳转，但照样提醒）
    script = 'display notification "%s" with title "%s"' % (
        message.replace('"', "'"), title.replace('"', "'"))
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode == 0, r.stderr.strip()


def main():
    force = "--force" in sys.argv
    today = datetime.now().strftime("%Y-%m-%d")
    marker = STATE / "last_notify.txt"
    if not force and marker.exists() and marker.read_text().strip() == today:
        print("今天已经提醒过了（--force 可强制）")
        return

    f = ensure_data()
    if not f:
        print("没有可用数据，跳过")
        return
    data = json.loads(f.read_text(encoding="utf-8"))
    heads = data.get("headlines") or []
    if not heads:
        print("今天没有要闻，跳过")
        return

    top = heads[0].get("title", "")
    lines = top_lines(data, 3)
    recent = heads[0].get("published") or ""
    sub = "共 %d 条 · 点开看全部" % len(heads)
    ok, err = send("AI 情报日报", "\n".join(lines), point_url(), sub)

    if ok:
        marker.write_text(today, encoding="utf-8")
        print("已提醒：%s" % top[:50])
    else:
        print("通知失败：%s" % err)


if __name__ == "__main__":
    main()
