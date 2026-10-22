#!/bin/bash
# 停止常驻服务与定时刷新（已生成的文件和存档全部保留）
set -uo pipefail
UID_NUM="$(id -u)"
for L in serve refresh; do
  launchctl bootout "gui/$UID_NUM/com.dsh.aidaily.$L" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.dsh.aidaily.$L.plist"
  echo "已停止：com.dsh.aidaily.$L"
done
rm -f "$HOME/Library/LaunchAgents/com.dsh.aidaily.plist"
echo "日报文件仍在 ~/AI-Daily/output/"
echo "想临时再看一次：cd ~/AI-Daily && /usr/bin/python3 bin/serve.py"
