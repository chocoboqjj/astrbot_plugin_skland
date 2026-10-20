"""
签到卡片图片渲染

生成群聊/私聊的签到状态卡片图片。依赖 Pillow 与中文字体，
两者任一缺失时调用方应回退到纯文本输出。

字体：随插件打包了「筑紫A丸 + Google Sans 圆润版」（fonts/ 目录），
无需依赖系统字体，各平台渲染效果一致。
"""

import glob
import os
import tempfile
from typing import Optional

# 插件目录（用于查找随插件打包的字体）
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGED_FONT_DIR = os.path.join(_PLUGIN_DIR, "fonts")

# 随插件打包的字体（放在 plugins/astrbot_plugin_skland/fonts/ 下）
_PACKAGED_FONTS = [
    "ChikugoMaruGothic-Bold.ttf",
    "NotoSansSC-Regular.otf",
    "NotoSansSC-Bold.otf",
    "NotoSansSC-Regular.ttf",
    "NotoSansSC-Bold.ttf",
]

# 用户本机安装的中文字体（打包字体缺失时的兜底）
_USER_FONT_DIRS = [
    os.path.expanduser("~/Library/Fonts"),   # macOS
    os.path.expanduser("~/.fonts"),            # Linux 用户级
    os.path.expanduser("~/.local/share/fonts"),  # Linux 用户级
    os.path.expanduser(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),  # Windows
]

# 系统级中文字体候选（最后的兜底）
_SYSTEM_FONTS = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def _find_packaged_font() -> Optional[str]:
    """查找随插件打包的字体"""
    if not os.path.isdir(_PACKAGED_FONT_DIR):
        return None
    for name in _PACKAGED_FONTS:
        path = os.path.join(_PACKAGED_FONT_DIR, name)
        if os.path.exists(path):
            return path
    # 兜底：目录下任意 ttf/otf/ttc
    for pattern in ("*.otf", "*.ttf", "*.ttc"):
        hits = sorted(glob.glob(os.path.join(_PACKAGED_FONT_DIR, pattern)))
        if hits:
            return hits[0]
    return None


def _find_user_font() -> Optional[str]:
    """查找用户本机安装的中文字体"""
    for directory in _USER_FONT_DIRS:
        if not os.path.isdir(directory):
            continue
        for pattern in ("*.ttc", "*.otf", "*.ttf"):
            for path in sorted(glob.glob(os.path.join(directory, pattern))):
                if _is_cjk_font(path):
                    return path
    return None


def _is_cjk_font(path: str) -> bool:
    """粗略判断字体是否包含中文字形（避免选到纯西文字体）"""
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(path, fontNumber=0, lazy=True)
        cmap = font.getBestCmap()
        # 「森」是卡片里必然出现的字
        has_cjk = 0x68EE in cmap
        font.close()
        return has_cjk
    except Exception:
        # fontTools 不可用时不做判断，交由调用方验证
        return True


def _find_font() -> Optional[str]:
    """
    按优先级查找中文字体：
    1. 随插件打包的字体（保证各平台渲染效果一致）
    2. 用户本机安装的中文字体
    3. 系统级字体
    """
    for finder in (_find_packaged_font, _find_user_font):
        try:
            path = finder()
        except Exception:
            path = None
        if path:
            return path
    for path in _SYSTEM_FONTS:
        if os.path.exists(path):
            return path
    return None

# 深色主题配色
THEME_DARK = {
    "bg": (22, 24, 30),
    "accent": (94, 168, 255),
    "ok": (86, 196, 126),
    "bad": (232, 106, 106),
    "muted": (128, 136, 148),
    "text": (236, 239, 243),
    "div": (46, 50, 60),
    "dot_off": (78, 84, 96),
    "foot": (90, 96, 108),
}

# 浅色主题配色
THEME_LIGHT = {
    "bg": (244, 246, 250),
    "accent": (26, 115, 232),
    "ok": (35, 158, 92),
    "bad": (214, 69, 69),
    "muted": (128, 136, 148),
    "text": (28, 32, 40),
    "div": (224, 228, 235),
    "dot_off": (176, 182, 192),
    "foot": (168, 174, 184),
}

GAME_LABELS = (("明日方舟", "arknights"), ("终末地", "endfield"))


def _pick_bold_index(font_path: str) -> int:
    """在 ttc 字体集中挑一个加粗字重，返回 index"""
    try:
        from PIL import ImageFont
    except ImportError:
        return 0
    for i in range(4):
        try:
            name = str(ImageFont.truetype(font_path, 20, index=i).getname())
        except Exception:
            break
        if "W6" in name or "Bold" in name:
            return i
    return 0


def _has_multiple_weights(font_path: str) -> bool:
    """
    判断字体是否包含多个字重。

    打包的筑紫A丸是单字重 Bold 字体，此时加粗与常规需靠渲染技巧区分。
    """
    try:
        from PIL import ImageFont
    except ImportError:
        return False
    names = set()
    for i in range(4):
        try:
            names.add(str(ImageFont.truetype(font_path, 20, index=i).getname()))
        except Exception:
            break
    return len(names) > 1


class CardRenderer:
    """签到状态卡片渲染器"""

    def __init__(self, theme: str = "dark"):
        self.theme_name = "light" if theme == "light" else "dark"
        self.theme = THEME_LIGHT if theme == "light" else THEME_DARK
        self.font_path = _find_font()
        self.bold_index = _pick_bold_index(self.font_path) if self.font_path else 0
        # 单字重字体需要用「挖细」技巧模拟轻字重
        self._has_single_weight = bool(
            self.font_path and not _has_multiple_weights(self.font_path)
        )
        self._font_cache = {}

    @property
    def available(self) -> bool:
        """是否具备渲染能力（Pillow + 中文字体齐备）"""
        if not self.font_path:
            return False
        try:
            import PIL  # noqa: F401
        except ImportError:
            return False
        return True

    def _font(self, size: int, bold: bool = False):
        from PIL import ImageFont
        key = (size, bold)
        if key not in self._font_cache:
            idx = self.bold_index if bold else 0
            try:
                self._font_cache[key] = ImageFont.truetype(self.font_path, size, index=idx)
            except Exception:
                self._font_cache[key] = ImageFont.truetype(self.font_path, size)
        return self._font_cache[key]

    def _text(self, d, xy, text: str, size: int, color, bold: bool = False,
              anchor: str = "la") -> None:
        """
        绘制文本。

        随插件打包的筑紫A丸只有一个 Bold 字重，若直接用它渲染全部文字，
        标题/正文/辅助信息的层级就拉不开了。这里对「非加粗」文本用
        背景色描边做一次「挖细」，视觉上得到轻字重的效果。
        背景为纯色，描边不会留下杂边。
        """
        font = self._font(size, bold)
        if not bold and self._has_single_weight:
            bg = self.theme["bg"]
            d.text(xy, text, font=font, fill=bg, stroke_width=1, stroke_fill=bg,
                   anchor=anchor)
        d.text(xy, text, font=font, fill=color, anchor=anchor)

    @staticmethod
    def _check(d, x, y, size, color):
        """矢量对勾，避免依赖字体符号"""
        d.line([(x, y + size * 0.52), (x + size * 0.34, y + size * 0.85),
                (x + size * 0.95, y + size * 0.12)],
               fill=color, width=max(2, size // 8), joint="curve")

    @staticmethod
    def _cross(d, x, y, size, color):
        """矢量叉号"""
        d.line([(x + size * 0.18, y + size * 0.18),
                (x + size * 0.82, y + size * 0.82)],
               fill=color, width=max(2, size // 8))
        d.line([(x + size * 0.82, y + size * 0.18),
                (x + size * 0.18, y + size * 0.82)],
               fill=color, width=max(2, size // 8))

    def render(self, rows: list, title: str = "森空岛签到",
               subtitle: str = "Skland Daily Check-in") -> Optional[str]:
        """
        渲染签到卡片。

        rows: [{"name": str, "ak_name": str, "ef_name": str,
                "arknights": bool, "endfield": bool}]
        返回临时图片路径；不可用或失败时返回 None，由调用方回退纯文本。
        """
        if not self.available:
            return None

        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return None

        try:
            c = self.theme
            W, PAD, ROW = 720, 44, 54

            # 一个人占一行，先算行数再决定画布高度
            entry_count = sum(
                1 for r in rows
                if r.get("ak_name") or r.get("ef_name") or r.get("name")
            )
            H = 150 + max(entry_count, 1) * ROW + 62

            img = Image.new("RGB", (W, H), c["bg"])
            d = ImageDraw.Draw(img)

            # 顶部强调条
            for i in range(3):
                d.rectangle([0, i, W, i + 1], fill=c["accent"])

            # 标题
            self._text(d, (PAD, 34), title, 36, c["text"], bold=True)
            self._text(d, (PAD + 2, 82), subtitle, 16, c["muted"])

            # 右上统计：按「人」计数（与底部汇总口径一致）
            signed = sum(1 for r in rows if r.get("arknights") or r.get("endfield"))
            stat = f"{signed}/{len(rows)}"
            sw = d.textlength(stat, font=self._font(34, True))
            self._text(d, (W - PAD - sw, 34), stat, 34, c["accent"], bold=True)
            lw = d.textlength("已签到", font=self._font(15))
            self._text(d, (W - PAD - lw, 78), "已签到", 15, c["muted"])

            d.rectangle([PAD, 118, W - PAD, 119], fill=c["div"])

            # 按游戏分行：一个账号可同时绑定两个游戏，角色名往往不同，
            # 因此每个游戏各占一行，名字紧跟所属游戏，避免张冠李戴。
            # 一个人一行：两个游戏用斜杠横向合并，勾叉与名字一一对应
            y = 142
            for r in rows:
                # ak_name 为空表示未绑定方舟，此时不能用 name 兜底
                ak_name = str(r.get("ak_name") or "")
                ef_name = str(r.get("ef_name") or "")
                fallback = str(r.get("name") or "")
                if not ak_name and not ef_name:
                    # 两个游戏都没绑定昵称时，至少用兜底名占一行
                    ak_name = fallback
                if not ak_name and not ef_name:
                    continue

                ak_ok = bool(r.get("arknights")) and bool(ak_name)
                ef_ok = bool(r.get("endfield")) and bool(ef_name)

                # 状态点
                if ak_ok or ef_ok:
                    d.ellipse([PAD, y + 10, PAD + 12, y + 22], fill=c["ok"])
                else:
                    d.ellipse([PAD, y + 10, PAD + 12, y + 22], fill=c["dot_off"])

                x = PAD + 28
                # 只渲染实际绑定的游戏
                if ak_name:
                    (self._check if ak_ok else self._cross)(d, x, y + 5, 20,
                                                          c["ok"] if ak_ok else c["bad"])
                    x += 30
                    self._text(d, (x, y + 3), "明日方舟", 20, c["text"] if ak_ok else c["muted"])
                    x += 96

                if ef_name:
                    if ak_name:
                        # 斜杠
                        self._text(d, (x - 8, y + 3), "/", 20, c["muted"])
                        x += 18
                    (self._check if ef_ok else self._cross)(d, x, y + 5, 20,
                                                          c["ok"] if ef_ok else c["bad"])
                    x += 30
                    self._text(d, (x, y + 3), "终末地", 20, c["text"] if ef_ok else c["muted"])
                    x += 96
                else:
                    x += 4

                # 角色名（两个游戏用斜杠连接）
                label = ak_name[:20] if len(ak_name) > 20 else ak_name
                if ef_name:
                    ef_short = ef_name[:20] if len(ef_name) > 20 else ef_name
                    label = f"{label} / {ef_short}" if label else ef_short
                self._text(d, (x, y), label, 23, c["text"], bold=True)

                # 奖励按游戏归属，各自显示在对应游戏名下方
                # 奖励道具名称不展示
                y += ROW

            d.rectangle([PAD, H - 64, W - PAD, H - 63], fill=c["div"])
            # 统计按实际绑定的游戏计数，未绑定某游戏的不计入
            ak_c = sum(1 for r in rows
                       if r.get("arknights") and (r.get("ak_name") or r.get("name")))
            ef_c = sum(1 for r in rows
                       if r.get("endfield") and (r.get("ef_name") or r.get("ak_name")))
            self._text(d, (PAD, H - 48), f"明日方舟 {ak_c} 人", 19, c["muted"])
            self._text(d, (PAD + 160, H - 48), f"终末地 {ef_c} 人", 19, c["muted"])
            footer = "Created By 梅莉"
            tw = d.textlength(footer, font=self._font(15))
            self._text(d, (W - PAD - tw, H - 45), footer, 15, c["foot"])

            fd, path = tempfile.mkstemp(prefix="skland_card_", suffix=".png")
            os.close(fd)
            img.save(path)
            return path
        except Exception:
            return None
