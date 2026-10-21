"""
AstrBot Plugin - 森空岛签到 (Skland Sign-In)

Commands:
- skd (group): Show sign-in status for all bound users in the group
- skd (private): Show user's own sign-in status
- skdlogin (all): Login with token and immediately sign in
    群聊使用时会自动撤回含 token 的那条消息
    多次登录可绑定多个森空岛账号
- skdlogout (all): Logout and remove token
    带 token 参数时只移除指定的森空岛账号
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
- auto_sign_hour: 自动签到时间（小时，0-23），默认 8
- auto_sign_minute: 自动签到时间（分钟，0-59），默认 30
- push_enabled: 每日签到结果推送开关
- push_hour: 签到结果推送时间（小时，0-23），默认 9
- push_minute: 签到结果推送时间（分钟，0-59），默认 0
- show_player_name: 显示玩家昵称（否则显示平台昵称）
- auto_sign_delay: 签到延时
- max_users: 最大用户数量
- push_mode: 推送模式（auto/private/pull）
- group_broadcast_enabled: 自动签到后向群播报汇总
- live_query_in_group: 群聊 /skd 是否实时查询
- card_image_enabled: /skd 是否以图片卡片输出
- push_card_image: 每日签到推送与新人登录后是否以图片卡片输出
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


@register(PLUGIN_NAME, "AstrBot", "森空岛自动签到插件（支持 OneBot 与 QQ 官方机器人）", "1.7.0")
class SklandPlugin(Star):
    """森空岛签到插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api = SklandAPI(max_retries=3)
        self.scheduler = AsyncIOScheduler()
        self._card = CardRenderer(theme=self.config.get("card_theme", "dark"))
        # 已提示过「无撤回权限」的群，避免每次登录都重复刷 WARN 日志
        self._recall_denied_groups: set[str] = set()
        self._init_config()
    # ==================== Config ====================

    def _init_config(self):
        """注册后台配置项（默认值与 _conf_schema.json 保持一致）"""
        defaults = [
            ("自动签到开关", "auto_sign_enabled", True,
             "开启后，将在指定时间自动为所有已注册用户签到"),
            ("自动签到时间（小时）", "auto_sign_hour", 8,
             "自动签到执行的小时（0-23），默认 8 即早上 8:30 执行签到（只落库，不推送）"),
            ("自动签到时间（分钟）", "auto_sign_minute", 30,
             "自动签到执行的分钟（0-59），默认 30 即 8:30 执行"),
            ("签到结果推送时间（小时）", "push_hour", 9,
             "每日签到结果推送的小时（0-23），默认 9。独立于签到时间，读取已落库结果后推送"),
            ("签到结果推送时间（分钟）", "push_minute", 0,
             "每日签到结果推送的分钟（0-59），默认 0 即 9:00 推送"),
            ("签到结果推送开关", "push_enabled", True,
             "关闭后只做签到落库，不主动推送签到结果（用户仍可用 /skd 主动查询）"),
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
            ("签到结果图片推送", "push_card_image", True,
             "每日签到结果推送与新用户登录成功后，以图片卡片输出（与 /skd 同款）；"
             "缺少 Pillow 或中文字体时自动回退为纯文本"),
            ("卡片主题", "card_theme", "dark",
             "dark=深色 / light=浅色，仅在启用图片卡片时生效"),
            ("token 冲突处理", "token_conflict", "reject",
             "已绑定的 token 被再次绑定时如何处理："
             "reject=一律拒绝（推荐，仅支持 QQ 官方平台）；"
             "merge=自动复用原记录，用于清理历史重复数据"),
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
            "auto_sign_hour": self.config.get("auto_sign_hour", 8),
            "auto_sign_minute": self.config.get("auto_sign_minute", 30),
            "push_hour": self.config.get("push_hour", 9),
            "push_minute": self.config.get("push_minute", 0),
            "push_enabled": self.config.get("push_enabled", True),
            "show_player_name": self.config.get("show_player_name", True),
            "auto_sign_delay": self.config.get("auto_sign_delay", 10),
            "max_users": self.config.get("max_users", 10),
            "push_mode": self.config.get("push_mode", "auto"),
            "group_broadcast_enabled": self.config.get("group_broadcast_enabled", False),
            "live_query_in_group": self.config.get("live_query_in_group", False),
            "card_image_enabled": self.config.get("card_image_enabled", False),
            "push_card_image": self.config.get("push_card_image", True),
            "card_theme": self.config.get("card_theme", "dark"),
            "token_conflict": self.config.get("token_conflict", "reject"),
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
        """
        解析规范用户键。

        当前只支持 QQ 官方平台，sender_id 即稳定的 openid，
        通常原样返回。保留 alias 读取是为了兼容历史数据 ——
        早期版本支持跨平台（OneBot 的 QQ 号 / QQ 官方的 openid）时
        会写入别名记录指回主记录，这里继续解析避免存量数据失效。
        """
        seen = set()
        current = user_id
        while current in users and current not in seen:
            seen.add(current)
            alias = (users[current].get("alias") or {}).get("uid")
            if not alias or alias not in users:
                break
            current = alias
        return current if current in users else user_id

    # ==================== Accounts (多账号支持) ====================

    @staticmethod
    def _get_accounts(user_data: dict) -> list[dict]:
        """
        取该用户绑定的所有森空岛账号。

        一个平台用户可以绑定多个森空岛账号（多个 token），每个账号
        有各自的昵称、签到状态与结果。旧数据只有单个 token，
        这里把它视为「唯一账号」返回，保证向后兼容。
        """
        accounts = user_data.get("accounts")
        if isinstance(accounts, list) and accounts:
            return accounts
        # 旧结构：token 直接挂在 user_data 上
        if user_data.get("token"):
            legacy = {
                "token": user_data["token"],
                "nickname": user_data.get("nickname", ""),
                "game_names": user_data.get("game_names") or {},
                "last_sign": user_data.get("last_sign") or {},
                "game_awards": user_data.get("game_awards") or {},
                "last_result": user_data.get("last_result") or {},
            }
            return [legacy]
        # 没有 token 但保留了历史签到结果：仍要能展示
        if user_data.get("last_result") or user_data.get("last_sign"):
            return [{
                "token": "",
                "nickname": user_data.get("nickname", ""),
                "game_names": user_data.get("game_names") or {},
                "last_sign": user_data.get("last_sign") or {},
                "game_awards": user_data.get("game_awards") or {},
                "last_result": user_data.get("last_result") or {},
            }]
        return []

    @staticmethod
    def _normalize_accounts_inplace(user_data: dict) -> int:
        """
        就地规范化该用户所有账号的 token，返回被修改的账号数。

        `_get_accounts` 对旧结构返回的是新建的 dict，改它不会影响原数据；
        清理存量脏 token 时必须用这个方法才能真正写回。
        """
        changed = 0
        accounts = user_data.get("accounts")
        if isinstance(accounts, list):
            for acc in accounts:
                if not isinstance(acc, dict):
                    continue
                tok = acc.get("token")
                if not tok:
                    continue
                norm = SklandPlugin._normalize_token(tok)
                if norm != tok:
                    acc["token"] = norm
                    changed += 1
            return changed
        # 旧结构：token 挂在顶层
        tok = user_data.get("token")
        if tok:
            norm = SklandPlugin._normalize_token(tok)
            if norm != tok:
                user_data["token"] = norm
                changed += 1
        return changed

    # 复制粘贴可能带入的不可见字符（用转义码写，避免源码里出现裸字符）
    # U+200B 零宽空格 / U+200C 零宽非连接符 / U+200D 零宽连接符
    # U+200E LRM / U+200F RLM / U+2060 单词连接符
    # U+FEFF BOM / U+00AD 软连字符
    _INVISIBLE_CHARS = (
        "\u200b\u200c\u200d"   # 零宽空格 / 零宽非连接符 / 零宽连接符
        "\u200e\u200f\u2060"   # LRM / RLM / 单词连接符
        "\ufeff\u00ad"         # BOM / 软连字符
        "\u202a\u202b\u202c\u202d\u202e"  # 方向控制标记
        "\u2066\u2067\u2068\u2069"        # 方向隔离标记
    )

    @staticmethod
    def _normalize_token(token: str) -> str:
        """
        规范化 token，去除复制粘贴可能带入的隐形字符。

        森空岛 token 形如 "N6QKb2C3d4A1b2C3d4"。用户在 QQ 里复制粘贴时
        常混入零宽空格、全角空格、方向标记等不可见字符，导致同一个 token
        被当成两个不同账号 —— 这正是「登录两次出现重复」的常见原因。

        只做保守清理，不改变 token 本身的可打印字符。
        """
        if not token:
            return ""
        cleaned = "".join(
            ch for ch in token
            if ch not in SklandPlugin._INVISIBLE_CHARS
        )
        # 去掉所有空白（含全角空格 U+3000）
        cleaned = "".join(ch for ch in cleaned if not ch.isspace())
        return cleaned.strip()

    @staticmethod
    def _find_account_by_token(user_data: dict, token: str) -> dict | None:
        """按 token 找出已存在的账号"""
        for acc in SklandPlugin._get_accounts(user_data):
            if acc.get("token") == token:
                return acc
        return None

    @staticmethod
    def _count_real_users(users: dict) -> int:
        """
        统计真实用户数（排除别名记录）。

        别名记录是跨平台指向主记录的轻量条目（只有 alias 字段、无账号），
        不应计入用户总数。
        """
        return sum(
            1 for d in users.values()
            if isinstance(d, dict) and SklandPlugin._get_accounts(d)
        )

    @staticmethod
    def _find_user_by_token(users: dict, token: str, exclude: str = "") -> str:
        """
        在所有用户记录中查找持有该 token 的用户键。

        同一个人可能从不同平台（OneBot 的 QQ 号 / QQ 官方的 openid）
        分别登录，若不归一会产生两条记录、各持一份相同 token，
        列表里就出现重复账号。

        比较时**双方都做规范化**：历史数据里可能存着带隐形字符的脏 token，
        规范化后应视为同一个账号。
        """
        norm = SklandPlugin._normalize_token(token)
        for uid, data in users.items():
            if uid == exclude:
                continue
            if not isinstance(data, dict):
                continue
            for acc in SklandPlugin._get_accounts(data):
                raw = acc.get("token")
                if not raw:
                    continue
                if raw == token or SklandPlugin._normalize_token(raw) == norm:
                    return uid
        return ""

    @staticmethod
    def _merge_user_account(users: dict, target_id: str, source_id: str,
                            account: dict) -> str:
        """
        把 source_id 用户的账号合并到 target_id，并删除 source_id 记录。

        用于跨平台登录同一森空岛账号时合并记录，返回被合并掉的原用户键。
        """
        source = users.get(source_id)
        if not isinstance(source, dict):
            return source_id
        target = users.get(target_id)
        if not isinstance(target, dict):
            # 目标记录不存在（如跨平台首次登录），以 source 为基础新建
            target = {}

        # 合并平台绑定信息
        umo_map = dict(target.get("umo_by_platform") or {})
        for plat, umo in (source.get("umo_by_platform") or {}).items():
            umo_map.setdefault(plat, umo)
        target["umo_by_platform"] = umo_map
        target["umo"] = target.get("umo") or source.get("umo", "")
        target.setdefault("last_username",
                          source.get("last_username", ""))

        # 合并账号列表：按规范化 token 去重，脏 token 统一清理
        accounts = list(target.get("accounts") or [])
        for a in accounts:
            if a.get("token"):
                a["token"] = SklandPlugin._normalize_token(a["token"])
        existing = {a.get("token") for a in accounts}
        for acc in SklandPlugin._get_accounts(source):
            raw = acc.get("token")
            if not raw:
                continue
            norm = SklandPlugin._normalize_token(raw)
            if norm in existing:
                # 同一个 token：保留目标记录的状态，但补上来源的昵称
                for a in accounts:
                    if a.get("token") == norm:
                        if not a.get("nickname"):
                            a["nickname"] = acc.get("nickname", "")
                        if not a.get("game_names"):
                            a["game_names"] = acc.get("game_names") or {}
                continue
            acc["token"] = norm
            accounts.append(acc)
            existing.add(norm)
        target["accounts"] = accounts
        SklandPlugin._upsert_account(target, account)

        users[target_id] = target
        users.pop(source_id, None)
        return source_id

    @staticmethod
    def _upsert_account(user_data: dict, account: dict) -> list[dict]:
        """
        写入（或更新）一个森空岛账号，返回该用户的全部账号。

        同一个 token 视为同一个账号（更新）；不同 token 追加为新账号。
        """
        accounts = user_data.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
            # 迁移旧结构：把顶层 token 转成 accounts[0]
            legacy_token = user_data.get("token")
            if legacy_token:
                accounts.append({
                    "token": legacy_token,
                    "nickname": user_data.get("nickname", ""),
                    "game_names": user_data.get("game_names") or {},
                    "last_sign": user_data.get("last_sign") or {},
                    "game_awards": user_data.get("game_awards") or {},
                    "last_result": user_data.get("last_result") or {},
                })

        token = account.get("token")
        if not token:
            # 没有 token 的空账号不写入（如合并时只传平台信息）
            user_data["accounts"] = accounts
            return accounts

        # 按**规范化后的 token** 匹配：历史数据里可能存着带隐形字符的脏 token，
        # 若用精确比较，重复执行同一次 login 就会不断追加出「重复账号」
        norm = SklandPlugin._normalize_token(token)
        account["token"] = norm
        for acc in accounts:
            if acc.get("token") == norm:
                acc.update(account)
                break
        else:
            accounts.append(account)

        user_data["accounts"] = accounts
        # 顶层字段镜像第一个账号，兼容旧读取路径
        if accounts:
            first = accounts[0]
            user_data["token"] = first.get("token", "")
            user_data["nickname"] = first.get("nickname", "")
            user_data["game_names"] = first.get("game_names") or {}
            user_data["last_sign"] = first.get("last_sign") or {}
            user_data["game_awards"] = first.get("game_awards") or {}
            user_data["last_result"] = first.get("last_result") or {}
        return accounts

    @staticmethod
    def _remove_account(user_data: dict, token: str) -> bool:
        """按 token 移除一个账号，返回是否移除成功"""
        accounts = user_data.get("accounts")
        if not isinstance(accounts, list):
            accounts = SklandPlugin._get_accounts(user_data)
        remaining = [a for a in accounts if a.get("token") != token]
        if len(remaining) == len(accounts):
            return False
        user_data["accounts"] = remaining
        if remaining:
            first = remaining[0]
            user_data["token"] = first.get("token", "")
            user_data["nickname"] = first.get("nickname", "")
            user_data["game_names"] = first.get("game_names") or {}
            user_data["last_sign"] = first.get("last_sign") or {}
            user_data["game_awards"] = first.get("game_awards") or {}
            user_data["last_result"] = first.get("last_result") or {}
        else:
            for key in ("token", "nickname", "game_names", "last_sign",
                        "game_awards", "last_result"):
                user_data.pop(key, None)
        return True

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
        await self._migrate_sign_time()
        config = self._get_config()
        if config.get("auto_sign_enabled", False):
            self._start_auto_sign_job(
                config.get("auto_sign_hour", 8),
                config.get("auto_sign_minute", 30),
            )
            if config.get("push_enabled", True):
                self._start_auto_push_job(
                    config.get("push_hour", 9),
                    config.get("push_minute", 0),
                )
        if not self.scheduler.running:
            self.scheduler.start()

    async def _migrate_sign_time(self):
        """
        把签到时间迁移到「签到与推送分离」的新默认值。

        AstrBot 的配置文件是「首次写入后不再回填默认值」的：老实例里
        auto_sign_hour/minute 仍然是 0/30 或上一版的 9/00，只改 schema 不会生效。
        这里只在检测到**恰好是历史默认值**时改写，不动用户自己设置过的时间。

        用一次性标记位收口：迁移完成后写入 _sign_split_migrated，
        之后用户把签到时间手动调成 9:00 也不会再被改回去。
        """
        try:
            if self.config.get("_sign_split_migrated", False):
                return

            hour = self.config.get("auto_sign_hour", 8)
            minute = self.config.get("auto_sign_minute", 30)
            changed = False

            # 历史默认值：最早的 0:30，以及 1.6.0 的 9:00（签到推送合一）
            if (hour, minute) in ((0, 30), (9, 0)):
                self.config["auto_sign_hour"] = 8
                self.config["auto_sign_minute"] = 30
                changed = True
                logger.info(
                    "森空岛：签到时间已从 "
                    f"{hour:02d}:{minute:02d} 迁移到 08:30（推送独立为 09:00）"
                )

            # 补齐新增的推送时间配置，老实例没有这两个键
            if "push_hour" not in self.config:
                self.config["push_hour"] = 9
                changed = True
            if "push_minute" not in self.config:
                self.config["push_minute"] = 0
                changed = True
            if "push_enabled" not in self.config:
                self.config["push_enabled"] = True
                changed = True

            self.config["_sign_split_migrated"] = True
            changed = True

            if changed:
                saver = getattr(self.config, "save_config", None)
                if callable(saver):
                    saver()
        except Exception as e:
            logger.warning(f"森空岛：迁移签到时间失败（不影响启动）: {e}")

    async def terminate(self):
        """插件卸载"""
        if self.scheduler.running:
            self.scheduler.shutdown()
        await self.api.close()
        logger.info("森空岛签到插件已卸载")

    # ==================== Auto Sign-In ====================

    def _start_auto_sign_job(self, hour: int = 8, minute: int = 30):
        """
        启动自动签到定时任务（只签到、只落库，不推送）。

        签到状态在每日 0 点随日期自然重置（last_sign 存的是日期），
        实际签到安排在 0 点之后（默认 8:30），避开整点的接口高峰。
        推送由独立的 _start_auto_push_job 负责，两个时间互不影响。
        """
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        trigger = CronTrigger(hour=hour, minute=minute)
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
        logger.info(f"森空岛自动签到任务已启动，每天 {hour:02d}:{minute:02d} 执行（仅落库）")

    def _start_auto_push_job(self, hour: int = 9, minute: int = 0):
        """
        启动每日签到结果推送定时任务。

        与签到任务完全解耦：到点后读取库里已落库的签到结果，
        按 /skd 同款卡片推送给用户。因此推送时间可以晚于签到时间，
        也可以配成与签到同一时刻（此时两者仍各自独立触发）。
        """
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        trigger = CronTrigger(hour=hour, minute=minute)
        try:
            self.scheduler.remove_job("skland_auto_push")
        except Exception:
            pass

        self.scheduler.add_job(
            self._push_daily_results,
            trigger=trigger,
            id="skland_auto_push",
            misfire_grace_time=3600,
        )
        logger.info(f"森空岛签到结果推送任务已启动，每天 {hour:02d}:{minute:02d} 推送")

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

    @staticmethod
    def _account_signed_today(account: dict, today: str) -> bool:
        """
        判断该账号今天是否已签到。

        last_sign 里存的是签到日期（如 "2026-10-20"），跨天自然不相等，
        因此每日 0 点后状态自动失效，无需显式重置。
        """
        last_sign = account.get("last_sign") or {}
        return any(v == today for v in last_sign.values())

    async def _auto_sign_all_users(self):
        """
        为所有已注册用户执行自动签到，结果落库。

        这一步**不做任何推送**：推送由独立的 _push_daily_results 在
        另一个时间点读取落库结果后发出，两者时间互不影响。
        """
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

        for user_id, user_data in users.items():
            if max_delay > 0:
                delay = random.uniform(0, max_delay)
                logger.info(f"处理下一个用户前等待 {delay:.2f} 秒")
                await asyncio.sleep(delay)

            accounts = self._get_accounts(user_data)
            if not accounts:
                continue

            all_bodies = []
            failed_accounts = []

            for account in accounts:
                token = account.get("token")
                if not token:
                    continue
                # 今日已签到则跳过，不重复请求接口
                if self._account_signed_today(account, today):
                    logger.info(
                        f"用户 {user_id} 账号 {account.get('nickname') or '?'} "
                        f"今日已签到，跳过"
                    )
                    continue
                # 先给个兜底昵称：请求失败时 nickname 还没赋值，
                # 直接在 except 里引用会抛 UnboundLocalError，
                # 连带整个自动签到任务崩溃
                nickname = ""
                try:
                    results, nickname = await self.api.do_full_sign_in(token)
                    self._mark_signed(account, results, today)
                    self._store_game_names(account, results)
                    self._store_game_awards(account, results)

                    body = self._format_sign_status(results, nickname)
                    ok = any(self._is_signed_today(r) for r in results) if results else False

                    # 结果始终落库，供拉取模型与推送任务读取
                    account["last_result"] = {"time": stamp, "text": body, "ok": ok}
                    if nickname:
                        account["nickname"] = nickname

                    all_bodies.append(body)
                    logger.info(f"用户 {user_id} ({nickname}) 自动签到完成")
                except Exception as e:
                    logger.error(
                        f"用户 {user_id} 账号 {nickname or account.get('nickname') or '?'} "
                        f"签到失败: {e}")
                    account["last_result"] = {
                        "time": stamp, "text": f"签到失败：{e}", "ok": False}
                    failed_accounts.append(f"{account.get('nickname') or '账号'}: {e}")

            if not all_bodies and not failed_accounts:
                continue

            # 顶层镜像第一个账号（兼容旧读取路径）
            self._upsert_account(user_data, accounts[0])
            users[user_id] = user_data

        await self.put_kv_data("users", users)
        logger.info("自动签到执行完毕，结果已落库，等待推送时间点发送")

    async def _push_daily_results(self):
        """
        每日签到结果推送：读取库里已落库的签到结果，按 /skd 同款卡片推送给用户。

        与签到任务是两个独立的定时任务，因此：
        - 推送时间可以晚于签到时间（默认 8:30 签到、9:00 推送）
        - 签到失败重试也不会影响已经排好的推送
        - 插件在推送时点重启后仍能从库里拿到完整结果
        """
        config = self._get_config()
        if not config.get("auto_sign_enabled", False):
            logger.info("自动签到已关闭，跳过推送")
            return
        if not config.get("push_enabled", True):
            logger.info("签到结果推送已关闭，跳过推送")
            return

        users = await self.get_kv_data("users", {})
        if not users:
            logger.info("没有已注册的用户，跳过签到结果推送")
            return

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        groups = await self.get_kv_data("groups", {})
        broadcast_groups: dict[str, list[str]] = {}
        pushed = 0

        for user_id, user_data in users.items():
            accounts = self._get_accounts(user_data)
            if not accounts:
                continue

            # 只认今天落库的结果：last_result.time 是 ISO 时间串，
            # 取前 10 位比对日期，避免把昨天的结果当成今天的推出去
            todays = [a for a in accounts
                      if str((a.get("last_result") or {}).get("time") or "")[:10] == today]
            if not todays:
                continue

            bodies = [str((a.get("last_result") or {}).get("text") or "")
                      for a in todays]
            bodies = [b for b in bodies if b]
            failures = [b for b in bodies if b.startswith("签到失败")]
            bodies = [b for b in bodies if not b.startswith("签到失败")]

            if bodies:
                # 优先推送与 /skd 同款的签到卡片图片，渲染不可用时回退纯文本
                card_path = None
                if config.get("push_card_image", True):
                    card_path = self._render_card(
                        self._private_card_rows(user_data, today))
                if card_path:
                    await self._push_to_user(
                        user_id, user_data, "", image_path=card_path)
                else:
                    message = "🎮森空岛自动签到结果\n\n" + "\n\n".join(bodies)
                    await self._push_to_user(user_id, user_data, message)
                pushed += 1

            if failures:
                await self._push_to_user(
                    user_id, user_data,
                    "⚠️ 自动签到失败\n" + "\n".join(failures)
                    + "\n请使用 /skdlogin 重新登录",
                )

            if bodies:
                display = self._display_name(user_data.get("nickname", ""), user_data)
                for gkey in self._find_user_groups(groups, user_id):
                    broadcast_groups.setdefault(gkey, []).append(display)

        await self.put_kv_data("users", users)

        if config.get("group_broadcast_enabled", False):
            await self._broadcast_group_summary(broadcast_groups)
        logger.info(f"签到结果推送完毕，共推送 {pushed} 人")

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

    async def _push_to_user(self, user_id: str, user_data: dict, message: str,
                            image_path: str | None = None):
        """
        按各平台能力推送私聊消息；不支持时静默降级（结果已落库）。

        image_path 非空时推送结果图片（与 /skd 指令同款卡片），
        消息体只发图片，不再附带纯文本。
        """
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
                if image_path:
                    chain = MessageChain().file_image(image_path)
                else:
                    chain = MessageChain().message(message)
                await self.context.send_message(umo, chain)
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

    def _private_card_rows(self, user_data: dict, today: str = "") -> list:
        """
        把私聊的落库结果转成卡片行数据（合并该用户的所有账号）。

        与群聊卡片共用 _build_user_row，保证同一个人
        在 /skd 私聊、/skd 群聊、每日推送里看到的数据完全一致。
        """
        today = today or datetime.now().strftime("%Y-%m-%d")
        row = self._build_user_row(user_data, today)
        if row:
            return [row]
        # 没有可展示的角色名时，给一行只有名字的兜底数据
        return [{
            "name": self._display_name(user_data.get("nickname", ""), user_data),
            "ak_names": [], "ef_names": [],
            "ak_name": "", "ef_name": "",
            "arknights": "none", "endfield": "none",
            "ak_awards": [], "ef_awards": [],
            "failed": False,
        }]

    def _render_stored_result(self, user_data: dict) -> str:
        """把落库的签到结果渲染成查询回复（拉取模型），展示该用户所有账号"""
        accounts = self._get_accounts(user_data)
        fallback = self._display_name(user_data.get("nickname", ""), user_data)
        if not accounts:
            return "📋 森空岛签到\n\n暂无签到记录，发送 /skdlogin <token> 开始绑定"

        lines = ["📋 森空岛签到", ""]
        for idx, account in enumerate(accounts, 1):
            last = account.get("last_result") or {}
            text = last.get("text") or ""
            names = account.get("game_names") or {}
            nick = self._display_name(account.get("nickname", ""), user_data)

            stamp = last.get("time") or ""
            try:
                stamp = datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                stamp = ""

            # 多个账号时加标题区分
            if len(accounts) > 1:
                lines.append(f"【账号 {idx}】{nick}")
            else:
                lines.append(nick)

            if not text:
                lines.append("    暂无签到记录")
                continue

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
                lines.append(f"    {game}　{names.get(key) or nick}")
                if detail:
                    lines.append(f"      {detail}")
            if stamp:
                lines.append(f"    {stamp}")
            lines.append("")
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
        "skdmerge": "skdmerge",
        "合并重复": "skdmerge",
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
            async for res in self.skdlogout(event, arg):
                yield res
        elif target == "skdlogin":
            async for res in self.skdlogin(event, arg):
                yield res
        elif target == "skdmerge":
            async for res in self.skdmerge(event):
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
            "6. /skdmerge 合并重复账号记录（仅管理员）\n"
            "注：token 等同账号密码，群聊中发送后插件会自动撤回该条消息\n"
            "多账号：多次 /skdlogin 可绑定多个森空岛账号，\n"
            "     列表中同一游戏的角色名用 / 分隔；\n"
            "     /skdlogout <token> 可单独移除某个账号"
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

    @filter.command("skdmerge")
    async def skdmerge(self, event: AstrMessageEvent):
        """
        检测并合并重复账号记录。

        同一个人可能从不同平台（OneBot 的 QQ 号 / QQ 官方的 openid）
        分别登录，若未归一会产生两条记录、各持一份相同 token，
        签到列表里就会出现重复账号。本指令把相同 token 的记录合并。
        """
        if not event.is_admin():
            yield event.plain_result("该指令仅管理员可用")
            return

        users = await self.get_kv_data("users", {})
        groups = await self.get_kv_data("groups", {})

        # 按「规范化后的 token」归组：脏 token（含隐形字符）规范化后
        # 应与干净 token 归为同一组，否则存量重复清理不出来
        token_owner: dict[str, str] = {}
        dup_groups: dict[str, list[str]] = {}
        dirty_tokens = 0
        for uid, data in users.items():
            if not isinstance(data, dict):
                continue
            # 就地规范化（旧结构也能写回）
            dirty_tokens += self._normalize_accounts_inplace(data)
            for acc in self._get_accounts(data):
                tok = acc.get("token")
                if not tok:
                    continue
                norm = self._normalize_token(tok)
                if norm in token_owner:
                    dup_groups.setdefault(norm, [token_owner[norm]]).append(uid)
                else:
                    token_owner[norm] = uid

        if not dup_groups:
            msg = (
                f"当前共 {self._count_real_users(users)} 个用户记录，"
                f"{len(token_owner)} 个不同的森空岛账号"
            )
            if dirty_tokens:
                await self.put_kv_data("users", users)
                msg += f"\n🧹 已规范化 {dirty_tokens} 个含隐形字符的 token"
            yield event.plain_result(f"✅ 未发现重复账号记录\n{msg}")
            return

        lines = [
            f"🔍 发现 {len(dup_groups)} 组重复账号：",
            "═══════════════",
        ]
        merged = 0
        for tok, uids in dup_groups.items():
            # 保留第一个，其余合并进去
            keep = uids[0]
            for other in uids[1:]:
                self._merge_user_account(users, keep, other, {})
                merged += 1
                # 同步群成员列表
                for meta in groups.values():
                    if not isinstance(meta, dict):
                        continue
                    members = meta.get("members") or []
                    if other in members:
                        members.remove(other)
                        if keep not in members:
                            members.append(keep)
            names = "、".join(
                (users.get(u, {}).get("nickname")
                 or users.get(u, {}).get("last_username") or u)
                for u in uids if u in users
            ) or "、".join(uids)
            lines.append(f"  • {names}（token {tok[:6]}***）")
            lines.append("    → 已合并为 1 条记录")

        await self.put_kv_data("users", users)
        await self.put_kv_data("groups", groups)
        logger.info(f"skdmerge: 合并了 {merged} 条重复用户记录")
        lines.append("═══════════════")
        lines.append(f"✅ 已合并 {merged} 条重复记录，当前 {self._count_real_users(users)} 个用户")

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
        # 记录该用户本次登录前是否已有绑定账号：
        # 「新的人」= 首次绑定，而不是给已有账号补一次签到状态
        had_account_before = bool(self._get_accounts(users.get(user_id) or {}))

        # 规范化 token：去掉复制粘贴可能带入的隐形字符，
        # 否则同一个 token 会被当成两个不同账号
        token = self._normalize_token(token)
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
        if (user_id not in users and max_users > 0
                and self._count_real_users(users) >= max_users):
            # 消息里已含 token，拒绝绑定也要撤回，防止 token 留在群里
            yield event.plain_result(
                f"❌ 绑定失败：已达到最大用户数限制（{max_users}个）\n请联系管理员调整配置"
                + await self._recall_and_note(event, is_group)
            )
            return

        # 全局唯一：一个 token 只应存在于一条用户记录中。
        # 只支持 QQ 官方平台，sender_id 即稳定的 openid，
        # 因此「token 已绑在别的用户下」即可断定是他人冒用/共享，直接拒绝。
        # 仅当管理员显式配置 merge 时才自动复用（用于清理历史重复数据）。
        duplicate_of = self._find_user_by_token(users, token, exclude="")
        conflict_action = config.get("token_conflict", "reject")
        merged_note = ""
        if duplicate_of and duplicate_of != user_id:
            if conflict_action != "merge":
                owner = users.get(duplicate_of) or {}
                owner_nick = (owner.get("nickname")
                              or owner.get("last_username") or "其他用户")
                logger.warning(
                    f"用户 {user_id} 尝试绑定已属于 {duplicate_of}"
                    f"（{owner_nick}）的 token，已拒绝"
                )
                yield event.plain_result(
                    "❌ 绑定失败：该森空岛账号已被其他用户绑定\n\n"
                    f"• 当前绑定者：{owner_nick}\n"
                    "• 如果这是你自己的账号，请用当前 QQ 重新登录，\n"
                    "  或联系管理员使用 /skdmerge 合并记录\n"
                    "• 如果是账号共享，请让各自使用自己的 token"
                    # 拒绝时也要撤回，token 已经暴露在群里了
                    + await self._recall_and_note(event, is_group)
                )
                return

            logger.info(
                f"用户 {user_id} 提交的 token 已绑定在 {duplicate_of}，"
                f"按配置复用该记录"
            )
            user_id = duplicate_of
            merged_note = "\n\n🔗 该账号已绑定过，已复用原记录（不会重复）"
        elif duplicate_of == user_id:
            merged_note = "\n\n🔗 该账号此前已绑定，本次为更新签到状态"

        yield event.plain_result("正在登录并签到，请稍候...")
        try:
            results, nickname = await self.api.do_full_sign_in(token)
            now = datetime.now()

            # 取既有记录，避免覆盖丢失 token / last_sign 等字段
            user_data = users.get(user_id) or {}
            user_data["last_username"] = user_name
            user_data["bound_at"] = user_data.get("bound_at") or now.isoformat()
            user_data["platform_name"] = platform

            umo_map = dict(user_data.get("umo_by_platform") or {})
            umo_map[platform] = event.unified_msg_origin
            user_data["umo_by_platform"] = umo_map
            user_data["umo"] = event.unified_msg_origin

            # 每个 token 是一个独立账号：同 token 视为更新，新 token 追加
            is_new_account = self._find_account_by_token(user_data, token) is None
            account = {
                "token": token,
                "nickname": nickname,
                "last_sign": {},
                "game_names": {},
                "game_awards": {},
                "last_result": {},
            }
            self._mark_signed(account, results, now.strftime("%Y-%m-%d"))
            self._store_game_names(account, results)
            self._store_game_awards(account, results)

            body = self._format_sign_status(results, nickname)
            account["last_result"] = {
                "time": now.isoformat(timespec="seconds"),
                "text": body,
                "ok": any(self._is_signed_today(r) for r in results) if results else False,
            }
            accounts = self._upsert_account(user_data, account)
            account_count = len(accounts)

            users[user_id] = user_data

            # merge 模式下当前 sender 可能另有一条记录，需要合并删除，
            # 否则会出现两条用户记录；同时把群成员里的旧键换掉
            sender_id = event.get_sender_id()
            if (sender_id and sender_id != user_id and sender_id in users):
                other = users.get(sender_id) or {}
                if not other.get("alias"):
                    for acc in self._get_accounts(other):
                        if acc.get("token"):
                            self._upsert_account(user_data, dict(acc))
                    users.pop(sender_id, None)
                    groups = await self.get_kv_data("groups", {})
                    for meta in groups.values():
                        if not isinstance(meta, dict):
                            continue
                        members = meta.get("members") or []
                        if sender_id in members:
                            members.remove(sender_id)
                            if user_id not in members:
                                members.append(user_id)
                            meta["members"] = members
                    await self.put_kv_data("groups", groups)

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
            recall_note = await self._recall_and_note(event, is_group)

            notice = "" if self._can_push(platform) else \
                "\n\n（当前平台不支持主动推送，将在你查询时展示结果）"
            if account_count > 1:
                head = f"登录成功！（已绑定 {account_count} 个森空岛账号）"
            elif is_new_account:
                head = "登录成功！"
            else:
                head = "登录成功！（已更新该账号的签到状态）"
            yield event.plain_result(
                f"{head}\n{body}{notice}{recall_note}{merged_note}")

            # 新的人登录成功后，补发一张与 /skd 同款的签到结果图片，
            # 让用户立刻看到自己今天的签到状态与奖励
            if not had_account_before and config.get("push_card_image", True):
                card_path = self._render_card(
                    self._private_card_rows(user_data, now.strftime("%Y-%m-%d")))
                if card_path:
                    yield event.chain_result([Comp.Image.fromFileSystem(card_path)])
                else:
                    logger.warning("登录成功卡片渲染不可用，已仅返回文本结果")
        except Exception as e:
            logger.error(f"skdlogin失败: {e}")
            # 失败时也要撤回，防止 token 留在群里
            recall_note = await self._recall_and_note(event, is_group)
            yield event.plain_result(f"登录失败: {e}{recall_note}")

    # 官方错误码 → 用户可读提示
    # 40062003 是线上最常见的情况：机器人不是群管理员，撤不掉普通成员的消息。
    # 这是官方硬限制，唯一的解法是把机器人设为群管理员，因此提示里直接给出操作指引。
    _RECALL_ERROR_HINTS = {
        "40062003": "机器人不是群管理员，无权撤回他人消息。"
                    "请群主在群聊设置里把机器人添加为管理员，否则无法撤回",
        "40064004": "消息发送已超过 2 分钟，官方接口不允许撤回",
        "40061001": "请求参数无效",
        "50065001": "撤回失败，请稍后重试",
    }

    async def _recall_and_note(self, event: AstrMessageEvent, is_group: bool) -> str:
        """
        撤回含 token 的那条消息，并返回给用户的提示文案。

        群聊场景下，只要用户提交过 token，无论登录成功、失败还是被拒绝，
        都必须撤回原消息 —— token 等同账号密码，留在群里会被他人看到。
        各退出路径统一走这里，避免遗漏。
        """
        if not is_group:
            return ""
        recalled, reason = await self._recall_message(event)
        if recalled:
            return "\n\n🔒 已撤回你发送的 token 消息"
        # 撤回失败意味着 token 仍暴露在群里，提示要说清严重性和即时动作
        return (
            f"\n\n⚠️ 撤回失败{reason}\n"
            "🔴 你的 token 仍显示在群里，请立即手动撤回该消息！\n"
            "💡 建议改用私聊发送 /skdlogin，私聊内容不会留在群里"
        )

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
            # 40062003 是权限问题，日志里直接给出解法，避免反复排查。
            # 该问题不会自愈，同一群体内只详细提示一次，避免刷屏。
            fix = ""
            if "40062003" in text:
                fix = "。解法：把机器人设为该群管理员（群聊设置 → 管理 → 添加管理员）"
                denied = getattr(self, "_recall_denied_groups", None)
                if denied is None:
                    denied = set()
                    self._recall_denied_groups = denied
                if group_openid in denied:
                    logger.warning(
                        f"[森空岛] 群 {group_openid} 撤回仍无权限"
                        "（机器人非群管理员），已提示过，此处从略"
                    )
                    return False, reason
                denied.add(group_openid)
            logger.warning(
                f"[森空岛] 撤回群消息失败（群 {group_openid} 消息 {message_id}）："
                f"{e}{reason}{fix}。token 仍暴露在群里，已提示用户手动撤回"
            )
            return False, reason

    @filter.command("skdlogout")
    async def skdlogout(self, event: AstrMessageEvent, token: str = ""):
        """
        登出并清除绑定信息。

        不带参数：解除当前平台的绑定（保留账号），其他平台仍可推送。
        带 token：移除指定的森空岛账号；该平台没有其他账号时一并解除绑定。
        """
        users = await self.get_kv_data("users", {})
        user_id = self._resolve_user_key(users, event.get_sender_id())
        if user_id not in users:
            yield event.plain_result("您尚未绑定森空岛账号")
            return

        platform = self._detect_platform(event)
        user_data = users[user_id]
        accounts = self._get_accounts(user_data)
        token = (token or "").strip()

        # 指定 token：只移除该账号
        if token:
            if not any(a.get("token") == token for a in accounts):
                yield event.plain_result("未找到该 token 对应的账号")
                return
            self._remove_account(user_data, token)
            left = self._get_accounts(user_data)
            groups = await self.get_kv_data("groups", {})
            if not left:
                # 没有账号了，彻底解绑
                self._unbind_user(users, groups, user_id)
            else:
                users[user_id] = user_data
            await self.put_kv_data("groups", groups)
            await self.put_kv_data("users", users)
            yield event.plain_result(
                f"已移除该森空岛账号，剩余 {len(left)} 个账号"
                if left else "已移除该森空岛账号并解除绑定"
            )
            return

        # 不带 token：解除当前平台绑定
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
            groups = await self.get_kv_data("groups", {})
            self._unbind_user(users, groups, user_id)
            await self.put_kv_data("groups", groups)
            await self.put_kv_data("users", users)
            yield event.plain_result("已退出登录并清除绑定信息")

    @staticmethod
    def _unbind_user(users: dict, groups: dict, user_id: str) -> None:
        """彻底解除一个用户的绑定：从 users 移除，并从所有群成员列表中移除"""
        users.pop(user_id, None)
        for meta in groups.values():
            if isinstance(meta, dict) and user_id in (meta.get("members") or []):
                meta["members"].remove(user_id)

    @staticmethod
    def _merge_state(states: list[bool]) -> str:
        """
        合并多个账号在某个游戏上的签到状态。

        返回 "all"（全部已签）/ "part"（部分已签）/ "none"（都没签）。
        """
        if not states:
            return "none"
        signed = sum(1 for s in states if s)
        if signed == 0:
            return "none"
        if signed == len(states):
            return "all"
        return "part"

    def _collect_game_entries(self, accounts: list, game_key: str, game_name: str,
                              user_data: dict, today: str = "") -> tuple[list[str], list[bool],
                                                      list[str]]:
        """
        收集多个账号在某游戏上的角色名、签到状态与奖励。

        同名角色会合并：同一个人可能有多��森空岛账号，它们绑定的
        方舟角色名可能相同（如不同服务器/渠道服上的同名角色）。
        显示时同名只出现一次，状态取「任一已签即为已签」，奖励去重合并。

        today 为空时按「历史上签到过」判定；传入日期时只认当天的签到记录
        （last_sign 存的是日期，跨天自动失效）。

        返回 (角色名列表, 状态列表, 奖励列表)
        """
        today = today or datetime.now().strftime("%Y-%m-%d")
        names: list[str] = []
        states: list[bool] = []
        awards: list[str] = []

        for account in accounts:
            last_sign = account.get("last_sign") or {}
            names_map = account.get("game_names") or {}
            awards_map = account.get("game_awards") or {}
            nick = self._display_name(account.get("nickname", ""), user_data)
            signed = last_sign.get(game_key) == today
            name = names_map.get(game_name) or (nick if signed else "")
            if not name:
                continue
            items = [str(a) for a in (awards_map.get(game_name) or []) if a]

            if name in names:
                idx = names.index(name)
                # 状态取并集：任一账号签了就算这个角色签了
                states[idx] = states[idx] or signed
                for it in items:
                    if it not in awards:
                        awards.append(it)
            else:
                names.append(name)
                states.append(signed)
                for it in items:
                    if it not in awards:
                        awards.append(it)

        return names, states, awards

    @staticmethod
    def _normalize_state(state) -> str:
        """归一化状态：兼容 bool（旧格式）与 str（新格式）"""
        if isinstance(state, bool):
            return "all" if state else "none"
        if isinstance(state, str) and state in ("all", "part", "none"):
            return state
        return "none"

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
        # 别名记录（跨平台指向主记录）不计入用户数
        real_users = {uid: d for uid, d in users.items()
                      if isinstance(d, dict) and self._get_accounts(d)}
        signed_today = sum(
            1 for d in real_users.values()
            if any(v == today for v in (d.get("last_sign") or {}).values())
        )
        total_users = len(real_users)

        lines = [
            "📊 森空岛签到用户统计",
            "═══════════════════",
            f"📝 总注册用户: {total_users} 人",
            f"✅ 今日已签到: {signed_today} 人",
            f"📉 未签到用户: {total_users - signed_today} 人",
            f"🔌 当前平台: {platform}",
        ]

        if event.is_admin():
            if max_users > 0:
                lines.append(f"🎯 最大限制: {max_users} 人")
                lines.append(f"🆓 剩余名额: {max(0, max_users - self._count_real_users(users))} 人")
            lines.append(f"📤 推送模式: {config.get('push_mode', 'auto')}"
                         f"（本平台可推送: {self._can_push(platform)}）")

            if not self._is_group(event):
                if group_stats:
                    lines.append("\n📌 群聊分布（仅代表群内同玩的数量）:")
                    lines.extend(group_stats)
                if self._count_real_users(users) <= 20:
                    lines.append("\n👤 用户列表:")
                    for uid, d in users.items():
                        # 跳过别名记录（无账号，仅指向主记录）
                        if not isinstance(d, dict) or not self._get_accounts(d):
                            continue
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
        # 群成员统一存「主记录键」，与 _find_user_groups 的查询口径一致
        member_key = self._resolve_user_key(users_data, sender_id)
        if member_key in users_data and member_key not in members:
            members.append(member_key)
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
            accounts = self._get_accounts(user_data)
            if not accounts:
                continue

            failed = False
            if live:
                # 逐个账号记录失败：单个账号失败不应隐藏该用户的其他账号
                failed_accounts = 0
                queried_accounts = 0
                for account in accounts:
                    token = account.get("token")
                    if not token:
                        continue
                    # 今日已签到就不再请求接口，直接读落库结果。
                    # last_sign 存的是日期，跨天自动失效，等价于每日 0 点重置。
                    if self._account_signed_today(account, today):
                        continue
                    queried_accounts += 1
                    try:
                        results, live_nick = await self.api.do_full_sign_in(token)
                        if live_nick:
                            account["nickname"] = live_nick
                        self._mark_signed(account, results, today)
                        self._store_game_names(account, results)
                        self._store_game_awards(account, results)
                    except Exception as e:
                        logger.warning(f"群聊实时查询用户 {uid} 失败: {e}")
                        failed_accounts += 1
                # 只有当「需要查询的账号全部失败」且「没有任何账号有有效签到结果」
                # 时，才把整个用户标记为失败；否则仍展示各账号已有的签到状态
                if queried_accounts > 0 and failed_accounts == queried_accounts:
                    has_valid = any(
                        self._account_signed_today(a, today) for a in accounts
                    )
                    failed = not has_valid
                users_data[uid] = user_data

            row = self._build_user_row(user_data, today, failed)
            if row:
                rows.append(row)

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

    def _build_user_row(self, user_data: dict, today: str,
                        failed: bool = False) -> dict | None:
        """
        把单个用户转成一行卡片数据；没有可展示的角色名时返回 None。

        汇总该用户所有账号在两个游戏上的昵称、签到状态与奖励。
        同名角色会合并（不同账号/服务器可能重名），避免列表出现重复。
        """
        accounts = self._get_accounts(user_data)
        if not accounts:
            return None

        ak_names, ak_states, ak_awards = self._collect_game_entries(
            accounts, "arknights", "arknights", user_data, today)
        ef_names, ef_states, ef_awards = self._collect_game_entries(
            accounts, "endfield", "endfield", user_data, today)

        if not ak_names and not ef_names:
            return None

        return {
            "name": self._display_name(
                accounts[0].get("nickname", ""), user_data),
            # 各游戏的角色名列表：同游戏内用「/」分隔多个账号
            "ak_names": ak_names,
            "ef_names": ef_names,
            # 兼容旧的字符串字段
            "ak_name": "/".join(ak_names),
            "ef_name": "/".join(ef_names),
            # 状态：全签 ✓ / 部分签 ◐ / 全未签 ✗
            "arknights": self._merge_state(ak_states),
            "endfield": self._merge_state(ef_states),
            # 奖励按游戏归属，卡片里分开两行显示
            "ak_awards": ak_awards,
            "ef_awards": ef_awards,
            "failed": failed,
        }

    def _build_user_rows(self, users_data: dict, today: str) -> list:
        """
        把用户数据转成卡片行（与 /skd 群聊完全同构）。

        自动签到推送与登录成功推送都复用这里，保证推送出来的图
        和群里敲 /skd 看到的图一模一样。
        """
        rows = []
        for user_data in users_data.values():
            row = self._build_user_row(user_data, today)
            if row:
                rows.append(row)
        return rows

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
    # 角色名列宽度：多账号时会有多个名字用 / 分隔，需要留足空间
    _NAME_MAX = 30

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

            ak_names = r.get("ak_names")
            ef_names = r.get("ef_names")
            if ak_names is None and ef_names is None:
                # 兼容旧结构：字符串字段
                ak_names = [r.get("ak_name") or ""] if r.get("ak_name") else []
                ef_names = [r.get("ef_name") or ""] if r.get("ef_name") else []
            ak_names = [n for n in ak_names if n]
            ef_names = [n for n in ef_names if n]
            ak_name = self._truncate("/".join(ak_names), self._NAME_MAX)
            ef_name = self._truncate("/".join(ef_names), self._NAME_MAX)
            if not ak_name and not ef_name:
                # 两个游戏都没有昵称时，至少显示兜底名
                ak_name = self._truncate(r.get("name", ""), self._NAME_MAX)

            ak_state = self._normalize_state(r.get("arknights", "none"))
            ef_state = self._normalize_state(r.get("endfield", "none"))
            if ak_state in ("all", "part"):
                ak_count += 1
            if ef_state in ("all", "part"):
                ef_count += 1
            if ak_state != "none" or ef_state != "none":
                signed_count += 1

            # 只展示实际绑定（有昵称）的游戏，未绑定的不占列
            if ak_name and ef_name:
                games = "明日方舟/终末地"
                states = f"{self._state_mark(ak_state)}/{self._state_mark(ef_state)}"
                # 两个游戏的角色名之间用「｜」分隔，避免与账号间的「/」混淆
                names = f"{ak_name}｜{ef_name}"
            elif ef_name:
                games = "终末地"
                states = self._state_mark(ef_state)
                names = ef_name
            else:
                games = "明日方舟"
                states = self._state_mark(ak_state)
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

    @staticmethod
    def _state_mark(state) -> str:
        """状态标记：全部已签 ✓ / 部分已签 ◐ / 未签 ✗"""
        return {"all": "✓", "part": "◐", "none": "✗"}.get(
            SklandPlugin._normalize_state(state), "✗")