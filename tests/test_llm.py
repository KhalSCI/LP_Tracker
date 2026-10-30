"""Tests for the OpenRouter client."""
from unittest.mock import AsyncMock

import pytest

import llm


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "key")


class TestForeignScript:
    """Test cases for asking again when a Polish reply slips into another script."""

    async def test_cyrillic_reply_is_asked_for_again(self, monkeypatch):
        monkeypatch.setattr(llm, "request", AsyncMock(side_effect=["13 за całą grę", "13 zgonów na całą grę"]))

        assert await llm.chat([{"role": "user", "content": "?"}], latin_only=True) == "13 zgonów na całą grę"
        assert llm.request.await_count == 2

    async def test_none_when_every_try_has_it(self, monkeypatch):
        monkeypatch.setattr(llm, "request", AsyncMock(return_value="безлимитный режим"))

        assert await llm.complete("system", "prompt", latin_only=True) is None
        assert llm.request.await_count == 1 + llm.FOREIGN_SCRIPT_RETRIES

    async def test_polish_and_emoji_pass(self, monkeypatch):
        monkeypatch.setattr(llm, "request", AsyncMock(return_value="Źle, żółw by lepiej zagrał 💀"))

        assert await llm.chat([], latin_only=True) == "Źle, żółw by lepiej zagrał 💀"
        llm.request.assert_awaited_once()

    async def test_other_scripts_allowed_without_latin_only(self, monkeypatch):
        # e.g. scouting summaries may quote opponents' nicks
        monkeypatch.setattr(llm, "request", AsyncMock(return_value="Gracz Ёжик gra Ahri"))

        assert await llm.complete("system", "prompt") == "Gracz Ёжик gra Ahri"
