from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(raw: str | None, *, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _parse_float(raw: str | None, *, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    model: str = "gpt-4o-mini"
    environment: str = "development"
    obsidian_vault_path: Path | None = None
    embedding_model: str = "text-embedding-3-small"
    rag_enabled: bool = True
    rag_top_k: int = 5
    rag_similarity_threshold: float = 0.2
    rag_chunk_size: int = 800
    rag_chunk_overlap: int = 100
    rag_index_path: Path = Path("data/rag_index/index.json")
    memory_intelligence_enabled: bool = True
    memory_candidate_threshold: float = 0.35
    memory_confident_threshold: float = 0.65
    memory_model: str = "gpt-4o-mini"
    tools_enabled: bool = True
    max_tool_iterations: int = 5
    timezone: str | None = None
    calendar_enabled: bool = True
    google_calendar_credentials_path: Path = Path("data/google_calendar/credentials.json")
    google_calendar_token_path: Path = Path("data/google_calendar/token.json")
    google_calendar_id: str = "primary"
    calendar_require_create_confirmation: bool = True
    google_maps_api_key: str | None = None
    google_drive_folder_name: str | None = None
    weather_enabled: bool = True
    weather_provider: str = "open-meteo"
    weather_api_key: str | None = None
    weather_default_location: str = "Amsterdam"
    weather_units: str = "metric"
    weather_cache_ttl_seconds: int = 300
    telegram_bot_token: str | None = None
    transcribe_model: str = "whisper-1"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "nova"
    quiet_hours: str = "23:00-08:00"

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "Missing OPENAI_API_KEY. Add it to your .env file before running Miki."
            )

        model = os.getenv("MIKI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        memory_model = os.getenv("MIKI_MEMORY_MODEL", "").strip() or model
        environment = os.getenv("MIKI_ENV", "development").strip() or "development"
        obsidian_raw = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
        obsidian_vault_path = None
        if obsidian_raw:
            candidate = Path(obsidian_raw).expanduser()
            if not candidate.exists() or not candidate.is_dir():
                raise ValueError(
                    "Configured OBSIDIAN_VAULT_PATH must point to an existing directory. Check the path in your .env file."
                )
            obsidian_vault_path = candidate

        embedding_model = os.getenv("MIKI_EMBEDDING_MODEL", "text-embedding-3-small").strip() or "text-embedding-3-small"
        rag_enabled = _parse_bool(os.getenv("MIKI_RAG_ENABLED"), default=True)
        rag_top_k = _parse_int(os.getenv("MIKI_RAG_TOP_K"), default=5)
        rag_similarity_threshold = _parse_float(os.getenv("MIKI_RAG_SIMILARITY_THRESHOLD"), default=0.2)
        rag_chunk_size = _parse_int(os.getenv("MIKI_RAG_CHUNK_SIZE"), default=800)
        rag_chunk_overlap = _parse_int(os.getenv("MIKI_RAG_CHUNK_OVERLAP"), default=100)
        rag_index_path_raw = os.getenv("MIKI_RAG_INDEX_PATH", "").strip()
        rag_index_path = Path(rag_index_path_raw) if rag_index_path_raw else Path("data/rag_index/index.json")

        memory_intelligence_enabled = _parse_bool(os.getenv("MIKI_MEMORY_INTELLIGENCE_ENABLED"), default=True)
        memory_candidate_threshold = _parse_float(os.getenv("MIKI_MEMORY_CANDIDATE_THRESHOLD"), default=0.35)
        memory_confident_threshold = _parse_float(os.getenv("MIKI_MEMORY_CONFIDENT_THRESHOLD"), default=0.65)

        tools_enabled = _parse_bool(os.getenv("MIKI_TOOLS_ENABLED"), default=True)
        max_tool_iterations = _parse_int(os.getenv("MIKI_MAX_TOOL_ITERATIONS"), default=5)
        timezone_raw = os.getenv("MIKI_TIMEZONE", "").strip()
        timezone = timezone_raw or None

        calendar_enabled = _parse_bool(os.getenv("MIKI_CALENDAR_ENABLED"), default=True)
        google_credentials_raw = os.getenv("MIKI_GOOGLE_CREDENTIALS_PATH", "").strip()
        google_calendar_credentials_path = Path(google_credentials_raw) if google_credentials_raw else Path("data/google_calendar/credentials.json")
        google_token_raw = os.getenv("MIKI_GOOGLE_TOKEN_PATH", "").strip()
        google_calendar_token_path = Path(google_token_raw) if google_token_raw else Path("data/google_calendar/token.json")
        google_calendar_id = os.getenv("MIKI_GOOGLE_CALENDAR_ID", "primary").strip() or "primary"
        calendar_require_create_confirmation = _parse_bool(os.getenv("MIKI_CALENDAR_REQUIRE_CREATE_CONFIRMATION"), default=True)

        google_maps_api_key = os.getenv("MIKI_GOOGLE_MAPS_API_KEY", "").strip() or None
        google_drive_folder_name = os.getenv("MIKI_GOOGLE_DRIVE_FOLDER_NAME", "").strip() or None

        weather_enabled = _parse_bool(os.getenv("MIKI_WEATHER_ENABLED"), default=True)
        weather_provider = os.getenv("WEATHER_PROVIDER", "open-meteo").strip() or "open-meteo"
        weather_api_key_raw = os.getenv("WEATHER_API_KEY", "").strip()
        weather_api_key = weather_api_key_raw or None
        weather_default_location = os.getenv("WEATHER_DEFAULT_LOCATION", "Amsterdam").strip() or "Amsterdam"
        weather_units = os.getenv("WEATHER_UNITS", "metric").strip() or "metric"
        weather_cache_ttl_seconds = _parse_int(os.getenv("WEATHER_CACHE_TTL_SECONDS"), default=300)
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
        transcribe_model = os.getenv("MIKI_TRANSCRIBE_MODEL", "").strip() or "whisper-1"
        tts_model = os.getenv("MIKI_TTS_MODEL", "").strip() or "gpt-4o-mini-tts"
        tts_voice = os.getenv("MIKI_TTS_VOICE", "").strip() or "nova"
        quiet_hours = os.getenv("MIKI_QUIET_HOURS", "").strip() or "23:00-08:00"

        return cls(
            openai_api_key=api_key,
            model=model,
            memory_model=memory_model,
            environment=environment,
            obsidian_vault_path=obsidian_vault_path,
            embedding_model=embedding_model,
            rag_enabled=rag_enabled,
            rag_top_k=rag_top_k,
            rag_similarity_threshold=rag_similarity_threshold,
            rag_chunk_size=rag_chunk_size,
            rag_chunk_overlap=rag_chunk_overlap,
            rag_index_path=rag_index_path,
            memory_intelligence_enabled=memory_intelligence_enabled,
            memory_candidate_threshold=memory_candidate_threshold,
            memory_confident_threshold=memory_confident_threshold,
            tools_enabled=tools_enabled,
            max_tool_iterations=max_tool_iterations,
            timezone=timezone,
            calendar_enabled=calendar_enabled,
            google_calendar_credentials_path=google_calendar_credentials_path,
            google_calendar_token_path=google_calendar_token_path,
            google_calendar_id=google_calendar_id,
            calendar_require_create_confirmation=calendar_require_create_confirmation,
            google_maps_api_key=google_maps_api_key,
            google_drive_folder_name=google_drive_folder_name,
            weather_enabled=weather_enabled,
            weather_provider=weather_provider,
            weather_api_key=weather_api_key,
            weather_default_location=weather_default_location,
            weather_units=weather_units,
            weather_cache_ttl_seconds=weather_cache_ttl_seconds,
            telegram_bot_token=telegram_bot_token,
            transcribe_model=transcribe_model,
            tts_model=tts_model,
            tts_voice=tts_voice,
            quiet_hours=quiet_hours,
        )

    @property
    def is_development(self) -> bool:
        return self.environment.lower() in {"dev", "development"}

    @property
    def has_obsidian_vault(self) -> bool:
        return self.obsidian_vault_path is not None
