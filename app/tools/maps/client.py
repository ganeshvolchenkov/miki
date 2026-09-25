from __future__ import annotations
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

class MapsApiError(Exception):
    pass

class MapsClientProtocol(Protocol):
    def get_directions(self, origin: str, destination: str, mode: str = "transit") -> dict[str, Any]:
        ...

class GoogleMapsClient:
    def __init__(self, api_key: str) -> None:
        import googlemaps
        self.client = googlemaps.Client(key=api_key)

    def get_directions(self, origin: str, destination: str, mode: str = "transit") -> dict[str, Any]:
        try:
            directions = self.client.directions(
                origin,
                destination,
                mode=mode,
                alternatives=False
            )
            if not directions:
                return {"error": "No route found."}
                
            route = directions[0]["legs"][0]
            steps = []
            for step in route.get("steps", []):
                instruction = step.get("html_instructions", "")
                import re
                # strip html tags
                instruction = re.sub('<[^<]+>', '', instruction)
                
                if step.get("travel_mode") == "TRANSIT":
                    transit = step.get("transit_details", {})
                    line = transit.get("line", {}).get("short_name", "") or transit.get("line", {}).get("name", "")
                    vehicle = transit.get("line", {}).get("vehicle", {}).get("name", "Transit")
                    departure = transit.get("departure_stop", {}).get("name", "")
                    arrival = transit.get("arrival_stop", {}).get("name", "")
                    instruction = f"Take {vehicle} {line} from {departure} to {arrival}."
                
                steps.append({
                    "distance": step.get("distance", {}).get("text"),
                    "duration": step.get("duration", {}).get("text"),
                    "instruction": instruction
                })
                
            return {
                "distance": route.get("distance", {}).get("text"),
                "duration": route.get("duration", {}).get("text"),
                "start_address": route.get("start_address"),
                "end_address": route.get("end_address"),
                "steps": steps
            }
        except Exception as exc:
            raise MapsApiError(f"Failed to get directions: {exc}") from exc
