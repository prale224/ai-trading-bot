"""A narrowly scoped OpenJarvis MCP bridge for simulated PAPER orders.

This process exposes one MCP tool and can issue only GET /health and
POST /paper/orders to the existing paid PAPER service. It never receives
market prices or quantities from the model; the bot remains authoritative
for quote freshness, sizing, and risk checks.
"""
import ctypes
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes


SERVICE_BASE_URL = "https://paper-trading-bot-jzgh.onrender.com"
CREDENTIAL_TARGET = "YarvisPaperTradingOrderToken"
TOOL_NAME = "yarvis_open_paper_order"
PROTOCOL_VERSION = "2025-03-26"
REQUEST_TIMEOUT_SECONDS = 12
MAX_RESPONSE_BYTES = 256 * 1024

_SAFE_ORDER_REASONS = {
    "DATABASE_UNAVAILABLE", "DAILY_LOSS_LIMIT", "DUPLICATE_SIGNAL",
    "EMERGENCY_STOP", "INSUFFICIENT_SIMULATED_BALANCE", "INVALID_ORDER",
    "INVALID_PRICE", "INVALID_SIGNAL_ID", "MAX_OPEN_POSITIONS",
    "MAX_TOTAL_RISK", "PAPER_API_TOKEN_REQUIRED", "POSITION_ALREADY_OPEN",
    "RISK_UNAVAILABLE", "SPREAD_LIMIT", "STALE_MARKET_DATA", "UNKNOWN_SYMBOL",
}


class PaperToolError(Exception):
    """An expected, safe-to-return failure with a stable reason code."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _credential_api():
    if os.name != "nt":
        raise PaperToolError("CREDENTIAL_STORE_UNAVAILABLE",
                             "This bridge requires Windows Credential Manager.")
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return advapi32


def store_order_token(token):
    """Save a generated order-only token in the current Windows user vault."""
    if not isinstance(token, str) or len(token) < 32:
        raise PaperToolError("INVALID_CREDENTIAL", "The generated credential is invalid.")
    try:
        blob_bytes = token.encode("ascii")
    except UnicodeEncodeError:
        raise PaperToolError("INVALID_CREDENTIAL", "The generated credential is invalid.") from None
    if len(blob_bytes) > 512:
        raise PaperToolError("INVALID_CREDENTIAL", "The generated credential is invalid.")

    api = _credential_api()
    blob = (ctypes.c_ubyte * len(blob_bytes)).from_buffer_copy(blob_bytes)
    credential = _Credential()
    credential.Flags = 0
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = CREDENTIAL_TARGET
    credential.Comment = "Yarvis PAPER order access; only POST /paper/orders"
    credential.CredentialBlobSize = len(blob_bytes)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE, scoped to this Windows user
    credential.AttributeCount = 0
    credential.Attributes = None
    credential.TargetAlias = None
    credential.UserName = "Yarvis PAPER order tool"
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise PaperToolError("CREDENTIAL_STORE_WRITE_FAILED",
                             "Windows Credential Manager could not save the PAPER credential.")


def load_order_token():
    """Read the scoped API token from the current Windows user's vault."""
    api = _credential_api()
    pointer = ctypes.POINTER(_Credential)()
    if not api.CredReadW(CREDENTIAL_TARGET, 1, 0, ctypes.byref(pointer)):
        raise PaperToolError("CREDENTIAL_MISSING",
                             "The Yarvis PAPER credential is missing from Windows Credential Manager.")
    try:
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
    finally:
        api.CredFree(pointer)
    try:
        token = blob.decode("ascii")
    except UnicodeDecodeError:
        raise PaperToolError("CREDENTIAL_INVALID",
                             "The Yarvis PAPER credential could not be read safely.") from None
    if len(token) < 32:
        raise PaperToolError("CREDENTIAL_INVALID",
                             "The Yarvis PAPER credential could not be read safely.")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(method, path, body=None, token=None):
    """Call only the two fixed, allowlisted PAPER service routes."""
    if (method, path) not in (("GET", "/health"), ("POST", "/paper/orders")):
        raise PaperToolError("ROUTE_NOT_ALLOWED", "The requested route is not available to Yarvis.")
    url = SERVICE_BASE_URL + path
    headers = {"Accept": "application/json"}
    data = None
    if method == "POST":
        if not token:
            raise PaperToolError("CREDENTIAL_MISSING", "The Yarvis PAPER credential is unavailable.")
        headers["Authorization"] = "Bearer " + token
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PaperToolError("SERVICE_UNAVAILABLE",
                             "The PAPER service could not be reached; no order was submitted.") from None
    try:
        status = response.getcode()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        response.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PaperToolError("INVALID_SERVICE_RESPONSE", "The PAPER service response was too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PaperToolError("INVALID_SERVICE_RESPONSE", "The PAPER service returned invalid JSON.") from None
    if not isinstance(payload, dict):
        raise PaperToolError("INVALID_SERVICE_RESPONSE", "The PAPER service returned an invalid response.")
    return status, payload


def execute_paper_order(symbol, side, *, requester=None, token_reader=None, order_id_factory=None):
    """Open one simulated position only after live health and quote checks."""
    symbol = symbol.upper() if isinstance(symbol, str) else ""
    side = side.upper() if isinstance(side, str) else ""
    if symbol not in ("US100", "US500") or side not in ("BUY", "SELL"):
        raise PaperToolError("INVALID_ORDER", "Choose US100 or US500 and BUY or SELL.")
    requester = request_json if requester is None else requester
    token_reader = load_order_token if token_reader is None else token_reader
    order_id_factory = uuid.uuid4 if order_id_factory is None else order_id_factory

    status, health = requester("GET", "/health")
    if status != 200:
        raise PaperToolError("SERVICE_UNAVAILABLE", "The PAPER health check failed; no order was submitted.")
    if health.get("mode") != "PAPER_ONLY":
        raise PaperToolError("PAPER_MODE_REQUIRED", "The service did not confirm PAPER_ONLY mode.")
    if health.get("status") != "healthy":
        raise PaperToolError("SERVICE_NOT_HEALTHY", "The PAPER service is not healthy; no order was submitted.")
    if health.get("us_cash_session_clock_open") is not True:
        raise PaperToolError("SESSION_CLOSED", "The standard U.S. cash session is closed; no order was submitted.")
    markets = health.get("markets")
    market = markets.get(symbol) if isinstance(markets, dict) else None
    if not isinstance(market, dict) or market.get("fresh") is not True:
        raise PaperToolError("STALE_MARKET_DATA", "The selected quote is not fresh; no order was submitted.")

    token = token_reader()
    if not isinstance(token, str) or len(token) < 32:
        raise PaperToolError("CREDENTIAL_INVALID", "The Yarvis PAPER credential is invalid.")
    signal_id = "yarvis-" + str(order_id_factory().hex)
    status, result = requester("POST", "/paper/orders",
                               {"symbol": symbol, "side": side, "signal_id": signal_id}, token)
    if status == 200 and result.get("ok") is True:
        return result
    reason = result.get("reason")
    if not isinstance(reason, str) or reason not in _SAFE_ORDER_REASONS:
        reason = "PAPER_ORDER_REJECTED"
    return {"ok": False, "reason": reason}


def _tool_result(payload, *, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
            "isError": is_error}


def _response(request_id, result=None, error=None):
    response = {"jsonrpc": "2.0", "id": request_id}
    response["error" if error is not None else "result"] = error if error is not None else result
    return response


def handle_message(message, *, order_executor=None):
    """Handle one OpenJarvis MCP JSON-RPC line; notifications return None."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _response(message.get("id") if isinstance(message, dict) else None,
                         error={"code": -32600, "message": "Invalid JSON-RPC request"})
    request_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        if request_id is None:
            return None
        return _response(request_id, error={"code": -32600, "message": "Invalid JSON-RPC request"})
    if request_id is None:
        return None
    params = message.get("params")
    params = params if isinstance(params, dict) else {}

    if method == "initialize":
        if params.get("protocolVersion") != PROTOCOL_VERSION:
            return _response(request_id, error={"code": -32602, "message": "Unsupported MCP protocol version"})
        return _response(request_id, result={
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "yarvis-paper-order", "version": "1.0.0"},
        })
    if method == "ping":
        return _response(request_id, result={})
    if method == "tools/list":
        return _response(request_id, result={"tools": [{
            "name": TOOL_NAME,
            "description": (
                "Open one simulated PAPER position in the existing bot. Checks the public health "
                "endpoint, PAPER_ONLY mode, open U.S. cash session, and a fresh quote first. "
                "Only POST /paper/orders is authorized; the bot controls position size and risk. "
                "No live broker or real-money orders."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "enum": ["US100", "US500"]},
                    "side": {"type": "string", "enum": ["BUY", "SELL"]},
                },
                "required": ["symbol", "side"],
                "additionalProperties": False,
            },
        }]})
    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return _response(request_id, error={"code": -32602, "message": "Unknown tool"})
        arguments = params.get("arguments")
        if not isinstance(arguments, dict) or set(arguments) != {"symbol", "side"}:
            return _response(request_id, result=_tool_result(
                {"ok": False, "reason": "INVALID_ORDER", "message": "Provide only symbol and side."},
                is_error=True))
        executor = execute_paper_order if order_executor is None else order_executor
        try:
            result = executor(arguments["symbol"], arguments["side"])
        except PaperToolError as exc:
            return _response(request_id, result=_tool_result(
                {"ok": False, "reason": exc.reason, "message": exc.message}, is_error=True))
        return _response(request_id, result=_tool_result(result, is_error=result.get("ok") is not True))
    if method == "notifications/initialized":
        return None
    return _response(request_id, error={"code": -32601, "message": "Method not found"})


def serve_stdio():
    """Serve newline-delimited JSON-RPC on stdio, with no console noise."""
    for line in sys.stdin:
        try:
            if len(line) > 1024 * 1024:
                message = {"jsonrpc": "2.0", "id": None, "method": "invalid"}
                response = _response(None, error={"code": -32600, "message": "Request is too large"})
            else:
                message = json.loads(line)
                response = handle_message(message)
        except json.JSONDecodeError:
            response = _response(None, error={"code": -32700, "message": "Parse error"})
        except Exception:
            response = _response(None, error={"code": -32603, "message": "Internal error"})
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--provision-key"]:
        token = secrets.token_urlsafe(48)
        store_order_token(token)
        # Callers should capture this output and pass it directly to the
        # Render environment-variable API without displaying or logging it.
        sys.stdout.write(token + "\n")
        return 0
    if argv == ["--check-credential"]:
        load_order_token()
        sys.stdout.write("credential_found\n")
        return 0
    if argv == ["--serve"]:
        serve_stdio()
        return 0
    sys.stderr.write("Usage: yarvis_paper_mcp.py --serve|--provision-key|--check-credential\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

