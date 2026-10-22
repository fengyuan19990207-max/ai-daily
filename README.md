# AI 情报日报 · 常驻版

一个装在本机的常驻网页：电脑、手机（同一 Wi-Fi）随时打开，看的都是同一份
「全网 AI 社区榜单 + 最新消息 + 最强工具与方案」。每条都直链原文，可溯源，可深挖。

```
电脑    http://127.0.0.1:8787
手机    http://192.168.31.189:8787      ← 同一 Wi-Fi
公网    https://<随机>.trycloudflare.com   ← 用手机流量也能开（见页面底部「公网」）
RSS     http://192.168.31.189:8787/feed.xml
源说明  http://192.168.31.189:8787/sources
```

---

## 一、怎么看

| 场景 | 做法 |
| --- | --- |
| 电脑随时看 | 浏览器打开 `http://127.0.0.1:8787`，收藏成书签 |
| 手机随时看 | 同一 Wi-Fi 下打开 `http://192.168.31.189:8787` |
| 手机上像 App | Safari 打开 → 分享 → **添加到主屏幕**，桌面会多一个「AI 日报」图标，点开全屏无地址栏（PWA） |
| 用阅读器看 | 把 `/feed.xml` 丢进 Folo、FreshRSS、NetNewsWire 等，日报以 RSS 形式出现 |
| 看历史 | `http://…/d/2026-10-22`，每天一份存档 |
| 手机用流量在外面 | 用 Cloudflare 隧道给的 https 地址，见页面底部或 `state/tunnel_url.txt` |

数据**每小时自动更新一次**（launchd 定时任务，静默，不弹窗）。页面不自动刷新，
想看最新的按一下刷新即可——内容随时都在，不需要"每天等它生成"。

---

## 二、页面里有什么

| 板块 | 内容 | 来源 |
| --- | --- | --- |
| 今日要闻 | 近 36 小时被最多社区同时推的 AI 消息 | 全部源合并加权 |
| 模型战力榜 | 智能指数总榜 / 性价比榜 / 近 45 天新模型 | Artificial Analysis |
| 社区热榜 | HN 今日 AI 热帖（带分数、评论数）、Reddit 五个社区日榜 | Hacker News / Reddit |
| 全网热榜 | 百度热搜、头条热榜、B站热门、V2EX、掘金，各取 Top 8，AI 相关自动打标 | 各平台公开接口 |
| 开源与新品 | GitHub 今日趋势（含今日新增 star）、HF 趋势模型、Product Hunt 新品 | GitHub / Hugging Face / Product Hunt |
| 工具与方案 | 近 30 天新上架模型 + 输入输出价格 + 上下文长度 + 8 个常看榜单入口 | OpenRouter |
| 中文前沿 | 量子位、IT之家、中文快讯 | 量子位 / IT之家 / Google News 中文 |
| 论文速览 | arXiv cs.AI、cs.CL 当日关键词加权精选 | arXiv |

### 外文怎么看

外文标题会**自动翻成中文**，原文以小字压在原标题下面——既看得懂，又能核对原文，
点标题永远是进原文页面。

- 公司/产品/模型名（OpenAI、Claude、DeepSeek-V4.1、Kitten-TTS…）**保留英文原样**，不会被翻成"克劳德""小猫"
- 译文**永久缓存**，同一条只翻一次，不重复消耗额度
- 用的是免费机翻通道（Google → MyMemory 自动切换），**看得懂但不是文学级**

> 想要"术语统一、语句通顺"的专业级翻译，把 `config.json` 里 `translator` 填上你的
> DeepSeek / OpenRouter key 即可自动切换成大模型翻译（一次几十条标题，成本可忽略）：
> ```json
> "translator": {"provider": "deepseek", "api_key": "sk-...", "model": "deepseek-chat"}
> ```

### 为什么可信

1. **不转述**。每条标题就是**原文链接**，点开是一手页面；HN 条目另给讨论帖链接。
2. **可溯源**。`/sources` 列出每个源的权重、今日条数、抓取状态，失败标红。
3. **跨源共振**。同一件事被多个独立源同时报道才标「共振」并上浮——比单一榜单更硬的信号。
4. **噪音过滤**。短关键词（ai / llm / rag）整词匹配，`mailing`、`said` 不会被误判成 AI；
   财经行情、活动邀请函类内容进不了要闻位。
5. **单源失败不影响整体**。每源独立超时、独立降级，底部标出哪个源挂了。

---

## 三、怎么保证你不会忘（重要）

一个要你主动打开的东西，三天后基本就死了。所以做了三层：

1. **每天 08:00 和 20:30 弹一次系统通知** —— 带今天最重要的 3 条标题，不用打开就知道发生了什么
2. **通知点一下就进面板**（需要给 terminal-notifier 通知权限，见下）
3. **Dock 里常驻一个图标** —— Safari 打开面板 → 菜单栏「文件」→ **添加到程序坞**，
   会生成一个真正的 App 窗口（独立、无地址栏），每天在程序坞里杵着，想不起来都难

首次授权：系统设置 → 通知 → 找到 `terminal-notifier` → 允许。
没授权也能弹，只是点了不会跳转（会走系统通知兜底）。

## 四、为什么在手机上不卡

| 之前的问题 | 现在 |
| --- | --- |
| 标题文字超出一屏被**裁掉**（弹性布局没设 `min-width:0`） | 已修，390px 实测无横向溢出 |
| 每条请求都重新下载整页 | gzip 压缩：**69KB → 18.5KB** |
| 重复打开还是全量下载 | ETag 协商缓存：内容没变回 **304，0 字节** |
| HTTP/1.0，每个资源重开一次连接 | HTTP/1.1 长连接 |
| 整页 7000px 一次渲染完 | `content-visibility` 只渲染可视区，滚动时才画下面 |
| 固定渐变背景拖累滚动 | 改成顶部一条静态渐变 |

首屏只剩 1 个请求、18.5KB，之后再打开基本是 0 字节。

## 五、手机加入主屏（做成 App 图标）

1. 手机连上和 Mac 同一个 Wi-Fi
2. Safari 打开 `http://192.168.31.189:8787`
3. 点底部「分享」→「添加到主屏幕」→ 命名「AI 日报」
4. 之后像 App 一样点开，全屏显示

> 图标已内置（512/192/180），PWA manifest 已配好，不用再装任何东西。

---

## 六、自动行为

装了两个 launchd 用户级任务：

| 任务 | 作用 |
| --- | --- |
| `com.dsh.aidaily.serve` | 常驻 HTTP 服务，端口 8787，挂了自动拉起 |
| `com.dsh.aidaily.refresh` | 每小时抓一次，静默更新数据 |
| `com.dsh.aidaily.notify` | 每天 08:00 / 20:30 弹通知提醒 |

```bash
launchctl print gui/$(id -u)/com.dsh.aidaily.serve | head -12
launchctl print gui/$(id -u)/com.dsh.aidaily.refresh | head -12
bash run-now.command      # 立刻抓一次（不打开浏览器）
tail -f logs/run.log      # 看抓取结果
bash uninstall.sh         # 全部停掉（文件保留）
```

---

## 七、改哪里

全部在 `config.json`，改完下一次刷新生效：

- `feeds`：RSS/Atom 源（`weight` 越大越容易上要闻）
- `reddit_subs`：关注的 subreddit
- `keywords_ai`：AI 相关判定词（中英）
- `headline_hours` / `max_headlines`：要闻时间窗与条数
- `sheet`：各板块显示条数（`hot_count` 控制每个热榜平台显示几条）
- `brands`：**必须保留英文**的词表，翻译时用占位符保护，可自行增删
- `translator`：留空=免费机翻；填 key=大模型翻译

改了代码或配置后重启服务：

```bash
launchctl kickstart -k gui/$(id -u)/com.dsh.aidaily.serve
```

---

## 八、手机用流量（公网访问）

已接好 Cloudflare 隧道：`com.dsh.aidaily.tunnel` 常驻跑 `cloudflared`，把本机 8787 端口
映射成一个 https 公网地址。手机在 4G/公司网络都能打开，实测首屏 1-2 秒。

- 当前地址：页面底部「公网」那一行，或 `cat state/tunnel_url.txt`，或 `http://127.0.0.1:8787/api/tunnel`
- **地址会变**：Cloudflare 的临时隧道每次重启（重启电脑、换网）都会分配新地址。
  变了会弹通知告诉你新地址，页面底部也会实时更新
- 需要固定地址：用自己的域名 + 命名隧道（域名 ¥30-60/年），或改用 Tailscale（免费、名字稳定）
- 前提：Mac 醒着且联网；合盖睡眠时公网地址也会失效

## 九、不在同一 Wi-Fi 时（旧说明）

当前只监听局域网（更安全，不外露）。要在外面也能看，二选一：

1. **Tailscale（推荐）**：两端装好后，手机任何网络都能用 `http://<Mac的Tailscale IP>:8787`，
   不用端口映射、不暴露公网。
2. Cloudflare Tunnel：需要一个域名，适合长期对外。

两种都要额外装东西，所以没默认开启——要哪个说一声。

---

## 九、从"自己用"到"能收费"

代码已经按可进化来放，不是一次性脚本：

- **本机版**（现在）：launchd 跑抓取 + 常驻服务 + 通知，零成本
- **能发给朋友**：`.github/workflows/build.yml` 已配好，每小时在 GitHub 上构建一次，
  产物是纯静态站点（268KB，无服务端依赖），丢到 Cloudflare Pages 就有公网 https 链接
- **能收费**：付费点不在"看新闻"，在**按人订阅**（各自选源和主题）、推送、历史检索、导出

商业化的硬门槛先记在这，别到时候被卡：$99/年开发者账号、iOS 审核指南 4.2
（纯网页套壳会被拒，必须有推送/离线/订阅这类原生价值）、中国区上架需要 ICP 备案。

## 十、结构

```
~/AI-Daily
├── bin/ai_daily.py     采集 + 排序 + 渲染（纯标准库，单文件）
├── bin/serve.py        常驻 HTTP 服务（端口 8787）
├── bin/notify.py       每天一条通知提醒
├── .github/workflows/  云端每小时构建（可接 Cloudflare Pages）
├── config.json         所有可调参数
├── output/             index.html / 每日存档 / feed.xml / sources.html / 图标
├── data/               每日结构化 JSON
├── logs/               run.log、serve 与 refresh 日志
├── state/              Reddit 缓存等
├── install.sh          安装两个 launchd 任务
├── uninstall.sh        停止全部
└── run-now.command     手动抓一次
```

> 项目放在 `~/AI-Daily`（主目录）而不是 `~/Documents`：macOS 会阻止 launchd 进程读取
> 受保护目录，放 Documents 里后台任务会报 `Operation not permitted`。
> `~/Documents/AI-Daily` 是指向这里的符号链接，方便查找。
