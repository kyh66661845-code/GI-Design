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

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "OfficialNewsDashboard/1.0"
})


def utc_now():
    return datetime.now(UTC)


def iso_date(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)

        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def safe_url(value):
    if not isinstance(value, str):
        return ""

    parsed = urlparse(value)

    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value

    return ""


def article_id(title):
    normalized = re.sub(r"\s+", " ", title).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def build_feed_url(source):
    if source.get("language") == "ko":
        locale = {
            "hl": "ko",
            "gl": "KR",
            "ceid": "KR:ko"
        }
    else:
        locale = {
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en"
        }

    params = {
        "q": source["query"],
        **locale
    }

    return "https://news.google.com/rss/search?" + urlencode(params)


def load_previous():
    if not OUTPUT_PATH.exists():
        return {"articles": []}

    try:
        with OUTPUT_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass

    return {"articles": []}


def fetch_feed(url):
    last_error = None

    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=30)
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            # HTML 오류 페이지 등을 정상적인 RSS로 취급하지 않습니다.
            if not feed.get("version"):
                raise ValueError("RSS 형식의 응답이 아닙니다.")

            return feed

        except (requests.RequestException, ValueError) as error:
            last_error = error

            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    raise RuntimeError(str(last_error))


def get_published(entry):
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


def convert_entry(entry, source, collected_at):
    title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
    link = safe_url(entry.get("link", ""))

    if not title or not link:
        return None

    publisher_data = entry.get("source") or {}
    publisher = publisher_data.get("title") or source["name"]

    published = get_published(entry)

    return {
        "id": article_id(title),
        "title": title,
        "url": link,
        "publisher": publisher,
        "published_at": iso_date(published) if published else None,
        "first_seen_at": iso_date(collected_at),
        "last_seen_at": iso_date(collected_at),
        "categories": [source["category"]],
        "source_ids": [source["id"]],
        "source_names": [source["name"]],
        "kinds": [source["kind"]]
    }


def merge_article(old, new):
    merged = dict(old)

    # 새로운 응답의 제목·링크·출처를 반영합니다.
    for key in ("title", "url", "publisher", "last_seen_at"):
        merged[key] = new[key]

    if new.get("published_at"):
        merged["published_at"] = new["published_at"]

    merged["first_seen_at"] = (
        old.get("first_seen_at")
        or new["first_seen_at"]
    )

    for key in ("categories", "source_ids", "source_names", "kinds"):
        merged[key] = sorted(
            set(old.get(key, [])) | set(new.get(key, []))
        )

    return merged


def main():
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    sources = [
        source
        for source in config["sources"]
        if source.get("enabled", True)
    ]

    if not sources:
        raise RuntimeError("활성화된 수집 대상이 없습니다.")

    now = utc_now()
    retention_days = int(config.get("retention_days", 30))
    cutoff = now - timedelta(days=retention_days)

    max_articles = int(config.get("max_articles", 300))
    max_items = int(config.get("max_items_per_source", 25))

    previous = load_previous()

    articles_by_id = {
        article["id"]: article
        for article in previous.get("articles", [])
        if isinstance(article, dict) and article.get("id")
    }

    source_results = []
    successful_sources = 0

    for source in sources:
        print(f"수집 시작: {source['name']}", flush=True)

        try:
            feed = fetch_feed(build_feed_url(source))
            successful_sources += 1
            accepted_count = 0

            for entry in feed.entries[:max_items]:
                article = convert_entry(entry, source, now)

                if article is None:
                    continue

                published = parse_iso(article.get("published_at"))

                if published and published < cutoff:
                    continue

                key = article["id"]

                if key in articles_by_id:
                    articles_by_id[key] = merge_article(
                        articles_by_id[key],
                        article
                    )
                else:
                    articles_by_id[key] = article

                accepted_count += 1

            source_results.append({
                "id": source["id"],
                "name": source["name"],
                "category": source["category"],
                "status": "ok",
                "count": accepted_count
            })

            print(f"  처리 완료: {accepted_count}건", flush=True)

        except Exception as error:
            source_results.append({
                "id": source["id"],
                "name": source["name"],
                "category": source["category"],
                "status": "error",
                "count": 0,
                "error": str(error)[:300]
            })

            print(f"  수집 실패: {error}", flush=True)

        # 요청을 한꺼번에 보내지 않습니다.
        time.sleep(1)

    # 모든 요청이 실패하면 기존 파일을 덮어쓰지 않습니다.
    if successful_sources == 0:
        print("모든 수집 대상 요청 실패. 기존 news.json을 유지합니다.")
        return 1

    retained_articles = []

    for article in articles_by_id.values():
        article_date = (
            parse_iso(article.get("published_at"))
            or parse_iso(article.get("first_seen_at"))
        )

        if article_date and article_date >= cutoff:
            retained_articles.append(article)

    retained_articles.sort(
        key=lambda article: (
            parse_iso(article.get("published_at"))
            or parse_iso(article.get("first_seen_at"))
            or datetime.min.replace(tzinfo=UTC)
        ),
        reverse=True
    )

    retained_articles = retained_articles[:max_articles]

    output = {
        "updated_at": iso_date(now),
        "collection_method": "Google News RSS / domain-restricted search",
        "retention_days": retention_days,
        "source_results": source_results,
        "articles": retained_articles
    }

    temporary_path = OUTPUT_PATH.with_suffix(".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temporary_path.replace(OUTPUT_PATH)

    failed_sources = len(sources) - successful_sources

    print(
        f"저장 완료: {len(retained_articles)}개 기사, "
        f"정상 출처 {successful_sources}개, "
        f"실패 출처 {failed_sources}개",
        flush=True
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
