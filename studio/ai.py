"""AI 서비스 연결: 글(대본·스토리보드), 음성(TTS), 이미지.

글: Claude(Anthropic 공식 SDK), OpenAI, Google Gemini, 직접 붙여넣기(키 없이)
음성: 윈도우 기본 음성(무료), Google Gemini TTS, OpenAI TTS, ElevenLabs, 테스트용 무음
이미지: Google Gemini, OpenAI, 직접 업로드, 자리표시 카드(무료)
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

CLAUDE_DEFAULT = "claude-opus-5-5"
OPENAI_TEXT_DEFAULT = "gpt-4.1"
GEMINI_TEXT_DEFAULT = "gemini-2.5-flash"


class AiError(Exception):
    pass


# ---------------- HTTP ----------------

def http_json(url, body=None, headers=None, timeout=300, retries=3, raw=False, data=None):
    """data 를 주면 JSON 대신 그 바이트를 그대로 보낸다 (Azure SSML 등)."""
    if data is None:
        data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json", "User-Agent": "VideoWorkshop"}
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                payload = res.read()
                return payload if raw else json.loads(payload or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read()[:600].decode("utf-8", "replace")
            last = AiError(f"HTTP {e.code}: {detail}")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = float(e.headers.get("retry-after") or 0) or (2 ** attempt * 3)
                time.sleep(min(wait, 60))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = AiError(f"연결 실패: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise last
    raise last


# ---------------- 글 ----------------

def text_providers(cfg):
    return {
        "claude": bool(cfg.get("anthropic_key")),
        "openai": bool(cfg.get("openai_key")),
        "gemini": bool(cfg.get("gemini_key")),
        "manual": True,
    }


def generate_text(cfg, prompt, schema=None, max_tokens=64000):
    """설정된 글 AI로 prompt 를 보내 답 문자열을 받는다. schema 가 있으면 JSON 문자열."""
    p = cfg.get("text_provider") or "claude"
    if p == "claude":
        return _claude(cfg, prompt, schema, max_tokens)
    if p == "openai":
        return _openai_text(cfg, prompt, schema)
    if p == "gemini":
        return _gemini_text(cfg, prompt, schema)
    raise AiError("직접 붙여넣기 모드입니다. 화면의 '지시문 복사'로 진행하세요.")


def _claude(cfg, prompt, schema, max_tokens):
    key = cfg.get("anthropic_key")
    if not key:
        raise AiError("설정에서 Anthropic(Claude) API 키를 넣어 주세요.")
    try:
        import anthropic
    except ImportError as e:
        raise AiError("anthropic 패키지가 없습니다. 배포판을 다시 받거나 'pip install anthropic' 을 실행하세요.") from e
    client = anthropic.Anthropic(api_key=key, max_retries=3)
    output_config = {"effort": cfg.get("claude_effort") or "medium"}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    try:
        # 안전 분류기가 거절하면 서버가 권장 모델로 자동 재시도한다(fallbacks="default").
        with client.beta.messages.stream(
            model=cfg.get("claude_model") or CLAUDE_DEFAULT,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config=output_config,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise AiError("Claude API 키가 올바르지 않습니다.") from e
    except anthropic.PermissionDeniedError as e:
        raise AiError(f"Claude 권한 오류: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise AiError(f"Claude 모델 이름을 확인하세요: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise AiError("Claude 사용량 한도에 걸렸습니다. 잠시 뒤 다시 시도하세요.") from e
    except anthropic.APIStatusError as e:
        raise AiError(f"Claude 오류 ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AiError("Claude 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.") from e
    if msg.stop_reason == "refusal":
        raise AiError("Claude가 이 요청을 거절했습니다. 주제나 표현을 바꿔 보세요.")
    text = "".join(b.text for b in msg.content if b.type == "text")
    if msg.stop_reason == "max_tokens":
        raise AiError("답이 너무 길어 중간에 끊겼습니다. 분량을 줄여 다시 시도하세요.")
    return text


def _openai_text(cfg, prompt, schema):
    key = cfg.get("openai_key")
    if not key:
        raise AiError("설정에서 OpenAI API 키를 넣어 주세요.")
    body = {"model": cfg.get("openai_text_model") or OPENAI_TEXT_DEFAULT, "messages": [{"role": "user", "content": prompt}]}
    if schema:
        body["response_format"] = {"type": "json_object"}
    r = http_json("https://api.openai.com/v1/chat/completions", body, {"Authorization": f"Bearer {key}"}, timeout=600)
    return r["choices"][0]["message"]["content"]


def _gemini_text(cfg, prompt, schema):
    key = cfg.get("gemini_key")
    if not key:
        raise AiError("설정에서 Google Gemini API 키를 넣어 주세요.")
    model = cfg.get("gemini_text_model") or GEMINI_TEXT_DEFAULT
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 65536}}
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = http_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body,
                  {"x-goog-api-key": key}, timeout=600)
    try:
        return "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError):
        raise AiError(f"Gemini 응답을 읽지 못했습니다: {json.dumps(r, ensure_ascii=False)[:300]}")


# ---------------- 음성 ----------------

GEMINI_VOICES = ["Kore", "Puck", "Charon", "Aoede", "Fenrir", "Leda", "Orus", "Zephyr", "Sulafat", "Achird",
                 "Gacrux", "Schedar", "Sadaltager", "Vindemiatrix", "Despina", "Erinome", "Algenib", "Rasalgethi",
                 "Laomedeia", "Achernar", "Alnilam", "Pulcherrima", "Umbriel", "Iapetus", "Enceladus", "Autonoe",
                 "Callirrhoe", "Algieba", "Zubenelgenubi", "Sadachbia"]
OPENAI_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"]


def voice_catalog(cfg):
    """화면에 보여 줄 음성 목록. [{provider, id, name, desc, free}]"""
    out = []
    for v in sapi_voices():
        out.append({"provider": "sapi", "id": v["name"], "name": v["name"], "desc": f"윈도우 기본 음성 · {v['culture']}", "free": True})
    if cfg.get("gemini_key"):
        for v in GEMINI_VOICES:
            out.append({"provider": "gemini", "id": v, "name": v, "desc": "Gemini TTS · 다국어 · 말투 지시 가능", "free": False})
    if cfg.get("openai_key"):
        for v in OPENAI_VOICES:
            out.append({"provider": "openai", "id": v, "name": v, "desc": "OpenAI TTS · 다국어 · 말투 지시 가능", "free": False})
    if cfg.get("elevenlabs_key"):
        try:
            r = http_json("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": cfg["elevenlabs_key"]}, timeout=20, retries=1)
            for v in r.get("voices", []):
                labels = v.get("labels") or {}
                desc = " · ".join(x for x in (labels.get("gender"), labels.get("age"), labels.get("accent"), "ElevenLabs") if x)
                out.append({"provider": "elevenlabs", "id": v["voice_id"], "name": v.get("name", v["voice_id"]), "desc": desc, "free": False})
        except AiError as e:
            out.append({"provider": "elevenlabs", "id": "", "name": "ElevenLabs 목록 오류", "desc": str(e)[:80], "free": False})
    if cfg.get("azure_speech_key"):
        try:
            for v in azure_voices(cfg):
                out.append(v)
        except AiError as e:
            out.append({"provider": "azure", "id": "", "name": "Azure 목록 오류", "desc": str(e)[:80], "free": True})
    out.append({"provider": "silent", "id": "silent", "name": "테스트용 무음", "desc": "키 없이 흐름만 확인 (글자 수로 길이 계산)", "free": True})
    return out


_sapi_cache = []


def sapi_voices():
    if sys.platform != "win32":
        return []
    if _sapi_cache:
        return _sapi_cache
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          "$s.GetInstalledVoices()|%{$_.VoiceInfo}|%{$_.Name+'|'+$_.Culture.Name}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    for line in out.splitlines():
        if "|" in line:
            n, c = line.strip().split("|", 1)
            _sapi_cache.append({"name": n, "culture": c})
    _sapi_cache.sort(key=lambda v: (not v["culture"].startswith("ko"), v["name"]))
    return _sapi_cache


def synth_sapi_batch(items, voice, rate):
    """items: [(text, wav_path)]. PowerShell 한 번으로 모두 합성한다."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump([{"t": plain_spoken(t), "p": str(p)} for t, p in items], f, ensure_ascii=False)
        jpath = f.name
    r = max(-10, min(10, round((rate - 1.0) * 10)))
    ps = (f"Add-Type -AssemblyName System.Speech;$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SelectVoice('{voice.replace(chr(39), '')}');$s.Rate={r};"
          f"$j=Get-Content -Raw -Encoding UTF8 '{jpath.replace(chr(39), chr(39) * 2)}'|ConvertFrom-Json;"
          f"foreach($x in $j){{$s.SetOutputToWaveFile($x.p);$s.Speak($x.t)}};$s.SetOutputToNull()")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           timeout=60 + 20 * len(items), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode != 0:
            raise AiError("윈도우 음성 합성 실패: " + (p.stderr or "")[:300])
    finally:
        os.unlink(jpath)


def synth_one(cfg, provider, voice, text, out_base, speed=1.0, style=""):
    """문장 하나를 합성해 파일 경로(.wav/.mp3)를 돌려준다."""
    # 끊어 읽기 표시(⏸): Azure 는 정확한 쉼(SSML break), 나머지 엔진은 쉼표로 쉬게 한다
    if provider != "azure":
        text = plain_spoken(text)
        key = cfg.get("gemini_key") or ""
        model = cfg.get("gemini_tts_model") or "gemini-2.5-flash-preview-tts"
        spoken = f"{style.strip()}: {text}" if style.strip() else text
        body = {"contents": [{"parts": [{"text": spoken}]}], "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
        r = http_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body,
                      {"x-goog-api-key": key}, timeout=180, retries=5)
        try:
            part = next(p for p in r["candidates"][0]["content"]["parts"] if "inlineData" in p)
        except (KeyError, IndexError, StopIteration):
            raise AiError(f"Gemini TTS 응답 오류: {json.dumps(r, ensure_ascii=False)[:200]}")
        pcm = base64.b64decode(part["inlineData"]["data"])
        out = Path(str(out_base) + ".wav")
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        return out
    if provider == "openai":
        body = {"model": cfg.get("openai_tts_model") or "gpt-4o-mini-tts", "voice": voice, "input": text,
                "response_format": "wav", "speed": max(0.25, min(4.0, speed))}
        if style.strip():
            body["instructions"] = style.strip()
        data = http_json("https://api.openai.com/v1/audio/speech", body,
                         {"Authorization": f"Bearer {cfg.get('openai_key', '')}"}, timeout=180, retries=5, raw=True)
        out = Path(str(out_base) + ".wav")
        out.write_bytes(data)
        return out
    if provider == "elevenlabs":
        body = {"text": text, "model_id": cfg.get("elevenlabs_model") or "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": max(0.7, min(1.2, speed))}}
        data = http_json(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128", body,
                         {"xi-api-key": cfg.get("elevenlabs_key", ""), "Accept": "audio/mpeg"}, timeout=180, retries=5, raw=True)
        out = Path(str(out_base) + ".mp3")
        out.write_bytes(data)
        return out
    if provider == "azure":
        rate = round((max(0.5, min(2.0, speed)) - 1.0) * 100)
        lang = "-".join(voice.split("-")[:2]) if voice.count("-") >= 2 else "ko-KR"
        ssml = (f"<speak version='1.0' xml:lang='{lang}'><voice name='{xml_escape(voice)}'>"
                f"<prosody rate='{rate:+d}%'>{_azure_body(text)}</prosody></voice></speak>")
        data = http_json(f"https://{azure_region(cfg)}.tts.speech.microsoft.com/cognitiveservices/v1", None,
                         {"Ocp-Apim-Subscription-Key": cfg.get("azure_speech_key", ""), "Content-Type": "application/ssml+xml",
                          "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm"},
                         timeout=120, retries=6, raw=True, data=ssml.encode("utf-8"))
        out = Path(str(out_base) + ".wav")
        out.write_bytes(data)
        return out
    raise AiError(f"알 수 없는 음성 제공자: {provider}")


# ---------------- Azure 음성 (무료 등급: 한 달 50만 자) ----------------

def plain_spoken(text):
    """SSML 을 못 쓰는 엔진용: 직접 넣은 쉼(⏸)은 쉼표로, 자동 쉼(⏵)은 없앤다."""
    text = re.sub(r"\s*\u23f8\s*", ", ", text)
    return re.sub(r"\s*\u23f5\s*", " ", text).strip(", ")


def _azure_body(text):
    """SSML 본문: 직접 넣은 쉼(⏸)은 0.3초, 자동 끊어 읽기(⏵)는 0.18초."""
    out = []
    for tok in re.split(r"(\u23f8|\u23f5)", text):
        if tok == "\u23f8":
            out.append("<break time='300ms'/>")
        elif tok == "\u23f5":
            out.append("<break time='180ms'/>")
        elif tok.strip():
            out.append(xml_escape(tok.strip()))
    return "".join(out)


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;")


def azure_region(cfg):
    import re as _re
    r = (cfg.get("azure_region") or "koreacentral").strip().lower().replace(" ", "")
    if not _re.fullmatch(r"[a-z0-9]{3,30}", r):
        raise AiError("Azure 지역 이름이 올바르지 않습니다. 예: koreacentral")
    return r


_azure_cache = {}


def azure_voices(cfg):
    """한국어 음성과 한국어도 읽는 다국어 음성 목록."""
    key = (cfg.get("azure_speech_key"), azure_region(cfg))
    if key not in _azure_cache:
        r = http_json(f"https://{azure_region(cfg)}.tts.speech.microsoft.com/cognitiveservices/voices/list",
                      headers={"Ocp-Apim-Subscription-Key": cfg["azure_speech_key"]}, timeout=20, retries=1)
        out = []
        for v in r:
            locs = v.get("SecondaryLocaleList") or []
            if v.get("Locale") != "ko-KR" and not ("Multilingual" in v.get("ShortName", "") and "ko-KR" in locs):
                continue
            gender = {"Female": "여성", "Male": "남성"}.get(v.get("Gender"), "")
            kind = "한국어" if v.get("Locale") == "ko-KR" else "다국어"
            out.append({"provider": "azure", "id": v["ShortName"], "name": v.get("LocalName") or v.get("DisplayName") or v["ShortName"],
                        "desc": f"{gender} · {kind} · Azure 무료 등급 월 50만 자", "free": True})
        out.sort(key=lambda x: ("다국어" in x["desc"], x["name"]))
        _azure_cache[key] = out
    return _azure_cache[key]


# ---------------- 이미지 ----------------

def generate_image(cfg, provider, prompt, out_base, aspect="16:9", ref_image=None):
    """ref_image: 화풍 기준 그림 경로 (Gemini 만 사용). 그림체를 맞추는 데만 쓰고 내용은 따라 하지 않게 지시한다."""
    if provider == "gemini":
        key = cfg.get("gemini_key")
        if not key:
            raise AiError("설정에서 Google Gemini API 키를 넣어 주세요.")
        model = cfg.get("gemini_image_model") or "gemini-2.5-flash-image"
        parts = []
        if ref_image and Path(ref_image).exists():
            mime = "image/jpeg" if Path(ref_image).suffix.lower() in (".jpg", ".jpeg") else "image/png"
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(Path(ref_image).read_bytes()).decode()}})
            prompt = ("Use the attached image only as an art style reference (line work, coloring, texture, lighting). "
                      "Do not copy its content or composition. Draw this scene: " + prompt)
        parts.append({"text": prompt})
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect}}}
        r = http_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body,
                      {"x-goog-api-key": key}, timeout=240, retries=4)
        try:
            parts = r["candidates"][0]["content"]["parts"]
            part = next(p for p in parts if "inlineData" in p)
        except (KeyError, IndexError, StopIteration):
            reason = (r.get("candidates") or [{}])[0].get("finishReason") or r.get("promptFeedback", {}).get("blockReason")
            raise AiError(f"Gemini가 이미지를 만들지 않았습니다 ({reason or '응답 없음'}). 프롬프트를 바꿔 보세요.")
        mime = part["inlineData"].get("mimeType", "image/png")
        out = Path(str(out_base) + (".jpg" if "jpeg" in mime else ".png"))
        out.write_bytes(base64.b64decode(part["inlineData"]["data"]))
        return out
    if provider == "openai":
        key = cfg.get("openai_key")
        if not key:
            raise AiError("설정에서 OpenAI API 키를 넣어 주세요.")
        size = {"16:9": "1536x1024", "9:16": "1024x1536"}.get(aspect, "1024x1024")
        body = {"model": cfg.get("openai_image_model") or "gpt-image-1", "prompt": prompt, "size": size,
                "quality": cfg.get("openai_image_quality") or "medium", "n": 1}
        r = http_json("https://api.openai.com/v1/images/generations", body, {"Authorization": f"Bearer {key}"}, timeout=300, retries=4)
        out = Path(str(out_base) + ".png")
        out.write_bytes(base64.b64decode(r["data"][0]["b64_json"]))
        return out
    raise AiError(f"알 수 없는 이미지 제공자: {provider}")
