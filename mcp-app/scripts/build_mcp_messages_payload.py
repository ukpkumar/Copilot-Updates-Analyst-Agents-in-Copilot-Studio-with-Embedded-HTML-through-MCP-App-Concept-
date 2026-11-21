#!/usr/bin/env python
"""
Build an MCP dashboard payload from Copilot-related update sources.

Usage examples:
  python scripts/build_mcp_messages_payload.py --days 30 --out payload.json
  GRAPH_ACCESS_TOKEN=<token> python scripts/build_mcp_messages_payload.py --days 7
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Dict, Iterable, List, Optional

ROADMAP_RSS = "https://www.microsoft.com/releasecommunications/api/v2/m365/rss"
ROADMAP_JSON = "https://www.microsoft.com/releasecommunications/api/v2/m365"
SOURCES = [
    ("Microsoft 365 Blog", "https://www.microsoft.com/microsoft-365/blog/feed/"),
    ("Microsoft 365 Tech Community", "https://techcommunity.microsoft.com/t5/microsoft-365-blog/bg-p/microsoft_365blog/rss.board"),
    ("Copilot Studio Blog", "https://techcommunity.microsoft.com/t5/copilot-studio-blog/bg-p/CopilotStudioBlog/rss.board"),
]

KEYWORDS = [
    "copilot",
    "copilot studio",
    "agent 365",
    "cowork",
    "autopilot",
    "microsoft copilot code",
    "roadmap",
]

PRODUCT_HINTS = [
    "Copilot Studio",
    "Copilot",
    "Cowork",
    "Autopilot",
    "Agent 365",
    "Microsoft Copilot Code",
]


def fetch_bytes(url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "copilot-dashboard-mcp/1.0", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_dt(raw: Optional[str]) -> Optional[dt.datetime]:
    if not raw:
        return None
    value = raw.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is not None:
            return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    iso = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def text_or_empty(node: Optional[ET.Element]) -> str:
    if node is None or node.text is None:
        return ""
    return re.sub(r"\s+", " ", node.text).strip()


def clean_html_text(value: str) -> str:
    if not value:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def element_first(item: ET.Element, names: Iterable[str]) -> Optional[ET.Element]:
    for name in names:
        node = item.find(name)
        if node is not None:
            return node
    return None


def parse_rss_items(source: str, url: str) -> List[dict]:
    raw = fetch_bytes(url)
    root = ET.fromstring(raw)
    items = root.findall(".//item")
    entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    rows = []

    for item in items:
        title = text_or_empty(element_first(item, ["title"]))
        link_node = element_first(item, ["link"])
        link = text_or_empty(link_node)
        guid = text_or_empty(element_first(item, ["guid"]))
        pub = text_or_empty(element_first(item, ["pubDate", "lastBuildDate", "updated"]))
        summary = text_or_empty(element_first(item, ["description"]))
        rows.append(
            {
                "title": title,
                "link": link,
                "source_id": guid or link or stable_id(source, title, pub),
                "modified": parse_dt(pub),
                "summary": clean_html_text(summary),
                "source": source,
                "status": "",
                "roadmap_id": "",
            }
        )

    for entry in entries:
        title = text_or_empty(element_first(entry, ["{http://www.w3.org/2005/Atom}title"]))
        link = ""
        for ln in entry.findall("{http://www.w3.org/2005/Atom}link"):
            href = ln.attrib.get("href")
            if href:
                link = href
                break
        eid = text_or_empty(element_first(entry, ["{http://www.w3.org/2005/Atom}id"]))
        updated = text_or_empty(
            element_first(
                entry,
                [
                    "{http://www.w3.org/2005/Atom}updated",
                    "{http://www.w3.org/2005/Atom}published",
                ],
            )
        )
        summary = text_or_empty(element_first(entry, ["{http://www.w3.org/2005/Atom}summary"]))
        rows.append(
            {
                "title": title,
                "link": link,
                "source_id": eid or link or stable_id(source, title, updated),
                "modified": parse_dt(updated),
                "summary": clean_html_text(summary),
                "source": source,
                "status": "",
                "roadmap_id": "",
            }
        )

    return rows


def stable_id(source: str, title: str, stamp: str) -> str:
    key = f"{source}|{title}|{stamp}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def normalize_status(raw: str, rings: Optional[List[str]] = None) -> str:
    text = f"{raw or ''} {' '.join(rings or [])}".lower()
    if any(k in text for k in ("launched", "general availability", "ga", "available")):
        return "Generally available"
    if "rolling out" in text or "rollout" in text:
        return "Rolling out"
    if any(k in text for k in ("in development", "preview", "planned", "developing", "upcoming")):
        return "In development"
    return "Unknown"


def fetch_roadmap_rows_json() -> List[dict]:
    raw = fetch_bytes(ROADMAP_JSON)
    payload = json.loads(raw.decode("utf-8"))
    items = payload.get("items") or payload.get("value") or []
    rows = []
    for item in items:
        title = (item.get("title") or "").strip()
        description = clean_html_text(item.get("description") or "")
        modified_raw = item.get("modified") or item.get("created")
        more_urls = item.get("moreInfoUrls") or []
        first_url = more_urls[0] if more_urls else ""
        roadmap_id = str(item.get("id") or "").strip()
        status = normalize_status(item.get("status") or "", item.get("releaseRings") or [])
        rows.append(
            {
                "title": title,
                "link": first_url,
                "source_id": roadmap_id or first_url or stable_id("Roadmap", title, str(modified_raw)),
                "modified": parse_dt(modified_raw),
                "summary": description,
                "source": "Roadmap",
                "status": status,
                "roadmap_id": roadmap_id,
            }
        )
    return rows


def infer_product(text: str) -> str:
    lowered = text.lower()
    for product in PRODUCT_HINTS:
        if product.lower() in lowered:
            return product
    if "copilot" in lowered:
        return "Copilot"
    return "Microsoft 365"


def infer_change_type(text: str) -> str:
    lowered = text.lower()
    if any(k in lowered for k in ("retire", "deprecat", "end of support")):
        return "Retirement"
    if any(k in lowered for k in ("new", "introduc", "launch", "preview", "general availability", "ga")):
        return "New feature"
    return "Update"


def keyword_match(text: str) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in KEYWORDS)


def fetch_message_center_rows(graph_token: Optional[str]) -> List[dict]:
    if not graph_token:
        return []
    url = "https://graph.microsoft.com/beta/admin/serviceAnnouncement/messages?$top=100"
    raw = fetch_bytes(url, headers={"Authorization": f"Bearer {graph_token}"})
    payload = json.loads(raw.decode("utf-8"))
    rows = []
    for msg in payload.get("value", []):
        title = msg.get("title") or ""
        mc_id = msg.get("id") or ""
        modified_raw = msg.get("lastModifiedDateTime") or msg.get("startDateTime")
        rows.append(
            {
                "title": title,
                "link": msg.get("messageWebUrl") or "",
                "source_id": mc_id or stable_id("Message Center", title, str(modified_raw)),
                "modified": parse_dt(modified_raw),
                "summary": msg.get("body", {}).get("content", "") if isinstance(msg.get("body"), dict) else "",
                "source": "Message Center",
                "status": normalize_status(msg.get("severity", "")),
                "roadmap_id": "",
            }
        )
    return rows


def trim_and_filter(rows: List[dict], days: int, max_items: int) -> List[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days)
    out = []
    seen = set()
    for row in rows:
        title_text = (row.get("title") or "").strip()
        if not title_text:
            continue
        text = " ".join([title_text, row.get("summary", "")]).strip()
        if not keyword_match(text):
            continue
        modified = row.get("modified")
        if modified and modified < start:
            continue
        link = row.get("link", "")
        if link and not link.lower().startswith(("http://", "https://")):
            link = ""
        key = f"{row.get('source')}|{row.get('source_id')}|{row.get('title','').strip().lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "title": row.get("title", "").strip() or "(untitled)",
                "product": infer_product(text),
                "source": row.get("source", "Unknown"),
                "changeType": infer_change_type(text),
                "modified": (modified or now).astimezone(dt.timezone.utc),
                "link": link,
                "sourceId": row.get("source_id", ""),
                "status": row.get("status") or "Unknown",
                "description": clean_html_text(row.get("summary") or ""),
                "roadmapId": row.get("roadmap_id") or "",
            }
        )

    out.sort(key=lambda x: x["modified"], reverse=True)
    return out[:max_items]


def series_by_day(rows: List[dict], days: int) -> dict:
    now = dt.datetime.now(dt.timezone.utc).date()
    labels = []
    counts = []
    by_day = Counter(r["modified"].date() for r in rows)
    for i in range(days - 1, -1, -1):
        day = now - dt.timedelta(days=i)
        labels.append(day.strftime("%b %d"))
        counts.append(by_day.get(day, 0))
    return {"labels": labels, "counts": counts}


def build_payload(rows: List[dict], days: int, skipped_sources: List[str]) -> dict:
    by_source = Counter(r["source"] for r in rows)
    by_product = Counter(r["product"] for r in rows)
    by_change = Counter(r["changeType"] for r in rows)
    by_day = series_by_day(rows, max(days, 1))
    top_products = by_product.most_common(6)
    notes = []
    if skipped_sources:
        notes.append("Skipped sources: " + ", ".join(skipped_sources))
    notes.append("Message Center requires GRAPH_ACCESS_TOKEN for ingestion from Graph.")
    notes.append("Payload generated for MCP app render_dashboard tool.")

    return {
        "title": f"Copilot updates - last {days} days",
        "subtitle": "Roadmap, Message Center, official Microsoft blogs, and tech updates",
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "kpis": [
            {"label": "Total updates", "value": len(rows)},
            {"label": "Products covered", "value": len(by_product)},
            {"label": "Message Center items", "value": by_source.get("Message Center", 0)},
            {"label": "Roadmap items", "value": by_source.get("Roadmap", 0)},
        ],
        "charts": [
            {
                "type": "bar",
                "title": "Updates by product",
                "labels": [name for name, _ in top_products],
                "series": [{"name": "Updates", "values": [count for _, count in top_products]}],
            },
            {
                "type": "donut",
                "title": "By source",
                "labels": list(by_source.keys()),
                "series": [{"name": "Count", "values": [by_source[k] for k in by_source.keys()]}],
            },
            {
                "type": "line",
                "title": "Updates per day",
                "labels": by_day["labels"],
                "series": [{"name": "Updates", "values": by_day["counts"]}],
            },
            {
                "type": "donut",
                "title": "By change type",
                "labels": list(by_change.keys()),
                "series": [{"name": "Count", "values": [by_change[k] for k in by_change.keys()]}],
            },
        ],
        "table": {
            "columns": [
                {"key": "title", "label": "Title"},
                {"key": "product", "label": "Product"},
                {"key": "source", "label": "Source"},
                {"key": "status", "label": "Status"},
                {"key": "changeType", "label": "Change type"},
                {"key": "modified", "label": "Modified", "type": "date"},
                {"key": "roadmapId", "label": "Roadmap ID"},
                {"key": "link", "label": "Link", "type": "link"},
                {"key": "sourceId", "label": "Source ID"},
                {"key": "description", "label": "Description"},
            ],
            "rows": [
                {
                    "title": r["title"],
                    "product": r["product"],
                    "source": r["source"],
                    "status": r["status"],
                    "changeType": r["changeType"],
                    "modified": r["modified"].strftime("%Y-%m-%d"),
                    "roadmapId": r["roadmapId"],
                    "link": r["link"],
                    "sourceId": r["sourceId"],
                    "description": r["description"],
                }
                for r in rows
            ],
        },
        "filterKeys": ["product", "source", "status", "changeType"],
        "notes": " ".join(notes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Copilot message payload for MCP dashboard.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days.")
    parser.add_argument("--max-items", type=int, default=250, help="Max rows after filtering/dedup.")
    parser.add_argument("--out", help="Output file path. Defaults to stdout.")
    parser.add_argument("--graph-token", help="Microsoft Graph bearer token for Message Center.")
    args = parser.parse_args()

    all_rows: List[dict] = []
    skipped_sources: List[str] = []

    try:
        all_rows.extend(fetch_roadmap_rows_json())
    except (urllib.error.URLError, ET.ParseError, TimeoutError, json.JSONDecodeError) as exc:
        skipped_sources.append(f"Roadmap JSON ({exc.__class__.__name__})")
        # keep RSS fallback in case JSON endpoint is unavailable
        try:
            all_rows.extend(parse_rss_items("Roadmap", ROADMAP_RSS))
        except (urllib.error.URLError, ET.ParseError, TimeoutError, json.JSONDecodeError) as fallback_exc:
            skipped_sources.append(f"Roadmap RSS ({fallback_exc.__class__.__name__})")

    for source, url in SOURCES:
        try:
            all_rows.extend(parse_rss_items(source, url))
        except (urllib.error.URLError, ET.ParseError, TimeoutError, json.JSONDecodeError) as exc:
            skipped_sources.append(f"{source} ({exc.__class__.__name__})")

    graph_token = args.graph_token
    if not graph_token:
        # Keep env lookup late to avoid importing os unless needed.
        import os

        graph_token = os.environ.get("GRAPH_ACCESS_TOKEN")
    try:
        all_rows.extend(fetch_message_center_rows(graph_token))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        skipped_sources.append(f"Message Center ({exc.__class__.__name__})")

    rows = trim_and_filter(all_rows, days=max(args.days, 1), max_items=max(args.max_items, 1))
    payload = build_payload(rows, days=max(args.days, 1), skipped_sources=skipped_sources)
    text = json.dumps(payload, indent=2, ensure_ascii=False)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
