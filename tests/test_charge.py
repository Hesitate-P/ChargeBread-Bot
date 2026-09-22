"""今日充能指数的契约（TDD）。

设计确认过的性质：
  · 纯玩梗 —— 不影响任何面包、不写库、不动奖励公式
  · 从 `(身份, 游戏日)` **推导**：当天恒定、跨群一致、零点跟签到一起重置
  · 数值均匀 0~100
  · 按指数落进 5 档，每档有自己的语料池（评语是"充能面包特别版一言"）
  · 命令只认 `/今日充能指数`（不带斜杠也认，那是解析器本来就有的容错）
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread import commands, game  # noqa: E402
from chargebread.rules import CHARGE_TIERS, charge_tier  # noqa: E402

DAY = date(2026, 9, 22)
UID = "AAAA1111BBBB2222CCCC3333DDDD4444"


class TierTest(unittest.TestCase):
    def test_five_tiers_cover_the_whole_range(self) -> None:
        """0~100 每一分都要落进某一档，不能有空档。"""
        seen = [charge_tier(value).name for value in range(0, 101)]
        self.assertEqual(len(set(seen)), 5, "应恰好五档")
        for value in range(0, 101):
            self.assertIsNotNone(charge_tier(value))

    def test_tier_boundaries(self) -> None:
        self.assertEqual(charge_tier(0).name, "亏电")
        self.assertEqual(charge_tier(19).name, "亏电")
        self.assertEqual(charge_tier(20).name, "虚电")
        self.assertEqual(charge_tier(39).name, "虚电")
        self.assertEqual(charge_tier(40).name, "半电")
        self.assertEqual(charge_tier(59).name, "半电")
        self.assertEqual(charge_tier(60).name, "满电")
        self.assertEqual(charge_tier(79).name, "满电")
        self.assertEqual(charge_tier(80).name, "超充")
        self.assertEqual(charge_tier(100).name, "超充")

    def test_every_tier_has_quotes(self) -> None:
        for tier in CHARGE_TIERS:
            self.assertGreaterEqual(len(tier.quotes), 3, f"{tier.name} 语料太少")

    def test_quotes_are_unique_across_the_whole_pool(self) -> None:
        """同一句话出现在两档里会显得偷懒。"""
        all_quotes = [q for tier in CHARGE_TIERS for q in tier.quotes]
        self.assertEqual(len(all_quotes), len(set(all_quotes)))

    def test_quotes_are_single_line_and_short(self) -> None:
        """一言体就该是一句话；带换行会撑破引用块。"""
        for tier in CHARGE_TIERS:
            for quote in tier.quotes:
                self.assertNotIn("\n", quote, quote)
                self.assertLessEqual(len(quote), 40, quote)
                self.assertTrue(quote.strip(), "空评语")


class DerivationTest(unittest.TestCase):
    def test_value_is_within_zero_to_hundred(self) -> None:
        for i in range(300):
            reading = game.charge_index(f"user-{i}", DAY)
            self.assertGreaterEqual(reading.index, 0)
            self.assertLessEqual(reading.index, 100)

    def test_same_user_same_day_is_stable(self) -> None:
        """当天查多少次都是同一个数 —— 这才叫"今日"，重摇就没意义了。"""
        first = game.charge_index(UID, DAY)
        for _ in range(5):
            self.assertEqual(game.charge_index(UID, DAY), first)

    def test_different_day_gives_a_different_reading(self) -> None:
        """连续 30 天里至少要有变化，否则等于每天一样。"""
        readings = {game.charge_index(UID, DAY + timedelta(days=i)).index for i in range(30)}
        self.assertGreater(len(readings), 5, "每天的指数几乎一样，那这个功能就废了")

    def test_different_users_get_different_readings(self) -> None:
        readings = {game.charge_index(f"user-{i}", DAY).index for i in range(50)}
        self.assertGreater(len(readings), 20, "不同人应普遍拿到不同的数")

    def test_roughly_uniform_distribution(self) -> None:
        """均匀 0~100：三百分之一的人落在任一分上，不该明显偏向某处。"""
        values = [game.charge_index(f"u-{i}", DAY).index for i in range(2000)]
        low = sum(1 for v in values if v <= 20)      # 期望约 21%
        high = sum(1 for v in values if v >= 80)     # 期望约 21%
        self.assertGreater(low, 200, f"低分段过少（{low}），分布偏了")
        self.assertGreater(high, 200, f"高分段过少（{high}），分布偏了")

    def test_tier_and_quote_match_the_index(self) -> None:
        for i in range(200):
            reading = game.charge_index(f"u-{i}", DAY)
            self.assertEqual(reading.tier, charge_tier(reading.index).name)
            self.assertIn(reading.quote, charge_tier(reading.index).quotes)

    def test_quote_is_stable_for_the_same_user_and_day(self) -> None:
        for i in range(30):
            uid = f"u-{i}"
            self.assertEqual(
                game.charge_index(uid, DAY).quote, game.charge_index(uid, DAY).quote
            )

    def test_no_storage_or_io_involved(self) -> None:
        """纯推导：函数签名里不该出现 db。"""
        import inspect

        params = list(inspect.signature(game.charge_index).parameters)
        self.assertEqual(params, ["user_id", "day"], params)


class CommandTest(unittest.TestCase):
    def test_recognises_the_documented_command(self) -> None:
        self.assertEqual(commands.parse("/今日充能指数").name, commands.CHARGE)
        self.assertEqual(commands.parse("今日充能指数").name, commands.CHARGE)

    def test_tolerates_spaces_and_fullwidth_slash(self) -> None:
        self.assertEqual(commands.parse("  ／今日充能指数  ").name, commands.CHARGE)

    def test_has_no_aliases(self) -> None:
        """按决定只做一个命令名。别把"今日充能"也认了 —— 少一个歧义源。"""
        for text in ("今日充能", "充能指数", "指数"):
            self.assertEqual(commands.parse(text).name, commands.UNKNOWN, text)

    def test_does_not_shadow_the_signin_command(self) -> None:
        self.assertEqual(commands.parse("/充能面包").name, commands.SIGNIN)
        self.assertEqual(commands.parse("充能面包").name, commands.SIGNIN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
