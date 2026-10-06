"""LLM providers: "system prompt + input + JSON schema -> JSON", nothing else.

No provider gets tools. Callers validate the returned JSON themselves; every call is recorded in
the `llm_call` audit table by `call_llm`.
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from majordome.config import Settings
from majordome.store.models import LlmCall


class LlmError(RuntimeError):
    pass


@dataclass
class LlmResult:
    output: dict[str, Any]
    model: str | None
    usage: dict[str, Any] | None
    duration_ms: int


class Provider(Protocol):
    name: str

    async def complete(self, system: str, input: str, schema: dict[str, Any],
                       model: str) -> LlmResult: ...


class GatewayProvider:
    """Claude Code in the sandboxed `llm` container, through deploy/llm/gateway.mjs."""

    name = "claude_code"

    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(base_url=url, timeout=240, transport=transport,
                                      headers={"Authorization": f"Bearer {token}"})

    async def complete(self, system: str, input: str, schema: dict[str, Any],
                       model: str) -> LlmResult:
        started = time.monotonic()
        try:
            r = await self.http.post("/complete", json={
                "system": system, "input": input, "schema": schema, "model": model})
        except httpx.HTTPError as e:
            raise LlmError(f"gateway unreachable: {e}") from e
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not isinstance(body.get("output"), dict):
            raise LlmError(f"gateway {r.status_code}: {body.get('error') or r.text[:200]}")
        return LlmResult(body["output"], body.get("model"), body.get("usage"),
                         int(body.get("duration_ms") or (time.monotonic() - started) * 1000))


def provider_from(cfg: Settings) -> Provider:
    return GatewayProvider(cfg.llm_gateway_url, cfg.llm_gateway_token)


async def call_llm(s: AsyncSession, provider: Provider, *, purpose: str, system: str, input: str,
                   schema: dict[str, Any], model: str,
                   message_ids: list[int]) -> tuple[LlmResult | None, LlmCall]:
    """Call the provider and record the call (successful or not) in the audit log."""
    record = LlmCall(purpose=purpose, provider=provider.name, model=model,
                     message_ids=message_ids, input_chars=len(system) + len(input))
    s.add(record)
    try:
        result = await provider.complete(system, input, schema, model)
    except LlmError as e:
        record.error = str(e)[:2000]
        await s.flush()
        return None, record
    record.model = result.model or model
    record.output = json.loads(json.dumps(result.output))  # plain JSON only
    record.usage = result.usage
    record.duration_ms = result.duration_ms
    await s.flush()
    return result, record
