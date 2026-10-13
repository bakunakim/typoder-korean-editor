#!/usr/bin/env python3
"""hwpx_tool.py - TYPODER Korean Editor의 HWPX(한글, OWPML·KS X 6101) 읽기·빨간펜 교정 도구.

  inspect FILE              패키지 구조, 구역, 문단 수, 변경 추적 표시(insertBegin 등)와 머리 정보의 변경 추적 요소, 메모 수
  text FILE [--view V]      문단별 텍스트. V = accept(변경 수락본, 기본) | reject(원문) | markup([-삭제-]{+삽입+})
                            --plain 이면 문단 번호 없이 빈 줄로 구분(scan·consistency에 바로 넣을 수 있음)

한글의 진짜 변경 추적 기록 생성은 아직 없다. 대신 글자 모양을 이용한 빨간펜 교정본을 만든다. 공개 스키마로 확인한 것은 <hp:t> 안의 insertBegin·insertEnd·deleteBegin·deleteEnd
표시와 run의 charTcId, p의 paraTcId, head의 trackchangeConfig까지이고, 표시의 속성과 머리 정보의 변경 목록·작성자 형식은
한글이 저장한 샘플로 확인한 뒤 구현한다(references/hwpx.md). 구형 .hwp(바이너리)는 읽지 않는다.
요소는 이름공간 대신 요소 이름으로 찾는다(2011년판 hancom.co.kr, 2021년판 owpml.org 모두).
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    sys.stderr.write("lxml이 필요합니다: pip install lxml\n")
    sys.exit(3)

PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=False)

SPECIAL = {"tab": "\t", "lineBreak": "\n", "nbSpace": "\u00a0", "fwSpace": "\u3000", "hyphen": "-"}
MARKS = {"insertBegin": ("ins", True), "insertEnd": ("ins", False), "deleteBegin": ("del", True), "deleteEnd": ("del", False)}


def local(tag):
    return etree.QName(tag).localname if isinstance(tag, str) else ""


class Hwpx:
    def __init__(self, path):
        self.path = path
        self.zip = zipfile.ZipFile(path)
        self.names = self.zip.namelist()

    def xml(self, name):
        return etree.fromstring(self.zip.read(name), PARSER)

    def sections(self):
        """content.hpf의 spine 순서, 없으면 번호 순서."""
        order = []
        hpf = next((n for n in self.names if n.lower().endswith("content.hpf")), None)
        if hpf:
            root = self.xml(hpf)
            items = {i.get("id"): i.get("href") for i in root.iter() if local(i.tag) == "item"}
            for ref in (e.get("idref") for e in root.iter() if local(e.tag) == "itemref"):
                href = items.get(ref, "")
                full = next((n for n in self.names if n.endswith(href)), None) if href else None
                if full and re.search(r"section\d+\.xml$", full):
                    order.append(full)
        if not order:
            order = sorted((n for n in self.names if re.search(r"Contents/section\d+\.xml$", n)),
                           key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        return order

    def paragraphs(self):
        """[(구역, 문단 요소, 표 안인지)] 문서 순서대로. 표·글상자 안 문단도 포함."""
        out = []
        for sec in self.sections():
            root = self.xml(sec)
            for p in root.iter():
                if local(p.tag) == "p":
                    nested = any(local(a.tag) in ("tc", "subList", "drawText") for a in p.iterancestors())
                    out.append((sec, p, nested))
        return out


def para_text(p, view="accept", state=None):
    """문단 한 개의 텍스트. state는 문단을 넘어 이어지는 삽입·삭제 구간 상태."""
    state = state if state is not None else {"ins": False, "del": False}
    buf = []

    def emit(s):
        if not s:
            return
        if view == "markup":
            if state["del"]:
                buf.append("[-%s-]" % s)
            elif state["ins"]:
                buf.append("{+%s+}" % s)
            else:
                buf.append(s)
        elif view == "accept" and not state["del"]:
            buf.append(s)
        elif view == "reject" and not state["ins"]:
            buf.append(s)

    for run in p:
        if local(run.tag) != "run":
            continue
        for t in run:
            if local(t.tag) != "t":
                continue
            emit(t.text)
            for ch in t:
                name = local(ch.tag)
                if name in MARKS:
                    kind, on = MARKS[name]
                    state[kind] = on
                elif name in SPECIAL:
                    emit(SPECIAL[name])
                emit(ch.tail)
    text = "".join(buf)
    return text.replace("-][-", "").replace("+}{+", "")


def redpen_styles(doc):
    """빨간펜 파일의 글자 모양: 빨간 취소선은 'del', 파란 밑줄은 'ins'."""
    header = doc.xml(next(n for n in doc.names if n.endswith("header.xml")))
    out = {}
    for c in header.iter():
        if local(c.tag) != "charPr":
            continue
        color = (c.get("textColor") or "").upper()
        kind = None
        for x in c:
            if local(x.tag) == "strikeout" and x.get("shape", "NONE") != "NONE" and color == DEL_COLOR:
                kind = "del"
            elif local(x.tag) == "underline" and x.get("type", "NONE") != "NONE" and color == INS_COLOR:
                kind = kind or "ins"
        out[c.get("id")] = kind
    return out


def redpen_para_text(p, styles, which):
    """빨간펜 문단 글자: which='final'이면 지운 글자(빨간 취소선)를 빼고, 'original'이면 넣은 글자(파란 밑줄)를 뺀다."""
    drop = "del" if which == "final" else "ins"
    buf = []
    for run in p:
        if local(run.tag) != "run" or styles.get(run.get("charPrIDRef")) == drop:
            continue
        for ch in run:
            if local(ch.tag) == "t":
                buf.append(ch.text or "")
                for sub in ch:
                    buf.append(SPECIAL_CHAR.get(local(sub.tag), ""))
                    buf.append(sub.tail or "")
    return "".join(buf)


def cmd_text(args):
    doc = Hwpx(args.file)
    if getattr(args, "redpen", None):  # 빨간펜 교정본에서 최종 글(final) 또는 원문(original)을 되살린다: 대조 도구에 넣을 때
        styles = redpen_styles(doc)
        lines = []
        for i, (_sec, p, nested) in enumerate(doc.paragraphs(), 1):
            t = redpen_para_text(p, styles, args.redpen)
            if args.plain:
                if t.strip():
                    lines.append(t)
            else:
                lines.append("%d%s\t%s" % (i, "(표)" if nested else "", t))
        print(("\n\n" if args.plain else "\n").join(lines))
        return 0
    state = {"ins": False, "del": False}
    lines = []
    for i, (_sec, p, nested) in enumerate(doc.paragraphs(), 1):
        t = para_text(p, args.view, state)
        if args.plain:
            if t.strip():
                lines.append(t)
        else:
            lines.append("%d%s\t%s" % (i, "(표)" if nested else "", t))
    print(("\n\n" if args.plain else "\n").join(lines))
    return 0


def cmd_inspect(args):
    doc = Hwpx(args.file)
    info = doc.zip.infolist()
    first = info[0] if info else None
    print("# HWPX 점검: %s" % args.file)
    mt = doc.zip.read("mimetype").decode("utf-8", "replace").strip() if "mimetype" in doc.names else None
    print("- mimetype: %s (첫 항목 %s, 압축 %s)" % (mt, "예" if first and first.filename == "mimetype" else "아니오",
                                                  "안 함" if first and first.compress_type == zipfile.ZIP_STORED else "함"))
    print("- 파트 %d개: %s" % (len(doc.names), ", ".join(n for n in doc.names if not n.startswith("BinData/"))[:400]))
    secs = doc.sections()
    print("- 구역 %d개: %s" % (len(secs), ", ".join(secs)))
    header = next((n for n in doc.names if n.endswith("header.xml")), None)
    if header:
        h = doc.xml(header)
        print("- 머리 정보 이름공간: %s" % h.nsmap)
        tc = [e for e in h.iter() if "track" in local(e.tag).lower() or "tc" == local(e.tag).lower()]
        print("- 머리 정보의 변경 추적 관련 요소 %d개" % len(tc))
        for e in tc[:12]:
            print("    <%s %s> %s" % (local(e.tag), " ".join('%s="%s"' % (local(k), v) for k, v in e.attrib.items()),
                                     (e.text or "").strip()[:40]))
    marks, tcids, memos, paras = {}, 0, 0, 0
    for _sec, p, _n in doc.paragraphs():
        paras += 1
        if p.get("paraTcId") is not None:
            tcids += 1
    for sec in secs:
        root = doc.xml(sec)
        for e in root.iter():
            n = local(e.tag)
            if n in MARKS:
                marks.setdefault(n, []).append(dict((local(k), v) for k, v in e.attrib.items()))
            if n == "run" and e.get("charTcId") is not None:
                tcids += 1
            if n in ("memo", "memoGroup") or (n == "fieldBegin" and (e.get("type") or "").upper() == "MEMO"):
                memos += 1
    print("- 문단 %d개, 메모 %d개, charTcId/paraTcId가 붙은 요소 %d개" % (paras, memos, tcids))
    if marks:
        for n, lst in marks.items():
            print("- %s %d개, 속성 예: %s" % (n, len(lst), lst[0]))
    else:
        print("- 변경 추적 표시 없음")
    return 0


# ================================================================ 빨간펜 교정본(redpen)
# 한글 변경 추적 대신 글자 모양(색·취소선·밑줄)으로 수정을 보여 준다. 맥·윈도우 한글 모두 기본 글자 모양만 쓰므로 같은 파일을 연다.
import copy
import json
import os
from difflib import SequenceMatcher

OBJ = "\ufffc"  # 문단 안의 개체(구역 설정·표·그림 등) 자리. 편집으로 옮기거나 지울 수 없다
SPECIAL_CHAR = {"tab": "\t", "lineBreak": "\n", "nbSpace": "\u00a0", "fwSpace": "\u3000", "hyphen": "\u00ad"}
CHAR_SPECIAL = {v: k for k, v in SPECIAL_CHAR.items()}
DEL_COLOR, INS_COLOR = "#FF0000", "#0000FF"


class EditError(Exception):
    pass


def _atoms(p):
    """문단 → [('c', 글자, run) | ('o', 요소, run)]. 글자 하나·개체 하나가 텍스트 위치 하나."""
    atoms = []
    for run in p:
        if local(run.tag) != "run":
            continue
        for ch in run:
            if local(ch.tag) == "t":
                atoms += [("c", c, run) for c in (ch.text or "")]
                for sub in ch:
                    sn = local(sub.tag)
                    if sn not in SPECIAL_CHAR:
                        raise EditError("형광펜·제목 표시·변경 추적 표시(%s)가 있는 문단은 고치지 않습니다" % sn)
                    atoms.append(("c", SPECIAL_CHAR[sn], run))
                    atoms += [("c", c, run) for c in (sub.tail or "")]
            else:
                atoms.append(("o", ch, run))
    return atoms


def _text(atoms):
    return "".join(a[1] if a[0] == "c" else OBJ for a in atoms)


def _load(path):
    doc = Hwpx(path)
    roots = {sec: doc.xml(sec) for sec in doc.sections()}
    paras = [(sec, p) for sec, root in roots.items() for p in root.iter() if local(p.tag) == "p"]
    return doc, roots, paras


def redpen_hunks(a, b):
    """a→b 수정 묶음. 띄어쓰기만 다른 곳은 글자 단위로 다시 나눠 '한 칸 추가·삭제'로 보이게 한다."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import track_changes as tc
    merged = []
    for s, e, t in tc.diff_hunks(a, b, "word"):
        if merged and s - merged[-1][1] <= 2:
            ps, pe, pt = merged[-1]
            merged[-1] = (ps, e, pt + a[pe:s] + t)
        else:
            merged.append((s, e, t))
    out = []
    for s, e, t in merged:
        old = a[s:e]
        if re.sub(r"\s", "", old) == re.sub(r"\s", "", t):
            for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, t, autojunk=False).get_opcodes():
                if tag != "equal":
                    out.append((s + i1, s + i2, t[j1:j2]))
        else:
            out += [(s + s2, s + e2, t2) for s2, e2, t2 in (tc.diff_hunks(old, t, "word") or [(0, len(old), t)])]
    return out


VISIBLE = re.compile(r"[^\s.,!?…·:;'\"“”‘’()\[\]{}<>~\-]")


def expand_for_display(a, hunks):
    """빨간펜 표시용. 띄어쓰기·문장부호만 바뀐 수정은 색이 보이지 않으므로 앞뒤 어절까지 넓혀
    '옛 어절(빨간 취소선) + 새 어절(파란 밑줄)'로 보이게 한다. 글자가 바뀐 수정은 그대로 둔다."""
    spans = []
    for s, e, t in sorted(hunks):
        if VISIBLE.search(a[s:e]) or VISIBLE.search(t):
            spans.append([s, e, [(s, e, t)]])
            continue
        l, r = s, e
        while l > 0 and not a[l - 1].isspace() and a[l - 1] != OBJ:
            l -= 1
        while r < len(a) and not a[r].isspace() and a[r] != OBJ:
            r += 1
        spans.append([l, r, [(s, e, t)]])
    merged = []
    for sp in sorted(spans, key=lambda x: (x[0], x[1])):
        if merged and sp[0] < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], sp[1])
            merged[-1][2] += sp[2]
        else:
            merged.append(sp)
    out = []
    for l, r, hs in merged:
        new, pos = "", l
        for s, e, t in sorted(hs):
            new += a[pos:s] + t
            pos = e
        out.append((l, r, new + a[pos:r]))
    return out


def _hunks_keep_objects(old, new):
    if [c for c in old if c == OBJ] != [c for c in new if c == OBJ]:
        raise EditError("문단 안 개체(구역 설정·표·그림) 자리가 달라졌습니다")
    hunks, base = [], 0
    for so, sn in zip(old.split(OBJ), new.split(OBJ)):
        hunks += [(base + s, base + e, t) for s, e, t in redpen_hunks(so, sn)]
        base += len(so) + 1
    return hunks


GATE_LOG = []  # 마지막 redpen의 위험 수정 판정(보고서에 싣는다)


def _spec_list(spec, key):
    """편집 목록의 보호 목록을 문자열/배열 어느 쪽이든 문자열 배열로 정규화한다."""
    v = spec.get(key) or []
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def resolve_edits(paras, spec):
    """편집 목록 → {문단 번호: 새 글}, 문단별 편집 기록. 문단 번호는 `text` 명령의 번호(1부터)."""
    atoms = {}
    cur, log = {}, []
    texts = {}

    def text_of(i):
        if i not in texts:
            atoms[i] = _atoms(paras[i][1])
            texts[i] = _text(atoms[i])
        return cur.get(i, texts[i])

    del GATE_LOG[:]  # 해석할 때마다 새로(빨간펜·수정본을 만들며 두 번 해석해도 기록은 한 번)
    gated_paras = {}  # 게이트가 되돌린 수정이 있는 문단 → 그 수정 번호(뒤 수정이 앞 수정에 기대면 함께 되돌린다)
    for n, e in enumerate(spec.get("edits", []), 1):
        if "para" in e:
            i = int(e["para"]) - 1
            if not 0 <= i < len(paras):
                raise EditError("%d번 편집: 문단 %s이 없습니다(전체 %d)" % (n, e["para"], len(paras)))
        elif "find" in e:
            hits = [k for k in range(len(paras)) if e["find"] in _plain(paras[k][1], cur.get(k))]
            if len(hits) != 1:
                raise EditError("%d번 편집: '%s'가 든 문단이 %d개입니다(1개여야 함). para로 지정하세요" % (n, e["find"], len(hits)))
            i = hits[0]
        else:
            raise EditError("%d번 편집: para 또는 find가 필요합니다" % n)
        before = text_of(i)
        if e.get("op") == "comment":
            log.append({"n": n, "para": i + 1, "old": "", "new": "", "note": e.get("text", ""), **_why(e)})
            continue
        anchor_missing = ("old" in e and "new_text" not in e and before.count(e["old"]) == 0) or \
            ("new_text" not in e and "old" not in e and "text" in e and before.count(e.get("after") or e.get("before") or "") != 1)
        if anchor_missing and i in gated_paras:  # 앞 수정이 게이트에 되돌려져 이 수정이 찾을 글이 없다: 멈추지 않고 함께 되돌린다
            GATE_LOG.append({"id": "E%03d" % n, "para": i + 1, "kinds": ["앞 수정에 기댐"], "why": [], "old": e.get("old", "")[:60],
                             "new": e.get("new", e.get("new_text", ""))[:60], "decision": "REVERT",
                             "note": "앞 수정(%s)이 되돌려져 적용할 수 없어 함께 되돌림" % gated_paras[i]})
            continue
        if "new_text" in e:
            after = e["new_text"]
        elif "old" in e:
            k = before.count(e["old"])
            occ = int(e.get("occurrence", 1 if k == 1 else 0))
            if k == 0 or not 1 <= occ <= k:
                raise EditError("%d번 편집: 문단 %d에 '%s'가 %d번 있습니다%s" % (n, i + 1, e["old"], k, "" if k else "(없음)"))
            pos = -1
            for _ in range(occ):
                pos = before.index(e["old"], pos + 1)
            after = before[:pos] + e.get("new", "") + before[pos + len(e["old"]):]
        elif "text" in e and ("after" in e or "before" in e):
            anchor = e.get("after") or e.get("before")
            if before.count(anchor) != 1:
                raise EditError("%d번 편집: 기준 문구 '%s'가 문단 %d에 %d번 있습니다" % (n, anchor, i + 1, before.count(anchor)))
            pos = before.index(anchor) + (len(anchor) if "after" in e else 0)
            after = before[:pos] + e["text"] + before[pos:]
        else:
            raise EditError("%d번 편집: new_text, old/new, after|before/text 중 하나가 필요합니다" % n)
        if after != before and not spec.get("_no_gate"):  # 위험 수정 게이트: 유지(KEEP)+이유가 없으면 이 수정은 쓰지 않는다
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import fidelity_check as fc
            kinds, why = fc.gate_signals(before, after, text_of(i - 1) if i > 0 else "", text_of(i + 1) if i + 1 < len(paras) else "",
                                         spec.get("genre") or "일반", spec.get("dialogue") or "auto",
                                         _spec_list(spec, "terms"), _spec_list(spec, "protected_text"))
            if kinds:
                _o = lambda k: (texts.get(k, "") if isinstance(texts, dict) else texts[k]) or ""
                ctx_ = {"orig": "\n".join(_o(k) for k in range(len(paras))), "final": "\n".join(after if k == i else (cur.get(k) or _o(k)) for k in range(len(paras))),
                        "lost": fc.gate_signals.last_lost, "items": fc.gate_signals.last_items}
                dec, note = fc.gate_decide(e, kinds, ctx_)
                GATE_LOG.append({"id": "E%03d" % n, "para": i + 1, "kinds": kinds, "why": why, "old": e.get("old", "")[:60] or before[:60],
                                 "new": e.get("new", e.get("new_text", ""))[:60], "decision": dec, "note": note})
                if dec == "REVERT":
                    gated_paras.setdefault(i, "E%03d" % n)
                    continue
        cur[i] = after
        log.append({"n": n, "para": i + 1, "old": before, "new": after, **_why(e)})
    return atoms, texts, cur, log


def _why(e):
    return {"rule": e.get("rule", ""), "reason": e.get("reason", ""), "decor": e.get("decor")}  # 키가 없으면 None(규칙으로 추정)


def _plain(p, override=None):
    if override is not None:
        return override
    try:
        return _text(_atoms(p))
    except EditError:
        return ""


def _qn(p, name):
    return "{%s}%s" % (etree.QName(p.tag).namespace, name)


def _fill_t(t, text):
    last, buf = None, []

    def flush():
        if buf:
            s = "".join(buf)
            if last is None:
                t.text = (t.text or "") + s
            else:
                last.tail = (last.tail or "") + s
            buf.clear()

    for c in text:
        if c in CHAR_SPECIAL:
            flush()
            last = etree.SubElement(t, _qn(t, CHAR_SPECIAL[c]))
        else:
            buf.append(c)
    flush()


def rebuild_para(p, atoms, hunks, mode, style_ids=None):
    """mode='redpen': 지운 글자는 del 모양, 넣은 글자는 ins 모양으로 남긴다. mode='clean': 수정을 반영한다."""
    first_run = next((r for r in p if local(r.tag) == "run"), None)
    items, cur = [], 0
    for s, e, t in sorted(hunks):
        items += [a + (None,) for a in atoms[cur:s]]
        if mode == "redpen":
            items += [("c", a[1], a[2], "del") for a in atoms[s:e]]
        ref = atoms[s - 1][2] if s > 0 else (atoms[s][2] if s < len(atoms) else first_run)
        items += [("c", c, ref, "ins" if mode == "redpen" else None) for c in t]
        cur = max(cur, e)
    items += [a + (None,) for a in atoms[cur:]]
    groups = []
    for kind, val, run, style in items:
        if kind == "o":
            if groups and groups[-1]["kind"] == "o" and groups[-1]["src"] is run:
                groups[-1]["elems"].append(val)
            else:
                groups.append({"kind": "o", "src": run, "elems": [val]})
        else:
            key = (run.get("charPrIDRef", "0"), style)
            if groups and groups[-1]["kind"] == "c" and groups[-1]["key"] == key:
                groups[-1]["text"] += val
            else:
                groups.append({"kind": "c", "src": run, "key": key, "text": val})
    new_runs = []
    for g in groups:
        el = etree.Element(_qn(p, "run"))
        for k, v in g["src"].attrib.items():
            el.set(k, v)
        if g["kind"] == "o":
            for x in g["elems"]:
                el.append(x)
        else:
            base, style = g["key"]
            if style:
                el.set("charPrIDRef", str(style_ids[(base, style)]))
            _fill_t(etree.SubElement(el, _qn(p, "t")), g["text"])
        new_runs.append(el)
    if not new_runs and first_run is not None:
        el = etree.Element(_qn(p, "run"))
        for k, v in first_run.attrib.items():
            el.set(k, v)
        new_runs.append(el)
    old_runs = [r for r in p if local(r.tag) == "run"]
    at = list(p).index(old_runs[0]) if old_runs else 0
    for r in old_runs:
        p.remove(r)
    for k, r in enumerate(new_runs):
        p.insert(at + k, r)
    for x in [x for x in p if local(x.tag) == "linesegarray"]:
        p.remove(x)  # 글이 바뀐 문단의 줄 배치 캐시는 지워 한글이 다시 계산하게 한다


SHADE = {"del": "#FFD9D9", "ins": "#D9E6FF"}  # 원고에 이미 빨강·파랑·취소선·밑줄이 있을 때 교정 표시에 더하는 옅은 음영


def mark_conflicts(header, roots):
    """원고 본문에 이미 빨간·파란 글자나 취소선·밑줄이 쓰였는지. 있으면 빨간펜 표시와 헷갈린다. 반환: 그런 글 조각 수"""
    def tone(c):
        c = (c or "").lstrip("#")
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", c):
            return None
        r, g, b = (int(c[k:k + 2], 16) for k in (0, 2, 4))
        return "red" if r >= 150 and g <= 110 and b <= 110 else ("blue" if b >= 150 and r <= 110 and g <= 140 else None)
    risky = set()
    for cp in header.iter():
        if local(cp.tag) != "charPr":
            continue
        if tone(cp.get("textColor")):
            risky.add(cp.get("id"))
        for x in cp:
            if (local(x.tag) == "strikeout" and x.get("shape", "NONE") != "NONE") or (local(x.tag) == "underline" and x.get("type", "NONE") != "NONE"):
                risky.add(cp.get("id"))
    n = 0
    for root in roots.values():
        for run in root.iter():
            if local(run.tag) == "run" and run.get("charPrIDRef") in risky and any(local(t.tag) == "t" and (t.text or "").strip() for t in run):
                n += 1
    return n


def add_styles(header, pairs, shade=False):
    parent = next((e for e in header.iter() if local(e.tag) == "charProperties"), None)
    existing = {c.get("id"): c for c in parent if local(c.tag) == "charPr"} if parent is not None else {}
    if not existing:
        raise EditError("머리 정보(header.xml)에 글자 모양 목록(charProperties)이 없습니다. 한글이 저장한 파일이 아닐 수 있으니 한글에서 HWPX로 다시 저장해 달라고 하거나 DOCX로 받는다")
    nid = max(int(i) for i in existing) + 1
    ids = {}
    for base, style in sorted(pairs):
        if base not in existing:
            raise EditError("글자 모양 %s가 머리 정보에 없습니다" % base)
        cl = copy.deepcopy(existing[base])
        cl.set("id", str(nid))
        part = {"del": "strikeout", "ins": "underline"}[style]
        sub = next((x for x in cl if local(x.tag) == part), None)
        if sub is None:
            raise EditError("글자 모양 %s에 %s 항목이 없습니다" % (base, part))
        if shade:
            cl.set("shadeColor", SHADE[style])
        if style == "del":
            cl.set("textColor", DEL_COLOR)
            sub.set("shape", "SOLID")
            sub.set("color", DEL_COLOR)
        else:
            cl.set("textColor", INS_COLOR)
            sub.set("type", "BOTTOM")
            sub.set("shape", "SOLID")
            sub.set("color", INS_COLOR)
        parent.append(cl)
        ids[(base, style)] = nid
        nid += 1
    parent.set("itemCnt", str(sum(1 for c in parent if local(c.tag) == "charPr")))
    return ids


def _serialize(root, original):
    m = re.match(rb"<\?xml[^>]*\?>", original)
    return (m.group(0) if m else b'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>') + \
        etree.tostring(root, encoding="UTF-8", xml_declaration=False)


def same_file(a, b):
    """같은 파일인지(맥의 대소문자 무시·바로가기 포함). 둘 다 있으면 파일 시스템에 묻는다."""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _write(src, out, replaced):
    """임시 파일에 다 쓴 뒤 옮긴다. 원본과 같은 경로에는 쓰지 않는다."""
    if same_file(src, out):
        raise EditError("원본을 덮어쓸 수 없습니다: %s" % out)
    tmp = out + ".writing"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for info in zin.infolist():
            ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            ni.compress_type, ni.external_attr, ni.create_system = info.compress_type, info.external_attr, info.create_system
            zout.writestr(ni, replaced.get(info.filename, zin.read(info.filename)))
    os.replace(tmp, out)


def _preview(doc, texts_in_order):
    name = next((n for n in doc.names if n.endswith("PrvText.txt")), None)
    if not name:
        return {}
    raw = doc.zip.read(name)
    bom = raw.startswith(b"\xef\xbb\xbf")
    old = raw.decode("utf-8-sig", "replace")
    nl = "\r\n" if "\r\n" in old else "\n"
    new = nl.join(t.replace(OBJ, "") for t in texts_in_order)[:max(len(old), 1024)]
    return {name: (b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8")}


def make_outputs(src, spec, out_redpen, out_clean):
    results = {}
    conflict = 0
    for mode, out in (("redpen", out_redpen), ("clean", out_clean)):
        doc, roots, paras = _load(src)
        atoms, texts, cur, log = resolve_edits(paras, spec)
        changed = {i: _hunks_keep_objects(texts[i], t) for i, t in cur.items() if t != texts[i]}
        if mode == "redpen":
            changed = {i: expand_for_display(texts[i], h) for i, h in changed.items()}
        header_name = next(n for n in doc.names if n.endswith("header.xml"))
        header = doc.xml(header_name)
        ids = None
        if mode == "redpen":
            need = set()
            for i, hunks in changed.items():
                for s, e, t in hunks:
                    for a in atoms[i][s:e]:
                        need.add((a[2].get("charPrIDRef", "0"), "del"))
                    if t:
                        ref = atoms[i][s - 1][2] if s > 0 else (atoms[i][s][2] if s < len(atoms[i]) else
                                                               next(r for r in paras[i][1] if local(r.tag) == "run"))
                        need.add((ref.get("charPrIDRef", "0"), "ins"))
            conflict = mark_conflicts(header, roots)
            ids = add_styles(header, need, shade=bool(conflict))
        for i, hunks in changed.items():
            rebuild_para(paras[i][1], atoms[i], hunks, mode, ids)
            for anc in paras[i][1].iterancestors():  # 표·글상자 안 문단이면 바깥 문단의 줄 배치 캐시도 다시 계산하게
                if local(anc.tag) == "p":
                    for x in [x for x in anc if local(x.tag) == "linesegarray"]:
                        anc.remove(x)
        replaced = {sec: _serialize(root, doc.zip.read(sec)) for sec, root in roots.items()}
        if mode == "redpen":
            replaced[header_name] = _serialize(header, doc.zip.read(header_name))
        order = [cur.get(k, _plain(p)) for k, (_s, p) in enumerate(paras)]
        replaced.update(_preview(doc, order))
        _write(src, out, replaced)
        results[mode] = {"changed": changed, "ids": ids, "cur": cur, "texts": texts, "log": log,
                         "conflict": conflict if mode == "redpen" else 0}
    return results


def verify_outputs(src, out_redpen, out_clean, res):
    """만든 두 파일을 원본과 대조한다. [(항목, 통과, 설명)]"""
    checks = []
    o_doc, _r, o_paras = _load(src)
    for name, path in (("빨간펜", out_redpen), ("수정본", out_clean)):
        z = zipfile.ZipFile(path)
        first = z.infolist()[0]
        ok = first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED and z.testzip() is None
        checks.append(("%s: ZIP 구조(mimetype 첫 항목·무압축·손상 없음)" % name, ok, path))
        bad = []
        for n in z.namelist():
            if n.endswith(".xml") or n.endswith(".hpf"):
                try:
                    etree.fromstring(z.read(n), PARSER)
                except etree.XMLSyntaxError as ex:
                    bad.append("%s: %s" % (n, ex))
        checks.append(("%s: XML 파트 모두 읽힘" % name, not bad, "; ".join(bad) or "%d개" % len(z.namelist())))
        doc, _roots, paras = _load(path)
        checks.append(("%s: 문단 수 같음" % name, len(paras) == len(o_paras), "%d / %d" % (len(paras), len(o_paras))))
        r = res["redpen" if name == "빨간펜" else "clean"]
        pos = {id(p_): k for k, (_s, p_) in enumerate(paras)}
        outer = {pos[id(a)] for c in r["changed"] for a in paras[c][1].iterancestors() if local(a.tag) == "p" and id(a) in pos}
        same = all(etree.tostring(paras[k][1]) == etree.tostring(o_paras[k][1]) for k in range(min(len(paras), len(o_paras)))
                   if k not in r["changed"] and k not in outer)  # 고친 문단을 품은 바깥 문단(표 등)은 제외
        checks.append(("%s: 고치지 않은 문단은 원본과 바이트까지 같음" % name, same, "고친 문단 %d개" % len(r["changed"])))
        header = doc.xml(next(n for n in doc.names if n.endswith("header.xml")))
        cps = [c for c in header.iter() if local(c.tag) == "charPr"]
        cnt = next(e for e in header.iter() if local(e.tag) == "charProperties").get("itemCnt")
        used = {x.get("charPrIDRef") for _s, p in paras for x in p.iter() if local(x.tag) == "run"}
        known = {c.get("id") for c in cps}
        checks.append(("%s: 글자 모양 수·참조 일치" % name, str(len(cps)) == cnt and used <= known,
                       "모양 %d개(itemCnt %s), 없는 참조 %s" % (len(cps), cnt, sorted(used - known) or "없음")))
        if name == "수정본":
            exp = [r["cur"].get(k, None) for k in range(len(paras))]
            mism = [k + 1 for k in range(len(paras)) if exp[k] is not None
                    and _norm(para_text(paras[k][1])) != _norm(exp[k].replace(OBJ, ""))]
            checks.append(("수정본: 고친 문단 본문 = 기대값", not mism, "다른 문단 %s" % (mism or "없음")))
        else:
            ids = r["ids"] or {}
            dels = {v for (b, s), v in ids.items() if s == "del"}
            inss = {v for (b, s), v in ids.items() if s == "ins"}
            bad = []
            for k, hunks in r["changed"].items():
                orig = r["texts"][k]
                exp_del = "".join(orig[s:e] for s, e, t in sorted(hunks))
                exp_ins = "".join(t for s, e, t in sorted(hunks))
                got_del = "".join(para_text(_one_run(x)) for x in paras[k][1] if local(x.tag) == "run" and int(x.get("charPrIDRef", 0)) in dels)
                got_ins = "".join(para_text(_one_run(x)) for x in paras[k][1] if local(x.tag) == "run" and int(x.get("charPrIDRef", 0)) in inss)
                keep = _norm(para_text(paras[k][1]))
                merged = orig.replace(OBJ, "")
                if _norm(got_del) != _norm(exp_del) or _norm(got_ins) != _norm(exp_ins):
                    bad.append(k + 1)
                if _norm("".join(ch for ch in keep)) == "" and merged:
                    bad.append(k + 1)
            checks.append(("빨간펜: 빨간 부분 = 지운 글자, 파란 부분 = 넣은 글자", not bad, "다른 문단 %s" % (bad or "없음")))
            blind = []
            for k in r["changed"]:
                for x in paras[k][1]:
                    if local(x.tag) == "run" and int(x.get("charPrIDRef", 0)) in (dels | inss):
                        if not VISIBLE.search(para_text(_one_run(x))):
                            blind.append(k + 1)
            checks.append(("빨간펜: 색이 붙은 조각마다 눈에 보이는 글자가 있음(빈칸·문장부호만인 표시 없음)", not blind,
                           "보이지 않는 표시가 있는 문단 %s" % (sorted(set(blind)) or "없음")))
    return checks


def _one_run(run):
    wrap = etree.Element(_qn(run, "p"))
    wrap.append(copy.deepcopy(run))
    return wrap


def _norm(s):
    return s.replace("\u00ad", "-").replace("\u00a0", " ")


def write_report(src, res, out_redpen, out_clean, path):
    r = res["clean"]
    cf = res["redpen"].get("conflict", 0)
    L = ["# 빨간펜 교정 보고서: %s" % os.path.basename(src), "",
         "- 빨간펜 교정본: `%s` — **빨간 취소선 = 지운 글자, 파란 밑줄 = 넣은 글자**" % os.path.basename(out_redpen),
         ] + (["- 원고에 이미 빨간·파란 글자나 취소선·밑줄이 %d곳 있어, 교정 표시에는 **옅은 배경색(음영)**을 더했다. 음영이 있는 것만 교정 표시다." % cf] if cf else []) + [
         ] + (["- 수정 반영본: `%s` — 수정을 모두 반영한 깨끗한 원고" % os.path.basename(out_clean)] if out_clean else []) + [
         "- 수정 %d건, 고친 문단 %d개. 한글의 변경 추적이 아니라 글자 모양으로 표시했으므로 수락·거절 버튼은 없다. 수정을 모두 반영한 원고가 필요하면 요청한다."
         % (len([x for x in r["log"] if x["old"] != x["new"] or x.get("note")]), len(r["changed"])), "",
         "| # | 문단 | 원문 | 수정 | 규칙·이유 |", "|---|---|---|---|---|"]
    for x in r["log"]:
        if x.get("note"):
            L.append("| %d | %d | (메모) | %s | %s |" % (x["n"], x["para"], x["note"], " — ".join(v for v in (x["rule"], x["reason"]) if v)))
            continue
        hunks = sorted(redpen_hunks(x["old"], x["new"]))
        if hunks:  # 편집 하나를 앞뒤 문맥 한 덩어리로 보인다
            a0, b0 = max(0, hunks[0][0] - 8), min(len(x["old"]), hunks[-1][1] + 8)
            new, pos = "", a0
            for s, e, t in hunks:
                new += x["old"][pos:s] + t
                pos = e
            new += x["old"][pos:b0]
            old = x["old"][a0:b0].replace(OBJ, "")
            new = new.replace(OBJ, "")
        else:
            old = new = "-"
        L.append("| %d | %d | %s | %s | %s |" % (x["n"], x["para"], old.replace("|", "\\|"), new.replace("|", "\\|"),
                                                " — ".join(v for v in (x["rule"], x["reason"]) if v)))
    def dstate(x):  # decor 키가 있으면 그 값이 이긴다. 없으면 장식 규칙으로 글자를 줄인 수정을 추정한다
        if x.get("decor") is not None:
            return "명시" if x["decor"] else None
        return "추정" if (x.get("rule") or "")[:2] in ("L1", "L2", "L4", "U2") and x["old"] and len(x["new"]) < len(x["old"]) else None
    dec = [x for x in r["log"] if dstate(x)]
    if dec:
        L += ["", "## 지운 저정보량 장식 표현 (작가가 되살릴 수 있게)", ""]
        L += ["- %d번 · %d문단 `%s` → `%s` — %s%s" % (x["n"], x["para"], x["old"], x["new"], " · ".join(v for v in (x["rule"], x["reason"]) if v),
                                                  " (자동 추정: 장식이 아니면 편집 항목에 \"decor\": false)" if dstate(x) == "추정" else "") for x in dec]
    if GATE_LOG:
        keep_ = sum(1 for x in GATE_LOG if x["decision"] == "KEEP")
        L += ["", "## 위험 수정 판정 (게이트: 유지 %d, 되돌림 %d — 되돌린 수정은 파일에 없다)" % (keep_, len(GATE_LOG) - keep_), "",
              "| 번호 | 문단 | 위험 | 바꾼 것 | 판정 | 이유 |", "|---|---|---|---|---|---|"]
        L += ["| %s | %d | %s | %r → %r | %s | %s |" % (x["id"], x["para"], "·".join(x["kinds"]), x["old"], x["new"], x["decision"], x["note"].replace("|", "/"))
              for x in GATE_LOG]
    L += ["", "## 한글에서 확인할 것", "",
          "1. 두 파일이 모두 열리는가(맥·윈도우 한글).",
          "2. 빨간펜 교정본에서 빨간 취소선·파란 밑줄이 보이는가, 나머지 문단의 모양·쪽 나눔이 원본과 같은가.",
          "3. 고친 문단의 줄 바꿈이 자연스러운가(고친 문단은 한글이 줄 배치를 다시 계산한다)."]
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")


BIG_OPS = ("rewrite", "delete_paragraph", "merge_next", "split")


def reason_problems(edits, tier_fn=None):
    """수정 이유 강제: 모든 수정에 rule, '확실'이 아닌 수정과 판단 폭이 큰 수정(문단 다시 쓰기·삭제·합치기·나누기)에 reason.
    '더 매끄럽게'는 이유가 아니다(rewriting.md). 메모는 대상이 아니다."""
    out = []
    for i, e in enumerate(edits):
        op = e.get("op") or ("rewrite" if "new_text" in e else "replace" if "old" in e else "")
        if op == "comment":
            continue
        rule, reason = str(e.get("rule") or "").strip(), str(e.get("reason") or "").strip()
        where = "%d번 항목(%s)" % (i + 1, ("%s문단" % e["para"]) if "para" in e else ("'%s'" % str(e.get("find", ""))[:12]))
        if not rule:
            out.append("%s: rule(규칙 ID와 이름)이 없습니다." % where)
        tier = (tier_fn(e) if tier_fn else (e.get("tier") or "")) or ""
        if not reason and (tier != "확실" or op in BIG_OPS):
            out.append("%s: reason(수정 이유)이 없습니다. '확실'이 아닌 수정과 %s에는 이유가 필요합니다."
                       % (where, "문단 다시 쓰기·삭제·합치기·나누기" if op in BIG_OPS else "권장·제안 수정"))
    return out


def cmd_redpen(args):
    try:
        spec = json.loads(open(args.edits, encoding="utf-8-sig").read())
    except (OSError, ValueError) as ex:
        print("오류: 편집 목록을 읽지 못했습니다(%s): %s" % (args.edits, ex), file=sys.stderr)
        return 2
    if isinstance(spec, list):
        spec = {"edits": spec}
    if not isinstance(spec, dict) or not isinstance(spec.get("edits"), list) or not spec["edits"] or not all(isinstance(e, dict) for e in spec["edits"]):
        print("오류: 편집 목록은 {\"edits\": [{…}, …]} 형식이어야 하고 비어 있으면 안 됩니다.", file=sys.stderr)
        return 2
    spec["_no_gate"] = bool(getattr(args, "no_gate", False))
    del GATE_LOG[:]
    if not getattr(args, "allow_missing_reasons", False):
        tier_guess = lambda e: e.get("tier") or ("확실" if str(e.get("rule") or "").strip()[:1] == "P" else "")
        probs = reason_problems(spec.get("edits", []), tier_guess)
        if probs:
            print("오류: 수정 이유가 빠진 항목이 있어 적용하지 않았습니다(이유 없는 수정은 하지 않는다).", file=sys.stderr)
            for x in probs[:20]:
                print("  - " + x, file=sys.stderr)
            print("  옛 편집 목록이면 --allow-missing-reasons로 적용할 수 있다(권하지 않음).", file=sys.stderr)
            return 2
    stem = re.sub(r"\.hwpx$", "", args.file, flags=re.I)
    if not (args.output and args.report) and not os.access(os.path.dirname(os.path.abspath(args.file)), os.W_OK):
        stem = os.path.join(os.getcwd(), os.path.basename(stem))  # 업로드 폴더처럼 쓸 수 없는 곳이면 현재 폴더에
        print("알림: 원고 폴더에 쓸 수 없어 결과를 현재 폴더(%s)에 쓴다. 결과 위치는 -o·--report로 정할 수 있다" % os.getcwd())
    out_r = args.output or stem + "_redpen.hwpx"
    out_rep = args.report or stem + "_편집보고서.md"
    paths = [args.file, out_r, out_rep] + ([args.clean] if args.clean else [])
    if any(same_file(a, b) for i, a in enumerate(paths) for b in paths[i + 1:]):
        print("편집 오류: 원본을 덮어쓰거나 결과 파일끼리 같은 경로를 쓸 수 없습니다(-o·--clean·--report를 원본과 다른 이름으로)")
        return 2
    import shutil
    import tempfile
    tmpdir = None if args.clean else tempfile.mkdtemp()  # 수정 반영본은 검증에 늘 쓰고, --clean일 때만 남긴다
    out_c = args.clean or os.path.join(tmpdir, "clean.hwpx")
    try:
        try:
            res = make_outputs(args.file, spec, out_r, out_c)
        except EditError as ex:
            print("편집 오류: %s" % ex)
            return 2
        checks = verify_outputs(args.file, out_r, out_c, res)
        write_report(args.file, res, out_r, args.clean, out_rep)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    for name, ok, note in checks:
        print("[%s] %s  %s" % ("PASS" if ok else "FAIL", name, note.replace(out_c, "(검증용 임시 파일)") if not args.clean else note))
    if res["redpen"].get("conflict"):
        print("알림: 원고에 이미 빨간·파란 글자나 취소선·밑줄이 %d곳 있어 교정 표시에 옅은 음영을 더했다" % res["redpen"]["conflict"])
    print("결과: %s\n빨간펜: %s\n보고서: %s%s" % ("통과" if all(c[1] for c in checks) else "실패", out_r, out_rep,
                                           ("\n수정 반영본: %s" % out_c) if args.clean else ""))
    return 0 if all(c[1] for c in checks) else 1


# ---------------------------------------------------------------- 자체 시험(스킬만 받아도 돌릴 수 있게)
_ST_LS = '<hp:linesegarray><hp:lineseg textpos="0" vertpos="0" vertsize="1000" textheight="1000" baseline="850" spacing="600" horzpos="0" horzsize="42520" flags="393216"/></hp:linesegarray>'
_ST_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" version="1.4" secCnt="1"><hh:beginNum page="1" footnote="1" endnote="1" pic="1" tbl="1" equation="1"/><hh:refList><hh:charProperties itemCnt="1"><hh:charPr id="0" height="1000" textColor="#000000" shadeColor="none" useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="2"><hh:fontRef hangul="1" latin="1" hanja="1" japanese="1" other="1" symbol="1" user="1"/><hh:ratio hangul="100" latin="100" hanja="100" japanese="100" other="100" symbol="100" user="100"/><hh:spacing hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"/><hh:relSz hangul="100" latin="100" hanja="100" japanese="100" other="100" symbol="100" user="100"/><hh:offset hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"/><hh:underline type="NONE" shape="SOLID" color="#000000"/><hh:strikeout shape="NONE" color="#000000"/><hh:outline type="NONE"/><hh:shadow type="NONE" color="#C0C0C0" offsetX="10" offsetY="10"/></hh:charPr></hh:charProperties></hh:refList><hh:trackchageConfig flags="56"/></hh:head>'
_ST_HPF = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><opf:package xmlns:opf="http://www.idpf.org/2007/opf/"><opf:manifest><opf:item id="header" href="Contents/header.xml" media-type="application/xml"/><opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/></opf:manifest><opf:spine><opf:itemref idref="section0"/></opf:spine></opf:package>'


def _st_hwpx(path, body):
    """시험용 최소 HWPX: body는 hp:p들의 XML."""
    sec = ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><hs:sec xmlns:hs="%s" xmlns:hp="%s">' % ("http://www.hancom.co.kr/hwpml/2011/section", "http://www.hancom.co.kr/hwpml/2011/paragraph")
           + body + "</hs:sec>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        for n, c in (("Contents/content.hpf", _ST_HPF), ("Contents/header.xml", _ST_HEAD), ("Contents/section0.xml", sec)):
            z.writestr(n, c, compress_type=zipfile.ZIP_DEFLATED)


def cmd_selftest(args):
    """빨간펜 엔진 회귀 시험: 띄어쓰기 표시가 보이는지, 표 안 문단, 원본 덮어쓰기 방지, 임시 폴더 정리."""
    import contextlib
    import io
    import shutil
    import tempfile
    res = []

    def p_(text):
        return '<hp:p id="0" paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>%s</hp:t></hp:run>%s</hp:p>' % (text, _ST_LS)
    body = (p_("첫 문단은 그대로 둔다.") + p_("무리라는건 알고있었다.")
            + '<hp:p id="0" paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:tbl><hp:tr><hp:tc><hp:subList>'
            + p_("셀 안에 할수 있다.") + "</hp:subList></hp:tc></hp:tr></hp:tbl></hp:run>" + _ST_LS + "</hp:p>")

    def run(argv, strict=False):  # 엔진 동작 시험은 이유 검사를 끄고, 이유 강제는 strict=True로 따로 시험한다
        if argv and argv[0] == "redpen" and not strict:
            argv = argv[:1] + ["--allow-missing-reasons", "--no-gate"] + argv[1:]
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, out.getvalue()
    td = tempfile.mkdtemp()
    try:
        src = os.path.join(td, "src.hwpx")
        _st_hwpx(src, body)
        ep = os.path.join(td, "e.json")
        for label, edits in (("띄어쓰기(어절째 표시)", [{"find": "무리라는건", "old": "무리라는건 알고있었다", "new": "무리라는 건 알고 있었다"}]),
                             ("표 안 문단", [{"find": "셀 안에", "old": "할수", "new": "할 수"}])):
            open(ep, "w", encoding="utf-8").write(json.dumps({"edits": edits}, ensure_ascii=False))
            code, out = run(["redpen", src, ep, "-o", os.path.join(td, "r.hwpx"), "--report", os.path.join(td, "r.md")])
            res.append(("빨간펜: %s — 자동 검증 통과" % label, code == 0 and "[FAIL]" not in out))
        before = open(src, "rb").read()
        code, _o = run(["redpen", src, ep, "-o", src, "--report", os.path.join(td, "x.md")])
        res.append(("원본 덮어쓰기 방지(-o가 원본이면 거부, 원본 그대로)", code == 2 and open(src, "rb").read() == before))
        made = []
        real = tempfile.mkdtemp
        tempfile.mkdtemp = lambda *a, **k: made.append(1) or real(*a, **k)
        try:
            run(["redpen", src, ep, "-o", os.path.join(td, "r2.hwpx"), "--clean", os.path.join(td, "c2.hwpx"), "--report", os.path.join(td, "r2.md")])
        finally:
            tempfile.mkdtemp = real
        res.append(("--clean일 때 임시 폴더를 만들지 않음", not made))
        open(ep, "w", encoding="utf-8").write(json.dumps({"edits": [{"find": "첫 문단은", "old": "그대로 둔다", "new": "둔다", "rule": "L2 홍보 수식어", "decor": True}]}, ensure_ascii=False))
        rp = os.path.join(td, "decor.md")
        code, _o = run(["redpen", src, ep, "-o", os.path.join(td, "d.hwpx"), "--report", rp])
        _P5 = lambda t: '<hp:p id="0" paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>%s</hp:t></hp:run></hp:p>' % t
        _st_hwpx(os.path.join(td, "g5.hwpx"), "".join(_P5(x) for x in ("마당에는 낙엽이 쌓여 있었다.", "창고 앞에서는 준호가 버린 나뭇가지 세 묶음을 벽 옆에 모아 두고 있었다.", "바람이 불었다.")))
        ep5 = os.path.join(td, "g5.json")

        def _g5(edit):
            open(ep5, "w", encoding="utf-8").write(json.dumps({"genre": "웹소설", "edits": [dict(edit, para=2, rule="W2", reason="x", tier="권장")]}, ensure_ascii=False))
            with contextlib.redirect_stdout(io.StringIO()):
                c_ = main(["redpen", os.path.join(td, "g5.hwpx"), ep5, "-o", os.path.join(td, "g5_r.hwpx"), "--report", os.path.join(td, "g5.md")])
            d_ = Hwpx(os.path.join(td, "g5_r.hwpx"))
            s_ = redpen_styles(d_)
            return c_, [redpen_para_text(p, s_, "final") for _s, p, _n in d_.paragraphs()]
        c5, fin5 = _g5({"old": "세 묶음", "new": "다섯 묶음", "decision": "KEEP", "decision_reason": "앞 문장과 같은 뜻이다"})
        c6, fin6 = _g5({"old": "버린", "new": "버려진"})
        c7, fin7 = _g5({"old": "버린", "new": "버려진", "decision": "REVERT", "decision_reason": "버려진 것은 준호의 행동이 아니게 된다"})
        res.append(("위험 수정 게이트: 숫자 변경은 문맥 이유로 유지되지 않고, 확인 항목(버린 → 버려진)은 판정 없이 쓰고 REVERT면 되돌려도 빨간펜 자동 검증 통과",
                    c5 == 0 and any("세 묶음" in x for x in fin5) and c6 == 0 and any("버려진" in x for x in fin6)
                    and c7 == 0 and any("준호가 버린" in x for x in fin7)))
        res.append(("보고서에 '지운 저정보량 장식 표현' 구역", code == 0 and "## 지운 저정보량 장식 표현" in open(rp, encoding="utf-8").read()))
        open(ep, "w", encoding="utf-8").write(json.dumps({"edits": [{"find": "첫 문단은", "old": "그대로 둔다", "new": "둔다", "rule": "L1", "decor": False}]}, ensure_ascii=False))
        rp2 = os.path.join(td, "nodecor.md")
        code, _o = run(["redpen", src, ep, "-o", os.path.join(td, "e.hwpx"), "--report", rp2])
        open(ep, "w", encoding="utf-8").write(json.dumps({"edits": [{"find": "첫 문단은", "old": "그대로 둔다", "new": "둔다"}]}, ensure_ascii=False))
        c1, _o = run(["redpen", src, ep, "-o", os.path.join(td, "n1.hwpx"), "--report", os.path.join(td, "n1.md")], strict=True)
        open(ep, "w", encoding="utf-8").write(json.dumps({"edits": [{"find": "첫 문단은", "old": "그대로 둔다", "new": "둔다", "rule": "D2", "reason": "되풀이"}]}, ensure_ascii=False))
        c2, _o = run(["redpen", src, ep, "-o", os.path.join(td, "n2.hwpx"), "--report", os.path.join(td, "n2.md")], strict=True)
        res.append(("이유 강제: rule·reason 없는 수정은 거부, 있으면 적용", c1 == 2 and c2 == 0))
        res.append(("decor: false면 장식 표현 구역에 넣지 않음", code == 0 and "지운 저정보량 장식 표현" not in open(rp2, encoding="utf-8").read()))
        protected_hwpx = os.path.join(td, "protected.hwpx")
        _st_hwpx(protected_hwpx, p_("세린은 창가에 섰다.") + p_("승인 문구입니다."))
        open(ep, "w", encoding="utf-8").write(json.dumps({
            "genre": "소설", "terms": ["세린"], "protected_text": ["승인 문구입니다."], "edits": [
                {"para": 1, "old": "세린", "new": "세릭", "rule": "어색한 말", "tier": "권장", "reason": "표현 통일"},
                {"para": 2, "old": "문구", "new": "문장", "rule": "어색한 말", "tier": "권장", "reason": "표현 통일"}
            ]}, ensure_ascii=False))
        ph_out = os.path.join(td, "protected_r.hwpx")
        pc, _po = run(["redpen", protected_hwpx, ep, "-o", ph_out, "--report", os.path.join(td, "protected.md")], strict=True)
        ptxt = []
        if pc == 0:
            pd_ = Hwpx(ph_out); ps_ = redpen_styles(pd_)
            ptxt = [redpen_para_text(pp, ps_, "final") for _ss, pp, _nn in pd_.paragraphs()]
        joined_p = "\n".join(ptxt)
        res.append(("보호 목록: terms·protected_text를 빨간펜 게이트가 받아 확정 이름·보호 문구 수정을 되돌림",
                    pc == 0 and "세린은" in joined_p and "세릭은" not in joined_p
                    and "승인 문구입니다" in joined_p and "승인 문장입니다" not in joined_p))
        red_head = _ST_HEAD.replace('<hh:charProperties itemCnt="1">', '<hh:charProperties itemCnt="2">').replace(
            "</hh:charProperties>", '<hh:charPr id="1" height="1000" textColor="#FF0000" shadeColor="none" borderFillIDRef="2">'
            '<hh:underline type="NONE" shape="SOLID" color="#000000"/><hh:strikeout shape="NONE" color="#000000"/></hh:charPr></hh:charProperties>')
        red = os.path.join(td, "red.hwpx")
        sec = ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
               'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">' + p_("무리라는건 알았다.")
               + '<hp:p id="0" paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="1"><hp:t>작가 메모</hp:t></hp:run>' + _ST_LS + "</hp:p></hs:sec>")
        with zipfile.ZipFile(red, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
            for n, c in (("Contents/content.hpf", _ST_HPF), ("Contents/header.xml", red_head), ("Contents/section0.xml", sec)):
                z.writestr(n, c, compress_type=zipfile.ZIP_DEFLATED)
        open(ep, "w", encoding="utf-8").write(json.dumps({"edits": [{"find": "무리라는건", "old": "무리라는건", "new": "무리라는 건"}]}, ensure_ascii=False))
        rr = os.path.join(td, "red_r.hwpx")
        code, out = run(["redpen", red, ep, "-o", rr, "--report", os.path.join(td, "red.md")])
        head = zipfile.ZipFile(rr).read("Contents/header.xml").decode("utf-8") if code == 0 else ""
        res.append(("원고에 이미 빨간 글자가 있으면 교정 표시에 음영을 더하고 알림", code == 0 and SHADE["del"] in head and "음영" in out))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    for name, ok in res:
        print("%s  %s" % ("PASS" if ok else "FAIL", name))
    fails = sum(not ok for _n, ok in res)
    print("빨간펜 엔진 시험 %d건: 통과 %d, 실패 %d" % (len(res), len(res) - fails, fails))
    return 1 if fails else 0


def main(argv=None):
    try:  # 출력을 head 등으로 잘라도 조용히 끝나게
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="TYPODER Korean Editor - HWPX 읽기·빨간펜 교정 도구")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("selftest", help="빨간펜 엔진 회귀 시험")
    a.set_defaults(func=cmd_selftest)
    a = sub.add_parser("inspect", help="패키지 구조·구역·문단·변경 추적 표시를 확인")
    a.add_argument("file")
    a.set_defaults(func=cmd_inspect)
    a = sub.add_parser("redpen", help="빨간펜 교정본(_redpen.hwpx)과 편집 보고서 만들기(수정 반영본은 검증에 쓰고 --clean일 때만 남김)")
    a.add_argument("--allow-missing-reasons", action="store_true", help="rule·reason이 빠진 항목도 적용(옛 편집 목록용, 권하지 않음)")
    a.add_argument("--no-gate", action="store_true", help="위험 수정 게이트를 끈다(엔진 동작 시험용, 권하지 않음)")
    a.add_argument("file")
    a.add_argument("edits", help="편집 목록 JSON: {\"edits\": [{\"para\": 번호 | \"find\": 문구, \"old\": …, \"new\": …, \"rule\": …, \"reason\": …}]}")
    a.add_argument("-o", "--output")
    a.add_argument("--clean", help="수정 반영본도 파일로 남길 경로(기본은 만들지 않음. 검증에는 늘 씀)")
    a.add_argument("--report")
    a.set_defaults(func=cmd_redpen)
    a = sub.add_parser("text", help="HWPX 본문을 문단 번호와 함께 읽거나 빨간펜 교정본의 원문·최종본을 복원")
    a.add_argument("file")
    a.add_argument("--view", choices=["accept", "reject", "markup"], default="accept")
    a.add_argument("--plain", action="store_true")
    a.add_argument("--redpen", choices=["final", "original"], help="빨간펜 교정본에서 최종 글(빨간 취소선 뺌) 또는 원문(파란 밑줄 뺌)을 되살린다")
    a.set_defaults(func=cmd_text)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
