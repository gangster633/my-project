#!/usr/bin/env python3
"""윈도우용 배포 파일(zip)을 만든다. 리눅스에서 실행한다.

필요: x86_64-w64-mingw32-gcc, x86_64-w64-mingw32-windres (apt install gcc-mingw-w64-x86-64), Pillow
결과: app/dist/황금키워드찾기-<버전>-windows.zip
  황금키워드찾기/
    황금키워드찾기.exe      <- 더블클릭으로 실행
    runtime/               <- 윈도우용 포터블 파이썬 (python.org 공식 embeddable 패키지)
    app/                   <- 프로그램 코드와 화면
    사용법.txt
"""
import io
import re
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

PY_VERSION = "3.12.8"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
NAME = "황금키워드찾기"

HERE = Path(__file__).resolve().parent
VERSION = re.search(r'VERSION = "([^"]+)"', (HERE / "keyword_finder.py").read_text(encoding="utf-8")).group(1)
DIST = HERE / "dist"
OUT = DIST / NAME
CACHE = DIST / ".cache"

README = """황금 키워드 찾기 {version}

[실행]
  "{name}.exe" 를 더블클릭하면 브라우저에 프로그램 화면이 열립니다.
  처음 실행하면 "Windows의 PC 보호" 창이 뜰 수 있습니다. "추가 정보" -> "실행"을 누르세요.

[처음 설정]
  1. 왼쪽 "API 설정"에 네이버 검색광고 API 키 3개를 넣습니다. (필수)
     searchad.naver.com -> 도구 -> API 사용 관리
  2. 블로그 문서 수와 황금지수를 보려면 NAVER API HUB 블로그 검색 키도 넣습니다. (선택)
     ncloud.com 콘솔 -> NAVER API HUB -> Application 등록(블로그 검색 선택) -> 인증 정보
  3. "연결 테스트"로 키가 맞는지 확인합니다.

[사용]
  "키워드 수집"에 씨앗 키워드를 넣고 "수집 시작"을 누르면 연관 키워드가 쌓입니다.
  "키워드 찾기"에서 황금지수, 검색량, 상승률로 찾고 엑셀(CSV)로 저장할 수 있습니다.
  같은 씨앗을 매일 수집하면 전일·7일 대비 변화가 계산됩니다.

[종료]
  왼쪽 아래 "프로그램 종료"를 누르거나, 브라우저 탭을 닫으면 잠시 뒤 자동으로 종료됩니다.

[데이터 위치]
  API 키와 수집한 키워드는 이 PC의 %APPDATA%\\KeywordFinder 폴더에만 저장됩니다.
  프로그램 폴더를 지워도 데이터는 남습니다.
"""


def run(*cmd):
    subprocess.run(cmd, check=True)


def make_icon(path):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), radius=56, fill=(251, 191, 36, 255))
    font = None
    for f in ["/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]:
        if Path(f).exists():
            font = ImageFont.truetype(f, 150)
            break
    if font:
        d.text((128, 122), "金", font=font, fill=(59, 42, 0, 255), anchor="mm")
    else:  # 한자 글꼴이 없으면 막대그래프 모양
        for i, h in enumerate((70, 120, 170)):
            d.rounded_rectangle((52 + i * 56, 200 - h, 92 + i * 56, 200), radius=10, fill=(59, 42, 0, 255))
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "app" / "ui").mkdir(parents=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    # 1. 포터블 파이썬
    cached = CACHE / Path(PY_URL).name
    if not cached.exists():
        print("파이썬 내려받는 중:", PY_URL)
        cached.write_bytes(urllib.request.urlopen(PY_URL, timeout=120).read())
    with zipfile.ZipFile(cached) as z:
        z.extractall(OUT / "runtime")

    # 2. 프로그램 파일
    for f in ["keyword_finder.py", "naver.py", "categories.txt"]:
        shutil.copy2(HERE / f, OUT / "app" / f)
    shutil.copy2(HERE / "ui" / "index.html", OUT / "app" / "ui" / "index.html")
    (OUT / "사용법.txt").write_text(README.format(version=VERSION, name=NAME).replace("\n", "\r\n"), encoding="utf-8-sig")

    # 3. 실행 파일 (아이콘 포함)
    build = CACHE / "build"
    build.mkdir(exist_ok=True)
    make_icon(build / "icon.ico")
    (build / "icon.rc").write_text('1 ICON "icon.ico"\n', encoding="utf-8")
    run("x86_64-w64-mingw32-windres", str(build / "icon.rc"), "-O", "coff", "-o", str(build / "icon.o"))
    run("x86_64-w64-mingw32-gcc", "-municode", "-mwindows", "-O2", "-s",
        str(HERE / "windows" / "launcher.c"), str(build / "icon.o"), "-o", str(OUT / f"{NAME}.exe"))

    # 4. 압축
    zip_path = DIST / f"{NAME}-{VERSION}-windows.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(OUT.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(DIST).as_posix())
    print(f"완료: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
