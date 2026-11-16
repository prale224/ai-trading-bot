import json
import uuid

import pytest

from tools import yarvis_paper_mcp as bridge


def healthy(symbol="US100", *, fresh=True, status="healthy", mode="PAPER_ONLY", session=True):
    return {
        "status": status,
        "mode": mode,
        "us_cash_session_clock_open": session,
        "markets": {symbol: {"fresh": fresh}},
    }


def test_mcp_initialize_and_tool_list_expose_exactly_one_scoped_action():
    initialized = bridge.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    })
    listed = bridge.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert initialized["result"]["protocolVersion"] == "2025-03-26"
    tools = listed["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["yarvis_open_paper_order"]
    assert set(tools[0]["inputSchema"]["properties"]) == {"symbol", "side"}
    assert tools[0]["inputSchema"]["additionalProperties"] is False


@pytest.mark.parametrize("health", [
    healthy(mode="LIVE"),
    healthy(status="degraded"),
    healthy(session=False),
    healthy(fresh=False),
])
def test_preflight_blocks_orders_without_reading_token_or_posting(health):
    calls = []
    token_reads = []

    def requester(method, path, body=None, token=None):
        calls.append((method, path, body, token))
        return 200, health

    with pytest.raises(bridge.PaperToolError):
        bridge.execute_paper_order(
            "US100", "BUY", requester=requester,
            token_reader=lambda: token_reads.append(True),
        )

    assert calls == [("GET", "/health", None, None)]
    assert token_reads == []


def test_order_request_is_fixed_route_without_model_supplied_size_or_price():
    calls = []
    token = "test-only-order-token-with-at-least-32-characters"

    def requester(method, path, body=None, supplied_token=None):
        calls.append((method, path, body, supplied_token))
        if method == "GET":
            return 200, healthy("US500")
        return 200, {"ok": True, "position": {"symbol": "US500", "side": "SELL"}}

    result = bridge.execute_paper_order(
        "US500", "sell", requester=requester, token_reader=lambda: token,
        order_id_factory=lambda: uuid.UUID("12345678123456781234567812345678"),
    )

    assert result["ok"] is True
    assert calls[0] == ("GET", "/health", None, None)
    assert calls[1][0:2] == ("POST", "/paper/orders")
    assert calls[1][2] == {
        "symbol": "US500", "side": "SELL", "signal_id": "yarvis-12345678123456781234567812345678",
    }
    assert calls[1][3] == token
    assert not {"price", "quantity", "qty", "size"}.intersection(calls[1][2])


def test_non_health_routes_are_rejected_before_http_request(monkeypatch):
    monkeypatch.setattr(bridge.urllib.request, "build_opener", lambda *args: pytest.fail("HTTP opened"))
    with pytest.raises(bridge.PaperToolError, match="not available"):
        bridge.request_json("POST", "/paper/resume", {}, "test-token")


def test_http_redirects_are_not_followed():
    handler = bridge._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_tool_call_does_not_submit_during_discovery_and_uses_safe_result():
    tool_list = bridge.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert tool_list["result"]["tools"][0]["name"] == bridge.TOOL_NAME

    called = []
    response = bridge.handle_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": bridge.TOOL_NAME, "arguments": {"symbol": "US100", "side": "BUY"}},
    }, order_executor=lambda symbol, side: called.append((symbol, side)) or {
        "ok": False, "reason": "STALE_MARKET_DATA",
    })
    content = json.loads(response["result"]["content"][0]["text"])

    assert called == [("US100", "BUY")]
    assert response["result"]["isError"] is True
    assert content == {"ok": False, "reason": "STALE_MARKET_DATA"}


def test_invalid_tool_arguments_do_not_invoke_order_executor():
    called = []
    response = bridge.handle_message({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": bridge.TOOL_NAME,
                   "arguments": {"symbol": "US100", "side": "BUY", "quantity": 100}},
    }, order_executor=lambda *args: called.append(args))

    assert called == []
    assert response["result"]["isError"] is True
    assert "INVALID_ORDER" in response["result"]["content"][0]["text"]

