import pytest
import asyncio
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

pytest_plugins = ('pytest_asyncio',)


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def no_player_profiles(monkeypatch, tmp_path):
    """Tests don't read the real profiles.md (they write their own when they need one)."""
    import player_profiles
    monkeypatch.setattr(player_profiles, "PROFILES_PATH", tmp_path / "profiles.md")
