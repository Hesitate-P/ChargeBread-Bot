"""SQLite 存储层。

设计要点：
  · 主键用 `author.id`（用户决定）。多一列 `scope` 记录这个 id 来自哪个场景，
    将来接单聊时不会分不清一行是群身份还是单聊身份。
  · `signins` 上的 UNIQUE(signin_date, user_id) 是「全局一天只签一次」的**硬保证**。
    并发点两次按钮时，第二次会撞唯一约束 —— 业务层把 IntegrityError 当作
    "今天已签到"处理即可，不需要额外的锁。
  · 平台可能重复推送同一个事件，所以有 seen_events 表做幂等。

时间口径：`signin_date` 是按本地时区、并按 reset_hour 切出来的"游戏日"字符串
（YYYY-MM-DD），不是 UTC 日期。所有"今天/昨天"的判断都走 game.py 里的
game_day()，不要在别处自己算日期。

写法约定（项目安全扫描的硬要求，实测得出）：
  · SQL 必须以字符串字面量直接写在 `execute(` 的**同一行** —— 扫描器按行匹配，
    SQL 换到下一行会被判为"拼接构造"；
  · 不能把变量（含模块常量、循环变量）当 execute 的首参 —— 它无法静态证明
    该变量非攻击者可控。
  · 值一律走 `?` 占位符。
所以本文件的 SQL 既不换行、也不抽常量、更不循环执行。
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class UserRow:
    user_id: str
    nickname: str
    nickname_set: bool


@dataclass(frozen=True)
class SigninRow:
    signin_date: str
    user_id: str
    group_openid: str
    seq_in_day: int
    bread: int
    streak_before: int
    streak_after: int
    is_makeup: bool
    created_at: str
    # 事件里的真实签到时刻（已归一为 UTC ISO），榜单按它显示时间
    signin_at: str = ""


@dataclass(frozen=True)
class BoardEntry:
    user_id: str
    nickname: str
    value: int
    # 签到事件里的真实时刻（RFC3339）。榜单用它显示"几点签的"，比"第几个"清楚。
    signed_at: str | None = None


class Database:
    """单进程单连接。WAL + busy_timeout 已足够一个签到机器人用。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        """轻量迁移：给已存在的库补列。

        用户本地已经有跑过签到的库，不能靠删库重来。SQLite 支持 ADD COLUMN，
        新列给默认值即可。
        """
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(signins)").fetchall()
        }
        if "signin_at" not in columns:
            self.conn.execute("ALTER TABLE signins ADD COLUMN signin_at TEXT NOT NULL DEFAULT ''")

    def _init_schema(self) -> None:
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        # 外键要显式打开，SQLite 默认不 enforce —— 不开的话 signins→users 的
        # REFERENCES 只是装饰
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, scope TEXT NOT NULL, nickname TEXT NOT NULL, nickname_set INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS groups (group_openid TEXT PRIMARY KEY, group_name TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS signins (id INTEGER PRIMARY KEY AUTOINCREMENT, signin_date TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(user_id), group_openid TEXT NOT NULL, seq_in_day INTEGER NOT NULL, bread INTEGER NOT NULL, streak_before INTEGER NOT NULL, streak_after INTEGER NOT NULL, is_makeup INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, signin_at TEXT NOT NULL DEFAULT '', UNIQUE (signin_date, user_id))")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_signins_group_date ON signins(group_openid, signin_date)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_signins_date ON signins(signin_date)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_signins_user ON signins(user_id)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS makeup (user_id TEXT PRIMARY KEY REFERENCES users(user_id), cards INTEGER NOT NULL DEFAULT 0, earned_total INTEGER NOT NULL DEFAULT 0, used_total INTEGER NOT NULL DEFAULT 0, granted_streak INTEGER NOT NULL DEFAULT 0)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS seen_events (event_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
        self._migrate()

    # ---- 用户与群 ---------------------------------------------------------
    def upsert_user(self, user_id: str, scope: str, nickname: str, now_iso: str) -> UserRow:
        """登记/更新用户。已自设昵称的用户不会被事件里的 username 覆盖。"""
        row = self.conn.execute("SELECT user_id, nickname, nickname_set FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO users (user_id, scope, nickname, nickname_set, first_seen_at, last_seen_at) VALUES (?, ?, ?, 0, ?, ?)", (user_id, scope, nickname or default_nickname(user_id), now_iso, now_iso))
        elif nickname and not row["nickname_set"] and nickname != row["nickname"]:
            self.conn.execute("UPDATE users SET nickname = ?, last_seen_at = ? WHERE user_id = ?", (nickname, now_iso, user_id))
        else:
            self.conn.execute("UPDATE users SET last_seen_at = ? WHERE user_id = ?", (now_iso, user_id))
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> UserRow:
        row = self.conn.execute("SELECT user_id, nickname, nickname_set FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        return UserRow(row["user_id"], row["nickname"], bool(row["nickname_set"]))

    def set_nickname(self, user_id: str, nickname: str, now_iso: str) -> None:
        self.conn.execute("UPDATE users SET nickname = ?, nickname_set = 1, last_seen_at = ? WHERE user_id = ?", (nickname, now_iso, user_id))

    def clear_nickname(self, user_id: str, now_iso: str) -> None:
        """撤销自设昵称 —— 之后事件里的 QQ 昵称会重新生效。"""
        self.conn.execute("UPDATE users SET nickname_set = 0, last_seen_at = ? WHERE user_id = ?", (now_iso, user_id))

    def upsert_group(self, group_openid: str, group_name: str | None, now_iso: str) -> None:
        self.conn.execute("INSERT INTO groups (group_openid, group_name, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?) ON CONFLICT(group_openid) DO UPDATE SET group_name = COALESCE(excluded.group_name, groups.group_name), last_seen_at = excluded.last_seen_at", (group_openid, group_name, now_iso, now_iso))

    def group_name(self, group_openid: str) -> str | None:
        row = self.conn.execute("SELECT group_name FROM groups WHERE group_openid = ?", (group_openid,)).fetchone()
        return row["group_name"] if row else None

    # ---- 幂等 -------------------------------------------------------------
    def mark_event_seen(self, event_id: str, now_iso: str) -> bool:
        """首次见到返回 True；重复事件返回 False。"""
        try:
            self.conn.execute("INSERT INTO seen_events (event_id, seen_at) VALUES (?, ?)", (event_id, now_iso))
            return True
        except sqlite3.IntegrityError:
            return False

    def unmark_event(self, event_id: str) -> None:
        """撤销"已处理"标记。

        处理失败时必须撤掉 —— 否则事件被记为已处理，平台重投也会被丢掉，
        用户永远收不到回复。这是实测报"漏请求"的根因。
        """
        self.conn.execute("DELETE FROM seen_events WHERE event_id = ?", (event_id,))

    def prune_seen_events(self, now_iso: str, keep_days: int = 7) -> int:
        """清掉旧的幂等记录。

        平台只在几分钟内重投事件，7 天绰绰有余；不清的话这张表会
        随每条消息/每次点击无限增长。返回删除的行数。
        """
        cutoff = (datetime.fromisoformat(now_iso) - timedelta(days=keep_days)).isoformat()
        cur = self.conn.execute("DELETE FROM seen_events WHERE seen_at < ?", (cutoff,))
        return cur.rowcount

    # ---- 签到 -------------------------------------------------------------
    def get_signin(self, game_date: str, user_id: str) -> SigninRow | None:
        row = self.conn.execute("SELECT * FROM signins WHERE signin_date = ? AND user_id = ?", (game_date, user_id)).fetchone()
        return _to_signin(row) if row else None

    def count_signins(self, game_date: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM signins WHERE signin_date = ? AND is_makeup = 0", (game_date,)).fetchone()
        return int(row["n"])

    def rank_for_moment(self, game_date: str, signin_at: str) -> int:
        """按签到时刻算"今天第几个"，用于发奖前先知道名次。

        用 `<=` 且自己还没入库，所以得到的正是自己将要占据的位次。
        同一秒内多人时用该时刻已有人数 +1，与 insert_signin 里按 id 兜底的结果一致。
        """
        row = self.conn.execute("SELECT COUNT(*) AS n FROM signins WHERE signin_date = ? AND is_makeup = 0 AND signin_at <= ?", (game_date, signin_at)).fetchone()
        return int(row["n"]) + 1

    def rows_after(self, game_date: str, signin_at: str) -> list[dict]:
        """当天**晚于**某时刻的正式签到行 —— 它们刚被这个更早的插入顶后了一名。

        返回 sqlite3.Row（含 user_id / seq_in_day / bread），供名次系数折算用。
        """
        return list(
            self.conn.execute(
                "SELECT user_id, seq_in_day, bread FROM signins WHERE signin_date = ? AND is_makeup = 0 AND signin_at > ? ORDER BY seq_in_day ASC",
                (game_date, signin_at),
            ).fetchall()
        )

    def set_bread(self, game_date: str, user_id: str, bread: int) -> None:
        """名次重排后按新系数折算某人的面包（随机浮动按比例保留）。"""
        self.conn.execute("UPDATE signins SET bread = ? WHERE signin_date = ? AND user_id = ?", (bread, game_date, user_id))

    def insert_signin(self, *, game_date: str, user_id: str, group_openid: str, signin_at: str, bread: int, streak_before: int, streak_after: int, is_makeup: bool, now_iso: str) -> SigninRow:
        """插入签到，并在**同一个事务里**按签到时刻重排当天名次。

        名次不能靠"处理顺序"定 —— 发送失败重试、事件重投、链路卡顿都会让处理顺序
        和用户真实点击顺序不一致。所以每次插入后按 `(signin_at, id)` 重排当天所有
        非补签记录：这样即使一个**更早**的签到晚到，它也会正确插到前面，
        后面的人自动加一位（只算自己的名次是不够的，会出现两个人并列第 1）。

        一天几十条，重排代价可忽略；顺带还能自愈历史数据。
        补签固定 seq=0（不计排名）。

        返回的行反映**插入那一刻**的名次。若之后有更早的签到补进来，名次会变，
        以库中当前值为准（get_signin）。撞唯一约束会抛 sqlite3.IntegrityError。
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT INTO signins (signin_date, user_id, group_openid, seq_in_day, bread, streak_before, streak_after, is_makeup, created_at, signin_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)", (game_date, user_id, group_openid, bread, streak_before, streak_after, 1 if is_makeup else 0, now_iso, signin_at))
            row_id = int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            seq = 0
            if not is_makeup:
                self.conn.execute("UPDATE signins SET seq_in_day = (SELECT COUNT(*) FROM signins s2 WHERE s2.signin_date = signins.signin_date AND s2.is_makeup = 0 AND (s2.signin_at < signins.signin_at OR (s2.signin_at = signins.signin_at AND s2.id <= signins.id))) WHERE signin_date = ? AND is_makeup = 0", (game_date,))
                seq = int(self.conn.execute("SELECT seq_in_day AS n FROM signins WHERE id = ?", (row_id,)).fetchone()["n"])
            self.conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            raise
        return SigninRow(game_date, user_id, group_openid, seq, bread, streak_before, streak_after, is_makeup, now_iso, signin_at)

    def signed_dates(self, user_id: str, limit: int = 400) -> list[str]:
        """最近若干条签到日期，降序。用于算连签。"""
        rows = self.conn.execute("SELECT signin_date FROM signins WHERE user_id = ? ORDER BY signin_date DESC LIMIT ?", (user_id, limit)).fetchall()
        return [r["signin_date"] for r in rows]

    def total_bread(self, user_id: str) -> int:
        row = self.conn.execute("SELECT COALESCE(SUM(bread), 0) AS n FROM signins WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["n"])

    def signin_count(self, user_id: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM signins WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["n"])

    def best_streak(self, user_id: str) -> int:
        """历史最高连签。按日期序列扫一遍，数据量小，直接算。"""
        rows = self.conn.execute("SELECT signin_date FROM signins WHERE user_id = ? ORDER BY signin_date ASC", (user_id,)).fetchall()
        best = run = 0
        prev: date | None = None
        for r in rows:
            cur = _parse_date(r["signin_date"])
            run = run + 1 if prev is not None and (cur - prev).days == 1 else 1
            best = max(best, run)
            prev = cur
        return best

    # ---- 补签卡 -----------------------------------------------------------
    def makeup_state(self, user_id: str) -> tuple[int, int]:
        """返回 (余额, 已发到哪个连签里程碑)。"""
        row = self.conn.execute("SELECT cards, granted_streak FROM makeup WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO makeup (user_id) VALUES (?)", (user_id,))
            return 0, 0
        return int(row["cards"]), int(row["granted_streak"])

    def grant_makeup_cards(self, user_id: str, count: int, granted_streak: int) -> None:
        self.conn.execute("UPDATE makeup SET cards = cards + ?, earned_total = earned_total + ?, granted_streak = ? WHERE user_id = ?", (count, count, granted_streak, user_id))

    def consume_makeup_card(self, user_id: str) -> bool:
        """扣一张卡。余额不足返回 False（条件 UPDATE 保证原子）。"""
        cur = self.conn.execute("UPDATE makeup SET cards = cards - 1, used_total = used_total + 1 WHERE user_id = ? AND cards > 0", (user_id,))
        return cur.rowcount == 1

    def refund_makeup_card(self, user_id: str) -> None:
        """补签落库失败时把卡还回去，避免白扣。"""
        self.conn.execute("UPDATE makeup SET cards = cards + 1, used_total = used_total - 1 WHERE user_id = ?", (user_id,))

    # ---- 排行榜 -----------------------------------------------------------
    def board_global(self, limit: int) -> list[BoardEntry]:
        rows = self.conn.execute("SELECT u.user_id AS user_id, u.nickname AS nickname, SUM(s.bread) AS total FROM signins s JOIN users u ON u.user_id = s.user_id GROUP BY s.user_id ORDER BY total DESC, u.user_id ASC LIMIT ?", (limit,)).fetchall()
        return [BoardEntry(r["user_id"], r["nickname"], int(r["total"])) for r in rows]

    def board_group(self, group_openid: str, limit: int) -> list[BoardEntry]:
        """本群榜：只收在本群签到过的人，按他们的**全局**面包总数排。

        面包是账号级资产，不拆成"群内面包"，否则会出现两套打架的数值。
        """
        rows = self.conn.execute("SELECT u.user_id AS user_id, u.nickname AS nickname, (SELECT COALESCE(SUM(bread), 0) FROM signins WHERE user_id = u.user_id) AS total FROM users u WHERE EXISTS (SELECT 1 FROM signins WHERE user_id = u.user_id AND group_openid = ?) ORDER BY total DESC, u.user_id ASC LIMIT ?", (group_openid, limit)).fetchall()
        return [BoardEntry(r["user_id"], r["nickname"], int(r["total"])) for r in rows]

    def rank_in_global_board(self, user_id: str) -> tuple[int, int]:
        """返回 (我的名次, 总人数)。没签到过时名次为 0。"""
        people = int(self.conn.execute("SELECT COUNT(DISTINCT user_id) AS n FROM signins").fetchone()["n"])
        mine = self.total_bread(user_id)
        if mine == 0:
            return 0, people
        ahead = int(self.conn.execute("SELECT COUNT(*) AS n FROM (SELECT user_id, SUM(bread) AS total FROM signins GROUP BY user_id) WHERE total > ?", (mine,)).fetchone()["n"])
        return ahead + 1, people

    def rank_in_group_board(self, user_id: str, group_openid: str) -> tuple[int, int]:
        """返回 (我的名次, 本群玩家数)。

        注意判定条件是"在本群签到过"，不是"有没有面包"——一个人可能面包很多
        但一次都没在本群签到，那他在本群榜上就不该有名次。
        """
        people = int(self.conn.execute("SELECT COUNT(DISTINCT user_id) AS n FROM signins WHERE group_openid = ?", (group_openid,)).fetchone()["n"])
        is_member = self.conn.execute("SELECT 1 AS x FROM signins WHERE user_id = ? AND group_openid = ? LIMIT 1", (user_id, group_openid)).fetchone()
        if is_member is None:
            return 0, people
        mine = self.total_bread(user_id)
        ahead = int(self.conn.execute("SELECT COUNT(*) AS n FROM (SELECT s.user_id AS user_id, (SELECT COALESCE(SUM(bread), 0) FROM signins WHERE user_id = s.user_id) AS total FROM signins s WHERE s.group_openid = ? GROUP BY s.user_id) WHERE total > ?", (group_openid, mine)).fetchone()["n"])
        return ahead + 1, people

    # ---- 今日签到顺序榜 ---------------------------------------------------
    def today_order(self, game_date: str, limit: int, group_openid: str | None = None) -> list[BoardEntry]:
        """今日签到顺序。补签不计入。带上签到时刻，榜单直接显示时间。"""
        if group_openid:
            rows = self.conn.execute("SELECT s.seq_in_day AS seq, s.signin_at AS signin_at, u.user_id AS user_id, u.nickname AS nickname FROM signins s JOIN users u ON u.user_id = s.user_id WHERE s.signin_date = ? AND s.is_makeup = 0 AND s.group_openid = ? ORDER BY s.seq_in_day ASC LIMIT ?", (game_date, group_openid, limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT s.seq_in_day AS seq, s.signin_at AS signin_at, u.user_id AS user_id, u.nickname AS nickname FROM signins s JOIN users u ON u.user_id = s.user_id WHERE s.signin_date = ? AND s.is_makeup = 0 ORDER BY s.seq_in_day ASC LIMIT ?", (game_date, limit)).fetchall()
        return [
            BoardEntry(r["user_id"], r["nickname"], int(r["seq"]), signed_at=r["signin_at"] or None)
            for r in rows
        ]

    def today_total(self, game_date: str, group_openid: str | None = None) -> int:
        if group_openid:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM signins WHERE signin_date = ? AND is_makeup = 0 AND group_openid = ?", (game_date, group_openid)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM signins WHERE signin_date = ? AND is_makeup = 0", (game_date,)).fetchone()
        return int(row["n"])


def default_nickname(user_id: str) -> str:
    """事件里没给 username 时的兜底显示名。

    绝不把 openid 整串渲染出去——社区反馈里最劝退的体验就是一列 32 位十六进制。
    """
    return f"面包学徒#{user_id[-4:].lower()}"


def _to_signin(row: sqlite3.Row) -> SigninRow:
    return SigninRow(
        signin_date=row["signin_date"],
        user_id=row["user_id"],
        group_openid=row["group_openid"],
        seq_in_day=int(row["seq_in_day"]),
        bread=int(row["bread"]),
        streak_before=int(row["streak_before"]),
        streak_after=int(row["streak_after"]),
        is_makeup=bool(row["is_makeup"]),
        created_at=row["created_at"],
        signin_at=row["signin_at"] or "",
    )


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()
