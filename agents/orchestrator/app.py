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
import sys
import traceback
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Configure logging to stdout with force=True so any default handlers from
# BedrockAgentCoreApp / Strands don't suppress our records. PYTHONUNBUFFERED=1
# is set in the Dockerfile to ensure prompt flushing.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("orchestrator")
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
REGISTRY_ID = os.environ.get("AGENT_REGISTRY_ID", "")
REGISTRY_ARN = os.environ.get("AGENT_REGISTRY_ARN", "")
MODEL_ID = os.environ.get(
    "ORCHESTRATOR_MODEL_ID",
    "apac.anthropic.claude-sonnet-4-5-20250929-v1:0",
)

logger.info(
    "boot: REGION=%s REGISTRY_ID=%r REGISTRY_ARN=%r MODEL_ID=%r",
    REGION,
    REGISTRY_ID,
    REGISTRY_ARN,
    MODEL_ID,
)

# Defer the heavy imports until after logging is configured so any import-time
# failures appear in the same log stream.
try:
    from strands import Agent
    from strands_tools.a2a_client import A2AClientToolProvider
    logger.info("imports OK: strands + strands_tools.a2a_client")
except Exception:
    logger.exception("FATAL: import of strands / a2a_client failed")
    raise

_app = BedrockAgentCoreApp()
_agentcore = boto3.client("bedrock-agentcore", region_name=REGION)


def _list_subagent_endpoints() -> list[str]:
    """Return A2A endpoint URLs from approved A2A records in the registry."""
    endpoints: list[str] = []
    resp = _agentcore.search_registry_records(
        registryIds=[REGISTRY_ARN],
        searchQuery="agent",
        maxResults=20,
    )
    record_count = len(resp.get("registryRecords", []))
    logger.info("search_registry_records returned %d records", record_count)
    for rec in resp.get("registryRecords", []):
        name = rec.get("name")
        dtype = rec.get("descriptorType")
        status = rec.get("status")
        logger.info("  record name=%s type=%s status=%s", name, dtype, status)
        if dtype != "A2A":
            continue
        try:
            card_str = rec["descriptors"]["a2a"]["agentCard"]["inlineContent"]
            card = json.loads(card_str) if isinstance(card_str, str) else card_str
            url = card.get("url")
            logger.info("    -> url=%r", url)
            if url:
                endpoints.append(url)
        except (KeyError, json.JSONDecodeError) as e:
            logger.warning("could not parse agent card for record %s: %s", name, e)
    return endpoints


def _build_agent() -> Agent:
    try:
        endpoints = _list_subagent_endpoints()
    except Exception:
        logger.exception("FATAL: _list_subagent_endpoints raised")
        raise
    logger.info("discovered %d sub-agent endpoints: %s", len(endpoints), endpoints)

    tools: list[Any] = []
    if endpoints:
        try:
            provider = A2AClientToolProvider(known_agent_urls=endpoints)
            tools = list(provider.tools)
            logger.info("A2AClientToolProvider produced %d tools", len(tools))
        except Exception:
            logger.exception(
                "FATAL: A2AClientToolProvider(known_agent_urls=%s) raised", endpoints
            )
            raise

    try:
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
    except Exception:
        logger.exception("FATAL: Agent(...) construction failed (model=%r)", MODEL_ID)
        raise


@_app.entrypoint
def invoke(payload):  # signature matches the AgentCore Strands sample exactly
    """AgentCore Runtime HTTP entrypoint. Payload: {"prompt": "..."}."""
    logger.info("============ INVOKE CALLED ============")
    logger.info("INVOKE raw payload: %r (type=%s)", payload, type(payload).__name__)
    if not isinstance(payload, dict):
        try:
            payload = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else dict(payload)
            logger.info("INVOKE parsed payload to dict: %r", payload)
        except Exception:
            logger.exception("INVOKE could not coerce payload to dict")
            return {"error": f"unsupported payload type: {type(payload).__name__}"}
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "missing 'prompt' in payload"}

    try:
        agent = _build_agent()
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("orchestrator build failure:\n%s", tb)
        return {"error": f"orchestrator build failed: {type(e).__name__}: {e}", "trace": tb}

    try:
        result = agent(prompt)
        return {"result": str(result)}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("orchestrator invoke failure:\n%s", tb)
        return {"error": f"orchestrator invoke failed: {type(e).__name__}: {e}", "trace": tb}


if __name__ == "__main__":
    # Use importlib.metadata for an accurate installed version, regardless of
    # whether the package exposes __version__.
    try:
        from importlib.metadata import version as _pkg_version
        logger.info("bedrock-agentcore version: %s", _pkg_version("bedrock-agentcore"))
    except Exception:
        logger.exception("could not read bedrock-agentcore version")

    # Inspect the BedrockAgentCoreApp object so we can see what surface it
    # actually exposes (routes, run signature, etc.) without needing the SDK
    # docs handy.
    try:
        attrs = [a for a in dir(_app) if not a.startswith("_")]
        logger.info("BedrockAgentCoreApp public attrs: %s", attrs)
        import inspect
        sig = inspect.signature(_app.run)
        logger.info("_app.run signature: %s", sig)
    except Exception:
        logger.exception("could not introspect BedrockAgentCoreApp")

    # Log any env vars AgentCore Runtime might inject for port/host.
    relevant_env = {
        k: v for k, v in os.environ.items()
        if any(token in k.upper() for token in ("PORT", "HOST", "BEDROCK", "AGENT"))
    }
    logger.info("relevant env vars: %s", relevant_env)

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    logger.info("starting BedrockAgentCoreApp on %s:%d", host, port)
    try:
        _app.run(host=host, port=port)
    except TypeError:
        logger.warning("_app.run(host, port) signature rejected; falling back to _app.run()")
        _app.run()
    except Exception:
        logger.exception("_app.run() raised unexpectedly")
        raise
