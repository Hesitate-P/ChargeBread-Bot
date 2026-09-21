"""db.py 冒烟测试：把唯一约束、连签、补签卡、双榜口径这些硬逻辑钉住。

跑法：  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.db import Database  # noqa: E402

NOW = "2026-09-21T21:00:00+08:00"


def iso(day: str) -> str:
    return f"{day}T21:00:00+08:00"


def sign(
    db: Database,
    *,
    day: str,
    uid: str,
    group: str = "G1",
    clock: str = "08:00:00",
    bread: int = 10,
    streak_before: int = 0,
    streak_after: int = 1,
    is_makeup: bool = False,
):
    """插一条签到。

    名次现在由**签到时刻**决定（不是调用顺序），所以同一群里要让 clock 递增，
    期望的名次才符合直觉。
    """
    return db.insert_signin(
        game_date=day,
        user_id=uid,
        group_openid=group,
        signin_at=f"{day}T{clock}+08:00",
        bread=bread,
        streak_before=streak_before,
        streak_after=streak_after,
        is_makeup=is_makeup,
        now_iso=iso(day),
    )


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ---- 基础 -------------------------------------------------------------
    def test_upsert_user_and_default_nickname(self) -> None:
        u = self.db.upsert_user("AAAA1111BBBB2222CCCC3333DDDD4444", "group", "", NOW)
        self.assertTrue(u.nickname.startswith("面包学徒#"))
        self.assertIn("4444", u.nickname)
        self.assertFalse(u.nickname_set)

    def test_event_nickname_updates_until_user_sets_own(self) -> None:
        uid = "AAAA1111BBBB2222CCCC3333DDDD4444"
        self.db.upsert_user(uid, "group", "小明", NOW)
        self.assertEqual(self.db.get_user(uid).nickname, "小明")
        # 事件里昵称变了 → 跟着变
        self.db.upsert_user(uid, "group", "小明改名了", NOW)
        self.assertEqual(self.db.get_user(uid).nickname, "小明改名了")
        # 用户自设后 → 事件不再覆盖
        self.db.set_nickname(uid, "我自己起的", NOW)
        self.db.upsert_user(uid, "group", "事件又想改", NOW)
        self.assertEqual(self.db.get_user(uid).nickname, "我自己起的")
        self.assertTrue(self.db.get_user(uid).nickname_set)

    def test_mark_event_seen_is_idempotent(self) -> None:
        self.assertTrue(self.db.mark_event_seen("evt-1", NOW))
        self.assertFalse(self.db.mark_event_seen("evt-1", NOW))
        self.assertTrue(self.db.mark_event_seen("evt-2", NOW))

    def test_prune_seen_events_drops_only_old_rows(self) -> None:
        """幂等表会随每条消息无限增长，启动时要清理旧记录。"""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 9, 21, 21, 0, tzinfo=tz)
        self.db.mark_event_seen("old", (now - timedelta(days=30)).isoformat())
        self.db.mark_event_seen("recent", (now - timedelta(days=1)).isoformat())

        removed = self.db.prune_seen_events(now.isoformat(), keep_days=7)
        self.assertEqual(removed, 1)
        # 旧的被清掉，新的还在（且仍能识别重复）
        self.assertTrue(self.db.mark_event_seen("old", now.isoformat()), "旧记录已清，可重新记")
        self.assertFalse(self.db.mark_event_seen("recent", now.isoformat()), "近期记录必须保留")

    def test_prune_is_a_noop_when_nothing_is_old(self) -> None:
        self.db.mark_event_seen("fresh", NOW)
        self.assertEqual(self.db.prune_seen_events(NOW, keep_days=7), 0)

    # ---- 签到唯一约束（全局一天一次） --------------------------------------
    def test_second_signin_same_day_hits_unique_constraint(self) -> None:
        import sqlite3

        uid = "AAAA1111BBBB2222CCCC3333DDDD4444"
        self.db.upsert_user(uid, "group", "小明", NOW)
        sign(self.db, day="2026-09-21", uid=uid, group="G1", bread=15)
        with self.assertRaises(sqlite3.IntegrityError):
            sign(self.db, day="2026-09-21", uid=uid, group="G2", bread=12, clock="09:00:00")
        # 换一天就行
        sign(self.db, day="2026-09-22", uid=uid, group="G2", bread=12, streak_before=1, streak_after=2)
        self.assertEqual(self.db.count_signins("2026-09-21"), 1)
        self.assertEqual(self.db.count_signins("2026-09-22"), 1)

    def test_makeup_row_does_not_count_toward_daily_order(self) -> None:
        uid = "AAAA1111BBBB2222CCCC3333DDDD4444"
        self.db.upsert_user(uid, "group", "小明", NOW)
        sign(self.db, day="2026-09-20", uid=uid, bread=9, is_makeup=True)
        self.assertEqual(self.db.count_signins("2026-09-20"), 0)
        self.assertEqual(self.db.today_total("2026-09-20"), 0)
        self.assertEqual(self.db.today_order("2026-09-20", 10), [])
        # 但面包照发
        self.assertEqual(self.db.total_bread(uid), 9)

    # ---- 名次按签到时刻，不按处理顺序 --------------------------------------
    def test_rank_follows_signin_time_not_insert_order(self) -> None:
        """后处理的早签到应该拿到更靠前的名次 —— 这是"顺序会错"的根治。"""
        a = "AAAA0000000000000000000000000001"
        b = "BBBB0000000000000000000000000002"
        for uid, name in ((a, "甲"), (b, "乙")):
            self.db.upsert_user(uid, "group", name, NOW)
        # 先插入"晚签到"的乙（模拟事件重投/处理错序），再插入"早签到"的甲
        sign(self.db, day="2026-09-21", uid=b, clock="09:00:00")
        sign(self.db, day="2026-09-21", uid=a, clock="08:00:00")
        # 名次以库中当前值为准：更早的签到晚到，会把后面的人往后顶一位
        self.assertEqual(self.db.get_signin("2026-09-21", a).seq_in_day, 1, "08:00 签到的应该是第 1 个")
        self.assertEqual(self.db.get_signin("2026-09-21", b).seq_in_day, 2, "09:00 签到的应被顶到第 2")
        order = [(e.nickname, e.signed_at) for e in self.db.today_order("2026-09-21", 10)]
        self.assertEqual([n for n, _ in order], ["甲", "乙"])

    def test_same_second_ties_break_by_insert_order(self) -> None:
        a = "AAAA0000000000000000000000000001"
        b = "BBBB0000000000000000000000000002"
        for uid, name in ((a, "甲"), (b, "乙")):
            self.db.upsert_user(uid, "group", name, NOW)
        first = sign(self.db, day="2026-09-21", uid=a, clock="08:00:00")
        second = sign(self.db, day="2026-09-21", uid=b, clock="08:00:00")
        self.assertEqual((first.seq_in_day, second.seq_in_day), (1, 2), "同一秒按入库先后")

    def test_rank_for_moment_predicts_the_position(self) -> None:
        a = "AAAA0000000000000000000000000001"
        self.db.upsert_user(a, "group", "甲", NOW)
        self.assertEqual(self.db.rank_for_moment("2026-09-21", "2026-09-21T08:00:00+08:00"), 1)
        sign(self.db, day="2026-09-21", uid=a, clock="08:00:00")
        self.assertEqual(self.db.rank_for_moment("2026-09-21", "2026-09-21T07:00:00+08:00"), 1)
        self.assertEqual(self.db.rank_for_moment("2026-09-21", "2026-09-21T09:00:00+08:00"), 2)

    def test_today_order_carries_signed_at_for_display(self) -> None:
        a = "AAAA0000000000000000000000000001"
        self.db.upsert_user(a, "group", "甲", NOW)
        sign(self.db, day="2026-09-21", uid=a, clock="08:03:12")
        entry = self.db.today_order("2026-09-21", 10)[0]
        self.assertIsNotNone(entry.signed_at, "榜单要拿到签到时刻才能显示时间")
        self.assertIn("08:03:12", entry.signed_at)

    # ---- 连签 -------------------------------------------------------------
    def test_best_streak_counts_consecutive_days(self) -> None:
        uid = "AAAA1111BBBB2222CCCC3333DDDD4444"
        self.db.upsert_user(uid, "group", "小明", NOW)
        for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-10", "2026-09-11"):
            sign(self.db, day=day, uid=uid)
        self.assertEqual(self.db.best_streak(uid), 3)
        self.assertEqual(
            self.db.signed_dates(uid, 3),
            ["2026-09-11", "2026-09-10", "2026-09-03"],
        )

    # ---- 补签卡 -----------------------------------------------------------
    def test_makeup_card_balance_and_atomic_consume(self) -> None:
        uid = "AAAA1111BBBB2222CCCC3333DDDD4444"
        self.db.upsert_user(uid, "group", "小明", NOW)
        self.assertEqual(self.db.makeup_state(uid), (0, 0))
        self.assertFalse(self.db.consume_makeup_card(uid), "没卡时不该扣成功")
        self.db.grant_makeup_cards(uid, 1, granted_streak=7)
        self.assertEqual(self.db.makeup_state(uid), (1, 7))
        self.assertTrue(self.db.consume_makeup_card(uid))
        self.assertFalse(self.db.consume_makeup_card(uid), "扣完不能再扣")
        self.assertEqual(self.db.makeup_state(uid), (0, 7))

    # ---- 双榜口径 ---------------------------------------------------------
    def test_group_board_only_lists_group_members_but_uses_global_total(self) -> None:
        """本群榜只收在本群签到过的人，但分值是他们的全局面包总数。"""
        a = "AAAA0000000000000000000000000001"
        b = "BBBB0000000000000000000000000002"
        c = "CCCC0000000000000000000000000003"
        for uid, name in ((a, "甲"), (b, "乙"), (c, "丙")):
            self.db.upsert_user(uid, "group", name, NOW)

        # 甲：在 G1 签 1 次，在 G2 签 1 次 → 全局 20
        sign(self.db, day="2026-09-20", uid=a, group="G1")
        sign(self.db, day="2026-09-21", uid=a, group="G2", streak_before=1, streak_after=2)
        # 乙：只在 G1 签 → 全局 10
        sign(self.db, day="2026-09-21", uid=b, group="G1", clock="09:00:00")
        # 丙：只在 G2 签，高分 → 全局 30
        sign(self.db, day="2026-09-21", uid=c, group="G2", clock="10:00:00", bread=30)

        g1 = [(e.nickname, e.value) for e in self.db.board_group("G1", 10)]
        self.assertEqual(g1, [("甲", 20), ("乙", 10)], "本群榜应只含甲和乙，且用全局分")
        self.assertNotIn("丙", [n for n, _ in g1])

        glob = [(e.nickname, e.value) for e in self.db.board_global(10)]
        self.assertEqual(glob, [("丙", 30), ("甲", 20), ("乙", 10)])

    def test_today_order_and_group_filter(self) -> None:
        a = "AAAA0000000000000000000000000001"
        b = "BBBB0000000000000000000000000002"
        for uid, name in ((a, "甲"), (b, "乙")):
            self.db.upsert_user(uid, "group", name, NOW)
        # 乙 08:00 签在 G2，甲 09:00 签在 G1
        sign(self.db, day="2026-09-21", uid=b, group="G2", clock="08:00:00")
        sign(self.db, day="2026-09-21", uid=a, group="G1", clock="09:00:00")

        all_order = [(e.nickname, e.value) for e in self.db.today_order("2026-09-21", 10)]
        self.assertEqual(all_order, [("乙", 1), ("甲", 2)], "全部口径按签到时刻排")
        g1_order = [(e.nickname, e.value) for e in self.db.today_order("2026-09-21", 10, "G1")]
        self.assertEqual(g1_order, [("甲", 2)], "本群口径只含在本群签的人")
        self.assertEqual(self.db.today_total("2026-09-21"), 2)
        self.assertEqual(self.db.today_total("2026-09-21", "G1"), 1)

    def test_rank_lookups(self) -> None:
        a = "AAAA0000000000000000000000000001"
        b = "BBBB0000000000000000000000000002"
        c = "CCCC0000000000000000000000000003"
        for uid, name in ((a, "甲"), (b, "乙"), (c, "丙")):
            self.db.upsert_user(uid, "group", name, NOW)
        sign(self.db, day="2026-09-21", uid=a, group="G1", clock="08:00:00", bread=30)
        sign(self.db, day="2026-09-21", uid=b, group="G1", clock="09:00:00", bread=20)
        sign(self.db, day="2026-09-21", uid=c, group="G2", clock="10:00:00", bread=10)

        self.assertEqual(self.db.rank_in_global_board(a), (1, 3))
        self.assertEqual(self.db.rank_in_global_board(b), (2, 3))
        self.assertEqual(self.db.rank_in_global_board(c), (3, 3))
        self.assertEqual(self.db.rank_in_group_board(b, "G1"), (2, 2))
        self.assertEqual(self.db.rank_in_group_board(c, "G1"), (0, 2), "不在本群的人名次为 0")

    def test_unknown_user_has_zero_everything(self) -> None:
        ghost = "FFFF0000000000000000000000000009"
        self.assertEqual(self.db.total_bread(ghost), 0)
        self.assertEqual(self.db.rank_in_global_board(ghost), (0, 0))
        self.assertEqual(self.db.best_streak(ghost), 0)

    # ---- 群 ---------------------------------------------------------------
    def test_group_name_upsert_keeps_existing_when_none(self) -> None:
        self.db.upsert_group("G1", "Starry Lights", NOW)
        self.assertEqual(self.db.group_name("G1"), "Starry Lights")
        self.db.upsert_group("G1", None, NOW)
        self.assertEqual(self.db.group_name("G1"), "Starry Lights", "None 不该抹掉已知群名")


if __name__ == "__main__":
    unittest.main(verbosity=2)
