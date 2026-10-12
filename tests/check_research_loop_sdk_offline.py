"""Serialize the real SDK requests, with sockets and DNS blocked."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def deny_network(*args, **kwargs):
    raise AssertionError("Network forbidden in research loop SDK check")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network
socket.getaddrinfo = deny_network

import httpx
import openai
import research_loop as loop
from test_research_loop import manifest, NOW, URL, response


async def check():
    assert openai.__version__ == "2.13.0"
    sent = []
    def handler(request):
        payload = json.loads(request.content)
        stage = loop.STAGES[len(sent)]
        sent.append(payload)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.method == "POST"
        assert payload["store"] is False and payload["service_tier"] == "default"
        assert "temperature" not in payload
        if stage == "research":
            assert payload["model"] == "gpt-6-luna"
            assert payload["tools"][0]["filters"] == {"allowed_domains": ["nasa.gov"]}
            assert payload["max_tool_calls"] == 2
        else:
            assert payload["model"] == "gpt-6.1-sol"
            assert payload["reasoning"] == {"effort": "high"}
            assert payload["tools"] == [] and payload["tool_choice"] == "none"
            if stage == "forecast":
                assert payload["text"]["format"]["schema"]["properties"]["source_urls"]["items"]["enum"] == [URL]
        data = response(stage)
        data.update(object="response", created_at=1)
        for i, item in enumerate(data["output"]):
            item["id"] = f"item_{len(sent)}_{i}"
        return httpx.Response(200, json=data)
    with tempfile.TemporaryDirectory() as temp:
        ledger = loop.Ledger(Path(temp) / "loop.sqlite", manifest(), Decimal("1"))
        try:
            async with openai.AsyncOpenAI(api_key="offline-placeholder", max_retries=0,
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
                result = await loop.run_loop(ledger, client, checkpoint=lambda r: None, clock=lambda: NOW)
                await loop.run_loop(ledger, client, checkpoint=lambda r: None, clock=lambda: NOW)
            assert len(sent) == 3 and result["completed_pairs"] == 1
            assert result["usage_complete"] is True
            print("Real SDK offline check passed: 3 stages; resumed with 0 duplicate calls.")
        finally:
            ledger.close()


if __name__ == "__main__":
    asyncio.run(check())
