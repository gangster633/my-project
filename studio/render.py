"""FFmpeg 영상 엔진: 음성 다듬기, 화면 효과(켄번즈), 장면 전환, 자막(ASS), 배경음 덕킹, 로고, 썸네일.

필터에 윈도우 경로(C:\\...)를 넣으면 이스케이프가 까다로우므로, 항상 프로젝트 폴더를 작업 폴더(cwd)로 두고
상대 경로만 쓴다.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RenderError(Exception):
    pass


# ---------------- 실행 파일 ----------------

def tool(name):
    """프로그램 폴더의 tools/ffmpeg.exe 를 먼저, 없으면 PATH 에서 찾는다."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in (HERE.parent / "tools", HERE / "tools"):
        if (d / exe).exists():
            return str(d / exe)
    found = shutil.which(name)
    if not found:
        raise RenderError(f"{name} 을 찾지 못했습니다. 프로그램 폴더의 tools 안에 {exe} 가 있어야 합니다.")
    return found


_major = []


def ffmpeg_major():
    """ffmpeg 주 버전. 7 부터 -filter_complex_script 대신 -/filter_complex 를 쓴다 (8 이상에서는 옛 옵션이 없다)."""
    if not _major:
        try:
            out = subprocess.run([tool("ffmpeg"), "-hide_banner", "-version"], capture_output=True, text=True,
                                 creationflags=NOWIN).stdout
            m = re.match(r"ffmpeg version n?(\d+)\.", out)
            _major.append(int(m.group(1)) if m else 99)  # 개발판(N-xxxx)은 최신으로 본다
        except (OSError, RenderError):
            _major.append(99)
    return _major[0]


# ---------------- 영상 인코더 (그래픽카드 가속) ----------------

# 그래픽카드 인코더는 CPU(x264)보다 5~10배 빠르다. 실제로 1프레임을 만들어 보고 되는 것만 쓴다.
HW_ENCODERS = {
    "nvenc": ("NVIDIA 그래픽카드", ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"]),
    "qsv": ("인텔 그래픽", ["-c:v", "h264_qsv", "-preset", "faster", "-global_quality", "22", "-pix_fmt", "nv12"]),
    "amf": ("AMD 그래픽카드", ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22", "-pix_fmt", "yuv420p"]),
}
CPU_ENCODER = ("CPU", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"])
_hw = {}


def _hw_works(key):
    if key not in _hw:
        try:
            run(["-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=30:d=0.2"] + HW_ENCODERS[key][1] + ["-f", "null", "-"], timeout=30)
            _hw[key] = True
        except (RenderError, OSError, subprocess.SubprocessError):
            _hw[key] = False
    return _hw[key]


def pick_encoder(pref="auto"):
    """(이름, ffmpeg 인자, 키). pref: auto(되는 그래픽카드 → CPU), cpu, nvenc, qsv, amf."""
    if pref in HW_ENCODERS and _hw_works(pref):
        return HW_ENCODERS[pref][0], HW_ENCODERS[pref][1], pref
    if pref == "auto":
        for k in HW_ENCODERS:
            if _hw_works(k):
                return HW_ENCODERS[k][0], HW_ENCODERS[k][1], k
    return CPU_ENCODER[0], CPU_ENCODER[1], "cpu"


def ffmpeg_ok():
    try:
        tool("ffmpeg")
        tool("ffprobe")
        return True
    except RenderError:
        return False


def run(args, cwd=None, timeout=None, progress=None, total=None):
    """ffmpeg 실행. progress(초) 콜백이 있으면 진행 시간을 알려 준다."""
    cmd = [tool("ffmpeg"), "-hide_banner", "-y", "-loglevel", "error"]
    if progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(a) for a in args]
    if not progress:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NOWIN)
        if p.returncode != 0:
            raise RenderError("ffmpeg 오류: " + p.stderr.strip()[-1200:])
        return
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", creationflags=NOWIN)
    err = []
    import threading
    t = threading.Thread(target=lambda: err.extend(p.stderr), daemon=True)
    t.start()
    for line in p.stdout:
        if line.startswith("out_time_us="):
            try:
                progress(int(line.split("=")[1]) / 1e6)
            except ValueError:
                pass
    p.wait()
    t.join(2)
    if p.returncode != 0:
        raise RenderError("ffmpeg 오류: " + "".join(err).strip()[-1200:])


def duration(path):
    p = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, creationflags=NOWIN)
    try:
        return float(json.loads(p.stdout)["format"]["duration"])
    except (ValueError, KeyError):
        raise RenderError(f"길이를 읽지 못했습니다: {path}")


def media_info(path):
    p = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True, creationflags=NOWIN)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {}


# ---------------- 음성 ----------------

def clean_voice(src, dst, speed=1.0, trim=True, compress=None):
    """문장 음성을 44.1kHz 모노 wav 로. 앞뒤 무음 자르기, 긴 무음 줄이기, 속도 조절(atempo)."""
    f = []
    if trim:
        f.append("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.03")
        f += ["areverse", "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05", "areverse"]
    if compress:
        f.append(f"silenceremove=stop_periods=-1:stop_duration={compress['min']}:stop_threshold={compress['db']}dB:stop_silence={compress['keep']}")
    if abs(speed - 1.0) > 0.01:
        s = max(0.5, min(2.0, speed))
        f.append(f"atempo={s:.3f}")
    args = ["-i", src]
    if f:
        args += ["-af", ",".join(f)]
    args += ["-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", dst]
    run(args)


def silent_wav(dst, seconds):
    run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", f"{max(0.2, seconds):.3f}", "-c:a", "pcm_s16le", dst])


def concat_audio(parts, gaps, dst, workdir):
    """parts: wav 목록, gaps: 각 문장 뒤 쉼(초). 문장 시작·끝 시각 목록을 돌려준다."""
    workdir = Path(workdir)
    lst, t, times = [], 0.0, []
    sil_cache = {}
    for i, (p, g) in enumerate(zip(parts, gaps)):
        d = duration(p)
        times.append((round(t, 3), round(t + d, 3)))
        lst.append(Path(p).resolve().as_posix())
        t += d
        if g > 0:
            key = round(g, 2)
            if key not in sil_cache:
                sp = workdir / f"gap_{int(key * 1000)}.wav"
                silent_wav(sp, key)
                sil_cache[key] = sp.resolve().as_posix()
            lst.append(sil_cache[key])
            t += key
    listfile = workdir / "concat.txt"
    q = lambda x: x.replace("'", "'\\''")  # noqa: E731 - concat 목록의 작은따옴표 이스케이프
    listfile.write_text("".join(f"file '{q(x)}'\n" for x in lst), encoding="utf-8")
    run(["-f", "concat", "-safe", "0", "-i", listfile, "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1", dst])
    return times, round(t, 3)


# ---------------- 글꼴 ----------------

def font_family(path):
    """TTF/OTF/TTC 이름표에서 글꼴 이름(한국어 우선)을 읽는다."""
    try:
        data = Path(path).read_bytes()
        off = 0
        if data[:4] == b"ttcf":
            off = struct.unpack(">I", data[12:16])[0]
        num = struct.unpack(">H", data[off + 4:off + 6])[0]
        for i in range(num):
            rec = off + 12 + i * 16
            tag = data[rec:rec + 4]
            if tag == b"name":
                toff = struct.unpack(">I", data[rec + 8:rec + 12])[0]
                count, soff = struct.unpack(">HH", data[toff + 2:toff + 6])
                names = {}
                for j in range(count):
                    r = toff + 6 + j * 12
                    pid, eid, lid, nid, ln, so = struct.unpack(">HHHHHH", data[r:r + 12])
                    if nid not in (1, 16):
                        continue
                    raw = data[toff + soff + so: toff + soff + so + ln]
                    try:
                        s = raw.decode("utf-16-be") if pid in (0, 3) else raw.decode("latin-1")
                    except UnicodeDecodeError:
                        continue
                    names[(nid, lid)] = s
                for k in ((16, 0x0409), (1, 0x0409), (1, 0x0412), (16, 0x0412)):
                    if k in names:
                        return names[k]
                if names:
                    return next(iter(names.values()))
    except (OSError, struct.error, IndexError):
        pass
    return Path(path).stem


def system_fonts(user_dir=None):
    """자막에 쓸 수 있는 글꼴 [{file, family}]. 한국어 글꼴을 앞에."""
    dirs = []
    if user_dir:
        dirs.append(Path(user_dir))
    if sys.platform == "win32":
        dirs += [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path.home() / ".fonts"]
    ko_hint = re.compile(r"malgun|gulim|batang|dotum|gungsuh|nanum|noto.*(cjk|kr)|gmarket|pretendard|jalnan|black ?han|do ?hyeon|jua|spoqa|ibm ?plex ?sans ?kr|wqy|apple ?sd|cafe24|tmon|yeon|maru|s-core|one ?mobile|binggrae", re.I)
    out, seen = [], set()
    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            if f.suffix.lower() not in (".ttf", ".otf", ".ttc") or f.name.lower() in seen:
                continue
            if user_dir and d == Path(user_dir) or ko_hint.search(f.name):
                seen.add(f.name.lower())
                out.append({"file": str(f), "family": font_family(f), "user": bool(user_dir and d == Path(user_dir))})
    return out


def default_font():
    fonts = system_fonts()
    for pref in ("malgunbd", "malgun", "NanumGothicBold", "NotoSansCJK-Bold", "wqy-zenhei"):
        for f in fonts:
            if Path(f["file"]).stem.lower().startswith(pref.lower()):
                return f
    return fonts[0] if fonts else None


# ---------------- 그림 ----------------

def _dt_text(workdir, name, text):
    p = Path(workdir) / name
    # 글자는 drawtext 의 expansion=none 과 함께 쓴다. 기본값이면 "30%" 의 % 를 특수 기호(%{...})로 읽어
    # 줄이 사라지거나(FFmpeg 6) "Stray %" 오류가 난다(FFmpeg 7 이상).
    p.write_text(text, encoding="utf-8")
    return name


def placeholder_image(workdir, out_name, text, idx, w, h, font_rel):
    """키 없이 흐름을 확인하는 자리표시 그림 (장면 설명 글자를 넣은 그라데이션 카드)."""
    palettes = [("0x1e3a5f", "0x0f766e"), ("0x3b1f5c", "0x9d174d"), ("0x14532d", "0x0e7490"),
                ("0x7c2d12", "0x854d0e"), ("0x1e1b4b", "0x1d4ed8"), ("0x3f3f46", "0x0f766e")]
    a, b = palettes[idx % len(palettes)]
    wrapped = "\n".join(_wrap(text, 22 if w >= h else 12))[:200]
    tf = _dt_text(workdir, f"_ph_{idx}.txt", wrapped)
    num = _dt_text(workdir, f"_phn_{idx}.txt", f"장면 {idx + 1}")
    fs = int(h * 0.055)
    vf = (f"drawtext=expansion=none:fontfile={font_rel}:textfile={tf}:fontcolor=white:fontsize={fs}:line_spacing={fs // 3}:"
          f"x=(w-text_w)/2:y=(h-text_h)/2:box=0,"
          f"drawtext=expansion=none:fontfile={font_rel}:textfile={num}:fontcolor=white@0.6:fontsize={fs // 2}:x={fs // 2}:y={fs // 2}")
    run(["-f", "lavfi", "-i", f"gradients=s={w}x{h}:c0={a}:c1={b}:x0=0:y0=0:x1={w}:y1={h}:d=1",
         "-frames:v", "1", "-vf", vf, out_name], cwd=workdir)


def _wrap(text, n):
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + len(word) + 1 > n:
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        lines.append(cur)
    return lines[:6]


# ---------------- 장면 클립 ----------------

MOTIONS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down"]


# 화면 효과 방식이 바뀌면 올린다 (예전에 만든 장면 클립을 다시 만들게 하는 표시)
MOTION_VERSION = 2


def _perspective(motion, frames, strength=0.12):
    """켄번즈(줌·이동)를 perspective 필터로. zoompan 은 위치를 정수 픽셀로 반올림해 느린 움직임에서
    프레임마다 툭툭 흔들린다. perspective 는 픽셀 사이 위치까지 보간해서 매끄럽다(측정: 흔들림 약 1/8)."""
    N = max(frames - 1, 1)
    e = f"(0.5-0.5*cos(PI*min(in/{N},1)))"  # 부드럽게 시작·끝
    z1 = 1 + strength
    if motion == "zoom_in":
        z, fx, fy = f"(1+{strength}*{e})", "0.5", "0.5"
    elif motion == "zoom_out":
        z, fx, fy = f"({z1}-{strength}*{e})", "0.5", "0.5"
    elif motion == "pan_left":
        z, fx, fy = f"{z1}", f"(1-{e})", "0.5"
    elif motion == "pan_right":
        z, fx, fy = f"{z1}", e, "0.5"
    elif motion == "pan_up":
        z, fx, fy = f"{z1}", "0.5", f"(1-{e})"
    elif motion == "pan_down":
        z, fx, fy = f"{z1}", "0.5", e
    else:
        return None
    # 보여 줄 영역: 크기 W/z × H/z, 왼쪽 위 = (W - W/z)*fx, (H - H/z)*fy
    l, t = f"(W-W/{z})*{fx}", f"(H-H/{z})*{fy}"
    r, b = f"({l}+W/{z})", f"({t}+H/{z})"
    return (f"perspective=x0='{l}':y0='{t}':x1='{r}':y1='{t}':x2='{l}':y2='{b}':x3='{r}':y3='{b}'"
            f":interpolation=linear:sense=source:eval=frame")


def prepare_still(src, dst, w, h, vertical_blur, factor=1):
    """그림을 화면비에 맞춰 (factor 배) 크기로 준비. 세로 영상에 가로 그림이면 흐린 배경 + 가운데 배치."""
    W, H = w * factor, h * factor
    info = media_info(src)
    sw = sh = 0
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            sw, sh = s.get("width", 0), s.get("height", 0)
    if vertical_blur and sw and sh and (sw / sh) > (w / h) * 1.25:
        fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:2,eq=brightness=-0.15[bg];"
              f"[0:v]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
        run(["-i", src, "-filter_complex", fc, "-frames:v", "1", dst])
    else:
        run(["-i", src, "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1", "-frames:v", "1", dst])


def scene_clip(src, dst, dur, motion, w, h, fps=30, is_video=False, fill="freeze", vertical_blur=False, workdir=None, strength=0.12):
    """장면 하나를 dur 초짜리 무음 mp4 로."""
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps), "-an"]
    if is_video:
        src_d = duration(src)
        args = []
        if fill == "loop" and src_d < dur:
            args += ["-stream_loop", "-1"]
        args += ["-i", src]
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps}"
        if fill != "loop" and src_d < dur:
            vf += f",tpad=stop_mode=clone:stop_duration={dur - src_d + 0.5:.3f}"
        run(args + ["-t", f"{dur:.3f}", "-vf", vf] + enc + [dst])
        return
    frames = max(int(round(dur * fps)), 1)
    prep = Path(workdir or Path(dst).parent) / (Path(dst).stem + "_prep.png")
    prepare_still(src, prep, w, h, vertical_blur)
    eff = _perspective(motion, frames, strength)
    # 그림은 한 번만 읽어 loop 로 복제한다(-loop 1 은 프레임마다 PNG 를 다시 읽어 느리다).
    # 움직임 계산 전에 yuv420p 로 바꾸면 처리할 화소가 절반이라 더 빠르다. 결과 화면은 같다.
    vf = (f"format=yuv420p,loop=loop={frames - 1}:size=1:start=0,setpts=N/{fps}/TB,"
          + (eff + "," if eff else "") + "setsar=1")
    run(["-i", prep, "-vf", vf, "-frames:v", str(frames)] + enc + [dst])
    try:
        prep.unlink()
    except OSError:
        pass


# ---------------- 자막 ----------------

def _ass_color(hex_color, alpha=0):
    h = (hex_color or "#ffffff").lstrip("#")
    if len(h) != 6:
        h = "ffffff"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t):
    t = max(0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _text_units(line):
    """한 줄의 대략적인 너비(글자 크기 배수). 한글·기호는 1, 영문·숫자는 0.6, 띄어쓰기는 0.3."""
    u = 0.0
    for ch in line:
        u += 0.3 if ch == " " else (0.6 if ord(ch) < 128 else 1.0)
    return max(u, 1.0)


def _cta_size(cta, w, st):
    """쇼츠 끝 안내 문구 글자 크기(px). 원하는 크기(화면 폭 비율)로 하되, 가장 긴 줄이 화면 폭(띠 여백 포함)을 넘지 않게 줄인다."""
    want = float(st.get("cta_size", 0.13))
    if not cta:
        return int(w * want)
    return int(w * min(want, _cta_fit(cta)))


def _cta_fit(cta):
    longest = max(_text_units(l) for l in str(cta[0]).split("\n"))
    return 0.86 / longest  # 화면 폭의 86% 안에 들어가게 (띠 여백 몫을 뺀 값)


def _cta_scale_y(cta, st):
    """폭이 꽉 차서 더 키울 수 없으면 글자를 세로로 늘려 더 크게 보이게 한다(최대 1.4배)."""
    if not cta:
        return 100
    want, fit = float(st.get("cta_size", 0.13)), _cta_fit(cta)
    return int(min(max(want / fit, 1.0), 1.4) * 100)


def write_ass(path, cues, st, w, h, title=None, title_end=None, cta=None):
    """cues: [(start, end, text)]. st: 자막 스타일 설정. title: 쇼츠 위쪽에 고정할 제목.
    cta: (문구, 시작, 끝) 쇼츠 끝에 화면 가운데 아래쪽에 띄우는 안내 문구."""
    scale = h / 1080 if w >= h else w / 1080
    size = int(st.get("size", 17) * 3.2 * scale)  # 화면 높이 1080 기준 17 -> 약 54px
    pos = st.get("position", "bottom")
    align = {"bottom": 2, "center": 5, "top": 8}.get(pos, 2)
    margin_v = int(h * float(st.get("margin", 0.08))) if pos != "center" else 0
    box = st.get("box", True)
    back_alpha = int(255 * (1 - float(st.get("box_opacity", 0.6))))
    font = st.get("font_family") or "Malgun Gothic"
    fmt = (f"{size},{{primary}},&H000000FF,{{outline_c}},{{back_c}},{-1 if st.get('bold', True) else 0},0,0,0,100,100,0,"
           f"{float(st.get('angle', 0)):.1f},{{border}},{{outline}},0,{align},{int(w * 0.06)},{int(w * 0.06)},{margin_v},1")
    text_style = fmt.format(primary=_ass_color(st.get("color", "#ffffff")),
                            outline_c=_ass_color(st.get("outline_color", "#000000")),
                            back_c="&H80000000", border=1,
                            outline=f"{(0 if box else float(st.get('outline', 1.5)) * scale * 2):.1f}")
    # 띠(박스)는 따로 한 번만 그린다. 글자색을 바꿔도 박스에 이음매가 생기지 않게.
    box_style = fmt.format(primary="&HFF000000", outline_c=_ass_color(st.get("box_color", "#000000"), back_alpha),
                           back_c=_ass_color(st.get("box_color", "#000000"), back_alpha), border=3,
                           outline=f"{float(st.get('box_pad', 1.5)) * scale * 6:.1f}")
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Text,{font},{text_style}",
        f"Style: Box,{font},{box_style}",
        f"Style: Title,{font},{int(w * float(st.get('title_size', 0.075)))},{_ass_color(st.get('title_color', '#ffffff'))},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{w * 0.01:.1f},{w * 0.004:.1f},8,{int(w * 0.06)},{int(w * 0.06)},{int(h * 0.09)},1",
        f"Style: Cta,{font},{_cta_size(cta, w, st)},&H0000E6FF,&H000000FF,&H40000000,&H40000000,-1,0,0,0,100,{_cta_scale_y(cta, st)},0,0,3,{w * 0.03:.1f},0,5,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    accent = _ass_color(st.get("accent", "#ffd400"))
    base = _ass_color(st.get("color", "#ffffff"))
    if title:
        t = title.replace("{", "(").replace("}", ")").replace("\n", "\\N")
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(title_end or 3600)},Title,,0,0,0,,{t}")
    if cta:
        t = cta[0].replace("\\", "").replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N")
        lines.append(f"Dialogue: 3,{_ts(cta[1])},{_ts(cta[2])},Cta,,0,0,0,,{{\\pos({w // 2},{int(h * 0.5)})\\fad(350,0)}}{t}")
    for s, e, text in cues:
        t = text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", "\\N")
        if box:
            lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Box,,0,0,0,,{t}")
        if st.get("highlight_numbers", True):
            t = re.sub(r"(\d[\d,.]*\s?(?:%|퍼센트|[만억조천]?\s?원|주|년|배|명|개|위)?)", lambda m: "{\\c" + accent + "}" + m.group(1) + "{\\c" + base + "}", t)
        lines.append(f"Dialogue: 1,{_ts(s)},{_ts(e)},Text,,0,0,0,,{t}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def speed_up(src, dst, speed, fps=30, encoder=None, cwd=None):
    """완성된 영상을 speed 배로 빠르게 (목소리 높이는 그대로). 쇼츠용."""
    speed = float(speed)
    fc = f"[0:v]setpts=PTS/{speed:.4f}[v];[0:a]atempo={speed:.4f}[a]"
    head = ["-i", src, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    tail = ["-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst]
    venc = list(encoder or CPU_ENCODER[1])
    try:
        run(head + venc + tail, cwd=cwd)
    except RenderError:
        if venc == CPU_ENCODER[1]:
            raise
        run(head + CPU_ENCODER[1] + tail, cwd=cwd)


def write_srt(path, cues):
    def ts(t):
        ms = int(round(max(0, t) * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    Path(path).write_text("".join(f"{i}\n{ts(s)} --> {ts(e)}\n{t}\n\n" for i, (s, e, t) in enumerate(cues, 1)), encoding="utf-8-sig")


# ---------------- 최종 합성 ----------------

def final_render(workdir, clips, scene_times, transitions, narration, out, w, h, fps=30, trans_dur=0.5,
                 ass=None, fontsdir=None, logo=None, logo_cfg=None, bgm=None, bgm_cfg=None, total=None, progress=None,
                 encoder=None):
    """clips: 장면 클립 상대경로(작업 폴더 기준). scene_times: [(start, end)] 장면 구간.
    transitions[i]: i번째 장면으로 들어오는 전환 이름('cut' 포함). 클립 i 는 앞뒤로 T/2 씩 여유가 있어야 한다(cut 은 0)."""
    n = len(clips)
    args, fc = [], []
    for c in clips:
        args += ["-i", c]
    vidx = n
    # 영상 이어 붙이기
    if n == 1:
        fc.append("[0:v]null[vcat]")
    elif all(t == "cut" for t in transitions[1:]):
        fc.append("".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]")
    else:
        prev = "[0:v]"
        for i in range(1, n):
            t = transitions[i]
            d = trans_dur if t != "cut" else 0.04
            name = "fade" if t == "cut" else t
            off = scene_times[i][0] - d / 2
            label = f"[x{i}]" if i < n - 1 else "[vcat]"
            fc.append(f"{prev}[{i}:v]xfade=transition={name}:duration={d:.3f}:offset={off:.3f}{label}")
            prev = label
    vlast = "[vcat]"
    if ass:
        fd = f":fontsdir={fontsdir}" if fontsdir else ""
        fc.append(f"{vlast}ass={ass}{fd}[vsub]")
        vlast = "[vsub]"
    if logo:
        lc = logo_cfg or {}
        lw = int(w * float(lc.get("scale", 0.12)))
        op = float(lc.get("opacity", 0.9))
        x = f"(W-w)*{float(lc.get('x', 0.96)):.3f}"
        y = f"(H-h)*{float(lc.get('y', 0.04)):.3f}"
        args += ["-i", logo]
        fc.append(f"[{vidx}:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa={op:.2f}[lg]")
        fc.append(f"{vlast}[lg]overlay=x='{x}':y='{y}'[vlogo]")
        vlast = "[vlogo]"
        vidx += 1
    fc.append(f"{vlast}format=yuv420p[vout]")
    # 소리
    args += ["-i", narration]
    nidx = vidx
    vidx += 1
    if bgm:
        bc = bgm_cfg or {}
        args += ["-stream_loop", "-1", "-i", bgm]
        bidx = vidx
        vol = float(bc.get("volume", 0.25))
        fc.append(f"[{nidx}:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[nar][sc]")
        fc.append(f"[{bidx}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={vol:.2f}[bg0]")
        if bc.get("duck", True):
            fc.append("[bg0][sc]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=450:makeup=1[bg1]")
        else:
            fc.append("[sc]anullsink;[bg0]anull[bg1]")
        fade_st = max(0, (total or 0) - 3)
        fc.append(f"[bg1]afade=t=in:d=1.5,afade=t=out:st={fade_st:.2f}:d=3[bg2]")
        fc.append("[nar][bg2]amix=inputs=2:duration=first:normalize=0[amix]")
        fc.append("[amix]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        fc.append(f"[{nidx}:a]aformat=sample_rates=44100:channel_layouts=stereo,loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    script = Path(workdir) / "filter.txt"
    script.write_text(";\n".join(fc), encoding="utf-8")
    new_style = ffmpeg_major() >= 7
    args += (["-/filter_complex", "filter.txt"] if new_style else ["-filter_complex_script", "filter.txt"])
    venc = list(encoder or CPU_ENCODER[1])
    tail = ["-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    if total:
        tail += ["-t", f"{total:.3f}"]
    tail.append(out)
    head = args + ["-map", "[vout]", "-map", "[aout]"]
    try:
        run(head + venc + tail, cwd=workdir, progress=progress)
    except RenderError as e:
        if "Unrecognized option" in str(e) or "Option not found" in str(e):
            # 버전 판단이 틀렸을 때를 대비해 반대 방식으로 한 번 더 시도한다
            i = head.index("-/filter_complex" if new_style else "-filter_complex_script")
            head[i] = "-filter_complex_script" if new_style else "-/filter_complex"
            run(head + venc + tail, cwd=workdir, progress=progress)
        elif venc != CPU_ENCODER[1]:
            # 그래픽카드 인코더가 도중에 실패하면 CPU 로 다시 만든다
            run(head + CPU_ENCODER[1] + tail, cwd=workdir, progress=progress)
        else:
            raise


# ---------------- 썸네일 ----------------

THUMB_THEMES = [
    {"name": "노랑 강조", "color": "#FFE400", "border": "#000000", "box": None},
    {"name": "흰 글자 빨간 띠", "color": "#FFFFFF", "border": "#000000", "box": "#E11D48"},
    {"name": "흰 글자 검정 띠", "color": "#FFFFFF", "border": "#000000", "box": "#000000"},
]


def thumbnail(workdir, src_rel, out_rel, text, font_rel, theme, w=1280, h=720):
    lines = text.split("\n") if "\n" in text else _wrap(text, 9)
    lines = lines[:3]
    fs = int(h * (0.2 if len(lines) == 1 else 0.16 if len(lines) == 2 else 0.13))
    filters = [f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
               "eq=contrast=1.08:saturation=1.15", "drawbox=x=0:y=ih*0.45:w=iw:h=ih*0.55:color=black@0.35:t=fill"]
    total_h = len(lines) * fs * 1.15
    for i, line in enumerate(lines):
        tf = _dt_text(workdir, f"_th_{i}.txt", line)
        y = f"h-{total_h + h * 0.07:.0f}+{i * fs * 1.15:.0f}"
        box = f":box=1:boxcolor={theme['box']}@0.95:boxborderw={int(fs * 0.18)}" if theme.get("box") else ""
        filters.append(f"drawtext=expansion=none:fontfile={font_rel}:textfile={tf}:fontsize={fs}:fontcolor={theme['color']}:"
                       f"borderw={max(3, fs // 14)}:bordercolor={theme['border']}:x=(w-text_w)/2:y={y}{box}")
    run(["-i", src_rel, "-vf", ",".join(filters), "-frames:v", "1", "-q:v", "2", out_rel], cwd=workdir)
