from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    model: str = "gpt-4o-mini"
    environment: str = "development"

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "Missing OPENAI_API_KEY. Add it to your .env file before running Miki."
            )

        model = os.getenv("MIKI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        environment = os.getenv("MIKI_ENV", "development").strip() or "development"

        return cls(openai_api_key=api_key, model=model, environment=environment)

    @property
    def is_development(self) -> bool:
        return self.environment.lower() in {"dev", "development"}
