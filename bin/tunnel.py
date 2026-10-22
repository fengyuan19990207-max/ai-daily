#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本机面板通过 Cloudflare 隧道开到公网，手机用流量也能看。

- 自动拉起 cloudflared，抓它分配到的 https://xxx.trycloudflare.com 地址
- 地址写进 state/tunnel_url.txt（页面底部会显示），变了就弹通知
- cloudflared 掉了自动重连
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STATE = BASE / "state"
STATE.mkdir(exist_ok=True)
URL_FILE = STATE / "tunnel_url.txt"
LOG = BASE / "logs" / "tunnel.log"
CF = "/opt/homebrew/bin/cloudflared"
PORT = os.environ.get("AI_DAILY_PORT", "8787")

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    print(line, flush=True)


def notify(title, message, url):
    tn = "/opt/homebrew/bin/terminal-notifier"
    if Path(tn).exists():
        r = subprocess.run([tn, "-title", title, "-message", message,
                            "-open", url, "-group", "ai-daily-tunnel"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
    subprocess.run(["osascript", "-e",
                    'display notification "%s" with title "%s"'
                    % (message.replace('"', "'"), title.replace('"', "'"))],
                   check=False)


def run_once():
    """跑一次 cloudflared，返回它分配到的公网地址"""
    proc = subprocess.Popen(
        [CF, "tunnel", "--url", "http://127.0.0.1:%s" % PORT,
         "--no-autoupdate", "--loglevel", "info"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    found = None
    started = time.time()
    try:
        for line in proc.stdout:
            if found is None:
                m = URL_RE.search(line)
                if m:
                    found = m.group(0)
                    old = URL_FILE.read_text().strip() if URL_FILE.exists() else ""
                    URL_FILE.write_text(found + "\n", encoding="utf-8")
                    log("公网地址：%s" % found)
                    if found != old:
                        notify("AI 日报 · 公网地址已就绪",
                               "手机用流量也能打开：\n%s" % found, found)
            # 长时间没拿到地址就重来
            if found is None and time.time() - started > 90:
                log("90 秒没拿到地址，重启隧道")
                proc.terminate()
                return
    except KeyboardInterrupt:
        proc.terminate()
        raise
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


def main():
    if not Path(CF).exists():
        log("没装 cloudflared，先 brew install cloudflared")
        sys.exit(1)
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            log("隧道异常：%s" % e)
        log("5 秒后重连")
        time.sleep(5)


if __name__ == "__main__":
    main()
