"""Short texts from a cheap LLM through OpenRouter (disabled without OPENROUTER_API_KEY)."""
import asyncio

import aiohttp

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL

API_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT = aiohttp.ClientTimeout(total=60)


def enabled() -> bool:
    return bool(OPENROUTER_API_KEY)


async def complete(system: str, prompt: str, max_tokens: int = 1000, temperature: float = 0.9,
                   model: str = None) -> str | None:
    """The model's reply, or None when there's no key or the request fails (callers fall back)."""
    if not OPENROUTER_API_KEY:
        return None
    body = {
        "model": model or OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Short texts don't need thinking; models that always think keep it out of the reply
        "reasoning": {"effort": "low", "exclude": True},
    }
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "X-Title": "LP Tracker"}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
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
