#!/usr/bin/env python3
"""황금 키워드 찾기 - 윈도우 데스크톱 프로그램

내 PC에서 작은 웹 서버(127.0.0.1)를 띄우고 화면을 브라우저 창으로 연다.
네이버 API 키와 수집한 키워드는 PC 안(%APPDATA%\\KeywordFinder)에만 저장된다.
"""
import csv
import datetime
import io
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# 콘솔 코드페이지가 한글을 못 쓰는 환경에서도 출력 때문에 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

# 윈도우용 포터블 파이썬은 스크립트 폴더를 import 경로에 넣지 않으므로 직접 넣는다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import naver  # noqa: E402

APP_NAME = "황금 키워드 찾기"
VERSION = "0.1.0"
# PyInstaller로 묶으면 리소스는 _MEIPASS 아래에 풀린다.
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("KEYWORD_FINDER_HOME") or (Path(os.environ.get("APPDATA", Path.home())) / "KeywordFinder"))
CONFIG = DATA_DIR / "config.json"
RUNNING = DATA_DIR / "running.json"  # 이미 실행 중인 프로그램의 주소 (두 번 실행 방지)
DB = DATA_DIR / "keywords.db"
KST = datetime.timezone(datetime.timedelta(hours=9))
KEY_FIELDS = ["ad_api_key", "ad_secret", "ad_customer_id", "client_id", "client_secret"]

TOKEN = secrets.token_urlsafe(24)  # 다른 웹사이트가 이 로컬 서버를 몰래 부르지 못하게 막는 값
last_ping = [time.time()]  # 화면이 살아 있다는 신호를 마지막으로 받은 시각
IDLE_EXIT = 180  # 화면(브라우저 탭)이 이 초 동안 신호가 없으면 프로그램 종료 (백그라운드 탭은 타이머가 느려지므로 넉넉히)
categorize = naver.Categorizer(BASE / "categories.txt")


# ---------------- 저장소 ----------------

def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS keywords (
        k TEXT PRIMARY KEY, cat TEXT, pc INTEGER, mo INTEGER, comp TEXT,
        docs INTEGER, docs_at TEXT, seed TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS history (
        k TEXT, day TEXT, monthly INTEGER, PRIMARY KEY (k, day))""")
    return con


def today():
    return datetime.datetime.now(KST).date().isoformat()


def change(series, days_back):
    """series: [(day, monthly)] 오름차순. days_back 일 전(이전 기록 중 가장 가까운 것)과 비교한 변화율."""
    if len(series) < 2:
        return None
    last_day = datetime.date.fromisoformat(series[-1][0])
    target = (last_day - datetime.timedelta(days=days_back)).isoformat()
    old = [m for d, m in series[:-1] if d <= target]
    if not old or not old[-1]:
        return None
    return round((series[-1][1] - old[-1]) / old[-1] * 100, 1)


def all_rows():
    con = db()
    hist = {}
    for k, day, monthly in con.execute("SELECT k, day, monthly FROM history ORDER BY day"):
        hist.setdefault(k, []).append((day, monthly))
    rows = []
    for k, cat, pc, mo, comp, docs, seed, updated in con.execute(
        "SELECT k, cat, pc, mo, comp, docs, seed, updated_at FROM keywords"
    ):
        monthly = pc + mo
        series = hist.get(k, [])
        rows.append({
            "k": k, "cat": cat, "pc": pc, "mo": mo, "monthly": monthly, "daily": round(monthly / 30),
            "docs": docs, "ratio": round(docs / max(monthly, 1), 3) if docs is not None else None,
            "golden": naver.golden_index(monthly, docs), "chg1": change(series, 1), "chg7": change(series, 7),
            "comp": comp, "seed": seed, "updated": updated,
        })
    con.close()
    return rows


# ---------------- 수집 작업 ----------------

job = {"running": False, "done": 0, "total": 0, "message": "대기 중", "error": None, "added": 0}
job_lock = threading.Lock()


def set_job(**kw):
    with job_lock:
        job.update(kw)


def run_collect(seeds, category, want_docs, min_monthly, max_docs):
    cfg = load_config()
    try:
        found = {}
        batches = [seeds[i:i + 5] for i in range(0, len(seeds), 5)]
        set_job(running=True, done=0, total=len(batches), message="연관 키워드 수집 중", error=None, added=0)
        for n, batch in enumerate(batches, 1):
            for r in naver.related_keywords(cfg, batch):
                if r["pc"] + r["mo"] >= min_monthly and r["k"] not in found:
                    r["seed"] = batch[0]
                    found[r["k"]] = r
            set_job(done=n, message=f"연관 키워드 수집 중 ({n}/{len(batches)}) · {len(found):,}개")
            time.sleep(0.4)

        now = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        day = today()
        con = db()
        for r in found.values():
            cat = category or categorize(r["k"], "기타")
            con.execute(
                """INSERT INTO keywords (k, cat, pc, mo, comp, seed, updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(k) DO UPDATE SET cat=excluded.cat, pc=excluded.pc, mo=excluded.mo,
                   comp=excluded.comp, seed=excluded.seed, updated_at=excluded.updated_at""",
                (r["k"], cat, r["pc"], r["mo"], r["comp"], r["seed"], now),
            )
            con.execute("INSERT OR REPLACE INTO history (k, day, monthly) VALUES (?,?,?)", (r["k"], day, r["pc"] + r["mo"]))
        con.commit()

        if want_docs and cfg.get("client_id") and cfg.get("client_secret"):
            # 오늘 아직 문서 수를 안 센 키워드부터, 검색량 많은 순으로
            fresh = {k for (k,) in con.execute("SELECT k FROM keywords WHERE docs_at = ?", (day,))}
            targets = sorted((k for k in found if k not in fresh), key=lambda k: -(found[k]["pc"] + found[k]["mo"]))[:max_docs]
            set_job(done=0, total=len(targets), message="블로그 문서 수 세는 중")
            for n, k in enumerate(targets, 1):
                try:
                    con.execute("UPDATE keywords SET docs=?, docs_at=? WHERE k=?", (naver.blog_total(cfg, k), day, k))
                except naver.ApiError as e:
                    if "HTTP 4" in str(e) or "권한" in str(e):
                        raise
                if n % 50 == 0:
                    con.commit()
                    set_job(done=n, message=f"블로그 문서 수 세는 중 ({n:,}/{len(targets):,})")
                time.sleep(0.11)  # 초당 10회 제한
            con.commit()
        con.close()
        set_job(running=False, message=f"완료: 키워드 {len(found):,}개 저장", added=len(found))
    except Exception as e:  # 화면에 그대로 보여 준다
        set_job(running=False, error=str(e), message="실패")


# ---------------- 웹 서버 ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "KeywordFinder"

    def log_message(self, *args):
        pass

    def send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def authed(self, query_token=None):
        return secrets.compare_digest(self.headers.get("X-Token") or query_token or "", TOKEN)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        if not self.allowed():
            return self.send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            html = (BASE / "ui" / "index.html").read_text(encoding="utf-8")
            html = html.replace("__TOKEN__", TOKEN).replace("__VERSION__", VERSION)
            return self.send(HTTPStatus.OK, html, "text/html; charset=utf-8")
        q = dict(p.split("=", 1) for p in url.query.split("&") if "=" in p)
        if not self.authed(q.get("token")):
            return self.send(HTTPStatus.FORBIDDEN, {"error": "token"})
        last_ping[0] = time.time()
        if url.path == "/api/ping":
            return self.send(HTTPStatus.OK, {"ok": True, "app": APP_NAME})
        if url.path == "/api/config":
            cfg = load_config()
            return self.send(HTTPStatus.OK, {
                "has_ad": all(cfg.get(f) for f in KEY_FIELDS[:3]),
                "has_search": all(cfg.get(f) for f in KEY_FIELDS[3:]),
                "masked": {f: (cfg[f][:4] + "…" + cfg[f][-2:]) if cfg.get(f) and len(str(cfg[f])) > 8 else ("●●●" if cfg.get(f) else "") for f in KEY_FIELDS},
                "data_dir": str(DATA_DIR),
                "categories": categorize.categories,
            })
        if url.path == "/api/job":
            with job_lock:
                return self.send(HTTPStatus.OK, dict(job))
        if url.path == "/api/keywords":
            return self.send(HTTPStatus.OK, {"rows": all_rows()})
        if url.path == "/api/export.csv":
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["키워드", "분류", "PC 월검색", "모바일 월검색", "월간 검색 합계", "일 평균", "블로그 문서", "문서/월검색", "황금지수", "전일 대비(%)", "7일 대비(%)", "광고 경쟁", "씨앗 키워드", "갱신"])
            for r in sorted(all_rows(), key=lambda r: (r["golden"] is None, -(r["golden"] or 0), -r["monthly"])):
                w.writerow([r["k"], r["cat"], r["pc"], r["mo"], r["monthly"], r["daily"], r["docs"], r["ratio"], r["golden"], r["chg1"], r["chg7"], r["comp"], r["seed"], r["updated"]])
            name = f"keywords_{today()}.csv"
            return self.send(HTTPStatus.OK, "﻿" + out.getvalue(), "text/csv; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="{name}"'})
        self.send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if not self.allowed() or not self.authed():
            return self.send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            data = self.body()
        except ValueError:
            return self.send(HTTPStatus.BAD_REQUEST, {"error": "잘못된 요청"})

        if path == "/api/quit":
            self.send(HTTPStatus.OK, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return

        if path == "/api/config":
            cfg = load_config()
            for f in KEY_FIELDS:
                v = str(data.get(f, "")).strip()
                if v:
                    cfg[f] = v
                elif data.get("clear_" + f):
                    cfg.pop(f, None)
            save_config(cfg)
            return self.send(HTTPStatus.OK, {"ok": True})

        if path == "/api/test":
            cfg, result = load_config(), {}
            if all(cfg.get(f) for f in KEY_FIELDS[:3]):
                try:
                    result["ad"] = f"정상 (예: 노트북 연관 키워드 {len(naver.related_keywords(cfg, ['노트북'])):,}개)"
                except naver.ApiError as e:
                    result["ad"] = f"실패: {e}"
            else:
                result["ad"] = "키가 비어 있습니다"
            if all(cfg.get(f) for f in KEY_FIELDS[3:]):
                try:
                    result["search"] = f"정상 (예: 노트북 블로그 문서 {naver.blog_total(cfg, '노트북'):,}개)"
                except naver.ApiError as e:
                    result["search"] = f"실패: {e}"
            else:
                result["search"] = "키가 비어 있습니다 (선택 사항)"
            return self.send(HTTPStatus.OK, result)

        if path == "/api/collect":
            seeds = [s.strip().replace(" ", "") for s in str(data.get("seeds", "")).replace(",", "\n").splitlines()]
            seeds = list(dict.fromkeys(s for s in seeds if s))
            if not seeds:
                return self.send(HTTPStatus.BAD_REQUEST, {"error": "씨앗 키워드를 한 개 이상 입력하세요."})
            if not all(load_config().get(f) for f in KEY_FIELDS[:3]):
                return self.send(HTTPStatus.BAD_REQUEST, {"error": "설정에서 검색광고 API 키 3개를 먼저 넣어 주세요."})
            with job_lock:
                if job["running"]:
                    return self.send(HTTPStatus.CONFLICT, {"error": "이미 수집 중입니다."})
                job["running"] = True
            threading.Thread(target=run_collect, daemon=True, args=(
                seeds, str(data.get("category") or ""), bool(data.get("docs", True)),
                int(data.get("min_monthly") or 0), int(data.get("max_docs") or 1000),
            )).start()
            return self.send(HTTPStatus.OK, {"ok": True})

        if path == "/api/delete":
            con = db()
            if data.get("all"):
                con.execute("DELETE FROM keywords")
                con.execute("DELETE FROM history")
            else:
                ks = [str(k) for k in data.get("keywords", [])]
                con.executemany("DELETE FROM keywords WHERE k=?", [(k,) for k in ks])
                con.executemany("DELETE FROM history WHERE k=?", [(k,) for k in ks])
            con.commit()
            con.close()
            return self.send(HTTPStatus.OK, {"ok": True})

        self.send(HTTPStatus.NOT_FOUND, {"error": "not found"})


def open_window(url):
    """pywebview 가 있으면 프로그램 창으로, 없으면 기본 브라우저로 연다."""
    try:
        import webview  # type: ignore
    except ImportError:
        webbrowser.open(url)
        return False
    webview.create_window(APP_NAME, url, width=1280, height=860, min_size=(900, 600))
    webview.start()
    return True


def already_running():
    """이미 켜져 있으면 그 화면을 다시 열고 True."""
    try:
        info = json.loads(RUNNING.read_text(encoding="utf-8"))
        req = urllib.request.Request(info["url"] + "api/ping", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=2) as res:
            if json.load(res).get("app") == APP_NAME:
                webbrowser.open(info["url"])
                return True
    except Exception:
        pass
    return False


def main():
    if not os.environ.get("KEYWORD_FINDER_PORT") and already_running():
        return
    port = int(os.environ.get("KEYWORD_FINDER_PORT", "0"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNNING.write_text(json.dumps({"url": url, "token": TOKEN}), encoding="utf-8")
    print(f"{APP_NAME} {VERSION} 실행 중: {url}")
    print(f"데이터 폴더: {DATA_DIR}")
    if os.environ.get("KEYWORD_FINDER_NO_WINDOW"):
        server.serve_forever()
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if open_window(url):
        return  # 프로그램 창을 닫으면 끝
    # 브라우저로 연 경우: 화면 신호가 끊기면(탭을 닫으면) 종료. 수집 중에는 기다린다.
    last_ping[0] = time.time() + 30  # 브라우저가 뜰 시간을 준다
    while True:
        time.sleep(5)
        with job_lock:
            busy = job["running"]
        if not busy and time.time() - last_ping[0] > IDLE_EXIT:
            break


if __name__ == "__main__":
    main()
