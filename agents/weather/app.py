"""Weather sub-agent. Exposes itself via A2A on port 9000 (default A2A port).

Uses a free public API (Open-Meteo) so no API key is required for the prototype.
"""
from __future__ import annotations

import logging
import os

import httpx
from strands import Agent, tool
from strands.multiagent.a2a import A2AServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@tool
def get_weather(latitude: float, longitude: float) -> dict:
    """Return current weather for the given coordinates (Open-Meteo, free, no key)."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}&current_weather=true"
    )
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json().get("current_weather", {})


@tool
def geocode_city(city: str) -> dict:
    """Resolve a city name to lat/long via Open-Meteo geocoding."""
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=ja"
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return {"error": f"city '{city}' not found"}
        r = results[0]
        return {"latitude": r["latitude"], "longitude": r["longitude"], "name": r["name"]}


def build_agent() -> Agent:
    return Agent(
        name="weather-agent",
        description=(
            "天気検索エージェント。日本国内・海外の都市名や緯度経度から現在の天気を取得します。"
        ),
        system_prompt=(
            "あなたは天気情報エージェントです。ユーザーから都市名や場所が与えられたら、"
            "geocode_city で緯度経度を取得し、get_weather で現在の天気を返してください。"
        ),
        tools=[geocode_city, get_weather],
        callback_handler=None,
    )


def main() -> None:
    host = os.environ.get("A2A_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "9000"))
    agent = build_agent()
    server = A2AServer(agent=agent, host=host, port=port)
    logger.info("starting weather A2A server on %s:%s", host, port)
    server.serve()


if __name__ == "__main__":
    main()
