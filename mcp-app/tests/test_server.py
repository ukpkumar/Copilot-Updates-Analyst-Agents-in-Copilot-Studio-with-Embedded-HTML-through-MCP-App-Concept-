"""End-to-end protocol test: starts the server and walks the same calls Copilot Studio makes."""
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("TEST_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/mcp"
KEY = "test-key-123"

def post(body, key=KEY):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "x-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, dict(r.headers), (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), None

def main():
    env = dict(os.environ, PORT=str(PORT), MCP_API_KEY=KEY)
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    results = []
    def check(name, cond):
        results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name)
    try:
        time.sleep(1.0)
        s, _, _ = post({"jsonrpc": "2.0", "id": 0, "method": "ping"}, key="wrong")
        check("rejects a wrong API key (401)", s == 401)
        s, h, r = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
              "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}},
              "clientInfo": {"name": "test", "version": "1"}}})
        check("initialize returns 200 + protocol 2025-06-18", s == 200 and r["result"]["protocolVersion"] == "2025-06-18")
        check("initialize advertises the MCP Apps extension", "io.modelcontextprotocol/ui" in r["result"]["capabilities"]["extensions"])
        check("initialize issues an Mcp-Session-Id", any(k.lower() == "mcp-session-id" for k in h))
        s, _, _ = post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("initialized notification -> 202", s == 202)
        s, _, r = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool = r["result"]["tools"][0]
        check("tools/list exposes render_dashboard", tool["name"] == "render_dashboard")
        check("tool declares _meta.ui.resourceUri", tool["_meta"]["ui"]["resourceUri"] == "ui://copilot-dashboard/dashboard.html")
        s, _, r = post({"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ui://copilot-dashboard/dashboard.html"}})
        c = r["result"]["contents"][0]
        check("resources/read returns text/html;profile=mcp-app", c["mimeType"] == "text/html;profile=mcp-app")
        check("widget HTML is served as text and is a full document", c["text"].lstrip().lower().startswith("<!doctype html>"))
        check("widget performs the ui/initialize handshake", "ui/initialize" in c["text"] and "ui/notifications/initialized" in c["text"])
        payload = json.loads((ROOT / "tests" / "sample_payload.json").read_text())
        s, _, r = post({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "render_dashboard", "arguments": payload}})
        res = r["result"]
        sc = res["structuredContent"]
        check("tools/call returns structuredContent for the widget", sc["title"] == payload["title"] and len(sc["table"]["rows"]) == 5)
        check("tools/call returns a text fallback for text-only hosts", res["content"][0]["type"] == "text" and "Dashboard rendered" in res["content"][0]["text"])
        check("unsafe link (javascript:) is stripped", sc["table"]["rows"][4]["link"] is None)
        check("filters keep only known columns", sc["filterKeys"] == ["product", "source", "changeType"])
        s, _, r = post({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
        check("unknown tool -> JSON-RPC error", "error" in r)
        s, _, r = post([{"jsonrpc": "2.0", "id": 6, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        check("batch requests handled", s == 200 and isinstance(r, list) and len(r) == 1)
        big = dict(payload, table={"columns": payload["table"]["columns"], "rows": payload["table"]["rows"] * 200})
        s, _, r = post({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "render_dashboard", "arguments": big}})
        check("row cap of 500 enforced", len(r["result"]["structuredContent"]["table"]["rows"]) == 500)
    finally:
        proc.terminate()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
