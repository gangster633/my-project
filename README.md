# 데일리툴

자주 검색되는 계산기 8종을 모은 정적 사이트입니다. Google AdSense(`ca-pub-9368731506053936`) 코드와 `ads.txt`가 들어 있습니다.

| 도구 | 파일 |
|---|---|
| 글자수 세기 | `char-counter.html` |
| 만 나이 계산기 | `age.html` |
| D-day 계산기 | `dday.html` |
| 퍼센트 계산기 | `percent.html` |
| 평수 계산기 | `pyeong.html` |
| BMI 계산기 | `bmi.html` |
| 대출 이자 계산기 | `loan.html` |
| 시급 월급 계산기 | `wage.html` |

사이트 소개, 개인정보처리방침, 문의하기 페이지와 `sitemap.xml`, `robots.txt`도 함께 만들어집니다.

## 구조

```
src/pages/    각 페이지 본문 (여기를 수정)
src/static/   CSS, 아이콘 등 그대로 복사되는 파일
build.py      공통 헤더·푸터·애드센스 코드를 입혀 docs/ 를 생성
docs/         실제로 배포되는 결과물 (직접 수정하지 말 것)
```

수정 후에는 항상 다시 빌드합니다.

```bash
python3 build.py
```

## 배포 (GitHub Pages)

- 사이트 주소: https://daytool.kr
- Settings → Pages: 브랜치 `claude/jolly-gates-b52r4q`, 폴더 `/docs`, Custom domain `daytool.kr`
- `src/static/CNAME` 파일이 도메인을 지정합니다. 도메인을 바꾸면 이 파일과 `build.py`의 `SITE_URL`을 함께 바꾸고 다시 빌드합니다.

### 가비아 DNS 설정

| 타입 | 호스트 | 값 |
|---|---|---|
| A | @ | 185.199.108.153 |
| A | @ | 185.199.109.153 |
| A | @ | 185.199.110.153 |
| A | @ | 185.199.111.153 |
| CNAME | www | gangster633.github.io. |
