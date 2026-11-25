#!/usr/bin/env python3
"""AI 영상 공방 - 기사·주제 하나로 유튜브 영상(롱폼·쇼츠)을 만드는 윈도우 프로그램

내 PC에서 작은 웹 서버(127.0.0.1)를 띄우고 화면을 브라우저로 연다.
API 키와 프로젝트는 PC 안(%APPDATA%\\VideoWorkshop)에만 저장된다.
"""
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))  # 윈도우 배포판에 함께 넣은 anthropic 패키지
import ai  # noqa: E402
import pipeline  # noqa: E402
import prompts  # noqa: E402
import render  # noqa: E402

APP_NAME = "AI 영상 공방"
VERSION = "1.3.21"
DATA_DIR = Path(os.environ.get("VIDEO_WORKSHOP_HOME") or (Path(os.environ.get("APPDATA", Path.home())) / "VideoWorkshop"))
PROJECTS = DATA_DIR / "projects"
CONFIG = DATA_DIR / "config.json"
RUNNING = DATA_DIR / "running.json"
USER_FONTS = DATA_DIR / "fonts"
PREVIEWS = DATA_DIR / "previews"
STYLE_DIR = DATA_DIR / "style_examples"
pipeline.STYLE_DIR = STYLE_DIR
TOKEN = secrets.token_urlsafe(24)
last_ping = [time.time()]
IDLE_EXIT = 180
KEY_FIELDS = ["anthropic_key", "openai_key", "gemini_key", "elevenlabs_key", "azure_speech_key"]
SETTING_FIELDS = ["text_provider", "claude_model", "claude_effort", "openai_text_model", "gemini_text_model",
                  "image_provider", "gemini_image_model", "openai_image_model", "openai_image_quality",
                  "gemini_tts_model", "openai_tts_model", "elevenlabs_model", "azure_region", "tts_workers", "image_workers", "video_encoder", "render_workers",
                  "default_category", "categories_custom"]
store_lock = threading.RLock()
# 썸네일 편집기 글꼴 (모두 SIL Open Font License, 빌드 때 ui/fonts 에 넣는다)
THUMB_FONTS = [("BlackHanSans-Regular.ttf", "검은고딕 (굵고 강하게)"), ("DoHyeon-Regular.ttf", "도현 (깔끔한 굵은체)"),
               ("GothicA1-Black.ttf", "고딕 A1 (아주 굵게)"), ("Jua-Regular.ttf", "주아 (둥글고 귀엽게)"),
               ("Gugi-Regular.ttf", "구기 (레트로)"), ("NanumMyeongjo-ExtraBold.ttf", "나눔명조 (신문 헤드라인)"),
               ("YeonSung-Regular.ttf", "연성 (붓글씨 느낌)"), ("NanumPenScript-Regular.ttf", "나눔손글씨 펜 (손글씨)")]


# ---------------- 저장소 ----------------

def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG)


def pdir(pid):
    if not re.fullmatch(r"[\w-]{6,60}", pid or ""):
        raise ValueError("잘못된 프로젝트")
    return PROJECTS / pid


def load_project(pid):
    with store_lock:
        return pipeline.upgrade(json.loads((pdir(pid) / "project.json").read_text(encoding="utf-8")))


def save_project(p):
    with store_lock:
        d = pdir(p["id"])
        d.mkdir(parents=True, exist_ok=True)
        p["updated"] = time.strftime("%Y-%m-%d %H:%M")
        tmp = d / "project.tmp"
        tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(d / "project.json")


def list_projects():
    out = []
    if PROJECTS.exists():
        for f in PROJECTS.glob("*/project.json"):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            sb = p.get("storyboard") or {}
            thumb = next((s.get("image") for s in sb.get("scenes", []) if s.get("image")), None)
            out.append({"id": p["id"], "name": p.get("name"), "category": p.get("category"), "updated": p.get("updated") or p.get("created"),
                        "chars": len(re.sub(r"\s", "", p.get("script", ""))), "seconds": (p.get("audio") or {}).get("total"),
                        "scenes": len(sb.get("scenes", [])), "thumb": thumb, "done": "video" in (p.get("outputs") or {}),
                        "step": step_of(p)})
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


def step_of(p):
    if "video" in (p.get("outputs") or {}):
        return "export"
    sb = p.get("storyboard")
    if sb and all(s.get("image") or s.get("video") for s in sb.get("scenes", [])):
        return "video"
    if sb:
        return "image"
    if p.get("audio"):
        return "storyboard" if p.get("style", {}).get("chosen") else "style"
    if p.get("script"):
        return "voice"
    return "script"


# ---------------- 작업(백그라운드) ----------------

job = {"running": False, "pid": None, "step": None, "done": 0, "total": 0, "message": "대기 중", "error": None, "finished": None}
job_lock = threading.Lock()
cancel_flag = [False]


def set_job(**kw):
    with job_lock:
        job.update(kw)


STEP_NAMES = {"script": "대본", "factcheck": "사실 대조", "translate": "번역", "voice": "음성", "storyboard": "스토리보드",
              "images": "이미지", "render": "영상", "shorts": "쇼츠", "thumbs": "썸네일", "meta": "업로드 정보",
              "auto": "원클릭 자동 제작", "package": "편집용 묶음", "style_examples": "화풍 예시 그림"}


def start_job(pid, step, opts):
    with job_lock:
        if job["running"]:
            raise RuntimeError("다른 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요.")
        job.update(running=True, pid=pid, step=step, done=0, total=0, message=f"{STEP_NAMES.get(step, step)} 시작", error=None, finished=None)
    cancel_flag[0] = False
    threading.Thread(target=_run_job, args=(pid, step, opts), daemon=True).start()


def _run_job(pid, step, opts):
    cfg = load_config()
    ctx = pipeline.Ctx(report=lambda **kw: set_job(**kw), cancelled=lambda: cancel_flag[0])
    folder = pdir(pid)
    p = load_project(pid)
    try:
        steps = [step]
        if step == "auto":
            order = ("script", "factcheck", "voice", "storyboard", "images", "meta", "render", "thumbs", "shorts")
            steps = [s for s in order if s in opts.get("steps", order)]
            if p.get("script"):
                steps = [s for s in steps if s not in ("script", "factcheck")]
        for s in steps:
            ctx.check()
            if step == "auto":
                set_job(message=f"[{STEP_NAMES[s]}] 시작", done=0, total=0)
            _run_step(p, cfg, ctx, folder, s, opts)
            if s in ("render", "shorts"):
                p.pop("last_error", None)
            if step == "auto" and s == "factcheck":
                n = pipeline.apply_factcheck(p)  # 자동 제작에서는 제안된 수정을 바로 반영
                set_job(message=f"사실 대조: {n}곳 고침")
            save_project(p)
        save_config(cfg)
        set_job(running=False, message="완료", finished=time.time())
    except pipeline.Cancelled:
        save_project(p)
        set_job(running=False, message="취소됨", finished=time.time())
    except (ai.AiError, render.RenderError, ValueError, OSError) as e:
        p["last_error"] = {"step": STEP_NAMES.get(step, step), "message": str(e), "at": time.strftime("%Y-%m-%d %H:%M")}
        save_project(p)
        set_job(running=False, error=str(e), message="실패", finished=time.time())
    except Exception as e:  # 예상 못 한 오류도 화면에 보여 준다
        traceback.print_exc()
        p["last_error"] = {"step": STEP_NAMES.get(step, step), "message": f"{type(e).__name__}: {e}", "at": time.strftime("%Y-%m-%d %H:%M")}
        save_project(p)
        set_job(running=False, error=f"{type(e).__name__}: {e}", message="실패", finished=time.time())


def _run_step(p, cfg, ctx, folder, s, opts):
    if s == "script":
        pipeline.run_script(p, cfg, ctx)
    elif s == "factcheck":
        pipeline.run_factcheck(p, cfg, ctx)
    elif s == "translate":
        pipeline.run_translate(p, cfg, ctx, opts.get("lang", "ja"))
    elif s == "voice":
        lang = opts.get("lang", "ko")
        voice = opts.get("voice") if lang != "ko" else None
        pipeline.run_voice(p, cfg, ctx, folder, lang, voice)
    elif s == "storyboard":
        if opts.get("even") or (cfg.get("text_provider") == "manual"):
            pipeline.even_storyboard(p)
        else:
            pipeline.run_storyboard(p, cfg, ctx)
    elif s == "images":
        only = opts.get("only")
        pipeline.run_images(p, cfg, ctx, folder, only=only, provider=opts.get("provider"), overwrite=opts.get("overwrite", False))
    elif s == "render":
        pipeline.run_render(p, cfg, ctx, folder, lang=opts.get("lang", "ko"))
    elif s == "shorts":
        pipeline.run_render(p, cfg, ctx, folder, shorts=True)
    elif s == "thumbs":
        pipeline.run_thumbs(p, cfg, ctx, folder, int(opts.get("scene") or 0), opts.get("text"))
    elif s == "meta":
        if cfg.get("text_provider") == "manual" and opts.get("auto"):
            return
        pipeline.run_meta(p, cfg, ctx)
    elif s == "style_examples":
        pipeline.run_style_examples(cfg, ctx, opts.get("ids") or [x[0] for x in prompts.STYLES], opts.get("provider"))
    elif s == "package":
        ctx.report(message="편집용 묶음 만드는 중")
        pipeline.export_package(p, folder)
        p["outputs"]["package"] = {"file": "out/edit_package.zip", "made": time.strftime("%Y-%m-%d %H:%M")}


# ---------------- 반자동 그림 도우미 ----------------
# 무료 그림 사이트(Gemini 앱 등)에서 사람이 직접 그림을 받으면, 다운로드 폴더를 지켜보다가 다음 빈 장면에 넣는다.
# 사이트를 자동 조작하지 않으므로 약관 문제나 계정 위험이 없다.

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
helper = {"on": False, "pid": None, "dir": "", "since": 0.0, "target": None, "added": [], "error": None}
helper_lock = threading.Lock()


def default_downloads():
    home = Path.home()
    for d in (home / "Downloads", home / "다운로드"):
        if d.exists():
            return str(d)
    return str(home / "Downloads")


def next_empty(p, after=-1):
    scenes = p["storyboard"]["scenes"]
    order = list(range(after + 1, len(scenes))) + list(range(0, after + 1))
    return next((i for i in order if not scenes[i].get("image") and not scenes[i].get("video")), None)


def helper_loop():
    seen = {}
    while True:
        time.sleep(1.0)
        with helper_lock:
            if not helper["on"]:
                seen.clear()
                continue
            d, since, pid = Path(helper["dir"]), helper["since"], helper["pid"]
        try:
            files = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in IMG_EXT and f.stat().st_mtime >= since]
        except OSError as e:
            with helper_lock:
                helper["error"] = f"폴더를 읽지 못했습니다: {e}"
            continue
        for f in sorted(files, key=lambda x: x.stat().st_mtime):
            size = f.stat().st_size
            if seen.get(str(f)) == "done":
                continue
            if seen.get(str(f)) != size:  # 다운로드가 끝나 크기가 멈출 때까지 한 번 기다린다
                seen[str(f)] = size
                continue
            seen[str(f)] = "done"
            try:
                with store_lock:
                    p = load_project(pid)
                    with helper_lock:
                        target = helper["target"]
                    if target is None or target >= len(p["storyboard"]["scenes"]):
                        target = next_empty(p)
                    if target is None:
                        with helper_lock:
                            helper["error"] = "빈 장면이 없어요. 모든 장면에 그림이 들어갔어요."
                        continue
                    folder = pdir(pid)
                    (folder / "uploads").mkdir(exist_ok=True)
                    dst = folder / "uploads" / f"scene_{target:03d}_{int(time.time() * 1000) % 10_000_000}{f.suffix.lower()}"
                    shutil.copy2(f, dst)
                    sc = p["storyboard"]["scenes"][target]
                    sc["image"] = f"uploads/{dst.name}"
                    sc["image_prompt_used"] = "도우미로 받음"
                    save_project(p)
                    nxt = next_empty(p, target)
                with helper_lock:
                    helper["added"].append({"scene": target, "file": f.name, "at": time.time()})
                    helper["target"] = nxt
                    helper["error"] = None
            except (OSError, ValueError, KeyError) as e:
                with helper_lock:
                    helper["error"] = f"그림을 넣지 못했습니다: {e}"


threading.Thread(target=helper_loop, daemon=True).start()


# ---------------- 웹 서버 ----------------

def prompt_for(p, step, cfg):
    """단계별 지시문 (직접 붙여넣기·반자동 도우미용)."""
    if step == "script":
        return pipeline.script_prompt(p, cfg)
    elif step == "factcheck":
        return pipeline.factcheck_prompt(p)
    elif step.startswith("translate:"):
        return prompts.build_translate(p["script"], step.split(":")[1])
    elif step == "storyboard":
        return pipeline.storyboard_prompt(p)
    elif step == "meta":
        return pipeline.meta_prompt(p)
    elif step == "images":
        return pipeline.all_prompts_text(p)
    elif step == "helper_intro":
        return pipeline.helper_intro(p)
    elif step.startswith("helper_batch"):
        return pipeline.helper_batch(p, int(step.split(":")[1]) if ":" in step else 0)
    elif step == "helper_next":
        return pipeline.helper_next(p)
    elif step.startswith("scene:"):
        return pipeline.helper_scene_prompt(p, int(step.split(":")[1]))
    elif step.startswith("style:"):
        return prompts.style_sample_prompt(step.split(":", 1)[1])
    else:
        raise ValueError("알 수 없는 단계")


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoWorkshop"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def authed(self, q):
        return secrets.compare_digest(self.headers.get("X-Token") or (q.get("token") or [""])[0], TOKEN)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def send_file(self, path, download=None):
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            from urllib.parse import quote
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download)}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---- GET ----
    def do_GET(self):
        if not self.allowed():
            return self.send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            page = (HERE / "ui" / "index.html").read_text(encoding="utf-8")
            return self.send(HTTPStatus.OK, page.replace("__TOKEN__", TOKEN).replace("__VERSION__", VERSION), "text/html; charset=utf-8")
        if not self.authed(q):
            return self.send(HTTPStatus.FORBIDDEN, {"error": "token"})
        last_ping[0] = time.time()
        try:
            return self.route_get(url.path, q)
        except (ValueError, OSError, KeyError) as e:
            return self.send(HTTPStatus.BAD_REQUEST, {"error": str(e)})

    def route_get(self, path, q):
        one = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
        if path == "/api/ping":
            return self.send(HTTPStatus.OK, {"ok": True, "app": APP_NAME, "version": VERSION})
        if path.startswith("/files/"):
            parts = unquote(path[len("/files/"):]).split("/", 1)
            if parts[0] == "_preview":
                base = PREVIEWS
            elif parts[0] == "_styles":
                base = STYLE_DIR
            elif parts[0] == "_fonts":  # 썸네일 편집기용 무료 글꼴 (ui/fonts)
                base = HERE / "ui" / "fonts"
            else:
                base = pdir(parts[0])
            f = (base / parts[1]).resolve()
            if not str(f).startswith(str(base.resolve())) or not f.is_file():
                return self.send(HTTPStatus.NOT_FOUND, {"error": "없는 파일"})
            return self.send_file(f, one("download") or None)
        if path == "/api/config":
            cfg = load_config()
            fonts = render.system_fonts(USER_FONTS)
            return self.send(HTTPStatus.OK, {
                "keys": {k: bool(cfg.get(k)) for k in KEY_FIELDS},
                "masked": {k: (cfg[k][:5] + "…" + cfg[k][-3:]) if cfg.get(k) and len(cfg[k]) > 10 else "" for k in KEY_FIELDS},
                "settings": {k: cfg.get(k) for k in SETTING_FIELDS},
                "styles": [{"id": s[0], "group": s[1], "name": s[2], "desc": s[3], "prompt": s[4]} for s in prompts.STYLES],
                "categories": list(prompts.CATEGORIES) + [c for c in (cfg.get("categories_custom") or "").split(",") if c.strip()],
                "style_examples": {f.stem: f"{f.name}?t={int(f.stat().st_mtime)}" for f in sorted(STYLE_DIR.glob("*.*")) if not f.stem.startswith("_")} if STYLE_DIR.exists() else {},
                "fonts": fonts, "thumb_fonts": [{"file": f, "name": n} for f, n in THUMB_FONTS if (HERE / "ui" / "fonts" / f).exists()],
                "ffmpeg": render.ffmpeg_ok(), "data_dir": str(DATA_DIR), "version": VERSION,
                "defaults": {"claude_model": ai.CLAUDE_DEFAULT, "openai_text_model": ai.OPENAI_TEXT_DEFAULT,
                             "gemini_text_model": ai.GEMINI_TEXT_DEFAULT},
                "platform": sys.platform,
            })
        if path == "/api/projects":
            return self.send(HTTPStatus.OK, {"projects": list_projects()})
        if path == "/api/project":
            p = load_project(one("id"))
            cfg = load_config()
            return self.send(HTTPStatus.OK, {"project": p, "target_chars": pipeline.chars_for(p, cfg),
                                             "length_warning": pipeline.length_warning(p),
                                             "scene_times": pipeline.scene_times(p) if p.get("audio") and p.get("storyboard") else [],
                                             "chapters": [[pipeline.fmt_time(t), n] for t, n in pipeline.chapters(p)],
                                             "description": pipeline.full_description(p)})
        if path == "/api/helper":
            with helper_lock:
                h = dict(helper, added=list(helper["added"][-30:]))
            if not h["dir"]:
                h["dir"] = load_config().get("helper_dir") or default_downloads()
            return self.send(HTTPStatus.OK, h)
        if path == "/api/job":
            with job_lock:
                return self.send(HTTPStatus.OK, dict(job))
        if path == "/api/voices":
            return self.send(HTTPStatus.OK, {"voices": ai.voice_catalog(load_config())})
        if path == "/api/prompt":
            p = load_project(one("id"))
            step = one("step")
            cfg = load_config()
            text = prompt_for(p, step, cfg)
            return self.send(HTTPStatus.OK, {"prompt": text})
        return self.send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if not self.allowed() or not self.authed(q):
            return self.send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        last_ping[0] = time.time()
        try:
            if url.path == "/api/upload":
                return self.upload(q)
            return self.route_post(url.path, self.body_json())
        except RuntimeError as e:
            return self.send(HTTPStatus.CONFLICT, {"error": str(e)})
        except (ai.AiError, render.RenderError) as e:
            return self.send(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except (ValueError, OSError, KeyError, TypeError) as e:
            return self.send(HTTPStatus.BAD_REQUEST, {"error": f"{e}"})

    def upload(self, q):
        one = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
        kind = one("kind")
        name = Path(unquote(self.headers.get("X-Filename") or "file")).name
        ext = Path(name).suffix.lower()
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000_000:
            raise ValueError("파일이 너무 큽니다")
        allowed = {"scene": (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".m4v"),
                   "logo": (".png", ".jpg", ".jpeg", ".webp"), "bgm": (".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"),
                   "font": (".ttf", ".otf", ".ttc"), "style": (".png", ".jpg", ".jpeg", ".webp"), "thumb": (".jpg", ".jpeg")}
        if kind not in allowed or ext not in allowed[kind]:
            raise ValueError(f"이 파일 형식은 쓸 수 없습니다: {ext}")
        if kind == "style":
            sid = one("style")
            if sid not in prompts.STYLE_MAP:
                raise ValueError("없는 화풍")
            STYLE_DIR.mkdir(parents=True, exist_ok=True)
            for old in STYLE_DIR.glob(f"{sid}.*"):
                old.unlink()
            self._save_body(STYLE_DIR / f"{sid}{ext}", n)
            return self.send(HTTPStatus.OK, {"ok": True})
        if kind == "font":
            USER_FONTS.mkdir(parents=True, exist_ok=True)
            dst = USER_FONTS / name
            self._save_body(dst, n)
            return self.send(HTTPStatus.OK, {"ok": True, "file": str(dst), "family": render.font_family(dst)})
        pid = one("id")
        with store_lock:
            p = load_project(pid)
            folder = pdir(pid)
            if kind == "thumb":  # 썸네일 편집기에서 만든 그림: 맨 앞에 넣는다
                (folder / "out").mkdir(exist_ok=True)
                for old in (folder / "out").glob("thumb_custom_*.jpg"):
                    old.unlink()
                rel = f"out/thumb_custom_{int(time.time() * 1000) % 10_000_000}.jpg"
                self._save_body(folder / rel, n)
                thumbs = [t for t in p["outputs"].get("thumbs") or [] if not Path(t).name.startswith("thumb_custom_")]
                p["outputs"]["thumbs"] = [rel] + thumbs
                save_project(p)
                return self.send(HTTPStatus.OK, {"ok": True, "file": rel})
            sub = {"scene": "uploads", "logo": "assets", "bgm": "assets"}[kind]
            (folder / sub).mkdir(exist_ok=True)
            stamp = int(time.time() * 1000) % 10_000_000
            if kind == "scene":
                i = int(one("scene"))
                sc = p["storyboard"]["scenes"][i]
                dst = folder / sub / f"scene_{i:03d}_{stamp}{ext}"
                self._save_body(dst, n)
                rel = f"{sub}/{dst.name}"
                if ext in (".mp4", ".mov", ".webm", ".m4v"):
                    sc["video"] = rel
                else:
                    sc["image"] = rel
                    sc["image_prompt_used"] = "직접 올림"
            else:
                dst = folder / sub / f"{kind}{ext}"
                for old in (folder / sub).glob(f"{kind}.*"):
                    old.unlink()
                self._save_body(dst, n)
                p["video"][kind] = f"{sub}/{dst.name}"
            save_project(p)
        return self.send(HTTPStatus.OK, {"ok": True})

    def _save_body(self, dst, n):
        with open(dst, "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)

    def route_post(self, path, d):
        if path == "/api/quit":
            self.send(HTTPStatus.OK, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        if path == "/api/config":
            cfg = load_config()
            for k in KEY_FIELDS:
                v = str(d.get(k, "")).strip()
                if v:
                    cfg[k] = v
                elif d.get("clear_" + k):
                    cfg.pop(k, None)
            for k in SETTING_FIELDS:
                if k in d:
                    cfg[k] = d[k]
            save_config(cfg)
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/test_keys":
            cfg = load_config()
            res = {}
            if cfg.get("anthropic_key"):
                try:
                    res["anthropic_key"] = "정상: " + ai._claude(dict(cfg, claude_effort="low"), "'연결 성공'이라고만 답해.", None, 2000)[:30]
                except ai.AiError as e:
                    res["anthropic_key"] = f"실패: {e}"
            if cfg.get("openai_key"):
                try:
                    ai.http_json("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {cfg['openai_key']}"}, timeout=20, retries=0)
                    res["openai_key"] = "정상"
                except ai.AiError as e:
                    res["openai_key"] = f"실패: {e}"
            if cfg.get("gemini_key"):
                try:
                    ai.http_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", headers={"x-goog-api-key": cfg["gemini_key"]}, timeout=20, retries=0)
                    res["gemini_key"] = "정상"
                except ai.AiError as e:
                    res["gemini_key"] = f"실패: {e}"
            if cfg.get("elevenlabs_key"):
                try:
                    ai.http_json("https://api.elevenlabs.io/v1/user", headers={"xi-api-key": cfg["elevenlabs_key"]}, timeout=20, retries=0)
                    res["elevenlabs_key"] = "정상"
                except ai.AiError as e:
                    res["elevenlabs_key"] = f"실패: {e}"
            if cfg.get("azure_speech_key"):
                try:
                    ai._azure_cache.clear()
                    res["azure_speech_key"] = f"정상 (한국어 음성 {len(ai.azure_voices(cfg))}개)"
                except ai.AiError as e:
                    res["azure_speech_key"] = f"실패: {e} (키와 지역이 맞는지 확인하세요)"
            return self.send(HTTPStatus.OK, res)
        if path == "/api/project/new":
            cfg = load_config()
            p = pipeline.new_project(d.get("name", ""), d.get("category") or cfg.get("default_category") or "경제")
            if cfg.get("last_voice"):
                p["voice"].update(cfg["last_voice"])
            if cfg.get("last_style"):
                p["style"].update(cfg["last_style"])
            if cfg.get("last_sub"):
                p["sub"].update(cfg["last_sub"])
            if cfg.get("last_video"):
                p["video"].update({k: v for k, v in cfg["last_video"].items() if k not in ("logo", "bgm")})
            for k in ("topic", "source_url", "source_text", "minutes"):
                if d.get(k):
                    p[k] = d[k]
            save_project(p)
            # 로고·배경음은 이전 프로젝트 것을 이어서 쓴다
            prev = cfg.get("last_assets") or {}
            for kind in ("logo", "bgm"):
                src = Path(prev.get(kind, ""))
                if prev.get(kind) and src.exists():
                    (pdir(p["id"]) / "assets").mkdir(exist_ok=True)
                    shutil.copy2(src, pdir(p["id"]) / "assets" / src.name)
                    p["video"][kind] = f"assets/{src.name}"
            save_project(p)
            return self.send(HTTPStatus.OK, {"id": p["id"]})
        if path == "/api/project/save":
            with store_lock:
                p = load_project(d["id"])
                for k, v in d.get("fields", {}).items():
                    if k in ("id", "created"):
                        continue
                    if isinstance(v, dict) and isinstance(p.get(k), dict) and k not in ("storyboard", "meta", "audio"):
                        p[k].update(v)
                    else:
                        p[k] = v
                if "script" in d.get("fields", {}):
                    from textutil import check_script
                    p["checks"] = check_script(p["script"], pipeline.chars_for(p, load_config()))
                save_project(p)
            if any(k in d.get("fields", {}) for k in ("voice", "sub", "video", "style")):
                cfg = load_config()  # 다음 새 프로젝트의 기본값으로 기억
                cfg["last_voice"] = {k: p["voice"][k] for k in p["voice"] if k != "dictionary"}
                cfg["last_voice"]["dictionary"] = p["voice"].get("dictionary", "")
                cfg["last_sub"] = p["sub"]
                cfg["last_video"] = p["video"]
                cfg["last_style"] = {k: p["style"].get(k) for k in ("id", "custom", "scene_seconds", "chosen")}
                cfg["last_assets"] = {k: str(pdir(p["id"]) / p["video"][k]) for k in ("logo", "bgm") if p["video"].get(k)}
                save_config(cfg)
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/project/delete":
            with job_lock:
                if job["running"] and job["pid"] == d["id"]:
                    raise RuntimeError("작업 중인 프로젝트는 지울 수 없습니다.")
            shutil.rmtree(pdir(d["id"]), ignore_errors=True)
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/project/duplicate":
            p = load_project(d["id"])
            src = pdir(p["id"])
            new = pipeline.new_project(p["name"] + " (복사)", p["category"])
            shutil.copytree(src, pdir(new["id"]), ignore=shutil.ignore_patterns("build_*", "out"))
            p.update(id=new["id"], name=new["name"], created=new["created"], outputs={})
            save_project(p)
            return self.send(HTTPStatus.OK, {"id": p["id"]})
        if path == "/api/fetch_url":
            try:
                art = pipeline.fetch_article(d["url"].strip())
            except Exception as e:  # 사이트마다 오류가 제각각이라 메시지로 돌려준다
                raise ValueError(f"기사를 가져오지 못했습니다: {e}. 본문을 복사해 '참고 자료'에 직접 붙여 넣어도 됩니다.")
            return self.send(HTTPStatus.OK, art)
        if path == "/api/import":
            # 직접 붙여넣기 모드: 다른 AI 에서 받은 답을 넣는다
            with store_lock:
                p = load_project(d["id"])
                step, text = d["step"], d["text"]
                cfg = load_config()
                if not text.strip():
                    raise ValueError("붙여 넣은 글이 없어요. AI의 답을 복사해 넣어 주세요.")
                if prompts.is_prompt_echo(text, prompt_for(p, step, cfg)):
                    raise ValueError("붙여 넣은 글이 지시문이에요. 지시문을 AI 채팅에 보낸 뒤, AI가 돌려준 답을 복사해 넣어 주세요.")
                if step == "script":
                    pipeline.set_script(p, text, cfg)
                    first = p["script"].split("\n", 1)[0].strip()
                    summary = f"대본 {len(p['script']):,}자를 넣었어요. 첫 줄이 맞는지 확인해 주세요: “{first[:30]}{'…' if len(first) > 30 else ''}”"
                elif step == "factcheck":
                    pipeline.set_factcheck(p, text)
                    summary = f"확인할 곳 {len(p['factcheck'] or [])}개를 넣었어요"
                elif step.startswith("translate:"):
                    p["translations"][step.split(":")[1]] = prompts.parse_script(text)
                    summary = "번역을 넣었어요"
                elif step == "storyboard":
                    pipeline.set_storyboard(p, text)
                    summary = f"장면 {len(p['storyboard']['scenes'])}개로 나눴어요"
                elif step == "meta":
                    pipeline.set_meta(p, text)
                    summary = f"제목 후보 {len(p['meta']['titles'])}개, 태그 {len(p['meta']['tags'])}개를 넣었어요"
                else:
                    raise ValueError("알 수 없는 단계")
                save_project(p)
            return self.send(HTTPStatus.OK, {"ok": True, "summary": summary})
        if path == "/api/thumb_font":
            # 썸네일 편집기가 영상 자막과 같은 글꼴을 쓰도록 프로젝트 글꼴 경로를 알려 준다
            with store_lock:
                p = load_project(d["id"])
                rel = pipeline._ensure_font(p, pdir(p["id"]))
                save_project(p)
            return self.send(HTTPStatus.OK, {"font": rel, "family": p["sub"].get("font_family") or ""})
        if path == "/api/run":
            pdir(d["id"])
            start_job(d["id"], d["step"], d.get("opts") or {})
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/cancel":
            cancel_flag[0] = True
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/voice_preview":
            cfg = load_config()
            PREVIEWS.mkdir(parents=True, exist_ok=True)
            prov, vid = d["provider"], d["id"]
            text = d.get("text") or "안녕하세요. 오늘은 서울 집값이 86주 연속 오른 이야기를 해 볼게요."
            from textutil import tts_text
            spoken = tts_text(text, d.get("dictionary", ""), True, auto_pause=d.get("auto_pause", True) and prov == "azure")
            name = re.sub(r"[^\w]", "_", f"{prov}_{vid}")[:60] + f"_{int(time.time())}"
            if prov == "sapi":
                ai.synth_sapi_batch([(spoken, PREVIEWS / f"{name}.wav")], vid, float(d.get("speed") or 1))
                f = PREVIEWS / f"{name}.wav"
            elif prov == "silent":
                raise ValueError("테스트용 무음은 미리 들을 수 없습니다.")
            else:
                f = ai.synth_one(cfg, prov, vid, spoken, PREVIEWS / name, float(d.get("speed") or 1), d.get("style", ""))
            for old in sorted(PREVIEWS.iterdir())[:-20]:
                old.unlink(missing_ok=True)
            return self.send(HTTPStatus.OK, {"url": f"/files/_preview/{f.name}"})
        if path == "/api/scene":
            # 장면 편집: 프롬프트 수정, 나누기, 합치기, 그림 지우기
            with store_lock:
                p = load_project(d["id"])
                scenes = p["storyboard"]["scenes"]
                i = int(d["index"])
                act = d["action"]
                if act == "update":
                    for k in ("visual", "prompt", "motion", "transition", "mood"):
                        if k in d:
                            scenes[i][k] = d[k]
                elif act == "clear_media":
                    scenes[i].pop(d.get("what", "image"), None)
                elif act == "merge_next" and i + 1 < len(scenes):
                    scenes[i]["last"] = scenes[i + 1]["last"]
                    scenes[i]["visual"] = (scenes[i].get("visual", "") + " / " + scenes[i + 1].get("visual", "")).strip(" /")
                    scenes.pop(i + 1)
                elif act == "split":
                    sc = scenes[i]
                    if sc["last"] > sc["first"]:
                        mid = (sc["first"] + sc["last"]) // 2
                        new = dict(sc, first=mid + 1)
                        new.pop("image", None)
                        new.pop("video", None)
                        sc["last"] = mid
                        scenes.insert(i + 1, new)
                save_project(p)
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/helper/start":
            d_ = Path(str(d.get("dir") or default_downloads())).expanduser()
            if not d_.is_dir():
                raise ValueError(f"폴더가 없습니다: {d_}")
            p = load_project(d["id"])
            if not p.get("storyboard"):
                raise ValueError("스토리보드를 먼저 만들어 주세요.")
            cfg = load_config()
            cfg["helper_dir"] = str(d_)
            save_config(cfg)
            with helper_lock:
                helper.update(on=True, pid=d["id"], dir=str(d_), since=time.time() - 2, added=[], error=None,
                              target=d.get("target") if d.get("target") is not None else next_empty(p))
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/helper/target":
            with helper_lock:
                helper["target"] = d.get("target")
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/helper/stop":
            with helper_lock:
                helper["on"] = False
            return self.send(HTTPStatus.OK, {"ok": True})
        if path == "/api/open_folder":
            target = pdir(d["id"]) / (d.get("sub") or "")
            if sys.platform == "win32":
                os.startfile(str(target))  # noqa: S606 - 사용자가 누른 '폴더 열기'
            return self.send(HTTPStatus.OK, {"ok": True, "path": str(target)})
        return self.send(HTTPStatus.NOT_FOUND, {"error": "not found"})


# ---------------- 시작 ----------------

def already_running():
    try:
        info = json.loads(RUNNING.read_text(encoding="utf-8"))
        req = urllib.request.Request(info["url"] + "api/ping", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=2) as res:
            r = json.load(res)
        if r.get("app") != APP_NAME:
            return False
        if r.get("version") == VERSION:
            webbrowser.open(info["url"])
            return True
        # 다른 버전이 켜져 있으면 끄고 새 버전으로 시작한다 (새 폴더를 풀어도 옛 버전이 계속 열리던 문제)
        q = urllib.request.Request(info["url"] + "api/quit", data=b"{}", method="POST",
                                   headers={"X-Token": info["token"], "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(q, timeout=3).read()
        except Exception:
            pass
        time.sleep(1.5)
        return False
    except Exception:
        pass
    return False


def main():
    if not os.environ.get("VIDEO_WORKSHOP_PORT") and already_running():
        return
    port = int(os.environ.get("VIDEO_WORKSHOP_PORT", "0"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    PROJECTS.mkdir(parents=True, exist_ok=True)
    RUNNING.write_text(json.dumps({"url": url, "token": TOKEN}), encoding="utf-8")
    print(f"{APP_NAME} {VERSION} 실행 중: {url}")
    print(f"데이터 폴더: {DATA_DIR}")
    if os.environ.get("VIDEO_WORKSHOP_NO_WINDOW"):
        print(f"토큰: {TOKEN}")
        server.serve_forever()
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webbrowser.open(url)
    last_ping[0] = time.time() + 30
    while True:
        time.sleep(5)
        with job_lock:
            busy = job["running"]
        if not busy and time.time() - last_ping[0] > IDLE_EXIT:
            break


if __name__ == "__main__":
    main()
