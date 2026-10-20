"""
签到卡片图片渲染

生成群聊/私聊的签到状态卡片图片。依赖 Pillow 与中文字体，
两者任一缺失时调用方应回退到纯文本输出。
"""

import os
import tempfile
from typing import Optional

# 中文字体候选路径（覆盖 macOS / Linux / Windows 常见位置）
FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

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


def _find_font() -> Optional[str]:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


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


class CardRenderer:
    """签到状态卡片渲染器"""

    def __init__(self, theme: str = "dark"):
        self.theme = THEME_LIGHT if theme == "light" else THEME_DARK
        self.font_path = _find_font()
        self.bold_index = _pick_bold_index(self.font_path) if self.font_path else 0
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

        rows: [{"name": str, "arknights": bool, "endfield": bool, "awards": list[str]}]
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
            W, PAD, ROW = 720, 44, 66

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
            d.text((PAD, 34), title, font=self._font(36, True), fill=c["text"])
            d.text((PAD + 2, 82), subtitle, font=self._font(16), fill=c["muted"])

            # 右上统计：按「人」计数（与底部汇总口径一致）
            signed = sum(1 for r in rows if r.get("arknights") or r.get("endfield"))
            stat = f"{signed}/{len(rows)}"
            sw = d.textlength(stat, font=self._font(34, True))
            d.text((W - PAD - sw, 34), stat, font=self._font(34, True), fill=c["accent"])
            lw = d.textlength("已签到", font=self._font(15))
            d.text((W - PAD - lw, 78), "已签到", font=self._font(15), fill=c["muted"])

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
                    d.text((x, y + 3), "明日方舟", font=self._font(20),
                           fill=c["text"] if ak_ok else c["muted"])
                    x += 96

                if ef_name:
                    if ak_name:
                        # 斜杠
                        d.text((x - 8, y + 3), "/", font=self._font(20), fill=c["muted"])
                        x += 18
                    (self._check if ef_ok else self._cross)(d, x, y + 5, 20,
                                                          c["ok"] if ef_ok else c["bad"])
                    x += 30
                    d.text((x, y + 3), "终末地", font=self._font(20),
                           fill=c["text"] if ef_ok else c["muted"])
                    x += 96
                else:
                    x += 4

                # 角色名（两个游戏用斜杠连接）
                label = ak_name[:20] if len(ak_name) > 20 else ak_name
                if ef_name:
                    ef_short = ef_name[:20] if len(ef_name) > 20 else ef_name
                    label = f"{label} / {ef_short}" if label else ef_short
                d.text((x, y), label, font=self._font(23, True), fill=c["text"])

                # 奖励按游戏归属，各自显示在对应游戏名下方
                ak_awards = r.get("ak_awards") or []
                ef_awards = r.get("ef_awards") or []
                if not ak_awards and not ef_awards:
                    # 兼容旧数据：只有单一 awards 字段时归到方舟
                    ak_awards = r.get("awards") or []
                if ak_awards or ef_awards:
                    # 与游戏列对齐：方舟奖励在游戏名下方，终末地奖励右移
                    game_col = PAD + 58
                    ef_col = game_col + 96 + 18
                    if ak_awards:
                        d.text((game_col, y + 32),
                               "  ".join(str(a) for a in ak_awards),
                               font=self._font(15), fill=c["muted"])
                    if ef_awards:
                        d.text((ef_col, y + 32),
                               "  ".join(str(a) for a in ef_awards),
                               font=self._font(15), fill=c["muted"])

                y += ROW

            d.rectangle([PAD, H - 64, W - PAD, H - 63], fill=c["div"])
            # 统计按实际绑定的游戏计数，未绑定某游戏的不计入
            ak_c = sum(1 for r in rows
                       if r.get("arknights") and (r.get("ak_name") or r.get("name")))
            ef_c = sum(1 for r in rows
                       if r.get("endfield") and (r.get("ef_name") or r.get("ak_name")))
            d.text((PAD, H - 48), f"明日方舟 {ak_c} 人", font=self._font(19), fill=c["muted"])
            d.text((PAD + 160, H - 48), f"终末地 {ef_c} 人", font=self._font(19), fill=c["muted"])
            footer = "Created By 梅莉"
            tw = d.textlength(footer, font=self._font(15))
            d.text((W - PAD - tw, H - 45), footer, font=self._font(15), fill=c["foot"])

            fd, path = tempfile.mkstemp(prefix="skland_card_", suffix=".png")
            os.close(fd)
            img.save(path)
            return path
        except Exception:
            return None
