import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def env(name, default=""):
    return os.environ.get(name, "").strip() or default


class Settings:
    def __init__(self):
        self.tg_token = env("TG_TOKEN")
        self.allowed = {x.strip() for x in env("ALLOWED_USERS").split(",") if x.strip()}

        self.api_key = env("OPENCODE_API_KEY")
        self.api_url = env("OPENCODE_URL", "https://opencode.ai/zen/v1")
        self.default_model = env("DEFAULT_MODEL", "muse-spark-1.3-contributor-free")
        self.default_effort = env("DEFAULT_EFFORT", "xhigh")

        # ключ шифрования истории. без него история живёт только до конца смены
        self.state_key = env("STATE_KEY")
        self.state_dir = Path(env("STATE_DIR", str(ROOT / "state")))
        self.shift_minutes = int(env("SHIFT_MINUTES", "300"))


settings = Settings()
