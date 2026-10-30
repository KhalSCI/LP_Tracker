"""
Descriptions of the players written by the team (profiles.md, kept out of git): nicknames,
in-jokes and hooks for the bot's comments and conversations.
"""
import re
from pathlib import Path

PROFILES_PATH = Path(__file__).parent / "profiles.md"


def load_profiles() -> dict[str, str]:
    """
    Descriptions by lowercased roster nick, from the file's "## <nick>" sections (empty
    without the file). Read on every use, so edits work without a restart.
    """
    try:
        text = PROFILES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    parts = re.split(r"^## +(.+?)\s*$", text, flags=re.MULTILINE)
    return {nick.lower(): " ".join(body.split()) for nick, body in zip(parts[1::2], parts[2::2]) if body.strip()}


def profile(nick: str) -> str | None:
    return load_profiles().get((nick or "").lower())
