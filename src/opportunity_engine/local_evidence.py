"""Explicitly authorized local input; no website scraping or arbitrary URL fetch."""

import json
from pathlib import Path
from urllib.parse import urlsplit


def read_evidence(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    seen = set()
    with path.open(encoding="utf-8") as stream:
        while True:
            line = stream.readline(50001)
            if not line:
                break
            if len(line) > 50000 or len(records) >= 1000:
                raise ValueError("Local evidence input exceeds bounded import size")
            if not line.strip():
                continue
            item = json.loads(line)
            rights = item.get("rights", {})
            if (
                rights.get("local_analysis_allowed") is not True
                or rights.get("license")
                not in {
                    "user-authored",
                    "CC0-1.0",
                    "CC-BY-4.0",
                }
                or not rights.get("permission_reference")
            ):
                raise ValueError("Local analysis rights and permission reference required")
            identity = item.get("id")
            if not isinstance(identity, str) or not identity or len(identity) > 100:
                raise ValueError("Stable evidence ID required")
            if identity in seen:
                raise ValueError("Duplicate evidence ID")
            seen.add(identity)
            url = urlsplit(item.get("url", ""))
            if url.scheme != "https" or not url.netloc or url.username or url.password:
                raise ValueError("HTTPS attribution URL required; never embed credentials")
            evidence = item.get("evidence")
            if (
                not isinstance(evidence, dict)
                or not evidence
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in evidence.items())
            ):
                raise ValueError("Evidence must map field names to verbatim text")
            if not isinstance(item.get("title"), str) or not item["title"].strip():
                raise ValueError("Title required")
            records.append(
                {
                    "id": "local:" + identity,
                    "source": "AuthorizedLocalEvidence",
                    "role": "authorized_observation",
                    "title": item["title"],
                    "url": item["url"],
                    "published": item.get("published"),
                    "evidence": evidence,
                    "rights": rights,
                    "gap_assessment": "not_evaluated",
                }
            )
    return records
