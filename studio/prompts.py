"""AI에게 보내는 지시문(프롬프트)과 응답 해석. 각 단계 = build_*(...) -> 문자열, parse_*(문자열) -> 데이터.
API 키 없이 쓰는 '직접 붙여넣기' 모드도 같은 지시문을 복사해 Claude.ai 등에 붙여 넣고 답을 다시 붙여 넣는다.
"""
import json
import re

# ---------------- 화풍 16종 ----------------
STYLES = [
    # id, 그룹, 이름, 설명, 영어 프롬프트
    ("cinematic", "실사·3D", "시네마틱 실사", "영화 스틸컷 같은 사실적 장면과 극적인 조명",
     "cinematic photorealistic film still, dramatic lighting, shallow depth of field, 35mm, rich color grading"),
    ("pixar3d", "실사·3D", "3D 애니메이션", "극장판 3D 애니메이션 느낌, 생동감 있는 캐릭터",
     "3D animated feature film style, expressive stylized characters, soft global illumination, vibrant colors"),
    ("miniature", "실사·3D", "미니어처 디오라마", "장난감 블록·점토로 만든 틸트시프트 디오라마",
     "miniature diorama, clay and toy blocks, tilt-shift photography, soft studio light, cute and tactile"),
    ("cyberpunk", "실사·3D", "사이버펑크", "네온 불빛의 미래 도시, 보라·청록 대비",
     "cyberpunk 3D render, neon lights, rainy night city, purple and teal contrast, volumetric fog"),
    ("webtoon", "만화·애니", "한국 웹툰", "깔끔한 선과 밝은 채색의 웹툰",
     "Korean webtoon illustration, clean line art, bright cel shading, expressive faces, detailed backgrounds"),
    ("joseon", "만화·애니", "조선 수묵 웹툰", "한지 질감과 수묵 채색의 사극 웹툰",
     "Joseon dynasty Korean historical webtoon, ink wash and soft watercolor on hanji paper texture"),
    ("ghibli_like", "만화·애니", "수채 애니메이션", "따뜻한 손그림 수채 배경의 극장 애니메이션 느낌",
     "hand-painted watercolor anime film style, warm nostalgic light, lush detailed backgrounds, gentle palette"),
    ("retro_anime", "만화·애니", "80·90년대 애니", "셀 애니메이션 질감, 필름 그레인",
     "1990s retro anime cel animation, film grain, muted pastel colors, VHS era aesthetic"),
    ("infographic", "정보·뉴스", "인포그래픽", "숫자와 도표가 잘 보이는 깔끔한 정보 그림",
     "clean flat infographic illustration, simple icons and charts, bold shapes, minimal text, navy and orange palette"),
    ("news_photo", "정보·뉴스", "보도사진풍", "신문 사진 같은 다큐멘터리 실사",
     "documentary news photography style, natural light, realistic, editorial composition, no text"),
    ("flat", "정보·뉴스", "플랫 일러스트", "단순한 면과 색으로 이해하기 쉬운 그림",
     "modern flat vector illustration, simple geometric shapes, limited palette, friendly characters"),
    ("whiteboard", "정보·뉴스", "화이트보드 낙서", "펜으로 그린 설명용 낙서",
     "whiteboard doodle sketch, black marker lines with a few accent colors, explainer video style, white background"),
    ("dark_horror", "분위기", "미스터리·공포", "어둡고 서늘한 긴장감",
     "dark eerie atmosphere, desaturated colors, deep shadows, mysterious, cinematic horror illustration, no gore"),
    ("oil", "분위기", "유화", "붓 터치가 살아 있는 고전 유화",
     "classical oil painting, visible brush strokes, rich textures, chiaroscuro lighting"),
    ("senior_warm", "분위기", "따뜻한 감성", "중장년이 편하게 보는 부드러운 그림",
     "warm soft illustration, gentle pastel colors, cozy everyday Korean life, heartwarming mood"),
    ("pixel", "분위기", "픽셀 아트", "레트로 게임 도트 그래픽",
     "detailed 16-bit pixel art, retro game aesthetic, vibrant limited palette"),
    ("issue_ink", "정보·뉴스", "이슈 해설 일러스트", "굵은 펜 선과 수채 채색, 해질녘 한국 골목·아파트 풍경",
     "detailed Korean editorial illustration, bold ink outlines with watercolor wash, warm dusk lighting, "
     "everyday Korean streets and buildings, rich background detail, storytelling composition"),
    ("pop_comic", "만화·애니", "코믹 팝아트", "굵은 윤곽선과 강렬한 원색의 미국 코믹스",
     "American comic book pop art, bold black outlines, halftone dots, vivid primary colors"),
    ("chibi", "만화·애니", "치비", "머리가 큰 귀여운 2등신 캐릭터",
     "cute chibi illustration, big heads and small bodies, soft pastel colors, rounded shapes"),
]
# 화풍 예시 그림: 모든 화풍을 같은 장면으로 그려 비교하게 한다
STYLE_SAMPLE_SCENE = ("A small Korean town street at golden hour, a middle-aged person walking toward the viewer, "
                      "low houses and shops on both sides, mountains in the background")


def style_sample_prompt(style_id):
    s = STYLE_MAP[style_id]
    return f"{STYLE_SAMPLE_SCENE}. Art style: {s[4]}. No text, no watermark."
STYLE_MAP = {s[0]: s for s in STYLES}

# ---------------- 채널 성격(카테고리) ----------------
CATEGORIES = {
    "경제": "경제·부동산·재테크 채널. 숫자와 사실을 정확히, 쉬운 비유로 풀어 준다. 매수·매도를 직접 권하지 않는다.",
    "역사·야담": "옛이야기·역사 채널. 장면이 그려지듯 이야기체로, 인물의 감정과 반전을 살린다.",
    "미스터리": "미스터리·사건 채널. 긴장감을 쌓되 사실과 추측을 분명히 나눈다. 잔혹한 묘사는 피한다.",
    "건강": "건강 정보 채널. 근거가 있는 내용만, 단정하지 않고 '전문의와 상담'을 안내한다.",
    "사연·썰": "사연 낭독 채널. 1인칭 이야기체, 감정선과 반전, 공감 포인트를 살린다.",
    "지식·교양": "지식 채널. 하나의 궁금증에서 출발해 단계적으로 설명하고 예시를 든다.",
    "자기계발": "동기부여 채널. 구체적인 행동 팁과 사례, 과장된 성공 약속은 하지 않는다.",
    "시니어": "중장년 시청자 채널. 천천히, 쉬운 말로, 존중하는 말투. 생활에 바로 쓰는 정보를 준다.",
}

LANG_NAME = {"ko": "한국어", "ja": "일본어", "en": "영어"}


# ---------------- 대본 ----------------

def build_script(topic, source, minutes, chars, category, tone_note="", lang="ko", cta="end"):
    cat = CATEGORIES.get(category, category or "일반 정보 채널")
    src = f"\n\n[참고 자료 — 사실은 이 안에서만 가져올 것]\n{source.strip()[:20000]}" if source and source.strip() else ""
    cta_rule = {
        "early": "도입부(첫 1분 안)에 구독·좋아요 부탁을 한 번, 끝에 한 번 짧게 넣는다.",
        "end": "구독·좋아요 부탁은 마지막에 한 번만 짧게 넣는다.",
        "none": "구독·좋아요 부탁은 넣지 않는다.",
    }.get(cta, "")
    return f"""너는 조회수와 시청 지속률이 높은 유튜브 롱폼 내레이션 작가다. 아래 조건으로 {LANG_NAME.get(lang, '한국어')} 내레이션 대본을 써라.

[주제] {topic.strip()}
[채널 성격] {cat}
[분량] 약 {minutes}분 낭독, 공백 제외 약 {chars:,}자 (±5% 안에서 맞출 것)
{('[추가 요청] ' + tone_note.strip()) if tone_note.strip() else ''}

[구성]
1. 첫 3문장: 인사 없이 가장 궁금한 장면·숫자·질문으로 시작한다. 끝까지 보면 무엇을 알게 되는지 약속한다.
   바로 다음에 자료의 출처와 한계를 한 문장으로 밝혀 신뢰를 준다(예: "지금 다루는 숫자는 ○○ 발표 자료예요").
2. 본문은 3~6개 구간. 각 구간은 '## 소제목' 한 줄로 시작한다(소제목은 낭독하지 않고 챕터로만 쓴다).
3. 구간이 끝날 때마다 다음 내용을 궁금하게 만드는 연결 문장을 둔다.
4. 마지막은 핵심 3가지 요약과 시청자에게 던지는 질문으로 끝낸다. {cta_rule}

[문체]
- 말하듯 쓰는 해요체. 한 문장은 되도록 50자 이내.
- 문단은 2~4문장, 문단 사이 빈 줄.
- 숫자는 아라비아 숫자로 쓴다(예: 86주, 2,500만 원). 음성 변환은 프로그램이 알아서 한다.
- 괄호, 이모지, 특수기호, 표, 목록 기호를 쓰지 않는다.

[사실]
- 참고 자료가 있으면 그 안의 사실·숫자만 쓴다. 없는 숫자를 지어내지 않는다.
- 의견·전망은 '~라는 분석도 있어요'처럼 사실과 구분한다.
- 특정 종목·매물의 매수·매도를 권하지 않는다.

대본 본문만 출력하라. 앞뒤 설명, 제목, 따옴표를 붙이지 마라.{src}"""


def parse_script(text):
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text)
    lines = text.split("\n")
    # 모델이 붙인 머리말 제거 ("다음은 대본입니다:" 등)
    while lines and re.match(r"^(다음은|아래는|Here|물론|대본[:：])", lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


# ---------------- 사실 대조 ----------------

FACT_SCHEMA = {
    "type": "object",
    "properties": {"issues": {"type": "array", "items": {
        "type": "object",
        "properties": {"quote": {"type": "string"}, "problem": {"type": "string"}, "fix": {"type": "string"}},
        "required": ["quote", "problem", "fix"], "additionalProperties": False}}},
    "required": ["issues"], "additionalProperties": False,
}


def build_factcheck(script, source):
    return f"""아래 [대본]을 [참고 자료]와 대조해, 자료에 근거가 없거나 자료와 다른 사실·숫자·인용을 찾아라.
또 유튜브 정책상 위험한 표현(투자 권유, 의학적 단정, 혐오·비하, 과장 낚시)도 찾아라.
문제가 없으면 빈 목록을 낸다. 사소한 문체는 지적하지 마라.

JSON 한 개만 출력: {{"issues": [{{"quote": "대본의 문제 문장 그대로", "problem": "무엇이 문제인지", "fix": "고친 문장"}}]}}

[참고 자료]
{(source or '(자료 없음: 일반 상식과 다르거나 검증이 필요한 주장만 표시)')[:20000]}

[대본]
{script}"""


# ---------------- 번역 ----------------

def build_translate(script, target):
    return f"""아래 유튜브 내레이션 대본을 자연스러운 {LANG_NAME.get(target, target)} 내레이션으로 옮겨라.
- 직역보다 그 언어 유튜브에서 실제로 쓰는 말투로.
- '## 소제목' 줄과 문단 구분(빈 줄)은 그대로 유지하고 소제목도 번역한다.
- 숫자는 아라비아 숫자, 화폐 단위는 원문 그대로(원 → won/ウォン).
번역문만 출력하라.

{script}"""


# ---------------- 스토리보드 ----------------

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "character": {"type": "string"},
        "scenes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "first": {"type": "integer"}, "last": {"type": "integer"},
                "visual": {"type": "string"}, "prompt": {"type": "string"},
                "mood": {"type": "string", "enum": ["calm", "tense", "sad", "happy", "dramatic", "info"]},
                "motion": {"type": "string", "enum": ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "still"]},
                "transition": {"type": "string", "enum": ["cut", "fade", "dissolve", "slideleft", "slideright", "wipeleft", "circleopen", "fadeblack", "zoomin", "smoothleft"]},
            },
            "required": ["first", "last", "visual", "prompt", "mood", "motion", "transition"],
            "additionalProperties": False}},
    },
    "required": ["character", "scenes"], "additionalProperties": False,
}


def build_storyboard(sentences, scene_count, style_prompt, character):
    numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(sentences))
    char_rule = (f"주인공(반복 등장 인물)의 외형은 이것으로 고정: {character}" if character.strip()
                 else "반복 등장 인물이 필요하면 나이·성별·머리·옷차림을 영어 한 문장으로 정해 character 에 쓰고, 모든 장면에서 똑같이 유지한다. 필요 없으면 빈 문자열.")
    return f"""너는 유튜브 영상 스토리보드 감독이다. 번호 붙은 내레이션 문장을 연속된 장면 약 {scene_count}개로 묶고 장면마다 이미지 생성 프롬프트를 써라.

규칙
- 모든 문장이 순서대로 정확히 한 장면에 속해야 한다. 첫 장면 first=0, 마지막 장면 last={len(sentences) - 1}, 다음 장면 first = 이전 last + 1.
- 한 장면은 보통 1~4문장(5~15초). 도입부 장면은 짧게(1~2문장) 해서 화면이 빨리 바뀌게 한다.
- visual: 화면에 보일 장면을 한국어 한 문장으로.
- prompt: 이미지 생성용 영어 프롬프트. 피사체, 행동, 배경, 구도, 조명을 구체적으로. 화풍 문구는 쓰지 말 것(프로그램이 붙인다). 렌즈·심도·보케·사진 같은 촬영 용어도 쓰지 않는다. 사람이 나오지 않는 장면은 prompt 끝에 no people 을 쓴다. 글자·로고·워터마크가 나오지 않게 하고, 실존 인물의 얼굴이나 상표를 그리지 않는다.
- {char_rule}
- mood, motion(카메라 움직임), transition(이 장면으로 들어올 때 전환)을 내용에 맞게 고른다. 전환은 대부분 cut/fade/dissolve, 구간이 바뀌는 곳만 화려한 전환을 쓴다.
- 화풍 참고: {style_prompt}

JSON 한 개만 출력: {{"character": "...", "scenes": [{{"first": 0, "last": 1, "visual": "...", "prompt": "...", "mood": "calm", "motion": "zoom_in", "transition": "fade"}}]}}

[문장]
{numbered}"""


def fix_storyboard(data, n_sent):
    """AI가 낸 장면 범위를 검사해 빈틈·겹침 없이 0..n-1 을 덮도록 고친다."""
    if not isinstance(data, dict):
        raise ValueError("스토리보드 JSON 모양이 아니에요. AI의 답 전체를 넣어 주세요.")
    raw = [s for s in data.get("scenes", []) if isinstance(s, dict)]
    if not raw:
        raise ValueError("답에 장면(scenes)이 없어요. AI의 답 전체를 넣어 주세요.")
    if all(is_placeholder(s.get("prompt")) and is_placeholder(s.get("visual")) for s in raw):
        raise ValueError("장면 설명이 \"...\" 뿐이에요. 지시문 속 예시가 들어간 것 같아요. AI의 답을 넣어 주세요.")
    if is_placeholder(data.get("character")):
        data = dict(data, character="")
    scenes = sorted((s for s in data.get("scenes", []) if isinstance(s, dict)), key=lambda s: int(s.get("first", 0)))
    out, nxt = [], 0
    for s in scenes:
        last = min(int(s.get("last", nxt)), n_sent - 1)
        if last < nxt:
            continue
        s = dict(s, first=nxt, last=last)
        out.append(s)
        nxt = last + 1
        if nxt >= n_sent:
            break
    if nxt < n_sent:
        if out:
            out[-1]["last"] = n_sent - 1
        else:
            out.append({"first": 0, "last": n_sent - 1, "visual": "", "prompt": "", "mood": "calm", "motion": "zoom_in", "transition": "fade"})
    for s in out:
        s.setdefault("visual", "")
        s.setdefault("prompt", s.get("visual", ""))
        s.setdefault("mood", "calm")
        s.setdefault("motion", "zoom_in")
        s.setdefault("transition", "fade")
    return {"character": str(data.get("character", "")), "scenes": out}


# ---------------- 업로드 정보 ----------------

META_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "thumb_texts": {"type": "array", "items": {"type": "string"}},
        "pinned_comment": {"type": "string"},
        "shorts": {"type": "object", "properties": {
            "first": {"type": "integer"}, "last": {"type": "integer"}, "title": {"type": "string"}},
            "required": ["first", "last", "title"], "additionalProperties": False},
    },
    "required": ["titles", "description", "tags", "thumb_texts", "pinned_comment", "shorts"],
    "additionalProperties": False,
}


def build_meta(script, sentences, chapters):
    numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(sentences))
    return f"""아래 유튜브 롱폼 대본으로 업로드 정보를 만들어라.
- titles: 클릭하고 싶지만 내용과 일치하는 제목 5개 (각 40자 이내, 낚시·허위 금지)
- description: 첫 2줄에 핵심 요약, 이어서 영상 내용 3~5줄. 해시태그 3개를 마지막 줄에. 챕터는 프로그램이 붙이니 쓰지 마라.
- tags: 검색 태그 15개
- thumb_texts: 썸네일에 크게 넣을 짧은 문구 3개 (각 12자 이내)
- pinned_comment: 시청자 참여를 부르는 고정 댓글 한 개
- shorts: 쇼츠로 잘라 쓸 가장 강한 연속 구간(아래 문장 번호 first~last, 낭독 40~55초 분량)과 쇼츠 제목

챕터(참고): {', '.join(chapters) or '없음'}

JSON 한 개만 출력.

[문장]
{numbered}"""


# ---------------- 공통: 붙여 넣은 답 검사 ----------------

PLACEHOLDERS = {"", "...", "…", "..", "string", "<...>"}
PROMPT_MARKERS = ("JSON 한 개만 출력", "대본 본문만 출력하라")


def is_prompt_echo(text, prompt):
    """답 대신 지시문을 붙여 넣었는지: 지시문 첫 줄이나 지시문에만 있는 문구가 들어 있으면 그렇다고 본다."""
    first = next((l.strip() for l in (prompt or "").split("\n") if l.strip()), "")
    if len(first) >= 10 and first in text:
        return True
    return any(m in text and m in (prompt or "") for m in PROMPT_MARKERS)


def is_placeholder(v):
    return str(v if v is not None else "").strip() in PLACEHOLDERS


# ---------------- 공통: AI 답에서 JSON 꺼내기 ----------------

def _balanced(text, start):
    """text[start] 의 괄호와 짝이 맞는 끝 위치 (문자열 안 괄호는 무시). 없으면 None."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def extract_json(text):
    """AI 답에서 JSON 하나를 꺼낸다. 앞뒤 설명 글에 [직접 붙여넣기] 같은 괄호가 있어도
    실제로 JSON 으로 읽히는 첫 덩어리를 찾는다(객체 {...} 를 먼저)."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    first_err = None
    for opener in ("[{" if text.startswith("[") else "{["):
        pos = text.find(opener)
        while pos >= 0:
            end = _balanced(text, pos)
            try:
                return json.loads(text[pos:end] if end else text[pos:])
            except ValueError as e:
                first_err = first_err or e
            pos = text.find(opener, pos + 1)
    raise ValueError("답에서 JSON을 찾지 못했어요. AI의 답 전체를 넣어 주세요." + (f" ({first_err})" if first_err else ""))
