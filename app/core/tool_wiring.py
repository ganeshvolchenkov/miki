from __future__ import annotations

from typing import Protocol

from app.brain.tool_types import BrainToolTurn
from app.core.config import Settings
from app.core.timeutil import resolve_timezone
from app.core.tool_runner import ToolConversationRunner
from app.tools.calendar.wiring import build_calendar_tool
from app.tools.manager import ToolManager
from app.tools.registry import ToolRegistry
from app.tools.weather.wiring import build_weather_tool


class ToolCallingBrainProtocol(Protocol):
    def generate_tool_turn(self, *, input_items: list[dict], tools: list[dict]) -> BrainToolTurn:
        ...


def build_tool_registry(settings: Settings) -> ToolRegistry:
    """Builds the tool registry from Settings. Adding a future tool means
    adding one `if settings.<tool>_enabled: registry.register(...)` line
    here -- nothing else in Miki needs to change."""
    registry = ToolRegistry()

    if settings.calendar_enabled:
        registry.register(
            build_calendar_tool(
                credentials_path=settings.google_calendar_credentials_path,
                token_path=settings.google_calendar_token_path,
                require_create_confirmation=settings.calendar_require_create_confirmation,
                default_calendar_id=settings.google_calendar_id,
            )
        )
        
        # Also register MailTool sharing the same OAuth files
        from app.tools.mail.wiring import build_mail_tool
        registry.register(
            build_mail_tool(
                credentials_path=settings.google_calendar_credentials_path,
                token_path=settings.google_calendar_token_path,
            )
        )
        
        # Also register DriveTool sharing the same OAuth files
        from app.tools.drive.wiring import build_drive_tool
        registry.register(
            build_drive_tool(
                credentials_path=settings.google_calendar_credentials_path,
                token_path=settings.google_calendar_token_path,
                folder_name=settings.google_drive_folder_name,
            )
        )

        from app.tools.maps.wiring import build_maps_tool
        registry.register(build_maps_tool(settings.google_maps_api_key))

    if settings.weather_enabled:
        registry.register(
            build_weather_tool(
                default_location=settings.weather_default_location,
                cache_ttl_seconds=settings.weather_cache_ttl_seconds,
            )
        )

    return registry


def build_tool_runner(settings: Settings, brain: ToolCallingBrainProtocol) -> ToolConversationRunner | None:
    """Builds the tool-calling conversation loop, or returns None if tools
    are disabled or no tool ended up registered. MikiCore treats a missing
    tool_runner exactly like it did before tools existed at all -- the same
    graceful-fallback pattern used for RAG and Memory Intelligence.
    """
    if not settings.tools_enabled:
        return None

    registry = build_tool_registry(settings)
    if not registry.has_tools():
        return None

    return ToolConversationRunner(
        brain=brain,
        tool_manager=ToolManager(registry),
        tzinfo=resolve_timezone(settings.timezone),
        max_iterations=settings.max_tool_iterations,
    )
