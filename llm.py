"""Short texts from a cheap LLM through OpenRouter (disabled without OPENROUTER_API_KEY)."""
import asyncio
import re

import aiohttp

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL

API_URL = "https://openrouter.ai/api/v1/chat/completions"
# Scripts the cheap model sometimes slips into mid-sentence of a Polish text (Cyrillic, Japanese, Chinese, Korean)
FOREIGN_SCRIPT = re.compile(r"[\u0400-\u04ff\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")
# Extra requests for a latin_only reply that came back in one of them
FOREIGN_SCRIPT_RETRIES = 2


def enabled() -> bool:
    return bool(OPENROUTER_API_KEY)


async def complete(system: str, prompt: str, max_tokens: int = 1000, temperature: float = 0.9,
                   model: str = None, latin_only: bool = False, reasoning_effort: str = "low",
                   timeout: float = 60) -> str | None:
    """The model's reply, or None when there's no key or the request fails (callers fall back)."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    return await chat(messages, max_tokens, temperature, model, latin_only, reasoning_effort, timeout)


async def chat(messages: list[dict], max_tokens: int = 1000, temperature: float = 0.9,
               model: str = None, latin_only: bool = False, reasoning_effort: str = "low",
               timeout: float = 60) -> str | None:
    """
    The model's next message in a conversation ({"role", "content"} dicts, system first),
    or None when there's no key or the request fails. latin_only: a reply with Cyrillic or
    CJK letters is asked for again, and None if every try has them. reasoning_effort and a
    longer timeout are for the rare longer analyses (e.g. scouting).
    """
    if not OPENROUTER_API_KEY:
        return None
    body = {
        "model": model or OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Short texts don't need much thinking; it's kept out of the reply either way
        "reasoning": {"effort": reasoning_effort, "exclude": True},
    }
    for _ in range(1 + (FOREIGN_SCRIPT_RETRIES if latin_only else 0)):
        text = await request(body, timeout)
        if text is None or not latin_only or not FOREIGN_SCRIPT.search(text):
            return text
        print(f"OpenRouter reply in a foreign script, asking again: {text[:100]}")
    return None


async def request(body: dict, timeout: float = 60) -> str | None:
    """One chat completion request; None when it fails."""
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "X-Title": "LP Tracker"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(API_URL, json=body, headers=headers) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    print(f"OpenRouter error {response.status}: {str(data)[:300]}")
                    return None
        text = data["choices"][0]["message"]["content"] or ""
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError) as e:
        print(f"OpenRouter request failed: {e}")
        return None
    return text.strip() or None
