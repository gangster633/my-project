#!/usr/bin/env python3
"""네이버 키워드 수집기 (MVP)

1. collector/seeds.txt 의 씨앗 키워드로 네이버 검색광고 API(키워드도구)를 불러
   연관 키워드와 월간 검색수(PC/모바일)를 모은다.
2. 키워드마다 네이버 검색 API(블로그)로 블로그 문서 수를 센다.
3. 황금지수를 계산해 src/static/data/keywords.json 에 저장한다 (build.py 가 docs/ 로 복사).
4. 날짜별 검색량을 data/history.json 에 쌓아 전일·7일 전 대비 변화율을 구한다.

필요한 환경변수 (GitHub 저장소 Settings > Secrets and variables > Actions 에 등록):
  NAVER_AD_API_KEY, NAVER_AD_SECRET, NAVER_AD_CUSTOMER_ID   검색광고 API
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET                      네이버 개발자센터 검색 API (선택)
    두 값이 없으면 블로그 문서 수·황금지수는 비워 두고 검색량만 모은다.

표준 라이브러리만 사용한다.
"""
import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ROOT / "collector" / "seeds.txt"
CATEGORIES = ROOT / "collector" / "categories.txt"
OUT = ROOT / "src" / "static" / "data" / "keywords.json"
HISTORY = ROOT / "data" / "history.json"

MAX_KEYWORDS = int(os.environ.get("MAX_KEYWORDS", "3000"))  # 문서 수를 셀 최대 키워드 수 (검색 API 하루 25,000회 한도)
MIN_MONTHLY = int(os.environ.get("MIN_MONTHLY", "100"))     # 월간 검색수가 이보다 적으면 버림
HISTORY_DAYS = 35
KST = datetime.timezone(datetime.timedelta(hours=9))


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"환경변수 {name} 가 비어 있습니다.")
    return value


def http_json(url, headers, retries=3):
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"HTTP {e.code} {url}: {body}") from None
        except urllib.error.URLError:
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"요청 실패: {url}")


# ---------- 검색광고 API: 연관 키워드 + 월간 검색수 ----------

def searchad_keywords(hints):
    api_key, secret, customer = env("NAVER_AD_API_KEY"), env("NAVER_AD_SECRET"), env("NAVER_AD_CUSTOMER_ID")
    uri = "/keywordstool"
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(hmac.new(secret.encode(), f"{ts}.GET.{uri}".encode(), hashlib.sha256).digest()).decode()
    query = urllib.parse.urlencode({"hintKeywords": ",".join(hints), "showDetail": "1"})
    headers = {"X-Timestamp": ts, "X-API-KEY": api_key, "X-Customer": customer, "X-Signature": sig}
    return http_json(f"https://api.searchad.naver.com{uri}?{query}", headers).get("keywordList", [])


def to_count(v):
    # 검색수가 적으면 "< 10" 같은 문자열로 온다.
    if isinstance(v, (int, float)):
        return int(v)
    return 5


def load_seeds():
    seeds, category = [], "기타"
    for line in SEEDS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("##"):
            category = line.lstrip("#").strip()
        elif line and not line.startswith("#"):
            seeds.append((category, line.replace(" ", "")))
    return seeds


def load_category_words():
    words, category = {}, None
    for line in CATEGORIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("##"):
            category = line.lstrip("#").strip()
        elif line and not line.startswith("#") and category:
            for w in line.split(","):
                w = w.strip().replace(" ", "").upper()
                if w:
                    words[w] = category
    return words


def categorize(keyword, fallback, words):
    """키워드 안의 사전 단어 중 가장 뒤에서 끝나는 것(같으면 더 긴 것)의 분류. 없으면 fallback."""
    kw = keyword.replace(" ", "").upper()
    best = None  # (끝 위치, 길이, 분류)
    for w, cat in words.items():
        i = kw.rfind(w)
        if i != -1:
            cand = (i + len(w), len(w), cat)
            if best is None or cand[:2] > best[:2]:
                best = cand
    return best[2] if best else fallback


def collect_volumes(seeds):
    found = {}
    groups = {}
    for cat, kw in seeds:
        groups.setdefault(cat, []).append(kw)
    for cat, kws in groups.items():
        for i in range(0, len(kws), 5):  # 한 번에 씨앗 5개까지
            batch = kws[i:i + 5]
            rows = searchad_keywords(batch)
            print(f"[검색광고] {cat} {batch} -> {len(rows)}개")
            for r in rows:
                kw = r["relKeyword"]
                pc, mo = to_count(r.get("monthlyPcQcCnt")), to_count(r.get("monthlyMobileQcCnt"))
                if pc + mo < MIN_MONTHLY or kw in found:
                    continue
                found[kw] = {"k": kw, "cat": cat, "pc": pc, "mo": mo, "comp": r.get("compIdx", "")}
            time.sleep(0.5)
    return found


# ---------- 검색 API: 블로그 문서 수 ----------

def blog_total(keyword):
    headers = {"X-Naver-Client-Id": env("NAVER_CLIENT_ID"), "X-Naver-Client-Secret": env("NAVER_CLIENT_SECRET")}
    q = urllib.parse.urlencode({"query": keyword, "display": 1})
    return int(http_json(f"https://openapi.naver.com/v1/search/blog.json?{q}", headers).get("total", 0))


# ---------- 지표 계산 ----------

def golden_index(monthly, docs):
    """검색량 대비 문서가 적을수록, 검색량이 충분할수록 높다 (0~100)."""
    ratio = docs / max(monthly, 1)
    scarcity = 100 / (1 + ratio)                            # 문서/검색 비율이 0이면 100
    volume = min(1.0, math.log10(monthly + 1) / 4)          # 월 1만 회 이상이면 1
    return round(scarcity * volume, 1)


def change(series, days_back):
    if len(series) <= days_back:
        return None
    old, new = series[-1 - days_back][1], series[-1][1]
    return round((new - old) / old * 100, 1) if old else None


def main():
    today = datetime.datetime.now(KST).date().isoformat()
    found = collect_volumes(load_seeds())
    words = load_category_words()
    for it in found.values():
        it["cat"] = categorize(it["k"], it["cat"], words)
    items = sorted(found.values(), key=lambda x: x["pc"] + x["mo"], reverse=True)[:MAX_KEYWORDS]
    print(f"연관 키워드 {len(found)}개 중 {len(items)}개 문서 수 조회")

    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else {}

    has_search = bool(os.environ.get("NAVER_CLIENT_ID", "").strip() and os.environ.get("NAVER_CLIENT_SECRET", "").strip())
    if not has_search:
        print("검색 API 키가 없어 블로그 문서 수는 건너뜁니다.")
    for n, it in enumerate(items if has_search else [], 1):
        try:
            it["docs"] = blog_total(it["k"])
        except RuntimeError as e:
            print(f"  문서 수 실패 {it['k']}: {e}")
            it["docs"] = None
        if n % 200 == 0:
            print(f"  {n}/{len(items)}")
        time.sleep(0.12)  # 초당 10회 제한

    rows = []
    for it in items:
        monthly = it["pc"] + it["mo"]
        series = [p for p in history.get(it["k"], []) if p[0] != today] + [[today, monthly]]
        history[it["k"]] = series[-HISTORY_DAYS:]
        docs = it.get("docs")
        rows.append([
            it["k"], it["cat"], it["pc"], it["mo"],
            docs,
            round(docs / max(monthly, 1), 3) if docs is not None else None,
            golden_index(monthly, docs) if docs is not None else None,
            change(series, 1), change(series, 7),
            it["comp"],
        ])

    # 오래 안 나온 키워드는 기록에서 정리
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    history = {k: v for k, v in history.items() if v and v[-1][0] >= cutoff}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "fields": ["keyword", "category", "pc", "mobile", "docs", "ratio", "golden", "chg1", "chg7", "comp"],
        "rows": rows,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"저장 완료: {len(rows)}개 -> {OUT.relative_to(ROOT)}")


def recategorize():
    """API 호출 없이 저장된 keywords.json 의 분류만 사전으로 다시 매긴다."""
    data = json.loads(OUT.read_text(encoding="utf-8"))
    words = load_category_words()
    changed = 0
    for r in data["rows"]:
        new = categorize(r[0], r[1], words)
        changed += new != r[1]
        r[1] = new
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"분류 변경 {changed}개 / 전체 {len(data['rows'])}개")


if __name__ == "__main__":
    recategorize() if "--recategorize" in sys.argv else main()
