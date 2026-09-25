from __future__ import annotations
from typing import Optional
from app.tools.maps.client import GoogleMapsClient, MapsClientProtocol
from app.tools.maps.tool import MapsTool

def build_maps_client_factory(api_key: str | None):
    def factory() -> MapsClientProtocol | None:
        if not api_key:
            return None
        return GoogleMapsClient(api_key)
    return factory

def build_maps_tool(api_key: str | None = None) -> MapsTool:
    factory = build_maps_client_factory(api_key)
    return MapsTool(client_factory=factory)
