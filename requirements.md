# QQ 智能体 —— 运行依赖与前置条件

> **本机已全部就绪**，本节保留作为记录与换机参考。实际部署细节见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 本机已确认

| 项 | 状态 |
|---|---|
| Python | 3.12.14（`python`） |
| Python（PATH 上的） | 3.13.14，`python run.py` 直接可用 |
| 标准库 sqlite3 | 3.53.1 可用 |
| 外网 HTTPS | 通。**但 `github.com` 主站超时被墙**，`api.github.com` / `raw.githubusercontent.com` / `gh-proxy.com` 可用 |
| QQ NT | **9.9.33.52230** ✅（原 9.9.20.37625，因 NapCat 要求 build 40768+ 已升级） |
| NapCat | **v4.18.33** 已部署到 `<NAPCAT_DIR>` ✅ |
| 登录账号 | 100000002 ✅ |
| Node.js | 未安装（NapCat 自带依赖，不影响本项目） |
| Docker | 未安装 |
| git | 未安装 |
| 第三方 pip 包 | **不需要**（本项目全部标准库实现） |

## 必须由你自备的两样东西

1. **QQ 协议端（NapCat）** —— 负责真正的 QQ 登录，本项目通过 OneBot v11 WebSocket 与它对接。
   - NapCat 需要宿主 QQ 客户端（NTQQ）。官方仓库与部署文档：https://github.com/NapNeko/NapCatQQ
   - 本机没装 Docker，只能走 Windows 直装版；若你愿意装 Docker Desktop，容器方案更稳。
2. **一个 OpenAI 兼容的大模型 key** —— 默认用 DeepSeek（`https://api.deepseek.com`）。
   - 也可以用任何兼容 `/chat/completions` 的服务（官方 OpenAI、Moonshot、本地 Ollama / vLLM），改 `config.json` 里的 `base_url` 与 `model` 即可。

## 登录方式（唯一正确做法）

启动 NapCat → 终端/日志里出现二维码 → 用你自己的手机 QQ 扫码 → 登录凭证持久化在 NapCat 的 data 目录，之后重启免扫码。

**不要把 QQ 密码写进任何配置文件。** 本项目的代码里根本没有密码字段。

## 默认端口约定

| 组件 | 地址 |
|---|---|
| NapCat OneBot v11 WebSocket 服务端 | `ws://127.0.0.1:3001` |
| 本服务（出站连接方） | 主动连上面那个地址 |

如果 NapCat 里配的是 HTTP 服务端 + 反向 WS，请把 `config.json` 的 `onebot.ws_url` 改到实际地址。
