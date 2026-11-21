// Runs the widget script in Node with a minimal fake DOM and a fake host, and checks the
// MCP Apps handshake, rendering, filtering, sorting and link safety.
const fs = require("fs"), path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "widget", "dashboard.html"), "utf8");
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];
const sample = JSON.parse(fs.readFileSync(path.join(__dirname, "sample_payload.json"), "utf8"));

const sent = []; let listener = null; let results = [];
const check = (n, c) => { results.push([n, !!c]); console.log((c ? "PASS " : "FAIL ") + n); };
const appEl = { innerHTML: "", querySelectorAll: () => [], querySelector: () => null };
const docEl = { scrollHeight: 640, scrollWidth: 800, style: { setProperty() {} }, setAttribute(k, v) { this[k] = v; } };
const parent = { postMessage: (m) => sent.push(m) };
const win = {
  parent, addEventListener: (t, f) => { if (t === "message") listener = f; }, open() {},
};
const ctx = { window: win, document: { getElementById: () => appEl, documentElement: docEl, body: {} },
  setTimeout: (f, ms) => (ms >= 15000 ? 0 : setTimeout(f, 0)), Promise, Math, JSON, String, Object, Array, parseFloat, isNaN };
win.window = win;
new Function(...Object.keys(ctx), js)(...Object.values(ctx));

(async () => {
  const init = sent.find((m) => m.method === "ui/initialize");
  check("view sends ui/initialize with appCapabilities", init && init.params.appCapabilities);
  listener({ data: { jsonrpc: "2.0", id: init.id, result: { hostContext: { theme: "dark", displayMode: "inline", availableDisplayModes: ["inline", "fullscreen"] } } } });
  await new Promise((r) => setTimeout(r, 10));
  check("view sends ui/notifications/initialized after the reply", sent.some((m) => m.method === "ui/notifications/initialized"));
  check("host theme applied", docEl["data-theme"] === "dark");
  // simulate the server's normalised result
  const { execFileSync } = require("child_process");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const script = "import json,sys; sys.path.insert(0,'.'); import server; print(json.dumps(server.call_tool('render_dashboard', json.load(open('tests/sample_payload.json')))['structuredContent']))";
  const sc = JSON.parse(execFileSync(python, ["-c", script], { cwd: path.join(__dirname, "..") }).toString());
  listener({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { content: [], structuredContent: sc } } });
  const out = appEl.innerHTML;
  check("title rendered", out.includes("Copilot updates - last 7 days"));
  check("4 KPI tiles rendered", (out.match(/class="kpi"/g) || []).length === 4);
  check("3 charts rendered (bar, donut, line)", (out.match(/<svg/g) || []).length === 3 && out.includes("<polyline") && out.includes("<path"));
  check("table with 5 rows rendered", (out.match(/data-ask="/g) || []).length === 5);
  check("3 filter drop-downs rendered", (out.match(/data-filter="/g) || []).length === 3);
  check("full-screen button offered when host supports it", out.includes('data-act="fs"'));
  check("javascript: link not rendered as a link", !out.includes("javascript:"));
  check("4 safe links rendered", (out.match(/data-url="https:/g) || []).length === 4);
  check("text is HTML-escaped", !/<script>alert/.test(out));
  // XSS check: inject markup in a cell
  const evil = JSON.parse(JSON.stringify(sc)); evil.table.rows[0].title = '<img src=x onerror=alert(1)>';
  listener({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: evil } } });
  check("cell markup is escaped (no live <img onerror>)", !appEl.innerHTML.includes("<img src=x") && appEl.innerHTML.includes("&lt;img"));
  const failed = results.filter((r) => !r[1]).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
})();
