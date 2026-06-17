"""RepoMind config loader - reads .env safely."""
import os
from pathlib import Path
from dotenv import load_dotenv

_CONFIG_DIR = Path(__file__).resolve().parent
_ENV_PATH = _CONFIG_DIR / ".env"

if not _ENV_PATH.exists():
    raise FileNotFoundError(
        f"Config not found: {_ENV_PATH}\n"
        f"Copy .env.example -> .env and fill in your API keys."
    )

load_dotenv(_ENV_PATH)

# env var name -> python constant
ENV_KEY = "DEEPSEEK_API_KEY"
api_key = os.getenv(ENV_KEY, "").strip()
base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

if not api_key:
    raise ValueError(f"{ENV_KEY} missing in .env")


def get_deepseek_client():
    """Returns OpenAI-compatible client pointed at DeepSeek."""
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url=base_url)
