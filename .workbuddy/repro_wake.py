"""
复现 AstrBot 真实链路，验证 QQ 官方群里哪种写法能触发 /skd
仿真三段逻辑：
1) qq_official_platform_adapter._parse_from_qqofficial  (消息文本构造 + At 剥离)
2) pipeline/waking_check/stage.py                          (唤醒判定)
3) star/filter/command.py                                  (指令匹配)
"""
import re


# ---------- 1. 官方适配器：At 标记剥离 ----------
def strip_bot_mention_markup(content, mention_id):
    escaped_id = re.escape(mention_id)
    markup_pattern = rf'(?:<qqbot-at-user\s+id="{escaped_id}"\s*/>|<@!?{escaped_id}>)'
    return re.sub(rf"(?:[ \t]*{markup_pattern}[ \t]*)+", " ", content or "")


def build_group_msg(raw_content, bot_openid="BOT_OPENID_123"):
    """模拟适配器为群消息构造 (message_str, chain)"""
    # 适配器只把 is_you=True 的 mention 视为 @ 机器人
    bot_mentions = [{"id": bot_openid, "is_you": True, "username": "干员"}]
    bot_mention_ids = [m["id"] for m in bot_mentions]
    plain = raw_content or ""
    for mid in bot_mention_ids:
        plain = strip_bot_mention_markup(plain, mid)
    message_str = plain.strip()
    chain = [{"type": "At", "qq": bot_openid, "name": "干员"}, {"type": "Plain", "text": message_str}]
    return message_str, chain


# ---------- 2. 唤醒检查 ----------
def waking_check(message_str, chain, self_id, wake_prefixes=("/",), ignore_at_all=False):
    message_str = message_str.strip()
    is_at_or_wake = False
    stripped = message_str

    for wp in wake_prefixes:
        if message_str.startswith(wp):
            first = chain[0] if chain else None
            if (first and first["type"] == "At"
                    and str(first["qq"]) != str(self_id)
                    and str(first["qq"]) != "all"):
                break
            is_at_or_wake = True
            stripped = message_str[len(wp):].strip()
            break

    if not is_at_or_wake:
        for seg in chain:
            if seg["type"] == "At" and str(seg["qq"]) == str(self_id):
                is_at_or_wake = True
                stripped = ""
                break
    return is_at_or_wake, stripped


# ---------- 3. 指令匹配 ----------
COMMANDS = ["skd", "skdhelp", "skdlogin", "skdlogout", "skdusers"]


def command_filter(is_at_or_wake, stripped_msg, handler_params=None):
    if not is_at_or_wake:
        return False, "未唤醒"
    msg = re.sub(r"\s+", " ", stripped_msg.strip())
    for cmd in COMMANDS:
        if msg.startswith(f"{cmd} ") or msg == cmd:
            return True, f"匹配指令 {cmd}，参数={msg[len(cmd):].strip()!r}"
    return False, f"已唤醒但无匹配指令: {msg!r}"


SELF = "BOT_OPENID_123"
print("=" * 68)
print("QQ 官方群聊：不同写法能否触发 /skd")
print("=" * 68)

cases = [
    ("@机器人 skd",           '<qqbot-at-user id="BOT_OPENID_123"/> skd'),
    ("@机器人 /skd",          '<qqbot-at-user id="BOT_OPENID_123"/> /skd'),
    ("@机器人  /skd （双空格）", '<qqbot-at-user id="BOT_OPENID_123" />  /skd'),
    ("@机器人 skdlogin abc", '<qqbot-at-user id="BOT_OPENID_123"/> skdlogin abc'),
    ("/skd（不带@）",         '/skd'),
    ("skd（不带@不带/）",      'skd'),
]

for desc, raw in cases:
    ms, chain = build_group_msg(raw)
    woke, stripped = waking_check(ms, chain, SELF)
    ok, reason = command_filter(woke, stripped)
    flag = "✅ 触发" if ok else "❌ 无反应"
    print(f"\n{flag}  {desc}")
    print(f"      适配器解析 message_str = {ms!r}")
    print(f"      唤醒={'是' if woke else '否'}  剥离前缀后={stripped!r}")
    print(f"      结果: {reason}")

print("\n" + "=" * 68)
print("私聊 C2C 场景对比")
print("=" * 68)
for desc, raw in [("/skd", "/skd"), ("skd", "skd")]:
    woke, stripped = waking_check(raw, [{"type": "Plain", "text": raw}], SELF)
    # 私聊：friend_message_needs_wake_prefix 默认 False 时直接唤醒
    woke_priv = True
    ok, reason = command_filter(woke_priv, stripped)
    flag = "✅ 触发" if ok else "❌ 无反应"
    print(f"{flag}  私聊 {desc}  ->  {reason}")