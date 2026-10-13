#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fidelity_check.py - TYPODER Korean Editor 대조·탐지 도구.

  compare 원문 수정문 [--genre 장르] [--terms 용어.txt] [--json]
      원문과 수정문의 문장을 짝지어 비교한다. 보호 항목(숫자·단위·날짜·인용·URL·코드·
      식별자·스타일 시트 용어)의 값 변경·소실·추가, 문장별 의미 경계 표현(부정·범위·양태·
      의무·시간·귀속·인과)의 변화, 지운 문장과 새로 쓴 문장, 어절 변경률, 문체 변화를 보고한다.
      종료 코드: 2 기계적으로 확인된 사실 변화(보호 항목) · 1 읽고 판단할 항목 있음(실패가 아니다) · 0 기계 검사에서 추가 신호 없음(뜻이 보존됐다는 증명은 아니다).
  scan 원고 [--genre 장르] [--para-mode auto|blank|line] [--json]
      AI 문체 패턴(R 잔재·U 대조 틀·L 부풀림·J 반복·Q 출처)과
      교정·교열 신호(P 표기·H 호응·W 어휘·S 문장 짜임·D 글 전체) 후보를 찾는다.
  selftest   규칙마다 붙은 '잡아야 할 예'와 '잡으면 안 되는 예'로 탐지기를 시험한다.
  rules      규칙 목록.

결과는 모두 후보다. 판단은 references의 기준과 문맥으로 한다. 정규식을 만족시키려고
맞는 문장을 고치지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# ================================================================ 텍스트 공통


def nfc(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    return unicodedata.normalize("NFC", text)


def split_paragraphs(text, mode="auto"):
    text = nfc(text).strip()
    if mode == "line" or (mode == "auto" and not re.search(r"\n[ \t]*\n", text)):
        blocks = text.split("\n")
    else:
        blocks = [re.sub(r"\s*\n\s*", " ", b) for b in re.split(r"\n[ \t]*\n", text)]
    return [b.strip() for b in blocks if b.strip()]


SENT_BOUNDARY = re.compile(r"(?<=[.?!…])\s+|(?<=[.?!…][”’」』\"'])\s+")


def split_sentences(par):
    """문장 경계: 마침표·물음표·느낌표·말줄임표 뒤 공백, 또는 그 뒤에 닫는 따옴표가 온 뒤의 공백('“가자.” 그가 말했다.')."""
    return [s for s in SENT_BOUNDARY.split(par.strip()) if s]


OPEN_CLOSE = {"“": "”", "「": "」", "『": "』", "‘": "’"}  # ‘ ’는 속마음·강조: 원문 그대로 둘 구역(짝 없는 ’는 영어 축약이라 무시)


def quote_mask(par):
    """큰따옴표·낫표 안(대사·직접 인용)인지 글자마다 표시한다."""
    mask, stack, straight, code = [], [], False, False
    for ch in par:
        if ch in OPEN_CLOSE:
            stack.append(OPEN_CLOSE[ch])
            mask.append(True)
        elif stack and ch == stack[-1]:
            stack.pop()
            mask.append(True)
        elif ch == '"':
            straight = not straight
            mask.append(True)
        elif ch == "`" and not stack and not straight:  # 인라인 코드도 원문 그대로 둘 구역
            code = not code
            mask.append(True)
        else:
            mask.append(bool(stack) or straight or code)
    return mask


def eojeol(s):
    return len(s.split())


def jong(ch):
    c = ord(ch) - 0xAC00
    return c % 28 if 0 <= c < 11172 else -1


J_N, J_L = 4, 8


def is_heading(line):
    if re.match(r"^\s*#{1,6}\s", line):
        return True
    return len(line) < 40 and bool(re.match(r"^\s*\d+(\.\d+)*[.)]?\s+\S", line)) and not re.search(r"[.?!]$", line)


def is_list_item(line):
    return bool(re.match(r"^\s*(?:[-*•·▪◦]|\d+[.)]|[가-하][.)])\s+", line))


def snip(s, n=50):
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


def is_question(s):
    return s.rstrip("”’\"').").endswith("?")


UNSUPPORTED = {
    ".hwp": "구형 한글(.hwp)은 읽지 못한다. 한글에서 '다른 이름으로 저장 → HWPX(.hwpx)'로 저장해 넣는다",
    ".doc": "Word 97-2003(.doc)은 읽지 못한다. Word에서 .docx로 저장해 넣는다",
    ".pdf": "PDF는 읽지 않는다. 원본 문서(.docx·.hwpx)나 글을 뽑은 .txt를 넣는다",
    ".pages": "Pages 문서는 읽지 못한다. .docx로 내보내 넣는다",
    ".odt": "ODT는 읽지 못한다. .docx로 저장해 넣는다",
    ".rtf": "RTF는 읽지 못한다. .docx나 .txt로 저장해 넣는다",
}


def read_text_any(path):
    """글 파일을 읽는다: UTF-8 → (표식이 있으면) UTF-16 → CP949(EUC-KR). 글이 아닌 파일(이진)은 읽지 않고 멈춘다.
    이진 파일을 글자로 잘못 읽으면 엉터리 결과를 조용히 내기 때문이다."""
    ext = Path(path).suffix.lower()
    if ext in UNSUPPORTED:
        raise ValueError("%s: %s" % (path, UNSUPPORTED[ext]))
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:4] == b"%PDF" or raw[:4] == b"\xd0\xcf\x11\xe0" or raw[:2] == b"PK" or b"\x00" in raw[:4096]:
        raise ValueError("%s: 글 파일이 아니다(PDF·구형 문서·압축 파일 등). .txt·.md·.docx·.hwpx를 넣는다" % path)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        print("알림: %s를 UTF-8이 아니라 CP949(EUC-KR)로 읽었다" % path, file=sys.stderr)
        return raw.decode("cp949", errors="replace")


def load_source(path):
    """원고 파일 → (텍스트, 위치 표시 목록 또는 None).
    .hwpx는 hwpx_tool.py text와 같은 문단 번호('32문단'), .docx는 track_changes.py extract와 같은 주소('document 3')로
    위치를 보인다. 그래서 스캔 결과의 위치를 편집 목록에 그대로 쓸 수 있다. .txt·.md는 위치 표시 없이 읽는다."""
    p, low = str(path), str(path).lower()
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    if low.endswith(".hwpx"):
        import hwpx_tool as ht
        state, paras, labels = {"ins": False, "del": False}, [], []
        for i, (_sec, el, _nested) in enumerate(ht.Hwpx(p).paragraphs(), 1):
            t = re.sub(r"\s*\n\s*", " ", ht.para_text(el, "accept", state)).strip()
            if t:
                paras.append(t)
                labels.append("%d문단" % i)
        return "\n\n".join(paras), labels
    if low.endswith(".docx"):
        import track_changes as tc
        d = tc.Docx(p)
        models = tc.build_models(d)
        paras, labels = [], []
        for alias, _name in tc.text_parts(d):
            for k, pm in enumerate(models.get(alias, [])):
                t = tc.to_display(pm.T).replace("⟦TAB⟧", " ").replace("⟦BR⟧", " ")
                t = re.sub(r"\s+", " ", re.sub(r"⟦[A-Z]+⟧", "", t)).strip()
                if t:
                    paras.append(t)
                    labels.append("%s %d" % (alias, k))
        return "\n\n".join(paras), labels
    return read_text_any(p), None


def attach_labels(doc, labels, nfiles):
    """원본 위치 표시를 문단에 붙인다. 문단 수가 어긋나면 그 파일은 원고 번호로 둔다."""
    if not labels:
        return
    per = labels if nfiles > 1 else [labels]
    flat = []
    for f in range(nfiles):
        n = sum(1 for x in doc.file_of if x == f)
        lab = per[f] if f < len(per) else None
        if lab is not None and len(lab) != n:
            print("주의: %d번째 원고의 문단 수가 원본과 달라(%d/%d) 위치를 원고 번호로 보인다" % (f + 1, n, len(lab)), file=sys.stderr)
            lab = None
        flat += list(lab) if lab is not None else [None] * n
    doc.labels = flat


FILE_SEP = re.compile(r"^⟦FILE:(\d+)⟧$")


class Doc:
    def __init__(self, text, para_mode="auto"):
        self.text, self._offs = nfc(text or ""), None  # 원문(NFC)과 문장별 글자 위치(offsets): 게이트가 줄바꿈을 지키며 문장만 갈아 끼우는 데 쓴다
        self.paras, self.file_of, cur = [], [], 0
        for p in split_paragraphs(text, para_mode):
            m = FILE_SEP.match(p)
            if m:
                cur = int(m.group(1))
                continue
            self.paras.append(p)
            self.file_of.append(cur)
        self.file_start = {}
        for pi, f in enumerate(self.file_of):
            self.file_start.setdefault(f, pi)
        self.names = None
        self.labels = None  # 원본 파일의 위치 표시(편집 도구와 같은 번호). 없으면 원고 문단 번호
        self.sents = [split_sentences(p) for p in self.paras]
        self.code, inside = set(), False  # 마크다운 코드 블록(``` ~~~) 안 문단: 교정 대상이 아니다
        for pi, par in enumerate(self.paras):
            if inside or par.lstrip().startswith(("```", "~~~")):
                self.code.add(pi)
            if len(re.findall(r"```|~~~", par)) % 2 == 1:
                inside = not inside
        self.masks = [[True] * len(p) if pi in self.code else quote_mask(p) for pi, p in enumerate(self.paras)]
        self.flat = [(pi, s) for pi, ss in enumerate(self.sents) for s in ss]

    def offsets(self):
        """flat 문장마다 원문(self.text)에서 차지하는 (시작, 끝) 글자 위치. 문장 나누기는 공백만 지우므로,
        공백을 뺀 글자를 차례로 맞추면 위치가 하나로 정해진다(줄바꿈·빈 줄은 문장 사이에 그대로 남는다)."""
        if self._offs is None:
            idx = [k for k, ch in enumerate(self.text) if not ch.isspace()]
            stream = "".join(self.text[k] for k in idx)
            pos, offs = 0, []
            for _p, s in self.flat:
                t = re.sub(r"\s+", "", s)
                k = stream.find(t, pos) if t else -1
                if k < 0:
                    raise ValueError("문장 위치를 찾지 못했다: %s" % snip(s, 30))
                offs.append((idx[k], idx[k + len(t) - 1] + 1))
                pos = k + len(t)
            self._offs = offs
        return self._offs

    def raw(self, i1, i2):
        """flat 문장 i1..i2-1이 차지하는 원문 구간(문단 안 줄바꿈·목록 번호 모양 그대로). 문장이 없으면 빈 글."""
        if i2 <= i1:
            return ""
        offs = self.offsets()
        return self.text[offs[i1][0]:offs[i2 - 1][1]]

    def loc(self, pi):
        f = self.file_of[pi] if 0 <= pi < len(self.file_of) else 0
        lab = self.labels[pi] if self.labels and 0 <= pi < len(self.labels) else None
        if lab:
            return ("%s %s" % (self.names[f], lab)) if self.names else lab
        local = pi - self.file_start.get(f, 0) + 1
        return ("%s %d문단" % (self.names[f], local)) if self.names else "%d문단" % local

    def quoted_span(self, pi, start, end):
        seg = self.masks[pi][start:max(start + 1, end - 1)]
        return bool(seg) and all(seg)

    def quoted_sent(self, pi, sent):
        k = self.paras[pi].find(sent)
        return k >= 0 and self.quoted_span(pi, k, k + len(sent))


# ================================================================ 규칙 틀


class Rule:
    """rid 첫 글자가 묶음: R 잔재 정리, U 대조·재정의 풀기, L 부풀림 점검, J 반복·리듬, Q 출처 확인(AI 문체) /
    P 표기, H 호응, W 어휘, S 문장 짜임, D 글 전체(교정·교열)."""

    def __init__(self, rid, name, level, tier, detect, pos=(), neg=(), min_count=1, quote_ok=False):
        self.rid, self.name, self.level, self.tier = rid, name, level, tier
        self.detect, self.pos, self.neg = detect, list(pos), list(neg)
        self.min_count, self.quote_ok = min_count, quote_ok
        self.only = None  # 이 규칙을 켤 장르(없으면 전체)
        self.role = "형태"  # 확정(형태만으로 규범 오류) / 형태(표현·틀의 자리와 횟수) / 읽기(뜻을 봐야 하는 곳의 후보)

    @property
    def group(self):
        return self.rid[0]

    def run(self, doc):
        """[(문단 번호(0부터), 보여 줄 글, 인용·대사 안인지)]"""
        if isinstance(self.detect, str):
            pat = re.compile(self.detect, re.M)
            hits = []
            for pi, par in enumerate(doc.paras):
                for mo in pat.finditer(par):
                    shown = mo.group(0).strip(" ,.") or mo.group(0)
                    if len(shown) < 6:  # '시'처럼 짧으면 무엇을 잡았는지 알 수 있게 앞뒤 문맥과 함께
                        shown = "%s ← …%s…" % (shown, par[max(0, mo.start() - 8):mo.end() + 8].strip())
                    hits.append((pi, shown, doc.quoted_span(pi, mo.start(), mo.end())))
        else:
            hits = self.detect(doc)
        if self.quote_ok:
            hits = [(pi, t, False) for pi, t, _q in hits]
        return hits




def sentence_detector(func):
    """func(sentence) -> 보여 줄 글 또는 None"""
    def detect(doc):
        out = []
        for pi, s in doc.flat:
            shown = func(s)
            if shown:
                out.append((pi, shown, doc.quoted_sent(pi, s)))
        return out
    return detect


# ================================================================ AI 문체 탐지기

REVEAL = re.compile(r"아닙니다|아니다|아니에요|바로|정답은|답은|있습니다\.|있다\.")


def det_u1(doc):
    out, i, flat = [], 0, doc.flat
    while i < len(flat):
        pi, s = flat[i]
        if is_question(s) and not doc.quoted_sent(pi, s):
            window = flat[i:i + 6]
            qs = [x for p, x in window if is_question(x) and not doc.quoted_sent(p, x)]
            shown, revealed = [], False
            for _p, x in window:
                shown.append(x)
                if not is_question(x) and REVEAL.search(x):
                    revealed = True
                    break
            if len(qs) >= 2 and revealed:
                out.append((pi, "질문 %d개 → %s" % (len(qs), snip(" ".join(shown), 90)), False))
                i += len(shown)
                continue
        i += 1
    return out


def det_u2(doc):
    out = []
    for pi, s in doc.flat:
        if (re.search(r"(?:가장|제일|진짜|정말|더|특히)\s?(?:좋은|중요한|놀라운|큰|재밌는|흥미로운|반가운)\s?"
                      r"(?:점|건|것|부분|이유)(?:은요|는요|은|는|요|이요)?\s?\?$", s)
                or re.search(r"^(?:그런데|근데|하지만)?\s?(?:핵심|결과|비결|정답|반전|포인트|차이|이유)(?:은|는)(?:요)?\?$", s)
                or re.fullmatch(r"(?:솔직히|진짜로|정말로)(?:요)?\?", s)):
            out.append((pi, s, doc.quoted_sent(pi, s)))
    return out


NEG_END = re.compile(r"(?:이|가)?\s?(?:아니다|아닙니다|아니에요|아니야|아니었다|아니었습니다)[.!]?$")
SHORT_DECL = re.compile(r"(?:다|입니다|이다|예요|이에요|죠|요)[.!]?$")
STRAW = re.compile(r"단순한|단순히|그저|그냥|평범한|흔한|또 하나의|또 다른|한낱|고작")
INFLATE = re.compile(r"혁명|패러다임|전부|모든 것|미래|방식 자체|게임|혁신|본질")


def _bare_nominal(b):
    """'방향입니다.' '새로운 파트너다.'처럼 주어·목적어 없이 짧게 이름만 대는 문장인가(U4 후보의 짧은 명사형 단언)."""
    toks = b.strip().split()
    if not toks or len(toks) > 5 or any(re.sub(r"[.,!?”’\"']", "", t).endswith(("을", "를")) for t in toks):
        return False
    if len(toks) > 1 and re.match(r"[가-힣]+(?:은|는|이|가)$", toks[0]) and not re.match(r"[가-힣]+(?:적|한|된|운)$", toks[0]):
        return False
    last = re.sub(r"[.!?”’\"')]+$", "", toks[-1])
    if last.endswith(("입니다", "이다", "예요", "이에요", "이야")):
        return True
    return last.endswith("다") and len(last) >= 2 and jong(last[-2]) == 0 and not last.endswith(("니다", "하다"))




def det_contrast_any(doc):
    """부정으로 끝난 문장 바로 뒤의 짧은 단언(대비·반전 후보)과 '모두 ~라고 말한다' 뒤 반박. 어떤 대비인지는 모델이 읽고 판단한다."""
    out = []
    for k in range(len(doc.flat) - 1):
        (pa, a), (_pb, b) = doc.flat[k], doc.flat[k + 1]
        if not (NEG_END.search(a.strip()) and eojeol(b) <= 10 and SHORT_DECL.search(b.strip())):
            continue
        cue = (re.search(r"(?:필요한|중요한|가져야 할|배워야 할|해야 할)\s?(?:건|것은|것이)", a)
               or re.search(r"(?:은|는) \S+(?:하는|한) (?:게|것이) 아(?:니|닙)", a) or STRAW.search(a) or INFLATE.search(b))
        if cue or _bare_nominal(b):
            out.append((pa, a + " " + b, doc.quoted_sent(pa, a)))
    return out + det_u7(doc)


def det_u7(doc):
    out = []
    for k, (pi, s) in enumerate(doc.flat):
        nxt = doc.flat[k + 1][1] if k + 1 < len(doc.flat) else ""
        if (re.search(r"(?:모두|다들|많은 사람들?(?:이|은)?|사람들은|누구나|흔히들?|대부분(?:의 사람)?)\s?.{0,40}(?:라고|다고) "
                      r"(?:말한다|말합니다|합니다|한다|생각한다|생각합니다|믿는다|믿습니다|입을 모)", s)
                and re.search(r"^(?:틀렸|아닙니다|아니다|천만에|착각|오해|과연 그럴까)", nxt.strip())):
            out.append((pi, s + " " + nxt, doc.quoted_sent(pi, s)))
    return out


def det_u8(doc):
    out = []
    for k, (pi, s) in enumerate(doc.flat):
        nxt = doc.flat[k + 1][1] if k + 1 < len(doc.flat) else ""
        if (re.search(r"대부분의 사람|보통 사람|평범한 사람|많은 사람", s)
                and re.search(r"성공한 사람|성공하는 사람|상위\s?\d+\s?%|부자들?|고수들?|일류|(?:일을? )?잘하는 사람", s + " " + nxt)):
            out.append((pi, s + " " + nxt, doc.quoted_sent(pi, s)))
    return out


def det_u9(doc):
    out = []
    for k, (pi, s) in enumerate(doc.flat):
        nxt = doc.flat[k + 1][1] if k + 1 < len(doc.flat) else ""
        if (re.search(r"(?:멈추|멈춰|그만두|그만하|버리)(?:세요|십시오|라|어라)", s)
                and re.search(r"(?:시작하|택하|해 보)(?:세요|십시오|라)", s + " " + nxt)):
            out.append((pi, s + " " + nxt, doc.quoted_sent(pi, s)))
    return out


def det_j1(doc):
    out, run = [], []

    def flush():
        if len(run) >= 3:
            out.append((run[0], "%d문단 연속: %s" % (len(run), snip(" / ".join(doc.paras[x] for x in run), 90)), False))

    for pi, par in enumerate(doc.paras):
        one = (len(doc.sents[pi]) == 1 and eojeol(par) <= 15 and not is_heading(par)
               and not is_list_item(par) and not par.startswith(("“", "\"", "「", "-", "―")))
        if one:
            run.append(pi)
        else:
            flush()
            run = []
    flush()
    return out


def det_j2b(doc):
    out = []
    for pi, par in enumerate(doc.paras):
        if len(doc.sents[pi]) == 1 and re.search(
                r"^(?:이게|이것이|그게|이것이야말로) (?:핵심|전부|포인트|답)(?:이다|입니다|이에요|예요|다)?[.!]?$|"
                r"다시 (?:읽어|생각해) ?보(?:세요|라|십시오)|^기억하(?:세요|라|십시오)", par):
            out.append((pi, par, doc.quoted_sent(pi, par)))
    return out


def det_j3(doc):
    out, f = [], doc.flat
    for k in range(len(f) - 2):
        a, b, c = f[k][1], f[k + 1][1], f[k + 2][1]
        if max(eojeol(a), eojeol(b), eojeol(c)) <= 10 and re.search(r"(?:^|\s)더 ", b) and re.search(r"가장|최고|제일", c):
            out.append((f[k][0], " ".join((a, b, c)), doc.quoted_sent(f[k][0], a)))
    return out


ABSTRACT = r"혁신|영감|통찰|성장|도약|변화|가치|비전|열정|소통|협업|창의|효율|성과|신뢰|공감|재미|감동|행복|자유"


def det_j4(doc):
    out = []
    pat = re.compile(r"(\S+?)(?:,|·|와|과)\s?(\S+?)(?:,|·)?\s?(?:그리고|및|또)?\s?(\S+?)(?:을|를|이|가|은|는|의|까지|에)\s")
    for pi, s in doc.flat:
        mo = pat.search(s)
        if mo and sum(bool(re.match(ABSTRACT, g)) for g in mo.groups()) >= 2:
            out.append((pi, s, doc.quoted_sent(pi, s)))
    triads = [(pi, "서술어 셋 병렬: " + snip(s, 50), doc.quoted_sent(pi, s)) for pi, s in doc.flat if TRIAD_PRED.search(s)]
    if len([t for t in triads if not t[2]]) >= 2:  # 광고성 3단 리듬은 글 안에서 반복될 때만
        out += triads
    return out


def det_j5c(doc):
    seen = {}
    for pi, par in enumerate(doc.paras):
        for mo in re.finditer(r"\d+(?:\.\d+)?\s?(?:%|퍼센트|배|명|분|시간|개월|원|건)", par):
            seen.setdefault(mo.group(0).replace(" ", ""), []).append(pi)
    return [(v[0], "%s (%s문단)" % (k, ",".join(str(x + 1) for x in v)), False) for k, v in seen.items() if len(v) >= 2]


# ================================================================ 교정·교열 탐지기


CONFUSE = [
    (r"(?:카드|현금|계좌|간편|모바일|온라인|대금|요금|페이)[가-힣\s]{0,6}결재", "결제(돈을 치름)", "확실"),
    (r"결제를 (?:올리|올렸|받|맡|요청)|(?:기안|품의|보고서)[가-힣\s]{0,6}결제", "결재(윗사람의 승인)", "문맥 확인"),
    (r"(?:남용|과소비|폭력|차별|낭비|편견|과음|과속)[을를]? ?지향", "지양(피함)", "문맥 확인"),
    (r"(?:능력|재능|소질|잠재력|자기)[을를]? ?개발", "계발(재능·능력을 일깨움)", "문맥 확인"),
    (r"(?:시간|기간|인원|규모|수량|매출|재산|실력|거리)[을를]? ?늘(?:여|이)", "늘려·늘리(수나 양을 많게)", "확실"),
    (r"(?:정답|답|문제|퀴즈)[을를]? ?(?:맞추|맞췄|맞춰)", "맞히·맞혔(정답을 고름)", "확실"),
    (r"(?:편지|소포|택배|원고)[을를]? ?(?:붙이|붙였|붙여)|(?:회의|안건|표결|투표|재판)에 (?:붙이|붙였|붙여)",
     "부치·부쳤(보내다·회부하다)", "확실"),
    (r"(?:생각|의견|입장|취향|방식|문화|성격)[이가은는]? ?(?:서로 )?틀리", "다르(같지 않음)", "확실"),
    (r"(?:무엇|뭐|뭘|어디|누구|언제|어떻게|얼마)[가-힣]*\s[가-힣]*던지(?! 간에)|(?:하|가|오|먹)던지 말던지",
     "든지(고르는 뜻)", "문맥 확인"),
    (r"일체 (?:금지|없|사절|불가|하지)", "일절(전혀·절대로)", "확실"),
    (r"있다가 (?:봐|보자|만나|연락|갈게|할게)", "이따가(조금 뒤에)", "문맥 확인"),
    (r"(?:산|고개|담|창)\s?넘어(?:로|에|의)", "너머(공간 저쪽)", "확실"),
]




NEG_WORDS = re.compile(r"않|없|못|아니|아닙|(?<![가-힣])안 |마(?:세요|라|십시오)|말(?:고|라|아야)|금지|불가|모르|어렵")
H3_TABLE = [
    (re.compile(r"결코|좀처럼|조금도|도무지|차마|여간|추호도|결단코|절대로"), NEG_WORDS, "부정 서술어가 따라와야 함"),
    (re.compile(r"(?<![가-힣])(?:전혀|별로|그다지)(?! (?:다른|딴|새로운|반대|엉뚱|모르는|낯선|예상))"), NEG_WORDS,
     "주로 부정 서술어와 어울림"),
    (re.compile(r"왜냐하면"), re.compile(r"때문|거든|기에|므로|니까|어서|아서|해서|이라서|라서"), "'왜냐하면 ~때문이다'"),
    (re.compile(r"(?<![가-힣])비록"), re.compile(r"지만|어도|아도|해도|더라도|라도|지라도|으나|는데|은데|던데"),
     "'비록 ~지만/~더라도'"),
    (re.compile(r"(?<![가-힣])(?:만약|만일)"), re.compile(r"면|거든|경우|때|다면|라면"), "'만약 ~면'"),
]




H1_SUBJ = re.compile(r"(?:특징|장점|단점|강점|약점|요지|취지|이유|차이점|공통점)(?:은|는)\s")
H1_OK_END = re.compile(r"(?:이다|였다|이었다|입니다|였습니다|이었습니다|예요|이에요|이지요|이죠|이야|이라고|인데|거다|거예요)$")
H1_SKIP = re.compile(r"되|됐|진다|졌|받|당하|밝혀|알려|드러나|달라|바뀌|없|있|많|적|크|작|분명|명확|간단|같|다르|뚜렷|여러")




UI_NOUNS = ("회의", "문의", "논의", "동의", "정의", "주의", "합의", "협의", "강의", "토의", "건의", "편의", "거의",
            "이의", "질의", "결의", "호의", "성의", "창의", "민의")


def _genitive(tok):
    tok = tok.strip(",.")
    return tok.endswith("의") and len(tok) >= 2 and not any(tok.endswith(n) for n in UI_NOUNS)


def det_s3a(doc):
    out = []
    for pi, par in enumerate(doc.paras):
        toks = par.split()
        for a, b in zip(toks, toks[1:]):
            if _genitive(a) and _genitive(b):
                out.append((pi, a + " " + b.strip(",."), False))
    return out










def det_s4a(doc):
    return [(pi, "%d어절: %s" % (eojeol(s), snip(s, 50)), doc.quoted_sent(pi, s)) for pi, s in doc.flat if eojeol(s) >= 30]


def det_s4b(doc):
    out = []
    for pi, s in doc.flat:
        n = len(re.findall(r"[가-힣](?:으며|며|면서|함으로써|하는 가운데)(?=[\s,])", s))
        if n >= 3:
            out.append((pi, "연결 어미 %d개: %s" % (n, snip(s, 50)), doc.quoted_sent(pi, s)))
    return out


ENDINGS = [
    ("합니다체", None),
    ("해요체", re.compile(r"(?:요|죠)[.!]?[”’\"')]*$")),
    ("한다체", re.compile(r"(?:는다|ㄴ다|었다|았다|였다|이다|했다|한다|된다|진다|있다|없다|겠다|다)[.!]?[”’\"')]*$")),
]


J_B = 17  # 받침 ㅂ
HAPNIDA_TAIL = re.compile(r"([가-힣])(?:니다|니까)[.!?]?[”’\"')]*$")


def is_hapnida(s):
    """합니다체 끝: '습니다·습니까', 또는 받침 ㅂ 음절 뒤 '니다·니까'. 끝 두 글자만 보면 '아니다·다니다·지니다'를 잘못 잡는다."""
    s = s.strip()
    if re.search(r"(?:습니다|습니까)[.!?]?[”’\"')]*$", s):
        return True
    m = HAPNIDA_TAIL.search(s)
    return bool(m) and jong(m.group(1)) == J_B


def register_of(s):
    body = s.strip()
    if not body or re.search(r"[?？][”’\"')]*$", body) or re.search(r"(?:세요|십시오|시죠|합시다|읍시다)[.!]?$", body):
        return None
    if body.startswith(("“", "\"", "「", "『", "‘")) or re.search(r"[”」』\"]\s?(?:라고|하고|며|고)\s", body):
        return None
    for name, pat in ENDINGS:
        if (is_hapnida(body) if pat is None else pat.search(body)):
            return name
    return None


def register_counts(doc):
    return Counter(r for pi, s in doc.flat if not doc.quoted_sent(pi, s) for r in [register_of(s)] if r)


def det_d1(doc):
    regs = [(pi, s, register_of(s)) for pi, s in doc.flat if not doc.quoted_sent(pi, s)]
    c = Counter(r for _p, _s, r in regs if r)
    if len(c) < 2:
        return []
    main, n = c.most_common(1)[0]
    if n < 3:
        return []
    return [(pi, "%s (주 문체 %s)" % (snip(s, 50), main), False) for pi, s, r in regs if r and r != main]


def det_d3(doc):
    starts = Counter()
    where = {}
    for pi, s in doc.flat:
        mo = re.match(r"(또한|그리고|한편|그러나|하지만|게다가|더불어)[, ]", s)
        if mo:
            starts[mo.group(1)] += 1
            where.setdefault(mo.group(1), pi)
    return [(where[w], "문장 첫머리 '%s' %d회" % (w, n), False) for w, n in starts.items() if n >= 3]


def det_p5(doc):
    text = "\n".join(doc.paras)
    out = []
    if re.search(r"\d\s?%", text) and "퍼센트" in text:
        out.append((0, "'%'와 '퍼센트' 혼용", False))
    if re.search(r"저희 (?:팀|회사|부서)", text) and re.search(r"우리 (?:팀|회사|부서)", text):
        out.append((0, "'저희'와 '우리' 혼용(필자 선택 확인)", False))
    return out


# ================================================================ 규칙 표

RULES = [
    # ---- R 잔재 정리
    Rule("R1", "챗봇 잔재", "강", "권장",
         r"물론입니다[!.]|좋은 질문(?:입니다|이에요|이네요)|요청하신 .{0,30}(?:작성|정리|요약|번역|수정)해 드리겠습니다|"
         r"(?:AI|인공지능) (?:언어 )?모델로서|더 궁금한 (?:점|것)이 있으시면|도움이 필요하시면 언제든",
         ["물론입니다! 요청하신 보고서를 정리해 드리겠습니다."], ["물론 주의할 점도 있습니다."]),
    Rule("R1b", "안내·진행 예고 문구", "약", "제안",
         r"도움이 되(?:셨|었)기를|궁금한 점이 있으시면|언제든(?:지)? (?:문의|물어)|(?:에 대해|을|를) (?:알아보|살펴보)겠습니다|"
         r"이 글에서는 .{0,30}(?:알아보|살펴보|소개하)|오늘은 .{0,30}(?:말씀드리|소개해 드리)겠습니다",
         ["이 글에서는 재택근무의 장단점을 알아보겠습니다."], ["궁금한 점은 댓글로 남겨 주세요."]),
    Rule("R2a", "챗봇의 자기 한계 고지", "중", "권장",
         r"(?:제|저의) (?:학습 데이터|학습된 (?:데이터|정보)|지식)|학습 데이터 기준|제가 가진 정보는|실시간 (?:정보|검색)(?:에|을|를|이) (?:접근|확인|불가)|"
         r"최신 정보(?:를|는) (?:알 수 없|반영하지 못|제공하기 어렵)|지식 (?:기준일|컷오프)",
         ["제 학습 데이터 기준으로는 2023년까지의 정보만 있습니다.", "최신 정보는 알 수 없지만 대략 이렇습니다."],
         ["이 데이터 기준으로 2023년까지 집계했다. 최신 정보는 표 3에 있다."]),
    Rule("R2b", "한계 고지(자료·정보 부족)", "중", "확인",
         r"정확한 정보는 (?:확인|알려져)|공개된 (?:자료|정보)가 (?:제한|부족)|알려진 바가 (?:많지|없)|최신 정보가 아닐 수",
         ["공개된 자료가 제한적이어서 정확한 수치는 알 수 없습니다."], ["정확한 수치는 표 2에 있습니다."]),
    Rule("R3", "장식 서식(굵은 라벨·이모지 제목)", "약", "제안",
         r"\*\*[^*\n]{1,40}?[:：]\*\*|\*\*[^*\n]{1,40}\*\*\s?[:：]|^\s*#{1,6}\s*[\U0001F300-\U0001FAFF\u2600-\u27BF]|^[\U0001F300-\U0001FAFF\u2705\u2728\u2B50]",
         ["**핵심:** 핵심은 비용이다.", "**핵심**: 비용이다."], ["비용이 핵심이다.", "메모지에 굵은 글씨가 남아 있었다. **내일 오전 9시 / 3층 회의실**"]),
    # ---- U 대조·재정의 풀기
    Rule("U1", "연속 질문 후 정답 공개", "강", "제안", det_u1,
         ["시간이 없으신가요? 돈도 부족하신가요? 방법은 없을까요? 있습니다. 바로 자동화입니다."],
         ["그렇다면 모든 업무를 맡겨도 될까요? 저는 아니라고 봅니다. 판단은 사람의 몫이기 때문입니다."]),
    Rule("U2", "예고형 질문 다리", "중", "제안", det_u2,
         ["그런데 가장 놀라운 점은요? 비용이 절반으로 줄었습니다."],
         ["“근데 진짜 좋은 건요? 퇴근이 빨라졌어요.”라고 말했다."]),
    Rule("U3", "진실·본질 폭로", "중", "제안",
         r"(?:^|(?<=\s))(?:진실|사실|진짜 이유|진짜 문제|본질|핵심)(?:은|는|이란)\s?(?:\?|요\?|무엇일까|뭘까|이렇|이것|이거|간단|하나)",
         ["진실은 이렇습니다. 계획은 대부분 실행에서 무너집니다."], ["사실은 회의가 한 시간 늦게 시작됐다."]),
    Rule("U4", "부정 문장 뒤 짧은 단언(대비·반전 후보)", "중", "제안", det_contrast_any,
         ["문제는 속도가 아닙니다. 방향입니다.", "이 팀에 모자란 것은 인력이 아닙니다. 순서입니다.",
          "다들 장비가 먼저라고 생각합니다. 착각입니다. 기본기가 먼저입니다."],
         ["그는 학생이 아니다. 나는 매일 이 길을 다닌다.", "이 방법은 빠르다. 다만 비용이 든다."]),
    Rule("U4x", "'단순히 ~를 넘어'류 대비 틀", "약", "제안",
         r"단순(?:한|히)\s?\S+\s?(?:이|가)?\s?아니라|단순히 .{0,12}(?:를|을) 넘어|(?:에|데) 그치지 않고|"
         r"(?:보다|이 아니라) 중요한 (?:것|건)은|중요한 (?:것|건)은 [^.?!]{1,30}(?:이|가)? 아니라|문제는 [^.?!]{1,40}(?:데|것)에? 있는 (?:것이|게) 아니",
         ["이 변화는 시간을 줄이는 데 그치지 않고 문화를 바꾼다.", "중요한 것은 기술이 아니라 쓰는 방식이다."],
         ["이 변화로 시간이 줄었다. 중요한 것은 마감이다."]),
    Rule("U7w", "통념 세우기 문구", "약", "제안", r"오해하지 마(?:세요|십시오)|라고 생각할 수도 있지만|라고 착각하기 쉽",
         ["돈이 전부라고 생각할 수도 있지만 그렇지 않다."], ["돈이 전부는 아니다."]),
    Rule("U8", "'다수 대 잘하는 사람' 이분법", "중", "제안", det_u8,
         ["보통 사람은 월급을 모읍니다. 부자들은 시간을 삽니다."], ["많은 사람이 계획을 세우지만 실행은 어렵다."]),
    Rule("U9", "대칭 명령 전환(그만두기 → 시작하기)", "강", "제안", det_u9,
         ["핑계는 이제 그만두세요. 오늘 해 보세요."], ["작업을 멈추고 전원을 차단하십시오."]),
    # ---- L 부풀림·범용 표현 점검
    Rule("L1", "거창한 의미 부여", "중", "제안",
         r"새로운 패러다임|중요한 시사점|시사하는 바가 크|귀추가 주목|밝은 미래|빠르게 변화하는|현대 사회에서|"
         r"중요성이 (?:더욱 )?커지|한 획을 긋|새로운 장을 열|기념비적|역사적인 (?:순간|사건)|게임 체인저|판도를 바꾸|"
         r"전환점을 맞|무한한 가능성|무궁무진|혁명(?=이|적|을|입|의)|매우 (?:중요한|큰) 의미",
         ["업무 혁신의 새로운 패러다임이 열렸다."], ["새로운 결제 방식이 도입됐다."]),
    Rule("L2", "홍보·과장 수식어", "약", "제안",
         r"혁신적인|획기적인|놀라운|탁월한|압도적인|고무적인|괄목할 만한|독보적인|(?:를|을) 자랑|에 자리 잡은|최고의",
         ["획기적인 기능을 자랑합니다."], ["기능이 세 가지 있습니다."]),
    Rule("L2b", "이름 없는 '다양한' 반복", "약", "제안", r"다양한",
         ["다양한 기능과 다양한 혜택, 다양한 서비스가 있다."], ["다양한 의견이 나왔다."], min_count=3),
    Rule("L2c", "업무 외래어 겹침", "약", "제안", r"니즈|솔루션|시너지|로드맵|인사이트|레버리지|퍼포먼스|밸류|어젠다",
         ["고객 니즈에 맞춘 솔루션으로 시너지를 낸다."], ["고객 요구에 맞춘 해결책을 낸다."], min_count=3),
    Rule("L3", "빈 틀·겹친 추측", "중", "권장",
         r"(?:라|다)고 할 수 있|(?:라|다)고 볼 수 있|할 수도 있을 것|것으로 보일 수|이는 .{0,40}(?:를|을) 의미|라는 점에서 의미가 있",
         ["이는 매우 의미 있는 성과라고 할 수 있습니다."], ["이 도구로 초안을 만들 수 있습니다."]),
    # ---- J 반복·리듬 정리
    Rule("J1", "한 줄 한 문단 연속", "중", "제안", det_j1,
         ["처음엔 몰랐다.\n\n써 보고 알았다.\n\n생각보다 강력했다.\n\n모든 것이 바뀌었다."],
         ["처음엔 몰랐다. 써 보고 알았다.\n\n생각보다 강력했다."]),
    Rule("J2", "빈 결론 표지", "중", "권장",
         r"(?:^|(?<=[.?!]\s))(?:결론적으로|요약하자면|요컨대|정리하자면|이처럼|이렇듯)(?=[, ])|과언이 아니",
         ["결론적으로, 이 도구는 쓸 만하다."], ["결론은 다음 회의에서 정한다."]),
    Rule("J2b", "한 줄 마무리 강조", "강", "제안", det_j2b,
         ["이것이 핵심입니다."], ["이것이 핵심 지표인 매출입니다. 다음 분기에 다시 봅니다."]),
    Rule("J3", "3단 점층", "강", "제안", det_j3, ["빠르다. 더 빠르다. 가장 빠르다."], ["빠르다. 비용도 적다. 설치가 쉽다."]),
    Rule("J4", "추상명사 삼단 나열", "중", "제안", det_j4,
         ["이 도구로 혁신, 효율, 그리고 성장까지 잡을 수 있다."], ["고객 이름, 주문 번호, 문의 날짜를 먼저 확인한다."]),
    Rule("J4b", "쓸모없는 '첫째·둘째·셋째'", "약", "제안", r"첫째.{0,300}둘째.{0,300}셋째",
         ["첫째, 빠르다. 둘째, 싸다. 셋째, 쉽다."], ["빠르고 싸고 쉽다."]),
    Rule("J5c", "같은 수치 되풀이", "약", "제안", det_j5c,
         ["응답 시간이 40% 줄었다. 앞서 말했듯 40%나 줄었다."], ["응답 시간이 40% 줄었다. 비용은 10% 늘었다."]),
    # ---- Q 출처 확인
    Rule("Q1", "출처 없는 권위", "중", "확인",
         r"전문가들(?:은|에 따르면|이)|업계(?:에서는| 관계자들은)|(?:많은|여러) 연구(?:에 따르면|들이)|일각에서는|많은 이들이|입을 모(?:은|읍|아)",
         ["전문가들은 3년 안에 모두가 AI를 쓸 것이라고 전망한다."], ["한국은행은 물가가 안정될 것이라고 전망했다."]),
    # ---- P 표기
    Rule("P3a", "문장 안 영어식 쌍점", "중", "권장", r"[가-힣]다: [가-힣]", ["결론은 하나다: 실행이다."], ["장소: 3층 회의실"]),
    Rule("P3b", "느낌표·물음표 겹침", "약", "제안", r"[!?]{2,}", ["정말 대단하다!!"], ["정말 대단하다!"]),
    Rule("P3e", "보이지 않는 문자(폭 없는 공백 등)", "중", "확실",
         lambda doc: [(pi, "U+%04X: %s" % (ord(m.group(0)), par[max(0, m.start() - 6):m.end() + 6].replace(m.group(0), "⟦보이지 않음⟧")), False)
                      for pi, par in enumerate(doc.paras) for m in re.finditer("[\u200b\u2060\ufeff]", par)],
         ["무리라는\u200b건 알았다."], ["무리라는 건 알았다. 가족 사진 👨\u200d👩\u200d👧을 올렸다."]),
    Rule("P5", "표기 혼용", "약", "권장", det_p5, ["성장률은 40%다. 이익률은 20퍼센트다."], ["성장률은 40%다. 이익률은 20%다."]),
    # ---- H 호응
    # ---- W 어휘
    Rule("W2a", "번역투 '~에 있어(서)'", "중", "권장",
         r"(?:함|기|것|데|점|측면|과정|문제|교육|경영|운영|관계|분야)에 있어(?:서)?(?=[\s,의])|"
         r"[가-힣]+에 있어(?:서)?(?=,|\s(?:가장|중요|핵심|필요|무엇보다|특히|우선))",
         ["도입함에 있어서 중요한 것은 준비다.", "품질 관리에 있어서 핵심은 기록이다."], ["서울에 있어서 가깝다."]),
    Rule("W2b", "번역투 연결 사슬", "중", "권장", sentence_detector(
        lambda s: ("연결 %d개: %s" % (n, snip(s, 60))) if (n := len(re.findall(
            r"에 대한|에 대해|(?:을|를) 통해|(?:을|를) 통하여|(?:와|과) 관련(?:하여|된|한)|에 기반(?:하여|한|해)|에 있어", s))) >= 2 else None),
        ["경험에 대한 이야기를 통해 얻은 교훈에 대해 나누겠다."], ["저작권에 대한 논의가 이어졌다."]),
    Rule("W2c", "번역투 서술어", "중", "권장",
         r"필요로 하|에 위치한|(?:회의|시간|모임|간담회|기자회견|논의)(?:을|를) 가(?:졌|진|지|집|질)|"
         r"(?:회의|시간|모임|간담회|기자회견)(?:을|를) 갖|의미를 가(?:지|진|집|졌|질)|문제가 존재",
         ["매주 회의를 가집니다."], ["매주 회의를 엽니다."]),
    Rule("W2d", "부사로 쓴 '보다'", "약", "권장", r"(?:^|(?<=\s))보다 [가-힣]+(?:적인|적으로|한|하게)",
         ["보다 창의적인 업무에 집중한다."], ["예상보다 창의적인 결과가 나왔다."]),
    Rule("W2e", "번역 투 '당신'", "약", "권장", r"당신(?:의|은|이|을|에게|도|만)?(?=\s)",
         ["당신의 팀도 할 수 있습니다."], ["여러분의 팀도 할 수 있습니다."]),
    Rule("W3a", "'~를 진행하다'류 명사화", "중", "권장",
         r"(?:검토|논의|협의|분석|조사|개선|확인|점검|평가)(?:를|을) (?:진행|실시|수행)(?:하|했|한|할|합|해)",
         ["자료 검토를 진행했다."], ["자료를 검토했다."]),
    Rule("W3b", "'~이 가능하다'류 명사화", "약", "권장", r"[가-힣]{2}(?:이|가) 가능(?:하|해|합|했|한|할)",
         ["재사용이 가능한 용기다."], ["다시 쓸 수 있는 용기다."]),
    Rule("W3c", "'~ 시' 명사화", "약", "제안", r"(?<=[가-힣]{2})(?<!다섯)(?<!여섯)(?<!일곱)(?<!여덟)(?<!아홉)(?<!열한)(?<!열두)(?<!스무) 시(?=에는|에|,|\s)",
         ["방한 시 협의할 예정이다."], ["오후 3시에 만난다. 아홉 시에 만났다. 새벽 다섯 시 사십 분이었다. 열한 시, 그는 떠났다."]),
    Rule("W3d", "'-성' 추상명사 반복", "약", "제안", r"(?:가능|중요|필요|효율|연관|용이|편의|안정|정확|효과|생산)성",
         ["가능성과 중요성, 필요성과 효율성을 따졌다."], ["효율성이 높다."], min_count=4),
    Rule("W3e", "'~한 관계로'", "중", "권장", r"[가-힣]+(?:한|된|은|는|던|인) 관계로",
         ["공사가 지연된 관계로 개장을 미뤘다."], ["두 사람의 관계로 보아 협력이 쉽다."]),
    Rule("W3f", "'~의 경우'", "약", "제안", r"(?<!최악)(?<!최선)(?<!만일)의 경우(?:에는|에|,|\s)",
         ["신입 사원의 경우 교육을 받는다."], ["최악의 경우 서비스가 멈춘다."]),
    # ---- S 문장 짜임
    Rule("S1a", "진행형 '~ 중에 있다'", "중", "권장",
         r"(?:개발|검토|건설|건립|공사|수리|점검|조사|준비|증설|진행|추진|협의) 중에 있",
         ["회사는 새 공장을 건설 중에 있다."], ["참가자 중에 있는 한 명을 뽑는다."]),
    Rule("S1b", "불필요한 '-시키다'", "중", "권장",
         r"(?:소개|설득|입금|개선|주차|해소|금지|공유)시(?:키|켜|킨|킬|켰)",
         ["친구를 설득시켰다."], ["매출을 증가시켰다. 부모가 자녀를 교육시켰다."]),
    Rule("S2b", "자동사에 붙인 '-되다'", "약", "권장",
         r"(?:증가|감소|발생|성장|하락|상승|확대|축소|증대|급증|급감)(?:되|된|됐|됨)", ["매출이 증가되었다."], ["매출이 증가했다."]),
    Rule("S2c", "'~에 의해' 피동 구문", "약", "제안", r"에 의해(?:서)? [가-힣]+(?:되|됐|된|받|당)",
         ["보고서는 팀장에 의해 승인되었다."], ["팀장이 보고서를 승인했다."]),
    Rule("S3a", "'의' 연쇄", "중", "권장", det_s3a,
         ["우리 팀의 업무의 대부분이 바뀌었다."], ["팀장님의 결정 덕분이다. 회의의 결론이 나왔다."]),
    Rule("S3b", "'것' 연쇄", "중", "권장", r"(?:는|은|을) 것의|것은 .{0,30}(?:는|다는) 것이다",
         ["AI를 활용하는 것의 장점은 효율을 높인다는 것이다."], ["가장 힘들었던 것은 양식을 고치는 일이었다."]),
    Rule("S3c", "복수 '들' 겹침", "약", "권장", r"[가-힣]+들(?:의|에|을|를|이|은|과|와) [가-힣]+들",
         ["고객들의 문의들에 답했다."], ["고객 문의에 답했다."]),
    Rule("S4a", "긴 문장(30어절 이상)", "약", "권장", det_s4a,
         [" ".join(["가"] * 31) + "다."], ["짧은 문장이다."]),
    Rule("S4b", "연결 어미 사슬", "약", "권장", det_s4b,
         ["자료를 모으며 분석하고, 보고서를 쓰면서 발표를 준비하며 회의를 열었다."], ["자료를 모아 분석했다."]),
    Rule("S6a", "번역 투 3인칭 대명사", "약", "제안", r"그녀(?:의|는|가|를|에게|와|도)?(?=\s)",
         ["그녀는 보고서를 제출했다."], ["김 과장은 보고서를 제출했다."]),
    Rule("S6b", "사물 주어 의인화", "약", "권장",
         r"(?:결과|자료|데이터|수치|연구|조사|기술|통계)(?:는|은|가|이) .{0,30}(?:말해 준|말해 줍|말해준|가능하게 한|가능케)",
         ["이번 통계는 야간 이용이 늘었다는 것을 말해 준다."], ["이번 통계로 야간 이용이 늘었다는 것을 알 수 있다."]),
    # ---- D 글 전체
    Rule("D1", "문체 섞임", "중", "권장", det_d1,
         ["회의는 3시에 시작합니다. 자료를 먼저 배포합니다. 참석자는 자료를 준비한다. 장소는 3층입니다."],
         ["회의는 3시에 시작합니다. 자료를 먼저 배포합니다. 참석해 주세요. 장소는 3층입니다.", "그는 회사에 다닌다. 그는 학생이 아니다. 우리는 같은 기억을 지닌다. 결과는 분명하다."]),
    Rule("D3", "기계적 접속어 반복", "약", "제안", det_d3, ["또한 A다. 또한 B다. 또한 C다."], ["또한 A다. 그러나 B다. 따라서 C다."]),
]
# ================================================================ 2.2: 일반 글 AI 문체 · 소설 서술(N) · 문서 전체 지표 · 소설 보고

# ---- J5 문장 골격 반복 (일반 글·소설 공용). 낱말을 가리고 틀을 비교한다.
FRAME = set("문제는 중요한 핵심은 이유는 그제야 이제 아직 결국 어쩌면 분명 분명히 정말 너무 다시 또 이미 그리고 하지만 그러나 "
            "그래서 그런데 마치 아마 아무도 누구도 아무것도 모든 단지 오직 절대 결코 차라리 오히려 비로소 역시".split())
FRAME_ADV = set("그제야 결국 어쩌면 분명 분명히 이제 아직 마치 문제는 오히려 차라리 이미 단지 비로소 역시 결코".split())
PARTICLES = ("에서", "에게", "으로", "까지", "부터", "처럼", "보다", "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만")
COMMON_PRED = set("있었다 없었다 했다 였다 이었다 되었다 됐다 같았다 보였다 있다 없다 한다 된다 이다 있습니다 없습니다 합니다 됩니다 "
                  "했습니다 였습니다 입니다 있어요 없어요 해요".split())


def _pred_class(tok):
    t = re.sub(r"[.!?…\"'”’)]+$", "", tok)
    if tok.rstrip().endswith("?") or t.endswith("까"):
        return "V?"
    if t.endswith("다"):
        c = ord(t[-2]) - 0xAC00 if len(t) >= 2 else -1
        return "V었다" if 0 <= c < 11172 and c % 28 == 20 else "V다"  # 받침 ㅆ이면 과거
    return "V요" if t.endswith("요") else "V"


def skeleton(s):
    toks = s.split()
    out = []
    for i, tok in enumerate(toks):
        t = re.sub(r"[.,!?…\"'“”’‘()]", "", tok)
        if t in FRAME:
            out.append(t)
        elif i == len(toks) - 1:
            out.append(_pred_class(tok))
        else:
            out.append("X" + next((x for x in PARTICLES if t.endswith(x) and len(t) > len(x)), ""))
    return " ".join(out)


def det_j5(doc):
    out = []
    flat = [(pi, s) for pi, s in doc.flat if not doc.quoted_sent(pi, s)]
    k = 0
    while k <= len(flat) - 3:
        trio = [s for _p, s in flat[k:k + 3]]
        heads = [x.split()[0] if x.split() else "" for x in trio]
        lasts = [re.sub(r"[.!?…\"'”’)]+$", "", x.split()[-1]) if x.split() else "" for x in trio]
        tails = [" ".join(re.sub(r"[.!?”’\"')]+$", "", x).split()[-2:]) for x in trio]
        sk = [skeleton(x) for x in trio]
        why = None
        if len(heads[0]) > 1 and len(set(heads)) == 1 and len(set(sk)) == 1:
            why = "같은 틀 '%s'" % sk[0]
        elif len(lasts[0]) >= 3 and lasts[0] not in COMMON_PRED and len(set(lasts)) == 1:
            why = "같은 서술어 '%s'" % lasts[0]
        elif len(tails[0]) >= 4 and len(set(tails)) == 1:
            why = "같은 끝말 '%s'" % tails[0]
        if why:
            out.append((flat[k][0], "%s: %s" % (why, snip(" ".join(trio), 70)), False))
            k += 3
            continue
        k += 1
    return out


# ---- L 부풀림·범용 표현 점검: 안내판, 어디에나 붙는 문장, 근거 없는 유보·주관, 기계적 균형
SIGNPOSTS = (r"그렇다면 [^.?!]{0,30}(?:까|가|는가)\?|여기서 (?:상당히 |특히 |한 가지 |더 )?(?:중요한|주목할|눈여겨볼|핵심적인|흥미로운) (?:점|것|결론|부분)|"
             r"여기서 한 가지 짚고 넘어갈|(?:하지만|그러나) 이것이 전부는 아니|이러한 [^.?!]{0,15}에도 불구하고|"
             r"(?:이제|다음으로|그럼|먼저) [^.?!]{0,20}(?:살펴보자|살펴보겠습니다|알아보자|알아보겠습니다|들여다보자|보자)|"
             r"다음으로 알아볼 (?:것|내용)은|앞서 (?:살펴본|말한|언급한|설명한) (?:바와 같이|것처럼)|이를 바탕으로|"
             r"이것이 (?:의미하는|시사하는) (?:바|것)|^즉,$|이게 가장 중요합니다|가장 중요한 (?:점|것)은 바로|결론이 나옵니다")
L5_END = re.compile(r"(?:것이|게) (?:중요하다|중요합니다|필요하다|필요합니다|핵심이다|핵심입니다|바람직하다|바람직합니다)[.!]?$|"
                    r"필요가 있(?:다|습니다)[.!]?$|가치가 달라(?:진다|집니다)[.!]?$|중요한 역할을 (?:한다|합니다)[.!]?$|"
                    r"에 달려 있(?:다|습니다)[.!]?$")
L5_ABSTRACT = re.compile(r"적절한|적절히|효과적|올바른|올바르게|다양한|효율적|목적|방법|방식|가치|전략|균형|자신에게 맞|상황에 맞|어떻게 활용")
L5_OUTLOOK = re.compile(r"앞으로의 가능성이 기대|다양한 분야에서 활용될 수 있|많은 도움이 될 것으로 보|어떻게 활용(?:하|되)느냐가 중요|"
                        r"매우 중요한 변화(?:다|입니다)")
L5_REACT = re.compile(r"^(?:이건|이것은|이 부분은|이 부분이|여기서|생각보다|이 점은|이 점이)?\s?(?:꽤|좀|정말|상당히|확실히)?\s?"
                      r"(?:흥미롭|눈에 띈|눈에 띕|의외|재미있|재밌|의미가 크|의미가 큽|반갑|놀랍)[가-힣]*[.!]?$")


def l5_check(s):
    if eojeol(s) > 30 or re.search(r"\d", s):
        return None
    if L5_END.search(s.strip()) and L5_ABSTRACT.search(s):
        return snip(s, 70)
    return None


def det_l5(doc):
    """정보 없는 일반론·총평은 한 번도, 범용 감상은 두 번 이상일 때."""
    out, reacts = [], []
    for pi, s in doc.flat:
        if doc.quoted_sent(pi, s):
            continue
        if l5_check(s):
            out.append((pi, "'것이 중요하다' 틀: " + snip(s, 60), False))
        elif L5_OUTLOOK.search(s) and not re.search(r"\d", s):
            out.append((pi, "총평 문구: " + snip(s, 60), False))
        elif eojeol(s) <= 6 and L5_REACT.search(s.strip()):
            reacts.append((pi, "감상 문구: " + snip(s, 60), False))
    return out + (reacts if len(reacts) >= 2 else [])


MODEL_META_TIC = re.compile(
    r"(?:이|그)?\s*(?:차이|결과|방향|이유|원인|부분|점)(?:은|는|이|가)?\s*(?:꽤|상당히)\s*(?:중요|선명|분명|명확|흥미롭|직접적|강하)|"
    r"(?:더|여기서)\s+중요한\s+(?:건|것은)|이게\s+중요한\s+이유는|"
    r"가장\s+좋은\s+방향은|이쪽이\s+훨씬\s+좋(?:다|습니다)"
)


def det_l9(doc):
    """모델이 자주 쓰는 평가·해설 골격. 금지어가 아니라 문맥을 읽을 후보로만 올린다."""
    out = []
    for pi, s in doc.flat:
        if doc.quoted_sent(pi, s):
            continue
        if MODEL_META_TIC.search(s.strip()):
            out.append((pi, snip(s, 70), False))
    return out


L6_HEDGE = re.compile(r"판단하기(?:에는|엔)? 이르|단정하기(?:는|엔)? 어렵|(?:좀 더|조금 더|더) 지켜볼 필요|지켜봐야 (?:한다|합니다|할 것)|"
                      r"추가(?:적인)? 검증이 필요|(?:직접|좀 더|더) (?:확인해|써|사용해|써 보고|지켜) ?(?:봐야|볼 필요)|실제 사용에서는 다를 수|모든 경우에 적용된다고 (?:볼|할) 수는 없")


def det_l6(doc):
    """유보할 이유가 앞 문장·같은 문장에 없는 안전한 결론. 지우면 확신도가 바뀌므로 확인만."""
    out, flat = [], doc.flat
    for k, (pi, s) in enumerate(flat):
        if doc.quoted_sent(pi, s) or not L6_HEDGE.search(s):
            continue
        out.append((pi, snip(s, 60), False))  # 유보할 근거가 글 안에 있는지는 모델이 읽고 본다
    return out


L7_MARK = re.compile(r"^(?:개인적으로(?:는)?|솔직히(?: 말하면)?|내가 보기에(?:는)?|제가 보기에(?:는)?|내가 가장 궁금한 (?:건|것은)|"
                     r"제가 가장 궁금한 (?:건|것은))[, ]|^(?:이건|이것은|이 부분은) (?:좀 |꽤 |정말 )?(?:놀랍|반갑|재미있|재밌)|직접 해 ?보고 싶")


def det_l7(doc):
    """이유·경험이 이어지지 않는 주관 표지. 두 번 이상일 때만."""
    marks, flat = [], doc.flat
    for k, (pi, s) in enumerate(flat):
        if doc.quoted_sent(pi, s) or not L7_MARK.search(s.strip()):
            continue
        marks.append((pi, snip(s, 60), False))  # 경험·이유가 글 안에 있는지는 모델이 읽고 본다
    return marks if len(marks) >= 2 else []


L8_POS = re.compile(r"장점이 크|좋아졌|좋은 결과|뛰어나|개선됐|개선되었|훌륭|나아졌|효과가 크|성능이 좋")
L8_CAVEAT = re.compile(r"^(?:다만|하지만|그러나|물론),? [^.?!]{0,20}(?:한계도 있|주의할 필요가 있|주의가 필요|"
                       r"모든 (?:상황|경우)에서 그렇다고 (?:보기는|할 수는) 어렵|단점도 있|고려해야 할 (?:점|부분)도 있|아직 갈 길이)")


def det_l8(doc):
    """칭찬 바로 뒤 내용 없는 단서가 반복되는 기계적 균형. 두 번 이상일 때만."""
    out, flat = [], doc.flat
    for k in range(1, len(flat)):
        a, (pb, b) = flat[k - 1][1], flat[k]
        if L8_POS.search(a) and L8_CAVEAT.search(b.strip()) and not re.search(r"\d", b) and eojeol(b) <= 12:
            out.append((pb, snip(a, 30) + " / " + snip(b, 40), False))
    return out if len(out) >= 2 else []


Q2_PAT = (r"(?:제조사|회사|기업|업체|사람|전문가|누군가|그들|업계|정부|개발사|판매자|소비자|사용자|비평가|팬)(?:들)?(?:은|는|이|가|도)\s?"
          r"[“\"][^”\"]{2,60}[”\"](?:라고|하고|며)\s?(?:말한다|말합니다|주장한다|주장합니다|외친다|광고한다|홍보한다|생각한다|묻는다|말하는 듯)")


# ---- J 반복·리듬 정리: 결론, 되풀이, 대칭, 재요약
J6_MARK = re.compile(r"결국|결론적으로|요컨대|요약하자면|이처럼|이렇듯|무엇보다")
J6_TURN = re.compile(r"아니라|중요한 (?:것|건)은|보다 중요한")
J6_OUTLOOK = re.compile(r"올바르게|현명하게|가능성|미래|기대|도구|동료|함께|확장|균형|성장|기회")


def det_j6(doc):
    for pi in list(range(len(doc.paras)))[-2:]:
        par = doc.paras[pi]
        if doc.quoted_span(pi, 0, len(par)):
            continue
        if J6_MARK.search(par) and (J6_TURN.search(par) or J6_OUTLOOK.search(par)):
            return [(pi, snip(par, 90), False)]
    return []


J7_SYN = [("효율", "생산", "능률"), ("향상", "높", "개선", "증진", "끌어올"), ("활용", "사용", "이용"),
          ("중요", "핵심", "필수", "관건"), ("방법", "방식", "방안", "수단"), ("작업", "업무"),
          ("변화", "바꾸", "바뀌", "전환"), ("글쓰기", "집필", "작문"), ("절감", "줄이", "아끼", "절약")]
J7_STOP = set("것 수 이 그 저 등 때 중 더 더욱 매우 정말 가장 보다 및 또 또한 결과 과정 경우 부분 이를 이는 이것 그것 "
              "통해 위해 따라 대한 대해 관련 우리 사람 있다 없다 하다 되다 있습니다 없습니다 합니다 됩니다 한다 된다".split())
J7_SUFFIX = re.compile(r"(?:으로써|으로|에서|에게|까지|부터|처럼|이다|입니다|습니다|합니다|됩니다|했다|한다|하는|하고|하여|해서|하며|"
                       r"된다|되는|되어|시킬|시키|시켜|적으로|적인|할|한|함|된|적|성|화|를|을|은|는|이|가|의|에|와|과|도|만|로)$")


def _j7_stems(s):
    out = set()
    for w in re.findall(r"[가-힣]+|[A-Za-z][A-Za-z0-9]*", s):
        base = w
        for _ in range(3):
            nb = J7_SUFFIX.sub("", base)
            if nb == base or not nb:
                break
            base = nb
        key = base
        for gi, group in enumerate(J7_SYN):
            if any(base.startswith(m) for m in group):
                key = "#%d" % gi
                break
        if not key.startswith("#") and re.search(r"(?:다|고|며|게|지|서|면|요|니|네|죠|데|여|아|어|워)$", w):
            continue  # 서술어·연결형은 내용어로 세지 않는다
        if key in J7_STOP or (len(key) < 2 and not key.startswith("#")):
            continue
        out.add(key.lower())
    return out


def det_j7(doc):
    out, flat = [], doc.flat
    stems = [_j7_stems(s) for _p, s in flat]
    for k in range(1, len(flat)):
        pi, s = flat[k]
        cur = stems[k]
        if len(cur) < 3 or doc.quoted_sent(pi, s):
            continue
        prev = set().union(*stems[max(0, k - 2):k])
        if len(cur & prev) / len(cur) >= 0.75 and len(cur & prev) >= 2 and len(cur - prev) <= (1 if len(cur) >= 4 else 0):
            out.append((pi, "%s ← 앞 두 문장과 내용어 %d개 겹침" % (snip(s, 60), len(cur & prev)), False))
    return out


ENUM_START = re.compile(r"^\s*(?:첫째|둘째|셋째|넷째|다섯째|마지막으로|\d+[.)]|[-*•])[,\s]")


def det_j8(doc):
    out, items = [], []

    def flush():
        if len(items) >= 3:
            lens = [len(re.sub(r"\s", "", s)) for _p, s in items]
            ends = [re.sub(r"[.!?]+$", "", s.strip())[-2:] for _p, s in items]
            mean = sum(lens) / len(lens)
            cvv = (sum((x - mean) ** 2 for x in lens) / len(lens)) ** 0.5 / mean if mean else 1
            if cvv < 0.2 and Counter(ends).most_common(1)[0][1] >= 0.75 * len(items):
                out.append((items[0][0], "항목 %d개의 분량(편차 %.0f%%)과 끝말이 같음: %s"
                            % (len(items), 100 * cvv, snip(items[0][1], 40)), False))
        items.clear()

    for pi, s in doc.flat:
        if ENUM_START.match(s):
            items.append((pi, s))
        else:
            flush()
    flush()
    return out


J9_START = re.compile(r"^(?:즉|쉽게 말하면|쉽게 말해|다시 말해|다시 말하면|한마디로|정리하면|요컨대)[, ]|셈이(?:다|었다|입니다)[.!]?$")


def det_j9(doc):
    """즉·쉽게 말하면·한마디로·~인 셈이다로 앞 문장 내용을 다시 설명하는 문장."""
    out, flat = [], doc.flat
    for k in range(1, len(flat)):
        pi, s = flat[k]
        if doc.quoted_sent(pi, s) or not J9_START.search(s.strip()):
            continue
        cur, prev = _j7_stems(s), _j7_stems(flat[k - 1][1])
        if cur and len(cur & prev) / len(cur) >= 0.5:
            out.append((pi, snip(s, 60) + " ← 앞 문장과 내용어가 겹침", False))
    return out


# ---- N 소설·에세이 서술 (대사는 보지 않는다)
N_ONLY = {"소설", "웹소설", "에세이"}
N1_START = re.compile(r"(?:(?:하지만|그러나|그래서|그리고|그런데|다시|이내|곧|결국|그제야)\s)?(?:나는|내가)\s")
SIMILE = re.compile(r"처럼|마치|듯(?:이|한|했|하)|것만 같")


def det_n1(doc):
    """1인칭 서술 전용. 3인칭의 '그는'·이름 주어는 인물 추적 장치라 보지 않는다."""
    out = []
    for f in sorted(set(doc.file_of)):  # 회차마다 따로 본다
        flat = [(pi, s) for pi, s in doc.flat if doc.file_of[pi] == f and not doc.quoted_sent(pi, s)]
        out += _n1_one(flat)
    return out


def _n1_one(flat):
    out = []
    starts = [bool(N1_START.match(s)) for _p, s in flat]
    if sum(starts) >= 5 and sum(starts) >= 0.04 * max(1, len(flat)):
        return [(flat[j][0], "서술문 첫머리 '나는·내가'(이 회차 %d/%d): %s" % (sum(starts), len(flat), snip(flat[j][1], 40)), False)
                for j in range(len(flat)) if starts[j]]
    k = 0
    while k < len(flat):
        if starts[k]:
            hits = [j for j in range(k, min(len(flat), k + 6)) if starts[j]]
            if len(hits) >= 2:
                out.append((flat[k][0], "6문장 안에 %d번: %s" % (len(hits), snip(flat[k][1], 40)), False))
                k = hits[-1] + 1
                continue
        k += 1
    return out


BODY = ("눈썹", "미간", "입꼬리", "입술", "눈가", "눈빛", "눈", "고개", "어깨", "한숨", "숨", "주먹", "팔짱", "턱", "이마",
        "손끝", "손가락", "혀", "침", "코웃음", "헛기침", "미소", "웃음", "침묵", "정적")
BODY_RE = re.compile(r"(%s)(?:을|를|이|가|은|는|도)?$" % "|".join(BODY))
FIXED_CLICHE = re.compile(r"묘한 (?:기분|감정|느낌)|설명하기 어려운 (?:감정|기분)|알 수 없는 (?:감정|기분)|가슴 한구석이 [가-힣]+")
COMMON_GESTURE = ("고개 ㄲㄷ", "고개 ㅈ", "고개 ㄷ", "한숨 ㄴㅅ", "한숨 ㅅ")
PUNCT = r"[.,!?…\"'“”’‘]"


CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


def _verb_key(tok):
    """동사를 시제·연결 어미를 뗀 첫 두 음절의 초성으로 비교한다(떴다=뜨며, 끄덕였다=끄덕이며, 감았다≠굴렸다)."""
    t = re.sub(PUNCT, "", tok)
    t = re.sub(r"(?:다가|면서|다|며|고|자|서)$", "", t)
    t = re.sub(r"(?:었|았|였|으|어|아|여|셨|시)+$", "", t) or t
    return "".join(CHO[(ord(ch) - 0xAC00) // 588] for ch in t[:2] if 0 <= ord(ch) - 0xAC00 < 11172)


def gestures(doc):
    """서술문 속 신체 반응·동작: [(문단, 표준형, 실제 표현, 같은 문장의 주어)]"""
    out = []
    for pi, s in doc.flat:
        if doc.quoted_sent(pi, s):
            continue
        toks = s.split()
        subj = None
        for t in toks:
            tt = re.sub(PUNCT, "", t)
            m = re.match(r"([가-힣]{1,5})(?:은|는|이|가)$", tt)
            if m and not BODY_RE.match(tt):
                subj = m.group(1)
                break
        for i, t in enumerate(toks):
            m = BODY_RE.match(re.sub(PUNCT, "", t))
            if not m:
                continue
            rest = toks[i + 1:i + 3]
            vi = next((j for j, x in enumerate(rest) if re.search(r"(?:다|며|고|자|서|다가|면서)" + PUNCT + "*$", x)), None)
            if vi is None:
                continue
            adv = " ".join(re.sub(PUNCT, "", x) for x in rest[:vi])
            key = "%s %s%s" % (m.group(1), (adv + " ") if adv else "", _verb_key(rest[vi]))
            out.append((pi, key, " ".join([t] + rest[:vi + 1]), subj))
        for mo in FIXED_CLICHE.finditer(s):
            out.append((pi, mo.group(0), mo.group(0), subj))
    return out


def det_phrase_repeat(doc):
    toks, where = [], []
    for pi, s in doc.flat:
        if doc.quoted_sent(pi, s):
            continue
        for w in s.split():
            toks.append(re.sub(r"[.,!?“”\"'’‘…·*/#>|_~()\[\]]", "", w))
            where.append(pi)
    stop = set("수 것 번 더 한 그 이 저 때 적 좀 또 다시 있었다 없었다 했다 같았다 있다 없다 않았다 것이다 거다 있는 없는 하는 된 될 할 했던 있던".split())
    grams = {}
    for i in range(len(toks) - 2):
        g = tuple(toks[i:i + 3])
        if not all(g) or any(t.startswith(("고개", "한숨")) for t in g):
            continue  # 흔한 동작은 동작 세기에서 따로 본다
        if len([t for t in g if len(t) >= 2 and t not in stop]) < 2:
            continue
        grams.setdefault(g, []).append(where[i])
    out, seen = [], set()
    for g, v in sorted(grams.items(), key=lambda x: -len(x[1])):
        if len(v) >= 3 + sum(len(p) for p in doc.paras) // 20000 and tuple(v) not in seen:
            seen.add(tuple(v))
            for pi, m in _per_file(doc, v):
                out.append((pi, "구절 '%s' %d회%s" % (" ".join(g), len(v), m), False))
    return out


def _per_file(doc, pis):
    """여러 회차면 회차마다 첫 자리 하나씩(이 회차 횟수와 함께), 한 원고면 첫 자리 하나."""
    files = sorted({doc.file_of[p] for p in pis}, key=lambda f: min(p for p in pis if doc.file_of[p] == f))
    if len(set(doc.file_of)) <= 1:
        return [(min(pis), "")]
    return [(min(p for p in pis if doc.file_of[p] == f), " · 이 회차 %d회" % sum(1 for p in pis if doc.file_of[p] == f)) for f in files]


def det_n3(doc):
    """한 작품 안에서 같은 반응·동작·상투 표현이 되풀이되는지 센다(금지 목록이 아니다)."""
    gs = gestures(doc)
    total = Counter(k for _p, k, _s, _u in gs)
    where, surf = {}, {}
    for pi, k, sfc, _u in gs:
        where.setdefault(k, []).append(pi)
        surf.setdefault(k, Counter())[sfc] += 1
    out = []
    base = 3 + sum(len(p) for p in doc.paras) // 20000  # 원고가 길수록 문턱을 올린다(약 2만 자마다 +1)
    for k, n in total.most_common():
        if n >= base + (2 if k.startswith(COMMON_GESTURE) else 0):
            who = Counter(u for _p, kk, _s, u in gs if kk == k and u)
            whos = ", ".join("%s %d" % kv for kv in who.most_common(3))
            for pi, m in _per_file(doc, where[k]):
                out.append((pi, "동작 '%s' %d회%s%s" % (surf[k].most_common(1)[0][0], n, m, (" (주어: %s)" % whos) if whos else ""), False))
    return out + det_phrase_repeat(doc)


EMO_STATE = re.compile(r"화가 (?:나|났|치밀)|분노가|두려웠|무서웠|불안했|불안해졌|당황했|당황스러웠|놀랐|안도했|안도감|억울했|서러웠|"
                       r"부끄러웠|창피했|슬펐|기뻤|설렜|초조했|초조해졌|긴장했|긴장됐|허탈했|씁쓸했|쓸쓸했|막막했|답답했|섭섭했|서운했|"
                       r"혼란스러웠|짜증이 났|겁이 났|질투가|미안했|미안함|고마웠|죄책감|수치심|그리웠|외로웠")
SPEECH_ACT = re.compile(r"(?:거절했|허락했|경고했|위협했|사과했|부탁했|애원했|동의했|승낙했|수락했|항의했)|끝내려 하고 있었|"
                        r"(?:뜻|의미)(?:였|이었)다|셈이었다|사실상 [가-힣 ]{0,10}(?:였|이었)다|말은 [가-힣]{1,6}(?:였|이었)다")
PARA_PAIRS = [("조용했", "소리도 들리지 않"), ("조용했", "아무 소리"), ("대답하지 않", "침묵"), ("아무 말도 하지 않", "침묵"),
              ("처음 보는", "본 적이 없"), ("움직이지 않", "가만히")]
RECAP = re.compile(r"(?:아까|방금|앞서)[^.?!]{0,30}(?:말했던|말한|했던|하던) (?:것|말)은 [^.?!]{0,20}(?:뜻|의미|말)이었다|결국 이런 뜻이었다")


def det_n2(doc):
    """이미 전달된 것을 다시 말하는 곳: 행동·대사 뒤 해설, 같은 뜻 되풀이, 앞 장면 재정리, 감정 이름 연쇄."""
    out, flat = [], doc.flat
    ges = {}
    for pi, key, _sf, _u in gestures(doc):
        ges.setdefault(pi, []).append(key.split()[0])
    narr = [(k, pi, s) for k, (pi, s) in enumerate(flat) if not doc.quoted_sent(pi, s)]
    for k, pi, s in narr:
        if RECAP.search(s):
            out.append((pi, "'아까 ~ 말했던 것은' 문장: " + snip(s, 50), False))
            continue
        if k == 0:
            continue
        ppi, ps = flat[k - 1]
        prev_dialogue = doc.quoted_sent(ppi, ps)
        if (eojeol(s) <= 8 and EMO_STATE.search(s) and not prev_dialogue
                and any(b in ps for b in ges.get(ppi, []))):
            out.append((pi, "몸짓 문장 뒤 감정어 문장: %s / %s" % (snip(ps, 30), snip(s, 30)), False))
        elif SPEECH_ACT.search(s) and prev_dialogue:
            out.append((pi, "대사 뒤 해설어(거절했다·뜻이었다 등) 문장: %s / %s" % (snip(ps, 30), snip(s, 30)), False))
        elif not prev_dialogue and any((a in ps and b in s) or (b in ps and a in s) for a, b in PARA_PAIRS):
            out.append((pi, "짝 표현(조용했다/소리도 들리지 않았다 등) 이웃 문장: %s / %s" % (snip(ps, 30), snip(s, 30)), False))
    em = [i for i, (_k, _pi, s) in enumerate(narr) if eojeol(s) <= 6 and EMO_STATE.search(s)]
    i = 0
    while i < len(em):
        win = [j for j in em if em[i] <= j < em[i] + 5]
        if len(win) >= 3:
            out.append((narr[em[i]][1], "짧은 감정어 문장 %d개가 가까이: %s" % (len(win), snip(" / ".join(narr[j][2] for j in win), 60)), False))
            i += len(win)
            continue
        i += 1
    return out


SCENE_SEP = re.compile(r"^\s*(?:(?:[*＊]\s*){3,}|[◇◆○●□■#~\-=]{3,}|[◇◆]|…{2,}|\.{3,}|o\s?o\s?o)\s*$")
END_ABSTRACT = re.compile(r"돌이킬 수 없|시작되고 있|시작이었|끝이 아니|끝난 것이 아니|끝나지 않았|선택해야|운명|이제부터|"
                          r"아직 [가-힣 ]{0,8}(?:아니었|않았)|뭔가|무언가|모든 것이")
END_REALIZE = re.compile(r"깨달았|알게 되었|알 것 같았|이해했|그제야")


def scenes(doc):
    """[(시작 문단, 끝 문단, 파일)] 장면 구분 기호와 파일(회차) 경계로 나눈다."""
    out, start = [], None
    for pi, par in enumerate(doc.paras):
        f = doc.file_of[pi]
        if SCENE_SEP.match(par) or is_heading(par):
            if start is not None:
                out.append((start, pi - 1, doc.file_of[start]))
            start = None
            continue
        if start is None:
            start = pi
        elif doc.file_of[start] != f:
            out.append((start, pi - 1, doc.file_of[start]))
            start = pi
    if start is not None:
        out.append((start, len(doc.paras) - 1, doc.file_of[start]))
    return out


def end_type(doc, a, b):
    for pi in range(b, a - 1, -1):
        if not doc.sents[pi]:
            continue
        last = doc.sents[pi][-1]
        if doc.quoted_sent(pi, last):
            return "대사", last
        if END_REALIZE.search(last):
            return "깨달음", last
        if eojeol(last) <= 7 and END_ABSTRACT.search(last):
            return "추상 선언", last
        return "행동·묘사", last
    return None, ""




# ---- 교체: 삼단 나열(J4)에 서술어 셋 병렬을 더한다(글 안에서 두 번 이상일 때)
TRIAD_PRED = re.compile(r"(?:(?:더|덜|가장)\s)?[가-힣]{1,5}(?:고|며),\s(?:(?:더|덜|가장)\s)?[가-힣]{1,5}(?:고|며),\s(?:(?:더|덜|가장)\s)?[가-힣]{1,6}(?:다|습니다)")

RULES = [r for r in RULES if r.rid not in ("J5a", "J5b")]
RULES += [
    Rule("J5", "문장 골격 반복", "중", "제안", det_j5,
         ["문제는 돈이 아니었다. 문제는 시간이 아니었다. 문제는 그가 아니었다.", "그제야 깨달았다. 그제야 알았다. 그제야 이해했다.",
          "이상했다. 너무 이상했다. 분명 이상했다.", "초안을 만들 수 있습니다. 기록을 찾을 수 있습니다. 문의도 번역할 수 있습니다."],
         ["그는 문을 열었다. 그는 복도를 한참 걸었다. 그는 멈췄다.", "바람이 불었다. 비가 왔다. 아이가 울었다."]),
    Rule("L4", "친절한 전환 문장(안내판)", "약", "제안", SIGNPOSTS,
         ["그렇다면 이 문제는 어떻게 풀 수 있을까? 여기서 중요한 점이 있다. 하지만 이것이 전부는 아니다. 이제 실제 사례를 살펴보자."],
         ["그렇다면 이 문제는 어떻게 풀 수 있을까? 답은 비용에 있다."], min_count=3),
    Rule("L5", "어디에나 붙는 문장(일반론·총평·감상)", "약", "제안", det_l5,
         ["AI를 효과적으로 활용하려면 목적에 맞는 적절한 방법을 고르는 것이 중요하다.", "이건 꽤 흥미롭다. 가격은 그대로다. 생각보다 의미가 크다."],
         ["마감 전에 원고를 두 번 소리 내어 읽는 것이 중요하다.", "이건 꽤 흥미롭다. 가격은 그대로다."]),
    Rule("L6", "근거 없는 유보", "약", "확인", det_l6,
         ["성능은 좋아 보인다. 아직 판단하기에는 이르다."], ["성능은 좋아 보인다. 벤치마크는 세 종류다."]),
    Rule("L7", "근거 없는 주관 표지", "약", "확인", det_l7,
         ["솔직히 이건 좀 놀랍다. 가격은 그대로다. 개인적으로는 반갑다."],
         ["솔직히 좀 놀랐다. 지난달보다 두 배 빨랐다."]),
    Rule("L8", "기계적 균형", "약", "제안", det_l8,
         ["성능이 좋아졌다. 다만 한계도 있다. 가격도 개선됐다. 하지만 주의할 필요가 있다."], ["성능이 좋아졌다. 다만 배터리가 20% 빨리 닳는다."]),
    Rule("L9", "모델 평가·해설 말버릇", "약", "확인", det_l9,
         ["이 차이는 꽤 중요하다.", "방향이 꽤 선명하다. 여기서 중요한 건 비용이다."],
         ["처음 만든 컵치고는 꽤 마음에 들었다.", "가격은 비쌌다. 다만 배송은 빨랐다.", "결국 열차는 20분 늦게 출발했다."]),
    Rule("Q2", "가상 인용", "중", "확인", Q2_PAT,
         ["제조사는 “우리 제품이 최고입니다”라고 말한다."], ["제조사는 보도자료에서 배터리 수명이 두 배라고 밝혔다."]),
    Rule("J6", "모범 답안형 결론", "중", "제안", det_j6,
         ["본문이다.\n\n결국 이 도구는 사람을 대신하는 것이 아니라 돕는 도구다. 올바르게 쓰는 법을 배우는 것이 중요하다."],
         ["본문이다.\n\n회의는 목요일 오후 3시에 다시 연다."]),
    Rule("J7", "앞 문장 되풀이(표현만 바꾼 반복)", "약", "권장", det_j7,
         ["AI는 글쓰기의 효율성을 높인다. 이를 통해 작가는 더 효율적으로 작업한다. 결과적으로 글쓰기의 생산성이 향상된다."],
         ["AI는 글쓰기의 효율성을 높인다. 다만 사실 확인은 여전히 사람이 한다. 출처를 두 번 대조하는 이유다."]),
    Rule("J8", "항목 분량·끝말 대칭", "약", "제안", det_j8,
         ["첫째, 비용을 줄일 수 있다. 둘째, 속도를 높일 수 있다. 셋째, 실수를 막을 수 있다."],
         ["첫째, 비용을 줄일 수 있다. 둘째, 속도가 붙는데 이건 팀 규모에 따라 차이가 꽤 크다. 셋째, 실수를 막는다."]),
    Rule("J9", "재요약(즉·쉽게 말하면)", "약", "권장", det_j9,
         ["가격이 20% 내려갔다. 쉽게 말하면 가격이 더 싸진 것이다."], ["가격이 20% 내려갔다. 즉, 재고 부담이 줄었다."]),
    Rule("N1", "'나는·내가' 주어 밀집(1인칭)", "중", "권장", det_n1,
         ["나는 문을 열었다. 나는 복도를 걸었다. 바람이 찼다."], ["문을 열었다. 복도를 걸었다. 나는 멈췄다."]),
    Rule("N2", "설명 중복(해설·되풀이)", "중", "제안", det_n2,
         ["그는 주먹을 꽉 쥐었다. 화가 치밀어 올랐다.", "“난 가지 않겠어.”\n\n그는 단호하게 거절했다.", "“난 가지 않겠어.” 그는 단호하게 거절했다.",
          "방 안은 조용했다. 아무 소리도 들리지 않았다.", "놀랐다. 혼란스러웠다. 불안해졌다."],
         ["그는 주먹을 꽉 쥐었다. 문이 열렸다.", "“난 가지 않겠어.”\n\n그는 문을 닫았다.", "방 안은 조용했다. 창밖에서 개가 짖었다."]),
    Rule("N3", "반복 사용 표현·동작", "약", "제안", det_n3,
         ["그는 눈을 가늘게 떴다. 그는 창밖을 봤다. 그는 눈을 가늘게 뜨며 웃었다. 지우가 물었다. 그는 다시 눈을 가늘게 떴다."],
         ["그는 고개를 끄덕였다. 지우가 웃었다. 그는 고개를 끄덕였다. 문이 닫혔다. 그는 고개를 끄덕였다."]),
]
for _r in RULES:
    if _r.rid.startswith("N"):
        _r.only = N_ONLY
    if _r.rid == "J4":
        _r.pos.append("쉽고, 빠르고, 정확하다. 더 가볍고, 더 싸고, 더 튼튼하다.")
        _r.neg.append("일어나 씻고 밥을 먹었다.")

ROLE_FIXED = {"P3e"}  # 기계 점검: 모델이 글자로 볼 수 없는 것. 맞춤법·띄어쓰기·문법은 도구가 판정하지 않는다(모델이 한다)
ROLE_READ = {"U4", "J7", "L9"}  # 뜻을 봐야 하는 곳의 후보(넓게 모은다)
FORM_NAMES = {
    "R1": "챗봇 상투 문구", "U1": "질문 뒤 답을 내놓는 문단", "U2": "'그렇다면 ~일까'류 질문 문장", "U3": "'진실은·본질은' 문구",
    "U7w": "'오해하지 마세요'류 통념 문구", "U8": "'다수 대 잘하는 사람' 이분법 문구", "U9": "대칭 명령(그만두기 → 시작하기) 문장",
    "L1": "의미 부여 어휘(혁신·패러다임 등)", "L2": "홍보 수식어", "L3": "겹친 추측 표현", "L4": "안내 문장 표현(이제 살펴보자 등)",
    "L5": "'~것이 중요하다'·총평·감상 문구", "L6": "유보 표현(판단하기 이르다 등)", "L7": "주관 표지 반복(솔직히·개인적으로)",
    "L8": "칭찬 뒤 '다만 한계도' 짝", "L9": "모델 평가·해설 말버릇", "J2": "결론 표지(결론적으로·요약하자면)", "J2b": "문단 끝 짧은 한 줄", "J6": "끝 문단의 요약 표지·전망 어휘",
    "J7": "앞 문장과 낱말이 많이 겹치는 문장", "J9": "'즉·쉽게 말하면' 문장", "Q1": "'전문가들은·연구에 따르면' 문구",
    "Q2": "집단 주어 + 따옴표 말", "S6b": "사물 주어 문장", "D1": "종결 어미 섞임", "D3": "접속어 반복",
    "N1": "'나는·내가' 문장 첫머리 밀집", "N2": "행동·대사 뒤 감정어·해설어 문장", "N3": "되풀이된 동작·구절",
}
for _r in RULES:
    _r.role = "기계" if _r.rid in ROLE_FIXED else ("읽기" if _r.rid in ROLE_READ else "형태")
    _r.name = FORM_NAMES.get(_r.rid, _r.name)

SAFE_WORDS = re.compile(r"효과적|중요하|중요한|다양한|적절한|효율적|활용|제공|고려|가능하|가능한|필요하|필요한")
FICTION = {"소설", "웹소설"}


def section_lengths(doc):
    heads = [pi for pi, p in enumerate(doc.paras) if is_heading(p)]
    if len(heads) < 4:
        return []
    bounds = heads + [len(doc.paras)]
    return [sum(len(doc.paras[i]) for i in range(a + 1, b)) for a, b in zip(bounds, bounds[1:])]


def doc_metrics(doc, genre, findings):
    """문서 전체 경향. 판정이 아니라 여러 신호가 겹치는지 보여 준다."""
    narr = [s for pi, s in doc.flat if not doc.quoted_sent(pi, s)]
    lens = [eojeol(s) for s in narr]
    mean = sum(lens) / len(lens) if lens else 0.0
    cvv = (sum((x - mean) ** 2 for x in lens) / len(lens)) ** 0.5 / mean if mean else 0.0
    text = "\n".join(doc.paras)
    chars, words = max(1, len(text)), max(1, sum(eojeol(p) for p in doc.paras))
    cnt = Counter()
    for f in findings:
        cnt[f["id"]] += f["count"]
    fiction = genre in FICTION
    safe = 100 * len(SAFE_WORDS.findall(text)) / words
    contrast = sum(cnt[x] for x in ("U4", "U4x"))
    multi = [ss for ss in doc.sents if len(ss) >= 3]
    punch = sum(1 for ss in multi if eojeol(ss[-1]) <= 4 and eojeol(ss[-2]) >= 8)
    secs = section_lengths(doc)
    sec_cv = 1.0
    if secs and sum(secs):
        m_ = sum(secs) / len(secs)
        sec_cv = (sum((x - m_) ** 2 for x in secs) / len(secs)) ** 0.5 / m_
    rows = []

    def row(name, value, flag, note="", always=False):
        rows.append({"name": name, "value": value, "flag": bool(flag), "note": note, "always": always})

    row("문장 길이 변동계수", "%.2f (평균 %.1f어절)" % (cvv, mean), False, "참고만: 이 값으로 AI 여부를 가를 수 없다", True)
    row("문장 골격 반복", "%d곳" % cnt["J5"], (not fiction) and cnt["J5"] >= 2, "소설은 참고만" if fiction else "")
    row("안내 문장 표현", "%d개 (%.1f/1,000자)" % (cnt["L4"], 1000 * cnt["L4"] / chars),
        cnt["L4"] >= 3 and 1000 * cnt["L4"] / chars >= 1.5)
    row("부정 뒤 단언·대비 틀 문구", "%d개" % contrast, contrast >= 3)
    row("무난한 어휘 밀도", "%.1f/100어절" % safe, False, "참고만: 수정 대상을 특정하지 못해 신호 수에 넣지 않는다", True)
    row("'것이 중요하다'·총평·감상 문구", "%d문장" % cnt["L5"], cnt["L5"] >= 2)
    row("유보 표현", "%d문장" % cnt["L6"], cnt["L6"] >= 2)
    row("주관 표지 반복", "%d문장" % cnt["L7"], cnt["L7"] >= 2)
    row("칭찬 뒤 '다만 한계도' 짝", "%d곳" % cnt["L8"], cnt["L8"] >= 2)
    row("집단 주어 + 따옴표 말", "%d곳" % cnt["Q2"], cnt["Q2"] >= 1)
    row("앞 문장과 낱말이 겹치는 문장·'즉' 문장", "%d문장" % (cnt["J7"] + cnt["J9"]), cnt["J7"] + cnt["J9"] >= 2)
    row("항목 분량·끝말 대칭", "%d곳" % cnt["J8"], cnt["J8"] >= 1)
    row("소제목 분량 대칭", "절 %d개, 편차 %.0f%%" % (len(secs), 100 * sec_cv) if secs else "소제목 4개 미만", bool(secs) and sec_cv < 0.2)
    row("문단 끝 짧은 문장", "%d/%d문단" % (punch, len(multi)),
        (not fiction) and len(multi) >= 6 and punch >= 0.5 * len(multi), "소설은 참고만" if fiction else "")
    row("끝 문단의 요약 표지·전망 어휘", "있음" if cnt["J6"] else "없음", cnt["J6"] >= 1)
    bold = sum(len(re.findall(r"\*\*[^*\n]{1,80}\*\*", par)) for par in doc.paras)
    row("굵은 글씨(**…**) 수", "%d곳" % bold, False, "참고만: 소설의 적힌 글(편지·간판) 표시일 수도 있다")
    kinds = sorted({f["id"] for f in findings if f["group"] in "RUJLQ" and f["count"]})
    row("AI 문체 형태 후보 종류", "%d종 %s" % (len(kinds), " ".join(kinds)), False)
    if genre in N_ONLY:
        first = sum(1 for s in narr if N1_START.match(s))
        row("'나는·내가'로 시작하는 서술문", "%d/%d문장, 후보 %d곳" % (first, len(narr), cnt["N1"]), cnt["N1"] >= 2, "1인칭만 봄", True)
        row("행동·대사 뒤 감정어·해설어 문장", "%d곳" % cnt["N2"], cnt["N2"] >= 2)
        row("되풀이된 동작·구절", "%d개" % cnt["N3"], cnt["N3"] >= 3)
        row("비유 표지 밀도", "%.1f/1,000자" % (1000 * len(SIMILE.findall(text)) / chars), False, "참고만", True)
    for r_ in rows:
        r_["flag"] = False  # 도구는 판정하지 않는다. 문서 전체 경향은 모델이 원고를 읽고 수치를 근거로 쓴다
    return {"rows": rows}


# ---- 소설 보고(판정 없음): 장면 끝 분포, 자주 쓰는 틀, 인물별 동작·말투, 흐름, 회차 비교
SPEECH_VERB = r"(?:말했|물었|대답했|답했|외쳤|중얼거렸|덧붙였|속삭였|소리쳤|되물었|웃었|말을 이었|입을 열었|말했다)"
NOT_NAME = set("그것 이것 그건 이건 아무도 누구도 모두 나 그녀 그 내 우리 저 제 너 그러고 그러다 그런데 그래서 하지만 그리고 이번 오늘 지금 "
               "여기 거기 이제 아마 다들 사람 아이 남자 여자 소리 목소리 하나 둘 셋 마음 생각 얼굴 손 눈 문 불 물 빛 바람 시간 이야기 말".split())


def speech_family(q):
    t = re.sub(r"[.!?…~\s”\"]+$", "", q)
    if re.search(r"(?:십시오|세요|셔요|시죠|요|죠)$", t) or is_hapnida(t):
        return "존대"
    if re.search(r"(?:다|냐|니|자|라|어|아|야|지|군|네|걸|래|대|거든|잖아|마|게|고|데|나|까|구나|더라|와|봐|줘|해|돼|워|져|쳐|켜|가)$", t):
        return "반말"
    if t and "가" <= t[-1] <= "힣" and jong(t[-1]) == 0 and ((ord(t[-1]) - 0xAC00) % 588) // 28 in (0, 1, 4, 6, 9, 10, 14):
        return "반말"  # 받침 없이 ㅏ·ㅐ·ㅓ·ㅕ·ㅘ·ㅙ·ㅝ로 끝나는 해체(기다려, 보내, 내려)
    return "기타"


def speakers(doc):
    names = Counter()
    for pi, s in doc.flat:
        m = re.match(r"([가-힣]{2,5})(?:은|는|이|가)\s", s)
        if m and not doc.quoted_sent(pi, s):
            names[m.group(1)] += 1
    body = "\n".join(doc.paras)
    cand = {n for n, c in names.items() if c >= 3 and n not in NOT_NAME
            and re.search(re.escape(n) + r"(?:은|는|이|가)\s[^“\"\n]{0,15}" + SPEECH_VERB, body)}
    lines = {}
    for pi, par in enumerate(doc.paras):
        for mo in re.finditer(r"[“\"]([^”\"]{1,200})[”\"]", par):
            who = None
            for zone in (par[mo.end():mo.end() + 40], par[max(0, mo.start() - 40):mo.start()]):
                m = re.search(r"([가-힣]{2,5})(?:은|는|이|가)\s[^“\"]{0,15}" + SPEECH_VERB, zone)
                if m and m.group(1) in cand:
                    who = m.group(1)
                    break
            if not who:
                for dp in (pi + 1, pi - 1):
                    if 0 <= dp < len(doc.paras) and not doc.paras[dp].startswith(("“", "\"")):
                        m = re.match(r"([가-힣]{2,5})(?:은|는|이|가)\s", doc.paras[dp])
                        if m and m.group(1) in cand:
                            who = m.group(1)
                            break
            if who:
                lines.setdefault(who, []).append(mo.group(1))
    return lines


def voice_table(doc):
    rows, drift = [], []
    for who, qs in sorted(speakers(doc).items(), key=lambda x: -len(x[1])):
        if len(qs) < 3:
            continue
        fam = Counter(speech_family(q) for q in qs)
        main, mn = fam.most_common(1)[0]
        rows.append({"who": who, "lines": len(qs), "avg": round(sum(len(q.split()) for q in qs) / len(qs), 1),
                     "question": round(100 * sum(1 for q in qs if q.rstrip().endswith("?")) / len(qs)), "family": dict(fam)})
        known = fam["존대"] + fam["반말"]
        main = "존대" if fam["존대"] >= fam["반말"] else "반말"
        if known >= 3 and fam[main] >= 0.7 * known:
            odd = [q for q in qs if speech_family(q) not in (main, "기타")]
            if odd:
                drift.append({"who": who, "main": main, "odd": odd[:3]})
    similar = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if (max(a["family"], key=a["family"].get) == max(b["family"], key=b["family"].get)
                    and abs(a["avg"] - b["avg"]) <= 0.15 * max(a["avg"], b["avg"]) and abs(a["question"] - b["question"]) <= 10):
                similar.append((a["who"], b["who"]))
    return {"rows": rows, "drift": drift, "similar": similar}


THOUGHT = re.compile(r"\?|생각했|싶었|모르겠|걸까|을까|어쩌면")
FLOW_NAME = {"대": "대사", "짧": "짧은 서술", "감": "감정", "생": "생각", "행": "행동·묘사"}


def flow_patterns(doc):
    seq = []
    for pi, par in enumerate(doc.paras):
        if SCENE_SEP.match(par) or is_heading(par):
            seq.append("|")
        elif par.strip().startswith(("“", "\"", "「")):
            seq.append("대")
        elif len(doc.sents[pi]) == 1 and eojeol(par) <= 4:
            seq.append("짧")
        elif any(EMO_STATE.search(s) and eojeol(s) <= 6 for s in doc.sents[pi]):
            seq.append("감")
        elif THOUGHT.search(par):
            seq.append("생")
        else:
            seq.append("행")
    grams = Counter("→".join(FLOW_NAME[x] for x in seq[i:i + 4]) for i in range(len(seq) - 3)
                    if "|" not in seq[i:i + 4] and len(set(seq[i:i + 4])) >= 3)
    total = sum(grams.values()) or 1
    return [(g, n, round(100 * n / total)) for g, n in grams.most_common(2) if n >= 4 and n >= 0.1 * total]


def fiction_report(doc):
    sc = scenes(doc)
    last_of_file = {}
    for i, (_a, _b, f) in enumerate(sc):
        last_of_file[f] = i
    ends = []
    for i, (a, b, f) in enumerate(sc):
        _t, last = end_type(doc, a, b)
        if last:
            ends.append({"loc": doc.loc(a), "text": last, "chapter_end": last_of_file[f] == i})
    frames, ex = Counter(), {}
    for pi, s in doc.flat:
        toks = s.split()
        if toks and not doc.quoted_sent(pi, s) and re.sub(PUNCT, "", toks[0]) in FRAME_ADV:
            k = skeleton(s)
            frames[k] += 1
            ex.setdefault(k, s)
    per, surf = {}, {}
    for _p, key, sfc, subj in gestures(doc):
        surf.setdefault(key, Counter())[re.sub(PUNCT + "+$", "", sfc)] += 1
        if subj and subj not in NOT_NAME:
            per.setdefault(subj, Counter())[key] += 1
    return {
        "scenes": {"count": len(sc), "separators": any(SCENE_SEP.match(p) for p in doc.paras), "endings": ends},
        "frames": [(k, n, ex[k]) for k, n in frames.most_common(5) if n >= 3],
        "gestures_by_subject": {s: [(surf[k].most_common(1)[0][0], n) for k, n in c.most_common(3) if n >= 2] for s, c in per.items()
                                if sum(c.values()) >= 3 and any(n >= 2 for n in c.values())},
        "voices": voice_table(doc),
        "flow": flow_patterns(doc),
    }


def baseline_table(texts, names, para_mode):
    """회차 비교: 다른 회차 평균보다 1.5배 이상 튀는 값을 표시한다(절대 기준이 아니라 작가 자신과 비교)."""
    rows = []
    for t, n in zip(texts, names):
        d = Doc(t, para_mode)
        narr = [s for pi, s in d.flat if not d.quoted_sent(pi, s)]
        rows.append({
            "name": n,
            "서술문 평균 어절": round(sum(eojeol(s) for s in narr) / max(1, len(narr)), 1),
            "대사 비율 %": round(100 * (len(d.flat) - len(narr)) / max(1, len(d.flat))),
            "감정 서술/100서술문": round(100 * sum(1 for s in narr if EMO_STATE.search(s)) / max(1, len(narr)), 1),
            "비유/1,000자": round(1000 * len(SIMILE.findall(t)) / max(1, len(t)), 1),
            "'나는' 시작 %": round(100 * sum(1 for s in narr if N1_START.match(s)) / max(1, len(narr)), 1),
            "3회+ 반복 동작": sum(1 for c in Counter(k for _p, k, _s, _u in gestures(d)).values() if c >= 3),
        })
    keys = [k for k in rows[0] if k != "name"]
    for r in rows:
        r["튀는 값"] = []
        for k in keys:
            others = [x[k] for x in rows if x is not r]
            m = sum(others) / len(others) if others else 0
            if m > 0 and r[k] >= 1.5 * m and r[k] - m >= 1:
                r["튀는 값"].append(k)
    return rows


AI_GROUPS = "RULJQN"

# 장르 프로필: off = 끄는 규칙, weak = '약'으로 낮춰 보는 규칙, line = 줄 단위 문단
GENRES = {
    "일반": {},
    "보고서": {},
    "논문": {"off": {"L6"}, "weak": {"S2b", "S2c", "W3f", "L4"}},
    "블로그": {"off": {"R1b"}, "weak": {"J1", "P3b", "L7"}},
    "웹소설": {"line": True, "off": {"J1", "S6a", "P3b", "J2b", "J6", "J8", "L4", "J7", "L5", "L6", "L7", "L8", "Q2", "J9"},
             "weak": {"U1", "U2", "U3", "U4", "J3", "J4", "J5", "L1", "L2", "D1"}},
    "소설": {"off": {"J1", "S6a", "P3b", "J6", "J8", "L4", "J7", "L5", "L6", "L7", "L8", "Q2", "J9"},
           "weak": {"U1", "U2", "U3", "U4", "J2b", "J3", "J4", "J5", "L1", "L2", "D1"}},
    "에세이": {"weak": {"J1", "J2b", "L1", "L2", "L7", "N1", "N3"}},
    "이메일": {"off": {"R1b"}},
    "공지": {"off": {"R1b", "W3c", "J8"}},
    "보도자료": {"off": {"R1b"}},
    "광고": {"off": {"L2", "L2b"}, "weak": {"J3", "U4", "L1", "P3b"}},
    "발표": {"off": {"R1b", "J1", "L4"}},
    "자기소개서": {"weak": {"L2"}},
}


# ================================================================ scan


def _clusters(density):
    paras = sorted(p for p, n in density.items() if n)
    spans, start, prev = [], None, None
    for p in paras:
        if start is None:
            start = prev = p
        elif p - prev <= 1:
            prev = p
        else:
            spans.append((start, prev))
            start = prev = p
    if start is not None:
        spans.append((start, prev))
    return [(a, b) for a, b in spans if b > a or density[a] >= 2]


def merge_profile(genre, profile):
    """장르 프로필에 작품별 규칙 파일을 겹친다. off·weak는 더하고, strong은 약하게 보던 규칙을 원래 세기로 되돌린다."""
    base = GENRES.get(genre, {})
    if not profile:
        return base, []
    alias = {"R2": ["R2a", "R2b"]}  # 3.9에서 나뉜 옛 번호는 둘 다에 적용
    profile = dict(profile)
    for k in ("off", "weak", "strong"):
        if profile.get(k):
            profile[k] = [y for x in profile[k] for y in alias.get(x, [x])]
    known = {r.rid for r in RULES}
    unknown = sorted({x for k in ("off", "weak", "strong") for x in profile.get(k, ()) if x not in known})
    if unknown:
        print("주의: 작품 규칙 파일에 없는 규칙 번호: %s (옛 번호: U4w·U5·U6·U7은 3.0에서 U4로, N4는 소설 보고로 바뀌었다. `rules`로 목록 확인)"
              % ", ".join(unknown), file=sys.stderr)
    prof = {"line": base.get("line"),
            "off": set(base.get("off", ())) | set(profile.get("off", ())),
            "weak": (set(base.get("weak", ())) | set(profile.get("weak", ()))) - set(profile.get("strong", ()))}
    return prof, [k for k in profile.get("keep_phrases", []) if k]


def scan(text, genre="일반", para_mode="auto", names=None, profile=None, labels=None, focus=None):
    """text: 원고 하나(문자열) 또는 여러 회차(목록, names와 함께). profile: 작품별 규칙 파일(dict)."""
    prof, keep = merge_profile(genre, profile)
    if para_mode == "auto" and prof.get("line"):
        para_mode = "line"
    texts = text if isinstance(text, list) else None
    if texts:
        names = names or ["%d번" % (i + 1) for i in range(len(texts))]
        text = "".join("\n\n⟦FILE:%d⟧\n\n%s" % (i, t) for i, t in enumerate(texts))
    doc = Doc(text, para_mode)
    doc.names = names if texts else None
    attach_labels(doc, labels, len(texts) if texts else 1)
    focus_idx = None
    if texts and focus:
        def num(x):
            m_ = re.findall(r"\d+", str(x))
            return int(m_[-1]) if m_ else None
        want = [str(x).strip() for x in focus]
        focus_idx = {i for i, n in enumerate(names)
                     for w_ in want if w_ == n or w_ == str(i + 1) or (num(w_) is not None and num(w_) == num(n))}
        if not focus_idx:
            raise ValueError("--focus %s에 맞는 회차가 없다. 넣은 회차: %s" % (",".join(want), ", ".join(names)))
    findings = []
    for r in RULES:
        if r.rid in prof.get("off", ()) or (r.only and genre not in r.only):
            continue
        hits = [h for h in r.run(doc) if not any(k in h[1] for k in keep)]  # 작가가 지키기로 한 표현은 빼고
        if focus_idx is not None:
            hits = [h for h in hits if doc.file_of[h[0]] in focus_idx]  # 고칠 회차의 후보만(다른 회차는 비교 기준)
        hits = [h for h in hits if h[0] not in doc.code]  # 코드 블록 안은 후보가 아니다
        live = [h for h in hits if not h[2]]
        quoted = [h for h in hits if h[2]]
        if len(live) < r.min_count:
            live = []
        if not live and not quoted:
            continue
        findings.append({
            "id": r.rid, "name": r.name, "group": r.group, "tier": r.tier, "role": r.role,
            "common_in_genre": r.rid in prof.get("weak", ()),
            "count": len(live),
            "hits": [{"para": pi + 1, "loc": doc.loc(pi), "text": t} for pi, t, _q in live],
            "quoted": [{"para": pi + 1, "loc": doc.loc(pi), "text": t} for pi, t, _q in quoted],
        })
    words = sum(eojeol(p) for p in doc.paras)
    density = Counter()
    for f in findings:
        if f["group"] in AI_GROUPS:
            for h in f["hits"]:
                density[h["para"]] += 1
    rep = {
        "genre": genre, "files": names if texts else None, "profile": (profile or {}).get("name"),
        "stats": {"문단": len(doc.paras), "문장": len(doc.flat), "어절": words,
                  "평균 문장 어절": round(words / max(1, len(doc.flat)), 1),
                  "'것' 100어절당": round(100 * len(re.findall(r"(?<![가-힣])것", "\n".join(doc.paras))) / max(1, words), 1),
                  "물음표 문장": sum(1 for _p, s in doc.flat if is_question(s))},
        "register": dict(register_counts(doc)),
        "ai_density": [(doc.loc(p - 1), n) for p, n in sorted(density.items())],
        "clusters": [doc.loc(a - 1) if a == b else "%s~%s" % (doc.loc(a - 1), doc.loc(b - 1)) for a, b in _clusters(density)],
        "focus": [names[i] for i in sorted(focus_idx)] if focus_idx else None,
        "metrics": doc_metrics(Doc("\n\n".join(t for i, t in enumerate(texts) if i in focus_idx), para_mode) if focus_idx else doc,
                               genre, findings),
        "findings": findings,
    }
    if genre in N_ONLY:
        rep["fiction"] = fiction_report(doc)
        if texts and len(texts) > 1:
            rep["fiction"]["baseline"] = baseline_table(texts, names, para_mode)
    return rep


def print_fiction(fr):
    print("\n## 소설 보고 (판정 없음 — 작가가 판단할 자료)")
    sc = fr["scenes"]
    print("- 장면 %d개%s. 장면마다 마지막 문장(여러 장면이 같은 방식으로 끝나는지 읽고 판단, 회차 끝은 ▶):"
          % (sc["count"], "" if sc["separators"] else "(장면 구분 기호가 없어 회차 단위로 봄)"))
    for e in sc["endings"][:30]:
        print("    %s%s: %s" % ("▶ " if e["chapter_end"] else "", e["loc"], snip(e["text"], 60)))
    if fr["frames"]:
        print("- 자주 쓰는 문장 틀: " + "; ".join("'%s' %d회(예: %s)" % (k, n, snip(e, 30)) for k, n, e in fr["frames"]))
    if fr["gestures_by_subject"]:
        print("- 인물별 반복 동작: " + "; ".join("%s — %s" % (w, ", ".join("%s %d" % kv for kv in v))
                                          for w, v in fr["gestures_by_subject"].items()))
    v = fr["voices"]
    if v["rows"]:
        print("- 인물별 말투(대사 3줄 이상, 화자는 추정):")
        for r_ in v["rows"][:8]:
            print("    %s: %d줄 · 평균 %.1f어절 · 질문 %d%% · %s" % (r_["who"], r_["lines"], r_["avg"], r_["question"],
                                                         ", ".join("%s %d" % kv for kv in r_["family"].items())))
        for d in v["drift"]:
            print("    · 말투 이탈 후보: %s(주로 %s) → %s" % (d["who"], d["main"], " / ".join("“%s”" % snip(q, 25) for q in d["odd"])))
        if v["similar"]:
            print("    · 말투 지표가 비슷한 인물: " + ", ".join("%s·%s" % p for p in v["similar"]))
    if fr["flow"]:
        print("- 자주 반복되는 흐름: " + "; ".join("%s %d회(%d%%)" % g for g in fr["flow"]))
    if fr.get("baseline"):
        rows = fr["baseline"]
        keys = [k for k in rows[0] if k not in ("name", "튀는 값")]
        print("- 회차 비교(다른 회차 평균의 1.5배 이상이면 ▲):")
        print("    " + " | ".join(["회차"] + keys))
        for r_ in rows:
            print("    " + " | ".join([r_["name"]] + [("%s▲" % r_[k]) if k in r_["튀는 값"] else str(r_[k]) for k in keys]))


def print_scan(rep):
    st = rep["stats"]
    print("# 스캔: 형태와 수치 (장르: %s%s%s%s) — 도구는 형태만 보이고, 판단은 원고를 읽고 한다"
          % (rep["genre"], (" · 회차 %d개: %s" % (len(rep["files"]), ", ".join(rep["files"]))) if rep.get("files") else "",
             (" · 작품 규칙: %s" % rep["profile"]) if rep.get("profile") else "",
             (" · 초점: %s(후보는 이 회차만, 다른 회차는 비교 기준)" % ", ".join(rep["focus"])) if rep.get("focus") else ""))
    print("문단 %d · 문장 %d · 평균 %s어절 · '것' %s/100어절 · 물음표 문장 %d"
          % (st["문단"], st["문장"], st["평균 문장 어절"], st["'것' 100어절당"], st["물음표 문장"]))
    if rep["register"]:
        print("문체(평서문): " + ", ".join("%s %d" % kv for kv in sorted(rep["register"].items(), key=lambda x: -x[1])))
    if rep["ai_density"]:
        d = rep["ai_density"]
        print("형태 후보가 있는 문단: " + " · ".join("%s %d" % tuple(kv) for kv in d[:15]) + (" …" if len(d) > 15 else "")
              + ("  → 몰린 구간: " + ", ".join(rep["clusters"]) if rep["clusters"] else ""))
    sections = (("1. 기계 점검 — 모델이 글자로 볼 수 없는 것(보이지 않는 문자). 지운다", lambda f: f["role"] == "기계"),
                ("2. 형태 찾기 — 표현·틀이 있는 곳과 횟수. 문맥을 읽고 고침·둠·확인을 정한다", lambda f: f["role"] == "형태" and f["group"] != "N"),
                ("2-N. 형태 찾기(소설·에세이 서술)", lambda f: f["role"] == "형태" and f["group"] == "N"),
                ("3. 읽기 안내 — 뜻을 봐야 하는 곳의 후보(넓게 모음). 읽고 판단한다", lambda f: f["role"] == "읽기"))
    for title, pred in sections:
        if title.startswith("2-N") and rep["genre"] not in N_ONLY:
            continue
        items = sorted([f for f in rep["findings"] if f["count"] and pred(f)], key=lambda f: (-f["count"], f["id"]))
        print("\n## " + title)
        if not items:
            print("- 없음")
        for f in items:
            locs = []
            for h in f["hits"]:
                if h["loc"] not in locs:
                    locs.append(h["loc"])
            print("- %s %s — %d곳 (%s%s)%s" % (f["id"], f["name"], f["count"], ", ".join(locs[:6]), " …" if len(locs) > 6 else "",
                                             " · 이 장르에선 흔함" if f["common_in_genre"] else ""))
            for h in f["hits"][:3]:
                print("    %s: %s" % (h["loc"], snip(h["text"], 90)))
    m = rep.get("metrics")
    if m:
        print("\n## 4. 수치 — 판정 없음(문서 전체 경향은 원고를 읽고 이 값을 근거로 판단한다)")
        for r_ in m["rows"]:
            if r_["always"] or not re.match(r"^0(?:\D|$)|^없음|^소제목 4개 미만", r_["value"]):
                print("- %s: %s%s" % (r_["name"], r_["value"], (" · " + r_["note"]) if r_["note"] else ""))
    quoted = [(f, q) for f in rep["findings"] for q in f["quoted"]]
    if quoted:
        print("\n## 대사 안 — 편집 후보: 문맥을 읽고 판단한다(인물의 말버릇·사투리·높임은 지킨다)" if rep.get("genre") in DIALOGUE_GENRES
              else "\n## 인용 안 — 기본 보호(고치지 않고 표시만)")
        for f, q in quoted[:10]:
            print("- %s %s: %s %s" % (f["id"], f["name"], q["loc"], snip(q["text"], 70)))
    if rep.get("fiction"):
        print_fiction(rep["fiction"])


# ================================================================ compare

EXTRA_UNITS = ["번째", "번", "명", "개", "달", "시간", "시", "주", "회", "장", "권", "대", "층", "살", "사람", "마리", "잔", "병", "채", "벌", "켤레", "군데", "곳",
               "가지", "분", "초", "해", "날", "밤", "걸음", "배", "쪽", "줄", "통", "알", "그루", "송이", "자루", "칸", "판", "편", "곡", "차례", "건", "평", "쌍", "척",
               "끼", "년", "원", "호", "세", "위", "등", "점", "월", "일", "개월", "주일", "퍼센트", "달러", "엔", "유로", "도", "킬로", "미터", "그램", "리터"]
UNITS_KO = ["퍼센트포인트", "퍼센트", "페이지", "개월", "개국", "포인트", "시간", "분기", "번째", "가지", "단계", "달러", "유로", "위안",
            "년", "월", "일", "시", "분", "초", "명", "개", "회", "차", "배", "위", "곳", "건", "장", "권", "대", "쪽", "세",
            "살", "층", "평", "호", "번", "종", "점", "원", "엔", "주"]
UNITS_LAT = ["kWh", "GHz", "MHz", "%p", "km", "cm", "mm", "nm", "kg", "mg", "mL", "ml", "GB", "MB", "KB", "TB", "Hz",
             "kW", "°C", "%", "‰", "℃", "W", "V", "m", "g", "L"]
NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:(?<![A-Za-z0-9_.])(?P<sign>[+\-−–±])(?=[₩$€£¥]?\d))?(?P<cur>[₩$€£¥])?\s?(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"(?:\s?(?P<mag>천만|백만|십만|천억|백억|십억|조|억|만|천))?(?:\s?(?P<unit>(?:%s)|(?:%s)(?![A-Za-z])))?"
    % ("|".join(map(re.escape, sorted(set(UNITS_KO) | set(EXTRA_UNITS), key=len, reverse=True))), "|".join(map(re.escape, UNITS_LAT))))
DATE_RE = re.compile(r"(?<![0-9])(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})\.?(?![0-9])")
DATE_TAIL_RE = re.compile(r"(?<=[~∼～])\s?(0?[1-9]|1[0-2])\.(0?[1-9]|[12]\d|3[01])\.?(?![\d.])")  # 날짜 범위의 뒤쪽 달.날
YM_RE = re.compile(r"(?<![\d.,])((?:19|20)\d{2})\.(0?[1-9]|1[0-2])(?![\d.,%])(?!\s?(?:%|퍼센트|배|점|kg|g|km|m|원|달러|명|개))")  # 연.월(2024.3)
RANGE_RE = re.compile(r"(?<![\d.,])(\d+(?:\.\d+)?)\s?[~∼～\-–]\s?(\d+(?:\.\d+)?)\s?((?:%s)|(?:%s)(?![A-Za-z]))"
                      % ("|".join(map(re.escape, sorted(set(UNITS_KO) | set(EXTRA_UNITS), key=len, reverse=True))), "|".join(map(re.escape, UNITS_LAT))))
ORD_RE = re.compile(r"(?<![가-힣])제\s?(\d{1,4}(?=[가-힣]|\s|$)|[일이삼사오육륙칠팔구십백천]{1,4}(?=\s?(?:자|회|차|장|조|항|호|권|부|편|과|기|대|종|번|위|군|단|막|강|절|관|국|구|형|류|쪽)(?:[^가-힣]|$|[은는이가을를의에도와과로])))")  # 한글 수는 뒤에 차례 단위가 올 때만(제삼자·제이회). 제일(가장)은 수가 아니다
URL_RE = re.compile(r"https?://[^\s<>()\"'“”‘’]+|www\.[^\s<>()\"'“”‘’]+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
CODE_RE = re.compile(r"`[^`\n]+`")
PATH_RE = re.compile(r"(?<![\w/])(?:[A-Za-z]:\\|\.{1,2}/|~/)(?:[\w.\-]+[/\\])*[\w.\-]+")
LATIN_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.+#\-]*(?<![.\-])")
QUOTE_ITEM_RE = re.compile(r"“([^“”\n]{1,300})”|\"([^\"\n]{1,300})\"|「([^「」\n]{1,300})」|『([^『』\n]{1,300})』")  # 인용 항목: 큰따옴표·낫표(작은따옴표 ‘’는 강조·속마음이라 넣지 않는다)
SHORT_DATE_RE = re.compile(r"(?<![\d/.])(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?![\d/])")
IDIOM_NUM_RE = re.compile(r"(?<![가-힣])(?:두|세|네)\s?마리\s?토끼|(?<![가-힣\d])(?:백|천|만)\s?번(?=\s|을|이나|이고|씩|도)")
ENUM_RE = re.compile(r"(?m)^[ \t]*(?:[-*•·▪◦][ \t]*)?\(?\d{1,2}[.)](?=\s|$)|(?<![\w(./,~∼～\-–])\d{1,2}\)(?=\s)|\(\d{1,2}\)(?=\s|[가-힣])")  # 목록 번호
HALF_HOUR_RE = re.compile(r"(?:(?<![\d.])\d{1,2}|(?<![가-힣])(?:열한|열두|열|한|두|세|네|다섯|여섯|일곱|여덟|아홉))\s?시\s?반(?:(?![가-힣]{2})|(?=까지|부터|쯤에))")

GUARDS = [
    ("부정", r"않|없|아니|아닙|(?<![가-힣])못(?=\s|하|했|해|한|할|함|합)|(?<![가-힣])안(?= [가-힣]|하|할|해|했|한|돼|되|된)|금지|불가"),  # 잘못의 '못'은 부정이 아니다  # 못할·안돼처럼 붙여 쓴 꼴도 센다(못 할 → 못할은 띄어쓰기)
    ("범위·정도", r"약(?=\s?\d)|최대|최소|\d\S*\s?(?:이상|이하|미만|초과)|가량|(?<![가-힣\d])일부(?!러)|모든|전부|전체|대부분|모두|오직|단지"),  # 28일부터·제조일부터·일부러는 아니다
    ("양태·추측", r"수 있|수도 있|가능|보인다|보입니다|보이며|추정|예상|전망|듯|것 같"),
    ("의무·단정", r"반드시|꼭|항상|절대|결코|해야|하십시오|말아야"),
    ("시간·상태", r"아직|이미|예정|계획|추진|검토 중|진행 중|완료|마쳤|끝났|향후|현재"),
    ("귀속·출처", r"따르면|밝혔|말했|전했|주장|발표|에 의하면"),
    ("인과", r"때문|따라서|그래서|그 결과|덕분|탓|로 인해|므로(?!써)|니까|결과적으로"),  # -므로써(틀린 표기) → -ㅁ으로써는 수단이지 인과가 아니다
    ("강도", r"(?<![가-힣])(?:매우|정말|진짜|너무|아주|꽤|상당히|무척|몹시|굉장히|엄청|많이|조금|좀|약간|다소|훨씬|더욱|극히|겨우|불과|살짝|한층|가장|더|덜)(?![가-힣])"),
    ("진행·결과 상태", r"(?:고|아|어|여|해|워|와|져|려|혀|겨|켜|쳐|펴|서|놔|둬)\s?있(?=다|었|는|던|습|어|으|고|지|기)|는 중이"),
    ("의도·시도", r"(?:려고|으려고|려|으려|고자)\s?(?:하|했|한|할|해|합|하였)|려던|으려던|(?<![올내])려다가"),
]
GUARD_RE = [(name, re.compile(p)) for name, p in GUARDS]


TITLE_WORDS = (r"(?:씨|님|군|양|연구원|연구위원|대표님|대표|팀장님|팀장|과장님|과장|부장님|부장|차장|실장|본부장|국장|교수님|교수|박사님|박사|"
               r"선생님|선생|기자|의원|장관|사장님|사장|이사|위원장|위원|작가님|작가|감독님|감독|선수|원장|총장|회장님|회장|대리|주임|변호사|판사|"
               r"검사|목사님|목사|신부님|신부|스님|수녀|매니저)")
NAME_BEFORE_TITLE = re.compile(r"(?<![가-힣])([가-힣]{2,4})\s?" + TITLE_WORDS + r"(?=[\s,.!?…은는이가을를의에도와과께]|$)")
NOT_NAME_TITLE = set("신임 전임 현직 전직 초대 공동 수석 책임 선임 담당 명예 상임 비상임 사외 우리 저희 해당 각각 모든 여러 당시 예비 차기 후임 "
                     "담당자 소속 대학 학교 회사 연구소 병원 우리팀 이번 지난 다음 첫째 모두".split())


def name_candidates(text, fiction=False):
    """이름 후보: 직함·호칭 앞의 2~4음절 말(김민수 연구원), 소설이면 인물 판별로 찾은 이름. 완전한 개체명 인식이 아니라 보호용 후보다."""
    c = Counter()
    for mo in NAME_BEFORE_TITLE.finditer(text):
        n = mo.group(1)
        if n not in NOT_NAME_TITLE and n not in NOT_NAME_WORD and not re.search(r"(?:의|에|와|과|은|는|을|를|도|만|로)$", n) \
                and not any(k.startswith(n) for k in KIN_WORDS):  # '그가 변호사', '아버님'(아버+님)은 이름이 아니다
            c[n] += 1
    if fiction:
        try:
            import consistency as cs
            doc = Doc(text, "auto")
            found, total = cs.find_names(doc)
            for n in found:
                if n in NOT_NAME_WORD or n.endswith("들") or any(k.startswith(n) for k in cs.KIN) or (len(n) >= 3 and n.endswith(("장", "관", "원"))):
                    continue  # 대명사·복수·친족어 조각(아버)·직함(○○계장 같은 것)은 이름 후보가 아니다
                c[n] = max(c[n], total[n])
        except Exception:
            pass
    return c


KIN_WORDS = ("아버지", "어머니", "아버님", "어머님", "할아버지", "할머니", "할아범", "할멈", "오라버니", "형님", "누님", "언니", "오빠", "누나", "선생님", "사부님")
NOT_NAME_WORD = set("그가 그는 그녀 그녀가 그녀는 그들 이들 저들 우리 저희 너희 자기 여자 남자 사내 노인 아이 소년 소녀".split())


def sentence_shape(text):
    """서술문의 모양(판정 없음): 문장 수, 길이 변동계수, 가장 흔한 문장 끝(마지막 한글 두 글자)과 그 비율."""
    d = Doc(text)
    sents = [x for p, x in d.flat if not d.quoted_sent(p, x)]
    lens = [eojeol(x) for x in sents]
    ends = Counter(t[-2:] for t in (re.sub(r"[^가-힣]", "", x) for x in sents) if len(t) >= 2)
    if len(lens) < 2:
        return {"n": len(sents), "cv": None, "ends": dict(ends)}
    m = sum(lens) / len(lens)
    sd = (sum((v - m) ** 2 for v in lens) / (len(lens) - 1)) ** 0.5
    return {"n": len(sents), "cv": round(sd / m, 2) if m else None, "ends": dict(ends)}


LIST_RUN = re.compile(r"[가-힣A-Za-z0-9]{1,10}(?:\s*[,·]\s*(?:그리고\s+|및\s+)?[가-힣A-Za-z0-9]{1,10}){2,}")
LIST_SEP = re.compile(r"\s*[,·]\s*(?:그리고\s+|및\s+)?")


def list_item_losses(src_text, out_text):
    """원문 한 문장 안의 셋 이상 나열(A, B, 그리고 C / A·B·C)에서 수정본 전체에 없는 항목. 나열 항목은 필자의 주장이라
    문장 안에서 빠지면 의미 경계 검사로는 안 잡힌다. 막지 않고 '읽고 판단'으로 보인다. 나열이 통째로 사라진 것은 '지운 문장'에서 보인다."""
    try:
        import consistency as cs
    except Exception:
        return []
    out_n, res = nfc(out_text), []
    for m in LIST_RUN.finditer(nfc(src_text)):
        items = [x for x in LIST_SEP.split(m.group(0)) if x]
        if len(items) < 3:
            continue
        stems = [cs.stem(x)[0] for x in items]
        gone = [x for x in stems if x and x not in out_n]
        if gone and len(gone) < len(stems):
            res.append("[나열 항목 사라짐] %s (원문 나열: %s)" % (", ".join("'%s'" % g for g in gone), snip(m.group(0), 40)))
    return res


BLOCK_STOP = set(("나 너 저 그 이 우리 저희 너희 그녀 그들 이들 저들 자기 자신 제 내 네 그것 이것 저것 거기 여기 저기 그곳 이곳 "
                   "그런데 그리고 하지만 그래서 그러나 그러면 또한 또 결국 다만 한편 즉 게다가 물론 막상 "
                   "매우 정말 너무 아주 더 좀 잘 안 못 다시 이미 아직 바로 먼저 계속 모두 다 조금 약간 그냥 한번 한 번 "
                   "것 수 때 등 중 뿐 대로 만큼 듯 줄 적 채 데 바 곳 쪽 뒤 앞 위 속 안").split())
BLOCK_SKIP_PREFIX = ("하", "했", "한", "할", "해", "되", "됐", "된", "될", "돼", "있", "없", "않", "같", "싶", "였", "이었", "이라", "이다",
                     "거였", "거야", "거다", "것이", "거라", "건데", "거지", "그렇", "그런", "이런", "저런")
SPEECH_KEYS = ("말했", "말하", "대답", "답했", "물었", "물어", "외쳤", "외치", "소리", "중얼", "속삭", "덧붙", "되물", "대꾸", "말을")


PRED_END = re.compile(r"(?:다|고|며|면|서|니|지|게|요|죠|네|자|라|도록|려고|면서|지만|는데|은데|던|어|아|여|해|워|와|야)$")
NOUN_PARTICLES = ("이", "가", "을", "를", "에", "에서", "로", "으로", "와", "과", "의", "도", "만", "에게", "한테", "까지", "부터", "처럼", "보다",
                  "에는", "에도", "에서는", "에서도", "으로는", "으로도", "로는", "로도", "에게는", "에게도", "과는", "와는")


def _is_pred(tok):
    """활용하는 말(동사·형용사)로 보이는가: 명사는 어간 전체로 비교하고, 활용하는 말만 느슨하게 비교한다."""
    import consistency as cs
    st, p = cs.stem(tok)
    if p in NOUN_PARTICLES:
        return False
    if p in ("은", "는", "을", "를") and st and (st[-1] in "있없하되" or jong(st[-1]) == 20):
        return True  # 놓여있는, 했던 등
    if p:
        return False
    return bool(PRED_END.search(tok)) or (len(tok) >= 2 and jong(tok[-1]) in (4, 8) and tok[-1] not in "원실관장선단인민군면산강섬성"
                                          and not tok.endswith(("병원", "학원")))


def _units(text):
    """내용어 단위(토큰, 짧은 키): 짧은 키(조사를 뗀 어절의 앞 두 음절)는 말하는 동사·피동 판정에만 쓰고,
    있음·없음은 명사는 어간 전체로, 활용하는 말만 느슨하게 본다(block_propositions). 기능어·보조 용언은 뺀다."""
    import consistency as cs
    out = []
    for tok in re.findall(r"[가-힣A-Za-z]+", nfc(text)):
        st, p = cs.stem(tok)
        if tok in BLOCK_STOP or st in BLOCK_STOP or tok in ("그게", "그건", "이건", "저건", "내가", "제가", "네가") \
                or (len(tok) >= 2 and tok[-1] in "은는이가을를도의에" and tok[:-1] in BLOCK_STOP):
            continue  # 대명사·지시어에 조사가 붙은 꼴(나는, 그를, 이를)
        if st == tok and len(tok) == 2 and tok[1] in "을를이가은는에로와과도만의":
            st, p = tok[0], tok[1]  # 한 음절 명사 + 조사(집에, 말을): 조사 떼기가 포기한 꼴
        if len(st) == 1:
            if p in ("을", "를", "이", "가", "은", "는", "에", "로", "와", "과", "도", "만", "의"):
                out.append((tok, st))
            continue
        if len(st) <= 3 and st.startswith(BLOCK_SKIP_PREFIX):
            continue
        out.append((tok, st[:2]))
    return out


def block_propositions(src_text, out_text, window=2):
    """수정 블록마다 전후 명제 대조(사실만 보인다, 판정은 모델이): 원문 블록에만 있고 수정본의 같은 자리·앞뒤 문장에 없는 말
    (고유 정보 사라짐), 수정본에만 있고 원문 앞뒤에도 없는 말(새 정보), 능동 → 피동(행위자), 말하는 동사 변화."""
    import consistency as cs
    S_, O_ = Doc(src_text), Doc(out_text)
    ss, os_ = [(p, x) for p, x in S_.flat], [(p, x) for p, x in O_.flat]
    nospace = lambda seq: re.sub(r"\s+", "", nfc(" ".join(x for _p, x in seq)))

    def root(t):  # 활용에 덜 흔들리는 어간 열쇠: 첫 음절 + 둘째 음절의 첫소리(놓여있는·놓인·놓여 → 놓ㅇ)
        st = cs.stem(t)[0]
        return st[0] + str((ord(st[1]) - 0xAC00) // 588) if len(st) >= 2 and "가" <= st[1] <= "힣" else None

    def roots(seq):
        return {r_ for _p, x in seq for t in re.findall(r"[가-힣]+", nfc(x)) for r_ in [root(t)] if r_}
    out = []
    for i1, i2, j1, j2 in _gate_blocks(ss, os_):
        sb, ob = ss[i1:i2], os_[j1:j2]
        o_win = nospace(os_[max(0, j1 - window):j2 + window])
        s_win = nospace(ss[max(0, i1 - window):i2 + window])
        su, ou = _units(" ".join(x for _p, x in sb)), _units(" ".join(x for _p, x in ob))
        lost = [(t, k) for t, k in su if k not in o_win]  # 엄격(말하는 동사·피동 판정용)
        new = [(t, k) for t, k in ou if k not in s_win]
        o_roots, s_roots = roots(os_[max(0, j1 - window):j2 + window]), roots(ss[max(0, i1 - window):i2 + window])
        win_stems_o = {cs.stem(x)[0] for _p, y in os_[max(0, j1 - window):j2 + window] for x in re.findall(r"[가-힣]+", nfc(y))}
        win_stems_s = {cs.stem(x)[0] for _p, y in ss[max(0, i1 - window):i2 + window] for x in re.findall(r"[가-힣]+", nfc(y))}
        # 한 글자 교정(가디건 → 카디건)·표기 교정은 짝이 있으면 뺀다
        def paired(t, others):  # 글자 한두 개만 다른 짝(가디건 → 카디건, 기사 → 기차): 교정인지 뜻 변화인지는 모델이 판단한다
            st = cs.stem(t)[0]
            return next((u for u, _k in others if cs.stem(u)[0] != st and len(st) >= 2
                         and (cs.one_edit(st, cs.stem(u)[0]) or _near(st, cs.stem(u)[0]))), None)
        def present(t, win, win_roots):  # 명사는 어간 전체, 활용하는 말은 짧은 키나 어간 열쇠로
            st = cs.stem(t)[0]
            win_toks = win_stems_o if win is o_win else win_stems_s
            if len(st) >= 2 and st in win:
                return True
            if len(st) >= 2 and any("가" <= c <= "힣" and jong(c) == 0 and st[:i] + chr(ord(c) + 19) + st[i + 1:] in win for i, c in enumerate(st)):
                return True  # 사이시옷을 넣은 꼴: 화로 → 화롯(불), 개수 → 갯수
            bare = "".join(chr(ord(c) - 19) if "가" <= c <= "힣" and jong(c) == 19 else c for c in st)
            if len(st) >= 2 and bare != st and bare in win:
                return True  # 화롯불 → 화로 불
            if _is_pred(t):
                if any(len(u) == len(st) and u != st and cs.one_edit(st, u) for u in win_toks):
                    return False  # 먹었다 ↔ 먹였다, 열었다 ↔ 열렸다: 사동·피동일 수 있어 바뀐 낱말로 보인다
                return st[:2] in win or root(t) in win_roots
            return False
        lost_c = [(t, k) for t, k in su if not present(t, o_win, o_roots)]
        new_c = [(t, k) for t, k in ou if not present(t, s_win, s_roots)]
        swaps = [(t, paired(t, new_c)) for t, _k in lost_c if paired(t, new_c)]
        sw_old, sw_new = {a for a, _b in swaps}, {b for _a, b in swaps}
        lost2 = [(t, k) for t, k in lost_c if t not in sw_old]
        new2 = [(t, k) for t, k in new_c if t not in sw_new]
        notes = []
        for t, k in lost:
            for u, kk in new:
                if u[0] == t[0] and re.search(r"(?:진|된|되는|된다|졌다|되었다|됐다)$", u) and not re.search(r"(?:진|된|되는|된다|졌다|되었다|됐다)$", t) \
                        and not re.search(r"^.(?:지|진|질|짐|져|졌|되|된|될|됨|돼|됐)", t):  # 원문이 이미 -지다·-되다 동사면 시제만 바뀐 것(빠진다 → 빠졌다)
                    notes.append("능동 → 피동 '%s' → '%s': 행위자가 사라지거나 바뀌지 않았는지" % (t, u))
                if k.startswith(SPEECH_KEYS) and kk.startswith(SPEECH_KEYS) and k != kk:
                    notes.append("말하는 동사 '%s' → '%s': '대답·물음'이면 앞뒤에 질문·대답이 있어야 한다" % (t, u))
        if not (lost2 or new2 or notes or swaps):
            continue
        where = ("원문 %d문단" % (sb[0][0] + 1)) if sb else ("수정문 %d문단" % (ob[0][0] + 1))
        kind = "지움" if not ob else ("새로 씀" if not sb else "고침")
        out.append({"where": where, "kind": kind, "span": (i1, i2, j1, j2), "lost": sorted({t for t, _k in lost2}), "new": sorted({t for t, _k in new2}),
                    "swapped": ["%s → %s" % ab for ab in swaps],
                    "notes": sorted(set(notes)), "src": " ".join(x for _p, x in sb), "out": " ".join(x for _p, x in ob)})
    rank = lambda b: (0 if b["notes"] else 1 if (b["kind"] == "지움" and b["lost"]) else 2 if b["lost"] else 3 if b["new"] else 4)
    return sorted(out, key=rank)


# 실제 원고 시험에서 드러난 현상을 새로 지은 문장으로 옮긴 사례(반드시 잡을 실패 / 올리지 말 좋은 수정). 새 판마다 selftest로 다시 돈다.
REAL_MUST_FLAG = [
    ("이사는 끝났다고 짐이 정리된 건 아니었다. 상자를 풀고, 낡은 선반을 정리하고, 쓸 만한 그릇은 닦아야 했다. 문을 열자 먼지 냄새가 났다.",
     "이사는 끝났다고 짐이 정리된 건 아니었다. 상자를 풀고, 쓸 만한 그릇은 닦아야 했다. 문을 열자 먼지 냄새가 났다.", "선반"),
    ("현관에 우산이 세워져 있었다.\n\n“생각보다 넓네?”\n\n뒤에서 반장이 말했다.\n\n옆에 있던 인부가 웃었다.",
     "현관에 우산이 세워져 있었다.\n\n“생각보다 넓네?”\n\n뒤에서 반장이 대답했다.\n\n옆에 있던 인부가 웃었다.", "말하는 동사"),
    ("마당에는 낙엽이 쌓여 있었다. 창고 앞에서는 준호가 버린 나뭇가지를 벽 옆에 모아 두고 있었다. 나를 보자 손을 들어 인사했다.",
     "마당에는 낙엽이 쌓여 있었다. 창고 앞에서는 준호가 버려진 나뭇가지를 벽 옆에 모아 두고 있었다. 나를 보자 손을 들어 인사했다.", "능동 → 피동"),
    ("측정값 변화 없음. 온도 정상. 기사가 절삭 속도를 더 올렸다. 잠시 뒤였다.", "측정값 변화 없음. 온도 정상. 기사가 날을 더 빨리 돌렸다. 잠시 뒤였다.", "날을"),
    ("관리인은 장부를 하나씩 넘기며 사정을 설명했다. 비를 맞으며 편지에 그 이름을 적은 사람도 있었다. 그는 말이 없었다.",
     "관리인은 장부를 하나씩 넘기며 사정을 설명했다. 편지에 그 이름을 적은 사람도 있었다. 그는 말이 없었다.", "맞으며"),
]
REAL_MUST_REJECT_EXTRA = [  # 알려진 실패 가운데 블록 대조 밖에서 잡는 것(독립 사건을 -자로 이음)
    ("유나가 보온병을 열었다.\n\n나는 창문을 닫았다. 유나는 보온병 뚜껑을 닫았다.\n\n창밖이 어두웠다.",
     "유나가 보온병을 열었다.\n\n내가 창문을 닫자 유나는 보온병 뚜껑을 닫았다.\n\n창밖이 어두웠다.", "사건 연결"),
]
GATE_EV = (  # 고유 정보 삭제의 근거 문장 시험(새로 지은 문장)
    "창고는 어수선했다.\n\n젖은 상자를 들어내고, 깨진 병을 치우고, 쓸 물건은 따로 모아야 했다.\n\n정리는 길었다.",
    "창고는 어수선했다.\n\n젖은 상자는 들어내고 쓸 물건은 따로 모아야 했다.\n\n정리는 길었다.",
    "현장은 조용했다.\n\n기사가 절삭 속도를 더 올렸다.\n\n먼지가 일었다.", "현장은 조용했다.\n\n기사가 날을 더 빨리 돌렸다.\n\n먼지가 일었다.",
    "나는 손을 뗐다. 기계가 멈췄다. 소음이 잦아들었다. 바람이 불었다.\n\n반장이 다가왔다. 나는 손을 뗐다. 모두 숨을 골랐다.",
    "나는 손을 뗐다. 기계가 멈췄다. 소음이 잦아들었다. 바람이 불었다.\n\n반장이 다가왔다. 모두 숨을 골랐다.",
    "그는 문을 열었다.\n\n“너도 알다시피 우리는 십 년째 이 마을에 살았잖아. 그러니 떠날 수 없어.”\n\n바람이 불었다.",
    "그는 문을 열었다.\n\n“십 년을 산 마을이야. 떠날 수 없어.”\n\n바람이 불었다.")
REAL_KEEP_WITH_REASON = (  # 앞 문장에 같은 행동이 있어 중복만 지운 수정: 위험으로 잡히되 유지+이유면 남는다
    "지훈이 커튼을 친다.\n\n수아가 천천히 지훈을 바라본다. 아까의 굳은 얼굴은 없다. 지치고 불안한 눈으로 지훈을 바라볼 뿐이다.\n\n잠깐의 정적.",
    "지훈이 커튼을 친다.\n\n수아가 천천히 지훈을 바라본다. 아까의 굳은 얼굴은 없다. 지치고 불안한 눈빛뿐이다.\n\n잠깐의 정적.")
REAL_MUST_PASS = [
    ("팀장이 고개를 들었다. 나는 계기판에서 눈을 떼지 않았다. 경고등이 보였다.", "팀장이 고개를 들었다. 계기판에서 눈을 떼지 않았다. 경고등이 보였다."),
    ("현관을 봤다. 처음에는 기름얼룩인 줄 알았다. 한숨을 한번 쉬었다.", "현관을 봤다. 처음에는 기름 얼룩인 줄 알았다. 한숨을 한 번 쉬었다."),
    ("바람이 찼다. 얇은 가디건을 여몄다. 단추 하나가 없었다.", "바람이 찼다. 얇은 카디건을 여몄다. 단추 하나가 없었다."),
    ("창밖이 어두웠다. 나무 가지가 흔들렸다. 개가 짖었다.", "창밖이 어두웠다. 나뭇가지가 흔들렸다. 개가 짖었다."),
    ("그가 장갑을 벗어들었다. 걸음을 서두르지는 않았다. 서두르지도 늦추지도 않은 걸음으로 걸어갔다.",
     "그가 장갑을 벗어 들었다. 서두르지도 늦추지도 않은 걸음으로 걸어갔다."),
]


def tense_of(sentence):
    """문장 끝 서술어의 시제를 거칠게 가른다: 과거(받침 ㅆ, '있'은 제외) / 전망('-겠-', '-ㄹ 것이다', '예정') / 현재. 대사·물음은 보지 않는다."""
    t = re.sub(r"[\s.!?…~”\"’'」』)\]]+$", "", nfc(sentence)).strip()
    t = re.sub(r"^[“\"'‘「『]+", "", t).strip()  # 대사도 본다(내일 갈 거야 → 어제 갔어)
    if not t or not re.search(r"(?:다|요|죠|야|지|어|아|여|네|군|래)$", t):
        return None
    words = t.split()
    last = words[-1]
    if last.startswith(("것", "거", "겁", "걸")) and len(words) >= 2:  # 할 것이다·할 겁니다·할 거예요·할 거다·할 거야
        prev = words[-2]
        if prev and "가" <= prev[-1] <= "힣" and jong(prev[-1]) == 8:
            return "전망"  # 할 것이다
        last = prev + last
    if "겠" in last or re.search(r"예정(?:이다|입니다|이에요)$", t):
        return "전망"
    if any("가" <= ch <= "힣" and jong(ch) == 20 and ch != "있" for ch in last[:-1]):
        return "과거"
    return "현재"


def fine_opcodes(sm):
    """difflib 블록을 잘게: 문장 수가 같은 replace는 문장 1:1로 나눈다(이어진 두 문장이 함께 바뀌어도 따로 판정·되돌림)."""
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace" and i2 - i1 == j2 - j1 and i2 - i1 > 1:
            for k in range(i2 - i1):
                yield "replace", i1 + k, i1 + k + 1, j1 + k, j1 + k + 1
        else:
            yield tag, i1, i2, j1, j2


def change_blocks(src_text, out_text):
    """바뀐 블록 전부(수정문, 앞뒤 한 문장, 원문). 마지막에 원문을 보지 않고 수정문만 새 문장처럼 다시 읽는 데 쓴다."""
    S_, O_ = Doc(src_text), Doc(out_text)
    ss, os_ = list(S_.flat), list(O_.flat)
    res = []
    for i1, i2, j1, j2 in _gate_blocks(ss, os_):
        res.append({"where": ("수정문 %d문단" % (os_[j1][0] + 1)) if j2 > j1 else ("원문 %d문단(지움)" % (ss[i1][0] + 1)),
                    "out": " ".join(x for _p, x in os_[j1:j2]), "src": " ".join(x for _p, x in ss[i1:i2]),
                    "before": os_[j1 - 1][1] if j1 > 0 else "", "after": os_[j2][1] if j2 < len(os_) else ""})
    return res


def fc_notes_g(a, b, kind, genre):
    """자체 시험 도우미: 장르를 주고 문장 짝의 의미 경계에 kind가 있는가(대사는 문단으로 나눠 준다)."""
    return any(kind in x for x in compare("앞이다.\n\n%s\n\n뒤다." % a, "앞이다.\n\n%s\n\n뒤다." % b, genre=genre)["pair_notes"])


def fc_notes(a, b, kind):
    """자체 시험 도우미: 문장 짝의 의미 경계에 kind가 있는가."""
    return any(kind in x for x in compare("앞 문장이다. %s 뒤 문장이다." % a, "앞 문장이다. %s 뒤 문장이다." % b)["pair_notes"])


def _near(a, b):
    """두 낱말이 같은 길이에 음절 하나만 다른가(맞춤법 교정일 수도, 다른 낱말일 수도 있다)."""
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def settle_weak(ga, gb):
    """약한 수량 항목(붙여 쓴 백일장·삼십만원, 애매한 달 이름 이월)은 숫자로 단정하지 않는다: 상대편에 같은 값의 확실한 숫자가 있으면
    같은 것으로 보고(삼십만원 → 30만 원), 나머지는 버린다(경보 없음). 값이 바뀌었는지는 바뀐 낱말로 모델이 본다."""
    for a_, b_ in ((ga, gb), (gb, ga)):
        for (cat_, key_) in list(a_):
            if cat_ == "수량?" and b_.get(("숫자", key_)):
                a_[("숫자", key_)] += a_.pop((cat_, key_))
    for c_ in (ga, gb):
        for k_ in [k for k in c_ if k[0] == "수량?"]:
            del c_[k_]


def number_anchors(text):
    """문장 안의 숫자와 명사를 나온 차례대로(철수·10살·영희·20살). 뜻을 해석하지 않고 차례만 본다."""
    import consistency as cs
    out = []
    for tok in re.findall(r"\S+", nfc(text)):
        core = tok.strip(",.!?…:;()[]“”\"'‘’")
        if re.search(r"\d", core):
            for mo in re.finditer(r"[+\-−–]?\d+(?:[.,]\d+)*[^\d\s:,.]*", core):
                out.append(re.sub(r"(?:이다|입니다|이었다|였다|이고|이며|이|가|은|는|을|를|도|의|에|로|와|과|다)$", "", mo.group(0)))
            continue
        st, p = cs.stem(core) if re.search(r"[가-힣]", core) else (re.sub(r"(?:이|가|은|는|을|를|도|의|와|과)$", "", core), "")
        if not p:
            m_ = re.match(r"(.+?)(?:이다|입니다|이었다|였다|이에요|예요)$", st) or (re.match(r"(.+)다$", st) if len(st) >= 3 and jong(st[-2]) == 0 else None)
            if m_:  # 서술격(영희다·학생이다): 명사로. '먹었다'(ㅆ 뒤)·'크다'(두 글자)는 동사·형용사
                st, p = m_.group(1), "이다"
        if st and st not in BLOCK_STOP and (p or len(st) >= 2 or re.match(r"[A-Za-z]", st)) and not (not p and PRED_END.search(core)):
            out.append(st)
    return out


# ---- 위험 수정 게이트: 사실 항목은 판정 없이 되돌리고(override와 근거가 있으면 유지), 확인 항목은 기본 유지(REVERT 판정이면 되돌림)
LINK_ENDING = re.compile(r"[가-힣](?:자마자|자|니까|므로|더니|는데|은데|면서|지만|어서|아서|여서|해서)(?=[\s,])")
# 위험 수정 게이트: 게이트가 스스로 되돌리는 것은 글자로 확인되는 사실뿐이다. 나머지 신호는 추정이라 확인 목록으로 보이고(기본 유지), 모델이 읽고 틀렸으면 REVERT한다.
FACT_KINDS = ("사실 변화", "부정 뒤집힘", "시간·상태 변화", "범위 변화", "관계 뒤집힘", "인과 뒤집힘")  # 숫자·단위·날짜·인용·식별자·이름 목록(--terms), 부정, 이미·아직, 모두·오직,
# 두 이름의 자리 맞바꿈(철수가 영희를 → 영희가 철수를), 한 문장 안 원인·결과 맞바꿈(비가 와서 취소됐다 → 취소돼서 비가 왔다)
REVIEW_KINDS = ("부정 표현 변화", "시점 표현 변화", "범위 표현 변화", "행위자 변화", "화자·대상·관계 변화", "인과 변화", "새 정보 추가", "고유 정보 삭제",
                "낱말 바꿈", "말하는 동사 변화", "사건 연결 추가", "반복 정보 변화", "강조·한정 변화")
BOUNDARY_CONTEXT_KINDS = ("반복 정보 변화", "강조·한정 변화")
CRITICAL_KINDS = FACT_KINDS + REVIEW_KINDS
REASON_CONTEXT = re.compile(r"앞|뒤|이미|같은|반복|중복|질문|문맥|원문|사용자|작가|지시|출처|설정|남아|남는|그대로|이어지|연속")


def _gate_blocks(ss, os_):
    """바뀐 곳을 문장 묶음(원문 i1:i2 ↔ 수정문 j1:j2)으로 나눈다. difflib가 찾은 바뀐 구간 안을 문장 짝짓기(align)로 n:m 묶음으로 가른다.
    문장 수가 같다고 한 문장씩 짝짓지 않는다: 문장을 합치고 나눈 뒤 한 칸씩 밀린 짝을 만들면 되돌릴 때 문장이 빠지거나 겹친다.
    붙어 있는 지움·새로 씀(짝을 못 지은 고침)은 한 묶음으로 합친다."""
    import difflib
    norm = lambda x: re.sub(r"\s+", " ", nfc(x)).strip()
    sm = difflib.SequenceMatcher(a=[norm(x) for _p, x in ss], b=[norm(x) for _p, x in os_], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or (i2 - i1 == 1 and j2 - j1 == 1):
            out.append((i1, i2, j1, j2))
            continue
        groups, loose, ci, cj = [], [], i1, j1
        for si, oi in align([x for _p, x in ss[i1:i2]], [x for _p, x in os_[j1:j2]], GATE_MOVES):
            g = (ci, ci + len(si), cj, cj + len(oi))
            ci, cj = g[1], g[3]
            indel = not si or not oi
            if indel and groups and loose[-1]:
                groups[-1] = (groups[-1][0], g[1], groups[-1][2], g[3])
            else:
                groups.append(g)
                loose.append(indel)
        out += _merge_loose(groups, ss, os_)
    return out


def _syl_keys(texts):
    """내용어의 첫 음절 열쇠(기능어·대명사 빼고): 문장을 합치거나 나눈 짝을 찾는 데 쓴다."""
    keys = set()
    for t in texts:
        for tok in re.findall(r"[가-힣]+", nfc(t)):
            if tok in BLOCK_STOP or tok in GATE_FUNC or tok in DROPPABLE or (re.sub(PARTICLE_TAIL, "", tok) or tok) in BLOCK_STOP:
                continue
            k = _syl_class(tok[0])
            if k:
                keys.add(k)
    return keys


def _merge_loose(groups, ss, os_):
    """짝 없이 지운 원문 문장(또는 새로 쓴 수정문 문장)의 내용이 이웃 묶음에 70% 넘게 들어 있으면 그 묶음에 합친다
    (설탕 안 넣음. + 제목 → '제목에는 설탕을 넣지 않았어요'처럼 두 문장을 합친 편집을 한 묶음으로 본다)."""
    groups = list(groups)
    changed = True
    while changed:
        changed = False
        for idx, (i1, i2, j1, j2) in enumerate(groups):
            if (i2 > i1) == (j2 > j1):
                continue
            mine = _syl_keys([x for _p, x in ss[i1:i2]]) if i2 > i1 else _syl_keys([x for _p, x in os_[j1:j2]])
            loose_txt = " ".join(x for _p, x in (ss[i1:i2] if i2 > i1 else os_[j1:j2]))
            for nb in (idx - 1, idx + 1):
                if not 0 <= nb < len(groups):
                    continue
                a1, a2, b1, b2 = groups[nb]
                theirs = _syl_keys([x for _p, x in os_[b1:b2]]) if i2 > i1 else _syl_keys([x for _p, x in ss[a1:a2]])
                other = " ".join(x for _p, x in (os_[b1:b2] if i2 > i1 else ss[a1:a2]))  # 이웃 묶음의 맞은편(지운 문장이면 이웃의 수정문)
                same = " ".join(x for _p, x in (ss[a1:a2] if i2 > i1 else os_[b1:b2]))
                if (len(mine) >= 2 and theirs and len(mine & theirs) / len(mine) >= 0.7) or _moved_into(loose_txt, other, same):
                    lo, hi = sorted((idx, nb))
                    g1, g2 = groups[lo], groups[hi]
                    groups[lo:hi + 1] = [(g1[0], g2[1], g1[2], g2[3])]
                    changed = True
                    break
            if changed:
                break
    return groups


def _moved_into(loose, other, same):
    """짧게 지운 문장(그게 전부임.)의 내용어가 모두 이웃 문장의 맞은편에만 있다(…두 시간 반이 전부다): 두 문장을 합친 편집이다.
    내용어는 대명사·접속어·기능어를 뺀 두 음절 이상 낱말이고, 첫 두 음절로 찾는다(전부임 → 전부다)."""
    toks = [w for w in re.findall(r"[가-힣]+", nfc(loose)) if len(w) >= 2 and w not in BLOCK_STOP and w not in GATE_FUNC and w not in DROPPABLE
            and not re.fullmatch(r"(?:그|이|저)(?:게|건|거|걸|것은|것이|것을)", w)]
    if not toks:
        return False
    has = lambda text, w: any(x.startswith(w[:2]) for x in re.findall(r"[가-힣]+", nfc(text)))
    return all(has(other, w) and not has(same, w) for w in toks)


def _indexed_blocks(src_text, out_text):
    S_, O_ = Doc(src_text), Doc(out_text)
    ss, os_ = list(S_.flat), list(O_.flat)
    return S_, O_, ss, os_, _gate_blocks(ss, os_)


def _similar_word(a, b):
    import difflib
    return difflib.SequenceMatcher(a=a, b=b).ratio() >= 0.6


GATE_FUNC = {"그러고는", "그러고", "그리고", "그러나", "하지만", "그런데", "그래서", "따라서", "게다가", "또한", "다시", "달리", "마치", "결국", "이내", "그러자",
             "그때", "그러면", "그저", "이미", "아직", "역시", "오히려", "정확하게는", "정확히는",
             # 담화 표지·말버릇: 지워도 정보가 사라지지 않는다(확신·강도 변화는 강도·양태 경계가 확인 항목으로 보인다)
             "알다시피", "그러니", "그러니까", "솔직히", "사실", "사실은", "말하자면", "다시 말해", "요컨대", "이른바", "이를테면", "아무래도", "어쨌든",
             "아무튼", "있잖아", "근데", "그럼", "글쎄", "그냥", "물론", "확실히", "분명히", "정말", "진짜", "너무", "매우", "아주", "참", "좀",
             # 기능 동사·번역투·빈 결론 표지·과장 부사(STYLE_SAFE): 지우거나 바꿔도 명제가 사라지지 않는다
             "통해", "통하여", "통한", "위해", "위하여", "위한", "대해", "대하여", "관해", "관하여", "있어서", "있어서는", "점에서",
             "결론적으로", "요컨대", "정리하자면", "종합하면", "한마디로", "놀랍게도", "무엇보다도", "동안",
             # 의미 경계 낱말: 사라진 말로 세지 않고 BOUNDARY_CLASSES가 부류별로 판정한다(시간·상태·범위는 강제, 반복·강조·한정은 근거 필요)
             "벌써", "여전히", "아직도", "모두", "모두가", "모두를", "모두의", "전부", "모든", "오직", "오로지", "또", "또다시", "다시금", "재차", "거듭",
             "무엇보다", "그저", "그냥", "한편"}


def _choseong_jungseong(ch):
    k = ord(ch) - 0xAC00
    return (k // 588, (k % 588) // 28) if 0 <= k < 11172 else None


def cs_stem0(w):
    import consistency as cs
    return cs.stem(w)[0]


def _similar_any(w, tokens):
    """사라진(또는 새) 말이 상대편 블록의 어떤 말과 비슷한가: 오타 교정·조사 바꿈·띄어쓰기(되서 → 돼서, 할수있다 → 할 수 있다).
    위험 수정 게이트: 활용하는 말은 첫 음절(첫소리·모음 계열)도 같아야 비슷하다(줄었다 ↔ 늘었다, 같습니다 ↔ 올랐습니다는 끝만 같은 다른 말)."""
    import consistency as cs
    sw = cs.stem(w)[0]
    cands = list(tokens) + [a + b for a, b in zip(tokens, tokens[1:])]
    pred = _is_pred(w)
    head = lambda t: _syl_class(t[0])[:2] if t and _syl_class(t[0]) else None
    return any((_similar_word(w, t) or _similar_word(sw, cs.stem(t)[0])) and (not pred or head(w) == head(t)) for t in cands)


def _swap_far(pair):
    """'a → b'(한 음절만 다른 짝)에서 다른 음절의 자모가 둘 넘게 다른가: 맞춤법 교정(갯수 → 개수, 가디건 → 카디건, 됬 → 됐)은 대개 하나만 다르다."""
    import consistency as cs
    a, _s, b = pair.partition(" → ")
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    if len(a) != len(b) or len(diff) != 1:
        return False
    x, y = diff[0]
    if x in SWAP_PARTICLES and y in SWAP_PARTICLES:
        return False  # 조사·접미사만 바뀌었다(민수뿐 → 민수만, 년째 → 년을)
    return not _jamo_close(x, y)


SWAP_PARTICLES = set("을를이가은는의에로과와도만뿐째서고며면나랑께든요죠다음지으함임")  # 조사·어미 음절(메모체 -음·-함 포함)


ADVERBS = set("반드시 끝내 끝끝내 결국 이미 아직 다시 매우 너무 조금 결코 자주 가끔 늘 항상 벌써 드디어 마침내 비로소 겨우 오히려 차라리 도저히 전혀 거의 "
              "아주 몹시 서로 함께 같이 모두 다만 곧 또 더 덜 가장 매번 계속 점점 차차 슬쩍 살짝 이제 여전히 아마 분명 정말 진짜 꼭 절대 무척 훨씬 "
              "한참 잠시 잠깐 문득 갑자기 마저 역시 특히 굳이 부디 제발 미리 이윽고 영영 도무지 좀처럼 별로 과연 설마 막상 어차피 워낙 대체로 주로 "
              "대개 흔히 종종 이따금 여태 아까 방금 금방 이내 곧장 바로 당장 몰래 억지로 일부러 저절로 스스로".split())  # 부사: 보태도 '새 정보'가 아니다(강도·양태 경계가 본다)
REQUEST_FORM = re.compile(r"(?:십시오|시오|세요|바랍니다|바라요|드립니다|드려요|드리며|주시기|주십|합니다|습니다|ㅂ니다|해요|어요|아요)$")


def _style_spans(text, follow=""):
    """STYLE_SAFE 틀: 이 안의 말은 지워도 명제가 사라지지 않는다(AI 질문 훅, '단순한 X가 아닙니다. 그것은', 과장 수식, 수사적 부정).
    과장 수식 뒤의 명사는 추상어로 한정한다(강력한 태풍 같은 실제 사건은 틀이 아니다)."""
    pats = (r"(?:왜|어떻게|무엇이|무엇을|뭐가|어째서)[^.?!\n]{0,60}\?\s*(?:[^.?!\n]{0,40}\?\s*)*(?:답|이유|정답|비결|해답)은\s*(?:간단|단순|명확|하나)[^.!?\n]{0,12}[.!]?",
            r"(?:답|이유|정답|비결|해답)은\s*(?:간단|단순|명확)(?:합니다|하다|해요|하죠)[.!]?",
            r"(?:혁신적인|획기적인|놀라운|엄청난|강력한|폭발적인|압도적인|대단한)\s+(?:변화|효과|가치|결과|영향|성과|힘|경험|습관)(?:를|을|이|가|의|는|은)?"
            r"(?:\s+(?:가져다주는|가져오는|만드는|주는|일으키는|낳는|지닌|가진))?",
            r"(?:그\s*)?(?:가치|의미|효과|기쁨|감동)(?:는|은|가|이)\s*이루\s*(?:다\s*)?말할\s*수\s*없[가-힣]*",
            r"(?:다시|바꿔|달리)\s*말(?:해|하면|하자면)(?:서)?",  # 담화 표지(다시 말해 = 즉)
            STYLE_NEG.pattern)
    spans = list(_merely_safe_spans(text, follow))  # '단순한 X가 아닙니다'는 뒤 말이 확정 단언일 때만 틀이다
    for pt in pats:
        spans += [m.span() for m in re.finditer(pt, nfc(text or ""))]
    return spans


def _style_safe_lost(w, src_b, follow=""):
    """사라진 말이 원문 블록에서 모두 STYLE_SAFE 틀 안에만 나왔는가(follow: 블록 바로 뒤 문장, 틀의 뒤 말을 보는 데만 쓴다)."""
    t = nfc(src_b or ""); spans = _style_spans(t, follow)
    occ = [m.span() for m in re.finditer(re.escape(w), t)]
    return bool(occ) and all(any(a <= x and y <= b for a, b in spans) for x, y in occ)


def _paraphrase_kept(w, out_b):
    """같은 뜻의 바꿔 말하기: 가능하다 ↔ 수 있다, 불가(능)하다 ↔ 수 없다, 매일·날마다 ↔ ~마다, 요청 어미끼리(부탁드려요 ↔ 주십시오)."""
    o = nfc(out_b or "")
    if re.match(r"가능", w) and "수 있" in o:
        return True
    if re.match(r"불가", w) and "수 없" in o:
        return True
    if w in ("매일", "날마다", "매번") and "마다" in o:
        return True
    return bool(REQUEST_FORM.search(w)) and bool(re.search(r"(?:십시오|세요|바랍니다|드립니다|드려요|주시기)", o))


def _memo_verb_kept(w, toks):
    """메모체 명사형 끝(-음·-ㅁ: 회귀함·떨림·나감·참음·똑같음)으로 끝난 말이 수정문에서 같은 동사의 다른 꼴로 남았는가(회귀했다·떨렸다·나갔다·참았다)."""
    if len(w) < 2 or not ("가" <= w[-1] <= "힣"):
        return False
    if w.endswith("음"):
        stem = w[:-1]
    elif jong(w[-1]) == 16:
        stem = w[:-1] + chr(ord(w[-1]) - 16)
    else:
        return False
    if not stem:
        return False
    for t in toks:
        if len(t) < len(stem):
            continue
        if len(stem) == 1:
            if t[0] == stem[0] and len(t) >= 2:
                return True
        elif t[:len(stem) - 1] == stem[:-1] and _choseong_jungseong(t[len(stem) - 1]) and \
                _choseong_jungseong(t[len(stem) - 1])[0] == _choseong_jungseong(stem[-1])[0]:
            return True
    return False


def _nounish(tok):
    """새 정보로 셀 말: 명사 같은 말(서술어·접속어·부사는 뺀다). 원문에 없던 구체 명사·방법이 새로 생겼는지 보는 데 쓴다."""
    import consistency as cs
    st, p = cs.stem(tok)
    if st in BLOCK_STOP or st in GATE_FUNC or tok in GATE_FUNC or len(st) < 2:
        return False
    if re.search(r"(?:히|게|듯|처럼|같이|듯이)$", st) or REQUEST_FORM.search(tok) or tok in ADVERBS or st in ADVERBS:
        return False
    if p in NOUN_PARTICLES or p in ("은", "는", "이", "가") or (not p and not PRED_END.search(tok) and not _is_pred(tok)):
        return True
    # 받침 ㄹ로 끝나는 맨 명사(생활 방식의 '생활')를 동사 관형형(할·갈)으로 오인한 경우만 바로잡는다
    return (not p and len(tok) >= 2 and ord(tok[-1]) >= 0xAC00 and (ord(tok[-1]) - 0xAC00) % 28 == 8
            and not PRED_END.search(tok) and not re.search(r"[을를할될갈올볼쓸살알울줄]$", tok))  # 날을(목적격 조사)은 아니다


CAUSE_NOUN_MARKERS = {"탓에", "탓으로", "까닭에", "까닭으로", "덕에", "덕분에", "덕택에", "영향으로", "때문에", "때문으로", "바람에", "통에",
                      "결과로", "인해", "인하여", "말미암아"}  # 명사형 인과 표지: 비가 온 탓에 / 폭우로 인해
CAUSE_ENDING = re.compile(r"(?:아서|어서|여서|해서|워서|와서|져서|돼서|니까|므로|느라고|느라|기에|길래)$")  # 어미형 인과 표지
VOWEL_FAMILY = {8: "ㅗ", 9: "ㅗ", 10: "ㅗ", 11: "ㅗ", 13: "ㅜ", 14: "ㅜ", 15: "ㅜ", 16: "ㅜ", 0: "ㅏ", 1: "ㅏ",
                4: "ㅓ", 5: "ㅓ", 18: "ㅓ", 6: "ㅣ", 7: "ㅣ", 20: "ㅣ"}  # 활용에서 줄거나 바뀌는 모음을 한 계열로(오/와, 되/돼, 하/해, 쓰/써, 지/져)


def _event_key(tok):
    """사건 핵심어 열쇠: 첫 음절의 첫소리 + 모음 계열(오니까·온·왔다는 같은 열쇠)."""
    k = ord(tok[0]) - 0xAC00
    if not 0 <= k < 11172:
        return None
    return (k // 588, VOWEL_FAMILY.get((k % 588) // 28, (k % 588) // 28))


CAUSE_LEXICON = ("덕분", "덕택", "까닭", "영향", "때문", "바람", "결과", "여파", "이유", "사유", "탓", "덕", "통")  # 명사형 인과 어휘(+로/으로/에/에서)
CAUSE_NOUN_RE = re.compile(r"^(?:%s)(?:으로|로|에|에서)$" % "|".join(CAUSE_LEXICON))
CAUSE_VERBAL_RE = re.compile(r"^(?:인해|인하여|인한|(?:기인|말미암)(?:해|하여|해서|돼|되어|돼서|아|한|된)|비롯(?:돼|되어|돼서|된))$")  # 비롯해(포함해)는 뺀다


def _is_cause_marker(w, prev=""):
    """중앙 인과 표지 판정: 명사형(탓에·까닭에·덕분에·영향으로·때문에·결과로·여파로·이유로·사유로), 동사형(인해·기인해·비롯돼·말미암아),
    '원인이 되어', 어미형(-아서·-니까·-므로·-느라·-기에·-길래)."""
    w = re.sub(r"[,]$", "", w)
    if CAUSE_NOUN_RE.match(w) or CAUSE_VERBAL_RE.match(w) or w in CAUSE_NOUN_MARKERS:
        return True
    if w in ("되어", "돼", "돼서", "되어서", "되면서") and prev.startswith("원인"):
        return True
    return bool(CAUSE_ENDING.search(w))


def _is_marker_word(w, prev=""):
    """독립된 인과 표지 낱말(탓에·기인해·원인이 되어 등)만: 사건 핵심어에서 뺀다. 어미가 붙은 동사(막히니까·와서)는 사건이라 남긴다."""
    w = re.sub(r"[,]$", "", w)
    return bool(CAUSE_NOUN_RE.match(w) or CAUSE_VERBAL_RE.match(w) or w in CAUSE_NOUN_MARKERS
                or (w in ("되어", "돼", "돼서", "되어서", "되면서") and prev.startswith("원인")))


def _cause_splits(sent):
    """인과 표지 자리에서 원인 절 | 결과 절로 나눌 수 있는 곳들. 1층: 어미형(-아서·-니까·-므로·줄어든 내려서·와서 등)과
    명사형(탓에·까닭에·덕분에·영향으로·때문에·바람에·로 인해 등) 표지를 같은 경계로 본다."""
    toks = nfc(sent).strip().rstrip(".!?…").split()
    out = []
    for k, tok in enumerate(toks[:-1]):
        w = re.sub(r"[,]$", "", tok)
        hit = _is_cause_marker(w, toks[k - 1] if k else "")
        if not hit and len(w) >= 2 and w.endswith("서") and not re.search(r"(?:에서|에게서|로서|으로서)$", w):
            cj = _choseong_jungseong(w[-2])
            k2 = ord(w[-2]) - 0xAC00
            hit = bool(cj) and k2 % 28 == 0 and cj[1] in (0, 1, 4, 5, 6, 9, 10, 11, 14, 15)  # 받침 없는 ㅏㅐㅓㅔㅕㅘㅙㅚㅝㅞ + 서
        if hit:
            out.append((" ".join(toks[:k + 1]), " ".join(toks[k + 1:])))
    return out


INTER_CAUSE = re.compile(r"^(?:그래서|따라서|그러므로|그러니까|그러니|결과적으로|그\s?결과|이\s?결과|그\s?때문에|이\s?때문에|이에\s?따라|그에\s?따라|"
                         r"이로\s?인해|그로\s?인해|이로써|그\s?여파로|이\s?여파로|그\s?탓에|이\s?탓에|그\s?덕분에|이\s?덕분에|그\s?바람에|그\s?까닭에|이\s?까닭에)[,\s]+")


def _edge_keys(t):
    toks = re.findall(r"[가-힣]+", nfc(t))
    keys = set()
    for k, tok in enumerate(toks):
        if tok in BLOCK_STOP or _is_marker_word(tok, toks[k - 1] if k else "") or tok in ("때문", "탓", "까닭", "덕분", "영향", "결과", "원인이", "원인"):
            continue
        key = _event_key(tok)
        if key:
            keys.add(key)
    return keys


def _refine_cause(left, right):
    """명사를 받는 표지(기인해·비롯돼·여파로·때문에 등) 앞에 주제어(~은/는)와 명사구가 따로 있으면, 주제어는 결과이고 표지 바로 앞 명사구가 원인이다
    (도로 침수는 폭우에 기인해 발생했다: 원인 폭우 → 결과 도로 침수). 어미형 표지(-아서 등)와 꾸미는 절(비가 온 탓에)은 그대로 둔다."""
    toks = left.split()
    if len(toks) >= 3:
        marker, prevw = toks[-1], toks[-2]
        noun_marker = bool(CAUSE_NOUN_RE.match(marker) or CAUSE_VERBAL_RE.match(marker) or marker in CAUSE_NOUN_MARKERS
                           or (marker in ("되어", "돼", "돼서", "되어서") and prevw.startswith("원인")))
        if noun_marker:
            tops = [k for k, t in enumerate(toks[:-2]) if re.search(r"[은는]$", t)]
            if tops:
                t = tops[-1]
                return " ".join(toks[t + 1:-1]), " ".join(toks[:t + 1]) + " " + right
    return left, right


def _causal_edges(flat):
    """원인 → 결과 관계들(대조 짝과 무관): 문장 안의 인과 표지와, 문장 첫머리의 연결어(그래서·그 결과·이에 따라 등)로 이어진 앞 문장 → 이 문장."""
    edges = []
    for k, (_p, sent) in enumerate(flat):
        t = nfc(sent).strip()
        for left, right in _cause_splits(t):
            cause_t, effect_t = _refine_cause(left, right)
            edges.append((_edge_keys(cause_t), _edge_keys(effect_t), {k}, t))
        m = INTER_CAUSE.match(t)
        if m and k > 0:
            edges.append((_edge_keys(flat[k - 1][1]), _edge_keys(t[m.end():]), {k - 1, k}, nfc(flat[k - 1][1]).strip() + " " + t))
    return edges


def _edge_reversals(src_flat, out_flat):
    """원문의 인과 관계가 수정문에서 방향이 뒤집혔는가. 원인과 결과에 함께 든 말(발생했다 등)은 빼고 구별하는 말로 본다.
    같은 방향의 관계가 수정문에 그대로 있으면 역전이 아니다. 반환: [(설명, 원문 문장 번호들, 수정문 문장 번호들)]"""
    ov = lambda x, y: len(x & y) / min(len(x), len(y)) if x and y else 0
    se, oe = _causal_edges(src_flat), _causal_edges(out_flat)
    res = []
    for c, e, si, st in se:
        c1, e1 = c - e, e - c
        if not c1 or not e1:
            continue
        if any(ov(c1, c2 - e2) >= 0.6 and ov(e1, e2 - c2) >= 0.6 for c2, e2, _oi, _ot in oe):
            continue
        for c2, e2, oi, ot in oe:
            c3, e3 = c2 - e2, e2 - c2
            c1m, e1m = c1 - c3, e1 - e3  # 자리를 옮긴 말만 본다(제자리에 남은 서술어 '발생했다' 등은 뺀다)
            if c3 and e3 and c1m and e1m and ov(c1m, e3) >= 0.6 and ov(e1m, c3) >= 0.6 and ov(c1, c3) < 0.4:
                res.append(("인과 역전: '%s'가 '%s'로" % (snip(st, 36), snip(ot, 36)), si, oi))
                break
    return res


def _cause_reversed(a, b):
    """2층: 두 문장이 모두 인과 표지로 두 절을 잇고, 원인 절과 결과 절의 사건 핵심어(첫소리+모음 계열)가 서로 자리를 바꿨는가."""
    def keys(t):
        out = set()
        for tok in re.findall(r"[가-힣]+", t):
            if tok in BLOCK_STOP or _is_marker_word(tok) or tok in ("때문", "탓", "까닭", "덕분", "영향", "결과", "원인이", "원인"):
                continue
            k = _event_key(tok)
            if k:
                out.add(k)
        return out
    ov = lambda x, y: len(x & y) / min(len(x), len(y)) if x and y else 0  # 작은 쪽 기준 겹침(한쪽 절에 말이 조금 더해져도 같은 사건)

    def splits(t, loose):  # 인과 표지 자리(+ loose: '-서' 없이 줄어든 연결 어미 잠겨·내려·도착해). 한쪽은 반드시 인과 표지가 있어야 한다
        res = list(_cause_splits(t))
        if not loose:
            return res
        toks = nfc(t).strip().rstrip(".!?…").split()
        for k, tok in enumerate(toks[:-1]):
            w = re.sub(r"[,]$", "", tok)
            cj = _choseong_jungseong(w[-1]) if w else None
            if (cj and (ord(w[-1]) - 0xAC00) % 28 == 0 and cj[1] in (0, 1, 4, 6, 9, 14) and len(w) >= 2 and k > 0 and w not in BLOCK_STOP
                    and not re.search(r"(?:에서|에게서|로서|으로서|과|와|가|나|야|라|서|처럼|보다|대로)$", w)):
                res.append((" ".join(toks[:k + 1]), " ".join(toks[k + 1:])))
        return res
    sa, sb = _cause_splits(a), _cause_splits(b)
    if not sa and not sb:
        return None
    for a1s, a2s in splits(a, not sa):
        for b1s, b2s in splits(b, not sb):
            a1, a2, b1, b2 = keys(a1s), keys(a2s), keys(b1s), keys(b2s)
            if a1 and a2 and b1 and b2 and ov(a1, b2) >= 0.6 and ov(a2, b1) >= 0.6 and ov(a1, b1) < 0.4 and ov(a2, b2) < 0.4:
                return "인과 역전: '%s → %s'가 '%s → %s'로" % (snip(a1s, 20), snip(a2s, 20), snip(b1s, 20), snip(b2s, 20))
    return None


# ---- 규칙 고침 안전 처리(STYLE_SAFE): 스킬 규칙(W2b·W2c·W2d·W3a·W3b·W3e·W3f·S6b)이 권하는 표준 고침은 '원문이 처음부터 그렇게 쓰였을 때'처럼 판정한다.
# 수정문이 그 자리를 표준 고침으로 바꿨을 때만 원문을 표준형으로 바꿔 다시 대조하고, 그 블록에 다른 신호(위험·대조 메모)나
# 띄어쓰기·문장부호·같은 명사의 조사·기능어 말고 다른 차이가 하나라도 남으면 기존 위험 신호와 함께 원래 항목을 그대로 둔다.
def _pp(noun, pair):
    """받침에 맞는 조사: _pp('시간', ('이', '가')) → '이'."""
    k = ord(noun[-1]) - 0xAC00 if noun else -1
    return pair[0] if 0 <= k < 11172 and k % 28 else pair[1]


RF_ISS = {  # 위치하다·존재하다의 꼴 → 있다(없다)의 꼴
    "하지 않는다": ("없다",), "하지 않습니다": ("없습니다",), "하지 않았다": ("없었다",), "하지 않았습니다": ("없었습니다",), "하지 않는": ("없는",),
    "하지 않을": ("없을",), "하지 않고": ("없고",), "하지 않으며": ("없으며",), "하지 않아": ("없어",), "하지 않지만": ("없지만",),
    "한다": ("있다",), "하다": ("있다",), "합니다": ("있습니다",), "했다": ("있었다",), "하였다": ("있었다",), "했습니다": ("있었습니다",),
    "하였습니다": ("있었습니다",), "하는": ("있는",), "한": ("있는",), "했던": ("있던", "있었던"), "하던": ("있던",), "할": ("있을",), "하고": ("있고",),
    "하며": ("있으며",), "하면": ("있으면",), "해서": ("있어서",), "하여": ("있어",), "해": ("있어",), "하지만": ("있지만",), "했고": ("있었고",),
    "했으며": ("있었으며",), "했지만": ("있었지만",), "하므로": ("있으므로",), "해요": ("있어요",), "했어요": ("있었어요",),
    "하지 않아요": ("없어요",), "하지 않았어요": ("없었어요",), "하죠": ("있죠",), "했으나": ("있었으나",), "하나": ("있으나",)}
RF_GAJI = {  # (회의 등을) 가지다의 꼴 → (하다의 꼴, 열다의 꼴)
    "가졌습니다": ("했습니다", "열었습니다"), "가졌으며": ("했으며", "열었으며"), "가졌지만": ("했지만", "열었지만"), "가졌는데": ("했는데", "열었는데"),
    "가졌어요": ("했어요", "열었어요"), "가졌으나": ("했으나", "열었으나"), "가졌다고": ("했다고", "열었다고"), "가졌다는": ("했다는", "열었다는"),
    "가진다고": ("한다고", "연다고"), "가진다는": ("한다는", "연다는"), "갖는다고": ("한다고", "연다고"), "갖는다는": ("한다는", "연다는"), "가져요": ("해요", "열어요"), "가졌던": ("했던", "열었던"), "가졌고": ("했고", "열었고"), "가졌다": ("했다", "열었다"), "가집니다": ("합니다", "엽니다"),
    "가진다": ("한다", "연다"), "가지다": ("하다", "열다"), "가지고": ("하고", "열고"), "가지며": ("하며", "열며"), "가지는": ("하는", "여는"),
    "가지기로": ("하기로", "열기로"), "가지려고": ("하려고", "열려고"), "가지면": ("하면", "열면"), "가지자": ("하자", "열자"), "가지게": ("하게", "열게"),
    "가지지": ("하지", "열지"), "가져서": ("해서", "열어서"), "가져": ("해", "열어"), "가진": ("한", "연"), "가질": ("할", "열"), "갖습니다": ("합니다", "엽니다"),
    "갖는다": ("한다", "연다"), "갖기로": ("하기로", "열기로"), "갖는": ("하는", "여는"), "갖고": ("하고", "열고"), "갖게": ("하게", "열게"), "갖자": ("하자", "열자"),
    "갖지": ("하지", "열지")}
RF_EVENTS = ("기자회견", "간담회", "설명회", "토론회", "발표회", "공청회", "시상식", "기념식", "세미나", "워크숍", "회의", "모임", "회견", "논의", "행사",
             "토론", "회담", "면담", "협의", "총회")
RF_EVENT_VERB = ("회의", "논의", "토론", "회담", "면담", "협의", "회견")  # 'N하다'로 붙여 쓸 수 있는 말(회의했다·논의했다)
RF_UIMI = {"가지지 않는다": "없다", "갖지 않는다": "없다", "가졌습니다": "있었습니다", "가졌던": "있었던", "가졌다": "있었다", "가집니다": "있습니다",
           "가진다": "있다", "가지는": "있는", "가지고": "있고", "가지며": "있으며", "가진": "있는", "가질": "있을", "가져": "있어", "갖는다": "있다",
           "갖는": "있는", "갖고": "있고", "가진다는": "있다는", "가진다고": "있다고", "가졌다는": "있었다는", "가져요": "있어요", "가졌어요": "있었어요", "가졌으나": "있었으나", "가지나": "있으나"}
RF_PILYO = {"하지 않는다": ("하지 않다",), "하지 않습니다": ("하지 않습니다",), "하지 않았다": ("하지 않았다",), "하지 않는": ("하지 않은",),
            "한다는": ("하다는",), "한다고": ("하다고",), "합니다": ("합니다",), "했습니다": ("했습니다",), "하였다": ("하였다", "했다"), "한다": ("하다",),
            "했다": ("했다",), "하는": ("한",), "했던": ("했던",), "하던": ("하던", "했던"), "하고": ("하고",), "하며": ("하며",), "하므로": ("하므로",),
            "하지만": ("하지만",), "하면": ("하면",), "하기": ("하기",), "하게": ("하게",), "해서": ("해서",), "하여": ("하여", "해"), "했고": ("했고",),
            "했으며": ("했으며",), "한": ("했던", "한"), "할": ("할",), "해": ("해",), "해요": ("해요",), "했어요": ("했어요",), "하죠": ("하죠",)}
RF_W3A_NOUNS = ("검토", "논의", "협의", "분석", "조사", "개선", "확인", "점검", "평가", "수집", "교육", "실험", "측정", "심사", "상담", "조정", "보완",
                "수정", "정비", "보수", "수리", "연구", "개발", "설계", "검사", "감사", "홍보", "모집", "선발", "조치", "대응", "준비", "운영", "투표",
                "면접", "발표", "작업", "훈련", "복구", "방역", "교체", "설치", "등록", "접수", "승인", "검증", "보고", "공유", "배포", "실사", "이전")  # 'N하다'가 뜻이 같은 말(행사·사용은 뺀다)
RF_GANEUNG = {"하다": "다", "합니다": "습니다", "했다": "었다", "했습니다": "었습니다", "하며": "으며", "하고": "고", "해서": "어서", "해": "어", "한": "는",
              "할": "을", "하지만": "지만", "하면": "으면", "하므로": "으므로"}
_alt = lambda keys: "|".join(sorted((re.escape(k).replace("\\ ", r"\s+") for k in keys), key=len, reverse=True))


def _rf_iss(m, _out):
    return RF_ISS.get(re.sub(r"\s+", " ", m.group("e")), ())


def _rf_iss_aux(_m, _out):
    return ("있",)


def _rf_gaji(m, _out):
    n, p = m.group("n"), m.group("p")
    h, y = RF_GAJI[m.group("v")]
    return [n + p + " " + h, n + p + " " + y, n + p + " 개최" + h] + ([n + h] if n in RF_EVENT_VERB else [])


def _rf_uimi(m, _out):
    n = m.group("n")
    return (n + _pp(n, ("이", "가")) + " " + RF_UIMI[re.sub(r"\s+", " ", m.group("v"))],)


def _rf_pilyo(m, _out):
    n = m.group("n")
    return [n + _pp(n, ("이", "가")) + " 필요" + f for f in RF_PILYO[re.sub(r"\s+", " ", m.group("t"))]]


def _rf_deo(_m, _out):
    return ("더",)


def _rf_w3a(m, _out):
    x, xp, v, e = m.group("x") or m.group("xb") or "", m.group("xp") or m.group("xd") or "", m.group("v"), m.group("e")
    if m.group("md"):  # 300명에 대한 심층 면접을 실시했다 → 300명을 심층 면접했다
        v = m.group("md") + " " + v
    if not x:
        return [v + e]
    # 자료 검토를 진행했다 → 자료를 검토했다(앞말이 명사일 때), 자료에 대한 분석을 실시했다 → 자료를 분석했다, 담당자가 검토를 진행했다 → 담당자가 검토했다
    return [x + _pp(x, ("을", "를")) + " " + v + e] + ([] if xp else [x + " " + v + e])


def _rf_ganeung(m, _out):
    n, sfx = m.group("n"), RF_GANEUNG[m.group("e")]
    out = [n + "할 수 있" + sfx]
    if n in ("재사용", "재활용", "재이용"):
        out += ["다시 %s 수 있%s" % (w, sfx) for w in ("쓸", "사용할", "활용할", "이용할")]
    return out


def _rf_gwangye(m, out_text):
    v = m.group("v")
    if jong(v[-1]) != 4:  # 관형형(지연된·부족한·많은·나쁜)만
        return ()
    pre = v[:-1] if len(v) >= 2 else v
    toks = re.findall(r"[가-힣]+", nfc(out_text or ""))
    cands = []
    for k, t in enumerate(toks):
        if not t.startswith(pre) or t == v:
            continue
        causal = re.search(r"(?:서|되어|하여|이어|이라|여|어|아|해|돼)$", t) and not re.search(r"(?:에서|에게서|로서|으로서)$", t)
        if causal:
            cands.append(t)
        elif t.endswith("기") and k + 1 < len(toks) and toks[k + 1] == "때문에":
            cands.append(t + " 때문에")
    return cands


def _rf_iss_gwangye(m, _out):
    neg = bool(m.group("ng"))
    return ("없어서", "없기 때문에", "없으므로") if neg else ("있어서", "있기 때문에", "있으므로")


def _rf_gyeongu(m, _out):
    n, tail = m.group("n"), m.group("t") or ""
    if n in ("최악", "최선", "만일", "만약"):
        return ()
    if tail == "에도":
        return (n + "도", n + "에게도")
    return (n + _pp(n, ("은", "는")), n + "에게는", n + "에게", n + ("이라면" if _pp(n, (1, 0)) else "라면"))


def _rf_daehan(m, _out):
    return ((m.group("x"), m.group("x") + " " + m.group("y")),)  # (바꿀 말, 수정문에서 보여야 할 말)


def _hapnida(x):
    """한다체 끝(늘었다·높인다·많다·크다)을 합니다체로(늘었습니다·높입니다·많습니다·큽니다)."""
    if not x.endswith("다") or len(x) < 2:
        return None
    body, last = x[:-1], x[-2]
    k = ord(last) - 0xAC00
    if not 0 <= k < 11172:
        return None
    if x.endswith("는다"):
        return x[:-2] + "습니다"
    if k % 28 == 4 and not x.endswith(("있다", "없다")):  # 높인다 → 높입니다(ㄴ다)
        return body[:-1] + chr(0xAC00 + k - 4 + 17) + "니다"
    if k % 28:
        return body + "습니다"
    return body[:-1] + chr(0xAC00 + k + 17) + "니다"  # 크다 → 큽니다


def _rf_s6b(m, _out):
    n, x, verb = m.group("n"), m.group("x"), m.group("vb")
    inner = {x, re.sub(r"(?:위치|존재)(한다|했다|하였다)$", lambda g: {"한다": "있다", "했다": "있었다", "하였다": "있었다"}[g.group(1)], x),
             re.sub(r"(?:위치|존재)하지\s+않는다$", "없다", x)}
    if not re.search(r"준다|낸다", verb):  # 보여 줍니다·나타냅니다: 합니다체로
        inner = {h for h in (_hapnida(y) for y in inner) if h}
    return [pre + y for y in sorted(inner) for pre in (n + _pp(n, ("을", "를")) + " 보면 ", n + "에 따르면 ")]


RULE_FIXES = (  # (규칙, 원문 모양, 받아들이는 표준 고침들, 자기 표지만으로 자리를 알 수 있는가)
    ("W2c", re.compile(r"(?<![가-힣])위치(?:해|하여|하고)\s+있"), _rf_iss_aux, False),
    ("W2c", re.compile(r"(?<![가-힣])(?:위치|존재)(?P<e>%s)(?![가-힣])" % _alt(RF_ISS)), _rf_iss, False),
    ("W2c", re.compile(r"(?<![가-힣])(?P<n>%s)(?P<p>을|를)\s*(?P<v>%s)(?![가-힣])" % ("|".join(RF_EVENTS), _alt(RF_GAJI))), _rf_gaji, True),
    ("W2c", re.compile(r"(?<![가-힣])(?P<n>의미|의의)(?P<p>를|을)\s*(?P<v>%s)(?![가-힣])" % _alt(RF_UIMI)), _rf_uimi, True),
    ("W2c", re.compile(r"(?<![가-힣])(?P<n>[가-힣]+?)(?P<p>을|를)\s+필요로\s+(?P<t>%s)(?![가-힣])" % _alt(RF_PILYO)), _rf_pilyo, True),
    ("W2d", re.compile(r"(?:(?<=\s)|^)보다(?=\s+[가-힣]+)", re.M), _rf_deo, False),
    ("W3a", re.compile(r"(?<![가-힣0-9])(?:(?P<x>[가-힣0-9,]{1,8}?)(?:(?P<xp>의)|에\s+(?P<xd>대한|관한|대해|대하여|관해|관하여))\s+(?:(?P<md>[가-힣]{2,3})\s+)?"
                       r"|(?P<xb>[가-힣0-9,]{1,8}?)\s+)?(?P<v>%s)(?:을|를)\s*(?:진행|실시|수행)"
                       r"(?P<e>하|했|한|할|합|해)" % "|".join(RF_W3A_NOUNS)), _rf_w3a, True),
    ("W3b", re.compile(r"(?<![가-힣])(?P<n>[가-힣]{2,})(?:이|가)\s+가능(?P<e>%s)(?![가-힣])" % _alt(RF_GANEUNG)), _rf_ganeung, True),
    ("W3e", re.compile(r"(?<![가-힣])(?:위치|존재)(?:하지\s+(?P<ng>않는|않은)|한|하는)\s+관계로,?(?![가-힣])"), _rf_iss_gwangye, False),
    ("W3e", re.compile(r"(?<![가-힣])(?P<v>[가-힣]+)\s+관계로,?(?![가-힣])"), _rf_gwangye, False),
    ("W3f", re.compile(r"(?<![가-힣])(?P<n>[가-힣]+)의\s+경우(?P<t>에는|에도|에|엔|는)?,?(?![가-힣])"), _rf_gyeongu, False),
    ("W2b", re.compile(r"(?<![가-힣])(?P<x>[가-힣0-9]+)에\s+(?:대한|관한)(?=\s+(?P<y>[가-힣]+?)(?:을|를|이|가|은|는|의|에서|에게|에|으로|로|과|와|도|만)?(?![가-힣]))"),
     _rf_daehan, True),
    ("S6b", re.compile(r"(?<![가-힣])(?P<n>[가-힣]+)(?:은|는)\s+(?P<x>[^.?!\n]+?다)는\s+(?:것을|점을|사실을)\s*(?P<vb>말해\s*준다|보여\s*준다|나타낸다|말해\s*줍니다|보여\s*줍니다|나타냅니다)(?![가-힣])"),
     _rf_s6b, True),
)


def rule_fix_canon(src_text, out_text, protected_text=()):
    """원문의 번역투 자리 가운데 수정문이 그 규칙의 표준 고침으로 바꾼 곳만 표준 고침으로 바꾼 원문과, 그렇게 바뀐 원문 문장 번호들.
    인용(따옴표 안)과 보호 문구는 건드리지 않는다. 문장 수가 달라지면 아무것도 바꾸지 않는다."""
    src = nfc(src_text or ""); ns = lambda x: re.sub(r"[\s,]+", "", nfc(x or ""))
    out_ns = ns(out_text)
    if not src.strip() or not out_ns:
        return src_text, set()
    guard = [m.span() for m in re.finditer(r"“[^”]*”|\"[^\"\n]*\"|‘[^’]*’|「[^」]*」|『[^』]*』", src)]
    for p in protected_text or ():
        p = nfc(p or "").strip()
        if p:
            guard += [m.span() for m in re.finditer(re.escape(p), src)]
    canon, changed = src, False
    for _round in range(4):  # 고친 글에서 다시 찾는다(겹친 규칙: '임대 주택에 대한 수요가 높다는 것을 보여 준다')
        edits = []
        for _rid, rx, fn, self_anchor in RULE_FIXES:
            for m in rx.finditer(canon):
                s, e = m.span()
                if s == e or any(a < e and s < b for a, b in guard) or any(a < e and s < b for a, b, _c in edits):
                    continue
                left = canon[:s]; lt = re.split(r"\s+", left.rstrip())[-1] if left.strip() and left[-1:].isspace() else ""
                rest = re.match(r"\S*", canon[e:]).group(0); nxt = re.match(r"\S*\s+(\S+)", canon[e:])
                nt = nxt.group(1) if nxt else ""
                for c in fn(m, out_text):
                    c, probe = c if isinstance(c, tuple) else (c, c)
                    cs_ = ns(probe)
                    if not cs_ or cs_ not in out_ns:
                        continue
                    if (self_anchor and len(cs_) >= 4) or (lt and ns(lt + probe) in out_ns) or (nt and ns(probe + rest + " " + nt) in out_ns):
                        edits.append((s, e, c)); break
        if not edits:
            break
        for s, e, c in sorted(edits, reverse=True):
            canon = canon[:s] + c + canon[e:]
            shift = len(c) - (e - s)
            guard = [(a + (shift if a >= e else 0), b + (shift if b >= e else 0)) for a, b in guard]
        changed = True
    if not changed:
        return src_text, set()
    a, b = list(Doc(src_text).flat), list(Doc(canon).flat)
    if len(a) != len(b) or [p for p, _x in a] != [q for q, _y in b]:
        return src_text, set()
    return canon, {k for k, ((_p, x), (_q, y)) in enumerate(zip(a, b)) if nfc(x) != nfc(y)}


def _trivial_residual(a, b):
    """두 블록의 차이가 띄어쓰기·문장부호·기능어(GATE_FUNC)·같은 명사의 조사(작업은 → 작업에는, 일은 → 일에는)뿐인가."""
    import consistency as cs
    import difflib
    ta = [t for t in re.findall(r"[가-힣A-Za-z0-9]+", nfc(a or "")) if t not in GATE_FUNC]
    tb = [t for t in re.findall(r"[가-힣A-Za-z0-9]+", nfc(b or "")) if t not in GATE_FUNC]
    if "".join(ta) == "".join(tb):
        return True

    def same(x, y):
        if x == y:
            return True
        (sx, px), (sy, py) = cs.stem(x), cs.stem(y)
        if sx == sy and len(sx) >= 2 and (px or py):
            if not (px and py):  # 한쪽에만 조사(간판 → 간판을, 지난주 → 지난주에)
                pt = px or py
                if pt in NOUN_PARTICLES or pt in ("은", "는") or re.fullmatch(COMPOUND_PARTICLE.rstrip("$"), pt):
                    return True
            elif not _is_pred(x) and not _is_pred(y):
                return True
        for u, w in ((x, y), (y, x)):  # 한 음절 명사: 주제·주어 ↔ 장소 주제(이 일은 → 이 일에는)
            if re.fullmatch(r"[가-힣](?:은|는|이|가)", u) and re.fullmatch(r"[가-힣](?:에는|에서는|에게는)", w) and u[0] == w[0]:
                return True
        return False
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ta, b=tb, autojunk=False).get_opcodes():
        if tag == "equal" or "".join(ta[i1:i2]) == "".join(tb[j1:j2]):
            continue
        if tag != "replace" or i2 - i1 != j2 - j1 or not all(same(x, y) for x, y in zip(ta[i1:i2], tb[j1:j2])):
            return False
    return True


def risk_items(src_text, out_text, genre="일반", terms=(), dialogue="auto", protected_text=()):
    """바뀐 블록마다 위험 신호를 모은다. 위험(CRITICAL)이 하나라도 있으면 결정이 필요한 항목이 된다.
    번호는 블록의 원문·수정문으로 만들어, 다시 돌려도 같은 수정은 같은 번호다.
    스킬 규칙이 권하는 표준 고침(규칙 고침)만 바뀐 블록은 항목에서 뺀다(rule_fix_canon·_trivial_residual)."""
    items = _risk_items_core(src_text, out_text, genre, terms, dialogue, protected_text)
    if not items:
        return items
    canon, fixed = rule_fix_canon(src_text, out_text, protected_text)
    if not fixed:
        return items
    diag = []
    _risk_items_core(canon, out_text, genre, terms, dialogue, protected_text, _diag=diag)
    keep = []
    for it in items:
        i1, i2, j1, j2 = it["span"]
        if fixed & set(range(i1, i2)):
            si, oi = set(range(i1, i2)), set(range(j1, j2))
            over = [d for d in diag if (set(range(d["span"][0], d["span"][1])) & si) or (set(range(d["span"][2], d["span"][3])) & oi)]
            if all(d["clean"] for d in over):
                continue  # 표준 고침과 띄어쓰기·조사·기능어만 바뀌었다
        keep.append(it)
    return keep


def _cancel_units(lost, gained):
    """단위 없이 쓴 수에 단위를 밝힌 것(서울 10, 경기 5 → 서울 10곳, 경기 5곳)과 그 반대는 같은 값으로 본다: 사라진 쪽과 생긴 쪽에서 하나씩 지운다."""
    bare = lambda k: k[0] == "숫자" and re.fullmatch(r"[+\-]?[₩$€£¥]?\d+(?:\.\d+)?", k[1])
    date_like = lambda m: bool(re.match(r"[+\-]?[₩$€£¥]?\d+(?:\.\d+)?(?:년|월|일|시|분|초)$", m[1]))  # 날짜·시각보다 수량 단위(곳·명·개)와 먼저 짝짓는다
    for x, y in ((lost, gained), (gained, lost)):
        for k in [k for k in x if bare(k)]:
            for m in sorted(y, key=date_like):
                if x[k] <= 0:
                    break
                if m[0] == "숫자" and y[m] > 0 and m[1] != k[1] and m[1].startswith(k[1]) and not re.match(r"[\d.,]", m[1][len(k[1]):]):
                    n = min(x[k], y[m])
                    x[k] -= n
                    y[m] -= n
    for c in (lost, gained):
        for k in [k for k, v in c.items() if v <= 0]:
            del c[k]


def _role_swap(desc, a="", b=""):
    """대조 메모에서 두 이름이 자리를 맞바꿨는가: 역할: 'X' 주체 → 대상 과 역할: 'Y' 대상 → 주체(철수가 영희를 → 영희가 철수를),
    또는 한 자리에 다른 이름이 들어오고 원래 이름이 주어·주제 자리로 갔다(준호는 세린을 → 세린은 준호를). 문장 짜임을 바꾼 것(저수조 청소를 → 저수조를 청소하고)은 아니다."""
    moves = re.findall(r"역할: '([^']+)' (\S+) → (\S+?)(?=;|$|\s)", desc)
    for x, r1, r2 in moves:
        if any(y != x and s1 == r2 and s2 == r1 for y, s1, s2 in moves):
            return True
    subj = lambda n, t: bool(re.search(r"(?<![가-힣])%s(?:은|는|이|가|께서)(?![가-힣])" % re.escape(n), nfc(t or "")))
    for _slot, x, y in re.findall(r"역할 자리: (\S+) '([^']+)' → '([^']+)'", desc):
        if subj(y, a) and subj(x, b) and not subj(x, a) and not subj(y, b):
            return True
    return False


KO_COUNT_WORD = {2: "두", 3: "세", 4: "네", 5: "다섯", 6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉"}
ORD_ANY_RE = re.compile(r"(?<![가-힣])(?:(첫|둘|셋|넷|다섯|여섯|일곱|여덟|아홉)째|(첫|두|세|네|다섯|여섯|일곱|여덟|아홉)\s?번\s?째)")
ORD_VAL = {"첫": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}
LIST_MARK_RE = re.compile(r"^\s*(?:[-•·*▪◦]|\d{1,2}[.)]|[①-⑨]|[✅✔☑]️?)\s*\S")


def _list_len(seg):
    """한 구간에 쉼표·가운뎃점·'그리고'로 나열한 항목 수(굵은 표시는 뗀다). 체크 표시(✅)로 나열한 것도 센다."""
    seg = re.sub(r"\*\*|__", "", nfc(seg or ""))
    marks = len(re.findall(r"[✅✔☑•]", seg))
    if marks >= 2:
        return marks
    parts = [x for x in re.split(r"\s*(?:,|，|·|、|;)\s*(?:그리고\s+|및\s+)?|\s+(?:그리고|및)\s+", seg) if re.search(r"[가-힣A-Za-z0-9]", x)]
    return len(parts) if len(parts) >= 2 else 0


def _count_announced(text, n):
    """가짓수 예고: 'N가지'가 든 문장 앞뒤에 그 수만큼 항목을 나열했는가('A, B, 그리고 C라는 세 가지', 'A, B, C. 이 세 가지', '세 가지를 배웠다. 첫째, … 둘째, … 셋째, …').
    항목이 글에 그대로 있으면 가짓수 말을 지워도 사실은 사라지지 않는다(AI 문체 정리에서 흔하다): 사실 종류가 아니라 확인 목록이다."""
    pat = re.compile(r"(?<![가-힣0-9])(?:%d|%s)\s?가지" % (n, KO_COUNT_WORD.get(n, "")))
    fl = [x for _p, x in Doc(nfc(text or "")).flat]
    for i, s in enumerate(fl):
        m = pat.search(s)
        if not m:
            continue
        if n in (_list_len(s[:m.start()]), _list_len(re.sub(r"^\s*(?:를|을|은|는|이|가|의|로|에)?\s*[:：]?", "", s[m.end():])), _list_len(fl[i - 1] if i else "")):
            return True
        ords, bullets = set(), 0
        for x in fl[i + 1:i + 2 + 2 * n]:
            ords |= {ORD_VAL[a or b] for a, b in ORD_ANY_RE.findall(x[:12])}
            bullets += bool(LIST_MARK_RE.match(x))
        if len(ords) >= n or bullets >= n:
            return True
    for ln_i, ln in enumerate(nfc(text or "").split("\n")):  # 줄 목록: 'N가지' 줄 뒤에 목록 줄이 N개
        if pat.search(ln):
            rest = [x for x in nfc(text).split("\n")[ln_i + 1:] if x.strip()][:n]
            if len(rest) >= n and all(LIST_MARK_RE.match(x) or ORD_ANY_RE.match(x.strip().lstrip("*#").strip()) for x in rest):
                return True
    return False


def _ordinal_enum(text, n):
    """목록 서수: '첫 번째 …', '두 번째 …'(또는 첫째·둘째)가 줄 첫머리(굵은 표시·제목 표시 뒤 포함)에 1부터 n까지 모두 있는가. 소제목·목록 번호라 지워도 사실이 아니다."""
    vals = set()
    for ln in nfc(text or "").split("\n"):
        head = re.sub(r"^[\s#>*\-•·▪◦]+", "", ln)[:12]
        m = ORD_ANY_RE.match(head)
        if m:
            vals.add(ORD_VAL[m.group(1) or m.group(2)])
    return n >= 2 and all(v in vals for v in range(1, n + 1))


def _fact_changes(src_r, out_r, fctx):
    """글자로 확인되는 사실 변화 가운데 보호 항목과 이름 목록: [(종류, 근거)].
    숫자·단위·날짜·URL·코드·경로·숫자가 든 식별자·인용(소설·웹소설 대사는 제외)이 수정문 어디에도 없게 사라졌거나 원문 어디에도 없던 값이 생긴 것.
    같은 값이 다른 문장으로 옮겨 가거나 되풀이만 줄인 것, 단위를 밝힌 것(10 → 10곳), 목록 번호, '한'(한 번에·한 줄)은 사실 변화가 아니다.
    로마자로만 된 이름(KTX·TV)은 다른 로마자 이름으로 바뀐 것만 사실 변화다(한글 표기로 바꾼 TV → 텔레비전은 확인 목록).
    --terms의 이름·용어는 수정문 어디에도 없게 사라졌거나, 그 자리에서 한 글자만 다른 말로 바뀌었으면(세린 → 세릭) 사실 변화다."""
    import consistency as cs
    q = fctx["quotes"]
    pa, pb = protected_items(src_r, quotes=q), protected_items(out_r, quotes=q)
    settle_weak(pa, pb)
    lost, gained = pa - pb, pb - pa
    _cancel_units(lost, gained)
    letters = lambda k: k[0] == "식별자" and not re.search(r"[\d_]", k[1])
    num_part = lambda k: re.match(r"[+\-]?[₩$€£¥]?\d+(?:\.\d+)?", k[1]) if k[0] == "숫자" else None

    def unit_moved(k, g_this, g_that):  # 같은 수의 단위만 밝히거나 뺀 것(문서 전체로 본다): 18곳 추가. 서울 10 → 서울 10곳
        m = num_part(k)
        if not m:
            return False
        n = m.group(0)
        if n == k[1]:
            return any(kk[0] == "숫자" and kk[1] != n and kk[1].startswith(n) and not re.match(r"[\d.,]", kk[1][len(n):]) and g_this.get(kk, 0) == 0
                       for kk in g_that)
        return g_that.get(("숫자", n), 0) > 0 and g_this.get(("숫자", n), 0) == 0
    squash = lambda x: re.sub(r"\s+", "", nfc(x))
    words_kept = lambda k, other: k[0] == "인용" and len(squash(k[1])) >= 2 and squash(k[1]) in squash(other)  # 인용한 말이 글자 그대로 남았다: 부호만 바뀌었다
    lost_f = [k for k in lost if not letters(k) and fctx["g_out"].get(k, 0) == 0 and not unit_moved(k, fctx["g_src"], fctx["g_out"]) and not words_kept(k, fctx["out"])]
    gained_f = [k for k in gained if not letters(k) and fctx["g_src"].get(k, 0) == 0 and not unit_moved(k, fctx["g_out"], fctx["g_src"]) and not words_kept(k, fctx["src"])]
    ll = [k for k in lost if letters(k) and fctx["g_out"].get(k, 0) == 0]
    lg = [k for k in gained if letters(k) and fctx["g_src"].get(k, 0) == 0]
    if ll and lg:
        lost_f += ll
        gained_f += lg
    res, soft = [], []
    for k in list(lost_f):  # 가짓수 예고('세 가지를 배웠다. 첫째, …')와 목록 서수('두 번째 깨달음')는 항목이 원문에 나열돼 있으면 사실이 아니라 틀이다
        m_ = re.fullmatch(r"([2-9])(가지|번째|째)", k[1]) if k[0] == "숫자" else None
        if not m_ or any(g[0] == "숫자" and re.fullmatch(r"\d+" + m_.group(2), g[1]) for g in gained_f):
            continue  # 세 가지 → 네 가지는 값이 바뀐 것이다
        if _count_announced(fctx["src"], int(m_.group(1))) if m_.group(2) == "가지" else _ordinal_enum(fctx["src"], int(m_.group(1))):
            lost_f.remove(k)
            soft.append(k)
    if soft:
        res.append(("고유 정보 삭제", "가짓수·목록 서수 %s가 사라졌다(나열한 항목은 원문에 있다: 항목이 모두 남았는지 확인)" % _fmt_items(Counter({k: lost[k] for k in soft}))))
    if lost_f or gained_f:
        res.append(("사실 변화", "보호 항목: %s → %s" % (_fmt_items(Counter({k: lost[k] for k in lost_f})) or "없음",
                                                  _fmt_items(Counter({k: gained[k] for k in gained_f})) or "없음")))
    for t in fctx["terms"]:
        if not t:
            continue
        near = lambda text: sorted({text[k:k + len(t)] for k in range(max(0, len(text) - len(t) + 1))
                                    if text[k:k + len(t)] != t and text[k:k + 1] == t[:1] and re.fullmatch(r"[가-힣A-Za-z0-9 ]+", text[k:k + len(t)])
                                    and _jamo_close(t, text[k:k + len(t)])})
        fresh = [u for u in near(out_r) if u not in fctx["src"]]
        if fresh and src_r.count(t) <= out_r.count(t):  # 이름과 한 글자 다른 말이 새로 생겼다(원문 어디에도 없다): 동수 → 동숙
            res.append(("사실 변화", "이름·용어 '%s'와 한 글자 다른 '%s'가 새로 생겼다(--terms)" % (t, fresh[0])))
            continue
        if src_r.count(t) <= out_r.count(t):
            continue
        if t not in fctx["out"]:
            res.append(("사실 변화", "이름·용어 '%s'가 수정문 어디에도 없다(--terms)" % t))
            continue
        cands = {out_r[k:k + len(t)] for k in range(max(0, len(out_r) - len(t) + 1))}  # 자모 하나 차이(세린 → 세릭, 박 사장 → 박 사잦)
        twin = next((u for u in sorted(cands) if u != t and u[:1] == t[:1] and re.fullmatch(r"[가-힣A-Za-z0-9 ]+", u) and _jamo_close(t, u)), None)
        if twin:
            res.append(("사실 변화", "이름·용어 '%s'가 '%s'로 바뀌었다(--terms)" % (t, twin)))
    for ph0 in fctx.get("protected_text", ()):
        ph = nfc(ph0).strip().strip("“”\"「」『』‘’").strip()
        if not ph:
            continue
        gs, go = Counter(verbatim_units(fctx["src"], ph)), Counter(verbatim_units(fctx["out"], ph))
        if not (gs - go):
            continue
        ls, lo = Counter(verbatim_units(src_r, ph)), Counter(verbatim_units(out_r, ph))
        if ls - lo:
            res.append(("사실 변화", "보호 문구 '%s'가 글자 그대로 유지되지 않았다(--protected-text)" % snip(ph, 40)))
    return res


def _risk_items_core(src_text, out_text, genre="일반", terms=(), dialogue="auto", protected_text=(), _diag=None):
    """risk_items의 본체. _diag(목록)를 주면 블록마다 {'span', 'clean'}을 적는다(clean: 위험·대조 메모 없음 + 사소한 차이뿐).
    위험 수정 게이트: 블록은 문장 묶음(_gate_blocks)이고, 항목마다 사실 종류(fact)와 등급(grade: 사실|확인)을 적는다."""
    import hashlib
    r = compare(src_text, out_text, genre, terms, dialogue, protected_text)
    S_, O_, ss, os_, blocks = _indexed_blocks(src_text, out_text)
    norm = lambda x: re.sub(r"\s+", " ", nfc(x)).strip()
    bp_by = {tuple(b["span"]): b for b in r["blocks"] if b.get("span")}
    edge_revs = _edge_reversals(ss, os_)  # 대조가 문장을 어떻게 짝지었든 인과 방향을 본다
    notes = []
    for pn in r["pair_notes"]:
        m = re.match(r'[^"]*"([^"]*)"', pn)
        notes.append(((m.group(1).rstrip("…").strip() if m else ""), pn.split(" — ", 1)[-1]))
    quotes_on = r.get("quotes_protected", True)
    g_src, g_out = protected_items(S_.text, quotes=quotes_on), protected_items(O_.text, quotes=quotes_on)  # 문서 전체: 사실 변화는 문서 어디에도 없는 값만
    settle_weak(g_src, g_out)
    fctx = {"quotes": quotes_on, "g_src": g_src, "g_out": g_out,
            "terms": [nfc(t).strip() for t in (terms or ()) if t and nfc(t).strip()],
            "protected_text": [nfc(t).strip() for t in (protected_text or ()) if t and nfc(t).strip()],
            "out": O_.text, "src": S_.text}
    moved_cls = _boundary_doc_moved([x for _p, x in ss], [x for _p, x in os_])
    items = []
    for i1, i2, j1, j2 in blocks:
        src_b = " ".join(x for _p, x in ss[i1:i2]); out_b = " ".join(x for _p, x in os_[j1:j2])
        kinds, why, lost_words, boundary, need, noted = [], [], [], [], [], False
        f_src = ss[i2][1] if i2 < len(ss) else ""  # 블록 바로 뒤 문장('단순한 X가 아니다'의 뒤 말을 보는 데 쓴다)
        f_out = os_[j2][1] if j2 < len(os_) else ""
        bp = bp_by.get((i1, i2, j1, j2))  # 블록 대조와 같은 문장 번호로 짝짓는다(같은 문장이 두 번 나와도 섞이지 않는다)
        if bp:
            ns = " ".join(bp.get("notes", []))
            if "능동 → 피동" in ns or "피동 → 능동" in ns:
                kinds.append("행위자 변화"); why.append(next(x for x in bp["notes"] if "동" in x))
            if "말하는 동사" in ns:
                kinds.append("말하는 동사 변화"); why.append(next(x for x in bp["notes"] if "말하는 동사" in x))
            out_toks, src_toks = re.findall(r"[가-힣A-Za-z0-9]+", nfc(out_b)), re.findall(r"[가-힣A-Za-z0-9]+", nfc(src_b))
            def _verb_kept(w):  # 동사 활용만 바뀜(살았잖아 → 산, 줄이기 → 줄이려고): 같은 동사의 다른 꼴이 수정문에 있다
                if (_is_pred(w) or w.endswith("기")) and len(w) >= 3 and any(t.startswith(w[:2]) for t in out_toks if t):
                    return True
                if _memo_verb_kept(w, out_toks):  # 메모체(회귀함·떨림·나감·참음)를 문장으로 풀었다(회귀했다·떨렸다·나갔다·참았다)
                    return True
                return _is_pred(w) and any(len(t) <= 3 and _choseong_jungseong(t[0]) == _choseong_jungseong(w[0]) and not _nounish(t)
                                           for t in out_toks if t)  # 명사(유나가·환자가)는 같은 동사의 활용이 아니다
            lost = [w for w in bp["lost"] if not _similar_any(w, out_toks) and w not in GATE_FUNC and cs_stem0(w) not in GATE_FUNC and not _verb_kept(w)
                    and not _style_safe_lost(w, src_b, f_src) and not _paraphrase_kept(w, out_b)]
            src_bases = {_nbase(t) for t in src_toks}
            new = [w for w in bp["new"] if _nounish(w) and not _similar_any(w, src_toks) and _nbase(w) not in src_bases]  # 앱을 → 앱으로, 일은 → 일에는은 새 말이 아니다
            far = [p_ for p_ in bp.get("swapped", []) if _swap_far(p_)]  # 한 음절만 다른데 자모가 둘 넘게 다른 말(동문 → 서문, 대한 → 의한, 상반기 → 하반기)
            if far:
                kinds.append("낱말 바꿈"); why.append("바뀐 낱말: " + ", ".join(far[:5]))
            if lost and new:  # 말이 사라지고 다른 말이 생겼다: 뜻이 같은지 문맥으로 판정한다(눈으로 → 눈빛뿐이다, 절단 → 날)
                kinds.append("낱말 바꿈"); why.append("사라진 말: %s / 새 말: %s" % (", ".join(lost[:5]), ", ".join(new[:5])))
            elif lost:
                kinds.append("고유 정보 삭제"); why.append("사라진 말: " + ", ".join(lost[:5])); lost_words = list(lost)
            elif new:  # 원문에 대응하는 말이 없는 순수한 추가
                kinds.append("새 정보 추가"); why.append("새 말: " + ", ".join(new[:5]))
        for snip_, desc in notes:
            if snip_ and any(norm(x).startswith(snip_[:25]) for _p, x in ss[i1:i2]):
                noted = True
                if "부정" in desc and _double_neg_obligation(src_b, out_b):
                    continue  # 하지 않으면 안 된다 → 해야 한다
                if "조사:" in desc and re.search(r"뿐(?:만)?\s*아니라", src_b) and re.search(r"도 → (?:을|를|이|가)", desc):
                    continue  # 기획뿐만 아니라 디자인도 → 기획과 디자인을
                if _role_swap(desc, src_b, out_b):
                    kinds.append("관계 뒤집힘"); why.append(desc)
                elif any(k in desc for k in ("역할", "관형 기준", "조사:", "숫자 짝")):
                    kinds.append("화자·대상·관계 변화"); why.append(desc)
                if "인과" in desc:
                    kinds.append("인과 변화"); why.append(desc)
        if i2 > i1 and j2 > j1:  # 의미 경계: 시간·상태·범위(사실), 반복·강조·한정(확인), 부정(사실: 같은 사건이 한쪽에서만 부정됨)
            for bc in _boundary_changes([x for _p, x in ss[i1:i2]], [x for _p, x in os_[j1:j2]]):
                if bc["cls"] in moved_cls:
                    continue  # 문서 전체로는 같은 수: 문장을 옮기거나 합치며 자리만 바뀌었다
                if bc["kind"] in BOUNDARY_SOFT and _rest_dice(src_b, out_b, bc["rx"]) < 0.6:
                    bc = dict(bc, kind=BOUNDARY_SOFT[bc["kind"]])  # 문장을 다시 썼다: 글자로 가를 수 없어 확인 목록
                elif bc["cls"] == "오직" and bc["dir"] == "추가" and _cleft_count_only(src_b, out_b):
                    bc = dict(bc, kind="범위 표현 변화")  # 분열문의 초점이 수(빠진 건 그 한 번 → 그 한 번뿐): 수가 같아 뜻이 같은지 읽고 정한다
                elif bc["kind"] == "범위 변화" and i2 - i1 == 1 and _heading_like(src_b):
                    bc = dict(bc, kind="범위 표현 변화")  # 제목 줄의 '모든 순간을 바꿀' 같은 말은 대개 구호다: 사실 범위인지 읽고 정한다
                elif bc["kind"] in BOUNDARY_SOFT and _marker_sent_gone(bc, [x for _p, x in ss[i1:i2]], [x for _p, x in os_[j1:j2]]):
                    bc = dict(bc, kind=BOUNDARY_SOFT[bc["kind"]])  # 표지가 든 문장을 통째로 지웠거나 새로 썼다: 낱말 하나를 뺀 것이 아니라 고유 정보 삭제·새 정보처럼 읽고 정한다
                kinds.append(bc["kind"]); boundary.append(bc)
                why.append("%s: '%s' %s — %s" % (bc["kind"].replace(" 변화", ""), bc["marker"], bc["dir"], BOUNDARY_WHY[bc["kind"]]))
            nf = None if _double_neg_obligation(src_b, out_b) else (_neg_flip(src_b, out_b, f_src, f_out, S_.text, O_.text) or _merely_flip(src_b, out_b))
            if nf:
                kinds.append("부정 뒤집힘"); why.append(nf)
            else:
                sa_, oa_ = _attach_neg(src_b, out_b), _attach_neg(out_b, src_b)
                ta, tb = _neg_types(_respace(sa_, oa_), f_src), _neg_types(oa_, f_out)
                if sum(ta.values()) == sum(tb.values()) and ta["못"] != tb["못"]:
                    kinds.append("부정 표현 변화"); why.append("부정 표현: '못' %d → %d (할 수 없음과 하지 않음이 바뀌었는지)" % (ta["못"], tb["못"]))
        if i2 > i1:
            mh = _merely_hedged_lost(src_b, out_b, f_src)
            if mh:
                if "고유 정보 삭제" not in kinds:
                    kinds.append("고유 정보 삭제")
                why.append(mh[1]); need.append((mh[0], r"아니|아닙|아냐|않"))
        for desc_, si_, oi_ in edge_revs:
            if (si_ & set(range(i1, i2))) or (oi_ & set(range(j1, j2))):
                kinds.append("인과 변화"); why.append(desc_)
        if i2 - i1 == 1 and j2 - j1 == 1:  # 인과 역전: A해서 B → B해서 A(두 사건은 그대로, 원인과 결과가 자리를 바꿈) — 한 문장 안이라 글자로 확인된다(사실 종류)
            rv = _cause_reversed(src_b, out_b)
            if rv:
                kinds.append("인과 뒤집힘"); why.append(rv)
        if i2 - i1 >= 2 and j2 - j1 == 1 and len(LINK_ENDING.findall(out_b)) > len(LINK_ENDING.findall(src_b)):
            kinds.append("사건 연결 추가"); why.append("따로이던 문장을 이음: " + ", ".join(sorted(set(m.group(0)[1:] for m in LINK_ENDING.finditer(out_b)))))
        for k_, w_ in _fact_changes(S_.raw(i1, i2), O_.raw(j1, j2), fctx):  # 보호 항목·이름 목록(원문 구간 그대로: 목록 번호를 알아본다)
            kinds.append(k_); why.append(w_)
        crit = sorted(set(k for k in kinds if k in CRITICAL_KINDS), key=CRITICAL_KINDS.index)
        if _diag is not None:
            _diag.append({"span": (i1, i2, j1, j2), "clean": not crit and not noted and _trivial_residual(src_b, out_b)})
        if not crit:
            continue
        hid = "C" + hashlib.sha1((norm(src_b) + "\u0000" + norm(out_b)).encode("utf-8")).hexdigest()[:6]
        where = ("원문 %s → 수정문 %s" % (S_.loc(ss[i1][0]) if i2 > i1 else "-", O_.loc(os_[j1][0]) if j2 > j1 else "-"))
        fact = [k for k in crit if k in FACT_KINDS]
        items.append({"id": hid, "where": where, "kinds": crit, "fact": fact, "grade": "사실" if fact else "확인",
                      "why": list(dict.fromkeys(why)), "src": src_b, "out": out_b,
                      "span": (i1, i2, j1, j2), "lost": lost_words, "boundary": boundary, "need": need})
    return items


def _splice(src, base, spans):
    """base(수정문)의 문장 묶음을 원문 구간으로 갈아 끼운다(뒤에서부터). spans: [(i1, i2, j1, j2)] — Doc(src)·Doc(base)의 flat 문장 번호.
    되돌리지 않은 곳의 줄바꿈·빈 줄·목록 모양은 수정문 그대로, 되돌린 곳은 원문 구간 그대로 남는다."""
    S_, B_ = Doc(src), Doc(base)
    so, bo, st, bt = S_.offsets(), B_.offsets(), S_.text, B_.text
    edits = []
    for i1, i2, j1, j2 in spans:
        repl = st[so[i1][0]:so[i2 - 1][1]] if i2 > i1 else ""
        if j2 > j1:
            a, b = bo[j1][0], bo[j2 - 1][1]
            if not repl:  # 수정문에만 있는 문장을 뺀다: 앞뒤 공백 가운데 작은 쪽(줄바꿈이 적은 쪽)을 함께 지워 문단 나눔을 지킨다
                pre = bt[bo[j1 - 1][1]:a] if j1 > 0 else None
                post = bt[b:bo[j2][0]] if j2 < len(bo) else None
                if pre is not None and (post is None or pre.count("\n") <= post.count("\n")):
                    a = bo[j1 - 1][1]
                else:
                    b = bo[j2][0] if j2 < len(bo) else len(bt)
            edits.append((a, b, repl))
        elif repl:  # 수정문에서 지운 원문 문장을 제자리에 되살린다: 원문에서 같은 문단이던 쪽 문장에 붙인다
            before = st[so[i1 - 1][1]:so[i1][0]] if i1 > 0 else ""
            after = st[so[i2 - 1][1]:so[i2][0]] if i2 < len(so) else ""
            if j1 > 0 and not ("\n" in before and after and "\n" not in after and j1 < len(bo)):
                edits.append((bo[j1 - 1][1], bo[j1 - 1][1], (before or " ") + repl))  # 앞 문장 뒤에(원문의 앞 공백·줄바꿈과 함께)
            else:
                pos = bo[j1][0] if j1 < len(bo) else len(bt)
                edits.append((pos, pos, repl + (after or ("\n" if j1 >= len(bo) else " "))))  # 뒤 문장 앞에
    out = bt
    for a, b, rep in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:a] + rep + out[b:]
    return out


def _coarse_spans(src, base, spans):
    """문장 묶음들을 품은 difflib의 바뀐 구간 전체(문장 묶음 되돌림이 내용을 빠뜨리거나 겹칠 때 쓰는 넓은 되돌림)."""
    import difflib
    S_, B_ = Doc(src), Doc(base)
    norm = lambda x: re.sub(r"\s+", " ", nfc(x)).strip()
    sm = difflib.SequenceMatcher(a=[norm(x) for _p, x in S_.flat], b=[norm(x) for _p, x in B_.flat], autojunk=False)
    res = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal" and any(i1 <= a1 and a2 <= i2 and j1 <= b1 and b2 <= j2 for a1, a2, b1, b2 in spans):
            res.append((i1, i2, j1, j2))
    return res


def _gate_check(src, out, final):
    """되돌린 결과 점검(문장 단위). 돌려주는 것: (빠진 원문 문장 번호들, 겹친 최종본 문장 번호들).
    빠짐: 수정문이 살려 둔 원문 문장의 내용(글자 쌍의 60% 넘게, 한 문장이나 이어진 두 문장 안)이 최종본에 없다.
    겹침: 되살린 원문 문장의 고친 짝(그 원문 문장과 가장 닮은 수정문 문장, Dice 0.5 이상)이 최종본에서 세 문장 안에 함께 남았다.
    (제목·요약처럼 멀리 떨어져 같은 내용을 되풀이하는 문장은 겹침이 아니다.)"""
    S_, O_, F_ = [[x for _p, x in Doc(t).flat] for t in (src, out, final)]
    bs, bo, bf = [_bigrams(x) for x in S_], [_bigrams(x) for x in O_], [_bigrams(x) for x in F_]
    wins = lambda bl: bl + [x | y for x, y in zip(bl, bl[1:])]
    wo, wf = wins(bo), wins(bf)
    cont = lambda a, b: len(a & b) / len(a) if a else 1.0
    dice = lambda a, b: 2 * len(a & b) / (len(a) + len(b)) if (a or b) else 1.0
    lost = [k for k, a in enumerate(bs) if len(a) >= 8 and max((cont(a, b) for b in wo), default=0) >= 0.6
            and max((cont(a, b) for b in wf), default=0) < 0.6]
    n_ = lambda x: re.sub(r"\s+", "", x)
    sset, oset = {n_(x) for x in S_}, {n_(x) for x in O_}
    s_at = {n_(x): k for k, x in enumerate(S_)}
    restored_at = {}  # 되살린 원문 문장 번호 → 최종본 위치
    for f, y in enumerate(F_):
        if n_(y) in sset and n_(y) not in oset:
            restored_at[s_at[n_(y)]] = f
    dup = []
    for f, (y, b) in enumerate(zip(F_, bf)):
        if not restored_at or n_(y) in sset or n_(y) not in oset:
            continue  # 수정문에서 그대로 남긴 고친 문장만 본다
        sims = [dice(b, a) for a in bs]
        k = max(range(len(S_)), key=lambda i: sims[i]) if S_ else None
        if k is not None and k in restored_at and sims[k] >= 0.5 and abs(restored_at[k] - f) <= 3:
            dup.append(f)
    return lost, dup


def _lost_or_doubled(src, out, final):
    """_gate_check를 한 목록으로(빠짐·겹침 문장 설명)."""
    lost, dup = _gate_check(src, out, final)
    S_, F_ = [x for _p, x in Doc(src).flat], [x for _p, x in Doc(final).flat]
    return ["빠짐: " + snip(S_[k], 24) for k in lost] + ["겹침: " + snip(F_[f], 24) for f in dup]


def _kept_fact(it, kept):
    """다시 대조에서 나온 사실 항목이 앞서 사용자 지시·출처로 유지한 항목과 같은 수정인가."""
    n = lambda x: re.sub(r"\s+", "", nfc(x or ""))
    return any(it["id"] == k["id"] or (n(k["src"]) in n(it["src"]) and n(k["out"]) in n(it["out"]) and set(it.get("fact", [])) <= set(k.get("fact", [])))
               for k in kept)


def gate_apply(src_text, out_text, decisions, **kw):
    """위험 항목마다 판정을 적용한 최종본과 판정 기록.
    - 사실 항목(숫자·이름·인용·부정·시점·범위)은 사용자 지시·출처(override)가 없으면 되돌린다. 확인 항목은 기본 유지, 판정이 REVERT면 되돌린다.
    - 되돌릴 것이 없으면 수정문을 한 글자도 바꾸지 않고 돌려준다(줄바꿈·끝 줄바꿈 그대로).
    - 되돌릴 때는 그 문장 묶음만 원문 구간으로 갈아 끼운다. 나머지 줄바꿈·빈 줄은 수정문 그대로다.
    - 되돌린 뒤 다시 대조해 남은 사실 변화를 되돌린다(최대 3번). 문장이 빠지거나 겹치면 바뀐 구간째 되돌린다."""
    src, out = nfc(src_text or ""), nfc(out_text or "")
    items = risk_items(src, out, **kw)
    log, spans = [], []
    for it in items:
        d = decisions.get(it["id"], {}) if isinstance(decisions, dict) else {}
        final, note = gate_decide(d if isinstance(d, dict) else {}, it["kinds"], {"orig": src, "final": out, "lost": it.get("lost"), "items": [it]})
        log.append(dict(it, decision=final, note=note))
        if final == "REVERT":
            spans.append(it["span"])
    if not spans:
        return out_text, log
    kept = [x for x in log if x["decision"] == "KEEP" and x.get("fact")]

    def restore_lost(cur, extra):  # 되돌린 묶음 때문에 옮겨 간 원문 내용이 빠지거나 고친 짝이 겹쳐 남으면, 그 문장이 든 묶음도 되돌린다
        for _round in range(3):
            lost, dup = _gate_check(src, out, cur)
            if not lost and not dup:
                break
            S2, C2 = Doc(src), Doc(cur)
            sp = [g for g in _gate_blocks(S2.flat, C2.flat) if any(g[0] <= k < g[1] for k in lost) or any(g[2] <= f < g[3] for f in dup)]
            if not sp:
                break
            for i1, i2, j1, j2 in sp:
                extra.append({"id": "L%d" % (len(extra) + 1), "where": "원문 %s" % (S2.loc(S2.flat[i1][0]) if i2 > i1 else "-"), "kinds": ["함께 되돌림"],
                              "fact": [], "grade": "보존", "why": ["되돌린 문장의 원문 내용이 이 문장으로 옮겨 가 있었다(빠짐·겹침 방지)"],
                              "src": " ".join(x for _p, x in S2.flat[i1:i2]), "out": " ".join(x for _p, x in C2.flat[j1:j2]),
                              "span": (i1, i2, j1, j2), "lost": [], "boundary": [], "need": [],
                              "note": "함께 되돌림: 되돌린 문장의 내용이 이 문장으로 옮겨 가 있어 원문이 빠지지 않게 되살림"})
            cur = _splice(src, cur, sp)
        return cur

    def run(wide):
        extra = []
        cur = restore_lost(_splice(src, out, _coarse_spans(src, out, spans) if wide else spans), extra)
        for _round in range(3):
            again = [it for it in risk_items(src, cur, **kw) if it.get("fact") and not _kept_fact(it, kept)]
            if not again:
                break
            sp = [it["span"] for it in again]
            extra += [dict(it, note="다시 대조: 되돌린 뒤에도 남은 사실 변화(%s)라 되돌림" % "·".join(it["fact"])) for it in again]
            cur = restore_lost(_splice(src, cur, _coarse_spans(src, cur, sp) if wide else sp), extra)
        return cur, extra
    cur, extra = run(False)
    bad = _lost_or_doubled(src, out, cur)
    if bad:
        cur, extra = run(True)
        for x in log:
            if x["decision"] == "REVERT":
                x["note"] += " · 문장 묶음만 되돌리면 내용이 빠지거나 겹쳐(%s) 바뀐 구간째 되돌림" % ", ".join(bad[:3])
    log += [dict(it, decision="REVERT") for it in extra]
    return cur, log


def gate_signals(before, after, prev_text="", next_text="", genre="일반", dialogue="auto", terms=(), protected_text=()):
    """엔진용: 수정 하나(문단 전후)를 앞뒤 문단과 함께 판정한다. 반환: (위험 종류, 근거)."""
    clean = lambda x: re.sub(r"⟦[^⟧]*⟧", " ", x or "")
    ctx = lambda mid: "\n\n".join(x for x in (clean(prev_text), clean(mid), clean(next_text)) if x.strip())
    items = risk_items(ctx(before), ctx(after), genre=genre or "일반", terms=terms or (), dialogue=dialogue or "auto", protected_text=protected_text or ())
    kinds = sorted({k for it in items for k in it["kinds"]}, key=CRITICAL_KINDS.index)
    gate_signals.last_lost = [w for it in items for w in it.get("lost", [])]  # 엔진이 근거 문장 확인에 쓴다
    gate_signals.last_items = items
    return kinds, [w for it in items for w in it["why"]]


PARTICLE_TAIL = r"(?:으로|로|에서|에게|께서|을|를|이|가|은|는|의|와|과|도|만|에)$"
COMPOUND_PARTICLE = r"(?:에서는|에게는|으로는|에서도|에게도|으로도|에서만|에는|에도|에만|로는|로도|과는|와는)$"


def _nbase(t):
    """조사를 뗀 명사 밑말: 겹친 조사(에는·에서는·으로는 등)는 통째로, 아니면 조사 하나를 뗀다(일에는 → 일, 앱으로 → 앱)."""
    b = re.sub(COMPOUND_PARTICLE, "", t)
    return (b if b != t else re.sub(PARTICLE_TAIL, "", t)) or t


BOUNDARY_CLASSES = (
    # (부류, 위험 종류, 표지, 같은 뜻으로 남는 말, 띄어쓰기를 무시하고 셀 표지, 셀 때 뺄 말)
    ("이미", "시간·상태 변화", r"(?<![가-힣])(?:이미|벌써)(?:부터)?(?![가-힣])", "", ("이미", "벌써"), ("이미지",)),
    ("아직", "시간·상태 변화", r"(?<![가-힣])(?:아직|여전히)(?:도|은|까지|껏)?(?![가-힣])", r"(?<![가-힣])(?:여태|지금껏)", ("아직", "여전히"), ()),
    ("모두", "범위 변화", r"(?<![가-힣])(?:모두|전부)(?:가|를|의|는|에게|에|들|다|임|이다|였다|이었다|입니다|였습니다|이었습니다|이에요|예요|이야|야|인데|이지만|이라|라)?(?![가-힣])|(?<![가-힣])모든(?![가-힣])",
     r"(?<![가-힣])다(?![가-힣])|전원|빠짐없이|남김없이|하나하나|일일이|낱낱이", ("모두", "전부", "모든"), ("전부터",)),
    ("오직", "범위 변화", r"(?<![가-힣])(?:오직|오로지)(?![가-힣])|뿐(?!\s*만|\s*아니|더러)|(?<!\s수)(?<!\s수\s)(?<!^수)(?<!^수\s)밖에\s*(?:없|안\s|못\s)",  # 민수뿐이었다·볼 뿐이다·민수밖에 없었다(배타 범위). '~ㄹ 수밖에 없다'(당연)는 범위가 아니다
     r"[가-힣]만(?:이|을|를|은|는|의|으로|에게|에)?(?![가-힣])|외에는|외엔|말고는|말곤|유일|따름",
     ("오직", "오로지", "뿐", "밖에없", "밖에안", "밖에못"), ("뿐만", "뿐아니", "뿐더러")),
    ("다시", "반복 정보 변화", r"(?<![가-힣])(?:또다시|다시금|다시는|다시(?!\s*말(?:해|하면|하자면))|재차|거듭|또)(?![가-힣])",
     r"한\s?번\s?더|또\s?한\s?번|되풀|되돌|돌아(?:가|오|왔|갔|온|간|와|서)|재(?:개|시작|발|방문|확인|검토|가동|등장|회|도전|연|현|입|설치|부팅|생산|결합|편|"
     r"구성|작성|논의|조사|출발|수정|점검|시도|제출|투입|배치|발송|요청|평가|협상)",
     ("다시", "또", "재차", "거듭"), ("다시말", "또는", "또한", "또래", "또렷", "거듭된", "거듭되", "거듭난")),
    ("무엇보다", "강조·한정 변화", r"(?<![가-힣])무엇보다(?:도)?(?![가-힣])|(?<![가-힣])(?:가장|제일)(?=\s)", r"특히|최고|최우선|우선", ("무엇보다", "가장", "제일"), ("가장자리", "가장행렬")),  # 제일·가장(으뜸) 삭제도 본다
    ("그저", "강조·한정 변화", r"(?<![가-힣])(?:그저|그냥)(?![가-힣])", r"단지|다만|뿐|기만|[가-힣]만(?![가-힣])", ("그저", "그냥"), ("그저께",)),
)
BOUNDARY_SOFT = {"시간·상태 변화": "시점 표현 변화", "범위 변화": "범위 표현 변화"}  # 문장을 크게 다시 쓴 곳의 시점·범위 말 변화: 글자로 가를 수 없어 확인 목록
BOUNDARY_WHY = {"시간·상태 변화": "이미 일어났다는·아직 아니라는 시점 정보", "범위 변화": "전부인지 일부인지·그것뿐인지의 범위 정보",
                "시점 표현 변화": "시점 정보(문장을 다시 써서 글자로 가를 수 없음)", "범위 표현 변화": "범위 정보(문장을 다시 써서 글자로 가를 수 없음)",
                "반복 정보 변화": "같은 일이 되풀이됐다는 정보", "강조·한정 변화": "무엇이 가장 앞서는지·그것뿐인지의 우선순위·한정 정보"}


def _jamo_close(a, b):
    """자모 편집 거리가 1 이하인가(여전이 ↔ 여전히, 모은 ↔ 모든: 오타 교정). 이미 ↔ 이제(자모 둘)는 아니다."""
    import consistency as cs
    x, y = cs.jamo(a), cs.jamo(b)
    if abs(len(x) - len(y)) > 1:
        return False
    prev = list(range(len(y) + 1))
    for i, cx in enumerate(x, 1):
        cur = [i] + [0] * len(y)
        for j, cy in enumerate(y, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cx != cy))
        prev = cur
    return prev[-1] <= 1


def _typo_counterpart(marker, sents):
    """두 음절 이상인 표지 낱말이 상대편에서 자모 하나만 다른 말로 나오는가(오타를 고치거나 오타가 생긴 자리)."""
    m = re.sub(PARTICLE_TAIL, "", re.sub(r"\s+", "", marker)) or marker
    if len(m) < 2:
        return False
    for tok in re.findall(r"[가-힣]+", nfc(" ".join(sents))):
        if tok != m and len(tok) >= len(m) and _jamo_close(m, tok[:len(m)]):
            return True
    return False


def _boundary_changes(src_sents, out_sents):
    """의미 경계 낱말(시간·상태·범위·반복·강조·한정)이 든 문장 수가 달라졌는가. 같은 부류 안의 바꿈(이미 → 벌써, 모두 → 전부),
    같은 뜻으로 남는 말(오직 민수만 → 민수만, 모두 다 → 다, 다시 재개 → 재개, 무엇보다도 가장 → 가장, 그저 웃기만 → 웃기만),
    띄어쓰기만 고친 것(다시한번 → 다시 한번)은 변화가 아니다."""
    res = []
    for name, kind, pat, equiv, raw, excl in BOUNDARY_CLASSES:
        rx, ex = re.compile(pat), (re.compile(equiv) if equiv else None)
        cnt = lambda sents, eq=False: sum(1 for x in sents if rx.search(nfc(x)) or (eq and ex and ex.search(nfc(x))))
        a, b = cnt(src_sents), cnt(out_sents)
        if a == b:
            continue
        def raw_count(sents):
            t = re.sub(r"(?:(?<=\s)|^)수\s?밖에", "", nfc(" ".join(sents)))  # '~ㄹ 수밖에 없다'(당연)는 범위 표지가 아니다
            t = re.sub(r"\s+", "", t)
            for x in excl:
                t = t.replace(x, "")
            return sum(t.count(m) for m in raw)
        if raw_count(src_sents) == raw_count(out_sents):
            continue  # 띄어쓰기만 바뀌었다
        if a > b:
            if cnt(out_sents, True) >= a:
                continue  # 같은 뜻의 말이 남았다
            direction, sent = "삭제", next(nfc(x) for x in src_sents if rx.search(nfc(x)))
            if _typo_counterpart(rx.search(sent).group(0), out_sents):
                continue  # 표지 낱말의 오타만 바뀌었다
        else:
            if cnt(src_sents, True) >= b:
                continue
            direction, sent = "추가", next(nfc(x) for x in out_sents if rx.search(nfc(x)))
            if _typo_counterpart(rx.search(sent).group(0), src_sents):
                continue  # 오타를 고쳤다(여전이 → 여전히, 모은 것 → 모든 것)
        res.append({"cls": name, "kind": kind, "dir": direction, "marker": rx.search(sent).group(0).strip(), "sent": sent, "rx": pat, "equiv": equiv})
    return res


CLEFT_COUNT = r"(?:(?<![가-힣])(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|하나|둘|셋|넷)|\d+)\s?(?:번|명|개|곳|가지|회|차례|건|권|장|마리|사람|군데)?"


def _marker_sent_gone(bc, src_sents, out_sents):
    """의미 경계 낱말이 든 문장이 통째로 빠졌거나(삭제) 새로 생겼다(추가): 그 문장의 나머지 말(글자 쌍)이 반대쪽 묶음에 절반도 없다.
    낱말만 빼거나 보탠 것(모두 왔다 → 왔다)은 나머지 말이 남아 있어 해당하지 않는다."""
    rx = re.compile(bc["rx"])
    side, other = (src_sents, out_sents) if bc["dir"] == "삭제" else (out_sents, src_sents)
    ob = _bigrams(re.sub(bc["rx"], " ", nfc(" ".join(other))))
    segs = [s for x in side for s in re.split(r"\*\*|\n|(?<=[.!?])\s", nfc(x))]  # 굵은 제목 줄이 다음 문장과 한 문장으로 묶였어도 표지가 든 조각만 본다
    hits = [s for s in segs if rx.search(s)]
    if not hits:
        return False
    for x in hits:
        b = _bigrams(re.sub(bc["rx"], " ", x))
        if len(b) < 4 or len(b & ob) / len(b) >= 0.5:
            return False
    return True


def _heading_like(text):
    """제목 줄처럼 보이는가: 한 줄, 60자 이하, 문장 끝말(-다·-요 등)이나 마침표로 끝나지 않는다(이모지·굵은 글씨 표시는 뺀다)."""
    s = re.sub(r"[*#_`>]+", "", nfc(text or "")).strip()
    s = re.sub(r"[^\w\s가-힣,·:()\-–—~%/.!?'\"“”‘’\[\]]", "", s).strip()
    if not s or "\n" in s or len(s) > 60 or s.endswith((".", "。")):
        return False
    core = re.sub(r"[!?…\s]+$", "", s)
    return bool(core) and not re.search(r"[다요죠까네군니]$", core)


def _cleft_count_only(src_b, out_b):
    """분열문의 초점이 수이고(1년 동안 빠진 건 그 한 번), 수정문이 같은 수에 '뿐'만 붙였다(그 한 번뿐이다): 수가 같아 범위가 바뀐 것이 아닐 수 있다.
    초점이 수가 아닌 것(남은 건 이것이다 → 이것뿐이다)은 범위 변화로 둔다."""
    m = re.search("(" + CLEFT_COUNT + r")\s?뿐", nfc(out_b or ""))
    if not (m and _cleft(src_b)):
        return False
    return re.sub(r"\s", "", m.group(1)) in re.sub(r"\s", "", nfc(src_b))


def _cleft(text):
    """분열문(빠진 건 그 한 번, 필요한 것은 용기다)이 있는가: 초점 자리가 이미 '그것뿐'이라는 뜻을 담아, '뿐'을 보태도 범위가 바뀐 것이 아닐 수 있다."""
    for m in re.finditer(r"([가-힣]+)\s?(?:건|것은|거는|게|것이)\s+\S", nfc(text or "")):
        last = m.group(1)[-1]
        code = ord(last) - 0xAC00
        if 0 <= code < 11172 and (code % 28 == 4 or last in "는던"):  # 받침 ㄴ(빠진·필요한·남은) 또는 -는·-던
            return True
    return False


def _boundary_doc_moved(src_sents, out_sents):
    """문서 전체에서 의미 경계 낱말이 든 문장 수가 같은 부류(문장을 옮기거나 합치면서 자리만 바뀜): 블록에서 보인 변화는 옮김이다."""
    res = set()
    for name, _kind, pat, _equiv, _raw, _excl in BOUNDARY_CLASSES:
        rx = re.compile(pat)
        if sum(1 for x in src_sents if rx.search(nfc(x))) == sum(1 for x in out_sents if rx.search(nfc(x))):
            res.add(name)
    return res


def _rest_dice(a, b, rx):
    """의미 경계 낱말을 뺀 나머지가 얼마나 같은가(글자 쌍 Dice): 낱말 하나만 빼거나 보탠 문장은 높고, 다시 쓴 문장은 낮다."""
    ta, tb = _bigrams(re.sub(rx, " ", nfc(a or ""))), _bigrams(re.sub(rx, " ", nfc(b or "")))
    return 2 * len(ta & tb) / (len(ta) + len(tb)) if (ta or tb) else 1.0


def boundary_problem(ev, ctx, kinds):
    """반복·강조·한정 정보를 지우거나 보탤 때의 근거 문장(context_evidence) 확인: 원문과 수정 뒤의 글에 함께 있는(고치지 않은) 문장이고,
    고친 문장이 아니며, 반복이면 같은 사건(서술어와 다른 내용어 하나 이상), 강조·한정이면 같은 뜻의 말과 같은 대상을 담아야 한다. 문제가 없으면 None."""
    nos = lambda x: re.sub(r"\s+", "", nfc(x or ""))
    e_raw = nfc(ev or "").strip().strip("“”\"'‘’「」『』"); e = nos(e_raw)
    if len(e) < 5:
        return "앞뒤에 같은 정보가 있는 문장(context_evidence)을 인용하지 않았다"
    if not ctx or e not in nos(ctx.get("orig")) or e not in nos(ctx.get("final")):
        return "인용한 근거 문장이 원문과 수정 뒤의 글에 함께 있지 않다(고친 문장이거나 지어낸 문장)"
    for it in ctx.get("items") or []:
        for b in it.get("boundary") or []:
            if b["kind"] not in kinds:
                continue
            for x in split_sentences(nfc(it.get("src", ""))):  # 고친 원문 문장 자체(또는 그 일부)는 근거가 아니다
                nx = nos(x)
                if nx and (e in nx or nx in e):
                    return "고친 문장 자체를 근거로 인용했다"
            keys = _content_keys(b["sent"])
            if b["cls"] == "다시":
                root = _pred_root(b["sent"])
                if not (root and _root_in(root, e_raw) and (keys - {root[:2]}) & _content_keys(e_raw)):
                    return "근거 문장에 같은 사건(%s)이 없다: 반복 정보는 앞뒤에 같은 사건이 있을 때만 지우거나 보탠다" % (root or "서술어")
            elif not (re.search(b["rx"] + ("|" + b["equiv"] if b["equiv"] else ""), e_raw) and keys & _content_keys(e_raw)):
                return "근거 문장에 같은 뜻의 말('%s' 또는 그에 준하는 말)과 같은 대상이 함께 있지 않다" % b["marker"]
    return None


def _content_keys(text):
    """블록의 내용어 열쇠(조사를 뗀 어간의 앞 두 음절, 한 음절이면 그 음절). 기능어·대명사·담화 표지는 뺀다."""
    keys = set()
    for tok in re.findall(r"[가-힣]+", nfc(text or "")):
        if tok in BLOCK_STOP or tok in GATE_FUNC or tok in DROPPABLE:
            continue
        st = re.sub(PARTICLE_TAIL, "", tok) or tok
        if st in BLOCK_STOP or st in GATE_FUNC:
            continue
        keys.add(st[:2])
    return keys


def _removed_required(src_b, out_b):
    """중복 삭제의 근거가 담아야 할 것: 블록에서 없어진 내용어 가운데 고친 문장에 남지 않은 것(눈으로 → 눈빛은 남은 것)."""
    out_nos = re.sub(r"\s+", "", nfc(out_b or ""))
    return {k for k in _content_keys(src_b) - _content_keys(out_b) if k not in out_nos}


def evidence_problem(ev, ctx):
    """고유 정보 삭제의 근거 문장 확인: 수정 뒤의 글(원문에서 남은 문장이나 고친 문장)에 실제로 있고, 사라진 말을 담고 있어야 한다.
    같은 정보가 어디에 남는지 가리키게 하는 것이다(지운 문장은 수정 뒤에 없어 걸리고, 관계없는 문장은 사라진 말이 없어 걸린다). 문제가 없으면 None."""
    import consistency as cs
    if not ctx:
        return "근거를 확인할 문맥이 없다"
    nos = lambda x: re.sub(r"\s+", "", nfc(x or ""))
    e = nos(ev).strip("“”\"'‘’「」『』")
    if len(e) < 5:
        return "같은 정보가 남은 문장(duplicate_evidence)을 인용하지 않았다"
    if e not in nos(ctx.get("final")):
        return "인용한 근거 문장이 수정 뒤의 글에 없다(지운 문장이거나 지어낸 문장)"
    items_ = [it for it in (ctx.get("items") or []) if "고유 정보 삭제" in it.get("kinds", [])]
    for it in items_:
        for x_, neg_ in it.get("need") or []:  # '단순한 X가 아니다'를 지웠으면 근거에 X와 그 부정이 함께 있어야 한다
            if not (re.search(re.escape(x_[:2]), nfc(ev or "")) and re.search(neg_, nfc(ev or ""))):
                return "인용한 근거 문장에 지운 확정 부정('%s가 아니다')이 없다" % x_
    if items_:  # 없어진 내용 가운데 고친 문장에 남지 않은 것이 근거 문장에 있어야 한다(사라진 낱말 그대로가 아니라)
        need = set().union(*[_removed_required(it.get("src"), it.get("out")) for it in items_])
        miss = sorted(k for k in need if k not in e)
        if miss:
            return "인용한 근거 문장에 지운 내용(%s)이 없다" % ", ".join(miss[:4])
        return None
    lost = [w for w in ctx.get("lost") or [] if w]
    keys = set()
    for w in lost:  # 사라진 말의 어간(조사를 뗀 꼴도): 눈으로 → 눈, 무너질 → 무너
        for st in (cs.stem(w)[0], re.sub(r"(?:으로|로|에서|에게|께서|을|를|이|가|은|는|의|와|과|도|만|에)$", "", w)):
            if st:
                keys.add(st[:2] if len(st) >= 2 else st)
    if lost and not any(k in e for k in keys):
        return "인용한 근거 문장에 사라진 말(%s)이 없다" % ", ".join(lost[:3])
    return None


def _pred_root(sent):
    """문장 끝 서술어의 어간(끄덕였다 → 끄덕, 닫았다 → 닫, 출발했다 → 출발)."""
    toks = re.findall(r"[가-힣]+", nfc(sent or ""))
    if not toks:
        return ""
    w = toks[-1]
    w = re.sub(r"(?:었습니다|았습니다|였습니다|했습니다|습니다|었다|았다|였다|했다|한다|는다|었어|았어|였어|했어|었지|았지|다|요|죠)$", "", w) or w
    return re.sub(r"(?:었|았|였|했)$", "", w) or w


def _root_in(root, ev):
    """서술어 어간이 근거 문장에 있는가: 두 음절 이상이면 앞 두 음절 그대로, 한 음절이면 첫소리·모음 계열이 같은 낱말(닫 ↔ 닫았다·닫으면)."""
    e = re.sub(r"\s+", "", nfc(ev or ""))
    if len(root) >= 2:
        return root[:2] in e
    k = _event_key(root)
    return any(_event_key(t) == k and ord(t[0]) - 0xAC00 >= 0 and (ord(t[0]) - 0xAC00) % 28 == (ord(root[0]) - 0xAC00) % 28
               for t in re.findall(r"[가-힣]+", nfc(ev or "")))


def connection_problem(ev, ctx):
    """사건 연결 추가의 근거 문장 확인: 원문에 있고, 이어 붙인 문장들 자체가 아니며, 두 사건의 내용을 모두 담아야 한다."""
    nos = lambda x: re.sub(r"\s+", "", nfc(x or ""))
    e = nos(ev).strip("“”\"'‘’「」『』")
    if len(e) < 5:
        return "두 사건의 관계를 보여 주는 원문 문장(connection_evidence)을 인용하지 않았다"
    if not ctx or e not in nos(ctx.get("orig")):
        return "인용한 근거 문장을 원문에서 찾지 못했다"
    for it in [x for x in (ctx.get("items") or []) if "사건 연결 추가" in x.get("kinds", [])]:
        sents = split_sentences(nfc(it.get("src", "")))
        if any(e == nos(x) or e in nos(x) for x in sents):
            return "이어 붙인 문장 자체를 근거로 인용했다"
        if len(sents) >= 2 and not all(_content_keys(x) & {k for k in _content_keys(ev)} for x in (sents[0], sents[-1])):
            return "인용한 근거 문장이 두 사건을 함께 담고 있지 않다"
        if len(sents) >= 2:  # 인물 이름만 겹치면 안 된다: 이어 붙인 두 사건의 서술어가 근거 문장에 모두 있어야 한다
            miss = [pr for pr in (_pred_root(sents[0]), _pred_root(sents[-1])) if pr and not _root_in(pr, ev)]
            if miss:
                return "인용한 근거 문장에 연결하려는 사건의 서술어(%s)가 없다(인물만 같이 나온 문장은 근거가 아니다)" % ", ".join(miss)
    return None


def gate_decide(edit, kinds=(), ctx=None):
    """위험 수정의 결정.
    - 사실 종류(FACT_KINDS: 숫자·단위·날짜·인용·식별자·이름 목록, 부정, 이미·아직, 모두·오직)는 사용자 지시나 출처 확인이 근거와 함께
      명시된 경우(override: user|source, override_evidence 8자 이상)만 유지하고, 아니면 되돌린다. 모델의 이유로는 유지할 수 없다.
    - 확인 종류(REVIEW_KINDS)는 도구의 추정 신호다: 기본은 유지(좋은 편집을 살린다). 모델이 읽고 뜻이 바뀌었다고 보면 decision=REVERT로 되돌린다.
    - decision=REVERT는 종류와 상관없이 되돌린다."""
    d = str(edit.get("decision", "")).strip().upper()
    r = str(edit.get("decision_reason", edit.get("reason", ""))).strip()
    if d == "REVERT":
        return "REVERT", ("되돌림: " + r) if r else "되돌림(판정)"
    fact = [k for k in kinds if k in FACT_KINDS]
    if fact:
        ov = str(edit.get("override", "")).strip().lower()
        ev = str(edit.get("override_evidence", edit.get("evidence", ""))).strip()
        if ov in ("user", "source", "사용자", "출처") and len(ev) >= 8:
            return "KEEP", "사실 유지(%s): %s" % ("사용자 지시" if ov in ("user", "사용자") else "출처 확인", ev)
        fj = "·".join(fact); last = ord(fj[-1]) - 0xAC00
        return "REVERT", "자동 되돌림(사실): %s%s 사용자 지시·출처 없이 바꾸지 않는다(지시·출처가 있으면 override와 근거를 적는다)" % (
            fj, "은" if 0 <= last < 11172 and last % 28 else "는")
    return "KEEP", ("유지: " + r) if r else "유지(확인 항목: 뜻이 바뀌었으면 REVERT)"


def gate_log_md(log):
    rows = ["| 번호 | 위치 | 등급 | 종류 | 원문 → 수정 | 판정 | 이유 |", "|---|---|---|---|---|---|---|"]
    for it in log:
        rows.append("| %s | %s | %s | %s | %s → %s | %s | %s |" % (it["id"], it["where"], it.get("grade", "사실" if it.get("fact") else "확인"), "·".join(it["kinds"]),
                                                               snip(it["src"], 40) or "(없음)", snip(it["out"], 40) or "(지움)", it["decision"], it["note"].replace("|", "/")))
    return "\n".join(rows)


def block_summary(bl):
    if not bl:
        return ""
    return " · 행위자·말하는 동사 %d · 지운 문장의 고유 정보 %d · 사라진 말 %d · 새 말만 %d · 바뀐 낱말만 %d" % (
        sum(1 for b in bl if b["notes"]), sum(1 for b in bl if b["kind"] == "지움" and b["lost"]),
        sum(1 for b in bl if b["kind"] != "지움" and b["lost"]), sum(1 for b in bl if not b["lost"] and b["new"] and not b["notes"]),
        sum(1 for b in bl if not (b["lost"] or b["new"] or b["notes"]) and b.get("swapped")))


def one_letter_changes(src_text, out_text):
    """원문에서 줄어든 명사(조사가 붙은 적 있는 2~4음절) 자리에 자모 하나만 다른 새 낱말이 생겼으면 알린다(세린→세릭).
    이름 오타 변경을 놓치지 않기 위한 안전장치다(도구의 추측이라 '읽고 판단' 목록으로만 보인다). 맞춤법 교정도 여기에 사실로 보인다(교정인지 이름 오타인지는 모델이 판단한다)."""
    try:
        import consistency as cs
    except Exception:
        return []

    def stems(t):
        c, noun = Counter(), set()
        for tok in nfc(t).split():
            st, pt = cs.stem(tok)
            if re.fullmatch(r"[가-힣]{2,4}", st):
                c[st] += 1
                if pt:
                    noun.add(st)
        return c, noun

    c_src, noun = stems(src_text)
    c_out, _n = stems(out_text)
    stop = cs.KIN | cs.COMMON_PERSON | NOT_NAME
    new = [t for t in c_out if c_src[t] == 0]
    out = []
    for s in sorted(x for x in c_src if x in noun and x not in stop and c_out[x] < c_src[x]):
        t = next((t for t in new if len(t) == len(s) and cs.one_edit(s, t)), None)
        if t:
            out.append("[한 글자 바뀐 낱말] '%s' → '%s' (이름이면 되돌리고, 교정이면 둔다)" % (s, t))
    return out


def protected_items(text, quotes=True):
    """보호 항목. quotes=False면 따옴표 안을 인용 항목으로 세지 않는다(소설·웹소설 대사). 따옴표 안의 숫자·URL·식별자는 그대로 센다."""
    text = nfc(text)
    items, masked = Counter(), list(text)

    def blank(mo):
        for k in range(mo.start(), mo.end()):
            masked[k] = " "

    for kind, pat in (("URL", URL_RE), ("이메일", EMAIL_RE), ("코드", CODE_RE), ("경로", PATH_RE)):
        cur = "".join(masked)
        for mo in pat.finditer(cur):
            items[(kind, mo.group(0).rstrip(".,;:!?"))] += 1
            blank(mo)
    cur = "".join(masked)
    for mo in ENUM_RE.finditer(cur):  # 목록 번호(줄 첫머리 1.·2), 1)·(1))는 사실이 아니다: 목록을 문장으로 풀어도 사라진 숫자로 세지 않는다
        blank(mo)
    cur = "".join(masked)
    for mo in LATIN_RE.finditer(cur):
        if re.search(r"[A-Z0-9_+#.]", mo.group(0)) and not (mo.start() > 0 and cur[mo.start() - 1] == "#"):  # 해시태그(#AI)는 꼬리표라 식별자로 세지 않는다
            items[("식별자", mo.group(0))] += 1
        blank(mo)
    cur = "".join(masked)
    for mo in DATE_RE.finditer(cur):  # 2026.9.25 = 2026-09-25 = 2026년 9월 25일
        y, mth, d = int(mo.group(1)), int(mo.group(2)), int(mo.group(3))
        for k in ("%d년" % y, "%d월" % mth, "%d일" % d):
            items[("숫자", k)] += 1
    cur = DATE_RE.sub(lambda m: " " * len(m.group(0)), cur)
    for mo in DATE_TAIL_RE.finditer(cur):  # 날짜 범위의 뒤쪽(2025.10.13~11.7의 11.7 = 11월 7일)
        items[("숫자", "%d월" % int(mo.group(1)))] += 1
        items[("숫자", "%d일" % int(mo.group(2)))] += 1
    cur = DATE_TAIL_RE.sub(lambda m: " " * len(m.group(0)), cur)
    for mo in YM_RE.finditer(cur):  # 연.월(2024.3 = 2024년 3월)
        items[("숫자", "%d년" % int(mo.group(1)))] += 1
        items[("숫자", "%d월" % int(mo.group(2)))] += 1
    cur = YM_RE.sub(lambda m: " " * len(m.group(0)), cur)
    for mo in SHORT_DATE_RE.finditer(cur):  # 9/1 = 9월 1일(연도 없는 달/날)
        items[("숫자", "%d월" % int(mo.group(1)))] += 1
        items[("숫자", "%d일" % int(mo.group(2)))] += 1
    cur = SHORT_DATE_RE.sub(lambda m: " " * len(m.group(0)), cur)
    cur = IDIOM_NUM_RE.sub(lambda m: " " * len(m.group(0)), cur)  # 두 마리 토끼·백 번은 수가 아니라 관용구다
    for mo in RANGE_RE.finditer(cur):  # 범위의 앞 수는 뒤 수의 단위를 따른다: 2~4시 = 2시·4시, 3-5명 = 3명·5명
        a_, u_ = mo.group(1), unit_norm(mo.group(3))
        items[("숫자", ("%d" % float(a_) if float(a_) == int(float(a_)) else a_) + u_)] += 1
        cur = cur[:mo.start(1)] + " " * (mo.start(2) - mo.start(1)) + cur[mo.start(2):]  # 앞 수와 붙임표를 가린다(뒤 수는 부호 없이 단위와 함께 센다)
    for mo in ORD_RE.finditer(cur):  # 제3자 = 제삼자, 제2회 → 제3회는 다른 차례
        w = mo.group(1)
        v = int(w) if w.isdigit() else sino_value(w)
        if v:
            items[("숫자", "제%d" % v)] += 1
    cur = ORD_RE.sub(lambda m: " " * len(m.group(0)), cur)
    MAG = {"천": 10 ** 3, "만": 10 ** 4, "십만": 10 ** 5, "백만": 10 ** 6, "천만": 10 ** 7, "억": 10 ** 8, "십억": 10 ** 9, "백억": 10 ** 10, "천억": 10 ** 11, "조": 10 ** 12}
    pend = None  # 자릿말이 이어지면 합친다: 1억 2천만 원 = 120000000원
    for mo in NUM_RE.finditer(cur):
        num, dec, mag, unit = mo.group("num").replace(",", ""), mo.group("dec") or "", mo.group("mag"), mo.group("unit")
        v = float(num + dec) * (MAG[mag] if mag else 1)
        if pend and pend[1] and mo.start() <= pend[2] + 1 and mag and pend[0] > v:
            v, pend = pend[0] + v, None
        if mag and not unit:
            pend = (v, mag, mo.end())
            if cur[mo.end():mo.end() + 3].strip()[:1] in "0123456789":
                continue  # 다음 숫자와 합친다
        else:
            pend = None
        vs = ("%d" % v) if v == int(v) else str(v)
        sign = {"−": "-", "–": "-"}.get(mo.group("sign") or "", mo.group("sign") or "")  # -5%·+5%·5%는 서로 다른 값
        key = "%s%s%s%s" % (sign, mo.group("cur") or "", vs, unit_norm(unit))
        items[("숫자", key)] += 1
    for _mo in HALF_HOUR_RE.finditer(cur):  # 10시 반 = 10시 30분
        items[("숫자", "30분")] += 1
    for val, unit, weak in sino_counts(cur):  # 띄어 쓴 한자어 수 + 단위(십칠 분, 삼십만 원), 연도·날짜가 붙은 달 이름(2026년 오월, 오월 3일). 한 음절 수·붙여 쓴 것·그 밖의 달은 약한 항목
        items[("수량?" if weak else "숫자", "%d%s" % (val, unit_norm(unit)))] += 1
    # 따옴표는 종류가 아니라 자리로 본다: 줄 첫머리·공백 뒤는 여는 것, 글자 뒤이면서 공백·줄 끝·문장부호 앞은 닫는 것
    # (원문의 “…요?“, ”네…, “…습니까?" 같은 모양을 고친 것은 인용 변화가 아니다)
    for val, unit, weak in korean_counts(cur):  # 한글 수량: 두 명 = 2명, 첫 번째 = 1번째. 붙여 쓴 것은 약한 항목
        items[("수량?" if weak else "숫자", "%d%s" % (val, unit_norm(unit)))] += 1
    if not quotes:
        return items
    qtext = re.sub(r"(?:(?<=^)|(?<=\s))[“”\"](?=\S)", "“", text, flags=re.M)
    qtext = re.sub(r"(?<=\S)[“”\"](?=\s|$|[,.!?…)])", "”", qtext, flags=re.M)
    qtext = re.sub(r"“([^“”\"\n]{1,300})\"", r"“\1”", qtext)  # "밥이 달다"라고: 닫는 곧은따옴표 뒤에 조사가 바로 붙은 것
    for mo in QUOTE_ITEM_RE.finditer(qtext):
        inner = next(g for g in mo.groups() if g is not None).strip()
        if inner:
            items[("인용", inner)] += 1
    return items


KO_TENS = {"열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50, "예순": 60, "일흔": 70, "여든": 80, "아흔": 90}
KO_UNITS = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "석": 3, "셋": 3, "네": 4, "넉": 4, "넷": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}
KO_COUNTER = ("번\\s?째|번|명|개|달|시간|시|주일|주|회|장|권|대|층|살|사람|마리|잔|병|채|벌|켤레|군데|곳|가지|분|초|해|날|밤|걸음|배|쪽|줄|통|알|그루|"
              "송이|자루|칸|판|편|곡|차례|건|평|쌍|척|끼")
KO_NUM_RE = re.compile(r"(?<![가-힣])(열|스물|스무|서른|마흔|쉰|예순|일흔|여든|아흔)?\s?(한|하나|두|둘|세|석|셋|네|넉|넷|다섯|여섯|일곱|여덟|아홉)?(\s?)("
                       + KO_COUNTER + r")(?=[가-힣]{0,5}(?:[^가-힣]|$))")  # 살이었다·명이었는데처럼 서술어가 붙어도
KO_ORD_RE = re.compile(r"(?<![가-힣])(첫|둘|셋|넷|다섯|여섯|일곱|여덟|아홉|열)째(?![가-힣]{3})")
KO_TENS_BARE_RE = re.compile(r"(?<![가-힣])(열|스물|서른|마흔|쉰|예순|일흔|여든|아흔)(하나|둘|셋|넷|다섯|여섯|일곱|여덟|아홉)?"
                             r"(?=(?:이었다|였다|이었고|였고|이다|입니다|이에요|예요|이고|이며|이었는데|였는데|이었지만|였지만|이야|이지만|을|를|은|는|이|가|의|에|도)?(?:[^가-힣]|$))")
KO_BARE_RE = re.compile(r"(?<![가-힣0-9A-Za-z.,])((?:열|스물|스무|서른|마흔|쉰|예순|일흔|여든|아흔)(?:\s?(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉))?|두|세|네|다섯|여섯|일곱|여덟|아홉)(?=\s[가-힣])")


VERBY_UNITS = {"알", "살", "줄", "대", "채", "벌", "판", "통", "배", "쪽"}  # 동사 어간과 같은 단위: 뒤가 조사·서술어일 때만 수


def korean_counts(text):
    """고유어 수 + 수량 단위(두 명, 열세 건, 스물한 살, 첫 번째)와 서수(둘째)를 (값, 단위)로. '한' 같은 말은 단위 앞에서만 수로 본다.
    붙여 쓴 '한층'(부사)은 뺀다. '한번'은 띄어쓰기 교정(한번 → 한 번)과 짝이 맞도록 수로 본다."""
    out = []
    for m in KO_NUM_RE.finditer(text):
        tens, units, sp, unit = m.group(1), m.group(2), m.group(3), m.group(4)
        if not (tens or units):
            continue
        if unit in VERBY_UNITS and not re.match(r"(?:이었다|였다|이다|입니다|이에요|이었|이|가|을|를|은|는|도|만|의|에|로|과|와|씩|째|짜리)?(?:[^가-힣]|$)", text[m.end():]):
            continue  # '하나 알아들을'의 알, '한 살아남은'의 살은 동사
        if units == "한" and not tens:  # '못 한 채', '일을 한 채', '한 줄 알았다', '한 대로'의 '한'은 '하다'다
            prev = text[:m.start()].rstrip().split()[-1:] or [""]
            after = text[m.end():m.end() + 3]
            if (prev[0] in ("못", "안", "잘", "다", "더", "덜", "좀", "이미", "아직", "꼭", "먼저") or prev[0].endswith(("을", "를"))
                    or (unit == "줄" and re.match(r"\s?(?:알|몰)", after)) or (unit == "대" and after.startswith("로"))):
                continue
        val = KO_TENS.get(tens, 0) + KO_UNITS.get(units, 0)
        out.append((val, re.sub(r"\s", "", unit), not re.search(r"\s", m.group(0).strip()) or val == 1))  # 붙여 쓴 것(한편·세대·한번)과 '한'(한 번에·한 줄·주사 한 대)은 약한 항목
    for m in re.finditer(r"(?<![가-힣])첫(\s?)번\s?째", text):  # 첫 번 째 = 첫 번째(값 1: 약한 항목)
        out.append((1, "번째", True))
    taken = [m.span() for m in KO_NUM_RE.finditer(text)]
    for m in KO_BARE_RE.finditer(text):  # 단위 없이 이름씨 앞에 온 고유어 수(다섯 문제·열 문제)는 약한 항목: 10문제 = 열 문제 짝 맞추기에만
        if any(a < m.end() and m.start() < b for a, b in taken):
            continue
        g = re.match(r"(열|스물|스무|서른|마흔|쉰|예순|일흔|여든|아흔)?\s?(한|두|세|네|다섯|여섯|일곱|여덟|아홉)?$", m.group(1))
        if g:  # '열'(열다)·'네'(너의)만 쓰인 것은 다른 말과 겹쳐 약한 항목, 나머지(세 묶음·다섯 문제·스무 장)는 수
            out.append((KO_TENS.get(g.group(1), 0) + KO_UNITS.get(g.group(2), 0), "", m.group(1) in ("열", "네")))
    for m in KO_TENS_BARE_RE.finditer(text):  # 단위 없이 쓴 고유어 두 자리 수(올해 예순하나, 스물둘이었다): 61세 = 예순하나
        if any(a < m.end() and m.start() < b for a, b in taken):
            continue
        tens_, unit_ = m.group(1), m.group(2)
        if not unit_ and tens_ in ("열", "쉰"):
            continue  # '열'(열다)·'쉰'(쉬다)만 쓰인 것은 수로 단정하지 않는다
        out.append((KO_TENS[tens_] + (KO_UNITS.get(unit_, 0) if unit_ else 0), "", False))
    for m in KO_ORD_RE.finditer(text):  # 서수: 첫째와 목록 표지(둘째, · 셋째로)는 약한 항목(목록을 문장으로 풀면 사라진다). 둘째 아들·셋째 날은 수
        v = {"첫": 1, "둘": 2, "셋": 3, "넷": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}[m.group(1)]
        out.append((v, "째", v == 1 or bool(re.match(r"\s*[,.:)]|(?:로|으로)(?:\s|,)", text[m.end():]))))
    return out


SINO_D = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "륙": 6, "칠": 7, "팔": 8, "구": 9}
SINO_S = {"십": 10, "백": 100, "천": 1000}
SINO_B = {"만": 10 ** 4, "억": 10 ** 8, "조": 10 ** 12}
SINO_UNIT = ("분|초|시간|시|일|주일|주|개월|년|원|층|호|번|명|개|퍼센트|도|킬로미터|킬로그램|킬로|미터|센티미터|센티|그램|리터|세|대|위|등|회|장|쪽|권|건|배|평|점|달러|엔|유로")
SINO_TAIL = (r"(?:이었다|였다|이었고|였고|이었는데|였는데|이었지만|였지만|이었습니다|였습니다|입니다|이다|이에요|예요|이요|이며|이고|이라는|이라고|이라서|이라|이나|이면|이지만|이야|"
             r"에서는|에서|에는|에도|에게|으로는|으로|로는|로|이내|이상|이하|이후|이전|가량|정도|남짓|어치|짜리|동안|안팎|내외|만에|밖에|이나마|이라도|라도|"
             r"이|가|을|를|은|는|도|만|의|에|과|와|씩|째|간|쯤|께|부터|까지|당|마다|대|여|분|전|후|뒤|뿐|차)?")
SINO_RE = re.compile(r"(?<![가-힣0-9.,])([일이삼사오육륙칠팔구십백천만억조]+(?:\s(?=[일이삼사오육륙칠팔구십백천])[일이삼사오육륙칠팔구십백천만억조]+)*)(\s?)("
                     + SINO_UNIT + r")(?=" + SINO_TAIL + r"(?:[^가-힣]|$))")
MONTH_RE = re.compile(r"(?<![가-힣])(십일|십이|십|일|이|삼|사|오|유|육|칠|팔|구|시)월(?=(?:에는|에도|에서|부터|까지|초에|말에|에|은|는|의|을|도|과|와|로|초|말)?(?:[^가-힣]|$))")
MONTHS = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "유": 6, "육": 6, "칠": 7, "팔": 8, "구": 9, "시": 10, "십": 10, "십일": 11, "십이": 12}
UNIT_NORM = {"사람": "명", "세": "살", "퍼센트": "%", "프로": "%", "달": "개월", "주일": "주", "해": "년", "키로": "킬로", "킬로그램": "kg", "킬로미터": "km",
             "미터": "m", "센티미터": "cm", "센티": "cm", "그램": "g", "리터": "l"}


def unit_norm(u):
    """같은 단위의 다른 표기를 하나로(두 사람 = 2명, 스무 살 = 20세, 3퍼센트 = 3%)."""
    u = re.sub(r"\s", "", u or "")
    return UNIT_NORM.get(u, u)


def sino_value(w):
    total = section = num = 0
    for ch in w.replace(" ", ""):
        if ch in SINO_D:
            num = SINO_D[ch]
        elif ch in SINO_S:
            section += (num or 1) * SINO_S[ch]
            num = 0
        elif ch in SINO_B:
            total += ((section + num) or 1) * SINO_B[ch]
            section = num = 0
        else:
            return None
    return total + section + num


def sino_counts(text):
    """숫자로 읽는 방법이 하나뿐인 한자어 수만: 자릿값(십·백·천·만·억·조)이 든 수 + 단위(십칠 분, 삼십만 원, 이천이십육 년)와 달 이름(오월·유월·시월).
    한 음절 한자어 수(이 분, 오 분, 일 년)는 '이분·이번' 같은 다른 말과 겹쳐 보지 않는다(바뀐 낱말로 모델이 본다)."""
    out, masked = [], text
    for m in MONTH_RE.finditer(text):  # 달 이름을 먼저 읽고 그 자리를 가린다(십일월을 '10일'로 읽지 않게)
        after = text[m.end():m.end() + 6]
        before = text[max(0, m.start() - 6):m.start()]
        timed = (bool(re.match(r"\s?(?:\d{1,2}|[일이삼사오육칠팔구십]{1,3})\s?일", after))
                 or bool(re.search(r"년\s$", before)))  # 연도·날짜가 붙은 것만 확실한 달. '이월에'(회계)·'오월부터'는 숫자로 단정하지 않는다
        out.append((MONTHS[m.group(1)], "월", not timed))  # 연도가 앞에 있거나 날짜가 뒤에 있을 때만 강하게(2026년 오월, 오월 3일). 오월에·이월에는 약하게
        masked = masked[:m.start()] + "\u3000" * (m.end() - m.start()) + masked[m.end():]
    for m in SINO_RE.finditer(masked):
        w, sp, unit = m.group(1), m.group(2), m.group(3)
        one = not re.search(r"[십백천만억조]", w)
        if one and len(w) != 1:
            continue  # 자릿값 없는 여러 음절(삼사)은 수로 읽지 않는다. 한 음절 수(일 년·이 분·오 분)는 약한 항목(같은 값 짝 맞추기에만)
        v = sino_value(w)
        if v:
            out.append((v, unit, one or not sp))  # 한 음절 수(오 분 = '오는 분'일 수 있다)·붙여 쓴 것(백일장·삼십만원)은 약한 항목: 경보 없이 같은 값 짝 맞추기에만
    return out


ROLE_OF = {"이": "주체", "가": "주체", "께서": "주체", "을": "대상", "를": "대상", "에서": "출발·장소", "에게서": "출발·장소", "한테서": "출발·장소",
           "로부터": "출발·장소", "으로부터": "출발·장소", "으로": "방향", "로": "방향", "에": "방향", "에게": "받는 이", "한테": "받는 이", "께": "받는 이",
           "보다": "비교 기준", "의": "관형 기준"}


LIMIT_PARTICLES = ("만", "도", "조차", "마저", "밖에", "뿐", "까지", "부터", "만은", "만도", "도만")


DROPPABLE = ({p + j for p in ("나", "저", "그", "그녀", "우리", "저희", "그들", "너", "너희", "당신") for j in ("는", "은", "가", "이")}
             | {"내가", "제가", "네가", "그리고", "그런데", "및"})  # 빠지거나 더해져도 뜻이 거의 그대로인 말(1인칭 '나는' 줄이기 등)


def _content_seq(text):
    """무경고 변경 판단용: 주어 대명사(나는·그는)와 '그리고·그런데'를 뺀 낱말의 차례와, 뺀 말들."""
    toks = [t[:-1] if len(t) >= 3 and t[-1] in "과와" else t for t in re.findall(r"[가-힣A-Za-z0-9]+", nfc(text))]  # 나열의 '과·와'는 가운뎃점·쉼표와 같다
    return [t for t in toks if t not in DROPPABLE], sorted(t for t in toks if t in DROPPABLE)


def limit_particles(text):
    """명사마다 붙은 한정·첨가 조사(철수만 / 철수도 / 철수조차): 바뀌면 뜻이 바뀐다."""
    import consistency as cs
    out = defaultdict(list)
    for tok in re.findall(r"[가-힣]+", nfc(text)):
        st, p = _stem_role(tok)
        if st == tok and len(tok) == 2 and tok[0] in PRONOUNS and tok[1] in "은는이가을를도만":
            st, p = tok[0], tok[1]
        if p and st and (st in PRONOUNS or (st not in BLOCK_STOP and not _is_pred(tok))):
            out[st].append(p)
    return out


def role_seq(text):
    """역할마다 명사가 나온 차례(같은 역할에 명사가 여럿일 때 차례가 바뀌었는지 보는 데 쓴다)."""
    import consistency as cs
    out = defaultdict(list)
    toks = re.findall(r"[가-힣]+", nfc(text))
    for i, tok in enumerate(toks):
        st, p = _stem_role(tok)
        base = p[:-1] if (p not in ROLE_OF and len(p) >= 2 and p[-1] in "은는도만" and p[:-1] in ROLE_OF) else p
        if base in ROLE_OF and st and (st in PRONOUNS or (st not in BLOCK_STOP and not _is_pred(tok))):
            out[ROLE_OF[base]].append(st)
    return out


PRONOUNS = ("나", "너", "그", "그녀", "우리", "그들", "저", "당신", "자기", "자신", "너희", "저희", "이들", "얘", "쟤", "걔")


def _stem_role(tok):
    """역할 비교용: 한 음절 대명사(나를·그를)도 어간과 조사로 나눈다."""
    import consistency as cs
    st, p = cs.stem(tok)
    if st == tok and len(tok) >= 2 and tok[:-1] in PRONOUNS and tok[-1] in ROLE_OF:
        return tok[:-1], tok[-1]
    if st == tok and len(tok) >= 3 and tok[:-2] in PRONOUNS and tok[-2:] in ROLE_OF:
        return tok[:-2], tok[-2:]
    return st, p


def role_map(text):
    """명사마다 드러난 역할(주체·대상·출발·방향·받는 이·비교 기준). 은/는·도·만처럼 역할이 드러나지 않는 조사는 보지 않는다."""
    import consistency as cs
    out = defaultdict(list)
    toks = re.findall(r"[가-힣]+", nfc(text))
    for i, tok in enumerate(toks):
        st, p = _stem_role(tok)
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if p == "에" and nxt.startswith(("대해", "대한", "대하여", "관해", "관한", "의해", "의한", "따라", "따른", "비해", "불과", "있어")):
            continue  # '-에 대해' 등은 역할이 아니라 뒤따르는 말과 한 덩어리다
        if p in ("로", "으로") and (nxt.startswith(("인해", "인한", "말미암")) or tok.endswith(("로서", "로써"))):
            continue
        if p in ("을", "를") and nxt.startswith(("통해", "통한", "통하여", "위해", "위한", "위하여")):
            continue  # '경험을 통해 → 경험으로'는 역할 변화가 아니다
        base = p[:-1] if (p not in ROLE_OF and len(p) >= 2 and p[-1] in "은는도만" and p[:-1] in ROLE_OF) else p  # 에게는 → 에게
        if base in ROLE_OF and st and (st in PRONOUNS or (st not in BLOCK_STOP and not _is_pred(tok))):
            out[st].append(ROLE_OF[base])
    return out


ORDER_MARK = re.compile(r"^(?:먼저|처음|나중|다음|이후|마지막|첫째|둘째|셋째|넷째|다섯째|아침|점심|오후|저녁|\d+일차)(?:에|에는|엔|은|는|로|으로)?$")


def order_nouns(text):
    """순서 표지(먼저·첫째·다음·아침·1일차 등)가 있으면, 첫 표지 뒤에 나오는 명사의 차례."""
    import consistency as cs
    toks = re.findall(r"[가-힣0-9]+", nfc(text))
    idx = next((i for i, t in enumerate(toks) if ORDER_MARK.match(t)), None)
    if idx is None:
        return []
    seq = []
    for t in toks[idx + 1:]:
        st, p = cs.stem(t)
        if ORDER_MARK.match(t) or st in ("날", "번", "째") or st in BLOCK_STOP or (not p and PRED_END.search(t)):  # 맨 명사(서울)는 남긴다
            continue
        if p or len(st) >= 2:
            seq.append(st)
    return seq


STYLE_NEG = re.compile(  # 수사적 부정: 명제를 부정하지 않는다(이루 말할 수 없다 = 매우 크다, 뿐만 아니라 = 도, 적지 않다 = 많다)
    r"이루\s*(?:다\s*)?말할\s*수\s*(?:없|없는|없을)|더할\s*나위\s*(?:없|없는)|두말할\s*(?:것도|필요도|나위도)\s*없|말할\s*것도\s*없|비할\s*데\s*(?:없|없는)|그지\s*없|한없|끝없|"
    r"(?:뿐(?:만)?|만)\s*아니라|"
    # 위험 수정 게이트: 대조 틀(X가 아니라 Y)·강조 관용구(걷잡을 수 없는·형언할 수 없는·셀 수 없이·틀림없다·아낌없이)·'~ㄹ 수밖에 없다'(당연)·'X밖에 없다'(범위: 오직 X).
    # 완곡 긍정(적지 않다·그치지 않는다)은 세되, 부정이 걸린 말이 짝 문장에 부정 없이 남은 경우만 뒤집힘으로 본다(_neg_flip)
    r"아니라(?=[\s,])|틀림\s*없|손색이\s*없|다름\s*(?:이\s*)?없|아니나\s*다를까|"
    r"(?:걷잡을|형언할|헤아릴|셀|이루\s*헤아릴|말로\s*다\s*할)\s*수\s*(?:없|없는|없이|없을)|거침\s*없|아낌\s*없|빈틈\s*없|쉴\s*새\s*없|"
    r"수밖에\s*없|밖에\s*(?:없|안\s|못\s)|"
    r"(?:아니었을까|아닐까|아닌가|아니겠는가|않았을까|않을까|않은가|않겠는가)(?:요)?(?=\s*[?？])")  # 수사 의문(이유가 아니었을까? = 이유였을 것이다)
MERELY_NEG = re.compile(r"(?:단순한|단순히|단지|그저|한낱)\s*([^.?!\n]{0,20}?)\s*(?:이|가)?\s*(?:아니|아닙|아냐)")  # '단순한 X가 아니다' 틀
HEDGE = re.compile(r"수도\s*있|일\s*수\s*있|가능성|것\s*같|듯하|듯싶|아마|지도\s*모르|일지도|추정|보인다|보입니다|보이며|예상|전망|않을까|아닐까|의심|[?？]")


def _merely_frames(text, limit=None):
    """'단순한 X가 아니다' 틀마다 (시작, 끝, X, 뒤따르는 말, 상태). 뒤따르는 말은 '아니라 Y'면 같은 문장의 Y, 아니면 다음 문장이다.
    상태: definite(약한 양태 없이 단언) · hedged(~일 수도 있다·가능성·것 같다·아마·추정) · none(뒤따르는 말이 보이지 않음).
    limit를 주면 그 앞에서 시작하는 틀만 본다(뒤 문장은 문맥으로만 쓴다)."""
    t = nfc(text or ""); out = []
    for m in MERELY_NEG.finditer(t):
        if limit is not None and m.start() >= limit:
            break
        rest = t[m.end():]
        if rest.startswith("라"):  # 단순한 X가 아니라 Y
            follow = re.match(r"라\s*([^.?!\n]*)", rest).group(1); end = m.end() + 1
        else:
            cur = re.match(r"[^.?!\n]*[.?!]?", rest).group(0); end = m.end() + len(cur)
            nxt = re.match(r"\s*([^.?!\n]*[.?!]?)", t[end:]); follow = nxt.group(1) if nxt else ""
            lead = re.match(r"\s*(?:그것은|이것은|그건|이건)", t[end:])
            if lead and follow.strip() and not HEDGE.search(follow):
                end += lead.end()
        x = (m.group(1) or "").strip().split()
        status = "none" if not follow.strip() else ("hedged" if HEDGE.search(follow) else "definite")
        out.append((m.start(), end, re.sub(PARTICLE_TAIL, "", x[-1]) if x else "", follow.strip(), status))
    return out


def _merely_safe_spans(text, follow=""):
    """'단순한 X가 아니다' 틀이 STYLE_SAFE인 자리(text 안): 뒤따르는 말이 확정 단언일 때만(단순한 운동이 아닙니다. 그것은 삶의 방식입니다).
    '단순한 실수가 아닙니다. 범죄일 수도 있습니다'의 앞 문장은 확정 정보라 틀로 보지 않는다. follow는 text 바로 뒤 문장(블록 밖 문맥)."""
    t = nfc(text or ""); joined = t + (" " + nfc(follow) if follow else "")
    return [(a, min(b, len(t))) for a, b, _x, _f, st in _merely_frames(joined, limit=len(t)) if st == "definite"]


def _merely_flip(a, b):
    """'단순한 X가 아니다' → '단순한 X다'(부정이 사라진 같은 틀)."""
    for _s, _e, x, _f, _st in _merely_frames(a):
        if len(x) < 1:
            continue
        for sent in split_sentences(nfc(b)):
            if re.search(r"(?:단순한|단순히|단지|그저|한낱)\s*[^.?!\n]{0,12}?" + re.escape(x[:2]), sent) and not NEG_RE.search(sent):
                if not any(re.search(re.escape(x[:2]), s0) and not NEG_RE.search(s0) and re.search(r"단순|단지|그저|한낱", s0) for s0 in split_sentences(nfc(a))):
                    return "'단순한 %s가 아니다'가 '단순한 %s다'로: 부정이 사라졌다" % (x, x)
    return None


def _merely_hedged_lost(a, b, follow=""):
    """뒤 말이 가능성만 말하는 '단순한 X가 아니다'(확정 부정)가 수정문에서 사라졌는가. 사라졌으면 (X, 설명)."""
    t = nfc(a or "")
    for _s, _e, x, fol, st in _merely_frames(t + (" " + nfc(follow) if follow else ""), limit=len(t)):
        if st != "hedged" or len(x) < 1:
            continue
        if not any(re.search(re.escape(x[:2]), s0) and NEG_RE.search(s0) for s0 in split_sentences(nfc(b))):
            return x, "확정 부정 삭제: '단순한 %s가 아니다'의 뒤 말('%s')은 가능성만 말해 앞의 확정 부정을 대신하지 못한다" % (x, snip(fol, 24))
    return None


NEG_RE = re.compile(GUARDS[0][1])


def _double_neg_obligation(a, b):
    """이중 부정을 의무 표현으로 바꿨는가(하지 않으면 안 된다·하지 않을 수 없다 → 해야 한다)."""
    return bool(re.search(r"(?:지\s*않으면\s*안|지\s*않을\s*수\s*없|지\s*않고는\s*안)", a)) and bool(re.search(r"야\s*(?:한다|합니다|했다|해|하|할|돼|된다)|반드시|꼭", b))


def _neg_keys(t):
    """부정 비교용 사건 핵심어 열쇠(대명사·지시어·부정어·'수'는 뺀다)."""
    out = set()
    for tok in re.findall(r"[가-힣]+", nfc(t or "")):
        bare = re.sub(PARTICLE_TAIL, "", tok) or tok
        if (tok in BLOCK_STOP or bare in BLOCK_STOP or tok in DROPPABLE or tok in ("수", "아무", "아무도", "아무것도", "안", "못")
                or NEG_RE.search(tok) or re.match(r"(?:않|없|아니|아닙|아님|못하|못한|못했)", tok)):
            continue
        k = _event_key(tok)
        if k:
            out.add(k)
    return out


def _syl_class(ch):
    """활용에 흔들리지 않는 음절 열쇠: 첫소리 + 모음 계열 + 받침(없음·ㄴ·ㄷ·ㄹ·ㄻ·ㅁ·ㅂ·ㅅ·ㅆ·ㅎ은 활용·불규칙·명사형이라 하나로). 갈·갔·가·간·듣·들은 같고, 적과 정은 다르다."""
    k = ord(ch) - 0xAC00
    if not 0 <= k < 11172:
        return None
    jong = k % 28
    return (k // 588, VOWEL_FAMILY.get((k % 588) // 28, (k % 588) // 28), 0 if jong in (0, 4, 7, 8, 10, 16, 17, 19, 20, 27) else jong)  # 불규칙(듣/들·짓/지)·명사형(욺·봄)도 활용


def _pred_key(tok):
    """부정이 걸린 말의 열쇠: 어간 첫 음절 열쇠(+ 둘째 음절이 있으면 그 첫소리). 넣지 → 넣, 그치지 → 그ㅊ, 문제가 → 문ㅈ."""
    t = re.sub(r"(?:지|지는|지도|지를|지가)$", "", tok or "") or (tok or "")
    a = _syl_class(t[0]) if t else None
    if a is None:
        return None
    return (a, (ord(t[1]) - 0xAC00) // 588) if len(t) >= 2 and "가" <= t[1] <= "힣" else (a,)


def _pred_in(key, toks):
    """열쇠에 맞는 말(기능어 빼고)이 있는가."""
    for u in toks:
        if not u or u in BLOCK_STOP or u in GATE_FUNC or _syl_class(u[0]) != key[0]:
            continue
        if len(key) == 1 or (len(u) >= 2 and "가" <= u[1] <= "힣" and (ord(u[1]) - 0xAC00) // 588 == key[1]):
            return True
    return False


def _neg_preds(text):
    """부정이 걸린 말의 사건 열쇠(첫 음절): 넣지 않았다·안 넣음 → 넣, 갈 수 없었다 → 가, 문제가 없다 → 문, 학생이 아니다 → 학.
    부정 뒤집힘 후보가 문장을 합치거나 옮긴 결과인지(같은 말의 부정이 반대쪽 글에 같은 횟수만큼 있는지) 보는 데 쓴다. 열쇠마다 횟수(Counter)."""
    toks = re.findall(r"[가-힣]+", nfc(text or ""))
    out = Counter()
    for i, t in enumerate(toks):
        prev = toks[i - 1] if i > 0 else ""
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        tgt = ""
        if t.startswith("않"):
            tgt = prev
        elif t == "안" or t == "못":
            tgt = nxt
        elif t.startswith("못") and t[1:2] in tuple("하했한할해함합") and prev.endswith(("지", "지는", "지도")):
            tgt = prev
        elif t.startswith(("안", "못")) and len(t) > 1 and t[1] in "돼되된됐될됨하했한할해갔가간갈왔오온올":
            tgt = t[1:]
        elif t.startswith("없") and t != "없이":
            tgt = toks[i - 2] if prev in ("수", "수가", "수도") and i >= 2 else prev
        elif t.startswith(("아니", "아닙", "아냐", "아님")):
            tgt = prev
        elif "금지" in t or "불가" in t:
            tgt = re.sub(r"(?:금지|불가).*$", "", t) or prev
        k = _pred_key(tgt) if tgt else None
        if k:
            out[k] += 1
    return out


NOUN_AN = set("안경 안전 안내 안녕 안개 안방 안쪽 안심 안정 안목 안건 안색 안부 안팎 안주 안장 안구 안과 안면 안락 안보 안배 안착 안치 안테 안타 안달 "
              "안간 안기 안겨 안긴 안아 안고 안은 안을 안다 안았 안는 안거 안료 안마 안무 안약 안온 안위 안이 안일 안채 안뜰 안사 안살 안쓰 안절 안성 "
              "안에 안의 안으 안까 안도 안만 안과 안내 못자 못난 못생 못내 못해 못지 못다".split())
DOUBLE_NEG = re.compile(r"(?:없|아니|않|못하)(?:지|지는|지도|지가)\s*않|(?:않|없|아니|못하)(?:으면|면)\s*(?:안\s*(?:되|돼|된)|않)")


def _attach_neg(t, other):
    """붙여 쓴 짧은 부정(안내려가니·못갔다)을 띄어 쓴 꼴로 바꾼 글(비교용). 뒷말과 같은 사건 열쇠가 반대쪽 글에 있을 때만 바꾼다
    (안경·안에·못난 같은 말과 헷갈리지 않게)."""
    okeys = {_event_key(x) for x in re.findall(r"[가-힣]+", nfc(other or ""))}

    def rep(m):
        w = m.group(0)
        if w[:2] in NOUN_AN or len(w) < 3 or w[1] in "되돼된됐될됨하했한할해함합" or _event_key(w[1:]) not in okeys:
            return w  # 안돼·안하는(이미 세는 꼴)과 안경·안에 같은 말은 그대로
        return w[0] + " " + w[1:]
    return re.sub(r"(?<![가-힣])안[가-힣]{2,}", rep, nfc(t or ""))  # 못(못했다·못하지)은 '지 못하다'와 헷갈리지 않게 건드리지 않는다


def _neg_flip(a, b, fa="", fb="", doc_a=None, doc_b=None):
    """부정 뒤집힘(사실 종류): 띄어쓰기와 표기(아닙니다·아님·않 된다·안내려가니)를 맞춘 뒤 블록의 부정 표지 수가 달라졌을 때,
    부정이 든 문장마다 반대쪽 짝 문장(사건 핵심어를 나눠 가진 문장들)을 찾아 부정의 홀짝이 다르고, 부정이 걸린 말이 짝 문장에 부정 없이 남아 있으면 뒤집힘이다.
    갈 수 없었다 → 갔다, 않으면 안 된다 → 않아도 된다, 없지는 않다 → 없다(이중 부정)는 뒤집힘이다. 아니다 → 아닙니다, 되풀이한 부정을 하나로 줄인 것,
    이중 부정을 의무 표현으로 바꾼 것(않으면 안 된다 → 해야 한다), 다른 말로 바꿔 말한 부정(계속 떨렸다 → 멎지 않았다·쉽지 않다 → 어렵다)은 아니다.
    수사적 부정(아니라·적지 않다·수밖에 없다 등)은 세지 않는다. 짝이 없는 부정문(통째로 지움)은 삭제다(확인 목록)."""
    a, b = _attach_neg(a, b), _attach_neg(b, a)
    a = _respace(a, b)
    na, nb = len(NEG_RE.findall(_guard_norm(a, fa))), len(NEG_RE.findall(_guard_norm(b, fb)))
    if na == nb:
        return None
    if na - nb == 2 and re.search(r"야\s*(?:한다|합니다|했다|해|하|할|돼|된다)|반드시|꼭", b):
        return None
    neg_side, other = (a, b) if na > nb else (b, a)
    osents = [(_neg_keys(x), len(NEG_RE.findall(_guard_norm(x))), x) for x in split_sentences(nfc(other))]
    for sent in split_sentences(nfc(neg_side)):
        n = len(NEG_RE.findall(_guard_norm(sent)))
        ks = _neg_keys(sent)
        if not n or not ks or not osents:
            continue
        cover, negs, mate = set(), 0, []  # 반대쪽 짝: 사건 핵심어를 가장 많이 가진 문장부터, 새 핵심어를 보태는 동안 모은다(나눈 문장은 여럿이 짝이다)
        for ok_, on_, ox in sorted(osents, key=lambda o: -len(ks & o[0])):
            if len(cover & ks) / len(ks) >= 0.7 or not (ks & ok_) - cover:
                break
            cover |= ks & ok_
            negs += on_
            mate.append(ox)
        if len(cover) / len(ks) < 0.5:
            continue
        mt = " ".join(mate)
        if DOUBLE_NEG.search(nfc(sent)) or DOUBLE_NEG.search(nfc(mt)):  # 이중 부정이 끼면 부정의 홀짝으로 본다(없지는 않다 → 없다)
            if negs % 2 == n % 2:
                continue
        else:  # 부정이 걸린 말이 짝 문장에 부정 없이(또는 덜 부정된 채) 남아 있어야 같은 사건이 뒤집힌 것이다(나눈 문장에 부정이 흩어져도 말마다 본다)
            preds, mpreds = _neg_preds(sent), _neg_preds(mt)
            mtoks = re.findall(r"[가-힣]+", mt)
            if not any(_pred_in(k, mtoks) and mpreds[k] < preds[k] for k in preds):
                continue
        doc_same, doc_other = (doc_a, doc_b) if neg_side is a else (doc_b, doc_a)
        preds = _neg_preds(sent)
        if doc_other is not None and doc_same is not None and preds:
            cs_, co_ = _neg_preds(doc_same), _neg_preds(doc_other)
            if all(co_[k] > 0 and cs_[k] <= co_[k] for k in preds):
                continue  # 같은 말의 부정이 반대쪽 글에 같은 횟수만큼 있다(문장을 합치거나 옮김)
        return "부정 표지 수 변화(%d → %d): 같은 사건의 긍정·부정이 바뀌었는지" % (na, nb)
    return None


def _neg_types(text, follow=""):
    """부정 표지를 종류별로 센다(안·못·없·아니·금지). 긴 부정(-지 않다)은 안, 불가(능)는 없으로 센다."""
    c = Counter()
    for m in NEG_RE.finditer(_guard_norm(text, follow)):
        g = m.group(0)
        c["못" if g.startswith("못") else "없" if g.startswith("없") else "아니" if g.startswith("아니") else "금지" if g == "금지" else "안"] += 1
    return c


def _respace(a, b):
    """a의 띄어쓰기를 b에 맞춘 글(비교용): 공백을 뺀 글자가 같은 구간은 b의 띄어쓰기를 따른다.
    띄어쓰기만 고친 곳(안남았다 → 안 남았다, 할수있다 → 할 수 있다)을 의미 경계 변화로 세지 않기 위해서다."""
    import difflib
    a, b = nfc(a or ""), nfc(b or "")
    ca = [k for k, ch in enumerate(a) if not ch.isspace()]
    cb = [k for k, ch in enumerate(b) if not ch.isspace()]
    sa, sb = "".join(a[k] for k in ca), "".join(b[k] for k in cb)
    if sa == sb:
        return re.sub(r"\s+", " ", b).strip()
    gap = lambda s, idx, i: i > 0 and idx[i] - idx[i - 1] > 1  # i번째 글자 앞에 공백이 있었는가
    sp = [gap(a, ca, i) for i in range(len(ca))]
    for blk in difflib.SequenceMatcher(a=sa, b=sb, autojunk=False).get_matching_blocks():
        for t in range(1, blk.size):
            sp[blk.a + t] = gap(b, cb, blk.b + t)
    return "".join((" " if sp[i] else "") + a[k] for i, k in enumerate(ca))


def _guard_norm(text, follow=""):
    """의미 경계를 셀 때만 쓰는 정규화: 불가(능)하다 = 수 없다, 가능하다 = 수 있다, 수사적 부정은 뺀다.
    '단순한 X가 아니다'는 뒤 말이 가능성만 말할 때(~일 수도 있다)만 부정으로 센다. 뒤 말이 보이지 않으면 세지 않는다(뒤집힘은 _merely_flip이 따로 본다).
    위험 수정 게이트: 아닙니다·아냐·아님(메모체)은 아니다와 같은 부정, '않 된다'(맞춤법 오류)는 '안 된다'로 센다."""
    t = nfc(text or ""); joined = t + (" " + nfc(follow) if follow else "")
    for a_, b_, _x, _f, st in sorted(_merely_frames(joined, limit=len(t)), reverse=True):
        if st != "hedged":
            t = t[:a_] + " " + t[min(b_, len(t)):]
    t = STYLE_NEG.sub(" ", t)
    t = re.sub(r"아닙|아냐|아녜|아뇨|아님(?![가-힣])", "아니", t)
    t = re.sub(r"없이(?![가-힣])", "이", t)  # '없이'(개입 없이·빠짐없이·끊임없이)는 방식을 말하는 부사라 부정으로 세지 않는다
    t = re.sub(r"않\s?(?=돼|되|된|될|됨|됐)", "안 ", t)  # 않 돼·않 된다(맞춤법 오류) = 안 돼·안 된다
    t = re.sub(r"않", "안 ", t)  # 긴 부정(-지 않다)과 짧은 부정(안)은 같은 부정이다(못·없은 따로 센다)
    t = re.sub(r"불가능(?:하|한|해|합)?|불가(?:하|한|해|합)?", "수 없", t)
    t = re.sub(r"(?<!불)가능(?:하|한|해|합)?", "수 있", t)
    return re.sub(r"[ \t]{2,}", " ", t)


def guards(text):
    c = Counter()
    text = _guard_norm(text)
    for name, pat in GUARD_RE:
        for mo in pat.finditer(text):
            c[(name, mo.group(0).strip())] += 1
    return c


def _bigrams(s):
    t = re.sub(r"[\s\W_]+", "", s)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


ALIGN_MOVES = ((1, 0), (0, 1), (1, 1), (2, 1), (1, 2), (3, 1), (1, 3), (4, 1))
GATE_MOVES = ALIGN_MOVES + ((1, 4), (2, 2), (2, 3), (3, 2))  # 게이트 묶음: 한 문장을 넷으로 나누거나 두세 문장을 서로 섞은 편집도 한 묶음으로


def align(src, out, moves=ALIGN_MOVES):
    """문장 짝짓기: [(원문 번호들, 수정문 번호들)] — 1:1, 합침(k:1), 나눔(1:k), 지움, 새로 씀."""
    n, m = len(src), len(out)
    bs, bo = [_bigrams(s) for s in src], [_bigrams(s) for s in out]
    cache = {}

    def gsim(i0, i1, j0, j1):
        key = (i0, i1, j0, j1)
        if key not in cache:
            a, b = set().union(*bs[i0:i1]), set().union(*bo[j0:j1])
            cache[key] = 2 * len(a & b) / (len(a) + len(b)) if (a or b) else 1.0
        return cache[key]

    PEN, NEG = 0.3, -1e18
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    band = max(20, abs(n - m) + 20)
    for i in range(n + 1):
        center = i * m / n if n else 0
        for j in range(max(0, int(center - band)), min(m, int(center + band)) + 1):
            if dp[i][j] == NEG:
                continue
            for di, dj in moves:
                ni, nj = i + di, j + dj
                if ni > n or nj > m:
                    continue
                sc = dp[i][j] + (gsim(i, ni, j, nj) - PEN if di and dj else 0.0)
                if sc > dp[ni][nj]:
                    dp[ni][nj], back[ni][nj] = sc, (i, j)
    pairs, i, j = [], n, m
    while (i, j) != (0, 0):
        pi_, pj = back[i][j]
        pairs.append((list(range(pi_, i)), list(range(pj, j))))
        i, j = pi_, pj
    return pairs[::-1]


def change_rate(src_text, out_text):
    a, b = nfc(src_text).split(), nfc(out_text).split()
    sm = SequenceMatcher(None, a, b, autojunk=False)
    same = sum(bl.size for bl in sm.get_matching_blocks())
    return 1 - same / max(1, len(a))


def _fmt_items(counter):
    return ", ".join("%s %s%s" % (k, v, "×%d" % c if c > 1 else "") for (k, v), c in sorted(counter.items()))


DIALOGUE_GENRES = ("소설", "웹소설")  # 따옴표 안을 대사로 보는 장르(기본 편집 범위). 그 밖의 장르는 실제 인용으로 보호


QUOTE_SPAN_RE = re.compile(r"“[^”\n]*”|\"[^\"\n]*\"|「[^」\n]*」|『[^』\n]*』|‘[^’\n]*’")


def verbatim_units(text, phrase):
    """보호 문구의 등장마다 비교할 단위: 따옴표 안이면 그 따옴표 안 전체(덧붙인 말도 잡는다),
    밖이면 문구부터 그 문장 끝까지. 따옴표 모양(“”·"")만 바뀐 것은 같은 것으로 본다."""
    t, ph = nfc(text), nfc(phrase)
    spans = [(m.start(), m.end()) for m in QUOTE_SPAN_RE.finditer(t)]
    out, start = [], 0
    while ph:
        i = t.find(ph, start)
        if i < 0:
            break
        span = next(((a, b) for a, b in spans if a <= i and i + len(ph) <= b), None)
        if span:
            out.append(t[span[0] + 1:span[1] - 1].strip())
        else:
            m = re.search(r"[.!?…\n]", t[i + len(ph) - 1:])
            end = (i + len(ph) - 1 + m.end()) if m else len(t)
            out.append(t[i:end].strip())
        start = i + len(ph)
    return out


def compare(src_text, out_text, genre="일반", terms=(), dialogue="auto", protected_text=()):
    S, O = Doc(src_text), Doc(out_text)
    src, out = S.flat, O.flat
    pairs = align([s for _p, s in src], [s for _p, s in out])
    quotes_on = dialogue == "protect" or (dialogue != "edit" and genre not in DIALOGUE_GENRES)
    pitems = lambda t: protected_items(t, quotes=quotes_on)
    g_src, g_out = pitems(src_text), pitems(out_text)
    settle_weak(g_src, g_out)
    protected, pair_notes, deleted, inserted = [], [], [], []
    kinds = Counter()
    # 순서를 옮긴 문장: 짝 없이 남은 '지움'과 '새로 씀'을 비슷한 것끼리 다시 짝짓는다.
    dels = [si for si, oi in pairs if si and not oi]
    inss = [oi for si, oi in pairs if oi and not si]
    used, moved = set(), {}
    for oi in inss:
        bo = set().union(*(_bigrams(out[k][1]) for k in oi))
        best, bsim = None, 0.0
        for d, si in enumerate(dels):
            if d in used:
                continue
            bs = set().union(*(_bigrams(src[k][1]) for k in si))
            sim = 2 * len(bs & bo) / (len(bs) + len(bo)) if (bs or bo) else 0.0
            if sim > bsim:
                best, bsim = d, sim
        if best is not None and bsim >= 0.4:
            used.add(best)
            moved[tuple(oi)] = dels[best]
    moved_src = {tuple(si) for si in moved.values()}
    units = []
    for si, oi in pairs:
        if si and not oi and tuple(si) in moved_src:
            continue
        if oi and not si and tuple(oi) in moved:
            units.append((moved[tuple(oi)], oi, True))
        else:
            units.append((si, oi, False))

    def loc_s(ix):
        return "원문 %d문단 \"%s\"" % (src[ix[0]][0] + 1, snip(" ".join(src[k][1] for k in ix), 40))

    def loc_o(ix):
        return "수정문 %d문단 \"%s\"" % (out[ix[0]][0] + 1, snip(" ".join(out[k][1] for k in ix), 40))

    unflagged = []  # 코드가 어떤 신호도 내지 못한 내용 변경(인과·순서 뒤집힘 등): 모른다고 모델에게 넘긴다
    content = lambda x: re.sub(r"[\W_]", "", nfc(x))  # 띄어쓰기·문장부호만 고친 것은 내용 변경이 아니다
    for si, oi, was_moved in units:
        if si and oi:
            a_txt, b_txt = " ".join(src[k][1] for k in si), " ".join(out[k][1] for k in oi)
            n_sig0 = len(protected) + len(pair_notes)
            kind = "옮김" if was_moved else "그대로" if nfc(a_txt).split() == nfc(b_txt).split() else (
                "고침" if len(si) == 1 and len(oi) == 1 else ("합침" if len(si) > 1 else "나눔"))
            kinds[kind] += 1
            pa, pb = pitems(S.raw(si[0], si[-1] + 1)), pitems(O.raw(oi[0], oi[-1] + 1))  # 원문 구간(목록 번호가 줄 첫머리인지 본다)
            settle_weak(pa, pb)
            lost, gained = pa - pb, pb - pa
            _cancel_units(lost, gained)  # 단위만 밝힌 것(서울 10 → 서울 10곳)은 값이 바뀐 것이 아니다(게이트와 같은 기준)
            moved = lambda x: g_src[x] == g_out[x]  # 문서 전체로는 같은 수: 값이 다른 문장으로 옮겨 갔을 뿐이다(제목 줄로 옮긴 주문번호·날짜 등)
            lost, gained = Counter({x: c for x, c in lost.items() if not moved(x)}), Counter({x: c for x, c in gained.items() if not moved(x)})
            lost_k, gained_k = {k for k, _v in lost}, {k for k, _v in gained}
            for k in lost_k & gained_k:
                protected.append("[값 바뀜] %s → %s: %s → %s" % (loc_s(si), loc_o(oi),
                                 _fmt_items(Counter({x: c for x, c in lost.items() if x[0] == k})),
                                 _fmt_items(Counter({x: c for x, c in gained.items() if x[0] == k}))))
            ga, gb = guards(_respace(a_txt, b_txt)), guards(b_txt)  # 띄어쓰기만 다른 곳은 같은 글로 센다(안남았다 → 안 남았다)
            PRESENCE = ("진행·결과 상태", "의도·시도")  # 모양(했·하, 겨있·려있)이 아니라 있고 없음을 본다
            sp = lambda c: Counter({(k, "(있음)" if k in PRESENCE else re.sub(r"\s", "", t)): v for (k, t), v in c.items()})
            ga, gb = sp(ga), sp(gb)  # 띄어쓰기만 다른 같은 표현(하고있는 ↔ 하고 있는)은 변화가 아니다
            gl, gg = ga - gb, gb - ga
            ta, tb = (tense_of(a_txt), tense_of(b_txt)) if len(si) == 1 and len(oi) == 1 else (None, None)
            ra, rb = role_map(a_txt), role_map(b_txt)
            roles_ = ["역할: '%s' %s → %s" % (n, ra[n][0], rb[n][0]) for n in sorted(set(ra) & set(rb))
                      if len(ra[n]) == 1 and len(rb[n]) == 1 and ra[n][0] != rb[n][0]]
            seen_ = {n for n in ra if any("'%s'" % n in x for x in roles_)}
            by_a, by_b = defaultdict(set), defaultdict(set)
            for n, rs_ in ra.items():
                for r_ in rs_:
                    by_a[r_].add(n)
            for n, rs_ in rb.items():
                for r_ in rs_:
                    by_b[r_].add(n)
            na_txt, nb_txt = re.sub(r"\s", "", nfc(a_txt)), re.sub(r"\s", "", nfc(b_txt))
            for r_ in sorted(set(by_a) & set(by_b)):  # 같은 자리(대상·비교 기준·받는 이 등)에 다른 명사가 왔고, 두 명사가 앞뒤 문장에 모두 있으면 자리만 바뀐 것
                if len(by_a[r_]) == 1 and len(by_b[r_]) == 1 and by_a[r_] != by_b[r_]:
                    x_, y_ = next(iter(by_a[r_])), next(iter(by_b[r_]))
                    if x_ in PRONOUNS and y_ in PRONOUNS and not ({x_, y_} & seen_):
                        roles_.append("역할 자리: %s 대명사 '%s' → '%s'" % (r_, x_, y_))  # 그의 책 → 그녀의 책
                    elif x_ in nb_txt and y_ in na_txt and not ({x_, y_} & seen_) and x_ not in y_ and y_ not in x_:  # 울음 소리 → 울음소리는 같은 것
                        roles_.append("역할 자리: %s '%s' → '%s'" % (r_, x_, y_))
            la_, lb_ = limit_particles(a_txt), limit_particles(b_txt)
            for n in sorted(set(la_) & set(lb_)):  # 한정·첨가 조사가 한쪽에라도 있고 바뀌었으면(철수만 → 철수도, 나는 → 나도)
                if la_[n] != lb_[n] and len(la_[n]) == 1 and len(lb_[n]) == 1 and (la_[n][0] in LIMIT_PARTICLES or lb_[n][0] in LIMIT_PARTICLES):
                    roles_.append("조사: '%s' %s → %s" % (n, la_[n][0], lb_[n][0]))
            num_a = sorted(k for k in pa.elements() if k[0] == "숫자")
            if num_a and num_a == sorted(k for k in pb.elements() if k[0] == "숫자"):  # 숫자는 그대로인데 숫자와 명사의 차례가 바뀌었으면
                an_a, an_b = number_anchors(a_txt), number_anchors(b_txt)
                if an_a != an_b and sorted(an_a) == sorted(an_b):
                    roles_.append("숫자 짝·차례: %s → %s (숫자가 가리키는 대상·순서가 바뀌지 않았는지)" % ("·".join(an_a), "·".join(an_b)))
            sa_, sb_ = role_seq(a_txt), role_seq(b_txt)
            for r_ in sorted(set(sa_) & set(sb_)):  # 같은 역할에 명사가 여럿이고 집합은 같은데 차례가 바뀌면(영희를 밀고 민수를 → 민수를 밀고 영희를)
                if len(sa_[r_]) >= 2 and sorted(sa_[r_]) == sorted(sb_[r_]) and sa_[r_] != sb_[r_]:
                    roles_.append("역할 순서: %s %s → %s" % (r_, "·".join(sa_[r_]), "·".join(sb_[r_])))
            oa, ob = order_nouns(a_txt), order_nouns(b_txt)
            order_ = (["순서: 순서 표지 뒤 차례 %s → %s" % ("·".join(oa), "·".join(ob))]
                      if len(oa) >= 2 and oa != ob and sorted(oa) == sorted(ob) else [])
            if gl or gg or (ta and tb and ta != tb) or roles_ or order_:
                cats = sorted({c for c, _t in gl} | {c for c, _t in gg})
                desc = (["시제(문장 끝): %s → %s" % (ta, tb)] if (ta and tb and ta != tb) else []) + roles_ + order_
                for c in cats:
                    ls = [t for (cc, t) in gl if cc == c]
                    gs = [t for (cc, t) in gg if cc == c]
                    desc.append("%s: %s%s" % (c, ("빠짐 " + "·".join("'%s'" % t for t in ls)) if ls else "",
                                              (" / 생김 " + "·".join("'%s'" % t for t in gs)) if gs else ""))
                pair_notes.append("%s → %s — %s" % (loc_s(si), loc_o(oi), "; ".join(desc)))
            if len(protected) + len(pair_notes) == n_sig0 and content(a_txt) != content(b_txt):
                unflagged.append((si, oi, a_txt, b_txt))
        elif si:
            kinds["지움"] += 1
            carried = Counter({k: v for k, v in pitems(S.raw(si[0], si[-1] + 1)).items() if k[0] != "수량?"})  # 약한 수량(이건·이번)은 알리지 않는다
            flag = [c for c, _t in guards(" ".join(src[k][1] for k in si))]
            deleted.append("%s%s%s" % (loc_s(si), (" [보호 항목: %s]" % _fmt_items(carried)) if carried else "",
                                        (" [의미 경계 표현: %s]" % "·".join(sorted(set(flag)))) if flag else ""))
        else:
            kinds["새로 씀"] += 1
            new_items = Counter({k: v for k, v in pitems(O.raw(oi[0], oi[-1] + 1)).items() if k[0] != "수량?"})
            inserted.append("%s%s" % (loc_o(oi), (" [보호 항목: %s]" % _fmt_items(new_items)) if new_items else ""))

    latin_notes = []  # 숫자 없는 로마자 낱말(TV·AI·YouTube)이 한쪽에만 있으면 한글 표기와 바꾼 것일 수 있어 읽고 판단으로
    letters_only = lambda it: it[0] == "식별자" and not re.search(r"\d", it[1])
    count_notes = []
    d_dec, d_inc = g_src - g_out, g_out - g_src
    _cancel_units(d_dec, d_inc)  # 단위만 밝히거나 뺀 수(10 → 10곳)는 사라지거나 생긴 사실이 아니다(게이트와 같은 기준). 원문에 이미 있던 '3곳'이 늘어난 것과도 짝짓는다
    d_lost = Counter({k: v for k, v in d_dec.items() if g_out[k] == 0})
    d_gain = Counter({k: v for k, v in d_inc.items() if g_src[k] == 0})
    for item in sorted(d_lost):
        m_ = re.fullmatch(r"([2-9])(가지|번째|째)", item[1]) if item[0] == "숫자" else None
        if m_ and not any(g[0] == "숫자" and re.fullmatch(r"\d+" + m_.group(2), g[1]) for g in d_gain) and (
                _count_announced(S.text, int(m_.group(1))) if m_.group(2) == "가지" else _ordinal_enum(S.text, int(m_.group(1)))):
            count_notes.append("[가짓수·목록 서수 사라짐] %s (나열한 항목은 원문에 있다: 항목이 모두 남았는지 확인)" % item[1])
            continue
        (latin_notes if letters_only(item) else protected).append("[사라짐] %s %s (원문 %d회 → 수정문 0회)" % (item[0], item[1], g_src[item]))
    for item in sorted(d_gain):
        (latin_notes if letters_only(item) else protected).append("[새로 생김] %s %s (원문에 없음: 새 사실을 만들지 않았는지 확인)" % item)
    counts = []
    for item in sorted(set(g_src) | set(g_out)):
        a, b = g_src[item], g_out[item]
        if a != b and a and b:
            counts.append("%s %s: %d회 → %d회" % (item[0], item[1], a, b))
    for term in terms:
        a, b = nfc(src_text).count(term), nfc(out_text).count(term)
        if a and not b:
            protected.append("[사라짐] 스타일 시트 용어 '%s' (원문 %d회)" % (term, a))
        elif a != b:
            counts.append("용어 '%s': %d회 → %d회" % (term, a, b))
    pt_missing = []
    for ph in protected_text:  # 글자 그대로 지킬 문구(실제 인용·승인 문구): 등장마다 따옴표 안 전체 또는 문장 끝까지 비교
        ph = nfc(ph).strip().strip("“”\"「」『』‘’").strip()
        if not ph:
            continue
        us, uo = Counter(verbatim_units(src_text, ph)), Counter(verbatim_units(out_text, ph))
        if not us:  # 원문에 없는 문구는 보호되지 않는다: 조용히 넘기지 않고 알린다
            pt_missing.append("[보호 문구를 원문에서 찾지 못함] '%s' — 목록의 글자가 원문과 같은지(띄어쓰기·문장부호) 확인한다. 이 문구는 보호되지 않았다" % snip(ph, 50))
            continue
        if sum(us.values()) >= 2:  # 같은 문구가 여러 곳이면 대사 같은 편집 자리까지 묶였을 수 있다: 어느 쪽이 인용인지는 모델이 판단한다
            pt_missing.append("[보호 문구가 %d곳에 등장함] '%s' — 모두 실제 보호 대상인지 확인한다. 대사처럼 고쳐도 되는 자리가 섞였으면 "
                              "보호 문구를 앞말까지 넣어 더 길게 적는다(예: `기사에는 “나는 자유롭다.”`). 그러면 그 자리만 보호한다" % (sum(us.values()), snip(ph, 40)))
        lost_ = us - uo
        if lost_:
            protected.append("[보호 문구 바뀜] '%s' — 원문 %d곳 가운데 %d곳이 글자 그대로 남지 않았다(바뀜·덧붙임·삭제): %s"
                             % (snip(ph, 40), sum(us.values()), sum(lost_.values()), " / ".join(snip(x, 50) for x in list(lost_)[:2])))
    fiction = genre in ("소설", "웹소설", "에세이")
    n_src, n_out = name_candidates(nfc(src_text), fiction), name_candidates(nfc(out_text), fiction)

    def n_count(t, n):
        return len(re.findall(r"(?<![가-힣])" + re.escape(n), nfc(t)))

    # 여기부터는 도구의 추측이다: 막지 않고(종료 코드 1) 모델이 읽고 판단할 목록으로 보인다
    review = []
    for n in sorted(n_src):
        if n in terms:
            continue
        a, b = n_count(src_text, n), n_count(out_text, n)
        if a and b < a:
            review.append("[이름 후보 줄어듦] '%s' %d회 → %d회 (직함 앞·인물 판별로 뽑은 후보. 이름이 바뀐 것이면 되돌린다)" % (n, a, b))
    for n in sorted(set(n_out) - set(n_src)):
        if n_count(src_text, n) == 0:
            review.append("[새 이름 후보] '%s' (원문에 없음: 이름을 바꾸거나 만들지 않았는지 본다)" % n)
    review += [x + " (숫자 없는 로마자 낱말: 한글 표기와 바꾼 것이면 두고, 다른 것으로 바꿨으면 되돌린다)" for x in latin_notes]
    review += count_notes
    review += pt_missing
    review += one_letter_changes(src_text, out_text)
    review += list_item_losses(src_text, out_text)
    advice = []
    if fiction and not terms:
        advice.append("이름 목록(--terms) 없이 대조했다. 짧은 소설은 이름 후보를 자동으로 다 찾지 못하니, 확정한 인물·지명 목록을 --terms로 넘긴다.")

    rate = change_rate(src_text, out_text)
    rs, ro = register_counts(S), register_counts(O)
    reg_note = None
    if rs and ro and rs.most_common(1)[0][0] != ro.most_common(1)[0][0]:
        reg_note = "주 문체가 바뀜: %s → %s (원문 높임 수준 유지가 원칙)" % (rs.most_common(1)[0][0], ro.most_common(1)[0][0])
    out_findings = scan(out_text, genre)["findings"]
    residue = [f for f in out_findings if f["count"] and (f["role"] == "기계" or f["group"] in "RU")]
    # 수정본에 새로 생긴 형태: 원문보다 늘어난 형태와 새 자리(1차 수정이 만든 공식을 잡는다). 사실만 보인다
    src_map = {f["id"]: f for f in scan(src_text, genre)["findings"]}
    new_forms = []
    for f in out_findings:
        if not f["count"]:
            continue
        s_ = src_map.get(f["id"])
        a = s_["count"] if s_ else 0
        old = [h["text"] for h in (s_["hits"] if s_ else [])]

        def same_spot(t):  # 원문 자리와 글자가 60% 이상 겹치면(띄어쓰기만 고친 것 등) 같은 자리로 본다
            bt = _bigrams(t)
            return t in old or any(len(bt & _bigrams(u)) / max(1, len(bt | _bigrams(u))) >= 0.6 for u in old)
        fresh = [h["text"] for h in f["hits"] if not same_spot(h["text"])]
        if f["count"] > a or fresh:  # 늘었거나, 개수는 같아도 다른 표현으로 바꿔치기한 새 자리가 있으면
            new_forms.append("%s %s: 원문 %d곳 → 수정본 %d곳%s" % (f["id"], f["name"], a, f["count"],
                             (" (새 자리: %s)" % " / ".join(snip(x, 50) for x in fresh[:2])) if fresh else ""))
    blocks_ = block_propositions(src_text, out_text)
    covered = " ".join(b["src"] + " " + b["out"] for b in blocks_)
    rv_txt = " ".join(review)
    for si, oi, a_txt, b_txt in unflagged:  # 블록 대조·읽고 판단 어디에도 걸리지 않은 짝만
        words_ = set(re.findall(r"[가-힣A-Za-z0-9]+", a_txt)) ^ set(re.findall(r"[가-힣A-Za-z0-9]+", b_txt))
        if nfc(a_txt).strip() in covered or any(w_ in rv_txt for w_ in words_ if len(w_) >= 2):
            continue
        (ka, da), (kb, db) = _content_seq(a_txt), _content_seq(b_txt)
        if ka == kb and (set(da) <= set(db) or set(db) <= set(da)):
            continue  # 주어 대명사·'그리고'가 빠지거나 더해지기만 했다. 서로 바뀐 것(그는 → 그녀는)과 다른 말의 변화는 올린다
        review.append("[무경고 내용 변경] %s → %s — 코드가 분류하지 못한 변경: 의미·인과·순서가 바뀌지 않았는지 읽어서 확인" % (loc_s(si), loc_o(oi)))
    return {
        "length": [len(nfc(src_text).strip()), len(nfc(out_text).strip())], "quotes_protected": quotes_on,
        "change_rate": round(rate, 3),
        "sentences": dict(kinds), "protected": protected, "review": review, "count_changes": counts, "advice": advice,
        "pair_notes": pair_notes, "deleted": deleted, "inserted": inserted,
        "register": {"source": dict(rs), "rewrite": dict(ro), "note": reg_note},
        "new_forms": new_forms, "shape": [sentence_shape(src_text), sentence_shape(out_text)],
        "blocks": blocks_,
        "residue": ["%s %s(%d곳)" % (f["id"], f["name"], f["count"]) for f in residue],
    }


def print_compare(r):
    for a in r.get("advice", []):
        print("※ " + a)
    sl, rl = r["length"]
    k = r["sentences"]
    print("# 원문·수정문 대조 (표면 검사: 의미 충실도를 증명하지 않는다)")
    line = "분량 %d자 → %d자 (%.2f) · 어절 변경률 %.0f%%" % (sl, rl, rl / max(1, sl), 100 * r["change_rate"])
    line += " · 따옴표 안: %s" % ("인용으로 보호" if r.get("quotes_protected", True) else "대사로 보고 편집 허용(숫자·부정·시제 등은 그대로 본다)")
    print(line)
    print("문장: " + " · ".join("%s %d" % (n, k.get(n, 0)) for n in ("그대로", "고침", "합침", "나눔", "옮김", "지움", "새로 씀")))
    if r.get("all_blocks") is not None:
        ws = r.get("review_with_source")
        print("\n## 수정 블록 전체 (--review-all, %d블록) — %s" % (len(r["all_blocks"]), "원문과 함께: 뜻이 보존됐는지 본다" if ws else
              "원문 없이 '수정'만 새 문장처럼 읽는다: 맞춤법·조사·호응·인용 어미(-고)·문장 접합. 다 읽은 뒤 --with-source로 뜻을 대조한다"))
        for b in r["all_blocks"]:
            print("- [%s] 수정: “%s”\n    앞: “%s” / 뒤: “%s”%s" % (b["where"], b["out"] or "(지움)", snip(b["before"], 60), snip(b["after"], 60),
                                                               ("\n    원문: “%s”" % snip(b["src"], 120)) if ws else ""))
    sections = [
        ("1. 보호 항목 — 반드시 확인 (사실: 숫자·URL·코드·인용·확정한 용어가 바뀜)", r["protected"]),
        ("1-2. 읽고 판단 — 이름·낱말·미분류 내용 변경 (기계적으로 확정하지 못한 변경 후보: 원문과 대조해 판단)", r.get("review", [])),
        ("2. 횟수만 바뀐 보호 항목 (지우거나 합친 문장 때문인지 확인)", r["count_changes"]),
        ("3. 문장별 의미 경계 표현 변화 (부정·범위·양태·의무·시간·귀속·인과)", r["pair_notes"]),
        ("3-2. 수정 블록별 명제 대조 — 사라진 고유 정보·새 정보·행위자·말하는 동사 (읽고 판단: 블록마다 되돌리거나, 살리거나, 원문 근거를 확인)"
         + block_summary(r.get("blocks", [])),
         ["%s [%s]%s%s%s%s — 원문 “%s” → 수정 “%s”" % (b["where"], b["kind"], (" 사라진 말: " + ", ".join(b["lost"][:6])) if b["lost"] else "",
                                                 (" · 새 말: " + ", ".join(b["new"][:6])) if b["new"] else "",
                                                 (" · 바뀐 낱말: " + ", ".join(b.get("swapped", [])[:6])) if b.get("swapped") else "",
                                                 (" · " + " / ".join(b["notes"])) if b["notes"] else "", snip(b["src"], 60), snip(b["out"], 60))
          for b in r.get("blocks", [])[:60]] + (["(외 %d블록: --json으로 전체)" % (len(r["blocks"]) - 60)] if len(r.get("blocks", [])) > 60 else [])),
        ("4. 지운 문장 — 명제가 정말 필요 없었는지", r["deleted"]),
        ("5. 새로 쓴 문장 — 원문 근거 확인", r["inserted"]),
    ]
    for title, items in sections:
        print("\n## %s (%d건)" % (title, len(items)))
        for x in items:
            print("- " + x)
    if r["register"]["note"]:
        print("\n## 문체\n- " + r["register"]["note"])
    if r.get("new_forms"):
        print("\n## 수정본에 새로 생기거나 바꿔 쓴 형태 — 1차 수정이 만든 공식인지 읽고 판단 (rewriting.md 6절)\n- " + "\n- ".join(r["new_forms"]))
    a_, b_ = r.get("shape", [None, None])
    if a_ and b_ and a_["n"] >= 5 and b_["n"] >= 5 and a_["cv"] is not None and b_["cv"] is not None:
        top = max(a_["ends"], key=a_["ends"].get) if a_["ends"] else None
        share = lambda sh: 100 * sh["ends"].get(top, 0) / max(1, sum(sh["ends"].values()))
        print("\n문장 모양(서술문, 판정 아님): 문장 %d → %d · 길이 변동계수 %.2f → %.2f%s — 수정본이 크게 평평해졌으면 원문 리듬이 기능하던 곳을 되살린다"
              % (a_["n"], b_["n"], a_["cv"], b_["cv"], (" · 원문에서 가장 흔한 끝 '%s' %.0f%% → %.0f%%" % (top, share(a_), share(b_))) if top else ""))
    if r["residue"]:
        print("\n## 수정문에 남은 형태(보이지 않는 문자·챗봇 잔재·질문/대비 틀) — 남긴 이유가 있는지 확인\n- " + ", ".join(r["residue"]))
    code_ = exit_code(r)
    print("\n" + {2: "종료 코드 2 — 기계적으로 확인된 사실 변화가 있다: 보호 항목을 되돌리거나, 바꾼 근거를 사용자에게 알린다.",
                  1: "종료 코드 1 — 읽고 판단할 항목이 있다(편집 실패가 아니다). 항목마다 한 번 판단해 되돌리거나 두고 이유를 남긴다. 새로 고친 곳이 없으면 다시 돌리지 않는다.",
                  0: "종료 코드 0 — 기계 검사에서 추가 신호가 없다. 뜻이 보존됐다는 증명은 아니니 수정문은 직접 다시 읽는다."}[code_])


def exit_code(r):
    if r["protected"]:
        return 2
    if r.get("blocks") or r.get("new_forms") or r.get("review") or r["count_changes"] or r["pair_notes"] or r["deleted"] or r["inserted"] or r["register"]["note"]:
        return 1
    return 0


# ================================================================ selftest / rules


def _unsupported_ok():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bad = [Path(td, "a.hwp"), Path(td, "b.txt")]
        bad[0].write_bytes(b"HWP Document File")
        bad[1].write_bytes(b"%PDF-1.4\x00")
        u16 = Path(td, "c.txt")
        u16.write_bytes("할수 있다.".encode("utf-16"))
        blocked = 0
        for b in bad:
            try:
                read_text_any(b)
            except ValueError:
                blocked += 1
        return blocked == 2 and read_text_any(u16) == "할수 있다."


def selftest():
    fails, total = [], 0
    for r in RULES:
        if not r.pos or not r.neg:
            fails.append((r.rid, "예시 없음", ""))
        for ex in r.pos:
            total += 1
            live = [h for h in r.run(Doc(ex)) if not h[2]]
            if len(live) < r.min_count:
                fails.append((r.rid, "놓침", ex))
        for ex in r.neg:
            total += 1
            live = [h for h in r.run(Doc(ex)) if not h[2]]
            if len(live) >= r.min_count:
                fails.append((r.rid, "오탐", "%s → %s" % (ex, live[0][1])))
    print("규칙 %d개, 예시 %d건 시험: 통과 %d, 실패 %d" % (len(RULES), total, total - len(fails), len(fails)))
    for rid, kind, ex in fails:
        print("  - %s %s: %s" % (rid, kind, ex))
    # 파이프라인 시험: 규칙 예문으로는 막을 수 없던 실제 결함(문장 분리·문체 판별·대조)이 되살아나지 않게
    def split_ok():
        d = Doc("“난 가지 않겠어.” 그는 거절했다.")
        return [d.quoted_sent(p, x) for p, x in d.flat] == [True, False]
    pipe = [("문체: 아니다·다니다·지니다는 한다체",
             lambda: [register_of(x) for x in ("그는 학생이 아니다.", "나는 길을 다닌다.", "같은 기억을 지닌다.")] == ["한다체"] * 3),
            ("문체: 합니다·아닙니다는 합니다체", lambda: [register_of(x) for x in ("그렇게 합니다.", "그건 아닙니다.")] == ["합니다체"] * 2),
            ("문장 분리: 같은 줄 대사와 서술", split_ok),
            ("대조: 이름 후보 변경은 읽고 판단 목록(김민수 → 김민석)", lambda: any("김민수" in x for x in compare("김민수 연구원은 발표했다.", "김민석 연구원은 발표했다.")["review"])),
            ("대조: 짧은 소설 이름 오타는 읽고 판단 목록(세린 → 세릭)", lambda: any("세린" in x for x in compare("세린은 문을 열었다. 유진이 봤다.", "세릭은 문을 열었다. 유진이 봤다.", genre="웹소설")["review"])),
            ("대조: 확정한 이름(--terms)이 바뀌면 보호 항목", lambda: bool(compare("세린은 웃었다.", "세릭은 웃었다.", genre="웹소설", terms=["세린"])["protected"])),
            ("대조: 맞춤법 교정(갯수 → 개수, 컨텐츠 → 콘텐츠)은 막지 않고 '바뀐 낱말'로 사실만 보인다(판단은 모델)", lambda: all(
                not (lambda r: r["protected"] or exit_code(r) == 2)(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b))
                and (lambda r: any(b_["swapped"] for b_ in r["blocks"]) or any("한 글자 바뀐 낱말" in x for x in r["review"]))(
                    compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b))
                for a, b in (("갯수가 많다.", "개수가 많다."), ("컨텐츠를 만든다.", "콘텐츠를 만든다.")))),
            ("속마음 ‘…’은 서술로 세지 않음", lambda: [Doc("‘괜찮아요.’ 나는 웃었다.").quoted_sent(p, x) for p, x in Doc("‘괜찮아요.’ 나는 웃었다.").flat] == [True, False]),
            ("글이 아닌 파일(.hwp·이진)은 읽지 않음", lambda: _unsupported_ok()),
            ("대조: 수정본에 새로 생긴 유보 표현을 알림",
             lambda: any(x.startswith("L6") for x in compare("성능이 좋다. 가격은 3만 원이다.", "성능이 좋다. 가격은 3만 원이다. 아직 판단하기에는 이르다.")["new_forms"])),
            ("대조: 유보 표현을 다른 유보 표현으로 바꿔치기하면 알림(개수가 같아도)",
             lambda: any(x.startswith("L6") for x in compare("성능이 좋다. 아직 판단하기에는 이르다.", "성능이 좋다. 결국 직접 확인해 볼 필요가 있다.")["new_forms"])),
            ("대조: 띄어쓰기만 고친 같은 자리는 새 형태가 아님",
             lambda: not compare("그는아직 판단하기에는 이르다고 말했다. 성능은 좋다.", "그는 아직 판단하기에는 이르다고 말했다. 성능은 좋다.")["new_forms"]),
            ("대조: 원문에 있던 형태는 새로 생긴 것으로 보지 않음",
             lambda: not compare("아직 판단하기에는 이르다. 성능은 좋다.", "아직 판단하기에는 이르다. 성능이 좋다.")["new_forms"]),
            ("조사 떼기: '집으로 → 집은'은 한 글자 바뀐 낱말이 아님", lambda: not compare("집으로 간다. 그 길은 멀다.", "집은 멀다. 그 길은 멀다.")["review"]),
            ("조사 떼기: '사과는'은 '사과'", lambda: __import__("consistency").stem("사과는")[0] == "사과"),
            ("대조: 나열 항목이 사라지면 읽고 판단 목록(혁신·성장·도약 → 성장)",
             lambda: any("나열 항목 사라짐" in x for x in compare("이 도구는 혁신, 성장, 그리고 도약을 만든다.", "이 도구는 성장을 만든다.")["review"])),
            ("말투: 해체 명령형(기다려·보내·내려)은 반말", lambda: [speech_family(x) for x in ("도현, 기다려.", "편지 보내.", "여기서 내려.")] == ["반말"] * 3),
            ("대조: 변경률은 정보로만 보고 폐기된 편집 강도 예산은 판정하지 않음", lambda: (lambda r: "change_rate" in r and "mode" not in r and "budget" not in r and "over_budget" not in r)(compare("가를 보았다.", "가를 봤다."))),
            ("블록 대조: 실제 원고 시험의 실패 유형을 잡는다(나열 항목 삭제·대답했다·버려진·날·비를 맞으며)", lambda: all(
                any(nd in json.dumps(b, ensure_ascii=False) for b in compare(o_, r_)["blocks"]) for o_, r_, nd in REAL_MUST_FLAG)),
            ("블록 대조: 실제 원고의 좋은 수정은 고유 정보 사라짐·의미 경계로 올리지 않는다('나는' 삭제·띄어쓰기·카디건·나뭇가지·되풀이 삭제)", lambda: all(
                not any(b["lost"] or b["notes"] for b in compare(o_, r_)["blocks"]) for o_, r_ in REAL_MUST_PASS)),
            ("대조: 닫는 자리의 여는 따옴표를 고친 것은 인용 변화가 아님", lambda: not compare(
                "“음... 팀장님한테 먼저 물어볼게요.“\n\n신입 직원이 머리를 긁적였다.", "“음... 팀장님한테 먼저 물어볼게요.”\n\n신입 직원이 머리를 긁적였다.")["protected"]),
            ("대조: 강도 부사가 빠지면 의미 경계(매우 화가 났다 → 화가 났다)", lambda: any("강도" in x for x in compare("앞이다. 그는 매우 화가 났다. 뒤다.", "앞이다. 그는 화가 났다. 뒤다.")["pair_notes"])),
            ("대조: 시제가 바뀌면 의미 경계(갔다 → 간다, 할 것이다 → 했다)", lambda: all(any("시제" in x for x in compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)["pair_notes"])
                                                                   for a, b in (("그는 학교에 갔다.", "그는 학교에 간다."), ("그는 내일 그 일을 할 것이다.", "그는 내일 그 일을 했다.")))),
            ("대조: 시제가 같으면 올리지 않음(있다·했다 유지)", lambda: not any("시제" in x for x in compare("앞이다. 맛있는 밥이 있다. 그는 먹었다. 뒤다.", "앞이다. 맛있는 밥이 있다. 그는 밥을 먹었다. 뒤다.")["pair_notes"])),
            ("블록 대조: 앞이 같은 다른 명사(백혈구 → 백혈병, 도서관 → 도서실, 부산역 → 부산항)", lambda: all(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)["blocks"]
                for a, b in (("백혈구 수치가 낮았다.", "백혈병 수치가 낮았다."), ("그는 도서관에서 일했다.", "그는 도서실에서 일했다."), ("그는 부산역에서 일했다.", "그는 부산항에서 일했다.")))),
            ("--review-all: 떨어진 수정 블록을 모두(고침·지움) 보인다", lambda: [b["out"] for b in change_blocks(
                "가를 봤다. 나를 봤다. 다를 봤다. 라를 봤다.", "가를 봤다. 나를 보았다. 다를 봤다.")] == ["나를 보았다.", ""]),
            ("대조: 더·덜, 진행·결과 상태, 의도·시도, 할 겁니다가 바뀌면 의미 경계", lambda: all(fc_notes(a, b, k) for a, b, k in (
                ("이건 더 중요하다.", "이건 중요하다.", "강도"), ("이건 덜 위험하다.", "이건 위험하다.", "강도"),
                ("그는 달리고 있었다.", "그는 달렸다.", "진행·결과 상태"), ("문이 열려 있었다.", "문이 열렸다.", "진행·결과 상태"),
                ("그는 떠나려고 했다.", "그는 떠났다.", "의도·시도"), ("그는 내일 갈 겁니다.", "그는 내일 갑니다.", "시제")))),
            ("블록 대조: 한 글자 다른 낱말은 교정이든(가디건 → 카디건) 뜻 변화든(기사 → 기차, 의사 → 이사) '바뀐 낱말'로 보인다", lambda: all(
                any(b["swapped"] for b in compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)["blocks"])
                for a, b in (("그는 기사다.", "그는 기차다."), ("그는 의사다.", "그는 이사다."), ("검은 가디건을 입었다.", "검은 카디건을 입었다.")))),
            ("대조: 오탐 아님: 올려다봤다(의도·시도 아님), 하고있는 → 하고 있는(띄어쓰기만)", lambda: not fc_notes("그는 형을 올려다 봤다.", "그는 형을 올려다봤다.", "의도")
                and not fc_notes("청소를 하고있는 직원이 보였다.", "청소를 하고 있는 직원이 보였다.", "진행")),
            ("다른 낱말로 바꾸면 고유 정보 사라짐·새 말(컨텐츠 → 책, 가디건 → 코트): 교정표로 면제하지 않는다", lambda: all(
                any(b["lost"] and b["new"] for b in compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)["blocks"])
                for a, b in (("컨텐츠를 읽었다.", "책을 읽었다."), ("가디건을 입었다.", "코트를 입었다.")))),
            ("한글 수량: 두 명 → 세 명·한 번 → 두 번·첫 번째 → 두 번째는 보호 항목, 두 명 → 2명·한번 → 한 번은 같은 수", lambda:
                all(exit_code(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)) == 2 for a, b in (("두 명이 왔다.", "세 명이 왔다."), ("그는 한 번 웃었다.", "그는 두 번 웃었다."), ("첫 번째 문을 열었다.", "두 번째 문을 열었다.")))
                and not compare("앞이다. 두 명이 왔다. 뒤다.", "앞이다. 2명이 왔다. 뒤다.")["protected"] and not compare("앞이다. 숨을 한번 쉬었다. 뒤다.", "앞이다. 숨을 한 번 쉬었다. 뒤다.")["protected"]),
            ("역할·관계·순서가 뒤바뀌면 의미 경계(철수가 영희를 → 영희가 철수를, 서울에서 부산으로 → 부산에서 서울로, 영희보다, 먼저 A·B → B·A)", lambda: all(
                fc_notes(a, b, k) for a, b, k in (("철수가 영희를 때렸다.", "영희가 철수를 때렸다.", "역할"), ("그는 서울에서 부산으로 갔다.", "그는 부산에서 서울로 갔다.", "역할"),
                                                  ("철수가 영희보다 크다.", "영희가 철수보다 크다.", "역할"), ("먼저 철수를 만나고 영희를 만났다.", "먼저 영희를 만나고 철수를 만났다.", "순서"),
                                                  ("첫째 날은 서울, 둘째 날은 부산에 갔다.", "첫째 날은 부산, 둘째 날은 서울에 갔다.", "순서")))),
            ("대명사 관계 뒤집힘·대명사 바꿈: 그는 그녀를 → 그녀는 그를, 나는 너를 → 너는 나를, 그의 책 → 그녀의 책", lambda: all(fc_notes(a, b, "역할") for a, b in (
                ("그는 그녀를 사랑했다.", "그녀는 그를 사랑했다."), ("나는 너를 믿었다.", "너는 나를 믿었다."), ("우리가 그들을 이겼다.", "그들이 우리를 이겼다."), ("그의 책을 읽었다.", "그녀의 책을 읽었다.")))),
            ("날짜 표기·차례 표기만 다른 것은 같은 수(2026.9.25 = 2026년 9월 25일, 제3자 = 제삼자), 제2회 → 제3회는 보호 항목", lambda:
                not compare("앞이다. 2026.9.25에 만났다. 뒤다.", "앞이다. 2026년 9월 25일에 만났다. 뒤다.")["protected"] and not compare("앞이다. 제3자가 봤다. 뒤다.", "앞이다. 제삼자가 봤다. 뒤다.")["protected"]
                and exit_code(compare("앞이다. 제2회 대회였다. 뒤다.", "앞이다. 제3회 대회였다. 뒤다.")) == 2),
            ("역할 오탐 아님: 은/는으로만 바뀐 것(도구가 좋다 → 도구는 좋다), 에 ↔ 로(학교에 → 학교로)", lambda:
                not fc_notes("이 도구가 좋다.", "이 도구는 좋다.", "역할") and not fc_notes("그는 학교에 갔다.", "그는 학교로 갔다.", "역할")
                and not fc_notes("남자는 한 식당에 대해 알려주었다.", "남자는 한 식당을 알려주었다.", "역할")),
            ("관계 뒤집힘(은/는이 끼어도): 철수는 영희를 → 영희는 철수를, 영희보다, 영희에게", lambda: all(fc_notes(a, b, "역할") for a, b in (
                ("철수는 영희를 때렸다.", "영희는 철수를 때렸다."), ("철수는 영희보다 크다.", "영희는 철수보다 크다."), ("서울은 부산보다 크다.", "부산은 서울보다 크다."),
                ("철수는 영희에게 편지를 줬다.", "영희는 철수에게 편지를 줬다.")))),
            ("관계 뒤집힘 오탐 아님: 대상 낱말만 바꿈(책을 → 편지를), 어순만 바꿈(영희를 철수가 불렀다)", lambda:
                not fc_notes("철수는 책을 읽었다.", "철수는 편지를 읽었다.", "역할 자리") and not fc_notes("철수가 영희를 불렀다.", "영희를 철수가 불렀다.", "역할")
                and not fc_notes("창밖에서 빗 소리가 이어졌다.", "창밖에서 빗소리가 이어졌다.", "역할")
                and not fc_notes("여느 학생들이 그렇지만 민호에게 수학은 골칫거리다.", "수학은 여느 학생에게 골칫거리이지만, 민호에게는 특히 그렇다.", "역할 자리")),
            ("한자어 숫자: 자릿값이 든 수·달 이름은 값이 바뀌면 보호 항목, 표기만 다르면 같은 수", lambda:
                all(exit_code(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)) == 2 for a, b in (("십칠 분이 걸렸다.", "십팔 분이 걸렸다."), ("삼십만 원을 냈다.", "사십만 원을 냈다."), ("2026년 오월에 떠났다.", "2026년 유월에 떠났다.")))
                and all(not compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)["protected"] for a, b in (("삼십만 원을 냈다.", "30만 원을 냈다."), ("삼십만 원을 냈다.", "300,000원을 냈다."), ("오월에 떠났다.", "5월에 떠났다."), ("이천이십육 년이다.", "2026년이다.")))),
            ("숫자로 단정하지 않음: 붙여 쓴 말(백일장·만년필·세대·한편·한쪽·한밤중·사원·만일)과 애매한 달(이월 예산·일월은)은 강한 항목이 아니다", lambda:
                not [x for x in sino_counts("이 분이 오셨다. 이번에 백화점에 갔다. 천천히 십자가를 봤다. 사원과 구원을 일시에 만일 만세. 백일장과 만년필. 예산 이월은 크다. 일월은 해와 달이다.") if not x[2]]
                and not [x for x in korean_counts("세대 차이. 한편, 그는 한쪽 구석에서 한밤중에 한번 웃었다.") if not x[2]]),
            ("한 음절 한자어 수는 숫자로 단정하지 않음(오 분 → 오는 분은 막지 않음, 오 분 → 육 분은 막지 않되 보임), 띄어쓰기만 고친 것은 같은 수(삼분 → 삼 분)", lambda:
                not compare("앞이다. 오늘 오 분이 누구예요? 뒤다.", "앞이다. 오늘 오는 분이 누구예요? 뒤다.")["protected"]
                and exit_code(compare("앞이다. 오 분 걸렸다. 뒤다.", "앞이다. 육 분 걸렸다. 뒤다.")) == 1
                and not compare("앞이다. 오 분 걸렸다. 뒤다.", "앞이다. 5분 걸렸다. 뒤다.")["protected"]
                and not compare("앞이다. 삼분 뒤였다. 뒤다.", "앞이다. 삼 분 뒤였다. 뒤다.")["protected"]
                and not compare("앞이다. 누구하나 알아 볼 수 없었다. 뒤다.", "앞이다. 누구 하나 알아볼 수 없었다. 뒤다.")["protected"]
                and not compare("앞이다. 지어낸 말 일수도 있었다. 뒤다.", "앞이다. 지어낸 말일 수도 있었다. 뒤다.")["protected"]),
            ("숫자 오인 안 함(종료 코드 2 없음): 이월 예산·일월은·백일장·만년필·백일몽·천일야화·세대·한편·한쪽·한밤중을 다른 말로 고쳐도", lambda: all(
                not compare("앞 문장이다. %s 뒤 문장이다." % a, "앞 문장이다. %s 뒤 문장이다." % b)["protected"] for a, b in (
                    ("예산 이월은 다음 해로 넘긴다는 뜻이다.", "예산을 다음 해로 넘긴다는 뜻이다."), ("일월은 해와 달이다.", "해와 달이다."), ("백일장을 열었다.", "글짓기 대회를 열었다."),
                    ("만년필을 샀다.", "펜을 샀다."), ("백일몽을 꾸었다.", "헛된 꿈을 꾸었다."), ("천일야화를 읽었다.", "아라비안나이트를 읽었다."), ("세대 차이가 컸다.", "나이 차이가 컸다."),
                    ("한편, 그는 웃었다.", "그는 웃었다."), ("한쪽 구석에 앉았다.", "구석에 앉았다."), ("한밤중에 깼다.", "밤중에 깼다.")))),
            ("로마자 식별자: 한글 표기와 바꾼 것(티비 → TV, 유튜브 → YouTube)은 읽고 판단, 숫자가 든 것(A15 → A16)과 다른 로마자로 바꾼 것(KTX → SRT)은 보호 항목", lambda:
                all((lambda r: not r["protected"] and any("로마자" in x for x in r["review"]))(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b))
                    for a, b in (("티비를 봤다.", "TV를 봤다."), ("유튜브에 올렸다.", "YouTube에 올렸다."), ("TV를 봤다.", "텔레비전을 봤다.")))
                and all(exit_code(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)) == 2 for a, b in (("A15 칩이다.", "A16 칩이다."), ("KTX를 탔다.", "SRT를 탔다."), ("GPT-4를 썼다.", "GPT-5를 썼다.")))),
            ("부호는 값의 일부(-5% ≠ 5%, −5℃ ≠ 5℃, +5% ≠ 5%), 범위·전화번호의 붙임표는 부호가 아님(3-5명, 010-1234-5678)", lambda:
                all(exit_code(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)) == 2 for a, b in (("수익률은 -5%였다.", "수익률은 5%였다."), ("기온은 −5℃였다.", "기온은 5℃였다."),
                                                                                                 ("+5% 올랐다.", "5% 올랐다."), ("-3,000원이었다.", "3,000원이었다.")))
                and not compare("앞이다. 3-5명이 온다. 뒤다.", "앞이다. 3~5명이 온다. 뒤다.")["protected"] and not compare("앞이다. −5℃였다. 뒤다.", "앞이다. -5℃였다. 뒤다.")["protected"]
                and not compare("앞이다. 010-1234-5678로 연락. 뒤다.", "앞이다. 010-1234-5678로 연락해. 뒤다.")["protected"]),
            ("숫자는 그대로인데 숫자와 명사의 차례가 바뀌면 확인 후보(10살·20살 교환, 1:2 → 2:1, 순위와 이름 교환)", lambda: all(fc_notes(a, b, "숫자 짝") for a, b in (
                ("철수는 10살, 영희는 20살이다.", "철수는 20살, 영희는 10살이다."), ("A는 10점, B는 20점이다.", "A는 20점, B는 10점이다."),
                ("비율은 1:2다.", "비율은 2:1이다."), ("1위는 철수, 2위는 영희다.", "1위는 영희, 2위는 철수다.")))
                and not fc_notes("철수는 10살이다.", "철수는 10살이었다.", "숫자 짝")),
            ("소설·웹소설: 대사 교정·윤문은 인용 보호로 막지 않고(메세지 → 메시지, 생각을 해 → 생각해), 대사 안의 숫자는 막고 부정·시제는 올린다", lambda:
                all(not compare("앞이다.\n\n%s\n\n뒤다." % a, "앞이다.\n\n%s\n\n뒤다." % b, genre="소설")["protected"] for a, b in (
                    ("“메세지 보냈어?”", "“메시지 보냈어?”"), ("“할수 있어.”", "“할 수 있어.”"), ("“그건 아니라고 생각을 해.”", "“그건 아니라고 생각해.”")))
                and exit_code(compare("앞이다.\n\n“10명이 죽었어.”\n\n뒤다.", "앞이다.\n\n“20명이 죽었어.”\n\n뒤다.", genre="웹소설")) == 2
                and fc_notes_g("“난 안 갔어.”", "“난 갔어.”", "부정", "소설") and fc_notes_g("“그럼 나는 내일 거기로 갈 거야.”", "“그럼 나는 어제 거기로 갔어.”", "시제", "소설")
                and exit_code(compare("앞이다.\n\n“내일 갈 거야.”\n\n뒤다.", "앞이다.\n\n“어제 갔어.”\n\n뒤다.", genre="소설")) >= 1),
            ("인용 보호: 보고서·블로그는 따옴표 안을 인용으로 보호, --dialogue로 바꿀 수 있음(소설에서 protect, 보고서에서 edit)", lambda:
                exit_code(compare("앞이다.\n\n“메세지 보냈어?”\n\n뒤다.", "앞이다.\n\n“메시지 보냈어?”\n\n뒤다.", genre="보고서")) == 2
                and exit_code(compare("앞이다.\n\n“메세지 보냈어?”\n\n뒤다.", "앞이다.\n\n“메시지 보냈어?”\n\n뒤다.", genre="소설", dialogue="protect")) == 2
                and not compare("앞이다.\n\n“메세지 보냈어?”\n\n뒤다.", "앞이다.\n\n“메시지 보냈어?”\n\n뒤다.", genre="보고서", dialogue="edit")["protected"]),
            ("--protected-text: 등장마다 글자 그대로인지(하나만 바뀜·인용 안 덧붙임·문장 끝 덧붙임은 보호 항목, 따옴표 밖 서술 수정·따옴표 모양만 바뀐 것은 아님)", lambda: (lambda base, P: all(
                exit_code(compare(base, v, genre="소설", protected_text=P)) == 2 for v in (
                    base.replace("편지 끝에도 “나는 자유롭다.”", "편지 끝에도 “나는 행복하다.”"), base.replace("벽에는 “나는 자유롭다.”", "벽에는 “나는 자유롭다. 그리고 행복하다.”"),
                    base.replace("제1조 모든 국민은 평등하다.", "제1조 모든 국민은 평등하다, 원칙적으로."), base.replace("편지 끝에도 “나는 자유롭다.”라고 적혀 있었다.", "")))
                and all(not compare(base, v, genre="소설", protected_text=P)["protected"] for v in (
                    base.replace("벽에는", "낡은 벽에는"), base.replace("벽에는 “나는 자유롭다.”", "벽에는 \"나는 자유롭다.\""))))(
                "앞이다.\n\n벽에는 “나는 자유롭다.”라고 적혀 있었다.\n\n편지 끝에도 “나는 자유롭다.”라고 적혀 있었다.\n\n제1조 모든 국민은 평등하다.\n\n뒤다.",
                ("나는 자유롭다.", "“제1조 모든 국민은 평등하다.”"))),
            ("--protected-text 평범한 경우에 멈추지 않음: 문구 그대로+다른 문장만 수정(숫자·용어 없음), 원문과 같은 수정문, 원문에 없는 문구는 읽고 판단으로 알림", lambda:
                (lambda r1, r2, r3: not r1["protected"] and exit_code(r2) == 0 and any("찾지 못함" in x for x in r3["review"]) and not r3["protected"])(
                    compare("벽에는 “나는 자유롭다.”라고 적혀 있었다.\n\n그는 웃었다.", "벽에는 “나는 자유롭다.”라고 적혀 있었다.\n\n그는 크게 웃었다.", genre="소설", protected_text=("나는 자유롭다.",)),
                    compare("벽에는 “나는 자유롭다.”라고 적혀 있었다.", "벽에는 “나는 자유롭다.”라고 적혀 있었다.", genre="소설", protected_text=("나는 자유롭다.",)),
                    compare("그는 웃었다.", "그는 크게 웃었다.", genre="소설", protected_text=("나는 자유롭다.",)))),
            ("보호 문구가 두 곳 이상이면 알림(종료 코드 1), 한 곳이면 알리지 않음. 앞말까지 넣은 긴 문구는 그 자리만 보호(대사 자리는 고쳐도 됨)", lambda:
                (lambda base: exit_code(compare(base, base, genre="소설", protected_text=("나는 자유롭다.",))) == 1
                 and any("2곳에 등장함" in x for x in compare(base, base, genre="소설", protected_text=("나는 자유롭다.",))["review"])
                 and exit_code(compare("기사에는 “나는 자유롭다.”라고 적혀 있었다.", "기사에는 “나는 자유롭다.”라고 적혀 있었다.", genre="소설", protected_text=("나는 자유롭다.",))) == 0
                 and not compare(base, base.replace("그가 “나는 자유롭다.”", "그가 “난 자유야.”"), genre="소설", protected_text=("기사에는 “나는 자유롭다.”",))["protected"]
                 and exit_code(compare(base, base.replace("기사에는 “나는 자유롭다.”", "기사에는 “나는 행복하다.”"), genre="소설", protected_text=("기사에는 “나는 자유롭다.”",))) == 2)(
                    "기사에는 “나는 자유롭다.”라고 적혀 있었다.\n\n그는 신문을 접었다.\n\n그가 “나는 자유롭다.”라고 중얼거렸다.")),
            ("--protected-text는 gate에서도 사실 항목으로 보호(글자 그대로 지킬 문구를 바꾸면 자동 되돌림)", lambda:
                (lambda a, b, its: bool(its) and any(i["fact"] and any("--protected-text" in w for w in i["why"]) for i in its)
                 and gate_apply(a, b, {}, genre="보고서", protected_text=("승인 문구입니다.",))[0] == a)(
                    "승인 문구입니다.", "승인 문장입니다.", risk_items("승인 문구입니다.", "승인 문장입니다.", genre="보고서", protected_text=("승인 문구입니다.",)))),
            ("--terms 횟수 변화는 --protected-text 없이도 보고(세린 2회 → 1회)", lambda:
                any("세린" in x and "2회 → 1회" in x for x in compare("세린은 세린을 봤다.", "세린은 그를 봤다.", genre="소설", terms=("세린",))["count_changes"])),
            ("위험 수정 게이트: 알려진 실패는 확인 목록에 오르고, 판정이 없으면 편집을 살리며, REVERT면 최종본이 원문으로 돌아간다", lambda: all(
                (lambda its: bool(its) and (any(i["fact"] for i in its) or gate_apply(o_, r_, {}, genre="소설")[0] == r_)
                 and re.sub(r"\s+", " ", gate_apply(o_, r_, {i["id"]: {"decision": "REVERT", "reason": "읽어 보니 뜻이 바뀌었다"} for i in its}, genre="소설")[0]).strip()
                 == re.sub(r"\s+", " ", o_).strip())(risk_items(o_, r_, genre="소설"))
                for o_, r_, _t in list(REAL_MUST_FLAG) + REAL_MUST_REJECT_EXTRA)),
            ("위험 수정 게이트: 좋은 수정은 위험 항목이 없고, 앞 문장과 겹친 행동을 지운 수정은 확인 항목 하나로 올라 판정 없이도 남으며 REVERT면 되돌린다", lambda:
                not any(risk_items(o_, r_, genre="소설") for o_, r_ in REAL_MUST_PASS)
                and (lambda o_, r_, its: len(its) == 1 and not its[0]["fact"] and "눈빛뿐" in gate_apply(o_, r_, {}, genre="소설")[0]
                     and "눈빛뿐" in gate_apply(o_, r_, {its[0]["id"]: {"decision": "KEEP"}}, genre="소설")[0]
                     and "눈빛뿐" not in gate_apply(o_, r_, {its[0]["id"]: {"decision": "REVERT"}}, genre="소설")[0])(
                    REAL_KEEP_WITH_REASON[0], REAL_KEEP_WITH_REASON[1], risk_items(*REAL_KEEP_WITH_REASON, genre="소설"))),
            ("판정 규칙: 사실 종류는 모델의 이유로 유지되지 않고 사용자 지시·출처 override(근거 8자 이상)만 유지, 확인 종류는 판정이 없어도 유지하고 REVERT면 되돌린다", lambda: (
                gate_decide({"decision": "KEEP", "decision_reason": "앞 문장과 같은 장면이다"}, ["부정 뒤집힘"])[0] == "REVERT"
                and gate_decide({}, ["사실 변화"])[0] == "REVERT"
                and gate_decide({"decision": "KEEP", "override": "user", "override_evidence": "짧음"}, ["범위 변화"])[0] == "REVERT"
                and gate_decide({"decision": "KEEP", "override": "source", "override_evidence": "원전 3장 12쪽에서 확인한 문장"}, ["사실 변화"])[0] == "KEEP"
                and gate_decide({"decision": "KEEP", "override": "user", "override_evidence": "작가 메모 '나머지는 아직 확인 안 됨'은 본문이 아니다"},
                                ["시간·상태 변화", "고유 정보 삭제"])[0] == "KEEP"
                and gate_decide({}, ["행위자 변화"])[0] == "KEEP" and gate_decide({}, ["고유 정보 삭제", "낱말 바꿈"])[0] == "KEEP"
                and gate_decide({"decision": "REVERT", "reason": "읽어 보니 대상이 바뀌었다"}, ["화자·대상·관계 변화"])[0] == "REVERT"
                and gate_decide({"decision": "REVERT"}, ["사실 변화"])[0] == "REVERT")),
            ("고유 정보 삭제·낱말 바꿈(확인 항목): 깨진 병 삭제·절삭 깊이 → 날·다른 문단과 겹친 행동 삭제는 확인 목록에 오르고, 판정이 없으면 남고 REVERT면 원문으로. "
             "대사의 담화 표지·동사 활용만 바뀐 경우는 위험 아님", lambda: (lambda T: (
                "깨진" not in T(GATE_EV[0], GATE_EV[1], None) and "깨진" in T(GATE_EV[0], GATE_EV[1], {"decision": "REVERT"})
                and "절삭" not in T(GATE_EV[2], GATE_EV[3], None) and "절삭" in T(GATE_EV[2], GATE_EV[3], {"decision": "REVERT"})
                and "반장이 다가왔다. 나는 손을 뗐다." not in T(GATE_EV[4], GATE_EV[5], None)
                and "반장이 다가왔다. 나는 손을 뗐다." in T(GATE_EV[4], GATE_EV[5], {"decision": "REVERT"})
                and all(risk_items(GATE_EV[k], GATE_EV[k + 1], genre="소설") for k in (0, 2, 4))
                and not risk_items(GATE_EV[6], GATE_EV[7], genre="소설")))(
                lambda o_, r_, d_: re.sub(r"\s+", " ", gate_apply(o_, r_, {i["id"]: d_ for i in risk_items(o_, r_, genre="소설")} if d_ else {}, genre="소설")[0]))),
            ("게이트 형식 보존: 되돌릴 것이 없으면 수정문을 한 글자도 바꾸지 않고(제목·서명 줄바꿈·끝 줄바꿈), 사실 변화는 그 문장만 원문으로 갈아 끼운다", lambda: (
                lambda src, out: gate_apply(src, out, {}, genre="블로그")[0] == out
                and gate_apply(src, out.replace("5,000원", "6,000원"), {}, genre="블로그")[0] == out.replace("입장료는 5,000원이에요.", "입장료는 5,000원이다.")
                and gate_apply(src, out.replace("겨울에는 쉽니다", "겨울에는 쉬지 않습니다"), {}, genre="블로그")[0] == out)(
                "주말 장터 안내\n\n토요일마다 장터가 열린다. 입장료는 5,000원이다. 겨울에는 쉽니다.\n\n- 운영팀 드림",
                "주말 장터 안내\n\n토요일마다 장터가 열려요. 입장료는 5,000원이에요. 겨울에는 쉽니다.\n\n- 운영팀 드림\n")),
            ("게이트 정렬: 문장을 합치고 나눈 뒤 사실을 되돌려도 문장이 빠지거나 겹치지 않는다(원문의 모든 문장이 한 번씩, 수정문의 남긴 문장도 한 번씩)", lambda: (
                lambda src, out: (lambda fin: all(fin.count(x) == 1 for x in ("시작했다", "1층", "12시", "처음 석 달", "120명", "15명", "할아버지"))
                                  and "13시" not in fin)(gate_apply(src, out, {}, genre="에세이")[0]))(
                "2025년 3월 첫 토요일 시작. 동네 복지관 1층 식당. 매주 토요일 점심만. 봉사자는 10시 반까지 모이고 배식은 12시부터. 처음 석 달은 설거지만 했음. "
                "하루 평균 120명 정도 오심. 봉사자는 보통 15명쯤. 매번 맨 앞에 서시는 할아버지가 계심.",
                "2025년 3월 첫 토요일, 동네 복지관 1층 식당에서 시작했다. 매주 토요일 점심만 한다. 봉사자는 10시 반까지 모이고, 배식은 13시부터다. "
                "처음 석 달은 설거지만 했다. 하루 평균 120명 정도가 오신다. 봉사자는 보통 15명쯤이다. 매번 맨 앞에 서시는 할아버지가 계신다.")),
            ("사실 오탐 아님: 아니다 → 아닙니다·안남았다 → 안 남았다·않 된다 → 안 된다·아님 → 아니다·므로써 → 함으로써·첫째/둘째 목록을 문장으로·"
             "한 번에·되풀이한 AI 줄임·단위 밝힘(서울 10 → 10곳)·10시 반 = 10시 30분·목록 번호(1. 2.)·되풀이한 부정 줄임", lambda: all(
                not [k for i in risk_items("앞 문장이다.\n\n%s\n\n뒤 문장이다." % a, "앞 문장이다.\n\n%s\n\n뒤 문장이다." % b) for k in i["kinds"] if k in FACT_KINDS]
                for a, b in (("모든 응대가 순조로웠던 것은 아니다.", "모든 응대가 순조로웠던 것은 아닙니다."), ("배터리가 얼마 안남았다는 걸 알았다.", "배터리가 얼마 안 남았다는 걸 알았다."),
                             ("그녀는 울면 않 된다고 생각했다.", "그녀는 울면 안 된다고 생각했다."), ("봉사는 거창한 거 아님.", "봉사는 거창한 게 아니다."),
                             ("예보를 분석하므로써 동선을 제안한다.", "예보를 분석함으로써 동선을 제안한다."),
                             ("저는 세 가지를 얻었습니다. 첫째, 인내. 둘째, 협업. 셋째, 책임감.", "저는 인내, 협업, 책임감이라는 세 가지를 얻었습니다."),
                             ("이 모든 것을 한 번에!", "이 모든 것을 한꺼번에 갖췄습니다!"), ("AI를 쓸 때 중요한 것은 AI를 잘 쓰는 것이다.", "AI를 쓸 때 중요한 것은 잘 쓰는 것이다."),
                             ("18곳 추가. 서울 10, 경기 5, 부산 3.", "새로 여는 18곳은 서울 10곳, 경기 5곳, 부산 3곳이다."), ("10시 반에 모인다.", "10시 30분에 모인다."),
                             ("1. 서론\n2. 본론", "서론과 본론으로 나뉜다."), ("설탕 안 넣음!! 설탕 안 넣었다는 거 꼭 강조.", "설탕을 넣지 않았어요!")))),
            ("사실 변화: 값이 바뀌거나 문서에서 사라진 숫자, 새 숫자, 다른 로마자 이름(KTX → SRT), --terms 이름이 사라지거나 한 글자 바뀜(세린 → 세릭), "
             "부정 홀짝(않으면 안 된다 → 않아도 된다, 열지 않고 → 열었다. 그리고), 이미·모두 삭제는 판정 없이 되돌린다", lambda: all(
                (lambda s_, o_: re.sub(r"\s+", " ", gate_apply(s_, o_, {}, genre="소설", terms=["세린"])[0]).strip() == re.sub(r"\s+", " ", s_).strip())(
                    "앞 문장이다.\n\n%s\n\n뒤 문장이다." % a, "앞 문장이다.\n\n%s\n\n뒤 문장이다." % b)
                for a, b in (("참가자는 3명이었다.", "참가자는 5명이었다."), ("표는 다 팔렸다.", "표 200장은 다 팔렸다."),
                             ("KTX를 탔다.", "SRT를 탔다."), ("세린은 문을 열었다.", "그녀는 문을 열었다."), ("세린은 문을 열었다. 세린이 웃었다.", "세릭은 문을 열었다. 세린이 웃었다."),
                             ("이 서류는 제출하지 않으면 안 된다.", "이 서류는 제출하지 않아도 된다."), ("그는 문을 열지 않고 창밖을 보았다.", "그는 문을 열었다. 그리고 창밖을 보았다."),
                             ("회의는 이미 끝났다.", "회의는 끝났다."), ("직원 모두가 반대했다.", "직원이 반대했다.")))
                and gate_apply("앞 문장이다.\n\n참가자는 3명이었다. 모두 왔다.\n\n뒤 문장이다.", "앞 문장이다.\n\n참가자가 모두 왔다.\n\n뒤 문장이다.", {})[0]
                == "앞 문장이다.\n\n참가자는 3명이었다. 참가자가 모두 왔다.\n\n뒤 문장이다."),  # 지운 문장만 되살린다(같은 문단 자리에)
            ("인과 역전은 확인 항목(인과 변화)으로 오른다(어미형 와서·내려서·니까·므로·느라, 명사형 때문에·탓에·까닭에, 계기 가서), 원인만 두고 결과를 바꾼 것·표지만 바꾼 것은 아님", lambda: all(
                any("인과 변화" in i["kinds"] for i in risk_items("앞이다.\n\n%s\n\n뒤다." % a, "앞이다.\n\n%s\n\n뒤다." % b, genre="소설")) for a, b in (
                    ("비가 와서 경기가 취소됐다.", "경기가 취소돼서 비가 왔다."), ("약을 먹어서 열이 내렸다.", "열이 내려서 약을 먹었다."), ("길이 막히니까 늦었다.", "늦으니까 길이 막혔다."),
                    ("눈 때문에 길이 막혔다.", "길이 막혔기 때문에 눈이 왔다."), ("학교에 가서 친구를 만났다.", "친구를 만나서 학교에 갔다."),
                    ("감기에 걸린 탓에 결석했다.", "결석한 탓에 감기에 걸렸다."), ("늦잠을 자느라 버스를 놓쳤다.", "버스를 놓치느라 늦잠을 잤다."),
                    ("물가가 오른 까닭에 소비가 줄었다.", "소비가 준 까닭에 물가가 올랐다."), ("배가 고프니까 짜증이 났다.", "짜증이 나니까 배가 고팠다.")))
                and not any(any("인과 역전" in w for i in risk_items("앞이다.\n\n%s\n\n뒤다." % a, "앞이다.\n\n%s\n\n뒤다." % b, genre="소설") for w in i["why"]) for a, b in (  # 역전 판정만 본다(표지 바꿈은 기존 인과 표지 판정의 알려진 한계)
                    ("비가 와서 경기가 취소됐다.", "비가 와 경기가 취소됐다."), ("학교에서 친구를 만나서 웃었다.", "학교에서 친구를 만나 웃었다."),
                    ("감기에 걸린 탓에 결석했다.", "감기에 걸려서 결석했다."), ("차가 막히는 바람에 회의에 늦었다.", "차가 막히는 바람에 회의에 조금 늦었다.")))),
            ("사건 연결 추가(확인 항목): 따로이던 두 사건을 -자로 이으면 확인 목록에 오르고, 판정이 없으면 남고 REVERT면 원문으로", lambda: (lambda o_, r_: (
                any("사건 연결 추가" in i["kinds"] for i in risk_items(o_, r_, genre="소설"))
                and "끄덕이자" in gate_apply(o_, r_, {}, genre="소설")[0]
                and "끄덕이자" not in gate_apply(o_, r_, {i["id"]: {"decision": "REVERT"} for i in risk_items(o_, r_, genre="소설")}, genre="소설")[0]))(
                "내가 고개를 끄덕이면 유나는 늘 뚜껑을 닫았다.\n\n나는 고개를 끄덕였다. 유나는 보온병 뚜껑을 닫았다.\n\n창밖이 어두웠다.",
                "내가 고개를 끄덕이면 유나는 늘 뚜껑을 닫았다.\n\n내가 고개를 끄덕이자 유나는 보온병 뚜껑을 닫았다.\n\n창밖이 어두웠다.")),
            ("인과 관계 비교(대조 짝과 무관): 문장 사이 역전(같은 서술어 반복·이에 따라·이로 인해)과 주제어형 역전(기인해·비롯돼)은 인과 변화, 역전 아닌 재배열은 아님", lambda: all(
                any("인과 변화" in i["kinds"] for i in risk_items("앞이다.\n\n%s\n\n뒤다." % a, "앞이다.\n\n%s\n\n뒤다." % b, genre="소설")) for a, b in (
                    ("지진이 발생했다. 그래서 해일이 발생했다.", "해일이 발생했다. 그래서 지진이 발생했다."), ("강풍이 불었다. 이에 따라 운항이 멈췄다.", "운항이 멈췄다. 이에 따라 강풍이 불었다."),
                    ("정전이 일어났다. 이로 인해 공장이 멈췄다.", "공장이 멈췄다. 이로 인해 정전이 일어났다."), ("화재는 누전에서 비롯돼 번졌다.", "누전은 화재에서 비롯돼 번졌다."),
                    ("병은 과로에 기인해 생겼다.", "과로는 병에 기인해 생겼다.")))
                and not any(any("인과 역전" in w for i in risk_items("앞이다.\n\n%s\n\n뒤다." % a, "앞이다.\n\n%s\n\n뒤다." % b, genre="소설") for w in i["why"]) for a, b in (
                    ("폭우가 쏟아졌다. 그래서 도로가 잠겼다.", "폭우가 쏟아져서 도로가 잠겼다."), ("폭우 여파로 침수가 발생했다.", "침수는 폭우 여파로 발생했다."),
                    ("화재는 누전에서 비롯돼 번졌다.", "누전에서 비롯된 화재가 번졌다."), ("기차는 눈이 내리는 바람에 늦었다.", "눈이 내리는 바람에 기차가 늦었다.")))),
            ("STYLE_SAFE: 이중 피동·번역투(에 있어서·를 통해·위해)·AI 질문 훅·'단순한 X가 아닙니다'·과장 수식·빈 결론 표지·수사적 부정·불가↔수 없·긴↔짧은 부정·요청 어미 통일·이중 부정의 의무 표현은 사실 종류가 아니고, "
             "실제 사건 삭제(강력한 태풍)·진짜 부정 뒤집힘(갈 수 없었다 → 갔다, 못 → 안, 불가 → 수 있다)·절삭 → 날은 여전히 잡는다", lambda: (lambda K: (
                all(not [k for k in K(a, b) if k in FACT_KINDS] and "고유 정보 삭제" not in K(a, b) for a, b in (
                    ("보고서는 내일까지 제출되어질 예정이다.", "보고서는 내일까지 제출될 예정이다."), ("업무 처리에 있어서 가장 중요한 것은 정확성이다.", "업무 처리에서 가장 중요한 것은 정확성이다."),
                    ("이번 조사를 통해 문제점을 확인했다.", "이번 조사로 문제점을 확인했다."), ("왜 우리는 계획을 세울까요? 답은 간단합니다. 계획은 불안을 줄여 줍니다.", "계획은 불안을 줄여 줍니다."),
                    ("독서는 단순한 취미가 아닙니다. 그것은 생각을 넓히는 습관입니다.", "독서는 생각을 넓히는 습관입니다."), ("결론적으로, 이 방법은 시간을 아껴 줍니다.", "이 방법은 시간을 아껴 줍니다."),
                    ("그날의 기쁨은 이루 말할 수 없었다.", "그날은 무척 기뻤다."), ("주말에는 주차가 불가합니다.", "주말에는 주차할 수 없습니다."), ("그는 먹지 않았다.", "그는 안 먹었다."),
                    ("자료는 금요일까지 보내 주세요. 문의는 메일로 부탁드려요.", "자료는 금요일까지 보내 주시기 바랍니다. 문의는 메일로 해 주십시오."),
                    ("이 서류는 제출하지 않으면 안 된다.", "이 서류는 반드시 제출해야 한다."), ("그는 오지 않았다.", "그는 끝내 오지 않았다.")))
                and all(K(a, b) for a, b in (("강력한 태풍이 상륙했다. 주민들은 대피했다.", "주민들은 대피했다."), ("그는 갈 수 없었다.", "그는 갔다."), ("그는 못 갔다.", "그는 안 갔다."),
                                             ("현장 출입은 불가하다.", "현장 출입은 할 수 있다."), ("작업자가 절삭 속도를 더 올렸다.", "작업자가 날을 더 빨리 돌렸다."),
                                             ("이 서류는 제출하지 않으면 안 된다.", "이 서류는 제출하지 않아도 된다.")))
                and any("부정 뒤집힘" in k for k in [K("그는 갈 수 없었다.", "그는 갔다.")])))(
                lambda a, b: [k for i in risk_items("아침은 흐렸다.\n\n%s\n\n끝이었다." % a, "아침은 흐렸다.\n\n%s\n\n끝이었다." % b, genre="보고서") for k in i["kinds"]])),
            ("의미 경계: 이미·아직(시간·상태)·모두·오직(범위)은 사실 종류라 '유지+이유'로도 되돌아가고, 다시·또(반복)·무엇보다·그저(강조·한정)는 확인 목록에 올라 "
             "판정이 없으면 남고 REVERT면 되돌아간다. 같은 부류 바꿈·같은 뜻으로 남는 말·띄어쓰기·오타 교정은 위험 아님", lambda: (lambda K, G: (
                all(G(a, b, {"decision": "KEEP", "reason": "앞뒤 문맥상 같은 뜻이다"}) == "src" for a, b in (
                    ("회의는 이미 끝났다.", "회의는 끝났다."), ("그녀는 여전히 그 집에 산다.", "그녀는 그 집에 산다."), ("직원 모두가 반대했다.", "직원이 반대했다."),
                    ("오로지 일에 매달렸다.", "일에 매달렸다."), ("회의가 끝났다.", "회의가 이미 끝났다."), ("회의는 이미 끝났다.", "회의는 이제 끝났다.")))
                and all(K(a, b) and G(a, b, {}) == "out" and G(a, b, {"decision": "REVERT"}) == "src" for a, b in (
                    ("그는 또다시 거짓말을 했다.", "그는 거짓말을 했다."), ("무엇보다 가족이 소중하다.", "가족이 소중하다."), ("그는 그냥 떠났다.", "그는 떠났다.")))
                and all(not K(a, b) for a, b in (("이미 벌써 끝났다.", "벌써 끝났다."), ("모두 다 먹었다.", "다 먹었다."), ("오직 민수만 왔다.", "민수만 왔다."),
                                                 ("공사를 다시 재개했다.", "공사를 재개했다."), ("무엇보다도 가장 중요한 것은 안전이다.", "가장 중요한 것은 안전이다."),
                                                 ("그는 그저 웃기만 했다.", "그는 웃기만 했다."), ("다시한번 확인했다.", "다시 한번 확인했다."),
                                                 ("그는 여전이 창밖만 보았다.", "그는 여전히 창밖만 보았다."), ("모은 사람이 찬성표를 던졌다.", "모든 사람이 찬성표를 던졌다."),
                                                 ("전화 또는 앱으로 신청한다.", "전화나 앱으로 신청한다.")))))(
                lambda a, b: [k for i in risk_items("아침은 흐렸다.\n\n%s\n\n끝이었다." % a, "아침은 흐렸다.\n\n%s\n\n끝이었다." % b, genre="소설") for k in i["kinds"]],
                lambda a, b, d: (lambda o_, r_: {re.sub(r"\s+", " ", o_).strip(): "src", re.sub(r"\s+", " ", r_).strip(): "out"}.get(
                    re.sub(r"\s+", " ", gate_apply(o_, r_, {i["id"]: d for i in risk_items(o_, r_, genre="소설")}, genre="소설")[0]).strip(), "?"))(
                    "아침은 흐렸다.\n\n%s\n\n끝이었다." % a, "아침은 흐렸다.\n\n%s\n\n끝이었다." % b))),
            ("'단순한 X가 아니다': 뒤 말이 확정 단언이면 정리해도 위험 아님(뒤 문장이 블록 밖이어도), 가능성뿐이면 확정 부정 삭제로 근거가 있어야 하고, '단순한 X다'로 바꾸면 부정 뒤집힘", lambda: (lambda K: (
                not K("이건 단순한 취미가 아니다. 이건 습관이다.", "이건 습관이다.")
                and not K("아침 걷기는 단순한 운동이 아닙니다. 그것은 삶의 방식입니다.", "아침 걷기는 운동 이상입니다. 그것은 삶의 방식입니다.")
                and not K("독서는 단순한 취미가 아니라 습관이다.", "독서는 습관이다.")
                and "고유 정보 삭제" in K("이건 단순한 실수가 아닙니다. 범죄일 수도 있습니다.", "범죄일 수도 있습니다.")
                and "고유 정보 삭제" in K("이건 단순한 사고가 아니다. 아마 누군가 계획했을 것이다.", "아마 누군가 계획했을 것이다.")
                and "부정 뒤집힘" in K("이건 단순한 사고가 아니다. 이건 살인이다.", "이건 단순한 사고다. 이건 살인이다.")))(
                lambda a, b: [k for i in risk_items("아침은 흐렸다.\n\n%s\n\n끝이었다." % a, "아침은 흐렸다.\n\n%s\n\n끝이었다." % b, genre="소설") for k in i["kinds"]])),
            ("배타 범위 '뿐': 민수뿐이었다 → 민수였다·웃을 뿐이었다 → 웃었다·이것 → 이것뿐은 범위 변화, 오직 민수만·하나뿐·뿐만 아니라·뿐더러·밖에 없다는 범위 변화 아님. "
             "짧은 명사(유나가·환자가·부장이)는 사라진 부사(유일하게·확실하게·분명하게)의 활용으로 치지 않되, 같은 동사의 짧은 활용(살았잖아 → 산)은 남은 것으로 본다", lambda: (lambda K: (
                all("범위 변화" in K(a, b) for a, b in (("담당자는 민수뿐이었다.", "담당자는 민수였다."), ("그녀는 웃을 뿐이었다.", "그녀는 웃었다."),
                                                    ("출구는 하나뿐이다.", "출구는 하나다."), ("남은 건 이것이다.", "남은 건 이것뿐이다.")))
                and all(not K(a, b) for a, b in (("담당자는 민수뿐이었다.", "담당자는 오직 민수만이었다."), ("남은 건 보고서뿐이다.", "남은 건 보고서 하나뿐이다."),
                                                 ("그녀뿐만 아니라 동생도 왔다.", "그녀와 동생이 왔다."), ("우리는 이 마을에서 십 년째 살았잖아.", "우리는 이 마을에서 십 년째 산 거야.")))
                and all("범위 변화" not in K(a, b) for a, b in (("값이 비쌀뿐더러 느리다.", "값이 비싸고 느리다."), ("담당자는 민수뿐이었다.", "담당자는 민수밖에 없었다.")))
                and all("고유 정보 삭제" in K(a, b) for a, b in (("유나가 유일하게 반대했다.", "유나가 반대했다."), ("환자가 확실하게 회복했다.", "환자가 회복했다."),
                                                        ("부장이 분명하게 거절했다.", "부장이 거절했다.")))))(
                lambda a, b: [k for i in risk_items("아침은 흐렸다.\n\n%s\n\n끝이었다." % a, "아침은 흐렸다.\n\n%s\n\n끝이었다." % b, genre="소설") for k in i["kinds"]])),
            ("가짓수 예고·목록 서수·당연의 '수밖에': 항목을 나열한 '세 가지'·소제목 서수('두 번째 깨달음')를 지우면 확인 목록(사실 아님), 나열이 없는 '세 가지'를 지우거나 "
             "가짓수를 바꾸면 사실 변화, '~ㄹ 수밖에 없다'는 범위 변화가 아니고 '민수밖에 없었다'는 범위다", lambda: (lambda F, K: (
                not F("이 일로 세 가지를 배웠다. 첫째, 소통. 둘째, 현장. 셋째, 끈기.", "이 일로 소통과 현장, 끈기를 배웠다.")
                and "고유 정보 삭제" in K("이 일로 세 가지를 배웠다. 첫째, 소통. 둘째, 현장. 셋째, 끈기.", "이 일로 소통과 현장, 끈기를 배웠다.")
                and not F("30L 용량, 1.2kg 무게, 16인치 수납이라는 세 가지 장점을 담았다.", "30L 용량에 무게는 1.2kg이고 16인치 노트북도 들어간다.")
                and not F("**첫 번째 깨달음: 자연**\n봄에 냉해를 입었다.\n\n**두 번째 깨달음: 이웃**\n이웃이 도왔다.", "봄에 냉해를 입었다.\n\n이웃이 도왔다.")
                and F("메뉴는 세 가지다. 가격은 만 원이다.", "메뉴가 있다. 가격은 만 원이다.") and F("이 일로 세 가지를 배웠다. 첫째, 소통. 둘째, 현장. 셋째, 끈기.", "이 일로 네 가지를 배웠다. 첫째, 소통. 둘째, 현장. 셋째, 끈기.")
                and F("수능을 두 번째 봤다. 결과는 좋았다.", "수능을 봤다. 결과는 좋았다.")
                and "범위 변화" not in K("당일 수확해 신선함은 압도적이다.", "당일 수확해 신선할 수밖에 없다.") and "범위 변화" in K("담당자는 민수였다.", "담당자는 민수밖에 없었다.")))(
                lambda a, b: any(i["fact"] for i in risk_items(a, b, genre="블로그")), lambda a, b: [k for i in risk_items(a, b, genre="블로그") for k in i["kinds"]])),
            ("위험 수정 게이트 세부: 닫는 곧은따옴표 뒤 조사(\"밥이 달다\"라고)도 인용, '조사 대상'·'구조 대원'은 수가 아님, 원문에 이미 있는 '3곳'으로 단위를 밝힌 수는 사라진 사실이 아님, "
             "합치며 옮긴 짧은 문장(그게 전부임.)은 이웃 묶음과 한 묶음, 수가 초점인 분열문에 '뿐'만 붙인 것(빠진 건 그 한 번 → 그 한 번뿐)은 확인 목록", lambda: (
                ("인용", "밥이 달다") in protected_items('할아버지는 "밥이 달다"라고 하셨다.')
                and not [k for k in protected_items("생활관 조사 대상은 모두 모였다. 구조 대원이 왔다.") if k[0] == "숫자"]
                and not compare("12곳에서 30곳으로 늘린다. 서울 10, 부산 3. 부산 3곳은 처음이다.", "12곳에서 30곳으로 늘린다. 서울 10곳, 부산 3곳이다. 부산 3곳은 처음이다.", genre="보도자료")["protected"]
                and not risk_items("봉사는 거창하지 않다. 한 달에 네 번, 한 번에 두 시간 반. 그게 전부임. 그래도 좋았다.",
                                   "봉사는 거창하지 않다. 한 달에 네 번, 한 번에 두 시간 반이 전부다. 그래도 좋았다.", genre="에세이")
                and [i["grade"] for i in risk_items("1년 동안 빠진 건 그 한 번.", "1년 동안 빠진 날은 그 한 번뿐이다.")] == ["확인"]
                and [i["grade"] for i in risk_items("참석한 건 세 명이었다.", "참석한 건 두 명뿐이었다.")] == ["사실"]
                and [i["grade"] for i in risk_items("✈️ **여행의 모든 순간을 바꿀 가방, 트래블팩 30**\n\n가볍다.", "트래블팩 30\n\n가볍다.")] == ["확인"]
                and [i["grade"] for i in risk_items("매장은 모두 쉰다.", "매장은 쉰다.")] == ["사실"]
                and [i["grade"] for i in risk_items("달리기는 하루를 바꿨다.\n\n**일상의 모든 날이 빛난다**\n새벽에 뛰었다.", "달리기는 하루를 바꿨다.\n\n새벽에 뛰었다.")] in ([], ["확인"])
                and not [i for i in risk_items("그 미소가 이유가 아니었을까? 할머니가 웃었다.", "그 미소가 이유였는지도 모른다. 할머니가 웃었다.") if i["fact"]]
                and ("숫자", "9월") in protected_items("월요일(9/21) 전화") and ("숫자", "2025년") in protected_items("(2024.3~2025.4) 동안")
                and ("숫자", "11월") in protected_items("2025.10.13~11.7의 4주"))),
            ("규칙 고침 안전 처리: 스킬 규칙이 권하는 표준 고침(위치·존재 → 있다, 회의를 가졌다 → 열었다, 필요로 한다 → 필요하다, 보다 → 더, 검토를 진행했다 → 검토했다, "
             "재사용이 가능하다 → 다시 쓸 수 있다, 관계로 → -어, 의 경우 → 은, 에 대한 → 붙여 씀, 보여 준다 → 보면)은 위험 항목이 없고, 뜻이 바뀌거나 같은 문장의 다른 곳이 "
             "바뀌거나 인용 안이면 그대로 항목이 남는다", lambda: (lambda K: (
                all(not K(a, b) for a, b in (
                    ("본사는 도시 외곽에 위치해 있다.", "본사는 도시 외곽에 있다."), ("이 규정에는 예외가 존재하지 않는다.", "이 규정에는 예외가 없다."),
                    ("시장은 어제 기자회견을 가졌다.", "시장은 어제 기자회견을 열었다."), ("이 결과는 큰 의미를 가진다.", "이 결과는 큰 의미가 있다."),
                    ("이 작업은 전문 인력을 필요로 합니다.", "이 작업에는 전문 인력이 필요합니다."), ("보다 나은 방법을 찾았다.", "더 나은 방법을 찾았다."),
                    ("감사팀은 회계 자료에 대한 검토를 실시했다.", "감사팀은 회계 자료를 검토했다."), ("이 용기는 재사용이 가능하다.", "이 용기는 다시 쓸 수 있다."),
                    ("공사가 지연된 관계로 개장을 미뤘다.", "공사가 지연되어 개장을 미뤘다."), ("신입 사원의 경우 교육을 받는다.", "신입 사원은 교육을 받는다."),
                    ("제품에 대한 문의가 늘었다.", "제품 문의가 늘었다."), ("이번 통계는 야간 이용이 늘었다는 것을 말해 준다.", "이번 통계를 보면 야간 이용이 늘었다."),
                    ("조사 결과는 임대 주택에 대한 수요가 높다는 것을 보여 줍니다.", "조사 결과를 보면 임대 주택 수요가 높습니다.")))
                and "부정 뒤집힘" in K("이 방식에는 문제가 존재한다.", "이 방식에는 문제가 없다.")
                and all(K(a, b) for a, b in (("우리는 매주 회의를 가졌다.", "우리는 매주 회의를 취소했다."), ("우리는 매주 회의를 가졌다.", "우리는 매달 회의를 열었다."),
                                             ("본사는 외곽에 위치해 있다.", "본사는 외곽에 있었다."), ("신입 사원의 경우 교육을 받는다.", "신입 사원도 교육을 받는다."),
                                             ("그는 “역 앞에 위치한 가게”라고 말했다.", "그는 “역 앞에 있는 가게”라고 말했다.")))))(
                lambda a, b: [k for i in risk_items("올해 계획이 확정됐다.\n\n%s\n\n이상입니다." % a, "올해 계획이 확정됐다.\n\n%s\n\n이상입니다." % b, genre="보고서") for k in i["kinds"]])),
            ("무경고 내용 변경 제외: 기능어만 바뀐 좋은 수정('나는' 삭제, '그리고' 삭제)은 올리지 않고, 한정 조사 변화(나는 → 나도)는 조사로 잡는다", lambda:
                exit_code(compare("앞이다. 나는 창문에서 눈을 뗐다. 뒤다.", "앞이다. 창문에서 눈을 뗐다. 뒤다.")) == 0
                and exit_code(compare("앞이다. 그리고 들어갔다. 뒤다.", "앞이다. 들어갔다. 뒤다.")) == 0 and fc_notes("나는 갔다.", "나도 갔다.", "조사")),
            ("무경고 내용 변경: 인과·시간·행동 순서를 뒤집어 코드가 분류하지 못해도 읽고 판단으로(종료 코드 1), 띄어쓰기·문장부호만 고친 것은 아님", lambda:
                all(exit_code(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)) == 1 for a, b in (
                    ("약을 먹어서 열이 내렸다.", "열이 내려서 약을 먹었다."), ("돈이 없어서 가지 못했다.", "가지 못해서 돈이 없었다."),
                    ("문이 열린 뒤 바람이 들어왔다.", "바람이 들어온 뒤 문이 열렸다."), ("밥을 먹고 약을 먹었다.", "약을 먹고 밥을 먹었다.")))
                and all(exit_code(compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)) == 0 for a, b in (
                    ("무리라는건 알았다.", "무리라는 건 알았다."), ("그는 왔다.", "그는 왔다!"), ("그래, 가자.", "그래 가자.")))),
            ("연도·날짜가 붙은 달은 강하게(2026년 십일월에 → 2026년 십이월에, 오월 3일 → 유월 3일), 이월에(회계)는 아님, 붙여 쓴 수를 띄어 쓴 숫자로 바꾼 것은 같은 수(삼십만원 → 30만 원)", lambda:
                exit_code(compare("앞이다. 2026년 십일월에 떠났다. 뒤다.", "앞이다. 2026년 십이월에 떠났다. 뒤다.")) == 2 and exit_code(compare("앞이다. 오월 3일에 쉬었다. 뒤다.", "앞이다. 유월 3일에 쉬었다. 뒤다.")) == 2
                and not compare("앞이다. 예산 이월에 문제가 있다. 뒤다.", "앞이다. 예산을 넘기는 데 문제가 있다. 뒤다.")["protected"]
                and not compare("앞이다. 삼십만원을 냈다. 뒤다.", "앞이다. 30만 원을 냈다. 뒤다.")["protected"] and not compare("앞이다. 한번 해 보자. 뒤다.", "앞이다. 한 번 해 보자. 뒤다.")["protected"]),
            ("한자어 숫자: 십일월·십이월은 달로만, 육월·십월은 유월·시월과 같은 수", lambda:
                [x[:2] for x in sino_counts("십일월과 십이월")] == [(11, "월"), (12, "월")]
                and not compare("앞이다. 십일월에 떠났다. 뒤다.", "앞이다. 11월에 떠났다. 뒤다.")["protected"]
                and not compare("앞이다. 육월에 떠나 십월에 왔다. 뒤다.", "앞이다. 유월에 떠나 시월에 왔다. 뒤다.")["protected"]),
            ("단위 동의어는 같은 수(두 사람 = 2명, 스무 살 = 20세, 3퍼센트 = 3%, 두 달 = 2개월)", lambda: all(not compare("앞이다. %s 뒤다." % a, "앞이다. %s 뒤다." % b)["protected"]
                for a, b in (("두 사람이 왔다.", "2명이 왔다."), ("스무 살이었다.", "20세였다."), ("3퍼센트 올랐다.", "3% 올랐다."), ("두 달 뒤였다.", "2개월 뒤였다.")))),
            ("역할 순서·관형 기준: 영희를 밀고 민수를 → 민수를 밀고 영희를, 영희에게·민수에게, 서울에서·대전에서, 영희의 책 → 철수의 책", lambda: all(fc_notes(a, b, k) for a, b, k in (
                ("철수는 영희를 밀고 민수를 때렸다.", "철수는 민수를 밀고 영희를 때렸다.", "역할 순서"), ("철수는 영희에게 책을 주고 민수에게 돈을 줬다.", "철수는 민수에게 책을 주고 영희에게 돈을 줬다.", "역할 순서"),
                ("서울에서 출발해 부산으로 가고 대전에서 멈췄다.", "대전에서 출발해 부산으로 가고 서울에서 멈췄다.", "역할 순서"), ("철수는 영희의 책을 찢었다.", "영희는 철수의 책을 찢었다.", "관형 기준")))),
            ("한글 수량 오탐 아님: '못 한 채'·'일을 한 채'·'한 줄 알았다'·'말한 대로'의 '한'은 수가 아님. '한'(집 한 채)은 약한 항목, '두'(집 두 채)부터 수", lambda:
                not korean_counts("둘은 역을 찾지 못 한 채 아침을 맞았다. 결정을 한 채 떠났다. 그가 다 한 줄 알았다.")
                and korean_counts("그는 집 한 채를 샀다.") == [(1, "채", True)] and korean_counts("그는 집 두 채를 샀다.") == [(2, "채", False)]),
            ("한글 수량 띄어쓰기만 고친 것은 같은 수(열 세건 → 열세 건, 네 번 째 → 네 번째)", lambda: not compare("앞이다. 총 열 세건의 사건이다. 뒤다.", "앞이다. 총 열세 건의 사건이다. 뒤다.")["protected"]
                and not compare("앞이다. 네 번 째 문을 열었다. 뒤다.", "앞이다. 네 번째 문을 열었다. 뒤다.")["protected"]),
            ("대조: 띄어쓰기만 고친 소설은 보호 항목 없음", lambda: not compare("무리라는건 알고있었다.", "무리라는 건 알고 있었다.", genre="웹소설")["protected"])]
    pfails = []
    for name, fn in pipe:
        try:
            ok = fn()
        except Exception as ex:
            ok = False
            name += " (%s)" % ex
        if not ok:
            pfails.append(name)
    print("파이프라인 시험 %d건: 통과 %d, 실패 %d" % (len(pipe), len(pipe) - len(pfails), len(pfails)))
    for name in pfails:
        print("  - " + name)
    return 1 if (fails or pfails) else 0


def print_rules():
    for role in ("기계", "형태", "읽기"):
        rs = [r for r in RULES if r.role == role]
        print("## %s (%d개)" % ({"기계": "기계 점검", "형태": "형태 찾기", "읽기": "읽기 안내"}[role], len(rs)))
        for r in rs:
            print("%-4s %s%s" % (r.rid, r.name, " [소설·에세이]" if r.only else ""))
    print("\n장르 프로필: " + ", ".join(GENRES))




# ---- 사실 대조(facts): 글자로 확인되는 값만 막고, 뜻의 판단은 의미 검수에 넘긴다
FACTS_CANDIDATE_KINDS = ("부정 뒤집힘", "시간·상태 변화", "범위 변화", "관계 뒤집힘", "인과 뒤집힘")


def facts_report(src_text, out_text, genre="일반", terms=(), dialogue="auto", protected_text=()):
    """원문과 수정본에서 (1) 사실 불일치: 숫자·단위·날짜·URL·코드·식별자·인용·확정한 이름(--terms)이 수정본 어디에도 없게 사라졌거나
    원문에 없던 값이 생긴 곳, (2) 의미 검수 후보: 부정·이미/아직·모두/오직·두 이름의 역할·한 문장 안 인과가 글자로 뒤집힌 듯한 곳을 모은다.
    (2)는 도구의 추측이라 막지 않는다. 문체 신호(낱말 바꿈, 지운 말, 새 말)는 보이지 않는다."""
    hard, cand = [], []
    for it in risk_items(src_text, out_text, genre, terms, dialogue, protected_text):
        fw = [w for w in it["why"] if w.startswith("보호 항목") or "(--terms)" in w or "(--protected-text)" in w]
        ck = [k for k in it["kinds"] if k in FACTS_CANDIDATE_KINDS]
        row = {"where": it["where"], "src": it["src"], "out": it["out"]}
        if fw:
            hard.append(dict(row, why=fw))
        if ck:
            cw = [w for w in it["why"] if w not in fw and not w.startswith(("사라진 말", "새 말", "바뀐 낱말", "따로이던"))]
            cand.append(dict(row, kinds=ck, why=cw[:3]))
    return {"facts": hard, "candidates": cand}


def print_facts(r):
    print("# 사실 대조 — 사실 불일치 %d건 · 의미 검수 후보 %d건" % (len(r["facts"]), len(r["candidates"])))
    if r["facts"]:
        print("\n## 사실 불일치\n글자로 확인된 값의 변화다. 검수자가 판정한다: 사용자가 바꾸라고 한 값, 되풀이를 한 번으로 줄인 값, 초안의 작업 메모에만 있던 값, "
              "요청이 걷으라고 한 문장 속 따옴표, 같은 값을 한글 수로 쓴 것(47세 → 마흔일곱 살), 장면 속 인물 수처럼 원문에서 바로 따라 나오는 수는 문제가 아니다. "
              "그 밖에는 수정본에서 그 문장을 다시 써 원문의 값과 맞춘다(원문 문장을 통째로 되붙이지 않는다).")
        for x in r["facts"]:
            print("- [%s] %s\n    원문: %s\n    수정: %s" % (x["where"], " / ".join(x["why"]), snip(x["src"], 120), snip(x["out"], 120) or "(지움)"))
    if r["candidates"]:
        print("\n## 의미 검수 후보\n도구가 글자로 본 신호일 뿐이다. 의미 검수에서 원문과 나란히 읽고 뜻이 같으면 둔다.")
        for x in r["candidates"]:
            print("- [%s] %s — %s\n    원문: %s\n    수정: %s" % (x["where"], "·".join(x["kinds"]), " / ".join(x["why"])[:200],
                                                        snip(x["src"], 120), snip(x["out"], 120) or "(지움)"))
    if not r["facts"] and not r["candidates"]:
        print("글자로 확인되는 사실 변화가 없다. 뜻의 보존을 증명하는 것은 아니다: 의미 검수를 한다.")


def main(argv=None):
    try:  # 출력을 head 등으로 잘라도 조용히 끝나게
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="TYPODER Korean Editor 대조·탐지 도구")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("source")
    c.add_argument("rewrite")
    g = sub.add_parser("gate", help="위험 수정 게이트: 사실 항목(숫자·이름·인용·부정·시점·범위·관계·인과 뒤집힘)은 판정 없이 되돌리고, 확인 항목은 기본 유지(REVERT 판정이면 되돌림)한 최종본을 만든다")
    g.add_argument("source"); g.add_argument("rewrite")
    g.add_argument("--genre", default="일반")
    g.add_argument("--dialogue", choices=["auto", "edit", "protect"], default="auto")
    g.add_argument("--terms"); g.add_argument("--protected-text")
    g.add_argument("--decisions", help="판정 JSON: {\"C1a2b3c\": {\"decision\": \"REVERT\", \"reason\": \"…\"}, …}. 사실 항목 유지는 override(user|source)+override_evidence")
    g.add_argument("--template", help="게이트 항목의 판정 틀(JSON)을 이 경로에 쓴다(사실 항목은 빈 판정·override 칸, 확인 항목은 KEEP)")
    g.add_argument("-o", "--output", help="최종본을 쓸 경로(주지 않으면 위험 항목만 보인다)")
    g.add_argument("--log", help="판정 표(마크다운)를 쓸 경로")
    c.add_argument("--dialogue", choices=["auto", "edit", "protect"], default="auto",
                   help="따옴표 안: auto(소설·웹소설은 대사로 편집 허용, 그 밖은 인용으로 보호)·edit·protect")
    c.add_argument("--review-all", action="store_true", help="바뀐 블록을 모두(수정문·앞뒤 문장) 순서대로 보인다: 원문 없이 수정문만 다시 읽는 마지막 검수용")
    c.add_argument("--with-source", action="store_true", help="--review-all에 원문도 함께 보인다(수정문만 읽은 다음 뜻을 대조할 때)")
    c.add_argument("--genre", choices=list(GENRES), default="일반")
    c.add_argument("--terms", help="보호할 용어(한 줄에 하나): 스타일 시트의 고유명사·용어")
    c.add_argument("--protected-text", help="글자 그대로 지킬 문구(한 줄에 하나): 소설 속 실제 인용(편지·기사·법조문)·승인 문구. 등장마다 비교한다")
    fx = sub.add_parser("facts", help="사실 대조: 숫자·날짜·확정 이름(--terms)·인용 불일치와 부정·범위·시점·인과 후보(의미 검수에서 판단)만 보인다")
    fx.add_argument("source"); fx.add_argument("rewrite")
    fx.add_argument("--genre", choices=list(GENRES), default="일반")
    fx.add_argument("--dialogue", choices=["auto", "edit", "protect"], default="auto")
    fx.add_argument("--terms", help="확정한 이름·용어(한 줄에 하나)")
    fx.add_argument("--protected-text", help="글자 그대로 지킬 문구(한 줄에 하나)")
    fx.add_argument("--json", action="store_true")
    nm = sub.add_parser("names", help="원문에서 이름 후보를 뽑아 보인다(확인한 것을 --terms로 넘긴다)")
    nm.add_argument("text")
    nm.add_argument("--genre", default="일반", choices=sorted(GENRES))
    c.add_argument("--json", action="store_true")
    s = sub.add_parser("scan")
    s.add_argument("text", nargs="+", help="원고 파일. 여러 회차를 주면 회차를 넘는 반복과 회차 비교를 본다")
    s.add_argument("--focus", help="여러 회차 중 후보를 볼 회차(파일 이름, 쉼표로 여럿). 다른 회차는 비교 기준으로만 쓴다")
    s.add_argument("--profile", help="작품별 규칙 파일(JSON): off·weak·strong 규칙 ID, keep_phrases(지킬 표현), genre")
    s.add_argument("--genre", choices=list(GENRES), default="일반")
    s.add_argument("--para-mode", choices=["auto", "blank", "line"], default="auto")
    s.add_argument("--json", action="store_true")
    sub.add_parser("selftest")
    sub.add_parser("rules")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "rules":
        print_rules()
        return 0
    try:
        if args.cmd == "names":
            load_source(args.text)
        elif args.cmd == "scan":
            for x in args.text:
                load_source(x)
        elif args.cmd == "compare":
            load_source(args.source)
            load_source(args.rewrite)
    except (ValueError, OSError) as ex:
        print("오류: %s" % ex, file=sys.stderr)
        return 2
    if args.cmd == "facts":
        try:
            src_t, out_t = load_source(args.source)[0], load_source(args.rewrite)[0]
        except (ValueError, OSError) as ex:
            print("오류: %s" % ex, file=sys.stderr)
            return 3
        terms_ = [t.strip() for t in read_text_any(args.terms).splitlines() if t.strip()] if args.terms else []
        ptext_ = [t.strip() for t in read_text_any(args.protected_text).splitlines() if t.strip() and not t.strip().startswith("#")] if args.protected_text else []
        r = facts_report(src_t, out_t, args.genre, terms_, args.dialogue, ptext_)
        print(json.dumps(r, ensure_ascii=False, indent=1)) if args.json else print_facts(r)
        return 2 if r["facts"] else (1 if r["candidates"] else 0)
    if args.cmd == "gate":
        src_t, out_t = load_source(args.source)[0], load_source(args.rewrite)[0]
        terms_ = [t.strip() for t in read_text_any(args.terms).splitlines() if t.strip()] if args.terms else []
        ptext_ = [t.strip() for t in read_text_any(args.protected_text).splitlines() if t.strip() and not t.strip().startswith("#")] if args.protected_text else []
        kw = dict(genre=args.genre, terms=terms_, dialogue=args.dialogue, protected_text=ptext_)
        items = risk_items(src_t, out_t, **kw)
        if args.template:
            Path(args.template).write_text(json.dumps({it["id"]: dict(
                ({"decision": "", "reason": "", "override": "", "override_evidence": ""} if it["fact"] else {"decision": "KEEP", "reason": ""}),
                grade=it["grade"], where=it["where"], kinds=it["kinds"], why=it["why"][:3], src=it["src"], out=it["out"]) for it in items},
                ensure_ascii=False, indent=1), encoding="utf-8")
        if not args.output:
            nf = sum(1 for it in items if it["fact"])
            print("# 게이트 항목 %d건 — 사실 %d건(판정 없이 되돌린다. 사용자 지시·출처가 있을 때만 override와 근거로 유지) · 확인 %d건(기본 유지. 읽고 뜻이 바뀌었으면 REVERT)"
                  % (len(items), nf, len(items) - nf))
            for it in items:
                print("- %s [%s] %s%s\n    원문: %s\n    수정: %s\n    근거: %s" % (it["id"], it["where"], "·".join(it["kinds"]),
                      " (사실: 판정 없이 되돌림)" if it["fact"] else " (확인: 기본 유지)", snip(it["src"], 100),
                      snip(it["out"], 100) or "(지움)", " / ".join(it["why"])[:200]))
            return 1 if items else 0
        dec = json.loads(read_text_any(args.decisions)) if args.decisions else {}
        final, log = gate_apply(src_t, out_t, dec, **kw)
        Path(args.output).write_text(final, encoding="utf-8")
        md = "# 게이트 판정\n\n" + (gate_log_md(log) if log else "게이트 항목 없음") + "\n"
        if args.log:
            Path(args.log).write_text(md, encoding="utf-8")
        cnt = Counter()
        for x in log:
            cnt[("함께 되돌림" if x.get("grade") == "보존" else ("사실 " if x.get("fact") else "확인 ") + ("유지" if x["decision"] == "KEEP" else "되돌림"))] += 1
        print(md)
        left = [it["id"] for it in risk_items(src_t, final, **kw) if it["fact"]]
        print("사실 되돌림 %d · 사실 유지(override) %d · 확인 유지 %d · 확인 되돌림(판정) %d · 함께 되돌림 %d → 최종본: %s. "
              "최종본에 남은 사실 항목은 override로 유지한 것뿐이어야 한다: %d건"
              % (cnt["사실 되돌림"], cnt["사실 유지"], cnt["확인 유지"], cnt["확인 되돌림"], cnt["함께 되돌림"], args.output, len(left)))
        print("다음: compare --review-all로 최종본의 나머지 수정을 읽는다. 확인 항목 가운데 뜻이 바뀐 것은 판정 파일에 REVERT를 적어 다시 돌린다.")
        return 0
    if args.cmd == "names":
        c = name_candidates(nfc(load_source(args.text)[0]), args.genre in ("소설", "웹소설", "에세이"))
        print("# 이름 후보 (확인한 것만 한 줄에 하나씩 --terms 파일에 넣는다)")
        for n, k in c.most_common():
            print("%s\t%d" % (n, k))
        return 0
    if args.cmd == "scan":
        loaded = [load_source(x) for x in args.text]
        texts, labels = [t for t, _l in loaded], [l for _t, l in loaded]
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8")) if args.profile else None
        focus = [x for x in (args.focus or "").split(",") if x.strip()] or None
        genre = (profile or {}).get("genre") if args.genre == "일반" and profile else args.genre
        genre = genre or args.genre
        if len(texts) > 1:
            try:
                rep = scan(texts, genre, args.para_mode, names=[Path(x).stem for x in args.text], profile=profile,
                           labels=labels, focus=focus)
            except ValueError as ex:
                print("오류: %s" % ex, file=sys.stderr)
                return 2
        else:
            rep = scan(texts[0], genre, args.para_mode, profile=profile, labels=labels[0])
        print(json.dumps(rep, ensure_ascii=False, indent=1)) if args.json else print_scan(rep)
        return 0
    terms = []
    if args.terms:
        terms = [t.strip() for t in read_text_any(args.terms).splitlines() if t.strip()]
    ptext = []
    if getattr(args, "protected_text", None):
        ptext = [t.strip() for t in read_text_any(args.protected_text).splitlines() if t.strip() and not t.strip().startswith("#")]
    r = compare(load_source(args.source)[0], load_source(args.rewrite)[0],
                args.genre, terms, args.dialogue, ptext)
    if getattr(args, "review_all", False):
        r["all_blocks"] = change_blocks(load_source(args.source)[0], load_source(args.rewrite)[0])
        r["review_with_source"] = bool(getattr(args, "with_source", False))
    print(json.dumps(r, ensure_ascii=False, indent=1)) if args.json else print_compare(r)
    return exit_code(r)


if __name__ == "__main__":
    sys.exit(main())
