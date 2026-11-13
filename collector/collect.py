#!/usr/bin/env python3
"""네이버 키워드 수집기 (MVP)

1. collector/seeds.txt 의 씨앗 키워드로 네이버 검색광고 API(키워드도구)를 불러
   연관 키워드와 월간 검색수(PC/모바일)를 모은다.
2. 키워드마다 네이버 검색 API(블로그)로 블로그 문서 수를 센다.
3. 황금지수를 계산해 src/static/data/keywords.json 에 저장한다 (build.py 가 docs/ 로 복사).
4. 날짜별 검색량을 data/history.json 에 쌓아 전일·7일 전 대비 변화율을 구한다.

필요한 환경변수 (GitHub 저장소 Settings > Secrets and variables > Actions 에 등록):
  NAVER_AD_API_KEY, NAVER_AD_SECRET, NAVER_AD_CUSTOMER_ID   검색광고 API
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET                      NAVER API HUB(ncloud.com) 블로그 검색 API (선택)
    두 값이 없으면 블로그 문서 수·황금지수는 비워 두고 검색량만 모은다.

표준 라이브러리만 사용한다.
"""
import base64
import csv
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
DOCS_CACHE = ROOT / "data" / "docs.json"  # {키워드: [문서 수, 센 날짜]}
DAILY = ROOT / "data" / "daily"  # 날짜별 전체 수집 기록 (지우지 않고 계속 쌓는다)

MAX_KEYWORDS = int(os.environ.get("MAX_KEYWORDS", "10000"))  # 사이트에 보여줄 최대 키워드 수 (날짜별 기록에는 전부 저장)
MAX_DOCS = int(os.environ.get("MAX_DOCS", "1000"))            # 하루에 블로그 문서 수를 새로 셀 최대 키워드 수
DOCS_REFRESH_DAYS = int(os.environ.get("DOCS_REFRESH_DAYS", "7"))  # 한 번 센 문서 수는 이 기간 동안 다시 세지 않는다
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
    # 2026년부터 네이버 검색 API는 NAVER API HUB(네이버 클라우드)로 옮겨졌다. 개발자센터 옛 키는 쓸 수 없다.
    headers = {"X-NCP-APIGW-API-KEY-ID": env("NAVER_CLIENT_ID"), "X-NCP-APIGW-API-KEY": env("NAVER_CLIENT_SECRET")}
    q = urllib.parse.urlencode({"query": keyword, "display": 1})
    return int(http_json(f"https://naverapihub.apigw.ntruss.com/search/v1/blog?{q}", headers).get("total", 0))


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
    ranked = sorted(found.values(), key=lambda x: x["pc"] + x["mo"], reverse=True)
    items = ranked[:MAX_KEYWORDS]
    print(f"연관 키워드 {len(found)}개 수집, 사이트에는 {len(items)}개 표시")

    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else {}

    has_search = bool(os.environ.get("NAVER_CLIENT_ID", "").strip() and os.environ.get("NAVER_CLIENT_SECRET", "").strip())
    if not has_search:
        print("검색 API 키가 없어 블로그 문서 수는 건너뜁니다.")
    # 문서 수는 하루 사이에 크게 변하지 않으므로, 최근에 센 값은 다시 쓰고
    # 오래됐거나 처음 보는 키워드만 검색량 많은 순으로 하루 MAX_DOCS 개까지 센다. (외부 호출과 실행 시간을 줄인다)
    docs_cache = json.loads(DOCS_CACHE.read_text(encoding="utf-8")) if DOCS_CACHE.exists() else {}
    if not docs_cache and OUT.exists():  # 처음에는 지난 결과 파일의 문서 수로 채운다
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        prev_day = prev.get("updated", today)[:10]
        docs_cache = {r[0]: [r[4], prev_day] for r in prev["rows"] if r[4] is not None}
    fresh_after = (datetime.date.fromisoformat(today) - datetime.timedelta(days=DOCS_REFRESH_DAYS)).isoformat()
    for it in items:
        cached = docs_cache.get(it["k"])
        if cached:
            it["docs"] = cached[0]
    stale = [it for it in items if not (docs_cache.get(it["k"]) and docs_cache[it["k"]][1] > fresh_after)]
    doc_targets = stale[:MAX_DOCS] if has_search else []
    print(f"블로그 문서 수: 저장된 값 {len(items) - len(stale)}개 재사용, 오늘 {len(doc_targets)}개 새로 조회")
    for n, it in enumerate(doc_targets, 1):
        try:
            it["docs"] = blog_total(it["k"])
            docs_cache[it["k"]] = [it["docs"], today]
        except RuntimeError as e:
            print(f"  문서 수 실패 {it['k']}: {e}")
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                print("검색 API 키가 올바르지 않아 블로그 문서 수 조회를 멈춥니다. Secrets 를 확인하세요.")
                break
        if n % 200 == 0:
            print(f"  {n}/{len(doc_targets)}")
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
    keep_after = (datetime.date.fromisoformat(today) - datetime.timedelta(days=60)).isoformat()
    docs_cache = {k: v for k, v in docs_cache.items() if v[1] >= keep_after}
    DOCS_CACHE.write_text(json.dumps(docs_cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"저장 완료: {len(rows)}개 -> {OUT.relative_to(ROOT)}")

    # 날짜별 기록: 그날 찾은 키워드 전부를 엑셀에서 바로 열리는 CSV로 남긴다.
    DAILY.mkdir(parents=True, exist_ok=True)
    daily = DAILY / f"{today}.csv"
    with daily.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword", "category", "pc", "mobile", "total", "blog_docs", "comp"])
        for it in ranked:
            w.writerow([it["k"], it["cat"], it["pc"], it["mo"], it["pc"] + it["mo"], it.get("docs", ""), it["comp"]])
    print(f"날짜별 기록: {len(ranked)}개 -> {daily.relative_to(ROOT)}")


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
