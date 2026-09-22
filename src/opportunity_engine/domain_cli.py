"""Generate a private domain configuration and importable, inactive n8n workflows."""

import argparse
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

from opportunity_engine.storage import atomic_write


def save(path, value):
    atomic_write(path, (json.dumps(value, indent=2) + "\n").encode())


def workflows(domain, slug, port, hour, timezone):
    def scheduled(name, operation, at):
        return {
            "name": f"{domain} — {name}",
            "active": False,
            "settings": {"timezone": timezone, "executionOrder": "v1"},
            "nodes": [
                {
                    "id": "schedule",
                    "name": "Schedule",
                    "type": "n8n-nodes-base.scheduleTrigger",
                    "typeVersion": 1.2,
                    "position": [0, 0],
                    "parameters": {
                        "rule": {
                            "interval": [
                                {
                                    "field": "days",
                                    "daysInterval": 1,
                                    "triggerAtHour": at,
                                    "triggerAtMinute": 0,
                                }
                            ]
                        }
                    },
                },
                {
                    "id": "request",
                    "name": "Run",
                    "type": "n8n-nodes-base.httpRequest",
                    "typeVersion": 4.2,
                    "position": [260, 0],
                    "parameters": {
                        "method": "POST",
                        "url": f"http://127.0.0.1:{port}/{operation}",
                        "authentication": "genericCredentialType",
                        "genericAuthType": "httpHeaderAuth",
                        "options": {"timeout": 3600000 if operation == "collect" else 90000},
                    },
                },
            ],
            "connections": {"Schedule": {"main": [[{"node": "Run", "type": "main", "index": 0}]]}},
        }

    validator = """const b=$input.first().json.body;
if(!b || b.schema_version!==1 || typeof b.report_id!=='string' ||
 !/^[A-Za-z0-9_-]{1,100}$/.test(b.report_id) ||
 !['complete','partial','failed'].includes(b.status) ||
 typeof b.digest!=='string' || !b.digest.trim() || b.digest.length>3200)
 throw Error('Invalid digest');
const text=`Research | ${b.status}\\n${b.report_id}\\n\\n${b.digest}`;
return [{json:{report_id:b.report_id,text:text.replace(/&/g,'&amp;')
 .replace(/</g,'&lt;').replace(/>/g,'&gt;')}}];"""
    receipt = """const r=$input.first().json; const v=r.result??r;
if(r.ok===false || !Number.isInteger(v.message_id)) throw Error('Missing Telegram receipt');
return [{json:{report_id:$('Validate').first().json.report_id,
 status:'telegram_accepted',telegram_message_id:v.message_id}}];"""
    specs = [
        (
            "Webhook",
            "webhook",
            2,
            {
                "httpMethod": "POST",
                "path": f"{slug}-report",
                "authentication": "headerAuth",
                "responseMode": "responseNode",
                "options": {},
            },
        ),
        ("Validate", "code", 2, {"jsCode": validator}),
        (
            "Telegram",
            "telegram",
            1.2,
            {
                "chatId": "REPLACE_WITH_YOUR_CHAT_ID",
                "text": "={{ $json.text }}",
                "additionalFields": {
                    "parse_mode": "HTML",
                    "appendAttribution": False,
                    "disable_web_page_preview": True,
                },
            },
        ),
        ("Receipt", "code", 2, {"jsCode": receipt}),
        (
            "Respond",
            "respondToWebhook",
            1.4,
            {
                "respondWith": "json",
                "responseBody": "={{ $json }}",
                "options": {"responseCode": 200},
            },
        ),
    ]
    nodes = [
        {
            "id": n,
            "name": n,
            "type": "n8n-nodes-base." + t,
            "typeVersion": v,
            "position": [i * 240, 0],
            "parameters": p,
            "retryOnFail": False,
        }
        for i, (n, t, v, p) in enumerate(specs)
    ]
    connections = {
        nodes[i]["name"]: {"main": [[{"node": nodes[i + 1]["name"], "type": "main", "index": 0}]]}
        for i in range(len(nodes) - 1)
    }
    delivery = {
        "name": f"{domain} — Telegram",
        "active": False,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }
    return [scheduled("collection", "collect", hour), scheduled("delivery", "deliver", 7), delivery]


def initialize(
    directory,
    domain,
    model="",
    repositories=(),
    products=(),
    hour=23,
    timezone="America/New_York",
    port=5681,
):
    from zoneinfo import ZoneInfo

    ZoneInfo(timezone)
    if not domain.strip() or len(domain) > 200:
        raise ValueError("Provide a domain of 1–200 characters")
    if not 0 <= hour <= 23 or not 1024 <= port <= 65535:
        raise ValueError("Invalid hour or port")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", x) for x in repositories):
        raise ValueError("Repositories must be owner/name")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    slug = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")[:40] or "research"
    slug += "-" + hashlib.sha256(str(directory).encode()).hexdigest()[:8]
    save(
        directory / "config.local.json",
        {
            "domain": domain,
            "root": str(directory / "state"),
            "port": port,
            "lookback_days": 365,
            "collection_max_seconds": 900,
            "cpsc_products": list(products),
            "github_repositories": list(repositories),
            "crossref_queries": [
                domain,
                domain + " usability evaluation",
                domain + " reliability interoperability",
            ],
            "evidence_file": str(directory / "evidence.local.jsonl"),
            "gap_analysis": {
                "enabled": bool(model),
                "model": model,
                "domain": domain,
                "max_input_bytes": 12000,
                "max_records_per_cycle": 20,
                "max_seconds": 1200,
            },
        },
    )
    save(
        directory / "delivery.local.json",
        {
            "url": f"http://127.0.0.1:5678/webhook/{slug}-report",
            "header": "X-Research-Token",
            "secret": secrets.token_urlsafe(32),
        },
    )
    atomic_write(directory / "evidence.local.jsonl", b"")
    save(directory / "n8n-workflows.local.json", workflows(domain, slug, port, hour, timezone))
    return directory


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"collect", "serve", "deliver"}:
        from opportunity_engine.horizon import main as run

        run()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init"])
    parser.add_argument("--domain")
    parser.add_argument("--output", default="local/my-domain")
    parser.add_argument("--model", default="")
    parser.add_argument("--repository", action="append", default=[])
    parser.add_argument("--product", action="append", default=[])
    parser.add_argument("--hour", type=int, default=23)
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--port", type=int, default=5681)
    args = parser.parse_args()
    domain = args.domain or input("Hardware or software domain to research: ").strip()
    directory = initialize(
        args.output,
        domain,
        args.model,
        args.repository,
        args.product,
        args.hour,
        args.timezone,
        args.port,
    )
    print(
        f"Created private domain setup in {directory}. Read README setup steps before publishing."
    )
