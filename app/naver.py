"""네이버 API 호출과 키워드 지표 계산 (표준 라이브러리만 사용)."""
import base64
import hashlib
import hmac
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class ApiError(Exception):
    pass


def _get_json(url, headers, retries=3):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 or e.code >= 500:
                last = f"HTTP {e.code}"
                time.sleep(2 ** attempt * 2)
                continue
            if e.code in (401, 403):
                raise ApiError(f"API 키가 올바르지 않거나 권한이 없습니다 (HTTP {e.code}). {body}") from None
            raise ApiError(f"HTTP {e.code}: {body}") from None
        except urllib.error.URLError as e:
            last = str(e.reason)
            time.sleep(2 ** attempt * 2)
    raise ApiError(f"네트워크 오류: {last}")


def _count(v):
    # 검색수가 적으면 "< 10" 같은 문자열로 온다.
    return int(v) if isinstance(v, (int, float)) else 5


def related_keywords(keys, hints):
    """검색광고 키워드도구: 씨앗(최대 5개)의 연관 키워드와 월간 검색수."""
    uri = "/keywordstool"
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(
        hmac.new(keys["ad_secret"].encode(), f"{ts}.GET.{uri}".encode(), hashlib.sha256).digest()
    ).decode()
    query = urllib.parse.urlencode({"hintKeywords": ",".join(h.replace(" ", "") for h in hints), "showDetail": "1"})
    headers = {"X-Timestamp": ts, "X-API-KEY": keys["ad_api_key"], "X-Customer": str(keys["ad_customer_id"]), "X-Signature": sig}
    rows = _get_json(f"https://api.searchad.naver.com{uri}?{query}", headers).get("keywordList", [])
    return [
        {"k": r["relKeyword"], "pc": _count(r.get("monthlyPcQcCnt")), "mo": _count(r.get("monthlyMobileQcCnt")), "comp": r.get("compIdx", "")}
        for r in rows
    ]


def blog_total(keys, keyword):
    """NAVER API HUB 블로그 검색: 블로그 문서 수. (2026년 개발자센터 검색 API가 API HUB로 이전됨)"""
    headers = {"X-NCP-APIGW-API-KEY-ID": keys["client_id"], "X-NCP-APIGW-API-KEY": keys["client_secret"]}
    q = urllib.parse.urlencode({"query": keyword, "display": 1})
    return int(_get_json(f"https://naverapihub.apigw.ntruss.com/search/v1/blog?{q}", headers).get("total", 0))


def _post_json(url, headers, body, retries=3):
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 or e.code >= 500:
                last = f"HTTP {e.code}"
                time.sleep(2 ** attempt * 2)
                continue
            if e.code in (401, 403):
                raise ApiError(f"API 키가 올바르지 않거나 이 API를 쓸 권한이 없습니다 (HTTP {e.code}). {msg}") from None
            raise ApiError(f"HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            last = str(e.reason)
            time.sleep(2 ** attempt * 2)
    raise ApiError(f"네트워크 오류: {last}")


def search_trend(keys, keywords, start, end):
    """NAVER API HUB 데이터랩 검색어 트렌드: 키워드(최대 5개)별 일별 상대지수 {키워드: {날짜: 비율}}.
    한 요청 안의 모든 키워드 중 최댓값이 100인 상대값이다."""
    headers = {"X-NCP-APIGW-API-KEY-ID": keys["client_id"], "X-NCP-APIGW-API-KEY": keys["client_secret"]}
    body = {
        "startDate": start, "endDate": end, "timeUnit": "date",
        "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords[:5]],
    }
    res = _post_json("https://naverapihub.apigw.ntruss.com/search-trend/v1/search", headers, body)
    return {r["title"]: {d["period"]: float(d["ratio"]) for d in r.get("data", [])} for r in res.get("results", [])}


def estimate_daily(monthly, ratios, days):
    """월간 검색수(최근 30일 합)를 데이터랩 일별 비율로 나눠 날짜별 검색수를 추정한다.
    요청 단위 정규화 상수는 자기 합으로 나누면서 사라지므로 키워드끼리 묶어 요청해도 된다."""
    vals = [ratios.get(d, 0.0) for d in days]
    total = sum(vals)
    if total <= 0:
        return [0] * len(days)
    return [round(monthly * v / total) for v in vals]


def golden_index(monthly, docs):
    """검색량 대비 문서가 적을수록, 검색량이 충분할수록 높다 (0~100)."""
    if docs is None:
        return None
    scarcity = 100 / (1 + docs / max(monthly, 1))
    volume = min(1.0, math.log10(monthly + 1) / 4)  # 월 1만 회 이상이면 1
    return round(scarcity * volume, 1)


class Categorizer:
    """키워드 안의 사전 단어 중 가장 뒤에서 끝나는 것(같으면 더 긴 것)으로 분류."""

    def __init__(self, path: Path):
        self.words = {}
        category = None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("##"):
                category = line.lstrip("#").strip()
            elif line and not line.startswith("#") and category:
                for w in line.split(","):
                    w = w.strip().replace(" ", "").upper()
                    if w:
                        self.words[w] = category
        self.categories = list(dict.fromkeys(self.words.values()))

    def __call__(self, keyword, fallback="기타"):
        kw = keyword.replace(" ", "").upper()
        best = None
        for w, cat in self.words.items():
            i = kw.rfind(w)
            if i != -1:
                cand = (i + len(w), len(w), cat)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        return best[2] if best else fallback
