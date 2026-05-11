"""Orchestrator agent.

Exposes an HTTP entrypoint via the bedrock-agentcore SDK (BedrockAgentCoreApp).
Discovers sub-agents from Agent Registry, wires them as A2A tools, and lets
the LLM decide which sub-agent to dispatch to.

This agent itself performs NO domain work — it only routes.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands_tools.a2a_client import A2AClientToolProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
REGISTRY_ID = os.environ["AGENT_REGISTRY_ID"]
REGISTRY_ARN = os.environ["AGENT_REGISTRY_ARN"]
MODEL_ID = os.environ.get(
    "ORCHESTRATOR_MODEL_ID",
    "apac.anthropic.claude-sonnet-4-5-20250929-v1:0",
)

_app = BedrockAgentCoreApp()
_agentcore = boto3.client("bedrock-agentcore", region_name=REGION)


def _list_subagent_endpoints() -> list[str]:
    """Search registry for A2A-type approved records and return their A2A URLs.

    Schema (control-plane create input mirrors the data-plane search output):
      descriptorType: 'A2A'
      descriptors.a2a.agentCard.inlineContent  → JSON A2A Agent Card
    The card's `url` field is the A2A endpoint to talk to.
    """
    endpoints: list[str] = []
    paginator_kwargs: dict[str, Any] = {
        "registryIds": [REGISTRY_ARN],
        "searchQuery": "agent",
        "maxResults": 20,  # search_registry_records caps at 20
    }
    resp = _agentcore.search_registry_records(**paginator_kwargs)
    for rec in resp.get("registryRecords", []):
        if rec.get("descriptorType") != "A2A":
            continue
        try:
            card_str = rec["descriptors"]["a2a"]["agentCard"]["inlineContent"]
            card = json.loads(card_str) if isinstance(card_str, str) else card_str
            url = card.get("url")
            if url:
                endpoints.append(url)
        except (KeyError, json.JSONDecodeError) as e:
            logger.warning("could not parse agent card for record %s: %s", rec.get("name"), e)
    return endpoints


def _build_agent() -> Agent:
    endpoints = _list_subagent_endpoints()
    logger.info("discovered %d sub-agent endpoints: %s", len(endpoints), endpoints)

    provider = A2AClientToolProvider(known_agent_urls=endpoints) if endpoints else None
    tools = provider.tools if provider else []

    return Agent(
        name="orchestrator",
        description="ユーザーのリクエストを適切なサブエージェントに振り分けるオーケストレーター。",
        system_prompt=(
            "あなたはオーケストレーターエージェントです。あなた自身は処理を実行しません。\n"
            "利用可能なサブエージェント（A2A 経由のツール）を見て、リクエスト内容に最も適した"
            "サブエージェントにディスパッチし、その回答をそのまま返してください。\n"
            "適切なサブエージェントが見つからない場合は、その旨を日本語で返してください。"
        ),
        model=MODEL_ID,
        tools=tools,
        callback_handler=None,
    )


@_app.entrypoint
def invoke(payload: dict, context: Any = None) -> dict:  # noqa: ARG001
    """AgentCore Runtime HTTP entrypoint.

    Expected payload: {"prompt": "<自然言語>"}.
    """
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "missing 'prompt' in payload"}
    agent = _build_agent()
    result = agent(prompt)
    return {"result": str(result)}


if __name__ == "__main__":
    _app.run()
