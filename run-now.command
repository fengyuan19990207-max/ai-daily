#!/bin/bash
# 立刻抓一次最新数据（服务在跑，刷新页面就能看到）
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
/usr/bin/python3 "$BASE/bin/ai_daily.py"
