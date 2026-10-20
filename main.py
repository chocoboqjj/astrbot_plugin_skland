"""
AstrBot Plugin - 森空岛签到 (Skland Sign-In)

Commands:
- skd (group): Show sign-in status for all bound users in the group
- skd (private): Show user's own sign-in status
- skdlogin (all): Login with token and immediately sign in
    群聊使用时会自动撤回含 token 的那条消息
- skdlogout (all): Logout and remove token
- skdusers (all): Show users and stats
- skdhelp (all): Show help
- skdcheck (all): Diagnose sign-in card image availability

平台适配:
    同时适配 OneBot(aiocqhttp) 与 QQ 官方机器人(qq_official)。
    QQ 官方机器人已于 2025-04-21 取消主动消息推送能力，在该平台上自动
    降级为「拉取模型」：定时任务仍执行签到，但结果只落库，用户主动查询
    时读取展示。OneBot 平台保持原有的定时私聊推送。

Config (AstrBot plugin config):
- auto_sign_enabled: 自动签到开关
- auto_sign_hour: 自动签到时间（小时，0-23）
- show_player_name: 显示玩家昵称（否则显示平台昵称）
- auto_sign_delay: 签到延时
- max_users: 最大用户数量
- push_mode: 推送模式（auto/private/pull）
- group_broadcast_enabled: 自动签到后向群播报汇总
- live_query_in_group: 群聊 /skd 是否实时查询
"""

import asyncio
import random
import re
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.config import put_config

from .skland_api import SklandAPI
from .skland_card import CardRenderer

PLUGIN_NAME = "astrbot_plugin_skland"

# 平台标识
PLATFORM_ONEBOT = "onebot"
PLATFORM_QQOFFICIAL = "qq_official"
PLATFORM_OTHER = "other"

# 各平台主动推送能力（由平台规则决定，详见 README）
# need_at_in_group 记录该平台群聊是否需要 @ 才能触发指令，作为平台差异的
# 事实描述保留；指令提示文案统一不在输出中展示该差异。
PLATFORM_CAPABILITY = {
    PLATFORM_ONEBOT: {
        "can_push_private": True,   # 支持定时私聊推送
        "can_push_group": True,     # 支持群内主动播报
        "need_at_in_group": False,  # 群聊无需 @ 即可触发指令
    },
    PLATFORM_QQOFFICIAL: {
        "can_push_private": False,  # 官方已取消主动推送
        "can_push_group": True,     # 群主开启开关后有限频能力
        "need_at_in_group": True,   # 群聊需 @ 机器人
    },
    PLATFORM_OTHER: {
        "can_push_private": False,
        "can_push_group": False,
        "need_at_in_group": False,
    },
}


@register(PLUGIN_NAME, "AstrBot", "森空岛自动签到插件（支持 OneBot 与 QQ 官方机器人）", "1.5.0")
class SklandPlugin(Star):
    """森空岛签到插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api = SklandAPI(max_retries=3)
        self.scheduler = AsyncIOScheduler()
        self._card = CardRenderer(theme=self.config.get("card_theme", "dark"))
        self._init_config()
    # ==================== Config ====================

    def _init_config(self):
        """注册后台配置项（默认值与 _conf_schema.json 保持一致）"""
        defaults = [
            ("自动签到开关", "auto_sign_enabled", True,
             "开启后，将在指定时间自动为所有已注册用户签到"),
            ("自动签到时间（小时）", "auto_sign_hour", 9,
             "自动签到执行的小时（0-23），默认早上9点"),
            ("显示玩家名称", "show_player_name", True,
             "开启后显示森空岛昵称，否则显示平台昵称"),
            ("自动签到的延迟", "auto_sign_delay", 10,
             "签到时向后随机延迟的秒数上限，用于规避风控"),
            ("最大用户数", "max_users", 10,
             "允许绑定的最大用户数量，0 表示无限制"),
            ("推送模式", "push_mode", "auto",
             "auto=按平台自动判断；private=强制私聊推送；pull=强制仅落库不推送"),
            ("自动签到群播报", "group_broadcast_enabled", False,
             "自动签到完成后向已有群播报一次汇总（需群主开启主动发言）"),
            ("群聊实时查询", "live_query_in_group", False,
             "开启后群聊 /skd 实时请求森空岛，人数多时较慢且请求量大"),
            ("签到卡片图片", "card_image_enabled", False,
             "开启后 /skd 以图片卡片输出签到状态；缺少 Pillow 或中文字体时自动回退为文本"),
            ("卡片主题", "card_theme", "dark",
             "dark=深色 / light=浅色，仅在启用图片卡片时生效"),
        ]
        for name, key, value, description in defaults:
            put_config(
                namespace=PLUGIN_NAME,
                name=name,
                key=key,
                value=value,
                description=description,
            )

    def _get_config(self) -> dict:
        """获取当前配置"""
        return {
            "auto_sign_enabled": self.config.get("auto_sign_enabled", True),
            "auto_sign_hour": self.config.get("auto_sign_hour", 9),
            "show_player_name": self.config.get("show_player_name", True),
            "auto_sign_delay": self.config.get("auto_sign_delay", 10),
            "max_users": self.config.get("max_users", 10),
            "push_mode": self.config.get("push_mode", "auto"),
            "group_broadcast_enabled": self.config.get("group_broadcast_enabled", False),
            "live_query_in_group": self.config.get("live_query_in_group", False),
            "card_image_enabled": self.config.get("card_image_enabled", False),
            "card_theme": self.config.get("card_theme", "dark"),
        }

    # ==================== Platform Capability ====================

    @staticmethod
    def _detect_platform(event: AstrMessageEvent) -> str:
        """
        探测事件来源平台。

        优先检查原始消息对象的类型来源（不受用户自定义平台 ID 影响），
        回退到平台名字符串匹配。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        module = (type(raw).__module__ if raw is not None else "").lower()
        clsname = (type(raw).__name__ if raw is not None else "").lower()

        if "botpy" in module or "botpy" in clsname:
            return PLATFORM_QQOFFICIAL

        try:
            name = (event.get_platform_name() or "").lower()
        except Exception:
            name = ""

        if "qq_official" in name or "qqofficial" in name:
            return PLATFORM_QQOFFICIAL
        if "aiocqhttp" in name or "onebot" in name:
            return PLATFORM_ONEBOT
        return PLATFORM_OTHER

    @staticmethod
    def _capability(platform: str) -> dict:
        return PLATFORM_CAPABILITY.get(platform, PLATFORM_CAPABILITY[PLATFORM_OTHER])

    def _can_push(self, platform: str) -> bool:
        """根据配置与平台能力判断是否允许主动推送私聊"""
        mode = self._get_config().get("push_mode", "auto")
        if mode == "pull":
            return False
        if mode == "private":
            return True
        return self._capability(platform).get("can_push_private", False)

    @staticmethod
    def _platform_from_umo(umo: str) -> str:
        """从 unified_msg_origin 前缀推断平台"""
        head = umo.split(":", 1)[0].lower() if umo else ""
        if "qq_official" in head or "qqofficial" in head:
            return PLATFORM_QQOFFICIAL
        if "aiocqhttp" in head or "onebot" in head:
            return PLATFORM_ONEBOT
        return PLATFORM_OTHER

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        """判断是否为群聊消息"""
        return bool(getattr(event.message_obj, "group_id", None))

    @staticmethod
    def _group_key(platform: str, group_id: str) -> str:
        """群记录按平台隔离，避免 openid 与 QQ 群号冲突"""
        return f"{platform}:{group_id}"

    # ==================== Storage Helpers ====================

    @staticmethod
    def _resolve_user_key(users: dict, user_id: str) -> str:
        """解析规范用户键；同一用户跨平台(openid/QQ号)通过 alias 归一"""
        seen = set()
        current = user_id
        while current in users and current not in seen:
            seen.add(current)
            alias = (users[current].get("alias") or {}).get("uid")
            if not alias or alias not in users:
                break
            current = alias
        return current if current in users else user_id

    async def _migrate_storage(self) -> None:
        """把旧版 groups 结构（group_id -> [user_id]）迁移为带平台前缀的新版"""
        groups = await self.get_kv_data("groups", {})
        if not groups:
            return
        migrated = {}
        changed = False
        for key, value in groups.items():
            if isinstance(value, list):
                migrated[f"{PLATFORM_ONEBOT}:{key}"] = {
                    "members": value,
                    "umo": "",
                    "platform": PLATFORM_ONEBOT,
                }
                changed = True
            else:
                migrated[key] = value
        if changed:
            await self.put_kv_data("groups", migrated)
            logger.info("森空岛插件：群记录结构已迁移到新版格式")

    # ==================== Lifecycle ====================

    async def initialize(self):
        """插件初始化"""
        logger.info("森空岛签到插件已加载")
        await self._migrate_storage()
        config = self._get_config()
        if config.get("auto_sign_enabled", False):
            self._start_auto_sign_job(config.get("auto_sign_hour", 9))
        if not self.scheduler.running:
            self.scheduler.start()

    async def terminate(self):
        """插件卸载"""
        if self.scheduler.running:
            self.scheduler.shutdown()
        await self.api.close()
        logger.info("森空岛签到插件已卸载")

    # ==================== Auto Sign-In ====================

    def _start_auto_sign_job(self, hour: int = 9):
        """启动自动签到定时任务"""
        hour = max(0, min(23, int(hour)))
        trigger = CronTrigger(hour=hour, minute=0)
        try:
            self.scheduler.remove_job("skland_auto_sign")
        except Exception:
            pass

        self.scheduler.add_job(
            self._auto_sign_all_users,
            trigger=trigger,
            id="skland_auto_sign",
            misfire_grace_time=3600,
        )
        logger.info(f"森空岛自动签到任务已启动，每天 {hour:02d}:00 执行")

    @staticmethod
    def _mark_signed(user_data: dict, results: list, today: str) -> None:
        """把签到成功（含「今日已签到」）的游戏写回用户数据"""
        last_sign = user_data.get("last_sign") or {}
        for r in results:
            if not SklandPlugin._is_signed_today_static(r):
                continue
            slot = "arknights" if r.game == "明日方舟" else (
                "endfield" if r.game == "终末地" else None
            )
            if slot:
                last_sign[slot] = today
        user_data["last_sign"] = last_sign

    @staticmethod
    def _store_game_names(user_data: dict, results: list) -> dict:
        """
        按游戏分别记录昵称。

        森空岛的一个账号可以同时绑定明日方舟和终末地，两者的角色名
        往往不同。旧逻辑只保存 bindings[0].nickname（恒为方舟昵称），
        这里改为按游戏分别存储，供展示时各用各的名字。
        """
        names = user_data.get("game_names") or {}
        for r in results:
            if not r.nickname:
                continue
            if r.game == "明日方舟":
                names.setdefault("arknights", r.nickname)
            elif r.game == "终末地":
                # 终末地可能绑定多个角色，展示首个即可，不被后续覆盖
                names.setdefault("endfield", r.nickname)
        user_data["game_names"] = names
        return names

    @staticmethod
    def _store_game_awards(user_data: dict, results: list) -> dict:
        """
        按游戏分别记录奖励。

        与 game_names 同理，奖励也归属到具体游戏，
        避免方舟的奖励被显示在终末地那行。
        """
        awards_map = user_data.get("game_awards") or {}
        for r in results:
            key = "arknights" if r.game == "明日方舟" else (
                "endfield" if r.game == "终末地" else None
            )
            if key is None:
                continue
            items = [str(a) for a in (getattr(r, "awards", None) or []) if a]
            if items:
                awards_map.setdefault(key, items)
        user_data["game_awards"] = awards_map
        return awards_map

    @staticmethod
    def _is_signed_today_static(result) -> bool:
        if result.success:
            return True
        error = result.error.lower() if result.error else ""
        return any(k in error for k in
                   ["已签到", "请勿重复", "重复签到", "already", "签到过", "今日已"])

    async def _auto_sign_all_users(self):
        """为所有已注册用户执行自动签到，结果落库，按平台能力决定是否推送"""
        config = self._get_config()
        if not config.get("auto_sign_enabled", False):
            logger.info("自动签到已关闭，跳过执行")
            return

        logger.info("开始执行自动签到...")
        users = await self.get_kv_data("users", {})
        if not users:
            logger.info("没有已注册的用户，跳过自动签到")
            return

        max_delay = config.get("auto_sign_delay", 10)
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        stamp = now.isoformat(timespec="seconds")
        broadcast_groups: dict[str, list[str]] = {}

        for user_id, user_data in users.items():
            if max_delay > 0:
                delay = random.uniform(0, max_delay)
                logger.info(f"处理下一个用户前等待 {delay:.2f} 秒")
                await asyncio.sleep(delay)

            if "token" not in user_data:
                continue

            try:
                results, nickname = await self.api.do_full_sign_in(user_data["token"])
                self._mark_signed(user_data, results, today)
                self._store_game_names(user_data, results)
                self._store_game_awards(user_data, results)

                body = self._format_sign_status(results, nickname)
                ok = any(self._is_signed_today(r) for r in results) if results else False

                # 结果始终落库，供拉取模型读取
                user_data["last_result"] = {"time": stamp, "text": body, "ok": ok}
                if nickname:
                    user_data["nickname"] = nickname

                message = f"🎮森空岛自动签到结果\n\n{body}"
                await self._push_to_user(user_id, user_data, message)

                display = self._display_name(user_data.get("nickname", ""), user_data)
                for gkey in self._find_user_groups(await self.get_kv_data("groups", {}), user_id):
                    broadcast_groups.setdefault(gkey, []).append(display)

                users[user_id] = user_data
                logger.info(f"用户 {user_id} ({nickname}) 自动签到完成")
            except Exception as e:
                logger.error(f"用户 {user_id} 自动签到失败: {e}")
                user_data["last_result"] = {"time": stamp, "text": f"签到失败：{e}", "ok": False}
                users[user_id] = user_data
                await self._push_to_user(
                    user_id, user_data,
                    f"⚠️ 自动签到失败\n错误: {e}\n请使用 /skdlogin 重新登录",
                )

        await self.put_kv_data("users", users)

        if config.get("group_broadcast_enabled", False):
            await self._broadcast_group_summary(broadcast_groups)
        logger.info("自动签到执行完毕")

    @staticmethod
    def _find_user_groups(groups: dict, user_id: str) -> list[str]:
        """找出该用户所在的群记录键"""
        return [k for k, v in groups.items()
                if isinstance(v, dict) and user_id in (v.get("members") or [])]

    @staticmethod
    def _sync_user_to_groups(groups: dict, user_id: str) -> list[str]:
        """
        把用户加入所有已知的群记录。

        群成员名单原先只在「用户自己发过 /skd」时追加，导致：
        - 登录后不主动发 skd，别人发 skd 时看不到这个人
        - 只在群 A 发过，群 B 的名单里就没有他

        登录属于全局行为，绑定后同步到所有已知群，各群名单才能一致。
        返回实际加入的群键列表。
        """
        added = []
        for key, meta in groups.items():
            if not isinstance(meta, dict):
                continue
            members = list(meta.get("members") or [])
            if user_id not in members:
                members.append(user_id)
                meta["members"] = members
                added.append(key)
        return added

    async def _push_to_user(self, user_id: str, user_data: dict, message: str):
        """按各平台能力推送私聊消息；不支持时静默降级（结果已落库）"""
        umo_map = dict(user_data.get("umo_by_platform") or {})
        if not umo_map:
            legacy_umo = user_data.get("umo")
            if legacy_umo:
                umo_map = {self._platform_from_umo(legacy_umo): legacy_umo}

        for platform, umo in umo_map.items():
            if not umo:
                continue
            if not self._can_push(platform):
                logger.info(f"用户 {user_id} 在 {platform} 平台不支持主动推送，结果已落库待查询")
                continue
            try:
                await self.context.send_message(umo, MessageChain().message(message))
                user_data["notify_ok_count"] = user_data.get("notify_ok_count", 0) + 1
                logger.info(f"已推送签到结果给用户 {user_id}（{platform}）")
            except Exception as e:
                user_data["notify_fail_count"] = user_data.get("notify_fail_count", 0) + 1
                logger.warning(f"推送失败给用户 {user_id}（{platform}）: {e}")

    async def _broadcast_group_summary(self, summary: dict[str, list[str]]):
        """向群播报自动签到汇总；失败静默降级，不影响主流程"""
        groups = await self.get_kv_data("groups", {})
        for group_key, names in summary.items():
            meta = groups.get(group_key)
            if not isinstance(meta, dict):
                continue
            platform = meta.get("platform", PLATFORM_ONEBOT)
            if not self._capability(platform).get("can_push_group", False):
                continue
            umo = meta.get("umo") or ""
            if not umo:
                continue
            lines = ["🎯 森空岛每日签到汇总", "═══════════════", f"成功：{len(names)} 人", ""]
            lines.extend(f"  ✅ {n}" for n in names)
            try:
                await self.context.send_message(umo, MessageChain().message("\n".join(lines)))
                logger.info(f"已向群 {group_key} 播报签到汇总")
            except Exception as e:
                logger.info(f"群 {group_key} 播报失败（已静默降级）: {e}")

    # ==================== Helpers ====================

    @staticmethod
    def _display_width(text: str) -> int:
        """按显示宽度计算字符串长度：中文/全角字符占 2 列"""
        return sum(2 if ord(c) > 0x2E80 else 1 for c in text)

    @classmethod
    def _pad(cls, text: str, width: int) -> str:
        """按显示宽度右侧补空格，保证列对齐"""
        return text + " " * max(0, width - cls._display_width(text))

    @classmethod
    def _truncate(cls, text: str, width: int) -> str:
        """按显示宽度截断，超长部分以省略号结尾"""
        if cls._display_width(text) <= width:
            return text
        out = ""
        used = 0
        for ch in text:
            w = 2 if ord(ch) > 0x2E80 else 1
            if used + w > width - 1:
                break
            out += ch
            used += w
        return out + "…"

    def _is_signed_today(self, result) -> bool:
        return self._is_signed_today_static(result)

    def _format_sign_status(self, results: list, nickname: str = "") -> str:
        if not results:
            return "没有绑定游戏"
        lines = []
        if nickname:
            lines.append(f"【{nickname}】")
        for r in results:
            if r.success or self._is_signed_today(r):
                award = ", ".join(r.awards) if getattr(r, "awards", None) else "无奖励"
                lines.append(f"{r.game} 已签到 ({award})")
            else:
                lines.append(f"{r.game} 签到失败: {r.error}")
        return "\n".join(lines)

    def _display_name(self, nickname: str, user_data: dict) -> str:
        """按配置决定显示森空岛昵称还是平台昵称"""
        fallback = (user_data.get("last_username") or "").strip() or "(未知)"
        if not self._get_config().get("show_player_name", True):
            return fallback
        return nickname.strip() if nickname and nickname.strip() else fallback

    def _private_card_rows(self, user_data: dict) -> list:
        """把私聊的落库结果转成卡片行数据"""
        last_sign = user_data.get("last_sign") or {}
        names = user_data.get("game_names") or {}
        fallback = self._display_name(user_data.get("nickname", ""), user_data)

        ak_signed = bool(last_sign.get("arknights"))
        ef_signed = bool(last_sign.get("endfield"))
        ak_name = names.get("arknights") or (fallback if ak_signed else "")
        ef_name = names.get("endfield") or (fallback if ef_signed else "")

        return [{
            "name": ak_name or ef_name or fallback,
            "ak_name": ak_name,
            "ef_name": ef_name,
            "arknights": ak_signed,
            "endfield": ef_signed,
        }]

    def _render_stored_result(self, user_data: dict) -> str:
        """把落库的签到结果渲染成查询回复（拉取模型）"""
        last = user_data.get("last_result") or {}
        text = last.get("text")
        names = user_data.get("game_names") or {}
        fallback = self._display_name(user_data.get("nickname", ""), user_data)
        if not text:
            return "📋 森空岛签到\n\n暂无签到记录，发送 /skdlogin <token> 开始绑定"

        stamp = last.get("time") or ""
        try:
            stamp = datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            stamp = ""

        # 落库文本形如「明日方舟 已签到 (奖励)」，这里按游戏拆出各自昵称与状态
        entries = []
        for line in text.split("\n"):
            line = line.strip()
            if not line or (line.startswith("【") and line.endswith("】")):
                continue
            game = "明日方舟" if line.startswith("明日方舟") else (
                "终末地" if line.startswith("终末地") else None
            )
            if game is None:
                continue
            key = "arknights" if game == "明日方舟" else "endfield"
            # 详情去掉开头重复的游戏名，只留状态
            detail = line[len(game):].strip()
            # 去掉奖励括号，只保留「已签到」状态
            if "已签到 (" in detail and detail.endswith(")"):
                detail = detail[:detail.index("(")].strip()
            name = names.get(key) or fallback
            entries.append((game, key, name, detail))

        lines = ["📋 森空岛签到", ""]
        for game, key, name, detail in entries:
            lines.append(f"  {game}　{name}")
            if detail:
                lines.append(f"    {detail}")
        if stamp:
            lines.append("")
            lines.append(f"  {stamp}")
        return "\n".join(lines)

    # ==================== Commands ====================

    # 裸指令别名：QQ 官方群里 @机器人 skd（不带 /）不会命中 CommandFilter。
    # 原因：适配器已剥离 At 标记，message_str 变成 "skd"；但它不以 wake_prefix
    # 开头，唤醒检查走 At 分支时会把 message_str 置空，CommandFilter 又要求
    # 剥前缀后全等匹配指令名，因此匹配失败。这里改从消息链的 Plain 段取原文。
    BARE_COMMANDS = {
        "skd": "skd",
        "签到": "skd",
        "森空岛签到": "skd",
        "森空岛": "skd",
        "skdhelp": "skdhelp",
        "帮助": "skdhelp",
        "skdusers": "skdusers",
        "签到统计": "skdusers",
        "skdcheck": "skdcheck",
        "卡片诊断": "skdcheck",
        "skdlogout": "skdlogout",
        "登出": "skdlogout",
        "skdlogin": "skdlogin",
        "登录": "skdlogin",
    }

    # QQ 官方平台在消息文本里插入的 @ 标记（如 <qqbot-at-user id="xxx" />）
    _AT_MARKUP = re.compile(
        r'<\s*qqbot-at-user\s+id="[^"]*"\s*/\s*>|<\s*@\s*[A-Za-z0-9_\-]+\s*>'
    )

    @classmethod
    def _raw_text(cls, event: AstrMessageEvent) -> str:
        """
        取消息的原始纯文本。

        1. 不能用 get_message_str()：唤醒检查阶段已对它做过处理，
           走 At 唤醒分支时会被置空，裸指令文本会丢失。
        2. 需要剥掉 QQ 官方平台的 @ 标记：适配器只会剥离「@机器人自己」的
           标记，@其他人时标记会原样留在文本里，导致指令匹配失败。
        """
        parts = []
        for seg in event.get_messages() or []:
            if type(seg).__name__ == "Plain":
                parts.append(getattr(seg, "text", "") or "")
        text = cls._AT_MARKUP.sub(" ", "".join(parts)).strip()
        if text:
            return text
        return cls._AT_MARKUP.sub(" ", event.get_message_str() or "").strip()

    @staticmethod
    def _is_at_bot(event: AstrMessageEvent) -> bool:
        """判断消息链中是否 @ 了机器人"""
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = ""
        for seg in event.get_messages() or []:
            if type(seg).__name__ == "At":
                at_id = str(getattr(seg, "qq", "") or "")
                if not self_id or at_id == self_id or at_id == "all":
                    return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL, priority=99)
    async def bare_command_listener(self, event: AstrMessageEvent):
        """兜底处理 @机器人 skd 这类不带 wake_prefix 的写法"""
        message = self._raw_text(event)
        if not message:
            return

        # 群聊必须真的 @ 了机器人才处理，避免抢答普通群聊
        if self._is_group(event) and not self._is_at_bot(event):
            return

        parts = message.split()
        if len(parts) > 2:
            return
        head = parts[0].lower() if parts else ""
        target = self.BARE_COMMANDS.get(head)
        if not target:
            return

        arg = parts[1] if len(parts) == 2 else ""

        if target == "skd":
            async for res in self.skd(event):
                yield res
        elif target == "skdhelp":
            async for res in self.skdhelp(event):
                yield res
        elif target == "skdusers":
            async for res in self.skdusers(event):
                yield res
        elif target == "skdcheck":
            async for res in self.skdcheck(event):
                yield res
        elif target == "skdlogout":
            async for res in self.skdlogout(event):
                yield res
        elif target == "skdlogin":
            async for res in self.skdlogin(event, arg):
                yield res

    @filter.command("skdhelp")
    async def skdhelp(self, event: AstrMessageEvent):
        """森空岛签到插件帮助"""
        yield event.plain_result(
            "森空岛签到插件帮助\n"
            "1. 发送 /skdlogin <token> 登录并签到\n"
            "2. 发送 /skdlogout 登出\n"
            "3. /skd 查看签到状态\n"
            "4. /skdusers 查看绑定统计\n"
            "5. /skdcheck 查看签到卡片图片的可用状态\n"
            "注：token 等同账号密码，群聊中发送后插件会自动撤回该条消息"
        )

    @filter.command("skdcheck")
    async def skdcheck(self, event: AstrMessageEvent):
        """诊断签到卡片图片是否可用"""
        config = self._get_config()
        enabled = config.get("card_image_enabled", False)
        theme = config.get("card_theme", "dark")

        try:
            import PIL
            pillow_ver = PIL.__version__
            has_pillow = True
        except ImportError:
            pillow_ver = "-"
            has_pillow = False

        card = self._card
        has_font = bool(card.font_path)

        lines = ["🔍 签到卡片诊断", "═══════════════"]
        lines.append(f"图片开关: {'✅ 已开启' if enabled else '❌ 未开启'}")
        if not enabled:
            lines.append("　　→ 在 WebUI「插件配置」中开启「签到卡片图片」")
        lines.append(f"卡片主题: {theme}")
        lines.append(f"Pillow: {'✅ 已安装 ' + pillow_ver if has_pillow else '❌ 未安装'}")
        if not has_pillow:
            lines.append("　　→ 执行 pip install Pillow")
        lines.append(f"中文字体: {'✅ 已找到' if has_font else '❌ 未找到'}")
        if has_font:
            lines.append(f"　　→ {card.font_path}")
        else:
            lines.append("　　→ 插件已内置字体，请确认 fonts/ 目录完整")
            lines.append("　　→ 或在系统中安装中文字体（fonts-noto-cjk 等）")

        if enabled and has_pillow and has_font:
            test_rows = [{
                "name": "测试", "ak_name": "测试", "ef_name": "",
                "arknights": True, "endfield": False,
            }]
            try:
                path = self._render_card(test_rows)
                if path:
                    lines.append(f"渲染测试: ✅ 成功（{path}）")
                else:
                    lines.append("渲染测试: ❌ 失败，请查看 AstrBot 日志")
            except Exception as e:
                lines.append(f"渲染测试: ❌ 异常 {e}")
        elif not enabled:
            lines.append("渲染测试: ⏭ 跳过（图片开关未开启）")

        yield event.plain_result("\n".join(lines))

    @filter.command("skdlogin")
    async def skdlogin(self, event: AstrMessageEvent, token: str = ""):
        """
        使用 token 登录并立即签到。

        私聊与群聊都支持。群聊场景下会立即撤回用户发出的那条消息，
        避免 token 被他人看到（撤回需机器人是群管理员且在 2 分钟内）。
        """
        config = self._get_config()
        platform = self._detect_platform(event)
        user_name = event.get_sender_name()
        is_group = self._is_group(event)

        users = await self.get_kv_data("users", {})
        user_id = self._resolve_user_key(users, event.get_sender_id())

        token = (token or "").strip()
        if not token:
            yield event.plain_result(
                "请先获取token，方法如下:\n"
                "1. 登录 鹰角网络通行证 后，打开 (https://web-api.hypergryph.com/account/info/hg) 记下 content 字段的值（推荐）。\n"
                "   或者登录 森空岛网页版 (https://www.skland.com/) 后，\n"
                "   打开 (https://web-api.skland.com/account/info/hg) 记下 content 字段的值。\n"
                "   content字段示例：N6QKb2C3d4A1b2C3d4，如果其中包含符号也一起复制\n"
                "2. 使用方法: /skdlogin <content>\n"
                "   注意：token 等同账号密码，"
                + ("群聊中发送后请立即撤回该条消息\n" if is_group
                   else "建议只在私聊中提交\n")
            )
            return

        max_users = config.get("max_users", 10)
        if user_id not in users and max_users > 0 and len(users) >= max_users:
            yield event.plain_result(
                f"❌ 绑定失败：已达到最大用户数限制（{max_users}个）\n请联系管理员调整配置"
            )
            return

        yield event.plain_result("正在登录并签到，请稍候...")
        try:
            results, nickname = await self.api.do_full_sign_in(token)
            now = datetime.now()

            # 复用既有记录，避免覆盖丢失 token / last_sign 等字段
            user_data = users.get(user_id) or {}
            user_data["token"] = token
            user_data["nickname"] = nickname
            user_data["last_username"] = user_name
            user_data["bound_at"] = user_data.get("bound_at") or now.isoformat()
            user_data["platform_name"] = platform

            umo_map = dict(user_data.get("umo_by_platform") or {})
            umo_map[platform] = event.unified_msg_origin
            user_data["umo_by_platform"] = umo_map
            user_data["umo"] = event.unified_msg_origin

            self._mark_signed(user_data, results, now.strftime("%Y-%m-%d"))
            self._store_game_names(user_data, results)
            self._store_game_awards(user_data, results)

            body = self._format_sign_status(results, nickname)
            user_data["last_result"] = {
                "time": now.isoformat(timespec="seconds"),
                "text": body,
                "ok": any(self._is_signed_today(r) for r in results) if results else False,
            }

            users[user_id] = user_data
            await self.put_kv_data("users", users)

            # 登录是全局行为：同步到所有已知群，保证各群名单一致
            groups = await self.get_kv_data("groups", {})
            added_groups = self._sync_user_to_groups(groups, user_id)
            if added_groups:
                await self.put_kv_data("groups", groups)
                logger.info(
                    f"用户 {user_id} 已加入 {len(added_groups)} 个群：{added_groups}"
                )

            # 群聊场景：立即撤回含 token 的那条消息，避免被他人看到
            recall_note = ""
            if is_group:
                recalled, reason = await self._recall_message(event)
                if recalled:
                    recall_note = "\n\n🔒 已撤回你发送的 token 消息"
                else:
                    recall_note = (
                        f"\n\n⚠️ 撤回失败{reason}，请手动删除你发送的那条消息"
                    )

            notice = "" if self._can_push(platform) else \
                "\n\n（当前平台不支持主动推送，将在你查询时展示结果）"
            yield event.plain_result(f"登录成功！\n{body}{notice}{recall_note}")
        except Exception as e:
            logger.error(f"skdlogin失败: {e}")
            # 失败时也要撤回，防止 token 留在群里
            if is_group:
                await self._recall_message(event)
            yield event.plain_result(f"登录失败: {e}")
    # 官方错误码 → 用户可读提示
    _RECALL_ERROR_HINTS = {
        "40062003": "机器人不是群管理员，无权撤回他人消息",
        "40064004": "消息发送已超过 2 分钟，官方接口不允许撤回",
        "40061001": "请求参数无效",
        "50065001": "撤回失败，请稍后重试",
    }

    async def _recall_message(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """
        撤回用户在群里发出的那条消息（用于保护 token）。

        对应官方接口 DELETE /v2/groups/{group_openid}/messages/{message_id}：
        - group_openid：event.message_obj.group_id 在官方适配器里就是
          message.group_openid（见 qqofficial_platform_adapter.py `abm.group_id = message.group_openid`）
        - message_id：event.message_obj.message_id 取自消息事件的 d.id，
          正是文档所说「群成员的消息ID从群消息事件里获取」

        官方限制：
        - 机器人须为**群管理员**，否则报 40062003 无操作权限
          （普通成员只能撤回机器人自己发的消息）
        - 消息发送超过 **2 分钟**不可撤回，报 40064004
        - 频率限制 10 QPS

        返回 (是否成功, 失败原因提示)。其他平台无此能力。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return False, "当前平台不支持撤回消息"
        message_id = getattr(event.message_obj, "message_id", None)
        group_openid = getattr(event.message_obj, "group_id", None)
        if not message_id or not group_openid:
            return False, "未取到消息 ID 或群 ID"

        try:
            from botpy.http import Route

            # 路径用占位符，实际值通过 kwargs 传入（与官方适配器写法一致）
            route = Route(
                "DELETE",
                "/v2/groups/{group_openid}/messages/{message_id}",
                group_openid=group_openid,
                message_id=message_id,
            )
            await bot.api._http.request(route)
            logger.info(f"[森空岛] 已撤回群 {group_openid} 中的 token 消息")
            return True, ""
        except Exception as e:
            text = str(e)
            hint = ""
            for code, desc in self._RECALL_ERROR_HINTS.items():
                if code in text:
                    hint = desc
                    break
            reason = f"（{hint}）" if hint else ""
            logger.warning(
                f"[森空岛] 撤回群消息失败（群 {group_openid} 消息 {message_id}）：{e}{reason}。"
                "请手动删除该条消息"
            )
            return False, reason

    @filter.command("skdlogout")
    async def skdlogout(self, event: AstrMessageEvent):
        """登出并清除绑定信息"""
        users = await self.get_kv_data("users", {})
        user_id = self._resolve_user_key(users, event.get_sender_id())
        if user_id not in users:
            yield event.plain_result("您尚未绑定森空岛账号")
            return

        platform = self._detect_platform(event)
        user_data = users[user_id]
        remaining = {k: v for k, v in (user_data.get("umo_by_platform") or {}).items()
                     if k != platform and v}

        if remaining:
            # 其他平台仍绑定，仅摘除当前平台
            user_data["umo_by_platform"] = remaining
            user_data["umo"] = next(iter(remaining.values()))
            users[user_id] = user_data
            await self.put_kv_data("users", users)
            yield event.plain_result("已解除当前平台的绑定，其他平台绑定仍然有效")
        else:
            del users[user_id]
            groups = await self.get_kv_data("groups", {})
            for meta in groups.values():
                if isinstance(meta, dict) and user_id in (meta.get("members") or []):
                    meta["members"].remove(user_id)
            await self.put_kv_data("groups", groups)
            await self.put_kv_data("users", users)
            yield event.plain_result("已退出登录并清除绑定信息")

    @filter.command("skdusers")
    async def skdusers(self, event: AstrMessageEvent):
        """查询当前注册用户数量"""
        users = await self.get_kv_data("users", {})
        groups = await self.get_kv_data("groups", {})
        config = self._get_config()
        max_users = config.get("max_users", 10)
        platform = self._detect_platform(event)

        group_stats = [
            f"  • 群 {k}: {len(v.get('members', []))} 人"
            for k, v in groups.items()
            if isinstance(v, dict) and v.get("members")
        ]

        today = datetime.now().strftime("%Y-%m-%d")
        signed_today = sum(
            1 for d in users.values()
            if any(v == today for v in (d.get("last_sign") or {}).values())
        )

        lines = [
            "📊 森空岛签到用户统计",
            "═══════════════════",
            f"📝 总注册用户: {len(users)} 人",
            f"✅ 今日已签到: {signed_today} 人",
            f"📉 未签到用户: {len(users) - signed_today} 人",
            f"🔌 当前平台: {platform}",
        ]

        if event.is_admin():
            if max_users > 0:
                lines.append(f"🎯 最大限制: {max_users} 人")
                lines.append(f"🆓 剩余名额: {max(0, max_users - len(users))} 人")
            lines.append(f"📤 推送模式: {config.get('push_mode', 'auto')}"
                         f"（本平台可推送: {self._can_push(platform)}）")

            if not self._is_group(event):
                if group_stats:
                    lines.append("\n📌 群聊分布（仅代表群内同玩的数量）:")
                    lines.extend(group_stats)
                if len(users) <= 20:
                    lines.append("\n👤 用户列表:")
                    for uid, d in users.items():
                        nick = d.get("nickname") or d.get("last_username", "未知")
                        bound = "、".join((d.get("umo_by_platform") or {}).keys()) \
                            or d.get("platform_name", "-")
                        last = list((d.get("last_sign") or {}).values())[-1] \
                            if d.get("last_sign") else "未签到"
                        lines.append(f"  • {nick} [{bound}] (最后签到: {last})")
                else:
                    lines.append("\n💡 用户数过多，不显示详细列表")
            else:
                lines.append("\n💡 如需查看详细用户列表请私信")
        yield event.plain_result("\n".join(lines))

    @filter.command("skd")
    async def skd(self, event: AstrMessageEvent):
        """群聊显示群成员签到状态，私聊显示自己"""
        config = self._get_config()
        platform = self._detect_platform(event)
        users_data = await self.get_kv_data("users", {})

        if not self._is_group(event):
            # ==================== 私聊：拉取模型 ====================
            user_id = self._resolve_user_key(users_data, event.get_sender_id())
            user_data = users_data.get(user_id)
            if not user_data:
                yield event.plain_result("你还未绑定账号，请使用 /skdlogin <token>")
                return

            text = self._render_stored_result(user_data)
            if config.get("card_image_enabled", False):
                path = self._render_card(self._private_card_rows(user_data))
                if path:
                    # 只发图片，不再附带纯文本
                    yield event.chain_result([Comp.Image.fromFileSystem(path)])
                    return
                logger.warning("签到卡片渲染不可用，已回退为纯文本输出（发送 /skdcheck 可诊断）")
            yield event.plain_result(text)
            return

        # ==================== 群聊 ====================
        group_id = event.message_obj.group_id
        sender_id = event.get_sender_id()
        gkey = self._group_key(platform, group_id)

        groups = await self.get_kv_data("groups", {})
        meta = groups.get(gkey)
        if not isinstance(meta, dict):
            meta = {"members": [], "umo": "", "platform": platform}
            groups[gkey] = meta
        meta["umo"] = event.unified_msg_origin
        meta["platform"] = platform
        members = list(meta.get("members") or [])
        if sender_id in users_data and sender_id not in members:
            members.append(sender_id)
        meta["members"] = members

        live = config.get("live_query_in_group", False)
        today = datetime.now().strftime("%Y-%m-%d")

        # 先收集数据，再统一渲染，保证昵称列对齐
        #
        # 签到名单取「所有已绑定用户」，而不是本群 members：
        # members 只记录「在本群发过 /skd 的人」，用它会导致
        #   - 登录后不主动发 skd 的人不出现
        #   - 不同群看到的名单不一致
        # 森空岛签到状态与群无关，统一按已绑定用户展示更符合预期。
        rows = []
        for uid, user_data in users_data.items():
            if not user_data or not user_data.get("token"):
                continue
            nickname = user_data.get("nickname", "")
            failed = False
            if live:
                try:
                    results, live_nick = await self.api.do_full_sign_in(user_data["token"])
                    nickname = live_nick or nickname
                    self._mark_signed(user_data, results, today)
                    self._store_game_names(user_data, results)
                    self._store_game_awards(user_data, results)
                    if nickname:
                        user_data["nickname"] = nickname
                    users_data[uid] = user_data
                except Exception as e:
                    logger.warning(f"群聊实时查询用户 {uid} 失败: {e}")
                    failed = True

            last_sign = user_data.get("last_sign") or {}
            names = user_data.get("game_names") or {}
            fallback = self._display_name(nickname, user_data)
            ak_signed = bool(last_sign.get("arknights"))
            ef_signed = bool(last_sign.get("endfield"))
            # 只在该游戏确实签到过时才显示，避免未绑定的游戏出现空列
            ak_name = names.get("arknights") or (fallback if ak_signed else "")
            ef_name = names.get("endfield") or (fallback if ef_signed else "")
            rows.append({
                "name": fallback,
                "ak_name": ak_name,
                "ef_name": ef_name,
                "arknights": ak_signed,
                "endfield": ef_signed,
                "failed": failed,
            })

        await self.put_kv_data("groups", groups)
        await self.put_kv_data("users", users_data)

        # 优先输出图片卡片，不可用时回退纯文本
        if config.get("card_image_enabled", False):
            path = self._render_card(rows)
            if path:
                # 只发图片，不再附带纯文本
                yield event.chain_result([Comp.Image.fromFileSystem(path)])
                return
            logger.warning("签到卡片渲染不可用，已回退为纯文本输出（发送 /skdcheck 可诊断）")

        yield event.plain_result(self._render_group_status(rows))

    def _render_card(self, rows: list) -> str | None:
        """渲染签到卡片图片，失败返回 None"""
        if not rows:
            return None
        try:
            # 主题按当前配置取，WebUI 改配置后无需重启插件
            theme = self._get_config().get("card_theme", "dark")
            if theme != self._card.theme_name:
                self._card = CardRenderer(theme=theme)
            if not self._card.available:
                logger.warning(
                    "签到卡片不可用：缺少 Pillow 或中文字体，"
                    "请安装 Pillow 并在系统安装中文字体（如 fonts-noto-cjk）"
                )
                return None
            return self._card.render(rows)
        except Exception as e:
            logger.warning(f"卡片渲染失败: {e}")
            return None

    # 群聊状态表的列宽（按显示宽度，中文占 2 列）
    _GAME_COL = 15
    _STATE_COL = 5
    _NAME_MAX = 20

    def _render_group_status(self, rows: list) -> str:
        """
        渲染群聊签到状态表。

        一个人一行：绑定了两个游戏时用斜杠横向合并，形如
        「明日方舟/终末地  ✓/✗  刀客/终末刀客」。
        斜杠两侧一一对应，不会张冠李戴；未绑定终末地的玩家只显示单游戏。
        """
        if not rows:
            return "📋 森空岛签到\n\n本群还没有人绑定账号"

        lines = ["📋 森空岛签到"]

        signed_count = 0
        ak_count = 0
        ef_count = 0
        for r in rows:
            if r.get("failed"):
                name = self._truncate(r.get("name", ""), self._NAME_MAX)
                lines.append(f"  ⚠️ 查询失败　·  {name}")
                continue

            ak_name = self._truncate(r.get("ak_name") or "", self._NAME_MAX)
            ef_name = self._truncate(r.get("ef_name") or "", self._NAME_MAX)
            if not ak_name and not ef_name:
                # 两个游戏都没有昵称时，至少显示兜底名
                ak_name = self._truncate(r.get("name", ""), self._NAME_MAX)

            if r["arknights"]:
                ak_count += 1
            if r["endfield"]:
                ef_count += 1
            if r["arknights"] or r["endfield"]:
                signed_count += 1

            # 只展示实际绑定（有昵称）的游戏，未绑定的不占列
            if ak_name and ef_name:
                games = "明日方舟/终末地"
                states = f"{'✓' if r['arknights'] else '✗'}/{'✓' if r['endfield'] else '✗'}"
                names = f"{ak_name}/{ef_name}"
            elif ef_name:
                games = "终末地"
                states = "✓" if r["endfield"] else "✗"
                names = ef_name
            else:
                games = "明日方舟"
                states = "✓" if r["arknights"] else "✗"
                names = ak_name

            lines.append(
                f"  {self._pad(games, self._GAME_COL + 1)}"
                f"{self._pad(states, self._STATE_COL)}"
                f"  {names}"
            )

        total = len(rows)
        lines.append("")
        lines.append(f"  已签到 {signed_count}/{total} 人"
                     f"　·　明日方舟 {ak_count}　·　终末地 {ef_count}")
        return "\n".join(lines)