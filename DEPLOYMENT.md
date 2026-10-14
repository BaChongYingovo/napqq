# 部署记录（本机实际状态）

> 这份文档记录**这台机器上实际装了什么、装在哪、怎么启停**，以及部署过程中踩到的、通用教程里不会写的坑。
> 换机器部署时按这份走，可以避开全部已知问题。

---

## 一、实际部署清单

| 组件 | 版本 / 位置 | 说明 |
|---|---|---|
| Windows | 11 25H2 (build 26200) | |
| QQ NT | **9.9.33.52230** | `C:\Program Files\Tencent\QQNT\QQ.exe` |
| NapCat | **v4.18.33** | `<NAPCAT_DIR>\` |
| NapCat 启动器 | `<NAPCAT_DIR>\launch-agent.cmd` | 自建的修正版（原因见第四节） |
| NapCat 后台运行器 | `<NAPCAT_DIR>\run-detached.cmd` | 输出写入 `napcat-console.log` |
| 安装包留存 | `<NAPCAT_DIR>\downloads\` | NapCat.Shell.v4.18.33.zip、QQ_9.9.33_x64.exe |
| 智能体 | `<WORKSPACE>\napqq\` | |
| 智能体自启项 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\QQAgent.cmd` | 删除此文件即关闭开机自启 |
| 登录账号 | **100000002**（昵称「，」） | |
| OneBot WebSocket | `ws://127.0.0.1:3001`（仅本机） | 配置在 `<NAPCAT_DIR>\config\onebot11.json` |
| NapCat WebUI | `http://127.0.0.1:6099/webui` | **token 不在文档里明文写**，用下面的命令读 |

**读 WebUI token（不要把它写进任何文档或截图）：**

```powershell
$py = "python"
& $py -c "import json;d=json.load(open(r'<NAPCAT_DIR>\config\webui.json',encoding='utf-8'));print(f\"http://127.0.0.1:{d['port']}/webui?token={d['token']}\")"
```

> 为什么改成这样：token 等于 NapCat 的管理权限，之前它被明文写在这份文档里。
> 文档将来可能被分享、截图、或提交到仓库——**凭据不该出现在文档里**。
> 另外每次重新扫码登录后 NapCat 会重新生成 token，写死也会过期。
| 单实例锁 | `127.0.0.1:39997` | 防止两个实例重复回复 |

---

## 二、启动 / 停止

### NapCat（协议端，必须先起）

```powershell
# 启动（会弹 UAC，必须管理员）
cmd /c "<NAPCAT_DIR>\launch-agent.cmd"

# 后台静默启动
Start-Process cmd -ArgumentList '/c','"<NAPCAT_DIR>\run-detached.cmd"' -Verb RunAs -WindowStyle Hidden

# 停止
<NAPCAT_DIR>\KillQQ.bat

# 看日志
Get-Content <NAPCAT_DIR>\napcat-console.log -Tail 30
```

### 智能体

```powershell
cd <WORKSPACE>\napqq

python run.py --check      # 自检：配置 + 大模型 + OneBot
python run.py --dry-run    # 干跑：连真实 QQ 但用假模型回复，验证链路不烧 token
python run.py              # 正式运行
start.cmd                  # 双击：先自检再启动
```

**顺序不能反**：先 NapCat（3001 端口监听），再智能体。反了的话智能体会自动重连，通常也能自愈，但日志里会有一段连接失败。

---

## 三、部署过程中确认的关键事实

1. **GitHub 主站在本机被墙**：`github.com` 连接超时；但 `api.github.com`、`raw.githubusercontent.com` 可访问。
   → 下载 NapCat 必须走代理，本次用的是 `https://gh-proxy.com/https://github.com/...`。
2. **腾讯官方 CDN 可访问**：`qqdl.gtimg.cn` 正常，QQ 安装包直连下载即可。
3. **QQ 安装包签名有效**：`Get-AuthenticodeSignature` 显示 Valid，签名者 `Tencent Technology (Shenzhen) Company Limited`（DigiCert 签发）。
4. **QQ 静默安装参数是 `/s`**（小写，来自 winget 官方清单里发布者声明的参数）。
5. **升级 QQ 会清掉注册表卸载项**：升级后 `HKLM\SOFTWARE\WOW6432Node\...\Uninstall\QQ` **消失**（详见第四节）。
6. **localhost 端口有限制**：NapCat WebUI 默认监听 `::`(IPv6 any) + 6099。

---

## 四、必须知道的四个坑（通用教程不会写）

### 坑 1：官方 `launcher.bat` 在升级 QQ 后会失效

`launcher.bat` 从注册表读 QQ 路径：

```bat
reg query "HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ" /v "UninstallString"
```

QQ 从 9.9.20 升到 9.9.33 后，这个键**不存在了**（连 `HKLM\SOFTWARE\Microsoft\...\Uninstall\QQ` 也没有）。结果是 `QQPath` 为空 → 报 `provided QQ path is invalid` → 卡住。

**解决**：用自建的 `launch-agent.cmd`，它按顺序探测下列路径，最后才回退到注册表：

```
C:\Program Files\Tencent\QQNT\QQ.exe
C:\Program Files (x86)\Tencent\QQNT\QQ.exe
%LOCALAPPDATA%\Programs\Tencent\QQNT\QQ.exe
D:\Program Files\Tencent\QQNT\QQ.exe
```

以后再升级 QQ 也不会再踩这个坑。

### 坑 2：NapCat 与 QQ 版本强绑定，且没有退路

NapCat v4.18.33 要求 **QQ build 40768+**。查了近 25 个 release，**从 2026-04 的 v4.18.0 起全部要求 40768+**（对应 QQ 9.9.33）。本机原来是 9.9.20.37625（build 37625），**太旧，装旧版 NapCat 也没用**。

→ 只能升级 QQ。QQ 安装包：`https://qqdl.gtimg.cn/qqfile/QQNT/9.9.33/release/497e2f1f/QQ_9.9.33_260813_x64_01.exe`

升级后 `QQ.exe` 的 FileVersion 从 `9.9.20.37625` 变为 `9.9.33.52230`。注意 QQ NT 是「启动器 + versions 目录」结构，真实版本在 `C:\Program Files\Tencent\QQNT\versions\9.9.33-52230\`。

### 坑 3：NapCat 需要独占 QQ 进程

NapCat 会自己拉起一个 QQ 进程并注入 DLL。如果此时你**已经打开了平时用的 QQ 客户端**，两个实例会抢同一份用户数据，NapCat 可能直接退出。

→ 跑 NapCat 时不要同时开普通 QQ 客户端。启动前先把 QQ 进程全部关掉：

```powershell
Get-Process QQ -ErrorAction SilentlyContinue | Stop-Process -Force
```

**实测**：第一次启动 NapCat 时因为 QQ 客户端在运行 + 控制台窗口被关掉，NapCat 直接退出了。改成后台隐藏运行 + 先杀 QQ 之后才稳定。

### 坑 4：把长驻进程当成「前台窗口」会被误关

第一次是用可见的 cmd 窗口启动的，窗口一关 NapCat 就死了（二维码 82 秒没刷新，进程消失）。

→ 用 `run-detached.cmd` 把输出重定向到 `napcat-console.log` 后台运行，这样关窗口不影响，出问题还能翻日志。

---

## 五、安全与运维注意事项

### 已修复的权限漏洞

原代码在 `owner_qq = 0` 时，判断写成 `if owner and sender_id != owner`——`owner` 为 0（假值）会让**条件整个短路，任何人都能发 `/pause`、`/reset`、`/persona` 控制机器人**。

已改为：**未配置 owner 时管理指令整体关闭**（`agent/bot.py`）。

### 当前配置的取舍

| 配置 | 当前值 | 影响 |
|---|---|---|
| `behaviour.owner_qq` | `0` | 管理指令关闭，**任何人都无法远程叫停机器人** |
| `behaviour.private_reply` | `true` | 任何人的私聊都会回 |
| `behaviour.group_reply_mode` | `mention` | 群里只在被 @ 时回 |
| `behaviour.active_hours` | `"09:00-24:00"` | **夜间静默**：时段外只记录不回复，消掉「7x24 在线」这个最硬的机器特征 |
| `vision.enabled` | `false` | 图片看不懂（`deepseek-chat` 不支持视觉） |
| `memory.enabled` | `true` | 超过 40 条消息自动压缩成长期记忆 |

**想让群里每条都回**：把 `group_reply_mode` 改成 `all`。但这是**被封号的头号诱因**，不建议。

**想恢复管理指令**：把 `behaviour.owner_qq` 改成你自己的 QQ 号即可（重启生效）。

### API key

`config.json` 里的 `llm.api_key` 是**明文**。分享这个文件夹之前先删掉或换 key。
也可以用环境变量 `DEEPSEEK_API_KEY` 代替（代码优先读环境变量）。

### 单实例保护

`run.py` 启动时会占用 `127.0.0.1:39997` 作为单实例锁。重复启动会被拒绝并返回退出码 3：

```
[x] 已经有一个实例在运行（单实例锁 127.0.0.1:39997 被占用）。
    同时跑两个会让每条消息被回复两次。
```

进程被强杀时端口由系统自动释放，不会留下陈旧锁文件。

---

## 六、故障排查

| 现象 | 检查 |
|---|---|
| 智能体报 OneBot 连接失败 | NapCat 是否在跑（`netstat -ano \| findstr :3001`），是否已登录 |
| 二维码不刷新、进程消失 | 看 `<NAPCAT_DIR>\napcat-console.log` 尾部；确认没有普通 QQ 客户端在抢 |
| 机器人不回消息 | 看 `napqq\data\agent.log`；**先确认是不是到了静默时段**（日志会写「静默时段(09:00-24:00)，仅记录 N 条不回复」）；群聊要 @ 它；私聊看是否命中黑名单 |
| 以为设了作息但其实是全天在线 | `--check` 会报 `active_hours 格式错误` 并拒绝启动。格式对了才会跑 |
| 每条消息被回两次 | 有两个实例在跑，检查 39997 和 python 进程 |
| 模型报 400 / 不支持图片 | 开了 `vision.enabled` 但 `deepseek-chat` 不支持视觉，关掉它 |
| 升级 QQ 后 NapCat 起不来 | 用 `launch-agent.cmd` 而不是官方 `launcher.bat`（注册表键会丢） |
