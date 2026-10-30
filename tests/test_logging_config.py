"""Log redaction: secrets must never reach the log file."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.logging_config import RedactingFilter, configure_logging


def record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("out-of-budget-us", logging.INFO, __file__, 1, message, args, None)


class TestRedaction:
    def test_masks_a_password_in_a_connection_url(self) -> None:
        item = record("connecting to mysql+mysqlconnector://user:hunter2@db:3306/adflow")

        RedactingFilter().filter(item)

        assert "hunter2" not in item.getMessage()
        assert "***" in item.getMessage()

    def test_masks_keyword_style_secrets(self) -> None:
        for message in (
            "password=hunter2",
            "token: abc123xyz",
            "api_key=sk-live-000",
            "Cookie: session-token-value",
            "session_id=9f8e7d",
            "Authorization: Bearer abc.def.ghi",
        ):
            item = record(message)
            RedactingFilter().filter(item)
            assert "***" in item.getMessage(), message

    def test_masks_secrets_passed_as_arguments(self) -> None:
        item = record("db url is %s", "mysql://u:sup3rs3cret@h/db")

        RedactingFilter().filter(item)

        assert "sup3rs3cret" not in item.getMessage()

    def test_masks_token_bearing_url_parameters(self) -> None:
        item = record(
            "redirected to https://www.amazon.com/ap/signin?ssoResponse=eyJhbGciOiJBMjU2S1cifQ"
        )

        RedactingFilter().filter(item)

        assert "eyJhbGciOiJBMjU2S1cifQ" not in item.getMessage()
        assert "ssoResponse=***" in item.getMessage()

    def test_leaves_ordinary_messages_intact(self) -> None:
        item = record("Inserted 42 rows into out_of_budget_campaign_data")

        RedactingFilter().filter(item)

        assert item.getMessage() == "Inserted 42 rows into out_of_budget_campaign_data"

    def test_non_string_arguments_survive(self) -> None:
        item = record("count=%d", 7)

        RedactingFilter().filter(item)

        assert item.getMessage() == "count=7"


class TestConfigureLogging:
    def test_writes_to_the_log_directory(self, tmp_path: Path) -> None:
        logger = configure_logging(tmp_path, level="INFO", console=False)

        logger.info("hello from the test")
        for handler in logger.handlers:
            handler.flush()

        assert (tmp_path / "adproduct.log").read_text().strip().endswith("hello from the test")

    def test_secrets_are_scrubbed_on_the_way_to_disk(self, tmp_path: Path) -> None:
        logger = configure_logging(tmp_path, level="INFO", console=False)

        logger.info("url=mysql+mysqlconnector://adflow:TopSecret1@10.0.0.5:3306/adflow")
        for handler in logger.handlers:
            handler.flush()

        assert "TopSecret1" not in (tmp_path / "adproduct.log").read_text()

    def test_repeated_configuration_does_not_duplicate_handlers(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, level="INFO", console=False)
        logger = configure_logging(tmp_path, level="INFO", console=False)

        assert len(logger.handlers) == 1

    def test_keeps_one_week_of_rotated_logs_by_default(self, tmp_path: Path) -> None:
        logger = configure_logging(tmp_path, level="INFO", console=False)

        assert logger.handlers[0].backupCount == 7

    def test_lines_are_plain_ist_time_level_stage_message(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, level="INFO", console=False)
        logger = logging.getLogger("advertised-product.downloader")

        logger.warning("Sponsored Brands downloaded")
        for handler in logging.getLogger("advertised-product").handlers:
            handler.flush()

        line = (tmp_path / "adproduct.log").read_text().strip()
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d IST \| WARN  \| Download +\| Sponsored Brands downloaded",
            line,
        )
