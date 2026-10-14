# NOTICE — 第三方依赖与使用限制

本项目作者：**八重櫻**（Copyright © 2026）

## 1. 运行依赖：NapCat（**禁止商业用途**）

本项目**不包含也不分发** NapCat 的代码或数据，但运行时需要它作为 QQ 协议端。

NapCat 使用自定义的 *Limited Redistribution License for NapCat*（Copyright © 2024 Mlikiowa），
其中第 3 条明确写着：

> **Non-Commercial Use: This code is not to be used for any commercial purposes.**

因此：

- **你不可以把「本项目 + NapCat」用于任何商业用途。**
- 本项目的 MIT 许可证只覆盖本仓库作者编写的代码，**不能覆盖 NapCat**，
  也不能授权你绕过 NapCat 的限制。
- 获取 NapCat：https://github.com/NapNeko/NapCatQQ （请自行阅读其许可）

## 2. QQ 客户端（腾讯专有软件）

NapCat 的工作方式是**注入腾讯官方 QQ 客户端**。请注意：

- QQ 客户端是腾讯的专有软件，其许可协议禁止逆向、修改、注入、绕过技术保护措施。
- 腾讯不提供面向第三方的官方消息 API；使用协议端属于灰色地带。
- 可能的后果包括**账号被封禁**，以及涉及平台协议/著作权层面的风险。
- **这一层风险不会因为 NapCat 作者授权你使用而消失。**

**本项目仅供个人学习与自用。请自行评估风险，不要用于对外经营。**

## 3. QQ 表情映射表（本地生成，不含在仓库内）

`agent/qq_faces.json` **不在本仓库中**。它由使用者自己运行：

```
python tools/gen_face_table.py
```

从**自己本机安装的 NapCat** 中读取生成，因此仓库里不含任何 NapCat 衍生数据。

未生成该文件时程序仍可正常运行，只是 QQ 系统表情会显示为 `[表情#id]` 而不是 `[表情:微笑]`。

## 4. 大模型 API

默认使用 DeepSeek（https://api.deepseek.com）。商用请自行确认该服务的条款。
也可以换成任何兼容 OpenAI `/chat/completions` 的服务。

## 5. 与 NapCat / 腾讯的关系

**本项目是第三方独立项目，与 NapCat 项目及其作者、与腾讯公司均无隶属关系，
未获其授权、赞助或认可。**

- 本项目不包含 NapCat 的代码或数据，也不分发 NapCat；使用者需自行获取并遵守其许可。
- 本项目与 NapCat 之间只是**协议依赖**（OneBot v11），不是插件、不是分支、不是衍生作品。
- **任何实现 OneBot v11 的协议端都可以替换 NapCat**（改 config.json 的 ws_url 即可）。
- 「napqq」这个名称只是项目自身的命名，不代表与 NapCat 有任何官方关联。
- 本项目不主张任何与 NapCat 或腾讯相关的商标权。

