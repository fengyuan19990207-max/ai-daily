#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 情报日报 · 常驻服务
----------------------
在本机 0.0.0.0:8787 起一个只读的 HTTP 服务，电脑和手机（同一 Wi-Fi）都能随时打开。
纯标准库，/usr/bin/python3 直接跑。

路由：
    /                    今天的面板
    /d/YYYY-MM-DD        某天的存档
    /api/latest.json     最新结构化数据
    /api/index.json      所有存档日期
    /feed.xml            今日要闻 RSS（可被 Folo / FreshRSS 等订阅）
    /sources             数据源与可信度说明
    /manifest.webmanifest, /icon-192.png, /icon-512.png, /apple-touch-icon.png
"""

import gzip
import io
import json
import mimetypes
import os
import re
import socket
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "output"
DATA = BASE / "data"
PORT = int(os.environ.get("AI_DAILY_PORT", "8787"))
HOST = os.environ.get("AI_DAILY_HOST", "0.0.0.0")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/rss+xml; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


def lan_ip():
    """优先 Wi-Fi/以太网口；UDP 法在有 Clash 之类 TUN 网卡时会拿到 198.18.x.x 假地址。"""
    import subprocess
    for iface in ("en0", "en1", "en2"):
        try:
            out = subprocess.run(["ipconfig", "getifaddr", iface],
                                 capture_output=True, text=True, timeout=3)
            ip = out.stdout.strip()
            if ip and not ip.startswith(("198.18.", "127.")):
                return ip
        except Exception:  # noqa: BLE001
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    server_version = "AIDaily/1.1"
    protocol_version = "HTTP/1.1"      # 保持连接，手机端少一轮握手
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):  # 静音：日志由 launchd 收集
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 工具 ----------
    def send_bytes(self, body, ctype, code=200, cache="no-cache", etag=None):
        if code >= 400:
            cache = "no-store"
        # 协商缓存：内容没变就回 304，手机上几乎零传输
        if etag and code == 200:
            inm = self.headers.get("If-None-Match")
            if inm and inm.strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 1024:
            body = gzip.compress(body, 6)
            encoding = "gzip"
        else:
            encoding = None
        self.send_response(code)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path: Path, cache="no-cache"):
        if not path.is_file():
            return self.not_found()
        ext = path.suffix.lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        st = path.stat()
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        self.send_bytes(path.read_bytes(), ctype, cache=cache, etag=etag)

    def not_found(self):
        body = ("<!doctype html><meta charset=utf-8>"
                "<body style='background:#0b0e14;color:#e6ebf5;font:16px -apple-system;"
                "padding:60px;text-align:center'>"
                "<h2>404</h2><p style='color:#8b98b0'>没有这一页。</p>"
                "<p><a style='color:#5b8cff' href='/'>回到今天的日报</a></p>").encode()
        self.send_bytes(body, "text/html; charset=utf-8", code=404)

    def latest_date(self):
        files = sorted(DATA.glob("20??-??-??.json"))
        return files[-1].stem if files else datetime.now().strftime("%Y-%m-%d")

    # ---------- 路由 ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/":
            return self.send_file(OUT / "index.html")

        m = re.fullmatch(r"/d/(20\d\d-\d\d-\d\d)", path)
        if m:
            day = m.group(1)
            target = OUT / (day + ".html")
            if not target.is_file():
                return self.not_found()
            return self.send_file(target, cache="max-age=3600")

        if path == "/api/latest.json":
            return self.send_file(DATA / (self.latest_date() + ".json"))

        if path == "/api/index.json":
            days = [p.stem for p in sorted(DATA.glob("20??-??-??.json"))]
            body = json.dumps({"days": days, "latest": days[-1] if days else None},
                              ensure_ascii=False).encode()
            return self.send_bytes(body, "application/json; charset=utf-8")

        if path == "/feed.xml":
            return self.send_file(OUT / "feed.xml", cache="max-age=600")

        if path == "/sources":
            return self.send_file(OUT / "sources.html", cache="max-age=600")

        if path in ("/manifest.webmanifest", "/icon-192.png", "/icon-512.png",
                    "/apple-touch-icon.png", "/favicon.ico"):
            name = "manifest.webmanifest" if path == "/manifest.webmanifest" else path.lstrip("/")
            if path == "/favicon.ico":
                name = "icon-192.png"
            return self.send_file(OUT / name, cache="max-age=86400")

        if path == "/health":
            return self.send_bytes(b'{"ok":true}', "application/json")

        return self.not_found()


def main():
    # 端口被占时给出可读提示
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("端口 %d 起不来：%s" % (PORT, e))
        sys.exit(1)
    srv.daemon_threads = True
    ip = lan_ip()
    print("AI 情报日报服务已启动")
    print("  本机： http://127.0.0.1:%d" % PORT)
    print("  手机： http://%s:%d   （同一 Wi-Fi）" % (ip, PORT))
    print("  RSS ： http://%s:%d/feed.xml" % (ip, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
