"""대본 텍스트 처리: 문장 나누기, 자막 줄 나누기, 숫자를 한국어 읽기로 바꾸기, 대본 자동 점검."""
import re

# ---------------- 문장 나누기 ----------------

_SENT_END = re.compile(r'(?<=[.!?…。！？])["\'”’)\]]*\s+')


def split_sections(script):
    """'## 소제목' 줄을 기준으로 [(제목, 본문)] 목록. 제목 없는 첫 부분은 제목 ''."""
    sections, title, buf = [], "", []
    for line in script.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^\s*#{1,6}\s*(.+?)\s*$", line)
        if m:
            if "".join(buf).strip():
                sections.append((title, "\n".join(buf).strip()))
            title, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((title, "\n".join(buf).strip()))
    return sections


def split_sentences(script):
    """대본을 낭독 단위 문장 목록으로. 각 문장: {text, para_end, section}."""
    out = []
    for title, body in split_sections(script):
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        for p in paras:
            p = re.sub(r"\s*\n\s*", " ", p)
            parts = [s.strip() for s in _SENT_END.split(p) if s.strip()]
            merged = []
            for s in parts:  # 아주 짧은 조각("네.")은 다음 문장과 합친다
                if merged and len(merged[-1]) < 6:
                    merged[-1] += " " + s
                else:
                    merged.append(s)
            for s in _split_long(merged):
                out.append({"text": s, "para_end": False, "section": title})
            if out:
                out[-1]["para_end"] = True
    return out


def _split_long(sentences, limit=110):
    """너무 긴 문장은 쉼표에서 나눠 음성 합성이 안정적이게 한다."""
    res = []
    for s in sentences:
        while len(s) > limit:
            cut = max(s.rfind(",", 0, limit), s.rfind("，", 0, limit))
            if cut < limit // 3:
                cut = s.rfind(" ", 0, limit)
            if cut < limit // 3:
                break
            res.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if s:
            res.append(s)
    return res


def plain_text(script):
    """소제목 줄을 뺀 낭독 본문."""
    return "\n\n".join(body for _, body in split_sections(script))


def char_count(script):
    return len(re.sub(r"\s", "", plain_text(script)))


# ---------------- 자막 ----------------

def subtitle_chunks(text, max_chars=25):
    """문장을 자막 한 화면 단위로 나눈다. 띄어쓰기 경계에서 자른다."""
    words, chunks, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        chunks.append(cur)
    # 마지막 조각이 너무 짧으면 앞에 붙인다
    if len(chunks) >= 2 and len(chunks[-1]) <= 4 and len(chunks[-2]) + len(chunks[-1]) + 1 <= max_chars + 6:
        last = chunks.pop()
        chunks[-1] += " " + last
    return chunks or [text]


# ---------------- 숫자 읽기 (자막은 숫자 그대로, 음성만 한국어로) ----------------

_DIG = "영일이삼사오육칠팔구"
_SMALL = ["", "십", "백", "천"]
_BIG = ["", "만", "억", "조", "경"]
_NATIVE = {1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯", 6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉",
           10: "열", 20: "스물", 30: "서른", 40: "마흔", 50: "쉰", 60: "예순", 70: "일흔", 80: "여든", 90: "아흔"}
_NATIVE_COUNTERS = ("시간", "시 ", "명", "개", "살", "마리", "번째", "번 ", "가지", "권", "잔", "대 ", "채", "곳", "달", "벌", "통", "켤레", "그루", "송이", "판")


def sino(n):
    """정수를 한자어 수 읽기로. 12345 -> 만이천삼백사십오"""
    if n == 0:
        return "영"
    if n < 0:
        return "마이너스 " + sino(-n)
    out, group = [], 0
    while n > 0:
        n, chunk = divmod(n, 10000)
        if chunk:
            s = ""
            for i, d in enumerate(reversed(str(chunk).zfill(4))):
                d = int(d)
                if d:
                    s = ("" if d == 1 and i > 0 else _DIG[d]) + _SMALL[i] + s
            if group == 1 and s == "일":  # 일만 -> 만
                s = ""
            out.insert(0, s + _BIG[group])
        group += 1
    return " ".join(out).strip()


def native(n):
    if 1 <= n <= 99:
        tens, ones = divmod(n, 10)
        return (_NATIVE.get(tens * 10, "") if tens else "") + (_NATIVE.get(ones, "") if ones else "")
    return sino(n)


def _decimal(s):
    whole, _, frac = s.partition(".")
    out = sino(int(whole))
    if frac:
        out += " 점 " + "".join(_DIG[int(c)] for c in frac)
    return out


_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)(\s?)(%|퍼센트|[가-힣]{0,3}\s?)")


def read_numbers(text):
    """음성용 텍스트: 86주 -> 팔십육 주, 2,500만 원 -> 이천오백만 원, 3명 -> 세 명, 0.4% -> 영 점 사 퍼센트"""
    def rep(m):
        raw, sp, tail = m.group(1), m.group(2), m.group(3)
        num = raw.replace(",", "")
        if "." in num:
            spoken = _decimal(num)
        else:
            try:
                n = int(num)
            except ValueError:
                return m.group(0)
            follow = tail + text[m.end():m.end() + 2]
            if 0 < n < 100 and any(follow.startswith(c.strip()) for c in _NATIVE_COUNTERS) and not follow.startswith("시간대"):
                spoken = "스무" if n == 20 else native(n)
            else:
                spoken = sino(n)
        if tail == "%":
            return spoken + " 퍼센트"
        if tail.startswith("월") and num in ("6", "10"):
            spoken = {"6": "유", "10": "시"}[num]
        if num == "1" and tail[:1] in ("만", "천", "백", "십"):  # 1만 -> 만, 1천만 -> 천만
            spoken = ""
        # 큰 단위(만·억·천)와 '여'는 붙이고, 나머지 단위(곳·원·명 등)는 띄워 읽어야 자연스럽다
        if tail and not sp and tail[:1] not in ("만", "억", "조", "천", "백", "십", "여", "월", "일"):
            sp = " "
        return spoken + sp + tail
    return _NUM.sub(rep, text)


def apply_dictionary(text, rules):
    """발음 사전 'A=B' 규칙 적용 (음성에만)."""
    for line in (rules or "").splitlines():
        if "=" in line:
            a, b = line.split("=", 1)
            if a.strip():
                text = text.replace(a.strip(), b.strip())
    return text


def tts_text(text, dictionary="", numbers=True):
    text = apply_dictionary(text, dictionary)
    if numbers:
        text = read_numbers(text)
    return re.sub(r"[\"“”‘’'\[\]{}<>*#_~`]", "", text)


# ---------------- 대본 자동 점검 ----------------

CLICKBAIT = ["충격", "경악", "소름", "미쳤", "역대급 폭락", "100%", "무조건 오른", "무조건 떨어", "확정", "반드시 오릅", "절대 안 떨어"]
INVEST = ["사세요", "파세요", "매수하세요", "매도하세요", "지금 사야", "지금 팔아야"]


def check_script(script, target_chars=None):
    """규칙 기반 점검. [{level, title, detail}] 목록 (level: ok/warn/bad)."""
    issues = []
    body = plain_text(script)
    n = char_count(script)
    sents = split_sentences(script)
    if target_chars:
        diff = (n - target_chars) / target_chars * 100
        if abs(diff) > 15:
            issues.append({"level": "warn", "title": "분량", "detail": f"목표 {target_chars:,}자 대비 {n:,}자 ({diff:+.0f}%)"})
    long_s = [s["text"] for s in sents if len(s["text"]) > 90]
    if long_s:
        issues.append({"level": "warn", "title": f"긴 문장 {len(long_s)}개", "detail": "듣기 편하도록 짧게 나누면 좋아요. 예: " + long_s[0][:60] + "…"})
    hook = " ".join(s["text"] for s in sents[:3])
    if re.search(r"안녕하세요|여러분 반갑|오늘은 .{0,12}(알아보|이야기)", hook):
        issues.append({"level": "warn", "title": "도입부", "detail": "첫 3문장에 인사·예고만 있어요. 바로 궁금증을 주는 문장으로 시작하면 이탈이 줄어요."})
    for w in CLICKBAIT:
        if w in body:
            issues.append({"level": "warn", "title": "과장 표현", "detail": f"'{w}' 같은 표현은 신뢰도와 광고 적합성을 떨어뜨릴 수 있어요."})
            break
    for w in INVEST:
        if w in body:
            issues.append({"level": "bad", "title": "투자 권유 표현", "detail": f"'{w}'처럼 매매를 직접 권하는 말은 피하는 게 안전해요."})
            break
    # 같은 문장 시작 반복
    starts = {}
    for s in sents:
        k = s["text"][:6]
        starts[k] = starts.get(k, 0) + 1
    rep = [k for k, v in starts.items() if v >= 6 and len(k) >= 4]
    if rep:
        issues.append({"level": "warn", "title": "반복되는 시작", "detail": f"'{rep[0]}…'로 시작하는 문장이 많아요."})
    secs = [t for t, _ in split_sections(script) if t]
    if n > 3000 and len(secs) < 3:
        issues.append({"level": "warn", "title": "소제목", "detail": "긴 영상은 '## 소제목'을 3개 이상 두면 챕터(타임스탬프)가 자동으로 만들어져요."})
    if not issues:
        issues.append({"level": "ok", "title": "통과", "detail": "규칙 점검을 모두 통과했어요."})
    return issues
