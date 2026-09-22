"""Factual, bounded horizon monitoring. Does not invoke legacy idea synthesis."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from opportunity_engine.gap_analysis import analyze_records
from opportunity_engine.http import HttpClient
from opportunity_engine.local_evidence import read_evidence
from opportunity_engine.pipeline import single_run_lock
from opportunity_engine.storage import RawStore, atomic_write

LIMITATIONS = [
    "Stage 1 factual monitoring only. Intent/reality gap analysis has not run.",
    "CPSC covers US recall notices, not the general product market or failure prevalence.",
    "GitHub covers selected public release metadata, not developer experience or tool quality.",
    "Publisher reviews and owner accounts require a separately authorized evidence route.",
    "Keyword and repository coverage is bounded; absence of records is not absence of gaps.",
]


def now() -> datetime:
    return datetime.now(UTC)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


def normalize_cpsc(item: dict) -> dict:
    """Keep attributed fields intact, including remedies and no-injury reports."""
    return {
        "id": f"cpsc:{item['RecallID']}",
        "source": "CPSC",
        "role": "official_notice",
        "title": item["Title"],
        "url": item["URL"],
        "published": item.get("RecallDate"),
        "updated": item.get("LastPublishDate"),
        "products": item.get("Products", []),
        "reported_hazards": item.get("Hazards", []),
        "reported_incidents": item.get("Injuries", []),
        "reported_remedies": item.get("Remedies", []),
        "manufacturers": item.get("Manufacturers", []),
        "importers": item.get("Importers", []),
        "description": item.get("Description", ""),
        "intent": "unknown",
        "gap_assessment": "not_evaluated",
    }


def normalize_release(repo: str, item: dict) -> dict:
    # Deliberately exclude author profiles and release body text.
    return {
        "id": f"github:{repo}:{item['id']}",
        "source": f"GitHub/{repo}",
        "role": "publisher_release_metadata",
        "title": item.get("name") or item["tag_name"],
        "url": item["html_url"],
        "published": item.get("published_at"),
        "version": item["tag_name"],
        "prerelease": bool(item.get("prerelease")),
        "gap_assessment": "not_evaluated",
    }


def normalize_crossref(item: dict) -> dict:
    doi = item["DOI"].lower()
    parts = (item.get("published") or {}).get("date-parts") or [[]]
    date = "-".join(str(x).zfill(2) for x in parts[0]) or None
    return {
        "id": f"doi:{doi}",
        "source": "Crossref",
        "role": "research_metadata",
        "title": " ".join(item.get("title") or [doi]),
        "url": f"https://doi.org/{doi}",
        "published": date,
        "publisher": item.get("publisher"),
        "type": item.get("type"),
        "gap_assessment": "not_evaluated_metadata_only",
    }


def collect(config: dict, *, client: HttpClient | None = None) -> dict:
    root = Path(config["root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with single_run_lock(root / "collection.lock"):
        deadline = time.monotonic() + config.get("collection_max_seconds", 900)
        client = client or HttpClient(
            user_agent="research-opportunity-engine/0.1",
            timeout_seconds=25,
            retries=1,
            deadline=deadline,
        )
        started = now()
        run_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        cutoff = (started - timedelta(days=config.get("lookback_days", 365))).date().isoformat()
        raw = RawStore(root)
        records: dict[str, dict] = {}
        coverage = []
        jobs = [("cpsc", term) for term in config["cpsc_products"]]
        jobs += [("github", repo) for repo in config["github_repositories"]]
        jobs += [("crossref", query) for query in config.get("crossref_queries", [])]
        cursor_path = root / "collection-cursor.json"
        if cursor_path.exists():
            cursor = tuple(read_json(cursor_path).get("next", []))
            if cursor in jobs:
                offset = jobs.index(cursor)
                jobs = jobs[offset:] + jobs[:offset]
        deferred = None
        if config.get("evidence_file"):
            jobs.append(("local", config["evidence_file"]))
        for kind, query in jobs:
            if time.monotonic() >= deadline:
                deferred = deferred or (kind, query)
                coverage.append(
                    {
                        "source": kind,
                        "query": query,
                        "status": "failed",
                        "error": "Collection deadline reached; query deferred",
                    }
                )
                continue
            try:
                if kind == "local":
                    normalized = read_evidence(Path(query))
                    checksum, snapshot = raw.put_json(kind, normalized)
                    truncated = False
                elif kind == "cpsc":
                    payload, body = client.get_json(
                        "https://www.saferproducts.gov/RestWebServices/Recall",
                        params={"format": "json", "RecallDateStart": cutoff, "ProductName": query},
                    )
                    if not isinstance(payload, list):
                        raise ValueError("Expected recall list")
                    normalized = [normalize_cpsc(x) for x in payload]
                    checksum, snapshot = raw.put(kind, body, "json")
                    truncated = False
                elif kind == "crossref":
                    payload, _ = client.get_json(
                        "https://api.crossref.org/works",
                        params={
                            "query.bibliographic": query,
                            "rows": 20,
                            "filter": f"from-pub-date:{cutoff}",
                            "select": "DOI,title,published,publisher,type",
                        },
                    )
                    items = payload["message"]["items"]
                    normalized = [normalize_crossref(x) for x in items]
                    checksum, snapshot = raw.put_json(kind, normalized)
                    truncated = payload["message"].get("total-results", 0) > len(items)
                else:
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", query):
                        raise ValueError("Invalid repository")
                    payload, _ = client.get_json(
                        f"https://api.github.com/repos/{query}/releases",
                        params={"per_page": 10},
                    )
                    if not isinstance(payload, list):
                        raise ValueError("Expected release list")
                    normalized = [normalize_release(query, x) for x in payload if not x["draft"]]
                    # Snapshot only the reviewed metadata subset, not third-party bodies.
                    checksum, snapshot = raw.put_json(kind, normalized)
                    truncated = len(payload) == 10
                for record in normalized:
                    record["snapshot"] = str(snapshot)
                    record["snapshot_sha256"] = checksum
                    records[record["id"]] = record
                coverage.append(
                    {
                        "source": kind,
                        "query": query,
                        "status": "ok",
                        "records": len(normalized),
                        "possibly_truncated": truncated,
                    }
                )
            except Exception as exc:
                coverage.append(
                    {
                        "source": kind,
                        "query": query,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:400],
                    }
                )
        write_json(cursor_path, {"next": list(deferred) if deferred else []})
        db = sqlite3.connect(root / "horizon.sqlite3")
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS observations "
                "(id TEXT PRIMARY KEY, fingerprint TEXT, first_seen TEXT, "
                "last_seen TEXT, data TEXT)"
            )
            for record in records.values():
                canonical = {k: v for k, v in record.items() if not k.startswith("snapshot")}
                encoded = json.dumps(canonical, sort_keys=True)
                fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
                old = db.execute(
                    "SELECT fingerprint,first_seen FROM observations WHERE id=?", (record["id"],)
                ).fetchone()
                record["change"] = (
                    "new" if old is None else ("unchanged" if old[0] == fingerprint else "updated")
                )
                db.execute(
                    "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?)",
                    (
                        record["id"],
                        fingerprint,
                        old[1] if old else started.isoformat(),
                        started.isoformat(),
                        json.dumps({k: v for k, v in record.items() if k != "change"}),
                    ),
                )
            # A record missing from this bounded scan is not deleted or marked resolved.
            failures = sum(x["status"] == "failed" for x in coverage)
            status = "failed" if failures == len(jobs) else "partial" if failures else "complete"
            report = {
                "report_id": f"horizon-{run_id}",
                "domain": config.get("domain", "unspecified"),
                "started_at": started.isoformat(),
                "finished_at": now().isoformat(),
                "status": status,
                "stage": "factual_monitoring",
                "gap_analysis": "not_run",
                "gaps": None,
                "lookback_start": cutoff,
                "coverage": coverage,
                "limitations": list(LIMITATIONS),
                "observations": list(records.values()),
            }
            if config.get("crossref_queries"):
                report["limitations"].append(
                    "Crossref supplies bounded research metadata, not article results or full text."
                )
            # Publish the factual checkpoint before slow/fallible GPU work. A
            # killed service must not roll back its catalog or hide fresh inputs.
            db.commit()
            # The catalog is the durable queue. Cache identities determine which
            # versions are pending, independently of this scan's retrieval window.
            analysis_records = list(records.values())
            for (stored,) in db.execute("SELECT data FROM observations ORDER BY first_seen,id"):
                item = json.loads(stored)
                # Local permission is authoritative on each import. Missing or
                # revoked local material must not revive from historical state.
                if item.get("source") == "CPSC" and item["id"] not in records:
                    item["retrieval_status"] = "historical_not_refreshed_this_cycle"
                    analysis_records.append(item)
            historical = len(analysis_records) - len(records)
            report["historical_analysis_records"] = historical
            if historical:
                report["limitations"].append(
                    f"Analysis includes {historical} stored notices not refreshed this cycle; "
                    "their current resolution status is unknown."
                )
            path = root / "reports" / (report["report_id"] + ".json")
            if config.get("gap_analysis", {}).get("enabled") and analysis_records:
                if report["status"] == "complete":
                    report["status"] = "partial"
                report["gap_analysis"] = "pending"
                write_json(path, report)
                atomic_write(path.with_suffix(".md"), render(report).encode())
                write_json(root / "latest.json", {"path": str(path)})
                try:
                    analysis = analyze_records(analysis_records, config["gap_analysis"], root)
                    report["analysis"] = analysis
                    report["gap_analysis"] = analysis["status"]
                    report["gaps"] = analysis["gaps"]
                    report["stage"] = "observations_and_tentative_gaps"
                    report["limitations"][0] = analysis["limits"]
                    report["status"] = status
                    if analysis["status"] != "complete" and report["status"] == "complete":
                        report["status"] = "partial"
                except Exception as exc:
                    report["gap_analysis"] = "failed"
                    report["analysis_error"] = type(exc).__name__
                    report["status"] = "partial" if records else "failed"
            report["finished_at"] = now().isoformat()
            write_json(path, report)
            atomic_write(path.with_suffix(".md"), render(report).encode())
            db.commit()
            write_json(root / "latest.json", {"path": str(path)})
            return report
        finally:
            db.close()


def render(report: dict) -> str:
    lines = [
        "# Product horizon — factual monitoring",
        "",
        f"Run: {report['report_id']}",
        f"Collection status: {report['status']}",
        "",
        "## Coverage limits",
        "",
    ]
    lines += [f"- {x}" for x in report["limitations"]]
    lines += ["", "## Source checks", ""]
    lines += [
        f"- {x['source']}/{x['query']}: {x['status']}; "
        f"records={x.get('records', 'unknown')}; "
        f"possibly truncated={x.get('possibly_truncated', 'unknown')}"
        for x in report["coverage"]
    ]
    if report.get("analysis"):
        analysis = report["analysis"]
        lines += [
            "",
            "## Tentative gap interpretations",
            "",
            f"Analyzed {analysis['processed']} of {analysis['eligible']} eligible notices; "
            f"{analysis['pending']} pending. {analysis['skipped_metadata_only']} metadata-only "
            "records excluded from interpretation.",
            "These are single-origin model interpretations, not validated market gaps.",
            "",
        ]
        if not report["gaps"]:
            lines += [
                "No candidate passed the completed review passes. "
                "This does not describe the unprocessed backlog.",
                "",
            ]
        for i, gap in enumerate(report["gaps"], 1):
            # Render source/model strings as escaped text, never active markup.
            def safe(value):
                return re.sub(r"([\\`*{}\[\]()<>#|])", r"\\\1", str(value))

            lines += [
                f"### {i}. {safe(gap['title'])}",
                "",
                f"Source: {gap['source_url']} ({gap['source_date'] or 'date unknown'})",
                "",
            ]
            for key in [
                "context",
                "intent",
                "attempt",
                "result",
                "consequence",
                "blocker",
                "alternatives",
                "counterevidence",
                "workaround",
            ]:
                claim = gap[key]
                lines += [
                    f"**{key.title()}:** {safe(claim['text'])} "
                    f"[{safe(', '.join(claim['evidence_ids']))}]",
                    "",
                ]
            lines += ["| Aspect | Value | Rationale / evidence |", "|---|---|---|"]
            for key, aspect in gap["aspects"].items():
                reason = aspect["rationale"]
                lines += [
                    f"| {key} | {aspect['value']} | {safe(reason['text'])} "
                    f"[{safe(', '.join(reason['evidence_ids']))}] |"
                ]
            lines += ["", "**Competing explanations (unverified questions):**", ""]
            lines += [f"- {safe(x)}" for x in gap["competing_explanations"]]
            lines += ["", "**Open questions:**", ""]
            lines += [f"- {safe(x)}" for x in gap["open_questions"]]
            lines += ["", f"Model review: {safe(gap['review_reason'])}", ""]
        lines += [
            "",
            "## Quote-level analysis audit",
            "",
            "````json",
            json.dumps(analysis, ensure_ascii=False, indent=2).replace("`", "\\u0060"),
            "````",
            "",
        ]
    lines += ["", "## Attributable observations", ""]
    for item in report["observations"]:
        # Code fence preserves source strings as data rather than executable markup.
        lines += [
            f"### {item['id']}",
            "",
            "````json",
            json.dumps(item, ensure_ascii=False, indent=2).replace("`", "\\u0060"),
            "````",
            "",
        ]
    return "\n".join(lines)


def digest(report: dict) -> dict:
    observations = report["observations"]
    counts = {
        kind: sum(x["change"] == kind for x in observations)
        for kind in ("new", "updated", "unchanged")
    }
    failures = sum(x["status"] == "failed" for x in report["coverage"])
    text = (
        f"FACTUAL MONITORING — {report['finished_at'][:10]}\n"
        f"{len(observations)} records: {counts['new']} newly observed, "
        f"{counts['updated']} updated, {counts['unchanged']} unchanged.\n"
        f"Source checks: {len(report['coverage']) - failures} successful; {failures} failed.\n"
    )
    if report.get("analysis"):
        analysis = report["analysis"]
        text += (
            f"Local gap review: {analysis['processed']}/{analysis['eligible']} eligible notices; "
            f"{analysis['pending']} pending. {len(report['gaps'])} tentative candidates.\n"
            "Single-origin model interpretations; not human-validated market gaps.\n\n"
        )
        for gap in report["gaps"][:5]:
            piece = f"Tentative ({gap.get('change', 'reviewed')}): "
            piece += f"{gap['title'][:120]}\n{gap['source_url'][:250]}\n"
            if len((text + piece).encode("utf-16-le")) // 2 > 2400:
                break
            text += piece
        if len(report["gaps"]) > 5:
            text += f"All {len(report['gaps'])} candidates retained in the full local report.\n"
    else:
        text += "Gap analysis has NOT run successfully; this is not a finding of zero gaps.\n\n"
    changed = [x for x in observations if x["change"] != "unchanged"]
    shown_changes = 0
    for item in sorted(changed, key=lambda x: x.get("published") or "", reverse=True)[:5]:
        piece = f"• {item['source']}: {item['title'][:160]}\n{item['url'][:300]}\n"
        if len((text + piece).encode("utf-16-le")) // 2 > 2400:
            break
        text += piece
        shown_changes += 1
    if len(changed) > shown_changes:
        text += (
            f"{len(changed) - shown_changes} additional changes "
            "retained in the full local report.\n"
        )
    text += (
        "\nAll records/candidates retained locally; above is a bounded preview.\n"
        "Coverage: selected official notices, metadata and authorized local evidence. "
        "Reviews and owner experiences are not connected unless explicitly imported. "
        "Full evidence and limitations stay local."
    )
    return {
        "schema_version": 1,
        "report_id": report["report_id"],
        "status": report["status"],
        "digest": text,
    }


def dispatch(config: dict, credential: dict) -> dict:
    root = Path(config["root"])
    with single_run_lock(root / "delivery.lock"):
        pointer = root / "latest.json"
        report = read_json(Path(read_json(pointer)["path"])) if pointer.exists() else None
        if report is None or now() - datetime.fromisoformat(report["finished_at"]) > timedelta(
            hours=24
        ):
            day = now().astimezone(ZoneInfo("America/New_York")).date().isoformat()
            payload = {
                "schema_version": 1,
                "report_id": f"missing-research-{day}",
                "status": "failed",
                "digest": "No fresh research report is available. "
                "The scheduled collection may have failed or the PC was offline. "
                "No conclusion about market gaps can be drawn.",
            }
        else:
            payload = digest(report)
        receipt = root / "deliveries" / (payload["report_id"] + ".json")
        if receipt.exists():
            previous = read_json(receipt)
            if previous["status"] == "telegram_accepted":
                return {"status": "already_delivered", "report_id": payload["report_id"]}
            raise RuntimeError("Prior delivery is uncertain; manual reconciliation required")
        # Persist before sending. Never retry an ambiguous Telegram send automatically.
        write_json(receipt, {"report_id": payload["report_id"], "status": "uncertain"})
        request = urllib.request.Request(
            credential["url"],
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                credential["header"]: credential["secret"],
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
        if (
            result.get("report_id") != payload["report_id"]
            or result.get("status") != "telegram_accepted"
            or type(result.get("telegram_message_id")) is not int
        ):
            raise ValueError("Invalid Telegram receipt; delivery remains uncertain")
        write_json(receipt, result)
        return result


def serve(config: dict, credential: dict) -> None:
    busy = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass  # Never log authentication headers or request bodies.

        def respond(self, code: int, value: dict) -> None:
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self.respond(200 if self.path == "/health" else 404, {"service": "horizon"})

        def do_POST(self) -> None:
            supplied = self.headers.get(credential["header"], "")
            if not hmac.compare_digest(supplied, credential["secret"]):
                self.respond(403, {"error": "Unauthorized"})
                return
            if self.path not in {"/collect", "/deliver"}:
                self.respond(404, {"error": "Unknown operation"})
                return
            if not busy.acquire(blocking=False):
                self.respond(409, {"error": "Operation already running"})
                return
            try:
                if self.path == "/collect":
                    result = collect(config)
                    analysis = result.get("analysis", {})
                    result = {
                        "report_id": result["report_id"],
                        "status": result["status"],
                        "observations": len(result["observations"]),
                        "gap_analysis": result["gap_analysis"],
                        "analyzed": analysis.get("processed", 0),
                        "pending": analysis.get("pending"),
                        "tentative_gaps": len(result.get("gaps") or []),
                    }
                else:
                    result = dispatch(config, credential)
                self.respond(200, result)
            except Exception as exc:
                self.respond(
                    500,
                    {
                        "error": type(exc).__name__,
                        "detail": "Operation failed; inspect local state before retrying.",
                    },
                )
            finally:
                busy.release()

    ThreadingHTTPServer(("127.0.0.1", config.get("port", 5681)), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["collect", "deliver", "serve"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--credential", type=Path, required=False)
    args = parser.parse_args()
    config = read_json(args.config)
    if args.action == "collect":
        report = collect(config)
        print(
            json.dumps(
                {
                    "report_id": report["report_id"],
                    "status": report["status"],
                    "records": len(report["observations"]),
                }
            )
        )
    elif args.action == "deliver":
        print(
            json.dumps(
                dispatch(
                    config, read_json(args.credential or args.config.parent / "delivery.local.json")
                )
            )
        )
    else:
        serve(config, read_json(args.credential or args.config.parent / "delivery.local.json"))


if __name__ == "__main__":
    main()
