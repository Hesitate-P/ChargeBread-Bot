"""game.py 测试：把签到、连签、补签、发卡这些规则钉住。

跑法：  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread import game  # noqa: E402
from chargebread.db import Database  # noqa: E402
from chargebread.rules import MAKEUP_BREAD, BASE_BREAD  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
UID = "AAAA1111BBBB2222CCCC3333DDDD4444"
UID2 = "BBBB1111BBBB2222CCCC3333DDDD4444"
NICK = "小明"


def at(day: str, hour: int = 21, minute: int = 0) -> datetime:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=TZ)


class GameDayTest(unittest.TestCase):
    def test_natural_day_when_reset_hour_is_zero(self) -> None:
        self.assertEqual(game.game_day(at("2026-09-21", 0, 0), TZ, 0), date(2026, 9, 21))
        self.assertEqual(game.game_day(at("2026-09-21", 23, 59), TZ, 0), date(2026, 9, 21))

    def test_reset_hour_shifts_small_hours_to_previous_day(self) -> None:
        self.assertEqual(game.game_day(at("2026-09-22", 2, 0), TZ, 4), date(2026, 9, 21))
        self.assertEqual(game.game_day(at("2026-09-22", 4, 0), TZ, 4), date(2026, 9, 22))
        self.assertEqual(game.game_day(at("2026-09-22", 23, 0), TZ, 4), date(2026, 9, 22))

    def test_timezone_is_respected(self) -> None:
        # 上海 21 日的 09:00 = UTC 21 日的 01:00，按上海仍算 21 日
        utc = ZoneInfo("UTC")
        moment = datetime(2026, 9, 21, 1, 0, tzinfo=utc)
        self.assertEqual(game.game_day(moment, TZ, 0), date(2026, 9, 21))


class StreakTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(game.current_streak(set(), date(2026, 9, 21)), 0)

    def test_only_today(self) -> None:
        self.assertEqual(game.current_streak({date(2026, 9, 21)}, date(2026, 9, 21)), 1)

    def test_consecutive_including_today(self) -> None:
        days = {date(2026, 9, 19), date(2026, 9, 20), date(2026, 9, 21)}
        self.assertEqual(game.current_streak(days, date(2026, 9, 21)), 3)

    def test_streak_still_alive_before_signing_today(self) -> None:
        """今天还没签，但昨天签了 —— 连签还没断。"""
        days = {date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20)}
        self.assertEqual(game.current_streak(days, date(2026, 9, 21)), 3)

    def test_broken_streak_resets_to_zero(self) -> None:
        """最近一次签到在前天或更早 —— 已断签。"""
        days = {date(2026, 9, 18), date(2026, 9, 19)}
        self.assertEqual(game.current_streak(days, date(2026, 9, 21)), 0)

    def test_gap_inside_history_does_not_break_current_run(self) -> None:
        days = {date(2026, 9, 15), date(2026, 9, 20), date(2026, 9, 21)}
        self.assertEqual(game.current_streak(days, date(2026, 9, 21)), 2)


class SignInTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _sign(self, day: str, uid: str = UID, group: str = "G1", seed: int = 1):
        return game.sign_in(
            self.db, user_id=uid, nickname=NICK, group_openid=group,
            now=at(day), tz=TZ, reset_hour=0, rng=random.Random(seed),
        )

    def test_first_signin_of_the_day_is_rank_one(self) -> None:
        out = self._sign("2026-09-21")
        self.assertEqual(out.status, game.STATUS_OK)
        self.assertEqual(out.rank, 1)
        self.assertEqual(out.streak, 1)
        self.assertEqual(out.bread, out.total_bread)
        self.assertGreater(out.bread, 0)

    def test_bread_is_within_expected_band_for_first_place(self) -> None:
        """第 1 名、连签 1 天：10 × 1.5 × [0.9,1.1] × 1.05 → 14~17。"""
        out = self._sign("2026-09-21")
        self.assertGreaterEqual(out.bread, 14)
        self.assertLessEqual(out.bread, 17)

    def test_rank_increases_with_each_player(self) -> None:
        first = self._sign("2026-09-21", uid=UID)
        second = self._sign("2026-09-21", uid=UID2)
        self.assertEqual(first.rank, 1)
        self.assertEqual(second.rank, 2)
        self.assertGreater(first.bread, second.bread, "第 1 名应该拿到更多面包")

    def test_signing_twice_same_day_is_rejected(self) -> None:
        self._sign("2026-09-21")
        again = self._sign("2026-09-21")
        self.assertEqual(again.status, game.STATUS_ALREADY)
        self.assertEqual(again.rank, 1)
        self.assertEqual(self.db.count_signins("2026-09-21"), 1)

    def test_globally_once_per_day_even_across_groups(self) -> None:
        """核心规则：不管在几个群里，全局一天只能签一次。"""
        self._sign("2026-09-21", group="G1")
        other = self._sign("2026-09-21", group="G2")
        self.assertEqual(other.status, game.STATUS_ALREADY)
        self.assertEqual(self.db.count_signins("2026-09-21"), 1)
        # 面包只加了一次
        self.assertEqual(self.db.total_bread(UID), self._sign("2026-09-22", group="G2").total_bread - self.db.get_signin("2026-09-22", UID).bread)

    def test_streak_grows_across_consecutive_days(self) -> None:
        for i, day in enumerate(("2026-09-19", "2026-09-20", "2026-09-21")):
            out = self._sign(day, seed=i)
            self.assertEqual(out.status, game.STATUS_OK)
            self.assertEqual(out.streak, i + 1)

    def test_streak_bonus_makes_later_days_worth_more(self) -> None:
        """同样的排名，连签越久拿得越多，说明连签加成生效。"""
        day1 = self._sign("2026-09-19", seed=7)
        last = day1
        for day in ("2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"):
            last = self._sign(day, seed=7)
        assert last.reward is not None
        self.assertGreater(last.reward.streak_bonus, 0)
        self.assertGreater(last.reward.total, day1.reward.total)

    def test_streak_resets_after_missing_a_day(self) -> None:
        self._sign("2026-09-19")
        self._sign("2026-09-20")
        after_gap = self._sign("2026-09-23")
        self.assertEqual(after_gap.streak, 1, "断签两天后连签应清零")

    def test_card_granted_when_streak_reaches_seven(self) -> None:
        start = date(2026, 9, 1)
        for i in range(6):
            day = (start + timedelta(days=i)).isoformat()
            out = self._sign(day)
            self.assertEqual(out.cards_granted, 0, f"第 {i + 1} 天不该发卡")
        seventh = self._sign((start + timedelta(days=6)).isoformat())
        self.assertEqual(seventh.streak, 7)
        self.assertEqual(seventh.cards_granted, 1, "连签满 7 天应发 1 张卡")
        self.assertEqual(seventh.cards_balance, 1)

    def test_card_not_granted_twice_for_same_milestone(self) -> None:
        start = date(2026, 9, 1)
        for i in range(7):
            self._sign((start + timedelta(days=i)).isoformat())
        # 第 8 天不该再发（还没到 14 天）
        eighth = self._sign((start + timedelta(days=7)).isoformat())
        self.assertEqual(eighth.cards_granted, 0)
        self.assertEqual(eighth.cards_balance, 1)

    def test_card_is_granted_again_after_a_broken_streak(self) -> None:
        """断签后**重新**连签 7 天，应该再拿一张卡。

        审查抓到的规格违背：里程碑当时是终身高水位，第二段连签一无所获，
        下一张卡要连签 56 天。
        """
        start = date(2026, 9, 1)
        for i in range(7):
            self._sign((start + timedelta(days=i)).isoformat())
        self.assertEqual(self.db.makeup_state(UID)[0], 1, "第一段 7 天拿到 1 张")

        # 断签 3 天（9/8~9/10），再从 9/11 连签 7 天
        base = start + timedelta(days=10)
        last = None
        for i in range(7):
            last = self._sign((base + timedelta(days=i)).isoformat())
        assert last is not None
        self.assertEqual(last.streak, 7)
        self.assertEqual(last.cards_granted, 1, "第二段 7 天连签应再发一张")
        self.assertEqual(last.cards_balance, 2)

    def test_milestone_counts_within_one_streak(self) -> None:
        """同一段连签里，14 天应发第二张（而不是被当成新一段从头算）。"""
        start = date(2026, 9, 1)
        granted = 0
        for i in range(14):
            out = self._sign((start + timedelta(days=i)).isoformat())
            granted += out.cards_granted
        self.assertEqual(granted, 2, "连签 14 天应该发两张（第 7 天和第 14 天）")


class BreadCorrectionTest(unittest.TestCase):
    """一个更早的签到晚到时，被顶后一名的人面包要按新系数折算。

    不折算就会出现"两个人都拿第 1 名的 1.5 倍"。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _sign(self, day: str, uid: str, minute: int):
        return game.sign_in(
            self.db, user_id=uid, nickname=uid[-4:], group_openid="G1",
            now=at(day, 21, 0), tz=TZ,
            signed_at=at(day, 8, minute),
            rng=random.Random(1),
        )

    def test_demoted_user_bread_is_rescaled(self) -> None:
        later = self._sign("2026-09-21", UID, 30)      # 08:30 签的，此刻是第 1
        self.assertEqual(later.rank, 1)
        self.assertGreaterEqual(later.bread, 14, "第 1 名应拿到 1.5 倍档")

        earlier = self._sign("2026-09-21", UID2, 0)    # 08:00 签到**晚到**
        self.assertEqual(earlier.rank, 1, "更早的签到应该排到第 1")

        demoted = self.db.get_signin("2026-09-21", UID)
        assert demoted is not None
        self.assertEqual(demoted.seq_in_day, 2, "晚到者插到前面，原第 1 被顶到第 2")
        # 随机浮动按比例保留，只换系数：1.5 → 1.35
        self.assertEqual(
            demoted.bread,
            round(later.bread * 1.35 / 1.5),
            "被顶到第 2 名后应按 1.35 档重算，不能留着第 1 名的 1.5 倍",
        )
        self.assertLess(demoted.bread, later.bread, "重算后应低于原来的 1.5 倍档")

    def test_same_tier_demotion_leaves_bread_alone(self) -> None:
        """名次系数档位没变（都在 1.0 档）就不该动面包。"""
        uids = [f"USER{i:028d}" for i in range(11)]
        outs = {
            uid: self._sign("2026-09-21", uid, 10 + i)
            for i, uid in enumerate(uids)
        }
        last_uid = uids[-1]                      # 第 11 名，1.0 档
        before = outs[last_uid].bread
        self._sign("2026-09-21", UID2, 0)        # 更早的签到晚到 → 所有人往后一位
        after = self.db.get_signin("2026-09-21", last_uid)
        assert after is not None
        self.assertEqual(after.seq_in_day, 12)
        self.assertEqual(after.bread, before, "同档内重排不该改面包")


class MakeUpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _sign(self, day: str, group: str = "G1"):
        return game.sign_in(
            self.db, user_id=UID, nickname=NICK, group_openid=group,
            now=at(day), tz=TZ, rng=random.Random(1),
        )

    def _makeup(self, day: str, group: str = "G1"):
        return game.make_up(
            self.db, user_id=UID, nickname=NICK, group_openid=group,
            now=at(day), tz=TZ,
        )

    def test_no_card_means_cannot_make_up(self) -> None:
        self._sign("2026-09-20")
        out = self._makeup("2026-09-22")   # 想补 21 号，但没卡
        self.assertEqual(out.status, game.STATUS_NO_CARD)

    def test_make_up_fills_gap_and_restores_streak(self) -> None:
        # 攒一张卡
        start = date(2026, 9, 1)
        for i in range(7):
            self._sign((start + timedelta(days=i)).isoformat())
        self.assertEqual(self.db.makeup_state(UID)[0], 1)

        # 9/8 漏签，9/9 来补 9/8
        gap_day = (start + timedelta(days=7)).isoformat()      # 2026-09-08
        out = self._makeup((start + timedelta(days=8)).isoformat())  # 09-09 补 09-08
        self.assertEqual(out.status, game.STATUS_OK)
        self.assertEqual(out.target_date.isoformat(), gap_day)
        self.assertEqual(out.bread, MAKEUP_BREAD)
        self.assertEqual(out.cards_balance, 0, "补签应扣掉那张卡")

        # 连签续上了：9/1~9/8 连续 8 天
        signed = {datetime.strptime(d, "%Y-%m-%d").date() for d in self.db.signed_dates(UID)}
        self.assertIn(date(2026, 9, 8), signed)
        self.assertEqual(game.current_streak(signed, date(2026, 9, 9)), 8)

    def test_make_up_does_not_take_a_daily_rank(self) -> None:
        start = date(2026, 9, 1)
        for i in range(7):
            self._sign((start + timedelta(days=i)).isoformat())
        target = (start + timedelta(days=7)).isoformat()
        self._makeup((start + timedelta(days=8)).isoformat())
        self.assertEqual(self.db.count_signins(target), 0, "补签不该进当天排名")
        self.assertEqual(self.db.today_total(target), 0)
        self.assertEqual(self.db.today_order(target, 10), [])

    def test_cannot_make_up_a_day_already_signed(self) -> None:
        start = date(2026, 9, 1)
        for i in range(7):
            self._sign((start + timedelta(days=i)).isoformat())
        # 昨天（9/7）已经签过了，补签应无事可做
        out = self._makeup((start + timedelta(days=7)).isoformat())
        self.assertEqual(out.status, game.STATUS_NOTHING_TO_MAKEUP)
        self.assertEqual(self.db.makeup_state(UID)[0], 1, "无事可做时不该扣卡")

    def test_make_up_only_targets_yesterday(self) -> None:
        start = date(2026, 9, 1)
        for i in range(7):
            self._sign((start + timedelta(days=i)).isoformat())
        # 9/10 来补，只能补 9/9（不是更早的 9/8）
        out = self._makeup((start + timedelta(days=9)).isoformat())
        self.assertEqual(out.target_date.isoformat(), "2026-09-09")


class RewardBandTest(unittest.TestCase):
    def test_late_ranks_have_a_floor(self) -> None:
        """第 11 名之后系数为 1.0，面包落在 9~11（不含连签加成）。"""
        from chargebread.rules import roll_reward

        rng = random.Random(0)
        values = [roll_reward(50, 0, rng=rng).total for _ in range(200)]
        self.assertGreaterEqual(min(values), 9)
        self.assertLessEqual(max(values), 11)

    def test_streak_bonus_is_capped_at_fifty_percent(self) -> None:
        from chargebread.rules import streak_bonus

        self.assertAlmostEqual(streak_bonus(0), 0.0)
        self.assertAlmostEqual(streak_bonus(1), 0.05)
        self.assertAlmostEqual(streak_bonus(10), 0.50)
        self.assertAlmostEqual(streak_bonus(99), 0.50, "加成必须封顶在 50%")

    def test_base_bread_unchanged(self) -> None:
        self.assertEqual(BASE_BREAD, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
