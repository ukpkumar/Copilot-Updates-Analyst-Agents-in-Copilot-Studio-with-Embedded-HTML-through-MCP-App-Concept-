"""
Copilot Dashboard MCP server  -  interactive HTML dashboards for Copilot Studio agents.

Implements the Model Context Protocol (Streamable HTTP transport, JSON responses) plus the
MCP Apps extension (io.modelcontextprotocol/ui). One tool, `render_dashboard`, takes the data
the agent has gathered (KPIs, charts, a table) and returns it as structuredContent. The tool
declares a UI resource (ui://copilot-dashboard/dashboard.html) which the host - Microsoft 365
Copilot or Copilot Studio's test chat - fetches and renders as an interactive, sandboxed
HTML widget inside the conversation.

Standard library only: no packages to install. Python 3.10+.

Environment variables
  PORT         port to listen on (default 8000)
  MCP_API_KEY  optional. When set, every request must send it in the `x-api-key` header
               (or `Authorization: Bearer <key>`). Configure the same key in Copilot Studio
               (Authentication: API key, Header, name x-api-key).
"""
from __future__ import annotations

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER_NAME = "copilot-dashboard-mcp"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
UI_URI = "ui://copilot-dashboard/dashboard.html"
UI_MIME = "text/html;profile=mcp-app"
WIDGET_PATH = Path(__file__).with_name("widget") / "dashboard.html"

MAX_ROWS, MAX_COLS, MAX_KPIS, MAX_CHARTS, MAX_POINTS = 500, 12, 12, 6, 60

# ---------------------------------------------------------------- tool + resource definitions
DASHBOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Dashboard title, e.g. 'Copilot updates - last 7 days'."},
        "subtitle": {"type": "string", "description": "One line of context: scope, date range, data source."},
        "generatedAt": {"type": "string", "description": "When the data was read (ISO date or readable text)."},
        "kpis": {
            "type": "array", "maxItems": MAX_KPIS,
            "description": "Headline numbers shown as tiles.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": ["string", "number"]},
                    "delta": {"type": "string", "description": "Change vs previous period, e.g. '+4 vs last week'."},
                    "trend": {"type": "string", "enum": ["up", "down", "flat"]},
                },
                "required": ["label", "value"],
            },
        },
        "charts": {
            "type": "array", "maxItems": MAX_CHARTS,
            "description": "Charts. bar/line: labels on the x-axis, one or more series. donut: one series of shares.",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["bar", "line", "donut"]},
                    "title": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "series": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "values": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["values"],
                        },
                    },
                },
                "required": ["type", "labels", "series"],
            },
        },
        "table": {
            "type": "object",
            "description": "Detail rows. Users can search, sort and filter them.",
            "properties": {
                "columns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "label": {"type": "string"},
                            "type": {"type": "string", "enum": ["text", "number", "date", "link"]},
                        },
                        "required": ["key"],
                    },
                },
                "rows": {"type": "array", "items": {"type": "object"}, "description": "One object per row, keyed by column key."},
            },
            "required": ["columns", "rows"],
        },
        "filterKeys": {
            "type": "array", "items": {"type": "string"},
            "description": "Column keys to offer as drop-down filters, e.g. ['source','product','changeType'].",
        },
        "notes": {"type": "string", "description": "Optional footnote: caveats, data gaps."},
    },
    "required": ["title"],
}

TOOLS = [
    {
        "name": "render_dashboard",
        "title": "Render interactive dashboard",
        "description": (
            "Shows data as an interactive dashboard (KPI tiles, charts, and a searchable, sortable, "
            "filterable table) rendered inside the chat. Call it whenever the user asks for a dashboard, "
            "overview, report, chart, trend, breakdown or a list of more than a few records. Pass the data "
            "you already retrieved; this tool does not look anything up."
        ),
        "inputSchema": DASHBOARD_SCHEMA,
        "annotations": {"title": "Render interactive dashboard", "readOnlyHint": True, "openWorldHint": False},
        "_meta": {
            "ui": {"resourceUri": UI_URI, "visibility": ["model", "app"]},
            # OpenAI Apps SDK keys, so the same server also renders in ChatGPT
            "openai/outputTemplate": UI_URI,
            "openai/toolInvocation/invoking": "Building dashboard…",
            "openai/toolInvocation/invoked": "Dashboard ready",
            "openai/widgetAccessible": True,
        },
    }
]

RESOURCE_META = {
    "ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": []}},
    "openai/widgetDescription": "Interactive dashboard of the data the agent retrieved.",
    "openai/widgetPrefersBorder": True,
    "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
}

RESOURCES = [
    {
        "uri": UI_URI,
        "name": "dashboard",
        "title": "Interactive dashboard",
        "description": "HTML view that renders render_dashboard results.",
        "mimeType": UI_MIME,
        "_meta": RESOURCE_META,
    }
]


# ---------------------------------------------------------------- tool logic
def _clip(text, n=200):
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def normalise_dashboard(args: dict) -> dict:
    """Validate and bound the payload so the widget always receives a predictable shape."""
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    title = _clip(args.get("title") or "Dashboard", 120)
    out = {
        "title": title,
        "subtitle": _clip(args.get("subtitle"), 300),
        "generatedAt": _clip(args.get("generatedAt"), 60),
        "notes": _clip(args.get("notes"), 1000),
        "kpis": [],
        "charts": [],
        "table": {"columns": [], "rows": []},
        "filterKeys": [],
    }
    for k in (args.get("kpis") or [])[:MAX_KPIS]:
        if isinstance(k, dict) and "label" in k:
            trend = k.get("trend") if k.get("trend") in ("up", "down", "flat") else None
            out["kpis"].append({"label": _clip(k["label"], 60), "value": _clip(k.get("value"), 40),
                                "delta": _clip(k.get("delta"), 60), "trend": trend})
    for c in (args.get("charts") or [])[:MAX_CHARTS]:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type") if c.get("type") in ("bar", "line", "donut") else "bar"
        labels = [_clip(x, 40) for x in (c.get("labels") or [])][:MAX_POINTS]
        series = []
        for s in (c.get("series") or [])[:6]:
            vals = []
            for v in (s.get("values") or [])[: len(labels)]:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    vals.append(0.0)
            series.append({"name": _clip(s.get("name"), 40), "values": vals})
        if labels and series:
            out["charts"].append({"type": ctype, "title": _clip(c.get("title"), 80), "labels": labels, "series": series})
    table = args.get("table") or {}
    cols = []
    for c in (table.get("columns") or [])[:MAX_COLS]:
        if isinstance(c, str):
            c = {"key": c}
        if isinstance(c, dict) and c.get("key"):
            ctype = c.get("type") if c.get("type") in ("text", "number", "date", "link") else "text"
            cols.append({"key": str(c["key"]), "label": _clip(c.get("label") or c["key"], 40), "type": ctype})
    rows = []
    for r in (table.get("rows") or [])[:MAX_ROWS]:
        if isinstance(r, list):
            r = {cols[i]["key"]: v for i, v in enumerate(r) if i < len(cols)}
        if isinstance(r, dict):
            row = {}
            for c in cols:
                v = r.get(c["key"])
                if c["type"] == "link":
                    v = v if isinstance(v, str) and v.lower().startswith(("https://", "http://")) else None
                row[c["key"]] = v if isinstance(v, (int, float)) or v is None else _clip(v, 500)
            rows.append(row)
    out["table"] = {"columns": cols, "rows": rows}
    keys = {c["key"] for c in cols}
    out["filterKeys"] = [k for k in (args.get("filterKeys") or []) if k in keys][:4]
    return out


def text_fallback(d: dict) -> str:
    """Plain-text/markdown version for the model and for hosts that cannot render the widget."""
    lines = [f"Dashboard rendered: {d['title']}"]
    if d["subtitle"]:
        lines.append(d["subtitle"])
    for k in d["kpis"]:
        lines.append(f"- {k['label']}: {k['value']}" + (f" ({k['delta']})" if k.get("delta") else ""))
    cols, rows = d["table"]["columns"], d["table"]["rows"]
    if cols:
        lines.append(f"Table: {len(rows)} rows. First rows:")
        lines.append("| " + " | ".join(c["label"] for c in cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for r in rows[:10]:
            lines.append("| " + " | ".join(_clip(r.get(c["key"]), 60) if r.get(c["key"]) is not None else "" for c in cols) + " |")
    lines.append("The user can see the interactive dashboard; summarise the key takeaways in 2-4 sentences "
                 "and do not repeat the whole table.")
    return "\n".join(lines)


def call_tool(name: str, args: dict) -> dict:
    if name != "render_dashboard":
        raise KeyError(name)
    try:
        d = normalise_dashboard(args or {})
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"Invalid dashboard data: {e}"}], "isError": True}
    return {
        "content": [{"type": "text", "text": text_fallback(d)}],
        "structuredContent": d,
        "_meta": {"openai/outputTemplate": UI_URI},
    }


def read_resource(uri: str) -> dict:
    if uri != UI_URI:
        raise KeyError(uri)
    html = WIDGET_PATH.read_text(encoding="utf-8")
    return {"contents": [{"uri": UI_URI, "mimeType": UI_MIME, "text": html, "_meta": RESOURCE_META}]}


# ---------------------------------------------------------------- JSON-RPC dispatch
def handle_rpc(msg: dict):
    """Return a JSON-RPC response dict, or None for notifications."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    is_notification = "id" not in msg

    def ok(result):
        return None if is_notification else {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return None if is_notification else {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return ok({
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": [UI_MIME]}},
            },
            "serverInfo": {"name": SERVER_NAME, "title": "Copilot Dashboard", "version": SERVER_VERSION},
            "instructions": "Use render_dashboard to show retrieved data as an interactive dashboard in chat.",
        })
    if method and method.startswith("notifications/"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        try:
            return ok(call_tool(params.get("name"), params.get("arguments") or {}))
        except KeyError:
            return err(-32602, f"Unknown tool: {params.get('name')}")
    if method == "resources/list":
        return ok({"resources": RESOURCES})
    if method == "resources/templates/list":
        return ok({"resourceTemplates": []})
    if method == "resources/read":
        try:
            return ok(read_resource(params.get("uri")))
        except KeyError:
            return err(-32002, f"Resource not found: {params.get('uri')}")
    if method == "prompts/list":
        return ok({"prompts": []})
    return err(-32601, f"Method not found: {method}")


class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def _send(self, status, body=None, extra=None):
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _authorised(self):
        key = os.environ.get("MCP_API_KEY")
        if not key:
            return True
        supplied = self.headers.get("x-api-key") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            supplied = supplied or auth[7:].strip()
        return supplied == key

    def do_GET(self):
        if self.path in ("/", "/healthz"):
            return self._send(200, {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})
        # This server does not open server-initiated SSE streams.
        return self._send(405, {"error": "Method not allowed"}, {"Allow": "POST"})

    def do_DELETE(self):
        return self._send(200 if self.path.startswith("/mcp") else 404, {})

    def do_POST(self):
        if not self.path.startswith("/mcp"):
            return self._send(404, {"error": "Not found"})
        if not self._authorised():
            return self._send(401, {"error": "Unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        headers = {}
        if isinstance(payload, dict) and payload.get("method") == "initialize":
            headers["Mcp-Session-Id"] = uuid.uuid4().hex
        if isinstance(payload, list):
            responses = [r for r in (handle_rpc(m) for m in payload if isinstance(m, dict)) if r is not None]
            return self._send(200, responses, headers) if responses else self._send(202, None, headers)
        if not isinstance(payload, dict):
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
        response = handle_rpc(payload)
        return self._send(202, None, headers) if response is None else self._send(200, response, headers)

    def log_message(self, fmt, *args):  # concise access log
        print(f"{self.address_string()} {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


def main():
    port = int(os.environ.get("PORT", "8000"))
    print(f"{SERVER_NAME} {SERVER_VERSION} listening on :{port}/mcp")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
