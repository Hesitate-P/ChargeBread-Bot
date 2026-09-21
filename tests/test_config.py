"""config.py 的契约（TDD）。

重点是把「出站目标必须过 netguard」这条规则钉住：API 域名会带着
`Authorization: QQBot <token>` 发请求，和其它出站 URL 一样不能免检。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.config import Config, ConfigError  # noqa: E402

BASE_ENV = {
    "QBOT_APP_ID": "1905655911",
    "QBOT_APP_SECRET": "secret-for-tests",
    "BREAD_IMAGE_URL": "https://img.example/a.png",
}


def env_with(**overrides: str) -> dict[str, str]:
    env = dict(BASE_ENV)
    env.update(overrides)
    return env


class ConfigTest(unittest.TestCase):
    def test_minimal_env_is_accepted(self) -> None:
        cfg = Config.from_env(env_file=Path("/nonexistent"), environ=env_with())
        self.assertEqual(cfg.app_id, "1905655911")
        self.assertEqual(cfg.reset_hour, 0)
        self.assertEqual(cfg.tz.key, "Asia/Shanghai")
        self.assertEqual(cfg.api_base, "https://api.bot.qq.com")

    def test_missing_credentials_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_env(env_file=Path("/nonexistent"), environ={"BREAD_IMAGE_URL": "https://x/a.png"})

    def test_missing_image_url_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_env(
                env_file=Path("/nonexistent"),
                environ={"QBOT_APP_ID": "1", "QBOT_APP_SECRET": "s"},
            )

    def test_bad_reset_hour_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_env(env_file=Path("/nonexistent"), environ=env_with(BREAD_RESET_HOUR="24"))

    def test_bad_timezone_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_env(env_file=Path("/nonexistent"), environ=env_with(BREAD_TZ="Not/AZone"))


class ApiBaseGuardTest(unittest.TestCase):
    """审查发现：api_base 原来完全没校验，却用它拼请求并附上凭据头。"""

    def test_plaintext_api_base_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_env(
                env_file=Path("/nonexistent"),
                environ=env_with(BREAD_API_BASE="http://api.bot.qq.com"),
            )

    def test_loopback_api_base_is_rejected(self) -> None:
        for bad in ("https://127.0.0.1", "https://localhost", "https://[::1]"):
            with self.assertRaises(ConfigError, msg=bad):
                Config.from_env(
                    env_file=Path("/nonexistent"), environ=env_with(BREAD_API_BASE=bad)
                )

    def test_private_ip_api_base_is_rejected(self) -> None:
        for bad in ("https://10.0.0.5", "https://192.168.1.1", "https://169.254.1.1"):
            with self.assertRaises(ConfigError, msg=bad):
                Config.from_env(
                    env_file=Path("/nonexistent"), environ=env_with(BREAD_API_BASE=bad)
                )

    def test_trailing_slash_is_normalised(self) -> None:
        cfg = Config.from_env(
            env_file=Path("/nonexistent"),
            environ=env_with(BREAD_API_BASE="https://api.bot.qq.com/"),
        )
        self.assertEqual(cfg.api_base, "https://api.bot.qq.com")


class AllowedGroupsTest(unittest.TestCase):
    def test_empty_means_all_groups(self) -> None:
        cfg = Config.from_env(env_file=Path("/nonexistent"), environ=env_with())
        self.assertEqual(cfg.allowed_groups, frozenset())

    def test_comma_separated_list_is_parsed(self) -> None:
        cfg = Config.from_env(
            env_file=Path("/nonexistent"),
            environ=env_with(BREAD_ALLOWED_GROUPS=" G1 , G2 ,, G3 "),
        )
        self.assertEqual(cfg.allowed_groups, frozenset({"G1", "G2", "G3"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
