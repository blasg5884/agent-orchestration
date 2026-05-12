"""Japanese postal-code (郵便番号) lookup sub-agent. A2A on port 9000.

Uses the free zipcloud API (https://zipcloud.ibsnet.co.jp/) — no API key required.
"""
from __future__ import annotations

import logging
import os
import re

import httpx
from strands import Agent, tool
from strands.multiagent.a2a import A2AServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ZIPCODE_PATTERN = re.compile(r"^\d{3}-?\d{4}$")


@tool
def lookup_zipcode(zipcode: str) -> dict:
    """Look up a Japanese address by 7-digit postal code (with or without hyphen)."""
    cleaned = zipcode.replace("-", "")
    if not ZIPCODE_PATTERN.match(zipcode) and not (len(cleaned) == 7 and cleaned.isdigit()):
        return {"error": f"invalid zipcode format: {zipcode!r} (expected 7 digits)"}
    url = f"https://zipcloud.ibsnet.co.jp/api/search?zipcode={cleaned}"
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != 200 or not data.get("results"):
        return {"error": data.get("message") or "not found", "zipcode": cleaned}
    r = data["results"][0]
    return {
        "zipcode": cleaned,
        "prefecture": r.get("address1"),
        "city": r.get("address2"),
        "town": r.get("address3"),
    }


def build_agent() -> Agent:
    return Agent(
        name="zipcode-agent",
        description=(
            "郵便番号検索エージェント。日本の7桁の郵便番号から都道府県・市区町村・町域を返します。"
        ),
        system_prompt=(
            "あなたは日本の郵便番号検索エージェントです。ユーザーが郵便番号（例: 100-0001）"
            "を提示したら lookup_zipcode を呼び出して住所を返してください。"
        ),
        tools=[lookup_zipcode],
        callback_handler=None,
    )


def main() -> None:
    host = os.environ.get("A2A_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "9000"))
    agent = build_agent()
    server = A2AServer(agent=agent, host=host, port=port)
    logger.info("starting zipcode A2A server on %s:%s", host, port)
    server.serve()


if __name__ == "__main__":
    main()
