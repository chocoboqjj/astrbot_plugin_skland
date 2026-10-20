# AstrBot Plugin - 森空岛签到

[![Build and Release](https://github.com/chocoboqjj/astrbot_plugin_skland/actions/workflows/release.yml/badge.svg)](https://github.com/chocoboqjj/astrbot_plugin_skland/actions/workflows/release.yml)

森空岛自动签到插件，支持明日方舟和终末地签到，**同时适配 OneBot(NapCat/LLOneBot) 与 QQ 官方机器人**。

> 本仓库基于 [Azincc/astrbot_plugin_skland](https://github.com/Azincc/astrbot_plugin_skland) 修改，
> 原作者为 [Azincc](https://github.com/Azincc)，遵循 MIT 许可。主要改动见「从 v1.4.0 升级」。

## 功能

- **skd** (群聊):        查看群内所有绑定用户的签到状态
- **skd** (私聊):        查看自己的签到状态（读取最近一次自动签到结果）
- **skdlogin** (私聊):   使用 token 登录并立即签到
- **skdlogout** (私聊):  登出并移除 token
- **skdusers** (全部):   查询用户统计，普通用户仅显示签到人数和名额
- **skdhelp** (全部):    查看帮助
- **skdcheck** (全部):   诊断签到卡片图片是否可用（缺依赖时会给出安装指引）

## 使用

### 获取 Token

1. 登录 [森空岛](https://www.skland.com/)
2. 访问 [森空岛/account/info/hg](https://web-api.skland.com/account/info/hg)
3. 找到返回的 JSON 中的 `{"content":"XXX"}`
4. 复制 `XXX` 部分

### 登录与签到

1. 私聊发送 `/skdlogin XXX` 进行登录
2. 登录成功后会自动执行一次签到
3. 之后可以发送 `/skd` 查看签到状态

> ⚠️ token 等同账号密码，只在私聊中提交。

## 指令写法

| 场景 | 写法 |
|------|------|
| 私聊 | `/skdlogin <token>`、`/skd`、`/skdlogout`、`/skdusers` |
| 群聊 | `/skd` |

> **注意**：AstrBot 的指令默认需要 `/` 前缀。插件额外做了兜底，`skd`、`签到` 等不带斜杠的写法也能识别。

## 平台适配说明（重要）

QQ 官方机器人与 OneBot 的能力差异较大，本插件会**自动识别平台并采用对应策略**，无需手动配置。

| 能力 | OneBot / NapCat | QQ 官方机器人 |
|------|-----------------|----------------|
| 定时私聊推送 | 支持 | **不支持**（官方已取消主动推送） |
| 群内主动播报 | 支持 | 需群主开启「允许主动发言」 |
| 用户 ID | QQ 号 | openid（与 QQ 号不通用） |

### 签到结果如何送达

- **OneBot**：定时任务签到完成后，照旧私发结果给你。
- **QQ 官方**：定时任务**照常执行签到**，但结果只写入数据库；你发送 `/skd` 时读取并展示，并附带签到时间。

这是因为腾讯已于 2025-04-21 取消 QQ 机器人的主动消息推送能力，官方仅保留「响应式交互」——必须用户先发起对话，机器人才能回复。因此 QQ 官方平台无法做到「每天主动私发签到结果」。

### QQ 官方平台使用须知

1. 若要接收全量群消息，需在手机端群设置里把「机器人可获取的群聊消息范围」设为「获取群内全部消息」。
2. 机器人**只能被添加到自己是群主的群聊**。
3. 正式环境需要配置 IP 白名单；沙箱环境最多 20 人。

## 配置项

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `auto_sign_enabled` | `true` | 自动签到总开关 |
| `auto_sign_hour` | `9` | 自动签到执行小时（0-23） |
| `auto_sign_delay` | `10` | 每个用户之间的随机延时上限（秒），规避风控 |
| `max_users` | `10` | 最大绑定用户数，0 表示无限制 |
| `show_player_name` | `true` | 显示森空岛昵称还是平台昵称 |
| `push_mode` | `auto` | `auto` 按平台自动判断 / `private` 强制私聊推送 / `pull` 强制仅落库 |
| `group_broadcast_enabled` | `false` | 自动签到后向群播报汇总，失败自动静默降级 |
| `live_query_in_group` | `false` | 群聊 `/skd` 是否实时请求森空岛 |
| `card_image_enabled` | `false` | `/skd` 以图片卡片输出签到状态 |
| `card_theme` | `dark` | `dark` 深色 / `light` 浅色卡片 |

> 群聊 `/skd` 默认**读取数据库**中的最近一次签到结果，不产生额外请求。开启 `live_query_in_group` 后会为每个群成员实时请求森空岛接口（每人 4~5 次 HTTP），人数多时较慢且请求量大。

### 签到卡片图片

开启 `card_image_enabled` 后，`/skd` 会输出一张签到状态卡片图片（附纯文本备用信息）。

渲染只需要 **Pillow**（已在 `requirements.txt` 中列出，AstrBot 会自动安装）—— 字体已随插件打包，**无需在系统安装中文字体**，各平台渲染效果一致。

字体查找优先级：

1. 插件内置字体（`fonts/` 目录）—— 跨平台效果一致
2. 用户本机安装的中文字体（`~/Library/Fonts`、`~/.fonts` 等）
3. 系统级中文字体（`Hiragino Sans GB`、`NotoSansCJK`、`wqy-zenhei` 等）

> ⚠️ **字体许可提示**
>
> 内置的筑紫A丸（BC筑紫S丸SC Bold）是 **Fontworks 的商业字体**，
> 并非 OFL 等自由许可。随插件对外分发可能违反其授权条款。
>
> 若要公开发布到插件市场，建议把 `fonts/` 里的字体换成
> Noto Sans SC / Sarasa Gothic SC 等开源字体，或直接删除 `fonts/` 目录
> （插件会自动回退到查找系统字体）。详见 `fonts/README.md`。

> 💡 发送 `/skdcheck` 可查看当前使用的字体与渲染自检结果。

> **字体缺失或 Pillow 未安装时，插件会自动回退为纯文本输出**，不会报错也不会影响签到功能。

### 图片没出来？先跑 /skdcheck

如果 `/skd` 返回的还是文字而不是图片，发送 `/skdcheck` 诊断：

```
🔍 签到卡片诊断
═══════════════
图片开关: ✅ 已开启
卡片主题: dark
Pillow: ✅ 已安装 12.3.0
中文字体: ✅ /System/Library/Fonts/Hiragino Sans GB.ttc
渲染测试: ✅ 成功（/tmp/skland_card_xxx.png）
```

它会逐项列出问题并给出对应的安装命令。常见情况：

| 诊断输出 | 处理方式 |
|---------|---------|
| 图片开关 ❌ 未开启 | WebUI →「插件配置」→ 勾选「签到卡片图片」 |
| Pillow ❌ 未安装 | `pip install Pillow`（AstrBot 通常会自动装） |
| 中文字体 ❌ 未找到 | 见上方 Docker 部署说明 |
| 渲染测试 ❌ 失败 | 查看 AstrBot 日志中的 `卡片渲染失败` 报错 |

### 接口日志

所有森空岛接口的请求参数与原始返回都会打在 AstrBot 日志里（`[森空岛]` 前缀），便于排查接口变更或签到失败：

```
[森空岛] -> POST https://as.hypergryph.com/user/oauth2/v2/grant
  请求体: {"appCode": "4ca99fa6b56cc2ba", "token": "ABCD***MNOP", "type": 0}
[森空岛] POST https://as.hypergryph.com/user/oauth2/v2/grant -> 200 (第 1 次尝试)
{"status": 0, "data": {"code": "AUTH***3456"}}
```

**敏感字段会自动脱敏**（保留首尾各 4 位）：用户 token、cred、sign、dId、deviceId、授权码等。接口状态码与业务数据（如奖励列表）完整保留，不会被误遮。

> 响应超过 2000 字符会截断，避免刷屏。

## 从 v1.4.0 升级

- 群记录结构会自动迁移为带平台前缀的格式，无需手动处理
- 原有 token 绑定数据保留，新增 `umo_by_platform` 字段用于多平台共存
- 同一个用户在 OneBot 与 QQ 官方平台各绑定一次后，**任一平台登出不会影响另一平台**
- 新增裸指令兜底：`skd`、`签到` 等无需 `/` 前缀的写法也能识别
- 新增 `game_names` 字段，按游戏分别保存昵称。一个森空岛账号可同时绑定明日方舟和终末地，两者的角色名往往不同，现在会各用各的名字显示（旧数据会在下次签到后自动补齐）
- 新增 `game_awards` 字段，按游戏分别保存奖励，奖励会显示在各自游戏名下（旧数据会自动回退为从文本解析）

## 安装

### 方式一：从 Release 下载

1. 前往 [Releases](https://github.com/chocoboqjj/astrbot_plugin_skland/releases) 页面
2. 下载最新的 `astrbot_plugin_skland-vX.X.X.zip`
3. 解压到 AstrBot 的 `plugins` 目录

### 方式二：使用 Git

```bash
cd /path/to/astrbot/plugins
git clone https://github.com/chocoboqjj/astrbot_plugin_skland.git
```

## 依赖

插件依赖已在 `requirements.txt` 中列出，AstrBot 会自动安装。

## 许可

MIT License