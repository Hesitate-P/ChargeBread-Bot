"""配置：全部来自环境变量 / .env，代码里不出现任何凭据字面量。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from .netguard import assert_public_http_url

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT / ".env"

API_BASE = "https://api.bot.qq.com"


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def load_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，# 开头为注释。不引入额外依赖。"""
    cfg: dict[str, str] = {}
    if not path.exists():
        return cfg
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().strip("'\"")
    return cfg


def _split_openids(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    image_url: str
    db_path: Path
    tz: ZoneInfo
    # 签到在每天几点重置。0 = 自然日 00:00（用户选定的方案）
    reset_hour: int = 0
    # 空集合 = 不限制；非空则只服务这些群
    allowed_groups: frozenset[str] = frozenset()
    api_base: str = API_BASE
    log_level: str = "INFO"

    @classmethod
    def from_env(
        cls, env_file: Path | None = None, environ: dict[str, str] | None = None
    ) -> "Config":
        file_cfg = load_env_file(env_file or DEFAULT_ENV_FILE)
        env = environ if environ is not None else dict(os.environ)

        def get(key: str, default: str = "") -> str:
            return env.get(key) or file_cfg.get(key) or default

        app_id = get("QBOT_APP_ID").strip()
        app_secret = get("QBOT_APP_SECRET").strip()
        if not app_id or not app_secret:
            raise ConfigError("缺少 QBOT_APP_ID / QBOT_APP_SECRET，请填到 .env")

        image_url = get("BREAD_IMAGE_URL").strip()
        if not image_url:
            raise ConfigError("缺少 BREAD_IMAGE_URL（充能面包图片的公网地址）")

        tz_name = get("BREAD_TZ", "Asia/Shanghai").strip() or "Asia/Shanghai"
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"时区 {tz_name!r} 无法加载：{exc}") from exc

        reset_hour = int(get("BREAD_RESET_HOUR", "0") or 0)
        if not 0 <= reset_hour <= 23:
            raise ConfigError(f"BREAD_RESET_HOUR 必须在 0..23，收到 {reset_hour}")

        api_base = (get("BREAD_API_BASE", API_BASE).rstrip("/") or API_BASE)
        # API 域名要带着 Authorization 头发请求，属于出站目标 —— 和其它出站 URL
        # 一样必须过 netguard（仅 https、拒绝内网/环回），不能因为是配置就免检。
        try:
            assert_public_http_url(api_base, require_https=True)
        except ValueError as exc:
            raise ConfigError(f"BREAD_API_BASE 不合法：{exc}") from exc

        db_path = Path(get("BREAD_DB_PATH", str(ROOT / "data" / "chargebread.sqlite3")))
        if not db_path.is_absolute():
            db_path = ROOT / db_path

        return cls(
            app_id=app_id,
            app_secret=app_secret,
            image_url=image_url,
            db_path=db_path,
            tz=tz,
            reset_hour=reset_hour,
            allowed_groups=_split_openids(get("BREAD_ALLOWED_GROUPS")),
            api_base=api_base,
            log_level=get("BREAD_LOG_LEVEL", "INFO").upper() or "INFO",
        )
