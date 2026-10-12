#!/usr/bin/env python3
"""src/ 의 페이지 조각에 공통 레이아웃(헤더·푸터·애드센스 코드)을 입혀 docs/ 에 정적 사이트를 만든다.

사용법: python3 build.py
도메인을 연결한 뒤에는 아래 SITE_URL 만 바꾸고 다시 실행하면 된다.
"""
import datetime
import html
import shutil
from pathlib import Path

SITE_NAME = "데일리툴"
SITE_URL = "https://gangster633.github.io/my-project"  # 도메인 연결 후 예: "https://dailytool.kr"
ADSENSE_CLIENT = "ca-pub-9368731506053936"

ROOT = Path(__file__).parent
SRC = ROOT / "src"
OUT = ROOT / "docs"

# (파일 이름, 제목, 설명, 메뉴에 표시할지)
TOOLS = [
    ("char-counter", "글자수 세기", "공백 포함·제외 글자수, 바이트, 단어 수를 실시간으로 세어 줍니다. 자기소개서, 블로그 글 분량 확인에 사용하세요."),
    ("age", "만 나이 계산기", "생년월일만 입력하면 만 나이, 연 나이, 띠, 다음 생일까지 남은 날을 바로 알려 줍니다."),
    ("dday", "D-day 계산기", "기념일, 시험일, 전역일까지 남은 날짜와 지난 날짜, 100일·1000일 기념일을 계산합니다."),
    ("percent", "퍼센트 계산기", "A의 B%는 얼마인지, 증가율·감소율, 할인가 등 자주 쓰는 퍼센트 계산을 한 번에 해결합니다."),
    ("pyeong", "평수 계산기", "평을 제곱미터(㎡)로, 제곱미터를 평으로 바로 변환합니다. 아파트 전용면적·공급면적 확인에 유용합니다."),
    ("bmi", "BMI 계산기", "키와 몸무게로 체질량지수(BMI)를 계산하고 대한비만학회 기준으로 비만도를 알려 줍니다."),
    ("loan", "대출 이자 계산기", "원리금균등, 원금균등, 만기일시 상환 방식별 월 상환액과 총 이자를 계산합니다."),
    ("wage", "시급 월급 계산기", "시급을 월급·연봉으로, 월급을 시급으로 환산합니다. 주휴수당 포함 계산을 지원합니다."),
]
PAGES = [
    ("about", "사이트 소개", f"{SITE_NAME}는 일상에서 자주 쓰는 계산기와 도구를 무료로 제공하는 사이트입니다."),
    ("privacy", "개인정보처리방침", f"{SITE_NAME}의 개인정보처리방침과 쿠키, 광고(Google AdSense) 관련 안내입니다."),
    ("contact", "문의하기", f"{SITE_NAME}에 대한 문의, 오류 제보, 도구 추가 요청 방법을 안내합니다."),
]

LAYOUT = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{full_title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{site}">
<meta property="og:title" content="{full_title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}">
<link rel="icon" href="assets/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="assets/style.css">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={adsense}" crossorigin="anonymous"></script>
</head>
<body>
<header class="site-header">
  <div class="wrap">
    <a class="logo" href="./">{site}</a>
    <nav class="nav">{nav}</nav>
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer class="site-footer">
  <div class="wrap">
    <p><a href="about.html">사이트 소개</a> · <a href="privacy.html">개인정보처리방침</a> · <a href="contact.html">문의하기</a></p>
    <p>계산 결과는 참고용이며, 중요한 결정은 관련 기관이나 전문가에게 확인하세요.</p>
    <p>&copy; {year} {site}</p>
  </div>
</footer>
</body>
</html>
"""


def render(slug, title, desc, body):
    path = "" if slug == "index" else f"{slug}.html"
    current = ' aria-current="page"'
    nav = "".join(
        f'<a href="{s}.html"{current if s == slug else ""}>{t}</a>' for s, t, _ in TOOLS
    )
    full_title = f"{SITE_NAME} - 자주 쓰는 무료 계산기 모음" if slug == "index" else f"{title} - {SITE_NAME}"
    return LAYOUT.format(
        full_title=html.escape(full_title),
        desc=html.escape(desc),
        url=f"{SITE_URL}/{path}",
        site=SITE_NAME,
        adsense=ADSENSE_CLIENT,
        nav=nav,
        body=body.strip(),
        year=datetime.date.today().year,
    )


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(SRC / "static", OUT)

    cards = "\n".join(
        f'<a class="card" href="{s}.html"><h2>{t}</h2><p>{d}</p></a>' for s, t, d in TOOLS
    )
    index_body = (SRC / "pages" / "index.html").read_text(encoding="utf-8").replace("{{cards}}", cards)
    (OUT / "index.html").write_text(
        render("index", SITE_NAME, "글자수 세기, 만 나이, D-day, 퍼센트, 평수, BMI, 대출 이자, 시급 계산기까지. 자주 쓰는 계산기를 설치 없이 무료로 사용하세요.", index_body),
        encoding="utf-8",
    )
    for slug, title, desc in TOOLS + PAGES:
        body = (SRC / "pages" / f"{slug}.html").read_text(encoding="utf-8")
        (OUT / f"{slug}.html").write_text(render(slug, title, desc, body), encoding="utf-8")

    today = datetime.date.today().isoformat()
    urls = [""] + [f"{s}.html" for s, _, _ in TOOLS + PAGES]
    sitemap = "\n".join(
        f"  <url><loc>{SITE_URL}/{u}</loc><lastmod>{today}</lastmod></url>" for u in urls
    )
    (OUT / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{sitemap}\n</urlset>\n',
        encoding="utf-8",
    )
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")
    (OUT / "ads.txt").write_text(f"google.com, {ADSENSE_CLIENT.removeprefix('ca-')}, DIRECT, f08c47fec0942fa0\n", encoding="utf-8")
    print(f"built {len(urls)} pages -> {OUT}")


if __name__ == "__main__":
    main()
