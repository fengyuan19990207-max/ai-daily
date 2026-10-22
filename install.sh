#!/bin/bash
# 安装「AI 情报日报」：常驻网页服务 + 定时刷新
#   面板:  http://<本机IP>:8787   （电脑、手机同一 Wi-Fi 都能开）
#   RSS :  http://<本机IP>:8787/feed.xml
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="/usr/bin/python3"
UID_NUM="$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"
echo "==> 项目目录 : $BASE"
echo "==> 解释器   : $PY ($("$PY" -V 2>&1))"
echo "==> 局域网 IP: $LAN_IP"

echo "==> 先生成今天的数据（约 20-60 秒）…"
"$PY" "$BASE/bin/ai_daily.py" --quiet

mkdir -p "$AGENTS"

# ---------- 1. 常驻网页服务 ----------
cat > "$AGENTS/com.dsh.aidaily.serve.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.dsh.aidaily.serve</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$BASE/bin/serve.py</string>
    </array>
    <key>WorkingDirectory</key><string>$BASE</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>$BASE/logs/serve.out.log</string>
    <key>StandardErrorPath</key><string>$BASE/logs/serve.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>AI_DAILY_PORT</key><string>8787</string>
    </dict>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

# ---------- 2. 定时刷新数据（每小时，不弹窗） ----------
cat > "$AGENTS/com.dsh.aidaily.refresh.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.dsh.aidaily.refresh</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$BASE/bin/ai_daily.py</string>
        <string>--quiet</string>
    </array>
    <key>WorkingDirectory</key><string>$BASE</string>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>7200</integer>
    <key>StandardOutPath</key><string>$BASE/logs/refresh.out.log</string>
    <key>StandardErrorPath</key><string>$BASE/logs/refresh.err.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
    <key>ProcessType</key><string>Background</string>
    <key>LowPriorityIO</key><true/>
</dict>
</plist>
EOF

# ---------- 3. 每天 08:00 提醒 ----------
cat > "$AGENTS/com.dsh.aidaily.notify.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.dsh.aidaily.notify</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$BASE/bin/notify.py</string>
    </array>
    <key>WorkingDirectory</key><string>$BASE</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>StandardOutPath</key><string>$BASE/logs/notify.out.log</string>
    <key>StandardErrorPath</key><string>$BASE/logs/notify.err.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

# ---------- 4. 公网隧道（手机用流量也能看） ----------
if [ -x /opt/homebrew/bin/cloudflared ]; then
cat > "$AGENTS/com.dsh.aidaily.tunnel.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.dsh.aidaily.tunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$BASE/bin/tunnel.py</string>
    </array>
    <key>WorkingDirectory</key><string>$BASE</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>15</integer>
    <key>StandardOutPath</key><string>$BASE/logs/tunnel.out.log</string>
    <key>StandardErrorPath</key><string>$BASE/logs/tunnel.err.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF
fi

# 清掉旧的「开机弹窗」任务，避免和常驻服务打架
launchctl bootout "gui/$UID_NUM/com.dsh.aidaily" 2>/dev/null || true
rm -f "$AGENTS/com.dsh.aidaily.plist"

for L in serve refresh notify tunnel; do
  launchctl bootout "gui/$UID_NUM/com.dsh.aidaily.$L" 2>/dev/null || true
done
sleep 1
launchctl bootstrap "gui/$UID_NUM" "$AGENTS/com.dsh.aidaily.serve.plist"
launchctl bootstrap "gui/$UID_NUM" "$AGENTS/com.dsh.aidaily.refresh.plist"
launchctl bootstrap "gui/$UID_NUM" "$AGENTS/com.dsh.aidaily.notify.plist"
if [ -f "$AGENTS/com.dsh.aidaily.tunnel.plist" ]; then
  launchctl bootstrap "gui/$UID_NUM" "$AGENTS/com.dsh.aidaily.tunnel.plist"
fi

sleep 2
echo ""
echo "================ 装好了 ================"
echo " 电脑打开 : http://127.0.0.1:8787"
echo " 手机打开 : http://$LAN_IP:8787   （同一 Wi-Fi）"
echo " 公网地址 : $(cat "$BASE/state/tunnel_url.txt" 2>/dev/null || echo 稍等几秒再看 logs/tunnel.log)"
echo " RSS 订阅 : http://$LAN_IP:8787/feed.xml"
echo ""
echo " 手机加到主屏：Safari 打开上面的地址 → 分享 → 添加到主屏幕"
echo " 数据每小时自动刷新；每天 08:00 弹一次通知提醒"
echo ""
echo " 状态：launchctl print gui/$UID_NUM/com.dsh.aidaily.serve | head -12"
echo " 日志：$BASE/logs/"
echo "========================================"
