"""领域逻辑：签到、连签、补签。

这一层把用户拍定的规则变成可测的函数，不碰网络、不碰渲染。

规则回顾（全部来自需求确认）：
  · 全局一天只能签一次 —— 不管你在几个群里，今天签过就不能再签。
    硬保证在数据库的 UNIQUE(signin_date, user_id) 上，这里只负责把
    撞约束翻译成"今天已签到"。
  · 面包 = 基础分 × 排名系数 × 随机波动 × 连签加成，见 rules.py。
  · 连签断掉清零；补签可续上。
  · 补签卡：连签每满 7 天送 1 张；只能补昨天；补签发保底面包、不计排名。
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Database, SigninRow
from .rules import (
    CHARGE_MAX,
    CHARGE_MIN,
    MAKEUP_BREAD,
    Reward,
    cards_earned_at_streak,
    charge_tier,
    rank_factor,
    roll_reward,
)

log = logging.getLogger("chargebread.game")

# 签到结果状态
STATUS_OK = "ok"
STATUS_ALREADY = "already"          # 今天已经签过（含并发重复点击）
STATUS_NO_CARD = "no_card"          # 没有补签卡
STATUS_NOTHING_TO_MAKEUP = "nothing_to_makeup"  # 昨天已签，或没有断口


def game_day(now: datetime, tz: ZoneInfo, reset_hour: int = 0) -> date:
    """按本地时区与重置点算出"游戏日"。

    reset_hour=0 就是自然日；reset_hour=4 时，凌晨 2 点仍算前一天。
    所有"今天/昨天"的判断都必须走这里，别在别处自己算日期。
    """
    local = now.astimezone(tz)
    if reset_hour and local.hour < reset_hour:
        return (local - timedelta(days=1)).date()
    return local.date()


def current_streak(signed: set[date], today: date) -> int:
    """当前连签天数。

    今天签了就从今天往前数；今天还没签但昨天签了，连签仍然活着（从昨天往前数）；
    否则为 0。补签只是往集合里加了一天，所以自动被算进去。
    """
    if today in signed:
        cursor = today
    elif (today - timedelta(days=1)) in signed:
        cursor = today - timedelta(days=1)
    else:
        return 0
    count = 0
    while cursor in signed:
        count += 1
        cursor -= timedelta(days=1)
    return count


@dataclass(frozen=True)
class SigninOutcome:
    status: str
    game_date: date
    rank: int = 0                     # 今天第几个签到的（全局）
    bread: int = 0
    reward: Reward | None = None
    streak: int = 0                   # 签到后的连签天数
    total_bread: int = 0
    cards_granted: int = 0
    cards_balance: int = 0
    existing: SigninRow | None = None  # status=already 时，今天那条记录


@dataclass(frozen=True)
class MakeupOutcome:
    status: str
    target_date: date
    bread: int = 0
    streak: int = 0
    total_bread: int = 0
    cards_balance: int = 0


def _signed_set(db: Database, user_id: str) -> set[date]:
    return {
        datetime.strptime(d, "%Y-%m-%d").date()
        for d in db.signed_dates(user_id, limit=400)
    }


def sign_in(
    db: Database,
    *,
    user_id: str,
    nickname: str,
    group_openid: str,
    now: datetime,
    tz: ZoneInfo,
    reset_hour: int = 0,
    signed_at: datetime | None = None,
    rng: random.Random | None = None,
) -> SigninOutcome:
    """执行一次签到。

    `signed_at` 是**用户按下那一刻**（事件自带的 timestamp），与 `now`（服务器时间）
    分开：名次和"游戏日"都按 signed_at 算，所以链路卡顿、发送重试、事件重投
    都不会把顺序算错。不传则退回用 now。

    无论成功与否都会先登记用户（这样排行榜里能看到"来过但今天没签"的人）。
    """
    moment = signed_at or now
    now_iso = now.isoformat()
    # 归一成 UTC ISO 存库：排序无歧义，展示时再按机器人时区换算
    signin_at = moment.astimezone(timezone.utc).isoformat()
    db.upsert_user(user_id, "group", nickname, now_iso)
    today = game_day(moment, tz, reset_hour)

    existing = db.get_signin(today.isoformat(), user_id)
    if existing is not None:
        return SigninOutcome(
            status=STATUS_ALREADY,
            game_date=today,
            rank=existing.seq_in_day,
            bread=existing.bread,
            streak=existing.streak_after,
            total_bread=db.total_bread(user_id),
            cards_balance=db.makeup_state(user_id)[0],
            existing=existing,
        )

    signed = _signed_set(db, user_id)
    streak_before = current_streak(signed, today)
    streak_after = streak_before + 1
    rank = db.rank_for_moment(today.isoformat(), signin_at)
    reward = roll_reward(rank, streak_after, rng=rng)

    try:
        row = db.insert_signin(
            game_date=today.isoformat(),
            user_id=user_id,
            group_openid=group_openid,
            signin_at=signin_at,
            bread=reward.total,
            streak_before=streak_before,
            streak_after=streak_after,
            is_makeup=False,
            now_iso=now_iso,
        )
    except sqlite3.IntegrityError:
        # 并发或平台重复推送导致同一人同一天插了两次：退回"已签到"
        row = db.get_signin(today.isoformat(), user_id)
        return SigninOutcome(
            status=STATUS_ALREADY,
            game_date=today,
            rank=row.seq_in_day if row else 0,
            bread=row.bread if row else 0,
            streak=row.streak_after if row else streak_after,
            total_bread=db.total_bread(user_id),
            cards_balance=db.makeup_state(user_id)[0],
            existing=row,
        )

    if row.seq_in_day != rank:
        log.debug("名次与预判不一致（预判 %d，实际 %d），以库中为准", rank, row.seq_in_day)

    # 一个**更早**的签到晚到时，比它晚的人全部被重排到后一名。重排改的是
    # seq_in_day，但那些人的面包是按旧名次的系数发的 —— 不纠正就会出现
    # "两个人都拿第 1 名的 1.5 倍"。随机浮动保留，只按系数比例折算。
    _correct_demoted_bread(db, today.isoformat(), signin_at)

    granted = _grant_cards(db, user_id, streak_after, restarted=streak_before == 0)
    balance, _ = db.makeup_state(user_id)
    return SigninOutcome(
        status=STATUS_OK,
        game_date=today,
        rank=row.seq_in_day,
        bread=reward.total,
        reward=reward,
        streak=streak_after,
        total_bread=db.total_bread(user_id),
        cards_granted=granted,
        cards_balance=balance,
    )


def _correct_demoted_bread(db: Database, game_date: str, signin_at: str) -> None:
    """把被这次插入顶到后一名的人的面包，按新旧名次系数折算。

    只处理名次系数**档位**变化的行（如 1.5 → 1.35）；档位没变（都在 1.0 档）
    就不动。随机浮动是各自签时 roll 的，按比例保留。
    """
    for r in db.rows_after(game_date, signin_at):
        old_factor = rank_factor(r["seq_in_day"] - 1)
        new_factor = rank_factor(r["seq_in_day"])
        if old_factor == new_factor:
            continue
        adjusted = max(1, round(r["bread"] * new_factor / old_factor))
        if adjusted != r["bread"]:
            db.set_bread(game_date, r["user_id"], adjusted)


def make_up(
    db: Database,
    *,
    user_id: str,
    nickname: str,
    group_openid: str,
    now: datetime,
    tz: ZoneInfo,
    reset_hour: int = 0,
) -> MakeupOutcome:
    """补签昨天。消耗一张卡，发保底面包，不计排名，把连签续上。"""
    now_iso = now.isoformat()
    db.upsert_user(user_id, "group", nickname, now_iso)
    today = game_day(now, tz, reset_hour)
    target = today - timedelta(days=1)
    target_iso = target.isoformat()

    if db.get_signin(target_iso, user_id) is not None:
        return MakeupOutcome(
            status=STATUS_NOTHING_TO_MAKEUP,
            target_date=target,
            total_bread=db.total_bread(user_id),
            cards_balance=db.makeup_state(user_id)[0],
        )

    balance, _ = db.makeup_state(user_id)
    if balance <= 0:
        return MakeupOutcome(
            status=STATUS_NO_CARD,
            target_date=target,
            total_bread=db.total_bread(user_id),
            cards_balance=0,
        )

    if not db.consume_makeup_card(user_id):
        # 条件 UPDATE 说明余额刚好被别人（或上一次点击）扣走了
        return MakeupOutcome(
            status=STATUS_NO_CARD,
            target_date=target,
            total_bread=db.total_bread(user_id),
            cards_balance=db.makeup_state(user_id)[0],
        )

    signed = _signed_set(db, user_id)
    streak_before = current_streak(signed, target)
    streak_after = streak_before + 1

    try:
        db.insert_signin(
            game_date=target_iso,
            user_id=user_id,
            group_openid=group_openid,
            signin_at=target_iso + "T00:00:00+00:00",  # 补签记为当天零点，不参与排序
            bread=MAKEUP_BREAD,
            streak_before=streak_before,
            streak_after=streak_after,
            is_makeup=True,
            now_iso=now_iso,
        )
    except sqlite3.IntegrityError:
        db.refund_makeup_card(user_id)   # 落库失败，把卡还回去
        return MakeupOutcome(
            status=STATUS_NOTHING_TO_MAKEUP,
            target_date=target,
            total_bread=db.total_bread(user_id),
            cards_balance=db.makeup_state(user_id)[0],
        )
    except Exception:
        # 卡已扣但落库因别的原因失败（磁盘满、锁超时……）：必须退卡，
        # 否则平台重投一次就多扣一张
        db.refund_makeup_card(user_id)
        raise

    # 补签可能把连签推到新的里程碑，所以也要检查发卡
    after = current_streak(_signed_set(db, user_id), today)
    _grant_cards(db, user_id, after, restarted=streak_before == 0)
    balance, _ = db.makeup_state(user_id)
    return MakeupOutcome(
        status=STATUS_OK,
        target_date=target,
        bread=MAKEUP_BREAD,
        streak=streak_after,
        total_bread=db.total_bread(user_id),
        cards_balance=balance,
    )


# ---- 今日充能指数 ------------------------------------------------------------
# 推导串里的版本号。改推导规则时**一起改它**，让"所有人指数重算"变成一次显式
# 动作，而不是某天大家发现数字突然全变了。
CHARGE_SALT = "chargebread:charge:v1"


@dataclass(frozen=True)
class ChargeReading:
    index: int
    tier: str
    quote: str


def charge_index(user_id: str, day: date) -> ChargeReading:
    """算某人某天的充能指数。

    纯推导，**不读库也不写库**：同样的 (身份, 游戏日) 永远得到同样的结果，
    所以它天然满足"当天恒定、跨群一致、零点跟签到一起重置"。
    指数是账号级的（和面包一样），不按群分开 —— 同一个人在哪个群查都一样。

    取值均匀落在 0~100；评语从该档的语料池里取，档内也由同一个摘要决定，
    所以同一人同一天连评语都是固定的。
    """
    digest = hashlib.sha256(f"{CHARGE_SALT}:{user_id}:{day.isoformat()}".encode()).digest()
    index = CHARGE_MIN + int.from_bytes(digest[:4], "big") % (CHARGE_MAX - CHARGE_MIN + 1)
    tier = charge_tier(index)
    quote = tier.quotes[int.from_bytes(digest[4:8], "big") % len(tier.quotes)]
    return ChargeReading(index=index, tier=tier.name, quote=quote)


def _grant_cards(db: Database, user_id: str, streak: int, *, restarted: bool = False) -> int:
    """按连签里程碑补发补签卡，返回本次发了几张。

    `restarted=True` 表示上一段连签已经断了（本次签到前连签为 0）。此时必须把
    里程碑计数清零 —— 否则第一段连签领过 7 天的卡之后，**第二段连签永远拿不到卡**
    （下一张要连签 56 天）。这是审查抓到的规格违背。

    注意不能靠"当前连签 < 上次已发的里程碑"来判断新一段：新一段正好攒到同一高度
    （比如又是 7 天）时那个比较为假，照样漏发。只有"上一段断了"这个信号是准的。
    """
    _, granted_up_to = db.makeup_state(user_id)
    if restarted and granted_up_to:
        db.grant_makeup_cards(user_id, 0, 0)
        granted_up_to = 0
    earned = cards_earned_at_streak(streak)
    if earned <= granted_up_to:
        return 0
    new_cards = earned - granted_up_to
    db.grant_makeup_cards(user_id, new_cards, earned)
    return new_cards
