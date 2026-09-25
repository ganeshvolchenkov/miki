from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Callable

from app.tools.base import ACCESS_READ, Tool, ToolError, ToolOperation, ToolResult
from app.tools.maps.client import MapsApiError, MapsClientProtocol

logger = logging.getLogger(__name__)

class MapsTool(Tool):
    def __init__(self, *, client_factory: Callable[[], MapsClientProtocol | None]) -> None:
        super().__init__(
            name="maps",
            description="Get directions and routes using Google Maps.",
            operations=[
                ToolOperation(
                    name="get_directions",
                    description="Get directions between two locations. Defaults to public transport.",
                    access=ACCESS_READ,
                    parameters={
                        "type": "object",
                        "properties": {
                            "origin": {"type": "string", "description": "Starting address or location."},
                            "destination": {"type": "string", "description": "Ending address or location."},
                            "mode": {"type": "string", "description": "Travel mode: transit, driving, walking, bicycling. Default is transit."}
                        },
                        "required": ["origin", "destination"]
                    }
                )
            ]
        )
        self._client_factory = client_factory

    def execute(self, operation: str, arguments: dict[str, Any]) -> ToolResult:
        if operation == "get_directions":
            return self._get_directions(arguments)
        raise ToolError("unknown_operation", f"Unknown operation: {operation}")

    def _get_directions(self, arguments: dict[str, Any]) -> ToolResult:
        origin = arguments.get("origin")
        destination = arguments.get("destination")
        mode = arguments.get("mode") or "transit"
        
        if not origin or not destination:
            raise ToolError("invalid_arguments", "Missing origin or destination.")

        url_origin = urllib.parse.quote_plus(origin)
        url_destination = urllib.parse.quote_plus(destination)
        maps_url = f"https://www.google.com/maps/dir/?api=1&origin={url_origin}&destination={url_destination}&travelmode={mode}"

        client = self._client_factory()
        if not client:
            return ToolResult(
                success=True, 
                operation="get_directions", 
                data={"url": maps_url},
                message="Google Maps API key is not configured, so I generated a clickable link to view the route."
            )

        try:
            directions = client.get_directions(origin, destination, mode)
            directions["url"] = maps_url
            return ToolResult(success=True, operation="get_directions", data=directions)
        except MapsApiError as exc:
            return ToolResult(
                success=True,
                operation="get_directions",
                data={"url": maps_url},
                message=f"Could not get turn-by-turn directions: {exc}. Provide the user with this Maps URL instead."
            )

    def is_available(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        client = self._client_factory()
        if client:
            return {"api_enabled": True}
        return {"api_enabled": False, "reason": "No API key provided, only URL generation is supported."}
