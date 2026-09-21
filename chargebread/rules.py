"""玩法规则：奖励公式、连签、补签卡。

所有可调数值集中在这里，改平衡不用碰业务逻辑。

规则来自需求确认阶段的逐条决定：
  · 面包 = 基础分 × 排名系数 × 随机波动 × 连签加成
  · 排名系数：第1名 1.5 / 第2名 1.35 / 第3名 1.25 / 4~10 名 1.1 / 11 名起 1.0
  · 随机波动 0.9 ~ 1.1
  · 连签加成：连签 N 天 +5%，封顶 +50%（即 10 天封顶）；断签清零
  · 补签卡：连签每满 7 天送 1 张；只能补昨天；补签发保底面包、不计排名、续上连签
  · 每日按自然日重置（0 点，Asia/Shanghai）
"""

from __future__ import annotations

import random
from dataclasses import dataclass

BASE_BREAD = 10
RANDOM_LOW = 0.90
RANDOM_HIGH = 1.10

# 排名 → 系数。11 名及以后取 DEFAULT_RANK_FACTOR
RANK_FACTORS: dict[int, float] = {1: 1.50, 2: 1.35, 3: 1.25}
MID_RANK_FACTOR = 1.10  # 4~10 名
MID_RANK_MAX = 10
DEFAULT_RANK_FACTOR = 1.00  # 11 名起

STREAK_BONUS_PER_DAY = 0.05
STREAK_BONUS_CAP_DAYS = 10  # 10 天 → +50%

# 补签：发保底面包，不计排名（取末位档的随机下沿）
MAKEUP_BREAD = int(round(BASE_BREAD * DEFAULT_RANK_FACTOR * RANDOM_LOW))

# 每连签满这么多天，送一张补签卡
MAKEUP_CARD_STREAK_STEP = 7

# 排行榜显示条数；自己不在前 N 时，底部单列一行显示自己
LEADERBOARD_TOP_N = 10


def rank_factor(rank: int) -> float:
    """第 rank 名（从 1 开始）的面包系数。"""
    if rank <= 0:
        return DEFAULT_RANK_FACTOR
    if rank in RANK_FACTORS:
        return RANK_FACTORS[rank]
    if rank <= MID_RANK_MAX:
        return MID_RANK_FACTOR
    return DEFAULT_RANK_FACTOR


def streak_bonus(streak: int) -> float:
    """连签 streak 天的加成比例。streak<=0 时无加成。"""
    days = max(0, min(streak, STREAK_BONUS_CAP_DAYS))
    return days * STREAK_BONUS_PER_DAY


@dataclass(frozen=True)
class Reward:
    base: int
    rank_factor: float
    random_factor: float
    streak_bonus: float
    total: int


def roll_reward(rank: int, streak: int, *, rng: random.Random | None = None) -> Reward:
    """按排名与连签算出这次签到发多少面包。"""
    rng = rng or random
    rf = rank_factor(rank)
    rand = rng.uniform(RANDOM_LOW, RANDOM_HIGH)
    sb = streak_bonus(streak)
    total = max(1, round(BASE_BREAD * rf * rand * (1.0 + sb)))
    return Reward(
        base=BASE_BREAD,
        rank_factor=rf,
        random_factor=rand,
        streak_bonus=sb,
        total=total,
    )


def cards_earned_at_streak(streak: int) -> int:
    """连签到 streak 天时，累计应得多少张补签卡。"""
    if streak <= 0:
        return 0
    return streak // MAKEUP_CARD_STREAK_STEP


def days_to_next_card(streak: int) -> int:
    """距离下一张补签卡还差几天连签。"""
    return MAKEUP_CARD_STREAK_STEP - (streak % MAKEUP_CARD_STREAK_STEP)
