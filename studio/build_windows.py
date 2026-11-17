#!/usr/bin/env python3
"""AI 영상 공방 윈도우 배포 파일(zip)을 만든다. 리눅스에서 실행한다.

필요: x86_64-w64-mingw32-gcc, x86_64-w64-mingw32-windres (apt install gcc-mingw-w64-x86-64), Pillow, pip
결과: studio/dist/AI영상공방-<버전>-windows.zip
  AI영상공방/
    AI영상공방.exe     <- 더블클릭으로 실행
    runtime/          <- 윈도우용 포터블 파이썬 (python.org 공식 embeddable 패키지)
    app/              <- 프로그램 코드와 화면, app/lib 에 anthropic SDK
    tools/            <- ffmpeg.exe, ffprobe.exe (gyan.dev essentials 빌드, GPL)
    사용법.txt
"""
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_VERSION = "3.12.8"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
NAME = "AI영상공방"

HERE = Path(__file__).resolve().parent
VERSION = re.search(r'VERSION = "([^"]+)"', (HERE / "studio.py").read_text(encoding="utf-8")).group(1)
DIST = HERE / "dist"
OUT = DIST / NAME
CACHE = DIST / ".cache"
CODE = ["studio.py", "pipeline.py", "render.py", "ai.py", "prompts.py", "textutil.py"]

README = """AI 영상 공방 {version}

[실행]
  "{name}.exe" 를 더블클릭하면 브라우저에 프로그램 화면이 열립니다.
  처음 실행하면 "Windows의 PC 보호" 창이 뜰 수 있습니다. "추가 정보" -> "실행"을 누르세요.

[처음 설정]
  왼쪽 "설정 · API 키"에서 쓰고 싶은 AI 키를 넣습니다. (하나만 있어도 됩니다)
    - Anthropic(Claude): 대본·스토리보드·제목에 추천   console.anthropic.com
    - Google Gemini: 무료 사용량 있음, 글·음성·그림      aistudio.google.com/apikey
    - OpenAI: 글·음성·그림                              platform.openai.com
    - ElevenLabs: 자연스러운 음성                        elevenlabs.io
  키가 하나도 없어도 됩니다: 글 AI를 "직접 붙여넣기"로 두고 Claude.ai 같은 채팅에서 받은 답을 붙여 넣고,
  음성은 윈도우 기본 음성, 그림은 직접 올리거나 자리표시 카드로 흐름을 확인할 수 있습니다.

[만드는 순서]
  홈에서 기사 링크나 주제 입력 -> 대본 -> 음성 -> 화풍 -> 스토리보드 -> 이미지 -> 영상 -> 완성(쇼츠·썸네일·제목)
  "원클릭 자동 제작"을 켜면 한 번에 끝까지 진행합니다.

[종료]
  왼쪽 아래 "종료"를 누르거나, 브라우저 탭을 닫으면 잠시 뒤 자동으로 종료됩니다. 작업 중에는 끝날 때까지 기다립니다.

[데이터 위치]
  API 키와 프로젝트(대본·음성·그림·영상)는 이 PC의 %APPDATA%\\VideoWorkshop 폴더에만 저장됩니다.
  프로그램 폴더를 지우거나 새 버전으로 바꿔도 데이터는 남습니다.

[포함된 프로그램]
  FFmpeg (https://ffmpeg.org, GPL) - tools 폴더. 빌드: https://www.gyan.dev/ffmpeg/builds/
  Python {pyver} (https://python.org), Anthropic Python SDK
"""


def run(*cmd):
    subprocess.run(cmd, check=True)


def fetch(url, name):
    cached = CACHE / name
    if not cached.exists():
        print("내려받는 중:", url)
        with urllib.request.urlopen(url, timeout=600) as r, open(cached, "wb") as f:
            shutil.copyfileobj(r, f)
    return cached


def make_icon(path):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(256):  # 청록 -> 보라 그라데이션
        t = y / 255
        d.line([(0, y), (256, y)], fill=(int(34 + (139 - 34) * t), int(195 + (92 - 195) * t), int(230 + (246 - 230) * t), 255))
    mask = Image.new("L", (256, 256), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8, 8, 248, 248), radius=56, fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    d.polygon([(98, 72), (98, 184), (190, 128)], fill=(255, 255, 255, 255))  # 재생 버튼
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "app" / "ui").mkdir(parents=True)
    (OUT / "tools").mkdir()
    CACHE.mkdir(parents=True, exist_ok=True)

    # 1. 포터블 파이썬
    with zipfile.ZipFile(fetch(PY_URL, Path(PY_URL).name)) as z:
        z.extractall(OUT / "runtime")

    # 2. 프로그램 코드 + anthropic SDK(윈도우용 휠)
    for f in CODE:
        shutil.copy2(HERE / f, OUT / "app" / f)
    shutil.copy2(HERE / "ui" / "index.html", OUT / "app" / "ui" / "index.html")
    run(sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--target", str(OUT / "app" / "lib"),
        "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp", "--only-binary=:all:", "anthropic")
    (OUT / "사용법.txt").write_text(README.format(version=VERSION, name=NAME, pyver=PY_VERSION).replace("\n", "\r\n"), encoding="utf-8-sig")

    # 3. FFmpeg
    with zipfile.ZipFile(fetch(FFMPEG_URL, "ffmpeg-essentials.zip")) as z:
        for info in z.infolist():
            base = Path(info.filename).name
            if base in ("ffmpeg.exe", "ffprobe.exe"):
                (OUT / "tools" / base).write_bytes(z.read(info))
            elif base == "LICENSE" and "/" in info.filename.strip("/"):
                (OUT / "tools" / "FFMPEG_LICENSE.txt").write_bytes(z.read(info))

    # 4. 실행 파일 (아이콘 포함)
    build = CACHE / "build"
    build.mkdir(exist_ok=True)
    make_icon(build / "icon.ico")
    (build / "icon.rc").write_text('1 ICON "icon.ico"\n', encoding="utf-8")
    run("x86_64-w64-mingw32-windres", str(build / "icon.rc"), "-O", "coff", "-o", str(build / "icon.o"))
    run("x86_64-w64-mingw32-gcc", "-municode", "-mwindows", "-O2", "-s",
        str(HERE / "windows" / "launcher.c"), str(build / "icon.o"), "-o", str(OUT / f"{NAME}.exe"))

    # 5. 압축
    zip_path = DIST / f"{NAME}-{VERSION}-windows.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(OUT.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(DIST).as_posix())
    print(f"완료: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
