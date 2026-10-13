#!/usr/bin/env python3
"""consistency.py - TYPODER Korean Editor 장편 일관성 점검.

여러 회차(또는 긴 원고 하나)를 함께 읽고 어긋남 후보와 대조표를 만든다. 판정이 아니다.
  1. 이름·용어 표기: 이름 변이(지우/래나), 띄어쓰기 흔들림(그다음/그 다음), 스타일 시트의 금지 변형
  2. 호칭: 서술 속 호칭 섞임(아버지/아빠), 인물별로 같은 상대를 부르는 말의 높낮이 흔들림(카딘 님/카딘)
  3. 인물 사이 존댓말·반말: 부름말이 있는 대사로 본 관계, 회차별 말투 변화
  4. 설정 대조표: 나이·연도·기간 언급을 나란히 놓고, 같은 회차의 다른 나이·같은 기간의 다른 표기를 표시

사용:
  python consistency.py check 1화.txt 2화.txt 3화.txt [--sheet 스타일시트.md] [-o 보고서.md] [--json]
  python consistency.py sheet 1화.txt 2화.txt 3화.txt -o 스타일시트_초안.md
화자는 대사 앞뒤 서술로 추정하므로 틀릴 수 있다. 표시된 곳은 원고를 읽고 판단한다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fidelity_check as fc  # noqa: E402

PARTICLES = sorted(("으로도", "으로는", "으로써", "으로서", "로도", "로는", "로써", "로서", "에서도", "에서는", "에게도", "에게는", "까지도", "부터는",
                    "에서부터", "에는", "에도", "과는", "와는", "이라도", "라도", "에게서", "에게", "한테", "께서", "으로", "에서", "까지", "부터", "처럼", "보다", "이랑", "하고", "이나", "이며",
                    "이야", "이여", "이었다", "이었고", "이었지만", "이다", "이고", "이라", "였다", "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만", "야", "랑"),
                   key=len, reverse=True)
KIN = set("아버지 어머니 아빠 엄마 할아버지 할머니 할아범 할멈 아범 어멈 형 형님 오빠 오라버니 언니 누나 누님 동생 아들 딸 남편 아내 여보 "
          "스승 스승님 사부 사부님 선생 선생님 주인 주인님 도련님 아가씨 전하 폐하 나리 마님 아저씨 아주머니 이모 삼촌 고모 숙부 백부".split())
COMMON_PERSON = set("바람 하늘 문 불 빛 비 눈발 그림자 누군가 사내 노인 소년 소녀 여인 여자 남자 청년 병사 기사 상인 손님 아이 아이들 사람 사람들 다들 모두 둘 셋 그녀 그들 "
                    "우리 너희 저희 자기 녀석 놈 년 이들 그놈 저놈 목소리 소리 얼굴 마음".split())
HON_UP = ("님", "씨", "경", "공", "선배님", "형님", "도련님", "아가씨", "전하", "폐하", "나리", "마님", "선생님", "사부님", "누님", "오라버니")
HON = r"(?:선배님|형님|도련님|아가씨|전하|폐하|나리|마님|선생님|사부님|누님|오라버니|님|씨|경|공|군|양|선배|형|오빠|언니|누나)"
VOC_HEAD = re.compile(r"^\s*(?:(?:아|저|어|야|자)(?:\s*,\s*|\s+))?([가-힣]{1,6}?)(\s?" + HON + r")?(야|아|여|이여)?\s?[,!…]")
VOC_TAIL = re.compile(r"[,…]\s?([가-힣]{1,6}?)(\s?" + HON + r")?(야|아)?[.!?…~]*$")
NUM_NATIVE_TENS = {"열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50, "예순": 60, "일흔": 70, "여든": 80, "아흔": 90}
NUM_NATIVE_ONES = {"하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "석": 3, "서": 3, "넷": 4, "네": 4, "넉": 4, "다섯": 5, "여섯": 6,
                   "일곱": 7, "여덟": 8, "아홉": 9}
SINO = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
SPACING_SKIP = {"한번", "잘못", "안되", "못하", "안돼", "못해", "이번", "그때", "이때"}
# 의존 명사와 어미가 모양이 같은 짝(하는 데/하는데, 갈 지/갈지, 그런 바/그런바)은 둘 다 맞는 말이라 흔들림으로 보지 않는다.
SPACING_DEP = {"데", "지", "바", "만큼", "대로", "듯", "뿐", "걸", "게"}
CONFUSABLE = {frozenset(p) for p in (("ㅐ", "ㅔ"), ("ㅒ", "ㅖ"), ("ㅚ", "ㅙ"), ("ㅙ", "ㅞ"), ("ㅚ", "ㅞ"), ("ㄱ", "ㄲ"), ("ㄷ", "ㄸ"), ("ㅂ", "ㅃ"),
                                         ("ㅅ", "ㅆ"), ("ㅈ", "ㅉ"), ("ㅌ", "ㄸ"), ("ㅋ", "ㄲ"), ("ㅍ", "ㅃ"), ("ㅊ", "ㅉ"), ("ㅓ", "ㅕ"),
                                         ("ㅗ", "ㅛ"), ("ㅜ", "ㅠ"), ("ㅡ", "ㅜ"), ("ㅔ", "ㅖ"), ("ㅐ", "ㅒ"))}
CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ",
        "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]


# ---------------------------------------------------------------- 공통
def load(paths, para_mode="auto"):
    """.hwpx·.docx는 원본을 직접 읽고 편집 도구와 같은 번호로 위치를 보인다(fidelity_check.load_source)."""
    loaded = [fc.load_source(p) for p in paths]
    texts, labels = [t for t, _l in loaded], [l for _t, l in loaded]
    names = [Path(p).stem for p in paths]
    joined = "".join("\n\n⟦FILE:%d⟧\n\n%s" % (i, t) for i, t in enumerate(texts))
    doc = fc.Doc(joined, para_mode)
    doc.names = names if len(paths) > 1 else None
    fc.attach_labels(doc, labels if len(paths) > 1 else labels[0], len(paths))
    return doc


def stem(tok, min_len=2):
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", tok)
    for p in PARTICLES:  # 긴 조사부터
        if t.endswith(p):
            if len(t) - len(p) >= min_len:
                return t[:-len(p)], p
            if p[0] in "으이":  # 매개 모음('으로'의 '으', '이나'의 '이')으로 시작하는 조사가 맞았는데 어간이 짧으면
                return t, ""      # 더 짧은 조사를 떼지 않는다('집으로' → '집으' 방지). '사과는'(과는 ✗ → 는 ✓)은 계속 본다
    return t, ""


def jamo(word):
    out = []
    for ch in word:
        c = ord(ch) - 0xAC00
        if 0 <= c < 11172:
            out += [CHO[c // 588], JUNG[(c % 588) // 28]] + ([JONG[c % 28]] if c % 28 else [])
        else:
            out.append(ch)
    return out


def one_edit(a, b):
    """자모 편집 거리가 1이면 (종류, 앞, 뒤)를, 아니면 None."""
    ja, jb = jamo(a), jamo(b)
    if ja == jb or abs(len(ja) - len(jb)) > 1:
        return None
    if len(ja) == len(jb):
        diff = [(x, y) for x, y in zip(ja, jb) if x != y]
        return ("바꿈", diff[0][0], diff[0][1]) if len(diff) == 1 else None
    short, long_ = (ja, jb) if len(ja) < len(jb) else (jb, ja)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return ("더함", "", long_[i])
    return None


def native_or_sino(word):
    """'서른셋'·'열두'·'아홉'·'삼십오'·'12' → 정수. 못 읽으면 None."""
    w = word.strip()
    if w.isdigit():
        return int(w)
    total, rest = 0, w
    for k, v in sorted(NUM_NATIVE_TENS.items(), key=lambda x: -len(x[0])):
        if rest.startswith(k):
            total, rest = v, rest[len(k):]
            break
    if rest in NUM_NATIVE_ONES:
        return total + NUM_NATIVE_ONES[rest]
    if rest == "" and total:
        return total
    if re.fullmatch(r"[일이삼사오육칠팔구]?십?[일이삼사오육칠팔구]?", w) and w:
        n, m = 0, re.fullmatch(r"([일이삼사오육칠팔구]?)(십?)([일이삼사오육칠팔구]?)", w)
        if m.group(2):
            n = (SINO.get(m.group(1), 1)) * 10
        elif m.group(1) and not m.group(3):
            return SINO[m.group(1)]
        return n + SINO.get(m.group(3), 0) if n else None
    return None


# ---------------------------------------------------------------- 인물·대사
def find_names(doc):
    """이름 후보: 세 번 이상 나오고, 말하기 동사의 주어·높임 호칭·부름말로 한 번 이상 쓰인 말."""
    body = "\n".join(doc.paras)
    total, ctx = Counter(), Counter()
    for _pi, s in doc.flat:
        for tok in s.split():
            st, _p = stem(tok)
            if len(st) >= 2:
                total[st] += 1
    for m in re.finditer(r"([가-힣]{2,5})(?:은|는|이|가)\s[^“\"\n.?!]{0,15}" + fc.SPEECH_VERB, body):
        ctx[m.group(1)] += 1
    for m in re.finditer(r"([가-힣]{2,4}) ?(?:님|씨|경|공|선배)(?=[\s,.!?…”\"])", body):
        ctx[m.group(1)] += 1
    for m in re.finditer(r"[“\"]([가-힣]{2,4})(?:야|아)[,!]", body):
        ctx[m.group(1)] += 1
    act = r"(?:고개|입|입술|손|눈|어깨|얼굴|목소리|턱|웃|말|물었|대답|중얼|돌아보|돌아봤|끄덕|걸어|걸었|앉|일어|울|속삭|소리쳤|한숨|숨을)"
    for pi, s in doc.flat:
        if doc.quoted_sent(pi, s):
            continue
        m = re.match(r"([가-힣]{2,5})(?:은|는|이|가)\s([^.?!]{0,25}?)" + act, s)
        if m and not re.search(r"[가-힣](?:은|는|이|가)\s", m.group(2)):
            ctx[m.group(1)] += 1
    for m in re.finditer(r"([가-힣]{2,5})의 (?:얼굴|손|눈|목소리|말|어깨|입|머리|어머니|아버지|집|방)", body):
        ctx[m.group(1)] += 1
    bad = KIN | COMMON_PERSON | fc.NOT_NAME
    return {n for n, c in ctx.items() if c >= 2 and total[n] >= 3 and n not in bad}, total


def speaker_pool(doc, names):
    subj = Counter()
    for pi, s in doc.flat:
        m = re.match(r"([가-힣]{1,5})(?:은|는|이|가)\s", s)
        if m and not doc.quoted_sent(pi, s):
            subj[m.group(1)] += 1
    return names | {k for k in KIN if subj[k] >= 3}


def dialogue_lines(doc, pool):
    """[(문단, 화자 또는 None, 대사)] 화자는 대사 앞뒤 서술로 추정."""
    out = []
    speech = re.compile(r"([가-힣]{1,5})(?:은|는|이|가)\s[^“\"]{0,15}" + fc.SPEECH_VERB)
    for pi, par in enumerate(doc.paras):
        for mo in re.finditer(r"[“\"]([^”\"]{1,300})[”\"]", par):
            who = None
            for zone in (par[mo.end():mo.end() + 40], par[max(0, mo.start() - 40):mo.start()]):
                m = speech.search(zone)
                if m and m.group(1) in pool:
                    who = m.group(1)
                    break
            if not who:
                for dp in (pi + 1, pi - 1):
                    if 0 <= dp < len(doc.paras) and doc.file_of[dp] == doc.file_of[pi] \
                            and not doc.paras[dp].lstrip().startswith(("“", "\"")):
                        m = re.match(r"([가-힣]{1,5})(?:은|는|이|가)\s", doc.paras[dp])
                        if m and m.group(1) in pool:
                            who = m.group(1)
                            break
            out.append((pi, who, mo.group(1)))
    return out


KIN_CANON = {"엄마": "어머니", "어머님": "어머니", "아빠": "아버지", "아버님": "아버지", "할매": "할머니", "할배": "할아버지",
             "오라버니": "오빠", "오라비": "오빠"}


def vocative(line, names):
    """대사 속 부름말: (기준 이름, 높임 여부, 실제 형태) 또는 None."""
    for rx in (VOC_HEAD, VOC_TAIL):
        m = rx.search(line)
        if not m:
            continue
        base, hon, suf = m.group(1), (m.group(2) or "").strip(), m.group(3) or ""
        if (base + hon) in KIN:
            base, hon = base + hon, ""
        if base in names or base in KIN or (hon and len(base) >= 2 and base not in COMMON_PERSON | fc.NOT_NAME):
            form = base + ((" " + hon) if hon in ("님", "씨", "경", "공", "군", "양") else hon) + suf
            key = KIN_CANON.get(base, base) if base in KIN else base  # 같은 사람을 가리키는 친족 호칭은 한 대상으로
            return key, hon in HON_UP or base in ("도련님", "아가씨", "전하", "폐하", "나리", "마님", "선생님", "사부님", "형님", "누님"), form
    return None


# ---------------------------------------------------------------- 1. 표기
def name_variants(doc, names, total):
    out = []
    locs = defaultdict(list)
    for pi, s in doc.flat:
        for tok in s.split():
            st, _p = stem(tok)
            if len(st) >= 2:
                locs[st].append(pi)
    for n in sorted(names, key=lambda x: -total[x]):
        for w, c in total.items():
            if w == n or c > max(2, total[n] // 10) or w in names or len(w) < 2:
                continue
            e = one_edit(n, w)
            if not e:
                continue
            if len(n) <= 2 and (e[0] != "바꿈" or frozenset((e[1], e[2])) not in CONFUSABLE):
                continue  # 두 글자 이름은 헷갈리기 쉬운 자모 바꿈만(입안≠도안)
            out.append({"main": n, "main_count": total[n], "variant": w, "count": c,
                        "edit": "%s→%s" % (e[1] or "∅", e[2]), "locs": [doc.loc(p) for p in locs[w][:5]]})
    return out


def similar_names(doc, names, total):
    """인물 후보끼리 자모 하나만 다른 쌍(세린/세릭). 한 회차에서 이름을 계속 잘못 쓰면 오타도 인물 후보가 되어
    name_variants가 비교하지 않는다. 서로 다른 인물일 수 있으니 막지 않고 확인 목록으로만 보인다."""
    locs = defaultdict(list)
    for pi, x in doc.flat:
        for tok in x.split():
            st, _p = stem(tok)
            if st in names:
                locs[st].append(pi)
    ns = sorted(names, key=lambda x: -total[x])
    out = []
    for i, a in enumerate(ns):
        for b in ns[i + 1:]:
            e = one_edit(a, b) if len(a) == len(b) else None
            if e:
                files_a = {doc.file_of[q] for q in locs[a]}
                files_b = {doc.file_of[q] for q in locs[b]}
                out.append({"a": a, "a_count": total[a], "b": b, "b_count": total[b], "edit": "%s→%s" % (e[1] or "∅", e[2]),
                            "b_locs": [doc.loc(q) for q in locs[b][:5]], "separate_files": not (files_a & files_b)})
    return out


def term_variants(doc, names, total):
    """이름이 아닌 명사의 헷갈리는 자모 한 개 차이(예: 컨텐츠/콘텐츠 류). 흔한 쪽 3회 이상, 드문 쪽 2회 이하."""
    out = []
    nounish = set()
    for _pi, s in doc.flat:
        for tok in s.split():
            st, p = stem(tok)
            if p and p not in ("야", "이여", "이야", "하고", "이며"):
                nounish.add(st)
    verbal = re.compile(r"(?:다|고|며|서|면|지|게|요|니|네|죠|어|아|여|워|자|까)$")
    frequent = [w for w, c in total.items() if c >= 3 and len(w) >= 3 and w not in names and w in nounish
                and not verbal.search(w) and re.fullmatch(r"[가-힣]+", w)]
    rare = [w for w, c in total.items() if c <= 2 and len(w) >= 3 and not verbal.search(w) and re.fullmatch(r"[가-힣]+", w)]
    by_len = defaultdict(list)
    for w in rare:
        by_len[len(w)].append(w)
    for f in frequent:
        for w in by_len[len(f)]:
            e = one_edit(f, w)
            if e and e[0] == "바꿈" and frozenset((e[1], e[2])) in CONFUSABLE and total[w] * 3 <= total[f]:
                out.append({"main": f, "main_count": total[f], "variant": w, "count": total[w], "edit": "%s→%s" % (e[1], e[2])})
    return out


def spacing_variants(doc):
    joined, split, where = Counter(), Counter(), defaultdict(list)
    for pi, s in doc.flat:
        toks = s.split()
        for t in toks:
            st, _p = stem(t)
            joined[st] += 1
        for a, b in zip(toks, toks[1:]):
            if not re.fullmatch(r"[가-힣]{1,4}", a):
                continue
            bs, _p = stem(b, min_len=1)  # '흰 죽이었다'의 '죽'처럼 한 글자 명사도
            if re.fullmatch(r"[가-힣]{1,4}", bs) and bs not in SPACING_DEP:
                key = a + bs
                split[key] += 1
                where[key].append(pi)
    out = []
    for key, n in split.items():
        if joined[key] and 2 <= len(key) <= 6 and key not in SPACING_SKIP and not any(key.startswith(x) for x in SPACING_SKIP):
            out.append({"joined": key, "joined_count": joined[key], "split_count": n, "locs": [doc.loc(p) for p in where[key][:5]]})
    return sorted(out, key=lambda x: -(x["joined_count"] + x["split_count"]))


def parse_sheet(path):
    """스타일 시트(마크다운 표 또는 JSON)에서 {채택 표기: [금지 변형]}을 읽는다."""
    text = fc.read_text_any(path)
    rules = {}
    if path.endswith(".json"):
        data = json.loads(text)
        for k, v in {**data.get("terms", {}), **data.get("names", {})}.items():
            rules[k] = list(v)
        return rules
    header = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
            continue
        if header is None:
            header = cells
            continue
        canon_i = next((i for i, h in enumerate(header) if "채택" in h or h in ("표기", "쓸 표기")), 0)
        var_i = next((i for i, h in enumerate(header) if "변형" in h), None)
        if var_i is None or var_i >= len(cells) or canon_i >= len(cells):
            continue
        canon, cell = cells[canon_i], cells[var_i]
        if not canon:
            continue
        if "비고" in header[var_i]:
            variants = re.findall(r"['‘\"“]([^'’\"”]+)['’\"”]\s*(?:아님|금지)", cell)
        else:
            variants = [re.sub(r"['‘’\"“”]", "", v).strip() for v in re.split(r"[,/·、]", cell)]
        variants = [v for v in variants if v and v != canon and v not in ("-", "없음")]
        if variants:
            rules.setdefault(canon, []).extend(variants)
    return rules


def sheet_violations(doc, rules):
    out = []
    for canon, variants in rules.items():
        for v in variants:
            rx = re.compile(r"(?<![가-힣])" + re.escape(v) + r"(?:" + "|".join(PARTICLES) + r")?(?![가-힣])")
            locs = [doc.loc(pi) for pi, s in doc.flat if rx.search(s)]
            if locs:
                out.append({"canon": canon, "variant": v, "count": len(locs), "locs": locs[:6]})
    return out


# ---------------------------------------------------------------- 2·3. 호칭과 말투
KIN_PAIRS = [("아버지", "아빠"), ("어머니", "엄마"), ("할아버지", "할아범"), ("할머니", "할멈"), ("형님", "형"), ("오라버니", "오빠"),
             ("누님", "누나"), ("스승님", "스승"), ("사부님", "사부"), ("선생님", "선생")]


def narration_address(doc):
    """서술문에서 같은 인물을 가리키는 호칭이 섞였는지(아버지/아빠)."""
    cnt, where = Counter(), defaultdict(list)
    for pi, s in doc.flat:
        if doc.quoted_sent(pi, s):
            continue
        for tok in s.split():
            st, _p = stem(tok)
            if st in KIN:
                cnt[st] += 1
                where[st].append(pi)
    out = []
    for a, b in KIN_PAIRS:
        if cnt[a] and cnt[b]:
            minor = a if cnt[a] < cnt[b] else b
            out.append({"forms": {a: cnt[a], b: cnt[b]}, "minor": minor, "locs": [doc.loc(p) for p in where[minor][:6]]})
    return out


def address_and_speech(doc, names, lines):
    by_speaker = defaultdict(lambda: defaultdict(Counter))  # 화자 → 상대 → 형태
    level = defaultdict(lambda: defaultdict(lambda: {"높임": [], "평칭": []}))
    pair = defaultdict(lambda: {"존대": [], "반말": []})
    per_file = defaultdict(lambda: defaultdict(Counter))  # 화자 → 회차 → 말투
    for pi, who, line in lines:
        if not who:
            continue
        fam = fc.speech_family(line)
        if fam != "기타":
            per_file[who][doc.file_of[pi]][fam] += 1
        v = vocative(line, names)
        if not v:
            continue
        base, up, form = v
        if base == who:
            continue
        by_speaker[who][base][form] += 1
        level[who][base]["높임" if up else "평칭"].append((doc.loc(pi), line))
        if fam != "기타":
            pair[(who, base)][fam].append((doc.loc(pi), line))
    shaky_address = [{"speaker": w, "target": t, "forms": dict(by_speaker[w][t]),
                      "minor": min(("높임", "평칭"), key=lambda k: len(lv[k])),
                      "examples": lv[min(("높임", "평칭"), key=lambda k: len(lv[k]))][:3]}
                     for w, targets in level.items() for t, lv in targets.items() if lv["높임"] and lv["평칭"]]
    shaky_pair = []
    rows = []
    for (w, t), fam in sorted(pair.items()):
        rows.append({"speaker": w, "target": t, "존대": len(fam["존대"]), "반말": len(fam["반말"])})
        if fam["존대"] and fam["반말"]:
            minor = "존대" if len(fam["존대"]) < len(fam["반말"]) else "반말"
            shaky_pair.append({"speaker": w, "target": t, "counts": {k: len(v) for k, v in fam.items()}, "minor": minor,
                               "examples": fam[minor][:3]})
    shifts = []
    for w, files in per_file.items():
        dom = {}
        for f, c in files.items():
            known = c["존대"] + c["반말"]
            if known >= 3:
                top = "존대" if c["존대"] >= c["반말"] else "반말"
                if c[top] >= 0.7 * known:
                    dom[f] = (top, c[top], known)
        if len({v[0] for v in dom.values()}) > 1:
            shifts.append({"speaker": w, "by_file": {(doc.names[f] if doc.names else "원고"): "%s %d/%d" % v for f, v in sorted(dom.items())}})
    return {"address": {w: {t: dict(c) for t, c in ts.items()} for w, ts in by_speaker.items()},
            "shaky_address": shaky_address, "pairs": rows, "shaky_pairs": shaky_pair, "shifts": shifts}


# ---------------------------------------------------------------- 4. 설정 대조표
AGE = re.compile(r"(?<![가-힣0-9])(?:(\d{1,3})\s?(?:살|세)|([가-힣]{1,4})\s?살)(?=이었|이라|이고|이다|였|의|에|은|는|도|로|과|와|까지|[\s,.!?…]|$)")  # 고유어 수사 뒤 '세'는 나이가 아니다(마흔세 날)
YEAR = re.compile(r"(?<!\d)((?:1[89]|2[01])\d{2})\s?년|(?<!\d)((?:1[89]|2[01])\d{2})\.\d{1,2}\.\d{1,2}")
SPAN = re.compile(r"(?<![가-힣0-9])(\d{1,3}|[가-힣]{1,4})\s?(년|해)\s?(동안|전|째|만에|뒤|후)")


def settings_table(doc, names, pool):
    ages, years, spans = [], [], []
    who_rx = re.compile(r"([가-힣]{1,5})(?:은|는|이|가|의)\s")
    for pi, s in doc.flat:
        for m in AGE.finditer(s):
            n = native_or_sino(m.group(1) or m.group(2))
            if n is None or not (0 < n < 150):
                continue
            before = s[:m.start()]
            cands = [x for x in who_rx.findall(before) if x in pool]
            prev = re.findall(r"([가-힣]{2,6})\s*$", before)
            if prev and not cands:
                t = prev[-1]
                cands = [x for x in pool if t == x or (t.endswith(x) and len(t) - len(x) == 1)]
            if not cands:  # '열두 살 지우는'처럼 이름이 나이 뒤에 오는 경우
                nxt = re.match(r"\s*(?:인|난|이던|짜리|의)?\s*([가-힣]{1,5}?)(?:은|는|이|가|을|를|의|도|와|과)?(?=[\s,.!?…]|$)", s[m.end():])
                if nxt and nxt.group(1) in pool:
                    cands = [nxt.group(1)]
            y = YEAR.search(s)
            ages.append({"who": cands[-1] if cands else "?", "age": n, "loc": doc.loc(pi), "file": doc.file_of[pi], "pi": pi,
                         "year": int(y.group(1) or y.group(2)) if y else None, "text": fc.snip(s, 60)})
        for m in YEAR.finditer(s):
            years.append({"year": int(m.group(1) or m.group(2)), "loc": doc.loc(pi), "text": fc.snip(s, 60)})
        for m in SPAN.finditer(s):
            n = native_or_sino(m.group(1))
            if n is not None:
                spans.append({"n": n, "form": m.group(0).strip(), "loc": doc.loc(pi)})
    clash = []
    grp = defaultdict(list)
    for a in ages:
        if a["who"] != "?":
            grp[(a["who"], a["file"])].append(a)
    for (w, f), items in grp.items():
        bad = set()
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if a["age"] == b["age"] or abs(a["pi"] - b["pi"]) > 30:
                    continue
                if a["year"] and b["year"] and b["age"] - a["age"] == b["year"] - a["year"]:
                    continue  # 연도가 흐른 만큼 나이가 늘었다
                bad |= {id(a), id(b)}
        if bad:
            clash.append({"who": w, "items": [(x["loc"], x["age"], x["text"]) for x in items if id(x) in bad]})
    span_forms = defaultdict(Counter)
    for sp in spans:
        span_forms[(sp["n"], re.sub(r"\s", "", sp["form"])[-2:])][re.sub(r"\s+", " ", sp["form"])] += 1
    span_clash = [{"n": n, "forms": dict(c)} for (n, _tail), c in span_forms.items() if len(c) > 1]
    return {"ages": ages, "age_clash": clash, "years": sorted(years, key=lambda x: x["year"]), "spans": spans,
            "span_clash": span_clash}


# ---------------------------------------------------------------- 실행
def check(paths, sheet=None, para_mode="auto"):
    doc = load(paths, para_mode)
    names, total = find_names(doc)
    rules = parse_sheet(sheet) if sheet else {}
    for canon in rules:
        if re.fullmatch(r"[가-힣]{2,5}", canon) and total[canon]:
            names.add(canon)
    pool = speaker_pool(doc, names)
    lines = dialogue_lines(doc, pool)
    return {
        "files": doc.names or [Path(paths[0]).stem],
        "names": sorted(((n, total[n]) for n in names), key=lambda x: -x[1]),
        "spelling": {"names": name_variants(doc, names, total), "similar": similar_names(doc, names, total),
                     "terms": term_variants(doc, names, total),
                     "spacing": spacing_variants(doc), "sheet": sheet_violations(doc, rules) if rules else None},
        "address": {"narration": narration_address(doc), **address_and_speech(doc, names, lines)},
        "dialogue": {"lines": len(lines), "attributed": sum(1 for _p, w, _l in lines if w)},
        "settings": settings_table(doc, names, pool),
    }


def jo(word, a, b):
    """받침에 맞는 조사: jo('도현', '이', '가') → '도현이'."""
    c = ord(word[-1]) - 0xAC00 if word else -1
    return word + (a if 0 <= c < 11172 and c % 28 else b)


def render(rep):
    L = ["# 일관성 점검 — %s" % ", ".join(rep["files"]), "",
         "판정이 아니라 어긋남 후보와 대조표다. 화자는 대사 앞뒤 서술로 추정했다(대사 %d줄 중 %d줄)."
         % (rep["dialogue"]["lines"], rep["dialogue"]["attributed"]), "",
         "인물 후보: " + (", ".join("%s(%d)" % x for x in rep["names"][:15]) or "없음"), ""]
    sp = rep["spelling"]
    L += ["## 1. 이름·용어 표기", ""]
    items = ["- 이름 변이: '%s' %d회 ↔ '%s' %d회 (%s) — %s" % (x["variant"], x["count"], x["main"], x["main_count"], x["edit"], ", ".join(x["locs"]))
             for x in sp["names"]]
    items += ["- 비슷한 이름 후보: '%s' %d회 / '%s' %d회 (%s)%s — 서로 다른 인물인지 확인. '%s'이(가) 나온 곳: %s"
              % (x["a"], x["a_count"], x["b"], x["b_count"], x["edit"], " · 서로 다른 회차에만 나옴" if x["separate_files"] else "",
                 x["b"], ", ".join(x["b_locs"])) for x in sp.get("similar", [])]
    items += ["- 용어 변이: '%s' %d회 ↔ '%s' %d회 (%s)" % (x["variant"], x["count"], x["main"], x["main_count"], x["edit"]) for x in sp["terms"]]
    items += ["- 띄어쓰기 흔들림: '%s' %d회 / '%s' %d회 — 띄운 곳: %s"
              % (x["joined"], x["joined_count"], "?", x["split_count"], ", ".join(x["locs"])) for x in sp["spacing"]]
    items = [i.replace("'?'", "띄어 쓴 형태") for i in items]
    if sp["sheet"] is not None:
        items += ["- 스타일 시트 위반: '%s' %d곳(채택: '%s') — %s" % (x["variant"], x["count"], x["canon"], ", ".join(x["locs"])) for x in sp["sheet"]]
    L += items or ["- 없음"]
    ad = rep["address"]
    L += ["", "## 2. 호칭", "", "### 서술 속 호칭 섞임"]
    L += ["- %s — 적은 쪽 '%s': %s" % (" / ".join("%s %d회" % kv for kv in x["forms"].items()), x["minor"], ", ".join(x["locs"]))
          for x in ad["narration"]] or ["- 없음"]
    L += ["", "### 인물별 부르는 말 (대사 속 부름말)"]
    L += ["- %s → %s" % (w, "; ".join("%s%s: %s" % ("⚠ " if len(f) > 1 else "", t, ", ".join("%s %d" % kv for kv in f.items())) for t, f in ts.items()))
          for w, ts in ad["address"].items()] or ["- 부름말을 찾지 못함"]
    if any(len(f) > 1 for ts in ad["address"].values() for f in ts.values()):
        L.append("  - ⚠ 같은 상대를 여러 말로 부른다(회차·관계 변화로 바뀐 것인지 확인)")
    for x in ad["shaky_address"]:
        L.append("  - ⚠ %s %s 높여 부르기도, 낮춰 부르기도 함(적은 쪽 %s): %s" % (
            jo(x["speaker"], "이", "가"), jo(x["target"], "을", "를"), x["minor"], " / ".join("%s “%s”" % (loc, fc.snip(q, 25)) for loc, q in x["examples"])))
    L += ["", "## 3. 인물 사이 존댓말·반말", "", "### 부름말이 있는 대사로 본 관계"]
    L += ["- %s → %s: 존대 %d · 반말 %d" % (x["speaker"], x["target"], x["존대"], x["반말"]) for x in ad["pairs"]] or ["- 없음"]
    for x in ad["shaky_pairs"]:
        L.append("  - ⚠ %s → %s 말투 흔들림(적은 쪽 %s): %s" % (
            x["speaker"], x["target"], x["minor"], " / ".join("%s “%s”" % (loc, fc.snip(q, 25)) for loc, q in x["examples"])))
    L += ["", "### 회차별 말투 변화 (한 회차 안에서 70% 이상인 말투가 회차마다 다름)"]
    L += ["- %s: %s" % (x["speaker"], " → ".join("%s %s" % kv for kv in x["by_file"].items())) for x in ad["shifts"]] or ["- 없음"]
    st = rep["settings"]
    L += ["", "## 4. 설정 대조표", "", "### 나이 (같은 회차에서 다르면 ⚠)"]
    L += ["- ⚠ %s: %s" % (x["who"], " / ".join("%s %d살 “%s”" % i for i in x["items"])) for x in st["age_clash"]]
    by_who = defaultdict(list)
    for a in st["ages"]:
        by_who[a["who"]].append("%s %d" % (a["loc"], a["age"]))
    L += ["- %s: %s" % (w, ", ".join(v[:8])) for w, v in by_who.items()] or ["- 나이 언급 없음"]
    L += ["", "### 연도·날짜"]
    L += ["- %d: %s “%s”" % (y["year"], y["loc"], y["text"]) for y in st["years"][:20]] or ["- 없음"]
    L += ["", "### 기간 (같은 기간을 다르게 적었으면 ⚠)"]
    L += ["- ⚠ %d: %s" % (x["n"], " / ".join("%s %d회" % kv for kv in x["forms"].items())) for x in st["span_clash"]]
    spans = Counter(re.sub(r"\s+", " ", s["form"]) for s in st["spans"])
    L += ["- " + ", ".join("%s %d" % kv for kv in spans.most_common(12))] if spans else ["- 없음"]
    return "\n".join(L) + "\n"


def draft_sheet(rep):
    sp, ad = rep["spelling"], rep["address"]
    variants = defaultdict(list)
    for x in sp["names"]:
        variants[x["main"]].append(x["variant"])
    L = ["# 스타일 시트 초안: %s" % ", ".join(rep["files"]), "",
         "consistency.py가 원고에서 뽑은 초안이다. 다수 표기를 채택안으로 올렸을 뿐이니 확인하고 고쳐 쓴 뒤 `check --sheet`로 넘긴다.", "",
         "## 고유명사·이름", "| 표기 | 종류 | 첫 등장 | 비고(금지 변형) |", "|---|---|---|---|"]
    L += ["| %s | 인물(%d회) | | %s |" % (n, c, " ".join("'%s' 아님" % v for v in variants.get(n, [])) or "확인 필요")
          for n, c in rep["names"][:30]]
    L += ["", "## 용어", "| 채택 표기 | 뜻·맥락 | 쓰지 않는 변형 | 근거 |", "|---|---|---|---|"]
    L += ["| %s | | %s | 원고 다수 표기(%d:%d) — 확인 필요 |" % (x["main"], x["variant"], x["main_count"], x["count"]) for x in sp["terms"]]
    for x in sp["spacing"]:
        a, b = (x["joined"], "띄어 씀") if x["joined_count"] >= x["split_count"] else ("띄어 씀", x["joined"])
        L.append("| %s | | %s | 띄어쓰기 %d:%d — 사전 확인 필요 |" % (
            x["joined"] if a == x["joined"] else "(띄어 쓴 형태)", x["joined"] if b == x["joined"] else "(띄어 쓴 형태)",
            max(x["joined_count"], x["split_count"]), min(x["joined_count"], x["split_count"])))
    L += ["", "## 문체", "- 존댓말·반말: 대사 인물별 (부름말이 있는 대사 기준)"]
    L += ["  - %s → %s: 존대 %d · 반말 %d" % (x["speaker"], x["target"], x["존대"], x["반말"]) for x in ad["pairs"]]
    L += ["- 인물별 부르는 말:"] + ["  - %s → %s" % (w, "; ".join("%s: %s" % (t, ", ".join(f)) for t, f in ts.items()))
                                  for w, ts in ad["address"].items()]
    if ad["narration"]:
        L += ["- 서술 호칭: " + "; ".join(" / ".join("%s %d" % kv for kv in x["forms"].items()) + " — 하나로 정할지 확인" for x in ad["narration"])]
    L += ["", "## 확인 필요", "- 위 표에서 '확인 필요'로 적은 항목", ""]
    return "\n".join(L)



# ---------------------------------------------------------------- 자체 시험(스킬만 받아도 돌릴 수 있게)
_ST_1 = ["지우는 열 살이었다.", "“어머니, 다녀오겠습니다.” 지우가 말했다.", "어머니가 웃었다.", "“도현 오라버니, 기다려 주세요.” 지우가 말했다.",
         "도현이 돌아보았다.", "“빨리 와.” 도현이 말했다.", "세린은 창가에 앉았다.", "세린은 웃었다.", "“가자.” 세린이 말했다.", "세린이 돌아보았다.",
         "도안은 검을 들었다.", "도한은 방패를 들었다.", "“간다.” 도안이 말했다.", "“좋아.” 도한이 말했다.", "도안은 웃었다.", "도한은 웃었다.",
         "열두 살 지우는 혼자 걸었다.", "“어머니, 저 왔어요.” 지우가 말했다."]
_ST_2 = ["“엄마, 다녀올게.” 지우가 말했다.", "“도현, 기다려.” 지우가 말했다.", "도현이 웃었다.", "“그래.” 도현이 말했다.",
         "세릭은 창가에 앉았다.", "세릭은 웃었다.", "“가자.” 세릭이 말했다.", "세릭이 돌아보았다.",
         "도안은 검을 닦았다.", "도한은 방패를 닦았다.", "“끝.” 도안이 말했다.", "“응.” 도한이 말했다."]


def cmd_selftest(args):
    """일관성 점검 회귀 시험: 이름·비슷한 이름·호칭·존댓말·나이. 한 번 결함이 있던 곳을 다시 막는다."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for name, paras in (("1화", _ST_1), ("2화", _ST_2)):
            pth = Path(td, name + ".txt")
            pth.write_text("\n\n".join(paras), encoding="utf-8")
            paths.append(str(pth))
        rep = check(paths)
    sp, ad, stt = rep["spelling"], rep["address"], rep["settings"]
    sim = sp.get("similar", [])
    res = [
        ("같은 이름(지우)은 표기 변이로 잡지 않음", not any("지우" in (x["main"], x["variant"]) for x in sp["names"])),
        ("비슷한 이름: 세린·세릭을 후보로(서로 다른 회차에만 나옴)", any({x["a"], x["b"]} == {"세린", "세릭"} and x["separate_files"] for x in sim)),
        ("비슷한 이름: 같은 회차에 함께 나오는 도안·도한은 후보만(회차 표시 없음)", any({x["a"], x["b"]} == {"도안", "도한"} and not x["separate_files"] for x in sim)),
        ("호칭: 어머니·엄마를 한 상대로 묶음", set(ad["address"].get("지우", {}).get("어머니", {})) >= {"어머니", "엄마"}),
        ("호칭: 도현을 높여(오라버니)·낮춰 부름", any(x["speaker"] == "지우" and x["target"] == "도현" for x in ad["shaky_address"])),
        ("말투: 지우 → 도현 존대·반말 섞임(기다려 주세요 / 기다려)", any(x["speaker"] == "지우" and x["target"] == "도현" for x in ad["shaky_pairs"])),
        ("나이: 같은 회차의 10살·12살 충돌(이름이 나이 뒤에 와도)", any(x["who"] == "지우" for x in stt["age_clash"])),
        ("부름말: 감탄사가 있어도(어, 지우야!) 이름을 잡음", (vocative("어, 지우야!", {"지우"}) or ("",))[0] == "지우"),
    ]
    for name, ok in res:
        print("%s  %s" % ("PASS" if ok else "FAIL", name))
    fails = sum(not ok for _n, ok in res)
    print("일관성 점검 시험 %d건: 통과 %d, 실패 %d" % (len(res), len(res) - fails, fails))
    return 1 if fails else 0

def main(argv=None):
    try:  # 출력을 head 등으로 잘라도 조용히 끝나게
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="TYPODER Korean Editor - 장편 일관성 점검")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="회차 전체의 표기·호칭·말투·설정 어긋남 후보")
    c.add_argument("texts", nargs="+")
    c.add_argument("--sheet", help="스타일 시트(마크다운 표 또는 JSON)")
    c.add_argument("--para-mode", default="auto", choices=["auto", "blank", "line"])
    c.add_argument("-o", "--output")
    c.add_argument("--json", action="store_true")
    s = sub.add_parser("sheet", help="원고에서 스타일 시트 초안 뽑기")
    s.add_argument("texts", nargs="+")
    s.add_argument("--para-mode", default="auto", choices=["auto", "blank", "line"])
    s.add_argument("-o", "--output")
    sub.add_parser("selftest", help="회귀 시험(이름·비슷한 이름·호칭·존댓말·나이)")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return cmd_selftest(args)
    try:
        rep = check(args.texts, getattr(args, "sheet", None), args.para_mode)
    except (ValueError, OSError) as ex:
        print("오류: %s" % ex, file=sys.stderr)
        return 2
    if args.cmd == "check":
        out = json.dumps(rep, ensure_ascii=False, indent=1, default=list) if args.json else render(rep)
    else:
        out = draft_sheet(rep)
    if args.output:
        import os
        ins = list(args.texts) + ([args.sheet] if getattr(args, "sheet", None) else [])
        for x in ins:
            if os.path.exists(args.output) and os.path.exists(x) and os.path.samefile(args.output, x):
                print("오류: 입력 파일(%s)을 덮어쓸 수 없습니다. 다른 이름을 지정하세요." % x, file=sys.stderr)
                return 2
        Path(args.output).write_text(out, encoding="utf-8")
        print("저장: %s" % args.output)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
