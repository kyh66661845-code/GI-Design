import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import feedparser
import requests


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "sources.json"
OUTPUT_PATH = ROOT / "news.json"
UTC = timezone.utc

# 일반적인 AI, 투자, 공장 등의 단어는 포함하지 않습니다.
SEMICONDUCTOR_PATTERN = re.compile(
    r"반도체|파운드리|팹리스|"
    r"\bsemiconductors?\b|"
    r"\bmicroelectronics\b|"
    r"\bfoundry\b|"
    r"\bfabless\b|"
    r"\bHBM\d*[A-Z]*\b|"
    r"\bDRAM\b|"
    r"\bNAND\b|"
    r"\bCHIPS\s+Act\b|"
    r"\bchip\s+(manufacturing|fabrication|packaging)\b|"
    r"\bsilicon\s+carbide\b",
    re.IGNORECASE
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "SemiconductorNewsDashboard/1.0"
})


def iso_date(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_date(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        return None


def is_semiconductor_title(title):
    return bool(SEMICONDUCTOR_PATTERN.search(title or ""))


def safe_url(value):
    if not isinstance(value, str):
        return ""

    parsed = urlparse(value)

    if parsed.scheme in ("https", "http") and parsed.netloc:
        return value

    return ""


def make_id(title):
    normalized = re.sub(r"\s+", " ", title).strip().casefold()
    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()[:24]


def feed_url(source):
    locale = (
        {"hl": "ko", "gl": "KR", "ceid": "KR:ko"}
        if source.get("language") == "ko"
        else {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    )

    return (
        "https://news.google.com/rss/search?"
        + urlencode({"q": source["query"], **locale})
    )


def fetch_feed(url):
    last_error = None

    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=30)
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            if not feed.get("version"):
                raise ValueError("RSS 형식이 아닙니다.")

            return feed

        except (requests.RequestException, ValueError) as error:
            last_error = error
            time.sleep(attempt + 1)

    raise RuntimeError(str(last_error))


def published_date(entry):
    parsed = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if not parsed:
        return None

    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def load_previous():
    try:
        data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    sources = [
        source for source in config["sources"]
        if source.get("enabled", True)
    ]

    if not sources:
        raise RuntimeError("활성화된 출처가 없습니다.")

    now = datetime.now(UTC)
    cutoff = now - timedelta(
        days=int(config.get("retention_days", 30))
    )

    previous = load_previous()

    # 이전에 모은 일반 산업 기사도 여기서 제외합니다.
    articles = {
        article["id"]: article
        for article in previous.get("articles", [])
        if (
            isinstance(article, dict)
            and article.get("id")
            and is_semiconductor_title(article.get("title", ""))
        )
    }

    results = []
    success_count = 0

    for source in sources:
        print(f"수집: {source['name']}", flush=True)

        try:
            feed = fetch_feed(feed_url(source))
            success_count += 1
            accepted = 0

            limit = int(config.get("max_items_per_source", 30))

            for entry in feed.entries[:limit]:
                title = re.sub(
                    r"\s+", " ", entry.get("title", "")
                ).strip()

                link = safe_url(entry.get("link", ""))

                # 검색 결과를 제목 키워드로 다시 검사합니다.
                if not title or not link:
                    continue

                if not is_semiconductor_title(title):
                    continue

                published = published_date(entry)

                if published and published < cutoff:
                    continue

                key = make_id(title)
                old = articles.get(key, {})

                publisher_data = entry.get("source") or {}

                # 기존 번역·요약 필드는 유지합니다.
                article = dict(old)

                article.update({
                    "id": key,
                    "title": title,
                    "url": link,
                    "publisher": (
                        publisher_data.get("title")
                        or source["name"]
                    ),
                    "published_at": (
                        iso_date(published)
                        if published
                        else old.get("published_at")
                    ),
                    "first_seen_at": (
                        old.get("first_seen_at")
                        or iso_date(now)
                    ),
                    "last_seen_at": iso_date(now)
                })

                additions = {
                    "categories": [source["category"]],
                    "source_ids": [source["id"]],
                    "source_names": [source["name"]],
                    "kinds": [source["kind"]],
                    "allowed_domains": source.get("domains", [])
                }

                for field, values in additions.items():
                    article[field] = sorted(
                        set(old.get(field, [])) | set(values)
                    )

                articles[key] = article
                accepted += 1

            results.append({
                "id": source["id"],
                "name": source["name"],
                "status": "ok",
                "count": accepted
            })

            print(f"  반도체 기사 {accepted}건", flush=True)

        except Exception as error:
            results.append({
                "id": source["id"],
                "name": source["name"],
                "status": "error",
                "count": 0,
                "error": str(error)[:200]
            })

            print(f"  요청 실패: {error}", flush=True)

        time.sleep(1)

    if success_count == 0:
        print("모든 요청 실패. 기존 파일을 유지합니다.")
        return 1

    retained = []

    for article in articles.values():
        date = (
            parse_date(article.get("published_at"))
            or parse_date(article.get("first_seen_at"))
        )

        if date and date >= cutoff:
            retained.append(article)

    retained.sort(
        key=lambda article: (
            parse_date(article.get("published_at"))
            or parse_date(article.get("first_seen_at"))
            or datetime.min.replace(tzinfo=UTC)
        ),
        reverse=True
    )

    retained = retained[:int(config.get("max_articles", 200))]

    output = {
        "updated_at": iso_date(now),
        "topic": "semiconductor",
        "source_results": results,
        "articles": retained
    }

    temporary = OUTPUT_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )
    temporary.replace(OUTPUT_PATH)

    print(f"저장 완료: {len(retained)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
