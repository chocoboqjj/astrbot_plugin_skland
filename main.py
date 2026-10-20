"""
AstrBot Plugin - 森空岛签到 (Skland Sign-In)

Commands:
- skd (group): Show sign-in status for all bound users in the group
- skd (private): Show user's own sign-in status
- skdlogin (private): Login with token and immediately sign in
- skdlogout (private): Logout and remove token
- skdusers (all): Show users and stats
- skdhelp (all): Show help

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
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.config import put_config

from .skland_api import SklandAPI

PLUGIN_NAME = "astrbot_plugin_skland"

# 平台标识
PLATFORM_ONEBOT = "onebot"
PLATFORM_QQOFFICIAL = "qq_official"
PLATFORM_OTHER = "other"

# 各平台主动推送能力（由平台规则决定，详见 README）
PLATFORM_CAPABILITY = {
    PLATFORM_ONEBOT: {
        "can_push_private": True,   # 支持定时私聊推送
        "can_push_group": True,     # 支持群内主动播报
        "need_at_in_group": False,  # 群聊无需 @ 即可触发指令
    },
    PLATFORM_QQOFFICIAL: {
        "can_push_private": False,  # 官方已取消主动推送
        "can_push_group": True,     # 群主开启开关后有限频能力
        "need_at_in_group": True,   # 群聊必须 @ 机器人
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

    def _render_stored_result(self, user_data: dict) -> str:
        """把落库的签到结果渲染成查询回复（拉取模型）"""
        last = user_data.get("last_result") or {}
        text = last.get("text")
        name = self._display_name(user_data.get("nickname", ""), user_data)
        header = f"森空岛签到状态【{name}】"
        if not text:
            return f"{header}\n\n暂无签到记录，发送 /skdlogin <token> 开始绑定"
        stamp = last.get("time") or ""
        try:
            stamp = datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            pass
        return f"{header}\n\n{text}\n\n签到时间: {stamp}" if stamp else f"{header}\n\n{text}"

    # ==================== Commands ====================

    @filter.command("skdhelp")
    async def skdhelp(self, event: AstrMessageEvent):
        """森空岛签到插件帮助"""
        platform = self._detect_platform(event)
        tip = "\n（QQ 官方机器人：群聊内需 @机器人 才能触发指令）" \
            if self._capability(platform).get("need_at_in_group") else ""
        yield event.plain_result(
            "森空岛签到插件帮助\n"
            "1. 私聊机器人发送 /skdlogin <token> 登录并签到\n"
            "2. 私聊机器人发送 /skdlogout 登出\n"
            "3. /skd 查看签到状态（群聊需 @机器人）\n"
            "4. /skdusers 查看绑定统计" + tip
        )

    @filter.command("skdlogin")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def skdlogin(self, event: AstrMessageEvent, token: str = ""):
        """使用 token 登录并立即签到"""
        config = self._get_config()
        platform = self._detect_platform(event)
        user_name = event.get_sender_name()

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
                "   注意：token 等同账号密码，只在私聊中提交"
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

            body = self._format_sign_status(results, nickname)
            user_data["last_result"] = {
                "time": now.isoformat(timespec="seconds"),
                "text": body,
                "ok": any(self._is_signed_today(r) for r in results) if results else False,
            }

            users[user_id] = user_data
            await self.put_kv_data("users", users)

            notice = "" if self._can_push(platform) else \
                "\n\n（当前平台不支持主动推送，将在你查询时展示结果）"
            yield event.plain_result(f"登录成功！\n{body}{notice}")
        except Exception as e:
            logger.error(f"skdlogin失败: {e}")
            yield event.plain_result(f"登录失败: {e}")

    @filter.command("skdlogout")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
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
            yield event.plain_result(self._render_stored_result(user_data))
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
        lines = [" 森空岛签到统计", "═══════════════", "方舟 | 终末 | 昵称", "-----------------"]

        for uid in members:
            user_data = users_data.get(uid)
            if not user_data:
                continue
            nickname = user_data.get("nickname", "")
            if live:
                try:
                    results, live_nick = await self.api.do_full_sign_in(user_data["token"])
                    nickname = live_nick or nickname
                    self._mark_signed(user_data, results, today)
                    if nickname:
                        user_data["nickname"] = nickname
                    users_data[uid] = user_data
                except Exception as e:
                    logger.warning(f"群聊实时查询用户 {uid} 失败: {e}")
                    lines.append(" ⚠️ | ⚠️ | (查询失败)")
                    continue

            last_sign = user_data.get("last_sign") or {}
            ak = "✅" if last_sign.get("arknights") else "❌"
            ef = "✅" if last_sign.get("endfield") else "❌"
            lines.append(f" {ak} | {ef} | {self._display_name(nickname, user_data)}")

        await self.put_kv_data("groups", groups)
        await self.put_kv_data("users", users_data)

        if self._capability(platform).get("need_at_in_group"):
            lines.append("\n(QQ 官方机器人：需 @机器人 + 指令，且默认收不到全量群消息)")
        lines.append("（状态来自最近一次自动签到，发送 /skdlogin 可刷新）")
        yield event.plain_result("\n".join(lines))