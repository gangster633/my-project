"""제작 단계별 작업. 각 함수는 project(dict)와 그 폴더를 받아 결과를 project 에 기록한다.

단계: 기사 가져오기 -> 대본 -> 점검/사실 대조 -> 번역 -> 음성 -> 스토리보드 -> 이미지 -> 영상 -> 쇼츠 -> 썸네일 -> 업로드 정보
"""
import concurrent.futures as cf
import hashlib
import html
import json
import os
import random
import re
import shutil
import time
import urllib.request
import zipfile
from pathlib import Path

import ai
import prompts
import render
import textutil


class Cancelled(Exception):
    pass


class Ctx:
    """진행 상황 보고와 취소 확인."""

    def __init__(self, report=None, cancelled=None):
        self.report = report or (lambda **kw: None)
        self.cancelled = cancelled or (lambda: False)

    def check(self):
        if self.cancelled():
            raise Cancelled()


# ---------------- 기본값 ----------------

DEFAULT_VOICE = {"provider": "", "id": "", "name": "", "speed": 1.0, "style": "", "trim": True,
                 "compress": True, "min_silence": 0.5, "keep_silence": 0.3, "silence_db": -50,
                 "gap": 0.25, "para_gap": 0.6, "dictionary": "", "numbers": True}
DEFAULT_SUB = {"font_file": "", "font_family": "", "size": 17, "color": "#ffffff", "accent": "#ffd400",
               "highlight_numbers": True, "box": True, "box_color": "#000000", "box_opacity": 0.6, "box_pad": 1.5,
               "outline": 1.5, "outline_color": "#000000", "bold": True, "position": "bottom", "margin": 0.08,
               "angle": 0, "max_chars": 25}
DEFAULT_VIDEO = {"resolution": "1920x1080", "fps": 30, "effect": "ai", "strength": 0.12, "transition": "ai",
                 "trans_dur": 0.5, "subtitles": True, "fill": "freeze", "logo": "", "logo_scale": 0.12, "logo_x": 0.96,
                 "logo_y": 0.04, "logo_opacity": 0.9, "bgm": "", "bgm_volume": 0.22, "bgm_duck": True}


def new_project(name, category="경제"):
    pid = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(str(time.time()).encode()).hexdigest()[:4]
    return {
        "id": pid, "name": name or "새 영상", "category": category, "created": time.strftime("%Y-%m-%d %H:%M"),
        "topic": "", "source_url": "", "source_text": "", "minutes": 10, "chars": 0, "tone_note": "", "cta": "end",
        "script": "", "translations": {}, "checks": [], "factcheck": None,
        "voice": dict(DEFAULT_VOICE), "audio": None, "audio_lang": {},
        "style": {"id": "webtoon", "custom": "", "character": "", "scene_seconds": 8},
        "storyboard": None, "image_provider": "", "video": dict(DEFAULT_VIDEO), "sub": dict(DEFAULT_SUB),
        "outputs": {}, "meta": None, "langs": [],
    }


def upgrade(p):
    """예전 버전 프로젝트에 새 항목을 채운다."""
    for k, v in new_project("").items():
        p.setdefault(k, v)
    for key, default in (("voice", DEFAULT_VOICE), ("sub", DEFAULT_SUB), ("video", DEFAULT_VIDEO)):
        for k, v in default.items():
            p[key].setdefault(k, v)
    return p


# ---------------- 1. 기사 가져오기 ----------------

def fetch_article(url):
    """뉴스 기사 주소에서 제목과 본문을 뽑는다 (네이버 뉴스 등 일반 기사 페이지)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                             "(KHTML, like Gecko) Chrome/130 Safari/537.36",
                                               "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as res:
        raw = res.read()
        charset = res.headers.get_content_charset() or ""
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000])
        charset = m.group(1).decode() if m else "utf-8"
    page = raw.decode(charset, "replace")
    title = ""
    for pat in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, page, re.S | re.I)
        if m:
            title = html.unescape(m.group(1)).strip()
            break
    body = ""
    for pat in (r'<article[^>]*id=["\']dic_area["\'][^>]*>(.*?)</article>',  # 네이버 뉴스
                r'<div[^>]+id=["\']dic_area["\'][^>]*>(.*?)</div>\s*</div>',
                r"<article[^>]*>(.*?)</article>",
                r'<div[^>]+(?:id|class)=["\'][^"\']*(?:article_body|articleBody|article-body|news_body|newsct_article|content_area)[^"\']*["\'][^>]*>(.*?)</div>'):
        m = re.search(pat, page, re.S | re.I)
        if m and len(_strip(m.group(1))) > 200:
            body = _strip(m.group(1))
            break
    if not body:  # 마지막 수단: 긴 <p> 문단 모으기
        paras = [_strip(x) for x in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S | re.I)]
        body = "\n".join(x for x in paras if len(x) > 40)
    if not body:
        m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']+)', page, re.I)
        body = html.unescape(m.group(1)) if m else ""
    return {"title": title, "text": body.strip()[:30000]}


def _strip(fragment):
    fragment = re.sub(r"<(script|style|figure|figcaption|em class=\"img_desc\")[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>|</p>|</div>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# ---------------- 2. 대본 ----------------

def chars_for(p, cfg):
    """목표 글자 수: 직접 입력 > 선택한 음성으로 실제 잰 1분당 글자 수 > 기본 450자/분."""
    if int(p.get("chars") or 0) > 0:
        return int(p["chars"])
    cpm = (cfg.get("voice_cpm") or {}).get(voice_key(p["voice"])) or cfg.get("default_cpm") or 450
    return int(float(p.get("minutes") or 10) * cpm)


def voice_key(v):
    return f"{v.get('provider')}:{v.get('id')}:{float(v.get('speed') or 1):.2f}"


def script_prompt(p, cfg):
    return prompts.build_script(p.get("topic") or p.get("name"), p.get("source_text", ""), p.get("minutes", 10),
                                chars_for(p, cfg), p.get("category", ""), p.get("tone_note", ""), "ko", p.get("cta", "end"))


def set_script(p, text, cfg):
    p["script"] = prompts.parse_script(text)
    p["checks"] = textutil.check_script(p["script"], chars_for(p, cfg))
    p["factcheck"] = None


def run_script(p, cfg, ctx):
    if not (p.get("topic") or p.get("source_text")):
        raise ai.AiError("주제를 쓰거나 기사 링크를 가져와 주세요.")
    ctx.report(message="대본 쓰는 중 (몇 분 걸릴 수 있어요)", done=0, total=1)
    set_script(p, ai.generate_text(cfg, script_prompt(p, cfg)), cfg)


def factcheck_prompt(p):
    return prompts.build_factcheck(p["script"], p.get("source_text", ""))


def set_factcheck(p, text):
    data = prompts.extract_json(text) if isinstance(text, str) else text
    p["factcheck"] = data.get("issues", []) if isinstance(data, dict) else data


def run_factcheck(p, cfg, ctx):
    ctx.report(message="자료와 대본 대조 중", done=0, total=1)
    set_factcheck(p, ai.generate_text(cfg, factcheck_prompt(p), schema=prompts.FACT_SCHEMA, max_tokens=16000))


def apply_factcheck(p):
    """사실 대조에서 제안한 고친 문장을 대본에 반영하고, 반영한 개수를 돌려준다."""
    n, left = 0, []
    for f in p.get("factcheck") or []:
        q, fix = (f.get("quote") or "").strip(), (f.get("fix") or "").strip()
        if q and fix and q in p["script"]:
            p["script"] = p["script"].replace(q, fix, 1)
            n += 1
        else:
            left.append(f)
    p["factcheck"] = left
    return n


def run_translate(p, cfg, ctx, lang):
    ctx.report(message=f"{prompts.LANG_NAME.get(lang, lang)} 번역 중", done=0, total=1)
    p["translations"][lang] = prompts.parse_script(ai.generate_text(cfg, prompts.build_translate(p["script"], lang)))


# ---------------- 3. 음성 ----------------

def script_for(p, lang):
    return p["script"] if lang == "ko" else p["translations"].get(lang, "")


def run_voice(p, cfg, ctx, folder, lang="ko", voice=None):
    v = dict(p["voice"]) if voice is None else dict(DEFAULT_VOICE, **voice)
    if not v.get("provider"):
        raise ai.AiError("음성을 먼저 골라 주세요.")
    script = script_for(p, lang)
    if not script.strip():
        raise ai.AiError("대본이 비어 있습니다." if lang == "ko" else "먼저 번역을 만들어 주세요.")
    sents = textutil.split_sentences(script)
    adir = Path(folder) / ("audio" if lang == "ko" else f"audio_{lang}")
    raw = adir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    speed = float(v.get("speed") or 1)
    numbers = bool(v.get("numbers", True)) and lang == "ko"
    for s in sents:
        s["tts"] = textutil.tts_text(s["text"], v.get("dictionary", ""), numbers)
    # 같은 문장·같은 음성이면 다시 만들지 않는다 (대본 일부만 고쳐도 빠르게)
    vk = voice_key(v) + "|" + (v.get("style") or "")
    jobs = []
    for i, s in enumerate(sents):
        h = hashlib.sha1((vk + "|" + s["tts"]).encode("utf-8")).hexdigest()[:16]
        s["hash"] = h
        existing = list(raw.glob(h + ".*"))
        if existing:
            s["raw"] = existing[0].name
        else:
            jobs.append(i)
    total = len(sents)
    ctx.report(message=f"음성 합성 중 (0/{total})", done=total - len(jobs), total=total)
    prov = v["provider"]
    if prov == "sapi":
        items = [(sents[i]["tts"], raw / (sents[i]["hash"] + ".wav")) for i in jobs]
        for k in range(0, len(items), 40):
            ctx.check()
            ai.synth_sapi_batch(items[k:k + 40], v["id"], speed)
            for i in jobs[k:k + 40]:
                sents[i]["raw"] = sents[i]["hash"] + ".wav"
            ctx.report(message=f"음성 합성 중 ({min(k + 40, len(items)) + total - len(jobs)}/{total})", done=min(k + 40, len(items)) + total - len(jobs))
    elif prov == "silent":
        for i in jobs:
            out = raw / (sents[i]["hash"] + ".wav")
            render.silent_wav(out, len(sents[i]["tts"].replace(" ", "")) / 7.5 / speed)
            sents[i]["raw"] = out.name
    else:
        done = [total - len(jobs)]
        workers = int(cfg.get("tts_workers") or 4)
        if prov == "azure":  # 무료 등급(F0)은 동시 요청이 적어 2개로 제한
            workers = min(workers, 2)

        def work(i):
            ctx.check()
            f = ai.synth_one(cfg, prov, v["id"], sents[i]["tts"], raw / sents[i]["hash"], speed, v.get("style", ""))
            sents[i]["raw"] = f.name
            done[0] += 1
            ctx.report(message=f"음성 합성 중 ({done[0]}/{total})", done=done[0])

        with cf.ThreadPoolExecutor(workers) as ex:
            list(ex.map(work, jobs))
    ctx.check()
    # 다듬기: 무음 자르기·압축, 속도(원래 속도 조절이 안 되는 음성만)
    ctx.report(message="음성 다듬는 중", done=0, total=total)
    clean = adir / "clean"
    if clean.exists():
        shutil.rmtree(clean)
    clean.mkdir()
    post_speed = speed if prov in ("gemini", "silent") else 1.0
    if prov == "silent":
        post_speed = 1.0
    compress = {"min": v["min_silence"], "keep": v["keep_silence"], "db": v["silence_db"]} if v.get("compress") else None
    parts, gaps = [], []
    for i, s in enumerate(sents):
        ctx.check()
        out = clean / f"{i:04d}.wav"
        render.clean_voice(raw / s["raw"], out, post_speed, trim=v.get("trim", True) and prov != "silent", compress=compress if prov != "silent" else None)
        parts.append(out)
        gaps.append(float(v["para_gap"]) if s["para_end"] else float(v["gap"]))
        if i % 10 == 0:
            ctx.report(done=i)
    gaps[-1] = 0.8
    times, total_sec = render.concat_audio(parts, gaps, adir / "narration.wav", adir)
    for s, (a, b) in zip(sents, times):
        s["start"], s["end"] = a, b
    audio = {"sentences": [{k: s[k] for k in ("text", "tts", "start", "end", "para_end", "section")} for s in sents],
             "narration": str((adir / "narration.wav").relative_to(folder).as_posix()), "total": total_sec,
             "voice": v, "made": time.strftime("%Y-%m-%d %H:%M"), "script_hash": hashlib.sha1(script.encode()).hexdigest()}
    if lang == "ko":
        p["audio"] = audio
        if prov != "silent":  # 이 음성의 실제 1분당 글자 수를 기억해 다음 대본 분량 계산에 쓴다
            chars = sum(len(s["text"].replace(" ", "")) for s in sents)
            cfg.setdefault("voice_cpm", {})[voice_key(v)] = round(chars / max(total_sec / 60, 0.1))
    else:
        p["audio_lang"][lang] = audio
    return audio


def length_warning(p):
    a = p.get("audio")
    if not a:
        return ""
    want = float(p.get("minutes") or 0) * 60
    got = a["total"]
    if want and abs(got - want) / want > 0.1:
        return f"요청 {want / 60:.0f}분, 실제 {got / 60:.1f}분입니다. 말하기 속도나 대본 분량으로 맞출 수 있어요."
    return ""


# ---------------- 4. 스토리보드 ----------------

def style_prompt(p):
    st = p["style"]
    base = prompts.STYLE_MAP.get(st.get("id"), prompts.STYLES[0])[4]
    return (st.get("custom") or "").strip() or base


# 사진·3D처럼 보여도 되는 화풍. 나머지(웹툰·수채·플랫 등)는 사진처럼 나오지 않게 막는다.
PHOTO_STYLES = {"cinematic", "news_photo", "pixar3d", "miniature", "cyberpunk"}
ILLUST_GUARD = "2D hand-drawn illustration, NOT a photograph, not photorealistic, no 3D render"
# 그림 화풍인데 장면 설명에 섞이면 사진 쪽으로 끌려가는 촬영 용어
_PHOTO_WORDS = re.compile(r",?\s*\b(shallow depth of field|depth of field|bokeh|photorealistic|photo-?realistic|photograph(?:ic|y)?|"
                          r"photo|DSLR|\d+\s?mm( lens)?|lens flare|cinematic lighting|film still|hyper-?realistic|realistic)\b", re.I)


def is_illustration(p):
    st = p["style"]
    return not (st.get("custom") or "").strip() and st.get("id") not in PHOTO_STYLES


def scene_text(p, sc):
    """장면 그림 설명. 그림 화풍이면 사진 촬영 용어를 뺀다."""
    t = sc.get("prompt") or sc.get("visual", "")
    if is_illustration(p):
        t = re.sub(r"\s{2,}", " ", _PHOTO_WORDS.sub("", t)).strip(" ,")
    return t


def storyboard_prompt(p):
    sents = [s["text"] for s in (p["audio"]["sentences"] if p.get("audio") else textutil.split_sentences(p["script"]))]
    total = p["audio"]["total"] if p.get("audio") else len("".join(sents)) / 7.5
    n = max(3, round(total / float(p["style"].get("scene_seconds") or 8)))
    return prompts.build_storyboard(sents, n, style_prompt(p), p["style"].get("character", ""))


def set_storyboard(p, text):
    data = prompts.extract_json(text) if isinstance(text, str) else text
    n = len(p["audio"]["sentences"]) if p.get("audio") else len(textutil.split_sentences(p.get("script") or ""))
    if not n:
        raise ValueError("대본이 비어 있어요. 대본을 먼저 저장해 주세요.")
    sb = prompts.fix_storyboard(data, n)
    old = {(s["first"], s["last"]): s for s in (p.get("storyboard") or {}).get("scenes", [])}
    for s in sb["scenes"]:  # 같은 범위 장면의 그림은 이어서 쓴다
        o = old.get((s["first"], s["last"]))
        if o:
            for k in ("image", "video", "image_prompt_used"):
                if o.get(k):
                    s[k] = o[k]
    if not p["style"].get("character") and sb.get("character"):
        p["style"]["character"] = sb["character"]
    p["storyboard"] = sb


def run_storyboard(p, cfg, ctx):
    if not p.get("script"):
        raise ai.AiError("대본을 먼저 만들어 주세요.")
    ctx.report(message="장면 나누고 그림 설명 쓰는 중", done=0, total=1)
    set_storyboard(p, ai.generate_text(cfg, storyboard_prompt(p), schema=prompts.STORY_SCHEMA))


def even_storyboard(p):
    """AI 없이: 문장을 시간 기준으로 고르게 묶는다."""
    sents = p["audio"]["sentences"] if p.get("audio") else [dict(s, start=i * 4, end=i * 4 + 4) for i, s in enumerate(textutil.split_sentences(p["script"]))]
    target = float(p["style"].get("scene_seconds") or 8)
    scenes, first, t0 = [], 0, sents[0]["start"] if sents else 0
    for i, s in enumerate(sents):
        if s["end"] - t0 >= target or i == len(sents) - 1 or s.get("para_end") and s["end"] - t0 >= target * 0.6:
            text = " ".join(x["text"] for x in sents[first:i + 1])
            scenes.append({"first": first, "last": i, "visual": text[:80], "prompt": text[:300], "mood": "calm",
                           "motion": random.Random(i).choice(render.MOTIONS), "transition": "fade"})
            first, t0 = i + 1, s["end"]
    set_storyboard(p, {"character": p["style"].get("character", ""), "scenes": scenes})


def scene_times(p, lang="ko"):
    """장면별 (시작, 끝) 초. 다른 언어는 같은 장면을 그 언어 길이에 비례해 배치하고 가까운 문장 경계에 맞춘다."""
    a = p["audio"]
    sents = a["sentences"]
    scenes = p["storyboard"]["scenes"]
    starts = [0.0] + [sents[s["first"]]["start"] for s in scenes[1:]]
    ends = starts[1:] + [a["total"]]
    if lang == "ko":
        return list(zip(starts, ends))
    b = p["audio_lang"][lang]
    ratio = b["total"] / a["total"]
    bounds = [x["start"] for x in b["sentences"]]
    out_starts = [0.0]
    for st in starts[1:]:
        t = st * ratio
        snap = min(bounds, key=lambda x: abs(x - t)) if bounds else t
        out_starts.append(max(snap, out_starts[-1] + 1.0))
    out_ends = out_starts[1:] + [b["total"]]
    return list(zip(out_starts, out_ends))


# ---------------- 5. 이미지 ----------------

def image_prompt(p, scene):
    char = p["style"].get("character", "").strip()
    parts = [scene_text(p, scene)]
    if char:
        parts.append(f"Recurring character (keep identical): {char}")
    parts.append(f"Art style: {style_prompt(p)}" + (f", {ILLUST_GUARD}" if is_illustration(p) else ""))
    if p["style"].get("allow_text"):
        parts.append("Any visible signs, labels or handwriting must be short, correct Korean (Hangul). No captions, no watermark, no logo.")
    else:
        parts.append("No text, no letters, no captions, no watermark, no logo.")
    return ". ".join(x.strip().rstrip(".") for x in parts if x.strip()) + "."


STYLE_DIR = None  # studio.py 가 정한다 (%APPDATA%\\VideoWorkshop\\style_examples)


def style_example(style_id):
    if not STYLE_DIR or not style_id:
        return None
    found = sorted(Path(STYLE_DIR).glob(f"{style_id}.*"))
    return found[0] if found else None


def run_style_examples(cfg, ctx, ids, provider=None):
    prov = provider or cfg.get("image_provider") or ("gemini" if cfg.get("gemini_key") else "openai" if cfg.get("openai_key") else "")
    if prov not in ("gemini", "openai"):
        raise ai.AiError("예시 그림을 자동으로 만들려면 Gemini나 OpenAI 키가 필요해요. 키가 없으면 카드에 그림을 끌어다 놓으세요.")
    Path(STYLE_DIR).mkdir(parents=True, exist_ok=True)
    errors = []
    for n, sid in enumerate(ids, 1):
        ctx.check()
        ctx.report(message=f"화풍 예시 그림 만드는 중 ({n}/{len(ids)})", done=n - 1, total=len(ids))
        try:
            tmp = ai.generate_image(cfg, prov, prompts.style_sample_prompt(sid), Path(STYLE_DIR) / f"_new_{sid}", "16:9")
            for old in Path(STYLE_DIR).glob(f"{sid}.*"):
                old.unlink()
            tmp.rename(Path(STYLE_DIR) / f"{sid}{tmp.suffix}")
        except ai.AiError as e:
            errors.append(f"{prompts.STYLE_MAP[sid][2]}: {e}")
    if errors:
        raise ai.AiError(f"{len(errors)}개 실패 (나머지는 저장됨). " + " / ".join(errors[:2]))


def run_images(p, cfg, ctx, folder, only=None, provider=None, overwrite=False):
    prov = (provider or p.get("image_provider") or cfg.get("image_provider")
            or ("gemini" if cfg.get("gemini_key") else "openai" if cfg.get("openai_key") else "placeholder"))
    scenes = p["storyboard"]["scenes"]
    idxs = [i for i in (only if only is not None else range(len(scenes))) if overwrite or only is not None or not scenes[i].get("image")]
    idir = Path(folder) / "images"
    idir.mkdir(exist_ok=True)
    W, H = _res(p)
    aspect = "16:9" if W >= H else "9:16"
    done, errors = [0], []
    ctx.report(message=f"이미지 만드는 중 (0/{len(idxs)})", done=0, total=len(idxs))
    font_rel = _ensure_font(p, folder) if prov == "placeholder" else None
    # 고른 화풍의 예시 그림을 기준으로 보내 그림체를 맞춘다 (직접 입력 화풍이면 쓰지 않음)
    ref = style_example(p["style"].get("id")) if p["style"].get("use_ref", True) and not (p["style"].get("custom") or "").strip() else None

    def work(i):
        ctx.check()
        sc = scenes[i]
        base = idir / f"scene_{i:03d}_{int(time.time() * 1000) % 100000}"
        try:
            if prov == "placeholder":
                name = base.name + ".png"
                render.placeholder_image(folder, f"images/{name}", sc.get("visual") or sc.get("prompt", ""), i, W // 2, H // 2, font_rel)
                f = idir / name
            else:
                f = ai.generate_image(cfg, prov, image_prompt(p, sc), base, aspect, ref_image=ref)
            old = sc.get("image")
            sc["image"] = f"images/{f.name}"
            sc["image_prompt_used"] = image_prompt(p, sc)
            if old and old != sc["image"]:
                (Path(folder) / old).unlink(missing_ok=True)
        except (ai.AiError, render.RenderError) as e:
            errors.append(f"장면 {i + 1}: {e}")
        done[0] += 1
        ctx.report(message=f"이미지 만드는 중 ({done[0]}/{len(idxs)})", done=done[0])

    workers = 1 if prov == "placeholder" else int(cfg.get("image_workers") or 3)
    with cf.ThreadPoolExecutor(workers) as ex:
        list(ex.map(work, idxs))
    if errors:
        raise ai.AiError(f"{len(errors)}개 실패 (나머지는 저장됨). " + " / ".join(errors[:3]))


def _aspect_word(p):
    w, h = _res(p)
    return "Wide 16:9 landscape image." if w >= h else "Tall 9:16 portrait image."


def all_prompts_text(p):
    lines = []
    for i, s in enumerate(p["storyboard"]["scenes"]):
        lines.append(f"[장면 {i + 1}] {s.get('visual', '')}\n{image_prompt(p, s)} {_aspect_word(p)}\n")
    return "\n".join(lines)


def helper_intro(p):
    """반자동 도우미: 그림 채팅 대화를 시작할 때 한 번 보내는 지시문 (그림체·인물·비율 고정)."""
    char = p["style"].get("character", "").strip()
    text = (f"지금부터 유튜브 영상에 쓸 그림을 장면마다 한 장씩 부탁할게요. 이 대화의 모든 그림은 아래 조건을 똑같이 지켜 주세요.\n"
            f"- 그림체: {style_prompt(p)}\n"
            + ("- 사진처럼 사실적인 그림이나 3D 렌더링은 안 돼요. 반드시 손으로 그린 2D 그림으로 그려 주세요.\n" if is_illustration(p) else "")
            + f"- 비율: {'가로 16:9' if _res(p)[0] >= _res(p)[1] else '세로 9:16'}\n")
    if char:
        text += f"- 반복해서 나오는 인물(외형을 항상 똑같이): {char}\n"
    text += ("- 그림 안에 글자는 짧은 한글만 써 주세요.\n" if p["style"].get("allow_text")
             else "- 그림 안에 글자, 자막, 워터마크를 넣지 마세요.\n")
    text += "이해했으면 '준비됐어요'라고만 답해 주세요. 그다음부터 장면 설명을 보내면 그림만 그려 주세요."
    return text


def helper_scene_prompt(p, i):
    """반자동 도우미: 장면마다 보내는 지시문. 대화가 길어지면 그림체를 잊으므로 매번 그림체를 다시 적는다."""
    sc = p["storyboard"]["scenes"][i]
    char = p["style"].get("character", "").strip()
    ratio = "가로 16:9" if _res(p)[0] >= _res(p)[1] else "세로 9:16"
    lines = [f"장면 {i + 1}: {sc.get('visual', '')}", scene_text(p, sc)]
    if char:
        lines.append(f"Recurring character (keep identical): {char}")
    lines.append(f"Art style: {style_prompt(p)}" + (f", {ILLUST_GUARD}" if is_illustration(p) else ""))
    lines.append(f"({ratio}, 처음과 같은 그림체" + (", 사진 말고 그림으로" if is_illustration(p) else "") + ", 글자 없이 그려 주세요)")
    return "\n".join(lines)


# ---------------- 6. 영상 ----------------

TRANS_CYCLE = ["fade", "dissolve", "slideleft", "smoothleft", "wipeleft", "circleopen", "fadeblack", "zoomin", "slideright"]


def _res(p, shorts=False):
    if shorts:
        return 1080, 1920
    w, h = (p["video"].get("resolution") or "1920x1080").split("x")
    return int(w), int(h)


def _ensure_font(p, folder):
    """자막·썸네일 글꼴을 프로젝트 fonts 폴더에 복사하고 상대 경로를 돌려준다."""
    sub = p["sub"]
    src = sub.get("font_file")
    if not src or not Path(src).exists():
        f = render.default_font()
        if not f:
            raise render.RenderError("한글 글꼴을 찾지 못했습니다. 영상 단계에서 글꼴 파일을 올려 주세요.")
        src, sub["font_file"], sub["font_family"] = f["file"], f["file"], f["family"]
    if not sub.get("font_family"):
        sub["font_family"] = render.font_family(src)
    fdir = Path(folder) / "fonts"
    fdir.mkdir(exist_ok=True)
    for old in fdir.iterdir():
        if old.name != Path(src).name:
            old.unlink()
    dst = fdir / Path(src).name
    if not dst.exists():
        shutil.copy2(src, dst)
    # drawtext 는 경로의 공백·한글을 피하려고 짧은 이름 사본을 쓴다
    simple = fdir / ("font" + Path(src).suffix.lower())
    if not simple.exists():
        shutil.copy2(src, simple)
    return f"fonts/{simple.name}"


def cues_for(sentences, max_chars, t_from=0.0, t_to=None):
    cues = []
    for s in sentences:
        if t_to is not None and (s["end"] <= t_from or s["start"] >= t_to):
            continue
        chunks = textutil.subtitle_chunks(s["text"], int(max_chars))
        total = sum(len(c) for c in chunks) or 1
        t = s["start"]
        span = s["end"] - s["start"]
        for c in chunks:
            d = span * len(c) / total
            cues.append((round(t - t_from, 3), round(t + d - t_from, 3), c))
            t += d
    return cues


def _motion(p, i, sc):
    mode = p["video"].get("effect", "ai")
    if mode == "none":
        return "still"
    if mode == "kenburns":
        return "zoom_in"
    if mode == "random":
        return random.Random(i * 7919).choice(render.MOTIONS)
    return sc.get("motion") or "zoom_in"


def _transition(p, i, sc):
    mode = p["video"].get("transition", "ai")
    if i == 0 or mode == "cut":
        return "cut"
    if mode == "fade":
        return "fade"
    if mode == "varied":
        return TRANS_CYCLE[i % len(TRANS_CYCLE)]
    t = sc.get("transition") or "fade"
    return t if t in ("cut",) + tuple(TRANS_CYCLE) or t in ("dissolve",) else "fade"


def run_render(p, cfg, ctx, folder, lang="ko", shorts=False):
    folder = Path(folder)
    if not p.get("audio"):
        raise ai.AiError("음성을 먼저 만들어 주세요.")
    if not p.get("storyboard"):
        raise ai.AiError("스토리보드를 먼저 만들어 주세요.")
    if lang != "ko" and lang not in p["audio_lang"]:
        raise ai.AiError("이 언어의 음성을 먼저 만들어 주세요.")
    vcfg, scfg = p["video"], dict(p["sub"])
    W, H = _res(p, shorts)
    fps = int(vcfg.get("fps") or 30)
    audio = p["audio"] if lang == "ko" else p["audio_lang"][lang]
    times = scene_times(p, lang)
    scenes = p["storyboard"]["scenes"]
    t_from, t_to = 0.0, audio["total"]
    if shorts:
        rng = (p.get("meta") or {}).get("shorts") or {}
        sents = audio["sentences"]
        if lang == "ko" and "first" in rng:
            a, b = max(0, int(rng["first"])), min(len(sents) - 1, int(rng["last"]))
        else:
            a, b = 0, 0
            while b + 1 < len(sents) and sents[b + 1]["end"] - sents[a]["start"] <= 58:
                b += 1
        t_from, t_to = sents[a]["start"], min(sents[b]["end"] + 0.6, sents[a]["start"] + 59.5)
    # 이 구간에 걸치는 장면만, 경계를 구간에 맞게 자른다
    picked = [(i, max(s, t_from) - t_from, min(e, t_to) - t_from) for i, (s, e) in enumerate(times) if e > t_from and s < t_to]
    total = t_to - t_from
    work = folder / ("build_shorts" if shorts else f"build_{lang}")
    cdir = work / "clips"
    cdir.mkdir(parents=True, exist_ok=True)
    font_rel = _ensure_font(p, folder)
    T = float(vcfg.get("trans_dur") or 0.5)
    trans = [_transition(p, k, scenes[i]) for k, (i, _, _) in enumerate(picked)]
    all_cut = all(t == "cut" for t in trans[1:])
    clips, used, todo = [], set(), []
    ctx.report(message=f"장면 클립 준비 중 (0/{len(picked)})", done=0, total=len(picked) + 1)
    for k, (i, s, e) in enumerate(picked):
        ctx.check()
        sc = scenes[i]
        pre = 0 if (k == 0 or all_cut) else (T if trans[k] != "cut" else 0.04) / 2
        post = 0 if (k == len(picked) - 1 or all_cut) else (T if trans[k + 1] != "cut" else 0.04) / 2
        dur = (e - s) + pre + post
        src, is_video = None, False
        if sc.get("video") and (folder / sc["video"]).exists():
            src, is_video = folder / sc["video"], True
        elif sc.get("image") and (folder / sc["image"]).exists():
            src = folder / sc["image"]
        else:
            ph = f"images/_auto_{i:03d}.png"
            if not (folder / ph).exists():
                render.placeholder_image(folder, ph, sc.get("visual") or "", i, W // 2, H // 2, font_rel)
            src = folder / ph
        motion = _motion(p, i, sc)
        key = hashlib.sha1(json.dumps([str(src), src.stat().st_mtime, round(dur, 3), motion, W, H, fps, is_video,
                                       vcfg.get("fill"), vcfg.get("strength"), shorts, render.MOTION_VERSION]).encode()).hexdigest()[:12]
        name = f"c{i:03d}_{key}.mp4"
        used.add(name)
        if not (cdir / name).exists():  # 바뀐 장면만 다시 만든다
            todo.append((str(src), name, dur, motion, is_video))
        clips.append(f"clips/{name}")
    # 장면 클립은 화면 효과(zoompan)가 한 코어만 쓰므로 여러 개를 동시에 만든다
    made = [len(picked) - len(todo)]
    ctx.report(message=f"장면 클립 만드는 중 ({made[0]}/{len(picked)})", done=made[0])

    def make_clip(job):
        ctx.check()
        src, name, dur, motion, is_video = job
        render.scene_clip(src, str(cdir / name), dur, motion, W, H, fps, is_video, vcfg.get("fill", "freeze"),
                          vertical_blur=H > W, workdir=cdir, strength=float(vcfg.get("strength") or 0.12))

    workers = max(1, min(int(cfg.get("render_workers") or max(1, min(3, (os.cpu_count() or 2) // 2))), 6))
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(make_clip, j) for j in todo]
        try:
            for fu in cf.as_completed(futs):
                fu.result()
                made[0] += 1
                ctx.report(message=f"장면 클립 만드는 중 ({made[0]}/{len(picked)})", done=made[0])
        except BaseException:
            for fu in futs:
                fu.cancel()
            raise
    for old in cdir.glob("*.mp4"):
        if old.name not in used:
            old.unlink()
    # 소리: 구간 잘라내기
    nar = folder / audio["narration"]
    if shorts:
        render.run(["-ss", f"{t_from:.3f}", "-t", f"{total:.3f}", "-i", nar, "-c:a", "pcm_s16le", work / "narration.wav"])
    else:
        shutil.copy2(nar, work / "narration.wav")
    # 자막
    ass = None
    cues = cues_for(audio["sentences"], scfg.get("max_chars", 25) if not shorts else min(int(scfg.get("max_chars", 25)), 14), t_from, t_to)
    if vcfg.get("subtitles", True):
        title = None
        if shorts:
            scfg.update(position="bottom", size=float(scfg.get("size", 17)) * 1.7, margin=0.26)
            title = ((p.get("meta") or {}).get("shorts") or {}).get("title") or p.get("name", "")
            title = "\n".join(textutil.subtitle_chunks(title, 12)[:2])
        if (fdir := folder / "fonts").exists():
            shutil.copytree(fdir, work / "fonts", dirs_exist_ok=True)
        render.write_ass(work / "subs.ass", cues, scfg, W, H, title=title, title_end=total)
        ass = "subs.ass"
    logo = bgm = None
    if vcfg.get("logo") and (folder / vcfg["logo"]).exists():
        shutil.copy2(folder / vcfg["logo"], work / ("logo" + Path(vcfg["logo"]).suffix))
        logo = "logo" + Path(vcfg["logo"]).suffix
    if vcfg.get("bgm") and (folder / vcfg["bgm"]).exists():
        shutil.copy2(folder / vcfg["bgm"], work / ("bgm" + Path(vcfg["bgm"]).suffix))
        bgm = "bgm" + Path(vcfg["bgm"]).suffix
    out_dir = folder / "out"
    out_dir.mkdir(exist_ok=True)
    stem = "shorts" if shorts else ("video" if lang == "ko" else f"video_{lang}")
    out_tmp = f"{stem}.tmp.mp4"
    enc_name, enc_args, _ = render.pick_encoder(cfg.get("video_encoder") or "auto")
    ctx.report(message=f"최종 영상 합치는 중 (0%, {enc_name})", done=len(picked), total=len(picked) + 1)
    t0 = time.time()

    def prog(sec):
        ctx.check()
        frac = min(0.99, sec / max(total, 1))
        left = ""
        if frac > 0.03:  # 지금 속도로 남은 시간 어림
            m = int((time.time() - t0) * (1 - frac) / frac / 60 + 0.5)
            left = f", 약 {m}분 남음" if m >= 1 else ", 1분 안에 끝나요"
        ctx.report(message=f"최종 영상 합치는 중 ({int(frac * 100)}%, {enc_name}{left})")

    render.final_render(work, clips, [(s, e) for _, s, e in picked], trans, "narration.wav", out_tmp, W, H, fps, T,
                        ass=ass, fontsdir="fonts" if ass else None, logo=logo,
                        logo_cfg={"scale": vcfg["logo_scale"], "x": vcfg["logo_x"], "y": vcfg["logo_y"], "opacity": vcfg["logo_opacity"]},
                        bgm=bgm, bgm_cfg={"volume": vcfg["bgm_volume"], "duck": vcfg["bgm_duck"]}, total=total, progress=prog,
                        encoder=enc_args)
    final = out_dir / f"{stem}.mp4"
    shutil.move(str(work / out_tmp), final)
    render.write_srt(out_dir / f"{stem}.srt", cues)
    p["outputs"][stem] = {"file": f"out/{stem}.mp4", "srt": f"out/{stem}.srt", "made": time.strftime("%Y-%m-%d %H:%M"),
                          "seconds": round(total, 1), "size": final.stat().st_size}


# ---------------- 7. 업로드 준비 ----------------

def chapters(p):
    a = p.get("audio")
    if not a:
        return []
    out, last = [], None
    for s in a["sentences"]:
        sec = s.get("section") or ""
        if sec != last:
            if not out and s["start"] > 0.5:  # 소제목 앞 도입부
                out.append((0.0, "인트로"))
            out.append((0.0 if not out else s["start"], sec or "인트로"))
            last = sec
    return out


def fmt_time(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def meta_prompt(p):
    sents = [s["text"] for s in p["audio"]["sentences"]] if p.get("audio") else [s["text"] for s in textutil.split_sentences(p["script"])]
    return prompts.build_meta(p["script"], sents, [c[1] for c in chapters(p)])


def set_meta(p, text):
    data = prompts.extract_json(text) if isinstance(text, str) else text
    if not isinstance(data, dict):
        raise ValueError("업로드 정보 JSON 모양이 아니에요. AI의 답 전체를 넣어 주세요.")
    data = dict(data)
    for k in ("titles", "tags", "thumb_texts"):
        data[k] = [str(t).strip() for t in (data.get(k) or []) if not prompts.is_placeholder(t)]
    if not data["titles"] and prompts.is_placeholder(data.get("description")):
        raise ValueError("제목과 설명이 비어 있어요. 지시문이 아니라 AI의 답을 넣어 주세요.")
    p["meta"] = {"titles": data.get("titles", []), "description": data.get("description", ""), "tags": data.get("tags", []),
                 "thumb_texts": data.get("thumb_texts", []), "pinned_comment": data.get("pinned_comment", ""),
                 "shorts": data.get("shorts") or {}}


def run_meta(p, cfg, ctx):
    ctx.report(message="제목·설명·태그 만드는 중", done=0, total=1)
    set_meta(p, ai.generate_text(cfg, meta_prompt(p), schema=prompts.META_SCHEMA, max_tokens=16000))


def full_description(p):
    m = p.get("meta") or {}
    desc = m.get("description", "")
    ch = chapters(p)
    if len(ch) >= 3:
        desc += "\n\n" + "\n".join(f"{fmt_time(t)} {name}" for t, name in ch)
    return desc


def run_thumbs(p, cfg, ctx, folder, scene=0, text=None):
    folder = Path(folder)
    scenes = (p.get("storyboard") or {}).get("scenes") or []
    src = None
    if scenes:
        sc = scenes[min(scene, len(scenes) - 1)]
        if sc.get("image") and (folder / sc["image"]).exists():
            src = sc["image"]
    if not src:
        raise ai.AiError("썸네일에 쓸 장면 이미지가 없습니다. 이미지를 먼저 만들어 주세요.")
    text = text or ((p.get("meta") or {}).get("thumb_texts") or [p.get("name", "")])[0]
    font_rel = _ensure_font(p, folder)
    (folder / "out").mkdir(exist_ok=True)
    files = []
    for k, theme in enumerate(render.THUMB_THEMES):
        ctx.check()
        rel = f"out/thumb_{k + 1}.jpg"
        render.thumbnail(folder, src, rel, text, font_rel, theme)
        files.append(rel)
    p["outputs"]["thumbs"] = files


def export_package(p, folder):
    """다른 편집 프로그램(캡컷·프리미어)용 묶음: 장면 그림, 음성, 자막, 장면 시간표."""
    folder = Path(folder)
    out = folder / "out" / "edit_package.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if p.get("audio"):
            z.write(folder / p["audio"]["narration"], "narration.wav")
            render.write_srt(folder / "out" / "_pkg.srt", cues_for(p["audio"]["sentences"], p["sub"].get("max_chars", 25)))
            z.write(folder / "out" / "_pkg.srt", "subtitles.srt")
        rows = ["장면,시작,끝,길이,파일,설명"]
        if p.get("storyboard") and p.get("audio"):
            for i, ((s, e), sc) in enumerate(zip(scene_times(p), p["storyboard"]["scenes"])):
                f = sc.get("video") or sc.get("image") or ""
                if f and (folder / f).exists():
                    name = f"scenes/{i + 1:03d}{Path(f).suffix}"
                    z.write(folder / f, name)
                else:
                    name = ""
                rows.append(f'{i + 1},{s:.2f},{e:.2f},{e - s:.2f},{name},"{sc.get("visual", "").replace(chr(34), "")}"')
        z.writestr("scenes.csv", "﻿" + "\n".join(rows))
        z.writestr("script.txt", p.get("script", ""))
    return out
