# 황금 키워드 찾기 (윈도우 프로그램)

네이버 검색광고 API로 연관 키워드와 월간 검색수를, 네이버 검색 API로 블로그 문서 수를 모아
검색량은 많고 경쟁 글은 적은 "황금 키워드"를 찾는 PC 프로그램입니다.
사용자가 자기 API 키를 넣어 쓰고, 키와 데이터는 그 PC의 `%APPDATA%\KeywordFinder`에만 저장됩니다.

## 구조
| 파일 | 역할 |
|---|---|
| `keyword_finder.py` | 프로그램 본체. 127.0.0.1 에 작은 웹 서버를 띄우고 브라우저로 화면을 연다. SQLite 저장, 수집 작업, CSV 내보내기 |
| `naver.py` | 네이버 API 호출, 황금지수 계산, 단어 사전 분류 |
| `ui/index.html` | 화면 (키워드 찾기 · 키워드 수집 · API 설정) |
| `categories.txt` | 분류 사전 |
| `windows/launcher.c` | 더블클릭용 실행 파일(콘솔 창 없이 실행) |
| `build_windows.py` | 윈도우 배포 zip 만들기 |

## 개발 중 실행 (리눅스/맥/윈도우 공통, 파이썬 3.10+)
```bash
python3 app/keyword_finder.py
```

## 윈도우 배포 파일 만들기 (리눅스에서)
```bash
sudo apt install gcc-mingw-w64-x86-64
python3 app/build_windows.py
```
결과: `app/dist/황금키워드찾기-<버전>-windows.zip` (python.org 공식 포터블 파이썬 포함, 약 11MB)

## 보안
- 서버는 127.0.0.1 에만 열리고, Host 헤더와 실행마다 바뀌는 토큰을 확인해 다른 웹사이트가 몰래 호출하지 못하게 한다.
- 화면(탭)을 닫으면 3분 뒤, "프로그램 종료"를 누르면 바로 종료된다. 수집 중에는 기다린다.
