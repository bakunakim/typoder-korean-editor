#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""track_changes.py - TYPODER Korean Editor의 Word 변경 내용 추적 엔진.

원본 .docx는 건드리지 않는다. 편집 결과를 Microsoft Word가 인식하는 실제
변경 내용 추적(OOXML w:ins / w:del, 문단 표시 삽입·삭제)과 메모(comments)로
기록한 새 파일을 만든다. 색·취소선으로 흉내 내지 않는다.

하위 명령
  extract INPUT.docx [--json] [-o FILE]
      편집 대상 문단을 번호와 함께 뽑는다. (part, para)가 apply의 주소다.
  apply   INPUT.docx EDITS.json -o OUT_tracked.docx [--dry-run] [...]
      편집 목록을 변경 내용 추적으로 적용한다. 하나라도 해석에 실패하면
      파일을 쓰지 않는다(--skip-errors로 완화).
  verify  TRACKED.docx --original INPUT.docx [--edits EDITS.json] [--render]
      ZIP, XML 파싱, 네임스페이스, w:ins/w:del 존재, 수정 ID 중복, 구조,
      왕복 검사(모두 거부 = 원문, 모두 수락 = 기대 결과)를 수행한다.
  accept  TRACKED.docx -o OUT_clean.docx
      모든 변경을 수락하고 편집자 메모를 지운 읽기용 완성본을 만든다.
  text    DOCX [--view accept|reject] [--parts document,footnotes]
      대조용 텍스트(문단 사이 빈 줄)를 출력한다.

편집 목록 형식과 작업 규칙: references/word-track-changes.md
의존성: Python 3.8+, lxml (선택: verify --render에 LibreOffice와 Poppler의 pdfinfo·pdftotext·pdftoppm. 없으면 그 검사를 건너뛰었다고 표시)
"""
from __future__ import annotations

import argparse
import copy
import io
from contextlib import redirect_stdout
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    sys.stderr.write("lxml이 필요합니다: pip install lxml\n")
    sys.exit(3)

# ---------------------------------------------------------------- 상수
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
COMMENTS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
XML_DECL = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
DEFAULT_AUTHOR = "TYPODER Korean Editor"
DEFAULT_INITIALS = "TKE"


def w(tag):
    return "{%s}%s" % (W_NS, tag)


def m(tag):
    return "{%s}%s" % (M_NS, tag)


PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)

# 문단 안의 글자가 아닌 개체는 한 글자짜리 표식으로 다룬다. 편집으로 지우거나 옮길 수 없다.
TOKEN_NAMES = ["TAB", "BR", "FN", "EN", "FNREF", "ENREF", "FLD", "IMG", "MATH", "SYM", "CMT", "OBJ"]
TOKEN_CHAR = {name: chr(0xE001 + i) for i, name in enumerate(TOKEN_NAMES)}
CHAR_TOKEN = {c: n for n, c in TOKEN_CHAR.items()}
TOKEN_CHARS = set(CHAR_TOKEN)
TOKEN_SPLIT_RE = re.compile("[" + "".join(TOKEN_CHAR.values()) + "]")
DISPLAY_RE = re.compile(r"⟦([A-Z]+)⟧")
TOKEN_HELP = {
    "TAB": "탭", "BR": "줄바꿈", "FN": "각주 번호", "EN": "미주 번호", "FNREF": "각주 앞 번호",
    "ENREF": "미주 앞 번호", "FLD": "필드(목차·상호 참조·쪽 번호 등, 수정 불가)", "IMG": "그림·개체",
    "MATH": "수식", "SYM": "기호 글꼴 문자", "CMT": "기존 메모 표시", "OBJ": "기타 개체",
}
RUN_TOKEN = {
    w("tab"): "TAB", w("ptab"): "TAB", w("br"): "BR", w("cr"): "BR",
    w("footnoteReference"): "FN", w("endnoteReference"): "EN",
    w("footnoteRef"): "FNREF", w("endnoteRef"): "ENREF",
    w("drawing"): "IMG", w("pict"): "IMG", w("object"): "IMG",
    w("sym"): "SYM", w("commentReference"): "CMT",
}
RUN_SKIP = {w("rPr"), w("instrText"), w("delInstrText"), w("lastRenderedPageBreak"),
            w("softHyphen"), w("annotationRef")}
PASS_CONTAINERS = {w("hyperlink"), w("smartTag"), w("customXml"), w("dir"), w("bdo")}
REV_WRAPPERS = {w("ins"), w("del"), w("moveTo"), w("moveFrom")}
MARK_TAGS = (w("ins"), w("del"), w("moveFrom"), w("moveTo"))
REVISION_TAGS = {w(t) for t in (
    "ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange", "sectPrChange", "tblPrChange",
    "tblGridChange", "trPrChange", "tcPrChange", "tblPrExChange", "numberingChange",
    "cellIns", "cellDel", "cellMerge")}
MARKUP_SIBLINGS = {w(t) for t in (
    "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd", "permStart", "permEnd",
    "proofErr", "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd")}

# CT_Settings 자식 순서(ISO/IEC 29500 wml.xsd). w:trackRevisions를 올바른 위치에 넣는 데 쓴다.
SETTINGS_ORDER = [
    "writeProtection", "view", "zoom", "removePersonalInformation", "removeDateAndTime",
    "doNotDisplayPageBoundaries", "displayBackgroundShape", "printPostScriptOverText",
    "printFractionalCharacterWidth", "printFormsData", "embedTrueTypeFonts", "embedSystemFonts",
    "saveSubsetFonts", "saveFormsData", "mirrorMargins", "alignBordersAndEdges",
    "bordersDoNotSurroundHeader", "bordersDoNotSurroundFooter", "gutterAtTop", "hideSpellingErrors",
    "hideGrammaticalErrors", "activeWritingStyle", "proofState", "formsDesign", "attachedTemplate",
    "linkStyles", "stylePaneFormatFilter", "stylePaneSortMethod", "documentType", "mailMerge",
    "revisionView", "trackRevisions", "doNotTrackMoves", "doNotTrackFormatting", "documentProtection",
    "autoFormatOverride", "styleLockTheme", "styleLockQFSet", "defaultTabStop", "autoHyphenation",
    "consecutiveHyphenLimit", "hyphenationZone", "doNotHyphenateCaps", "showEnvelope", "summaryLength",
    "clickAndTypeStyle", "defaultTableStyle", "evenAndOddHeaders", "bookFoldRevPrinting",
    "bookFoldPrinting", "bookFoldPrintingSheets", "drawingGridHorizontalSpacing",
    "drawingGridVerticalSpacing", "displayHorizontalDrawingGridEvery", "displayVerticalDrawingGridEvery",
    "doNotUseMarginsForDrawingGridOrigin", "drawingGridHorizontalOrigin", "drawingGridVerticalOrigin",
    "doNotShadeFormData", "noPunctuationKerning", "characterSpacingControl", "printTwoOnOne",
    "strictFirstAndLastChars", "noLineBreaksAfter", "noLineBreaksBefore", "savePreviewPicture",
    "doNotValidateAgainstSchema", "saveInvalidXml", "ignoreMixedContent", "alwaysShowPlaceholderText",
    "doNotDemarcateInvalidXml", "saveXmlDataOnly", "useXSLTWhenSaving", "saveThroughXslt", "showXMLTags",
    "alwaysMergeEmptyNamespace", "updateFields", "hdrShapeDefaults", "footnotePr", "endnotePr", "compat",
    "docVars", "rsids", "attachedSchema", "themeFontLang", "clrSchemeMapping",
    "doNotIncludeSubdocsInStats", "doNotAutoCompressPictures", "forceUpgrade", "captions",
    "readModeInkLockDown", "smartTagType", "shapeDefaults", "doNotEmbedSmartTags", "decimalSymbol",
    "listSeparator"]
SETTINGS_INDEX = {n: i for i, n in enumerate(SETTINGS_ORDER)}
PPR_TAIL = {w("sectPr"), w("pPrChange")}


class EditError(Exception):
    """편집 목록을 문서에 대응시키지 못했을 때."""


class DocxError(Exception):
    """문서 구조 문제."""


# ---------------------------------------------------------------- 표시 변환
def to_display(s):
    return "".join("⟦%s⟧" % CHAR_TOKEN[c] if c in TOKEN_CHARS else c for c in s)


def from_display(s):
    if "\n" in s or "\r" in s:
        raise EditError("새 텍스트에 줄바꿈이 있습니다. 문단을 나누려면 split 작업을 쓰세요.")

    def rep(mo):
        name = mo.group(1)
        if name not in TOKEN_CHAR:
            raise EditError("알 수 없는 개체 표식 ⟦%s⟧" % name)
        return TOKEN_CHAR[name]

    return DISPLAY_RE.sub(rep, s).replace("\t", TOKEN_CHAR["TAB"])


def strip_cmt(s):
    return s.replace(TOKEN_CHAR["CMT"], "")


def nbsp(s):
    return s.replace("\u00a0", " ")


# ---------------------------------------------------------------- 패키지
class Docx:
    def __init__(self, path):
        path = str(path)
        if not zipfile.is_zipfile(path):
            raise DocxError("ZIP(docx) 파일이 아닙니다: %s" % path)
        with zipfile.ZipFile(path) as z:
            self.infos = z.infolist()
            self.blobs = {i.filename: z.read(i.filename) for i in self.infos}
        self.path = path
        self._roots = {}
        self.dirty = set()
        self.new_parts = []
        self.main = self._main_part()
        self.rels_name = "%s/_rels/%s.rels" % tuple(self.main.rsplit("/", 1))

    def _main_part(self):
        if "_rels/.rels" in self.blobs:
            root = etree.fromstring(self.blobs["_rels/.rels"], PARSER)
            for rel in root:
                if str(rel.get("Type", "")).endswith("/officeDocument"):
                    return rel.get("Target").lstrip("/")
        if "word/document.xml" in self.blobs:
            return "word/document.xml"
        raise DocxError("본문(document.xml)을 찾지 못했습니다.")

    def exists(self, name):
        return name in self.blobs

    def root(self, name):
        if name not in self._roots:
            self._roots[name] = etree.fromstring(self.blobs[name], PARSER)
        return self._roots[name]

    def mark(self, name):
        self.dirty.add(name)

    def add_part(self, name, root):
        self._roots[name] = root
        self.blobs[name] = b""
        self.dirty.add(name)
        self.new_parts.append(name)

    def rels(self):
        """본문 관계: [(Id, Type, Target 경로)]"""
        out = []
        if not self.exists(self.rels_name):
            return out
        base = self.main.rsplit("/", 1)[0]
        for rel in self.root(self.rels_name):
            target = rel.get("Target", "")
            if rel.get("TargetMode") == "External":
                continue
            full = target.lstrip("/") if target.startswith("/") else os.path.normpath(
                base + "/" + target).replace("\\", "/")
            out.append((rel.get("Id"), rel.get("Type", ""), full))
        return out

    def rel_target(self, kind):
        for _id, typ, full in self.rels():
            if typ == REL_BASE + kind:
                return full
        return None

    def serialize(self, name):
        return XML_DECL + etree.tostring(self._roots[name], encoding="UTF-8")

    def save(self, out):
        out = str(out)
        tmp = out + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for info in self.infos:
                data = self.serialize(info.filename) if info.filename in self.dirty else self.blobs[info.filename]
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                z.writestr(zi, data)
            for name in self.new_parts:
                zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, self.serialize(name))
        os.replace(tmp, out)


def text_parts(doc):
    """편집 가능한 파트: [(별칭, 경로)]"""
    parts = [("document", doc.main)]
    for alias, kind in (("footnotes", "footnotes"), ("endnotes", "endnotes")):
        target = doc.rel_target(kind)
        if target and doc.exists(target):
            parts.append((alias, target))
    for _id, typ, full in sorted(doc.rels(), key=lambda x: x[2]):
        if typ in (REL_BASE + "header", REL_BASE + "footer") and doc.exists(full):
            parts.append((full.rsplit("/", 1)[-1][:-4], full))
    return parts


def resolve_part(doc, alias):
    alias = alias or "document"
    for a, name in text_parts(doc):
        if alias in (a, name):
            return a, name
    raise EditError("파트를 찾지 못했습니다: %s (가능: %s)" % (alias, ", ".join(a for a, _ in text_parts(doc))))


# ---------------------------------------------------------------- 문단·글자 모델
def _skip_para(p):
    for anc in p.iterancestors():
        tag = anc.tag
        if tag == w("txbxContent") or tag == "{%s}Fallback" % MC_NS:
            return True
        if tag in (w("footnote"), w("endnote")) and anc.get(w("type")) in (
                "separator", "continuationSeparator", "continuationNotice"):
            return True
    return False


def part_paragraphs(root):
    return [p for p in root.iter(w("p")) if not _skip_para(p)]


def walk_runs(container, view, ctx=()):
    """view 기준으로 보이는 런(또는 필드·수식 단위)을 문서 순서로 낸다."""
    for child in container:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag == w("r"):
            yield child, ctx
        elif tag in PASS_CONTAINERS:
            yield from walk_runs(child, view, ctx)
        elif tag == w("sdt"):
            content = child.find(w("sdtContent"))
            if content is not None:
                yield from walk_runs(content, view, ctx)
        elif tag == w("fldSimple") or tag in (m("oMath"), m("oMathPara")):
            yield child, ctx
        elif tag in (w("ins"), w("moveTo")):
            if view != "reject":
                yield from walk_runs(child, view, ctx + ("ins",))
        elif tag in (w("del"), w("moveFrom")):
            if view == "reject":
                yield from walk_runs(child, view, ctx + ("del",))


class Item:
    __slots__ = ("kind", "token", "text", "run", "t", "protected", "first", "last", "start", "end")

    def __init__(self, kind, token=None, text="", run=None, t=None, protected=False, first=None, last=None):
        self.kind, self.token, self.text, self.run, self.t = kind, token, text, run, t
        self.protected, self.first, self.last = protected, first, last
        self.start = self.end = 0

    @property
    def char(self):
        return self.text if self.kind == "text" else TOKEN_CHAR[self.token]


def build_items(p, view, depth=0):
    """문단의 글자 단위 모델. depth는 문단 경계를 넘는 필드(목차 등)의 깊이."""
    items = []
    open_field = None
    if depth > 0:
        open_field = Item("token", "FLD", protected=True)
        items.append(open_field)
    for el, ctx in walk_runs(p, view):
        prot = bool(ctx)
        if el.tag != w("r"):
            if depth == 0:
                name = "FLD" if el.tag == w("fldSimple") else "MATH"
                items.append(Item("token", name, protected=True, first=el, last=el))
            continue
        for ch in el:
            ct = ch.tag
            if not isinstance(ct, str) or ct in RUN_SKIP:
                continue
            if ct == w("fldChar"):
                typ = ch.get(w("fldCharType"))
                if typ == "begin":
                    if depth == 0:
                        open_field = Item("token", "FLD", protected=True, first=el, last=None)
                        items.append(open_field)
                    depth += 1
                elif typ == "end" and depth > 0:
                    depth -= 1
                    if depth == 0 and open_field is not None:
                        open_field.last = el
                        open_field = None
                continue
            if depth > 0:
                continue
            if ct in (w("t"), w("delText")):
                if ch.text:
                    items.append(Item("text", None, ch.text, run=el, t=ch, protected=prot, first=el, last=el))
            else:
                items.append(Item("token", RUN_TOKEN.get(ct, "OBJ"), run=el, protected=True, first=el, last=el))
    pos = 0
    for it in items:
        it.start = pos
        pos += len(it.char)
        it.end = pos
    return items, depth


def items_text(items):
    return "".join(it.char for it in items)


def mark_state(p):
    ppr = p.find(w("pPr"))
    rpr = ppr.find(w("rPr")) if ppr is not None else None
    if rpr is None:
        return set()
    return {etree.QName(c).localname for c in rpr if c.tag in MARK_TAGS}


def part_view(root, view):
    """view('accept'|'reject')로 본 문단 텍스트 목록(내부 표식 포함, 메모 표식 제외)."""
    out, carry, depth = [], "", 0
    for p in part_paragraphs(root):
        items, depth = build_items(p, view, depth)
        txt = strip_cmt(items_text(items))
        st = mark_state(p)
        joined = bool(st & {"del", "moveFrom"}) if view == "accept" else bool(st & {"ins", "moveTo"})
        if joined:
            carry += txt
            continue
        out.append(carry + txt)
        carry = ""
    if carry:
        out.append(carry)
    return out


class ParaModel:
    __slots__ = ("part", "index", "p", "T", "depth_in", "has_revisions")

    def __init__(self, part, index, p, T, depth_in, has_revisions):
        self.part, self.index, self.p, self.T = part, index, p, T
        self.depth_in, self.has_revisions = depth_in, has_revisions


def build_models(doc):
    models = {}
    for alias, name in text_parts(doc):
        root = doc.root(name)
        lst, depth = [], 0
        for i, p in enumerate(part_paragraphs(root)):
            d0 = depth
            items, depth = build_items(p, "accept", depth)
            has_rev = any(el.tag in REVISION_TAGS for el in p.iter())
            lst.append(ParaModel(alias, i, p, items_text(items), d0, has_rev))
        models[alias] = lst
    return models


def next_para_sibling(p):
    nxt = p.getnext()
    while nxt is not None and (not isinstance(nxt.tag, str) or nxt.tag in MARKUP_SIBLINGS):
        nxt = nxt.getnext()
    return nxt if nxt is not None and nxt.tag == w("p") else None


# ---------------------------------------------------------------- 차이 계산
TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)


def _prefix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and nbsp(a[i]) == nbsp(b[i]):
        i += 1
    return i


def _suffix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and nbsp(a[-1 - i]) == nbsp(b[-1 - i]):
        i += 1
    return i


def diff_hunks(a, b, granularity="word"):
    """a를 b로 바꾸는 최소 수정 묶음 [(a_start, a_end, 새 글자)]. 공백·NBSP는 같게 본다."""
    if nbsp(a) == nbsp(b):
        return []
    if granularity == "edit":
        p = _prefix(a, b)
        s = _suffix(a[p:], b[p:])
        return [(p, len(a) - s, b[p:len(b) - s])]
    ta, tb = TOKEN_RE.findall(a), TOKEN_RE.findall(b)
    offs = [0]
    for t in ta:
        offs.append(offs[-1] + len(t))
    sm = SequenceMatcher(None, [nbsp(t) for t in ta], [nbsp(t) for t in tb], autojunk=False)
    if len(ta) > 8 and sm.ratio() < 0.3:  # 거의 새로 쓴 문장은 한 덩어리로 보여 준다
        i = 0
        while i < min(len(ta), len(tb)) and nbsp(ta[i]) == nbsp(tb[i]):
            i += 1
        j = 0
        while j < min(len(ta), len(tb)) - i and nbsp(ta[-1 - j]) == nbsp(tb[-1 - j]):
            j += 1
        return [(offs[i], offs[len(ta) - j], "".join(tb[i:len(tb) - j]))]
    hunks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a_s, a_e = offs[i1], offs[i2]
        old, new = a[a_s:a_e], "".join(tb[j1:j2])
        for s, e, t in _refine(old, new) if tag == "replace" else [(0, len(old), new)]:
            if s != e or t:
                hunks.append((a_s + s, a_s + e, t))
    return hunks


def _clean(old, new):
    """편집자가 읽기 쉬운 조각인가: 순수 삭제·삽입이거나, 공백을 넘지 않는 한 낱말 교체."""
    return not old or not new or not (re.search(r"\s", old) or re.search(r"\s", new))


def _trim(old, new):
    p = _prefix(old, new)
    s = _suffix(old[p:], new[p:])
    return p, len(old) - s, new[p:len(new) - s]


def _refine(old, new):
    """교체 구간을 더 작은 수정으로 나눈다. 2글자 이상 공통 부분만 살리고, 깔끔하지 않으면 통째로 교체."""
    sm = SequenceMatcher(None, nbsp(old), nbsp(new), autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 2]
    if blocks:
        subs, ai, bi = [], 0, 0
        for b in blocks:
            subs.append((ai, b.a, new[bi:b.b]))
            ai, bi = b.a + b.size, b.b + b.size
        subs.append((ai, len(old), new[bi:]))
        out = []
        for s, e, t in subs:
            if s == e and not t:
                continue
            ps, pe, pt = _trim(old[s:e], t)
            out.append((s + ps, s + pe, pt))
        if out and all(_clean(old[s:e], t) for s, e, t in out):
            return out
    s, e, t = _trim(old, new)
    if _clean(old[s:e], t):
        return [(s, e, t)]
    return [(0, len(old), new)]


def segment_diff(old, new, granularity):
    """개체 표식을 기준으로 구간을 나눠 비교한다. 표식은 절대 수정 대상이 되지 않는다."""
    t_old = [c for c in old if c in TOKEN_CHARS]
    t_new = [c for c in new if c in TOKEN_CHARS]
    if t_old != t_new:
        raise EditError("개체 표식(⟦...⟧)의 순서나 개수가 원문과 다릅니다. 원문: %s / 새 글: %s"
                        % (to_display("".join(t_old)) or "없음", to_display("".join(t_new)) or "없음"))
    hunks, base = [], 0
    for so, sn in zip(TOKEN_SPLIT_RE.split(old), TOKEN_SPLIT_RE.split(new)):
        for s, e, t in diff_hunks(so, sn, granularity):
            hunks.append((base + s, base + e, t))
        base += len(so) + 1
    return hunks


# ---------------------------------------------------------------- 편집 해석
class Plan:
    def __init__(self):
        self.hunks = []      # (s, e, 새 글자, 편집 번호)
        self.comments = []   # (s, e, 메모, 편집 번호)
        self.splits = []     # (위치, 편집 번호)
        self.merge = None    # 편집 번호
        self.delete = None   # 편집 번호
        self.delete_mark = False
        self.new_paras = []  # (before|after, 글자, 편집 번호): 새 문단 넣기


def load_edits(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as ex:
        raise EditError("편집 목록을 읽지 못했습니다(%s): %s" % (path, ex))
    if isinstance(data, list):
        data = {"edits": data}
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        raise EditError("편집 목록은 배열이거나 {\"edits\": [...]} 형식이어야 합니다.")
    if not data["edits"]:
        raise EditError("편집 목록이 비어 있습니다(수정 항목이 하나도 없음).")
    if not all(isinstance(e, dict) for e in data["edits"]):
        raise EditError("편집 항목은 모두 객체({\"para\": …, \"old\": …})여야 합니다.")
    return data


SURE_RULE = re.compile(r"^\s*(?:P1(?!b)|P2(?!b)|P3c|P3d|P3e|P4|S2a)")  # 모델이 편집 목록에 적는 규범 오류 규칙(교정 방침 문서)
AI_RULE = re.compile(r"^\s*[RULJQN]\d")


def tier_of(e):
    """수정의 확신도. 편집 항목의 tier가 우선이고, 없으면 규칙 ID로 추정한다."""
    t = str(e.get("tier") or "").strip()
    if t in ("확실", "권장", "제안", "확인"):
        return t
    rule = str(e.get("rule") or "")
    if SURE_RULE.match(rule):
        return "확실"
    if AI_RULE.match(rule):
        return "제안"
    return "권장"



DECOR_RULES = ("L1", "L2", "L4", "U2")  # 저정보량 장식 표현을 지우는 규칙


def decor_state(e):
    """'명시'(decor: true) / '추정'(decor 키가 없고 장식 규칙으로 글자를 줄인 수정) / None.
    decor 키가 있으면 그 값이 이긴다: decor: false는 장식을 지운 것이 아니다(평가를 남긴 채 표현만 줄인 압축 등)."""
    if "decor" in e:
        return "명시" if e["decor"] else None
    rule, old, new = (e.get("rule") or "").strip(), e.get("old") or "", e.get("new") or ""
    return "추정" if rule[:2] in DECOR_RULES and bool(old) and len(new) < len(old) else None


def is_decor(e):
    return decor_state(e) is not None


BIG_OPS = ("rewrite", "delete_paragraph", "merge_next", "split", "insert_paragraph")


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




def _spec_list(spec, key):
    """편집 목록의 보호 목록을 문자열/배열 어느 쪽이든 문자열 배열로 정규화한다."""
    v = spec.get(key) or []
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def gate_plans(models, plans, spec):
    """위험 수정 게이트: 수정마다(그 수정 하나만 적용한 문단을 앞뒤 문단과 함께) 위험을 판정하고, 유지(KEEP)+이유가 없는
    위험 수정은 파일에 쓰지 않는다(자동 되돌림). 적용·검증·보고서가 같은 결과를 내도록 편집 목록만 보고 정한다."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fidelity_check as fc
    genre, dialogue = spec.get("genre") or "일반", spec.get("dialogue") or "auto"
    terms, protected_text = _spec_list(spec, "terms"), _spec_list(spec, "protected_text")
    by_ei = {}
    for key, plan in plans.items():
        for h in plan.hunks:
            by_ei.setdefault(h[3], set()).add(key)
    log, revert = [], set()
    plain = lambda t: re.sub(r"⟦[^⟧]*⟧", " ", to_display(t or ""))
    orig_all = "\n".join(plain(m.T) for _part, lst in models.items() for m in lst)
    for ei in sorted(by_ei):
        kinds, why, where, lost, after_map, gitems = [], [], [], [], {}, []
        first_before = None
        for (part, pi) in sorted(by_ei[ei]):
            pm = models[part][pi]
            if first_before is None:
                first_before = plain(pm.T).strip()
            after = pm.T
            for s_, en, txt, _e in sorted((h for h in plans[(part, pi)].hunks if h[3] == ei), key=lambda h: h[0], reverse=True):
                after = after[:s_] + txt + after[en:]
            prev = models[part][pi - 1].T if pi > 0 else ""
            nxt = models[part][pi + 1].T if pi + 1 < len(models[part]) else ""
            k, w = fc.gate_signals(plain(pm.T), plain(after), plain(prev), plain(nxt), genre, dialogue, terms, protected_text)
            kinds += k; why += w; where.append("%s %d" % (part, pi)); lost += fc.gate_signals.last_lost; gitems += fc.gate_signals.last_items
            after_map[(part, pi)] = after
        if not kinds:
            continue
        e = spec["edits"][ei]
        final_all = "\n".join(plain(after_map.get((part_, m.index), m.T)) for part_, lst in models.items() for m in lst)
        dec, note = fc.gate_decide(e, sorted(set(kinds)), {"orig": orig_all, "final": final_all, "lost": lost, "items": gitems})
        if dec == "REVERT":
            revert.add(ei)
        log.append({"id": "E%03d" % (ei + 1), "where": ", ".join(where), "kinds": sorted(set(kinds), key=fc.CRITICAL_KINDS.index),
                    "why": why, "old": str(e["old"] if "old" in e else (first_before or ""))[:60], "new": str(e.get("new", e.get("new_text", "")))[:60],
                    "decision": dec, "note": note})
    if revert:
        for key in list(plans):
            plan = plans[key]
            plan.hunks = [h for h in plan.hunks if h[3] not in revert]
            if not (plan.hunks or plan.comments or plan.splits or plan.merge is not None or plan.delete is not None or plan.new_paras):
                del plans[key]
    return plans, log, revert


def gate_table(log):
    rows = ["| 번호 | 위치 | 위험 | 바꾼 것 | 판정 | 이유 |", "|---|---|---|---|---|---|"]
    for x in log:
        rows.append("| %s | %s | %s | %r → %r | %s | %s |" % (x["id"], x["where"], "·".join(x["kinds"]), x["old"], x["new"], x["decision"], x["note"].replace("|", "/")))
    return "\n".join(rows)



def reason_comments(spec, how):
    """수정 이유 메모. auto = '확실'이 아닌 수정만, all = 전부, none = 없음."""
    out = {}
    if how == "none":
        return out
    for i, e in enumerate(spec["edits"]):
        if _op_of(e) in ("comment", "merge_next"):
            continue  # 문단 병합은 같은 문단의 글자 수정 메모로 설명된다
        if how == "all" or tier_of(e) != "확실":
            body = " — ".join(x for x in (str(e.get("rule") or "").strip(), str(e.get("reason") or "").strip()) if x)
            if body:
                out[i] = "[%s] %s" % (tier_of(e), body)
    return out


def _op_of(e):
    if e.get("op"):
        return e["op"]
    if "new_text" in e:
        return "rewrite"
    if "old" in e:
        return "replace"
    raise EditError("op를 알 수 없습니다(new_text 또는 old/new가 필요).")


def _find(models, part, para, needle, what="old"):
    if not needle:
        raise EditError("%s가 비어 있습니다." % what)
    nd = nbsp(needle)
    hits = []
    for pm in models[part]:
        if para is not None and pm.index != para:
            continue
        hay = nbsp(pm.T)
        k = hay.find(nd)
        while k >= 0:
            hits.append((pm, k, k + len(nd)))
            k = hay.find(nd, k + max(1, len(nd)))
    return hits


def _pick(hits, e, needle, part):
    where = "[%s %s]" % (part, e["para"]) if e.get("para") is not None else "[%s 전체]" % part
    if not hits:
        raise EditError("%s에서 '%s'을(를) 찾지 못했습니다. extract 결과의 글자를 그대로 복사하세요."
                        % (where, to_display(needle)))
    if e.get("all"):
        return hits
    occ = e.get("occurrence")
    if occ is not None:
        if not 1 <= int(occ) <= len(hits):
            raise EditError("%s에서 '%s'은(는) %d번 나옵니다. occurrence=%s는 범위 밖입니다."
                            % (where, to_display(needle), len(hits), occ))
        return [hits[int(occ) - 1]]
    if len(hits) > 1:
        where_count = Counter("%s %d" % (pm.part, pm.index) for pm, _, _ in hits)
        cand = ", ".join("%s(%d회)" % (k, v) for k, v in list(where_count.items())[:6])
        raise EditError("%s에서 '%s'이(가) %d번 나옵니다(%s). para·occurrence로 지정하거나 앞뒤 글자를 늘리세요."
                        % (where, to_display(needle), len(hits), cand))
    return hits


def _model(models, part, para):
    try:
        return models[part][int(para)]
    except (IndexError, ValueError, TypeError):
        raise EditError("[%s %s] 문단이 없습니다." % (part, para))


def _check_editable_span(pm, s, e, label):
    items, _ = build_items(pm.p, "accept", pm.depth_in)
    for it in items:
        overlap = it.end > s and it.start < e
        inside = it.start < s < it.end
        if (overlap or inside) and it.protected and it.kind == "text":
            raise EditError("[%s %d] %s 범위가 기존 변경 추적(다른 사람의 삽입) 구간과 겹칩니다."
                            % (pm.part, pm.index, label))
        if it.kind == "token" and it.last is None and it.start < e and s >= it.start:
            raise EditError("[%s %d] %s 위치가 문단 경계를 넘는 필드 안쪽입니다." % (pm.part, pm.index, label))


def resolve(doc, models, spec, granularity):
    plans, errors, warnings, summaries = {}, [], [], []
    for ei, e in enumerate(spec["edits"]):
        try:
            if not isinstance(e, dict):
                raise EditError("편집 항목은 객체여야 합니다.")
            op = _op_of(e)
            part, _ = resolve_part(doc, e.get("part"))
            para = e.get("para")
            if para is None and e.get("find") is not None:
                hits = _find(models, part, None, from_display(e["find"]), "find")
                if len({pm.index for pm, _, _ in hits}) != 1:
                    raise EditError("find '%s'에 해당하는 문단이 하나가 아닙니다." % e["find"])
                para = hits[0][0].index
            if op in ("rewrite", "replace", "insert", "delete"):
                targets = []
                if op == "rewrite":
                    pm = _model(models, part, para)
                    targets.append((pm, 0, len(pm.T), from_display(e["new_text"])))
                else:
                    if op == "insert":
                        text = from_display(e.get("text", ""))
                        if not text:
                            raise EditError("insert에는 text가 필요합니다.")
                        if e.get("after") is not None:
                            anchor = from_display(e["after"])
                            old, new = anchor, anchor + text
                        elif e.get("before") is not None:
                            anchor = from_display(e["before"])
                            old, new = anchor, text + anchor
                        else:
                            raise EditError("insert에는 after 또는 before 앵커가 필요합니다.")
                    else:
                        old = from_display(e.get("old", ""))
                        new = "" if op == "delete" else from_display(e.get("new", ""))
                    for pm, s, en in _pick(_find(models, part, para, old), e, old, part):
                        targets.append((pm, s, en, new))
                for pm, s, en, new in targets:
                    hunks = segment_diff(pm.T[s:en], new, granularity)
                    if not hunks:
                        warnings.append("편집 %d: 원문과 같아서 바뀐 것이 없습니다." % ei)
                    for hs, he, ht in hunks:
                        _check_editable_span(pm, s + hs, s + he, "수정")
                        plans.setdefault((part, pm.index), Plan()).hunks.append((s + hs, s + he, ht, ei))
                        summaries.append((ei, pm.part, pm.index, to_display(pm.T[s + hs:s + he]), to_display(ht)))
            elif op == "comment":
                text = e.get("text") or e.get("comment")
                if not text:
                    raise EditError("comment에는 text가 필요합니다.")
                if e.get("anchor"):
                    anchor = from_display(e["anchor"])
                    for pm, s, en in _pick(_find(models, part, para, anchor, "anchor"), e, anchor, part):
                        _check_editable_span(pm, s, en, "메모")
                        plans.setdefault((part, pm.index), Plan()).comments.append((s, en, text, ei))
                else:
                    pm = _model(models, part, para)
                    plans.setdefault((part, pm.index), Plan()).comments.append((0, len(pm.T), text, ei))
            elif op == "split":
                anchor = from_display(e.get("before", ""))
                for pm, s, en in _pick(_find(models, part, para, anchor, "before"), e, anchor, part):
                    k = 0
                    if e.get("delete_space", True):
                        while s - k > 0 and pm.T[s - k - 1] in " \u00a0\u3000":
                            k += 1
                    if s - k <= 0:
                        raise EditError("[%s %d] 문단 처음에서는 나눌 수 없습니다." % (part, pm.index))
                    plan = plans.setdefault((part, pm.index), Plan())
                    plan.splits.append((s, ei))
                    if k:
                        plan.hunks.append((s - k, s, "", ei))
                    summaries.append((ei, part, pm.index, "¶ 문단 나누기", to_display(pm.T[s:s + 12]) + "…"))
            elif op == "merge_next":
                pm = _model(models, part, para)
                nxt = next_para_sibling(pm.p)
                lst = models[part]
                if nxt is None or pm.index + 1 >= len(lst) or lst[pm.index + 1].p is not nxt:
                    raise EditError("[%s %d] 바로 뒤에 합칠 문단이 없습니다(표·구역 경계 포함)." % (part, pm.index))
                ppr = pm.p.find(w("pPr"))
                if ppr is not None and ppr.find(w("sectPr")) is not None:
                    raise EditError("[%s %d] 구역 나누기가 있는 문단은 합칠 수 없습니다." % (part, pm.index))
                if mark_state(pm.p):
                    raise EditError("[%s %d] 문단 표시에 이미 변경 추적이 있습니다." % (part, pm.index))
                nxt_T = lst[pm.index + 1].T
                joiner = e.get("joiner")
                if joiner is None:
                    joiner = "" if (not pm.T or not nxt_T or pm.T[-1].isspace() or nxt_T[0].isspace()) else " "
                plan = plans.setdefault((part, pm.index), Plan())
                if plan.merge is not None:
                    raise EditError("[%s %d] merge_next가 중복되었습니다." % (part, pm.index))
                plan.merge = ei
                if joiner:
                    plan.hunks.append((len(pm.T), len(pm.T), from_display(joiner), ei))
                if _para_signature(pm.p) != _para_signature(nxt):
                    warnings.append("편집 %d: [%s %d]와 다음 문단의 스타일·목록 설정이 다릅니다. 병합 결과 서식을 확인하세요."
                                    % (ei, part, pm.index))
                summaries.append((ei, part, pm.index, "¶ 다음 문단과 합치기", "연결: %r" % joiner))
            elif op == "insert_paragraph":
                pm = _model(models, part, para)
                raw = e.get("text", "")
                lines = [from_display(x) for x in (raw if isinstance(raw, list) else str(raw).split("\n")) if str(x).strip()]
                if not lines:
                    raise EditError("insert_paragraph에는 text가 필요합니다(여러 문단이면 줄바꿈이나 목록).")
                text = "\n".join(lines)
                where = e.get("position", "after")
                if where not in ("after", "before"):
                    raise EditError("insert_paragraph의 position은 after 또는 before입니다.")
                plan = plans.setdefault((part, pm.index), Plan())
                for line in lines:
                    plan.new_paras.append((where, line, ei))
                summaries.append((ei, part, pm.index, "¶ 새 문단(%s)" % ("앞" if where == "before" else "뒤"), to_display(text)))
            elif op == "delete_paragraph":
                pm = _model(models, part, para)
                if pm.has_revisions:
                    raise EditError("[%s %d] 기존 변경 추적이 있는 문단은 통째로 지울 수 없습니다." % (part, pm.index))
                if any(el.tag in (m("oMath"), m("oMathPara")) for el in pm.p.iter()):
                    raise EditError("[%s %d] 수식이 있는 문단은 통째로 지울 수 없습니다." % (part, pm.index))
                plan = plans.setdefault((part, pm.index), Plan())
                plan.delete = ei
                nxt = next_para_sibling(pm.p)
                lst = models[part]
                plan.delete_mark = bool(nxt is not None and pm.index + 1 < len(lst) and lst[pm.index + 1].p is nxt)
                if not plan.delete_mark:
                    warnings.append("편집 %d: [%s %d]는 영역의 마지막 문단이라 빈 문단이 남습니다." % (ei, part, pm.index))
                summaries.append((ei, part, pm.index, to_display(pm.T), "(문단 삭제)"))
            else:
                raise EditError("알 수 없는 op: %s" % op)
        except EditError as exc:
            errors.append("편집 %d: %s" % (ei, exc))
    # 같은 문단 안의 충돌 검사
    for (part, pi), plan in plans.items():
        if plan.delete is not None and (plan.hunks or plan.splits or plan.merge is not None or plan.new_paras):
            errors.append("[%s %d] 문단 삭제와 다른 편집이 겹칩니다." % (part, pi))
        if plan.merge is not None and any(x[0] == "after" for x in plan.new_paras):
            errors.append("[%s %d] 다음 문단과 합치기와 뒤에 새 문단 넣기가 겹칩니다." % (part, pi))
        if any(x[0] == "before" for x in plan.new_paras) and pi > 0:
            prev_plan = plans.get((part, pi - 1))
            if prev_plan is not None and prev_plan.merge is not None:
                errors.append("[%s %d] 앞 문단 합치기와 앞에 새 문단 넣기가 겹칩니다." % (part, pi))
        hs = sorted(plan.hunks, key=lambda h: (h[0], h[1]))
        for a, b in zip(hs, hs[1:]):
            if b[0] < a[1]:
                errors.append("[%s %d] 편집 %d·%d의 수정 범위가 겹칩니다." % (part, pi, a[3], b[3]))
        for off, ei in plan.splits:
            for s, e2, _t, ej in plan.hunks:
                if s < off < e2:
                    errors.append("[%s %d] 문단 나누기(편집 %d) 위치가 편집 %d의 수정 범위 안입니다." % (part, pi, ei, ej))
    return plans, errors, warnings, summaries


def _para_signature(p):
    ppr = p.find(w("pPr"))
    if ppr is None:
        return (None, None)
    st = ppr.find(w("pStyle"))
    num = ppr.find(w("numPr"))
    return (st.get(w("val")) if st is not None else None, num is not None)


# ---------------------------------------------------------------- 기대 결과(시뮬레이션)
def apply_hunks_text(T, hunks, points):
    hunks = sorted(hunks, key=lambda h: (h[0], h[1]))
    out, pos = [], 0
    for s, e, t, *_ in hunks:
        out.append(T[pos:s])
        out.append(t)
        pos = e
    out.append(T[pos:])
    mapped = []
    for o in points:
        shift = sum(len(t) - (e - s) for s, e, t, *_ in hunks if e <= o)
        mapped.append(o + shift)
    return "".join(out), mapped


def simulate(models, plans):
    result = {}
    for part, lst in models.items():
        out, carry = [], ""
        for pm in lst:
            plan = plans.get((part, pm.index))
            if plan is None:
                pieces, mark_del = [pm.T], False
            elif plan.delete is not None:
                pieces, mark_del = [""], plan.delete_mark
            else:
                newT, pts = apply_hunks_text(pm.T, plan.hunks, sorted(o for o, _ in plan.splits))
                pieces, prev = [], 0
                for q in pts:
                    pieces.append(newT[prev:q])
                    prev = q
                pieces.append(newT[prev:])
                mark_del = plan.merge is not None
            pieces[0] = carry + pieces[0]
            carry = ""
            if plan is not None:
                out.extend(t for wh, t, _ei in plan.new_paras if wh == "before")
            if mark_del:
                carry = pieces.pop()
            out.extend(pieces)
            if plan is not None and plan.delete is None:
                out.extend(t for wh, t, _ei in plan.new_paras if wh == "after")
        if carry:
            out.append(carry)
        result[part] = [strip_cmt(x) for x in out]
    return result


# ---------------------------------------------------------------- XML 변경
class Rev:
    def __init__(self, start_id, author, date, initials):
        self._next = start_id
        self.author, self.date, self.initials = author, date, initials
        self.counts = Counter()

    def next_id(self):
        v = self._next
        self._next += 1
        return v

    def make(self, tag):
        el = etree.Element(tag)
        el.set(w("id"), str(self.next_id()))
        el.set(w("author"), self.author)
        el.set(w("date"), self.date)
        self.counts[etree.QName(tag).localname] += 1
        return el


def max_existing_id(doc):
    mx = 0
    for name in doc.blobs:
        if not name.endswith(".xml"):
            continue
        try:
            root = doc.root(name)
        except etree.XMLSyntaxError:
            continue
        for el in root.iter():
            v = el.get(w("id")) if isinstance(el.tag, str) else None
            if v is not None and v.lstrip("-").isdigit():
                mx = max(mx, int(v))
    return mx


def outer(el):
    while el.getparent() is not None and el.getparent().tag in REV_WRAPPERS:
        el = el.getparent()
    return el


def strip_proof(p):
    for pe in list(p.iter(w("proofErr"))):
        pe.getparent().remove(pe)


def presplit_runs(p):
    """여러 내용 요소를 가진 런을 요소 하나짜리 런들로 나눈다(렌더링 동일)."""
    for el, ctx in list(walk_runs(p, "accept")):
        if el.tag != w("r") or ctx:
            continue
        content = [c for c in el if isinstance(c.tag, str) and c.tag != w("rPr")]
        if len(content) <= 1:
            continue
        rpr = el.find(w("rPr"))
        for c in content[:-1]:
            nr = etree.Element(w("r"), dict(el.attrib))
            if rpr is not None:
                nr.append(copy.deepcopy(rpr))
            nr.append(c)
            el.addprevious(nr)


def split_items(items, bounds):
    for it in items:
        if it.kind != "text":
            continue
        cuts = sorted(b - it.start for b in bounds if it.start < b < it.end)
        if not cuts:
            continue
        if it.protected:
            raise EditError("기존 변경 추적 구간 안을 나눌 수 없습니다.")
        text = it.text
        pieces = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
        for piece in pieces[:-1]:
            nr = copy.deepcopy(it.run)
            t = nr.find(w("t"))
            t.text = piece
            t.set(XML_SPACE, "preserve")
            it.run.addprevious(nr)
        it.t.text = pieces[-1]
        it.t.set(XML_SPACE, "preserve")


def clean_rpr(rpr):
    if rpr is None:
        return None
    r = copy.deepcopy(rpr)
    r.tag = w("rPr")
    for c in list(r):
        if c.tag in MARK_TAGS or c.tag == w("rPrChange"):
            r.remove(c)
    return r


def para_mark_rpr(p):
    ppr = p.find(w("pPr"))
    return ppr.find(w("rPr")) if ppr is not None else None


def wrap_deleted(runs, rev):
    groups = []
    for r in runs:
        if groups and groups[-1][-1].getnext() is r:
            groups[-1].append(r)
        else:
            groups.append([r])
    dels = []
    for g in groups:
        d = rev.make(w("del"))
        g[0].addprevious(d)
        for r in g:
            d.append(r)
            for t in r.iter(w("t")):
                t.tag = w("delText")
                t.set(XML_SPACE, "preserve")
            for t in r.iter(w("instrText")):
                t.tag = w("delInstrText")
        dels.append(d)
    return dels


def make_ins(text, rpr, rev):
    ins = rev.make(w("ins"))
    r = etree.SubElement(ins, w("r"))
    rp = clean_rpr(rpr)
    if rp is not None:
        r.append(rp)
    t = etree.SubElement(r, w("t"))
    t.text = text
    t.set(XML_SPACE, "preserve")
    return ins


def ensure_ppr(p):
    ppr = p.find(w("pPr"))
    if ppr is None:
        ppr = etree.Element(w("pPr"))
        p.insert(0, ppr)
    return ppr


def ensure_mark_rpr(ppr):
    rpr = ppr.find(w("rPr"))
    if rpr is None:
        rpr = etree.Element(w("rPr"))
        tail = [c for c in ppr if c.tag in PPR_TAIL]
        if tail:
            tail[0].addprevious(rpr)
        else:
            ppr.append(rpr)
    return rpr


def mark_para(p, kind, rev):
    rpr = ensure_mark_rpr(ensure_ppr(p))
    el = rev.make(w(kind))
    if kind == "ins":
        rpr.insert(0, el)
    else:
        idx = 1 if len(rpr) and rpr[0].tag == w("ins") else 0
        rpr.insert(idx, el)


def _text_rpr(items, idx, direction):
    rng = range(idx, -1, -1) if direction < 0 else range(idx, len(items))
    for k in rng:
        if 0 <= k < len(items) and items[k].kind == "text":
            return items[k].run.find(w("rPr"))
    return None


def _container(el):
    return outer(el).getparent()


def delete_runs_for_paragraph(p):
    runs = []

    def walk(container):
        for child in container:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag == w("r"):
                runs.append(child)
            elif tag in PASS_CONTAINERS or tag == w("fldSimple"):
                walk(child)
            elif tag == w("sdt"):
                c = child.find(w("sdtContent"))
                if c is not None:
                    walk(c)
    walk(p)
    return runs


def add_comment_markers(first_el, last_el, cid, ref_style):
    start = etree.Element(w("commentRangeStart"))
    start.set(w("id"), str(cid))
    end = etree.Element(w("commentRangeEnd"))
    end.set(w("id"), str(cid))
    ref = etree.Element(w("r"))
    if ref_style:
        rpr = etree.SubElement(ref, w("rPr"))
        etree.SubElement(rpr, w("rStyle")).set(w("val"), ref_style)
    etree.SubElement(ref, w("commentReference")).set(w("id"), str(cid))
    outer(first_el).addprevious(start)
    last = outer(last_el)
    last.addnext(end)
    end.addnext(ref)


def apply_plan(pm, plan, rev, ctx):
    p = pm.p
    strip_proof(p)
    presplit_runs(p)
    items, _ = build_items(p, "accept", pm.depth_in)
    if items_text(items) != pm.T:
        raise DocxError("[%s %d] 내부 텍스트 모델이 일치하지 않습니다." % (pm.part, pm.index))
    created = {}
    if plan.delete is not None:
        dels = wrap_deleted(delete_runs_for_paragraph(p), rev)
        created.setdefault(plan.delete, []).extend(dels)
        if plan.delete_mark:
            mark_para(p, "del", rev)
        reason = ctx["reasons"].get(plan.delete) if ctx.get("comment_reasons") else None
        if reason and dels:  # 문단 삭제는 판단이 큰 수정이라 이유를 남긴다
            cid = rev.next_id()
            ctx["comments"].append((cid, reason))
            add_comment_markers(dels[0], dels[-1], cid, ctx["ref_style"])
            rev.counts["comment"] += 1
        return created
    bounds = set()
    for s, e, _t, _ei in plan.hunks + plan.comments:
        bounds.update((s, e))
    bounds.update(o for o, _ in plan.splits)
    split_items(items, bounds)
    items, _ = build_items(p, "accept", pm.depth_in)

    def first_at(o):
        for it in items:
            if it.start == o and it.end > it.start:
                return it
        return None

    # 메모와 문단 나누기 위치는 변경 전에 잡아 둔다.
    comment_targets = []
    for s, e, text, ei in plan.comments:
        span = [it for it in items if it.start >= s and it.end <= e and it.end > it.start]
        comment_targets.append((span, text, ei))
    split_targets = []
    for o, ei in sorted(plan.splits):
        it = first_at(o)
        if it is None or it.first is None:
            raise EditError("[%s %d] 문단 나누기 위치를 찾지 못했습니다." % (pm.part, pm.index))
        split_targets.append((it.first, ei))

    after_anchor = {}
    for s, e, text, ei in sorted(plan.hunks, key=lambda h: (h[0], h[1])):
        dels_items = [it for it in items if it.start >= s and it.end <= e and it.end > it.start]
        for it in dels_items:
            if it.kind != "text" or it.protected:
                raise EditError("[%s %d] 개체나 보호 구간은 수정할 수 없습니다." % (pm.part, pm.index))
        made = created.setdefault(ei, [])
        if dels_items:
            dels = wrap_deleted([it.run for it in dels_items], rev)
            made.extend(dels)
            first_parent = dels[0].getparent()
            anchor = [d for d in dels if d.getparent() is first_parent][-1]
            if text:
                ins = make_ins(text, dels_items[0].run.find(w("rPr")), rev)
                anchor.addnext(ins)
                made.append(ins)
                anchor = ins
            after_anchor[e] = anchor
            continue
        # 순수 삽입
        idx_prev = max((k for k, it in enumerate(items) if it.end == s and it.end > it.start), default=None)
        idx_next = min((k for k, it in enumerate(items) if it.start == s and it.end > it.start), default=None)
        if s in after_anchor:
            src = _text_rpr(items, idx_prev if idx_prev is not None else (idx_next or 0), -1) \
                if idx_prev is not None else _text_rpr(items, idx_next or 0, 1)
            ins = make_ins(text, src, rev)
            after_anchor[s].addnext(ins)
        else:
            prev_it = items[idx_prev] if idx_prev is not None else None
            next_it = items[idx_next] if idx_next is not None else None
            if prev_it is not None and prev_it.last is None:
                raise EditError("[%s %d] 필드 안쪽에는 삽입할 수 없습니다." % (pm.part, pm.index))
            use_next = (prev_it is not None and next_it is not None and next_it.first is not None
                        and _container(prev_it.last) is not _container(next_it.first)
                        and _container(prev_it.last).tag == w("hyperlink"))
            if prev_it is not None and not use_next:
                src = _text_rpr(items, idx_prev, -1) or _text_rpr(items, idx_prev, 1)
                ins = make_ins(text, src if src is not None else para_mark_rpr(p), rev)
                outer(prev_it.last).addnext(ins)
            elif next_it is not None and next_it.first is not None:
                src = _text_rpr(items, idx_next, 1) or _text_rpr(items, idx_next, -1)
                ins = make_ins(text, src if src is not None else para_mark_rpr(p), rev)
                outer(next_it.first).addprevious(ins)
            else:
                ins = make_ins(text, para_mark_rpr(p), rev)
                p.append(ins)
        made.append(ins)
        after_anchor[s] = ins

    for span, text, ei in comment_targets:
        cid = rev.next_id()
        ctx["comments"].append((cid, text))
        if span:
            last_el = span[-1].last if span[-1].last is not None else span[-1].first
            add_comment_markers(span[0].first, last_el, cid, ctx["ref_style"])
        else:
            anchor = etree.Element(w("r"))
            p.append(anchor)
            add_comment_markers(anchor, anchor, cid, ctx["ref_style"])
            p.remove(anchor)
        rev.counts["comment"] += 1

    if ctx.get("comment_reasons"):
        order = {el: i for i, el in enumerate(p.iter())}
        seen = set()
        for ei, els in created.items():
            reason = ctx["reasons"].get(ei)
            if reason in seen:
                continue  # 같은 문단의 같은 이유는 한 번만
            seen.add(reason)
            els = [x for x in els if x in order]
            if reason and els:
                cid = rev.next_id()
                ctx["comments"].append((cid, reason))
                add_comment_markers(min(els, key=order.get), max(els, key=order.get), cid, ctx["ref_style"])
                rev.counts["comment"] += 1

    def blank_para():
        new_p = etree.Element(w("p"))
        ppr = p.find(w("pPr"))
        new_ppr = copy.deepcopy(ppr) if ppr is not None else etree.Element(w("pPr"))
        for c in list(new_ppr):
            if c.tag in PPR_TAIL:
                new_ppr.remove(c)
        mrpr = new_ppr.find(w("rPr"))
        if mrpr is not None:
            for c in list(mrpr):
                if c.tag in MARK_TAGS:
                    mrpr.remove(c)
        new_p.append(new_ppr)
        return new_p

    def fill_new(new_p, text, ei):
        ins = make_ins(text, para_mark_rpr(p), rev)
        new_p.append(ins)
        created.setdefault(ei, []).append(ins)
        reason = ctx["reasons"].get(ei) if ctx.get("comment_reasons") else None
        if reason:
            cid = rev.next_id()
            ctx["comments"].append((cid, reason))
            add_comment_markers(ins, ins, cid, ctx["ref_style"])
            rev.counts["comment"] += 1
        rev.counts["new_para"] += 1

    # 새 문단 넣기: 앞 문단은 나누기보다 먼저 넣어 맨 앞에 오게 한다. 새 문단의 문단 표시는 삽입으로 표시하므로
    # 모두 거부하면 새 문단이 통째로 사라진다. 뒤에 넣는 문단이 영역의 마지막이면 앞 문단의 표시를 삽입으로 둔다.
    if plan.new_paras and mark_state(p):
        raise EditError("[%s %d] 문단 표시에 이미 변경 추적이 있어 새 문단을 넣을 수 없습니다." % (pm.part, pm.index))
    for wh, text, ei in plan.new_paras:
        if wh == "before":
            new_p = blank_para()
            fill_new(new_p, text, ei)
            p.addprevious(new_p)
            mark_para(new_p, "ins", rev)
    afters = [(t, ei) for wh, t, ei in plan.new_paras if wh == "after"]
    if afters:
        chain, anchor = [p], p
        for text, ei in afters:
            new_p = blank_para()
            fill_new(new_p, text, ei)
            anchor.addnext(new_p)
            chain.append(new_p)
            anchor = new_p
        if next_para_sibling(chain[-1]) is not None:
            for q in chain[1:]:
                mark_para(q, "ins", rev)
        else:
            for q in chain[:-1]:
                mark_para(q, "ins", rev)

    for first_el, _ei in split_targets:
        top = first_el
        while top.getparent() is not p:
            if top.getparent() is None:
                raise EditError("[%s %d] 문단 나누기 위치를 찾지 못했습니다." % (pm.part, pm.index))
            top = top.getparent()
        if top is not first_el:
            runs = [r for r in top.iter(w("r"))]
            if not runs or runs[0] is not first_el:
                raise EditError("[%s %d] 하이퍼링크·콘텐츠 컨트롤 안에서는 문단을 나눌 수 없습니다." % (pm.part, pm.index))
        new_p = etree.Element(w("p"))
        ppr = p.find(w("pPr"))
        new_ppr = copy.deepcopy(ppr) if ppr is not None else etree.Element(w("pPr"))
        for c in list(new_ppr):
            if c.tag in PPR_TAIL:
                new_ppr.remove(c)
        mrpr = new_ppr.find(w("rPr"))
        if mrpr is not None:
            for c in list(mrpr):
                if c.tag in MARK_TAGS:
                    mrpr.remove(c)
        new_p.append(new_ppr)
        for child in list(p):
            if child is top:
                break
            if child.tag == w("pPr"):
                continue
            new_p.append(child)
        p.addprevious(new_p)
        mark_para(new_p, "ins", rev)
        rev.counts["split"] += 1

    if plan.merge is not None:
        mark_para(p, "del", rev)
        rev.counts["merge"] += 1
    return created


def ensure_comments_part(doc, comments, rev):
    if not comments:
        return
    target = doc.rel_target("comments")
    if target and doc.exists(target):
        root = doc.root(target)
        name = target
    else:
        name = doc.main.rsplit("/", 1)[0] + "/comments.xml"
        root = etree.Element(w("comments"), nsmap={"w": W_NS})
        doc.add_part(name, root)
        rels = doc.root(doc.rels_name)
        ids = {r.get("Id") for r in rels}
        n = 1
        while "rId%d" % n in ids:
            n += 1
        rel = etree.SubElement(rels, "{%s}Relationship" % PKG_REL_NS)
        rel.set("Id", "rId%d" % n)
        rel.set("Type", REL_BASE + "comments")
        rel.set("Target", name.rsplit("/", 1)[-1])
        doc.mark(doc.rels_name)
        ct = doc.root("[Content_Types].xml")
        if not any(o.get("PartName") == "/" + name for o in ct):
            ov = etree.SubElement(ct, "{%s}Override" % CT_NS)
            ov.set("PartName", "/" + name)
            ov.set("ContentType", COMMENTS_CT)
            doc.mark("[Content_Types].xml")
    for cid, text in comments:
        c = etree.SubElement(root, w("comment"))
        c.set(w("id"), str(cid))
        c.set(w("author"), rev.author)
        c.set(w("date"), rev.date)
        c.set(w("initials"), rev.initials)
        for line in str(text).split("\n"):
            cp = etree.SubElement(c, w("p"))
            r = etree.SubElement(cp, w("r"))
            t = etree.SubElement(r, w("t"))
            t.text = line
            t.set(XML_SPACE, "preserve")
    doc.mark(name)


def ensure_track_flag(doc):
    target = doc.rel_target("settings")
    if not target or not doc.exists(target):
        return False
    root = doc.root(target)
    if root.find(w("trackRevisions")) is not None:
        return True
    el = etree.Element(w("trackRevisions"))
    mine = SETTINGS_INDEX["trackRevisions"]
    for child in root:
        if not isinstance(child.tag, str) or etree.QName(child).namespace != W_NS:
            continue
        idx = SETTINGS_INDEX.get(etree.QName(child).localname)
        if idx is not None and idx > mine:
            child.addprevious(el)
            break
    else:
        root.append(el)
    doc.mark(target)
    return True


def style_names(doc):
    names = {}
    target = doc.rel_target("styles")
    if target and doc.exists(target):
        for st in doc.root(target).iter(w("style")):
            n = st.find(w("name"))
            names[st.get(w("styleId"))] = n.get(w("val")) if n is not None else st.get(w("styleId"))
    return names


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 명령: extract
def cmd_extract(args):
    doc = Docx(args.input)
    models = build_models(doc)
    names = style_names(doc)
    warnings = []
    paras = []
    rev_count = 0
    for alias, name in text_parts(doc):
        root = doc.root(name)
        rev_count += sum(1 for el in root.iter() if el.tag in (w("ins"), w("del"), w("moveFrom"), w("moveTo")))
        skipped = sum(1 for p in root.iter(w("p")) if _skip_para(p))
        if skipped and alias == "document":
            n_tb = sum(1 for p in root.iter(w("p")) if any(a.tag == w("txbxContent") for a in p.iterancestors()))
            if n_tb:
                warnings.append("텍스트 상자 안 문단 %d개는 편집 대상에서 제외했습니다." % n_tb)
        for pm in models[alias]:
            ppr = pm.p.find(w("pPr"))
            sid = None
            numbered = False
            if ppr is not None:
                st = ppr.find(w("pStyle"))
                sid = st.get(w("val")) if st is not None else None
                numbered = ppr.find(w("numPr")) is not None
            cell = None
            tc = next((a for a in pm.p.iterancestors(w("tc"))), None)
            if tc is not None:
                tr = tc.getparent()
                tbl = tr.getparent()
                cell = "r%d c%d" % (list(tbl.iterchildren(w("tr"))).index(tr) + 1,
                                    list(tr.iterchildren(w("tc"))).index(tc) + 1)
            paras.append({
                "part": alias, "para": pm.index, "style": names.get(sid, sid) if sid else None,
                "list": numbered, "table_cell": cell, "text": to_display(pm.T),
                "existing_revisions": pm.has_revisions,
            })
    if rev_count:
        warnings.append("기존 변경 추적 %d건이 있습니다. 해당 구간은 보호되어 수정할 수 없습니다. "
                        "가능하면 먼저 수락·거부한 파일로 작업하세요." % rev_count)
    all_text = "".join(pm.T for lst in models.values() for pm in lst)
    if re.search("[\u1100-\u11ff]", all_text):
        warnings.append("자모가 분리된(NFD) 한글이 있어 old 찾기가 실패할 수 있습니다.")
    if "⟦" in all_text:
        warnings.append("원문에 '⟦' 문자가 있어 개체 표식과 헷갈릴 수 있습니다.")
    settings = doc.rel_target("settings")
    if settings and doc.exists(settings) and doc.root(settings).find(w("documentProtection")) is not None:
        warnings.append("문서 보호 설정이 있습니다. Word에서 편집이 제한될 수 있습니다.")
    if args.json:
        out = json.dumps({"file": os.path.basename(args.input), "warnings": warnings,
                          "tokens": {("⟦%s⟧" % k): v for k, v in TOKEN_HELP.items()},
                          "paragraphs": paras}, ensure_ascii=False, indent=1)
    else:
        lines = ["# 문단 목록: %s" % os.path.basename(args.input),
                 "# 주소 = [part para]. 개체 표식 ⟦...⟧는 새 글에도 같은 순서로 그대로 둔다.",
                 "# " + ", ".join("⟦%s⟧ %s" % (k, v) for k, v in TOKEN_HELP.items())]
        lines += ["# 주의: " + x for x in warnings]
        for d in paras:
            tags = []
            if d["style"]:
                tags.append(d["style"])
            if d["list"]:
                tags.append("목록")
            if d["table_cell"]:
                tags.append("표 " + d["table_cell"])
            if d["existing_revisions"]:
                tags.append("기존 변경 있음")
            tag = " {%s}" % ", ".join(tags) if tags else ""
            lines.append("[%s %d]%s %s" % (d["part"], d["para"], tag, d["text"] or "(빈 문단)"))
        out = "\n".join(lines) + "\n"
    if args.output:
        if refuse_overwrite(args.output, args.input):
            return 2
        Path(args.output).write_text(out, encoding="utf-8")
        print("저장: %s (문단 %d개)" % (args.output, len(paras)))
    else:
        sys.stdout.write(out)
    return 0



def same_file(a, b):
    """같은 파일인지(맥의 대소문자 무시·바로가기 포함). 둘 다 있으면 파일 시스템에 묻는다."""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def refuse_overwrite(out, *inputs):
    """출력 경로가 입력 파일과 같으면 알리고 True(원고·추적 파일 보호)."""
    for x in inputs:
        if out and x and same_file(out, x):
            print("오류: 입력 파일(%s)을 덮어쓸 수 없습니다. 다른 이름을 지정하세요." % x, file=sys.stderr)
            return True
    return False

# ---------------------------------------------------------------- 명령: apply
def cmd_apply(args):
    if refuse_overwrite(args.output, args.input, getattr(args, "edits", None)):
        return 2
    doc = Docx(args.input)
    spec = load_edits(args.edits)
    if not getattr(args, "allow_missing_reasons", False):
        probs = reason_problems(spec["edits"], tier_of)
        if probs:
            print("오류: 수정 이유가 빠진 항목이 있어 적용하지 않았습니다(이유 없는 수정은 하지 않는다).", file=sys.stderr)
            for x in probs[:20]:
                print("  - " + x, file=sys.stderr)
            print("  옛 편집 목록이면 --allow-missing-reasons로 적용할 수 있다(권하지 않음).", file=sys.stderr)
            return 2
    author = args.author or spec.get("author") or DEFAULT_AUTHOR
    date = args.date or spec.get("date") or now_iso()
    granularity = args.granularity or spec.get("granularity") or "word"
    models = build_models(doc)
    plans, errors, warnings, summaries = resolve(doc, models, spec, granularity)
    gate_log = []
    if not errors and not getattr(args, "no_gate", False):
        plans, gate_log, gated = gate_plans(models, plans, spec)
        summaries = [x for x in summaries if x[0] not in gated]
        if gate_log:
            keep = sum(1 for x in gate_log if x["decision"] == "KEEP")
            cand = sum(1 for e in spec["edits"] if _op_of(e) != "comment")
            print("위험 수정 게이트: 위험 %d건 — 유지 %d, 되돌림 %d(되돌린 수정은 파일에 쓰지 않았다)" % (len(gate_log), keep, len(gate_log) - keep))
            print("편집 후보 %d / 게이트 되돌림 %d / 최종 적용 %d" % (cand, len(gated), cand - len(gated)))
            print(gate_table(gate_log))
    for x in warnings:
        print("주의: " + x)
    if errors:
        print("해석 실패 %d건:" % len(errors), file=sys.stderr)
        for x in errors:
            print("  - " + x, file=sys.stderr)
        if not args.skip_errors:
            print("파일을 쓰지 않았습니다. 편집 목록을 고친 뒤 다시 실행하세요.", file=sys.stderr)
            return 2
        bad = {int(re.match(r"편집 (\d+)", x).group(1)) for x in errors if re.match(r"편집 (\d+)", x)}
        spec = dict(spec, edits=[e if i not in bad else {"op": "noop"} for i, e in enumerate(spec["edits"])])
        spec["edits"] = [e for e in spec["edits"] if e.get("op") != "noop"]
        models = build_models(doc)
        plans, errors, warnings, summaries = resolve(doc, models, spec, granularity)
        if errors:
            print("남은 오류가 있어 중단합니다.", file=sys.stderr)
            return 2
    print("수정 묶음(hunk) 미리보기:")
    for ei, part, pi, old, new in summaries:
        print("  편집 %d [%s %d] %r -> %r" % (ei, part, pi, old, new))
    if args.dry_run:
        print("--dry-run: 파일을 쓰지 않았습니다.")
        return 0
    rev = Rev(max_existing_id(doc) + 1, author, date, args.initials)
    names = style_names(doc)
    ref_style = next((sid for sid, nm in names.items() if nm and nm.lower() == "annotation reference"), None)
    reasons = reason_comments(spec, "all" if args.comment_reasons else (args.reasons or spec.get("reasons") or "auto"))
    ctx = {"comments": [], "ref_style": ref_style, "comment_reasons": bool(reasons), "reasons": reasons}
    touched = set()
    for (part, pi), plan in plans.items():
        pm = models[part][pi]
        apply_plan(pm, plan, rev, ctx)
        touched.add(resolve_part(doc, part)[1])
    for name in touched:
        doc.mark(name)
    ensure_comments_part(doc, ctx["comments"], rev)
    flag = False if args.no_track_flag else ensure_track_flag(doc)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    doc.save(args.output)
    c = rev.counts
    print("완료: %s" % args.output)
    print("  삽입 w:ins %d / 삭제 w:del %d (문단 병합 %d, 문단 나누기 %d, 새 문단 %d) / 메모 %d / 작성자 '%s'"
          % (c["ins"], c["del"], c["merge"], c["split"], c["new_para"], c["comment"], author))
    print("  변경 내용 추적 켜기(w:trackRevisions): %s" % ("예" if flag else "아니오"))
    return 0


# ---------------------------------------------------------------- 명령: verify
def _views(doc, view):
    return {alias: part_view(doc.root(name), view) for alias, name in text_parts(doc)}


def cmd_verify(args):
    results = []

    def check(name, ok, detail=""):
        results.append((name, "PASS" if ok is True else ("INFO" if ok is None else "FAIL"), detail))

    path = args.tracked
    try:
        ok_zip = zipfile.is_zipfile(path)
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            names = z.namelist()
        dup = [n for n, k in Counter(names).items() if k > 1]
        required = {"[Content_Types].xml", "_rels/.rels"}
        check("ZIP 구조", ok_zip and bad is None and not dup and required <= set(names),
              "손상 항목: %s, 중복: %s" % (bad, dup) if (bad or dup) else "%d개 항목" % len(names))
    except zipfile.BadZipFile as exc:
        check("ZIP 구조", False, str(exc))
        return _report(results)
    doc = Docx(path)
    parse_errors = []
    for n in doc.blobs:
        if n.endswith(".xml") or n.endswith(".rels"):
            try:
                doc.root(n)
            except etree.XMLSyntaxError as exc:
                parse_errors.append("%s: %s" % (n, exc))
    check("XML 파싱", not parse_errors, "; ".join(parse_errors[:3]) or "모든 XML·rels 파싱 성공")
    if parse_errors:
        return _report(results)
    main = doc.root(doc.main)
    ns_ok = main.tag == w("document") and main.nsmap.get("w") == W_NS
    ign = main.get("{%s}Ignorable" % MC_NS, "")
    missing = [pfx for pfx in ign.split() if pfx not in main.nsmap]
    odd = sorted({etree.QName(el).namespace for el in main.iter() if isinstance(el.tag, str)
                  and el.prefix and re.fullmatch(r"ns\d+", el.prefix)})
    check("네임스페이스", ns_ok and not missing and not odd,
          "w 네임스페이스 정상, mc:Ignorable 접두사 선언 확인" if ns_ok and not missing and not odd
          else "w:%s 누락 접두사:%s 임의 접두사:%s" % (ns_ok, missing, odd))
    parts = text_parts(doc)
    cnt = Counter()
    ids = Counter()
    struct = []
    author = args.author or DEFAULT_AUTHOR
    for alias, name in parts:
        root = doc.root(name)
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            if el.tag in REVISION_TAGS:
                i = el.get(w("id"))
                ids[i] += 1
                if i is None or el.get(w("author")) is None:
                    struct.append("%s: %s에 w:id/w:author 없음" % (alias, etree.QName(el).localname))
                par = el.getparent()
                if el.tag in (w("ins"), w("del")):
                    in_mark = par is not None and par.tag == w("rPr")
                    if el.get(w("author")) == author:
                        cnt[("mark-" if in_mark else "") + etree.QName(el).localname] += 1
                    if in_mark and par.index(el) > 1:
                        struct.append("%s: 문단 표시 변경 요소 순서 오류" % alias)
            if el.tag == w("t") and any(a.tag in (w("del"), w("moveFrom")) for a in el.iterancestors()):
                struct.append("%s: 삭제 구간 안에 w:t (w:delText여야 함)" % alias)
            if el.tag == w("delText") and not any(a.tag in (w("del"), w("moveFrom")) for a in el.iterancestors()):
                struct.append("%s: 삭제 구간 밖의 w:delText" % alias)
            if el.tag in (w("ins"), w("del")) and el.getparent() is not None and el.getparent().tag != w("rPr"):
                if any(a.tag == el.tag for a in el.iterancestors()):
                    struct.append("%s: 같은 종류의 변경 요소가 중첩됨" % alias)
            if el.tag == w("pPr"):
                kids = [c.tag for c in el if isinstance(c.tag, str)]
                if w("rPr") in kids:
                    k = kids.index(w("rPr"))
                    if any(t not in PPR_TAIL for t in kids[k + 1:]):
                        struct.append("%s: w:pPr 안 w:rPr 위치 오류" % alias)
    run_level = cnt["ins"] + cnt["del"]
    if args.expect_clean:
        left = sum(ids.values())
        check("남은 변경 요소 없음(수락본)", left == 0, "변경 요소 %d개" % left)
    else:
        expect, all_gated = True, 0  # 편집 목록에 실제 수정이 있으면 변경 요소가 하나 이상 있어야 한다(삽입만·삭제만도 정상)
        if args.edits:
            try:
                sp = load_edits(args.edits)
                expect = any(_op_of(e) != "comment" for e in sp["edits"])
                if expect and args.original and not getattr(args, "no_gate", False):  # 게이트가 모두 되돌렸으면 변경 요소 0개가 정상
                    o_ = Docx(args.original)
                    m_ = build_models(o_)
                    pl_, er_, _w, _s = resolve(o_, m_, sp, sp.get("granularity") or "word")
                    if not er_:
                        pl_, _gl, gated_ = gate_plans(m_, pl_, sp)
                        if not any(p.hunks or p.splits or p.merge is not None or p.delete is not None or p.new_paras for p in pl_.values()):
                            expect, all_gated = False, len(gated_)
            except Exception:
                expect = True
        total = run_level + cnt["mark-ins"] + cnt["mark-del"]
        check("변경 요소 존재(w:ins·w:del·문단 표시)", total > 0 if (expect and not args.allow_missing) else True,
              "작성자 '%s': 삽입 %d, 삭제 %d, 문단 표시 삽입 %d, 문단 표시 삭제 %d%s"
              % (author, cnt["ins"], cnt["del"], cnt["mark-ins"], cnt["mark-del"],
                 "" if expect else (" (게이트에서 모든 수정 %d건이 되돌아가 변경 요소 없음: 정상)" % all_gated if all_gated else " (메모만 있는 편집 목록)")))
    dups = [i for i, k in ids.items() if k > 1]
    check("수정 ID 중복", not dups, "중복 ID: %s" % dups[:10] if dups else "변경 요소 %d개, ID 모두 고유" % sum(ids.values()))
    check("변경 요소 구조", not struct, "; ".join(struct[:5]) or "delText 위치·중첩·순서 정상")
    # 메모 무결성
    ctarget = doc.rel_target("comments")
    cref = set()
    for alias, name in parts:
        cref |= {el.get(w("id")) for el in doc.root(name).iter(w("commentReference"))}
    if ctarget and doc.exists(ctarget):
        cdef = {c.get(w("id")) for c in doc.root(ctarget).iter(w("comment"))}
        missing_c = cref - cdef
        check("메모 연결", not missing_c, "본문 메모 %d개, 정의 %d개" % (len(cref), len(cdef))
              if not missing_c else "정의 없는 메모 ID: %s" % sorted(missing_c))
    elif cref:
        check("메모 연결", False, "comments 파트 없이 메모 참조가 있습니다.")
    # 왕복 검사
    if args.original:
        orig = Docx(args.original)
        rv_t, rv_o = _views(doc, "reject"), _views(orig, "reject")
        diffs = _diff_views(rv_o, rv_t)
        check("모두 거부 = 원문", not diffs, diffs[0] if diffs else "모든 파트 문단 %d개 일치"
              % sum(len(v) for v in rv_o.values()))
        if args.edits:
            spec = load_edits(args.edits)
            models = build_models(orig)
            plans, errors, _w, _s = resolve(orig, models, spec, spec.get("granularity") or "word")
            if errors:
                check("모두 수락 = 기대 결과", False, "편집 목록 해석 오류: %s" % errors[0])
            else:
                if not getattr(args, "no_gate", False):
                    plans, _glog, _gated = gate_plans(models, plans, spec)  # 적용 때와 같은 게이트: 되돌린 수정은 기대 결과에서도 뺀다
                expected = simulate(models, plans)
                got = _views(doc, "accept")
                d2 = _diff_views(expected, got)
                check("모두 수락 = 기대 결과", not d2, d2[0] if d2 else "편집 목록대로 반영됨")
        else:
            check("모두 수락 = 기대 결과", None, "--edits를 주면 검사합니다.")
    settings = doc.rel_target("settings")
    track_on = bool(settings and doc.exists(settings) and doc.root(settings).find(w("trackRevisions")) is not None)
    check("변경 내용 추적 켜짐", True if track_on else None,
          "예" if track_on else "아니오: 검토자가 이어서 고치는 내용은 추적되지 않는다")
    if args.render:
        ok, detail = render_check(path, max_pages=getattr(args, "max_pages", 12))
        check("렌더링(열림·쪽 수·본문 글자 대조·쪽 이미지)", ok, detail)
    return _report(results)


def _diff_views(a, b):
    out = []
    for part in sorted(set(a) | set(b)):
        x, y = a.get(part, []), b.get(part, [])
        if x == y:
            continue
        if len(x) != len(y):
            out.append("%s: 문단 수 %d != %d" % (part, len(x), len(y)))
        for i, (p, q) in enumerate(zip(x, y)):
            if p != q:
                out.append("%s 문단 %d 불일치: 기대 %r / 실제 %r" % (part, i, to_display(p)[:80], to_display(q)[:80]))
                break
    return out


def _report(results):
    width = max(len(n) for n, _, _ in results) + 2
    fails = 0
    for name, status, detail in results:
        fails += status == "FAIL"
        print("[%s] %s %s" % (status, name.ljust(width), detail))
    info = [n for n, st, d in results if st == "INFO"]
    print("결과: %s%s" % ("통과" if not fails else "실패 %d건" % fails,
                         (" (확인하지 못했거나 알릴 항목: %s — 사용자에게 알린다)" % ", ".join(info)) if info else ""))
    return 0 if not fails else 1


def pick_pages(page_texts, paras, max_pages=12, fallback=3):
    """고친 문단(표시본 글, 공백 없음)을 PDF 쪽 글에서 찾아 볼 쪽을 고른다.
    지운·넣은 조각만으로 찾으면 흔한 조각이 모든 쪽에 걸리므로, 문단 전체(길면 머리·꼬리 30자)로 찾는다.
    고친 쪽을 앞뒤 쪽보다 먼저 담는다. 반환: (볼 쪽, 고친 쪽, 쪽을 못 찾은 문단 수, 제한 때문에 못 담은 고친 쪽)"""
    changed, missing, n = set(), 0, len(page_texts)
    for t in paras:
        hit = set()
        for keys in ([t] if len(t) <= 60 else [t[:30], t[-30:]], [t[:15]] if len(t) >= 15 else []):
            hit = {k + 1 for k, pt in enumerate(page_texts) if keys and any(key in pt for key in keys)}
            if hit:
                break
        if hit:
            changed |= hit
        else:
            missing += 1
    changed = sorted(changed)
    if not changed:
        return list(range(1, min(n, fallback) + 1)), [], missing, []
    want = changed[:max_pages]
    for c in changed:
        for pg in (c - 1, c + 1):
            if 1 <= pg <= n and pg not in want and len(want) < max_pages:
                want.append(pg)
    return sorted(want), changed, missing, changed[max_pages:]


def render_check(path, pages=3, max_pages=12):
    """LibreOffice로 PDF를 만들고 (1) 쪽 수, (2) 표시본 어절이 PDF에 빠짐없이 있는지, (3) 고친 쪽과 앞뒤 쪽 이미지를 남긴다.
    레이아웃 판정은 하지 않는다. 결과는 세 상태다: 확인함(True), 확인했는데 문제(False), 도구가 없어 확인 못 함(None, '건너뜀')."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None, "LibreOffice가 없어 렌더링 검사 건너뜀"
    env = dict(os.environ, SAL_USE_VCLPLUGIN="svp")
    helper = Path("/mnt/skills/public/docx/scripts/office")
    if (helper / "soffice.py").exists():
        sys.path.insert(0, str(helper.parent))
        try:
            from office.soffice import get_soffice_env  # type: ignore
            env = get_soffice_env()
        except Exception:
            pass
    with tempfile.TemporaryDirectory() as td:
        profile = Path(td, "profile").as_uri()
        cmd = [soffice, "-env:UserInstallation=" + profile, "--headless", "--norestore",
               "--convert-to", "pdf", "--outdir", td, str(path)]
        try:
            subprocess.run(cmd, env=env, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            return False, "시간 초과"
        pdf = Path(td, Path(path).stem + ".pdf")
        if not (pdf.exists() and pdf.stat().st_size > 0):
            return False, "PDF 변환 실패"
        notes, ok, npages, done_text, done_img = [], True, 0, False, False
        if shutil.which("pdfinfo"):
            m = re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout)
            npages = int(m.group(1)) if m else 0
            if npages:
                notes.append("PDF %d쪽" % npages)
        doc = Docx(path)
        name = next((n for a, n in text_parts(doc) if a == "document"), None)
        root = doc.root(name) if name else None
        pars = [par for par in (root.iter() if root is not None else []) if etree.QName(par).localname == "p"]

        def markup(par):  # PDF에는 변경 내용이 표시된 채로 찍힌다: 지운 글자와 넣은 글자를 문서 순서대로 이은 글
            return "".join(el.text or "" for el in par.iter() if etree.QName(el).localname in ("t", "delText"))
        page_texts = []
        if shutil.which("pdftotext"):
            raw = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True).stdout
            page_texts = [re.sub(r"\s", "", t) for t in raw.split("\f")]
            if page_texts and not page_texts[-1]:
                page_texts = page_texts[:-1]
            if not npages and page_texts:  # pdfinfo가 없으면 쪽 구분 문자로 센다
                npages = len(page_texts)
                notes.insert(0, "PDF %d쪽(pdftotext로 셈)" % npages)
            got = "".join(page_texts)
            words = {w for par in pars for w in markup(par).split() if len(w) >= 2}
            missing = [w for w in words if w not in got]
            cover = 1 - len(missing) / max(1, len(words))
            ok, done_text = cover >= 0.98, True
            notes.append("표시본 어절 %.1f%%가 PDF에 있음%s" % (100 * cover, (" (없는 예: %s)" % ", ".join(sorted(missing)[:4])) if missing else ""))
        else:
            notes.append("글자 대조 건너뜀(pdftotext 없음)")
        if shutil.which("pdftoppm") and page_texts:
            changed_paras = [re.sub(r"\s", "", markup(par)) for par in pars
                             if any(etree.QName(x).localname in ("ins", "del") for x in par.iter())]
            changed_paras = [t for t in changed_paras if t]
            want, changed, lost, omitted = pick_pages(page_texts, changed_paras, max_pages, pages)
            outdir = Path(path).with_name(Path(path).stem + "_render")
            if not os.access(str(outdir.parent), os.W_OK):  # 업로드 폴더처럼 쓸 수 없는 곳이면 현재 폴더에
                outdir = Path.cwd() / (Path(path).stem + "_render")
            outdir.mkdir(exist_ok=True)
            for pg in want:
                subprocess.run(["pdftoppm", "-r", "50", "-png", "-f", str(pg), "-l", str(pg), str(pdf), str(outdir / "page")],
                               capture_output=True)
            done_img = not omitted and not lost  # 고친 쪽을 하나라도 못 봤으면 쪽 모양 확인은 끝나지 않은 것이다
            label = ("고친 쪽 %s과 앞뒤" % ",".join(map(str, changed))) if changed else "앞쪽"
            notes.append("%s 이미지 %d장: %s — 열어서 겹침·잘림을 눈으로 확인" % (label, len(want), outdir))
            if omitted:
                notes.append("고친 쪽 중 이미지를 만들지 못해 확인하지 못한 쪽: %s(--max-pages를 늘려 다시 검증)" % ",".join(map(str, omitted)))
            if lost:
                notes.append("쪽을 찾지 못해 확인하지 못한 고친 문단 %d개" % lost)
        else:
            notes.append("쪽 이미지 건너뜀(%s)" % ("pdftoppm 없음" if not shutil.which("pdftoppm") else "쪽 글이 없어 고친 쪽을 찾을 수 없음"))
        if not npages:
            notes.append("쪽 수 확인 건너뜀(pdfinfo·pdftotext 없음)")
        if not ok:
            return False, ", ".join(notes)
        return (True if (npages and done_text and done_img) else None), ", ".join(notes)


# ---------------------------------------------------------------- 명령: accept
def accept_all(root, our_author, drop_comment_ids):
    # 뒤에서부터 합쳐야 연쇄 병합(6→7→8→9)이 끊기지 않는다.
    for p in reversed(list(root.iter(w("p")))):
        st = mark_state(p)
        if "del" in st or "moveFrom" in st:
            nxt = next_para_sibling(p)
            rpr = para_mark_rpr(p)
            for c in list(rpr):
                if c.tag in MARK_TAGS:
                    rpr.remove(c)
            if nxt is not None:
                nppr = nxt.find(w("pPr"))
                sect = nppr.find(w("sectPr")) if nppr is not None else None
                for child in list(nxt):
                    if child.tag != w("pPr"):
                        p.append(child)
                if sect is not None:
                    ensure_ppr(p).append(sect)
                nxt.getparent().remove(nxt)
    for el in list(root.iter(w("del"), w("moveFrom"), w("moveFromRangeStart"), w("moveFromRangeEnd"),
                             w("moveToRangeStart"), w("moveToRangeEnd"))):
        par = el.getparent()
        if par is not None:
            par.remove(el)
    for el in list(root.iter(w("ins"), w("moveTo"))):
        par = el.getparent()
        if par is None:
            continue
        if par.tag == w("rPr"):
            par.remove(el)
            continue
        idx = par.index(el)
        for k, child in enumerate(list(el)):
            par.insert(idx + k, child)
        par.remove(el)
    for tag in ("rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "tblGridChange", "trPrChange",
                "tcPrChange", "tblPrExChange", "numberingChange", "cellIns"):
        for el in list(root.iter(w(tag))):
            el.getparent().remove(el)
    if drop_comment_ids:
        for el in list(root.iter(w("commentRangeStart"), w("commentRangeEnd"))):
            if el.get(w("id")) in drop_comment_ids:
                el.getparent().remove(el)
        for ref in list(root.iter(w("commentReference"))):
            if ref.get(w("id")) in drop_comment_ids:
                run = ref.getparent()
                run.getparent().remove(run)


def cmd_accept(args):
    if refuse_overwrite(args.output, args.tracked):
        return 2
    doc = Docx(args.tracked)
    drop = set()
    ctarget = doc.rel_target("comments")
    if ctarget and doc.exists(ctarget) and not args.keep_comments:
        croot = doc.root(ctarget)
        for c in list(croot.iter(w("comment"))):
            if c.get(w("author")) == (args.author or DEFAULT_AUTHOR):
                drop.add(c.get(w("id")))
                croot.remove(c)
        if drop:
            doc.mark(ctarget)
    for alias, name in text_parts(doc):
        accept_all(doc.root(name), args.author or DEFAULT_AUTHOR, drop)
        doc.mark(name)
    settings = doc.rel_target("settings")
    if settings and doc.exists(settings):
        s = doc.root(settings)
        for el in list(s.iter(w("trackRevisions"))):
            s.remove(el)
        doc.mark(settings)
    doc.save(args.output)
    left = sum(1 for _a, name in text_parts(doc) for el in doc.root(name).iter()
               if isinstance(el.tag, str) and el.tag in REVISION_TAGS)
    print("완료: %s (남은 변경 요소 %d, 지운 편집자 메모 %d)" % (args.output, left, len(drop)))
    return 0 if left == 0 else 1


# ---------------------------------------------------------------- 명령: text
def cmd_text(args):
    doc = Docx(args.docx)
    wanted = [x.strip() for x in args.parts.split(",")] if args.parts else None
    chunks = []
    for alias, name in text_parts(doc):
        if wanted and alias not in wanted:
            continue
        for t in part_view(doc.root(name), args.view):
            t = to_display(t)
            if args.plain:
                t = t.replace("⟦TAB⟧", "\t").replace("⟦BR⟧", " ")
                t = DISPLAY_RE.sub("", t)
            if t.strip():
                chunks.append(t)
    out = "\n\n".join(chunks) + "\n"
    if args.output:
        if refuse_overwrite(args.output, args.docx):
            return 2
        Path(args.output).write_text(out, encoding="utf-8")
    else:
        sys.stdout.write(out)
    return 0



# ---------------------------------------------------------------- 명령: report
def cmd_report(args):
    """편집 목록·추적 파일·검증 결과로 편집 보고서 초안을 만든다. 빈칸은 편집자가 채운다."""
    author = args.author or DEFAULT_AUTHOR
    orig, tracked = Docx(args.original), Docx(args.tracked)
    spec = load_edits(args.edits)
    models = build_models(orig)
    _plans, errors, _warn, summaries = resolve(orig, models, spec, spec.get("granularity") or "word")
    gate_log_ = []
    if not errors and not getattr(args, "no_gate", False):
        _plans, gate_log_, _gated = gate_plans(models, _plans, spec)
        summaries = [x for x in summaries if x[0] not in _gated]
    if errors:
        print("오류: 편집 목록 해석 실패 - " + errors[0], file=sys.stderr)
        return 2
    cnt = Counter()
    for _alias, name in text_parts(tracked):
        for el in tracked.root(name).iter(w("ins"), w("del")):
            if el.get(w("author")) == author:
                cnt[("mark-" if el.getparent().tag == w("rPr") else "") + etree.QName(el).localname] += 1
    notes = []
    ctarget = tracked.rel_target("comments")
    if ctarget and tracked.exists(ctarget):
        for c in tracked.root(ctarget).iter(w("comment")):
            if c.get(w("author")) == author:
                notes.append("".join(t.text or "" for t in c.iter(w("t"))))
    buf = io.StringIO()
    with redirect_stdout(buf):
        vcode = cmd_verify(argparse.Namespace(tracked=args.tracked, original=args.original, edits=args.edits, author=author,
                                              allow_missing=False, expect_clean=False, render=args.render,
                                              no_gate=getattr(args, "no_gate", False)))
    vlines = buf.getvalue().splitlines()
    passed = sum(1 for ln in vlines if ln.startswith("[PASS]"))
    failed = [ln for ln in vlines if ln.startswith("[FAIL]")]
    ops = Counter(_op_of(e) if e.get("op") != "noop" else "noop" for e in spec["edits"])
    groups = {}
    for ei, e in enumerate(spec["edits"]):
        op = _op_of(e)
        if op == "comment":
            continue
        part = resolve_part(orig, e.get("part"))[0]
        pi = e.get("para")
        if pi is None:
            found = [x for x in summaries if x[0] == ei]
            pi = found[0][2] if found else -1
        pi = int(pi)
        T = to_display(models[part][pi].T) if 0 <= pi < len(models[part]) else ""
        old, new = {"rewrite": (T, e.get("new_text", "")), "replace": (e.get("old", ""), e.get("new", "")),
                    "delete": (e.get("old", ""), ""),
                    "insert": (e.get("after") or e.get("before") or "", "+" + e.get("text", "")),
                    "merge_next": ("¶", "다음 문단과 합침"), "split": ("¶", "'%s' 앞에서 나눔" % e.get("before", "")),
                    "delete_paragraph": (T, "(문단 삭제)"),
                    "insert_paragraph": ("¶", "+(새 문단) " + e.get("text", ""))}.get(op, ("", ""))
        key = "%s (%s)" % (e.get("rule") or e.get("reason") or "종류 미기재", tier_of(e))
        groups.setdefault(key, []).append((part, pi, old, new))
    name = os.path.basename(args.original)
    L = ["# 편집 보고서: %s" % name, "",
         "- 장르: %s / 작성자 표기: %s" % (spec.get("genre") or "(기재)", author),
         "- 결과: 편집 %d건 → 삽입 %d, 삭제 %d(문단 표시 삭제 %d 포함), 문단 병합 %d, 문단 나누기 %d, 문단 삭제 %d, 메모 %d"
         % (len(spec["edits"]), cnt["ins"], cnt["del"] + cnt["mark-del"], cnt["mark-del"], ops["merge_next"], ops["split"],
            ops["delete_paragraph"], len(notes)),
         "- 검증: verify %d개 항목 통과%s" % (passed, (", 실패: " + "; ".join(failed)) if failed else
                                         " (모두 거부 = 원문, 모두 수락 = 편집 목록의 기대 결과)"),
         "", "## 주요 수정 (종류별)", "", "| 종류 | 곳 | 예 |", "|---|---|---|"]
    for key, items in groups.items():
        where = sorted({"%s %d" % (pt, pi) if pt != "document" else "%d문단" % pi for pt, pi, _o, _n in items})
        _pt, _pi, old, new = items[0]
        ex = "%s → %s" % (old[:45] + ("…" if len(old) > 45 else "") or "∅", new[:45] + ("…" if len(new) > 45 else "") or "∅")
        L.append("| %s | %s | %s |" % (key.replace("|", "/"), ", ".join(where[:6]) + (" 외" if len(where) > 6 else ""),
                                      ex.replace("|", "/")))
    why = [t for t in notes if re.match(r"^\[(?:확실|권장|제안)\] ", t)]
    ask = [t for t in notes if t not in why]
    decor = [e for e in spec["edits"] if is_decor(e)]
    if decor:
        L += ["", "## 지운 저정보량 장식 표현 (작가가 되살릴 수 있게)", ""]
        def _decor_pair(e):
            if _op_of(e) not in ("rewrite", "delete_paragraph"):
                return e.get("old") or "", e.get("new", "")
            pi_ = e.get("para")
            part_ = resolve_part(orig, e.get("part"))[0]
            before_ = to_display(models[part_][int(pi_)].T) if pi_ is not None and 0 <= int(pi_) < len(models[part_]) else "(문단)"
            cut = lambda t: t[:80] + ("…" if len(t) > 80 else "")
            return cut(before_), (cut(e.get("new_text", "")) if _op_of(e) == "rewrite" else "(문단 삭제)")
        L += ["- %s `%s` → `%s` — %s%s" % ((("%s문단" % e["para"]) if "para" in e else "문단(find)",) + _decor_pair(e) +
                                          (" · ".join(v for v in (e.get("rule", ""), e.get("reason", "")) if v),
                                           " (자동 추정: 장식이 아니면 편집 항목에 \"decor\": false)" if decor_state(e) == "추정" else "")) for e in decor]
    L += ["", "## 확인이 필요한 곳 (메모로 남김)", ""]
    L += ["%d. %s" % (i + 1, t) for i, t in enumerate(ask)] or ["- 없음"]
    if gate_log_:
        keep_ = sum(1 for x in gate_log_ if x["decision"] == "KEEP")
        cand_ = sum(1 for e in spec["edits"] if _op_of(e) != "comment")
        L += ["", "## 위험 수정 판정 (게이트: 유지 %d, 되돌림 %d — 되돌린 수정은 파일에 없다)" % (keep_, len(gate_log_) - keep_), "",
              "편집 후보 %d / 게이트 되돌림 %d / 최종 적용 %d" % (cand_, len(_gated), cand_ - len(_gated)), "", gate_table(gate_log_)]
    if why:
        L += ["", "수정 이유 메모 %d개는 Word 메모에서 확인할 수 있다('확실'이 아닌 수정에 자동으로 붙음)." % len(why)]
    L += ["", "## 고치지 않은 것", "", "- (편집자가 작성: 일부러 둔 정상 문장·인용·대사·용어와 그 이유)",
          "", "## 스타일 시트 요약", "", "- (긴 문서면 작성: 문체, 숫자·용어 표기, 결정한 원칙)", ""]
    out = "\n".join(L)
    if args.output:
        if refuse_overwrite(args.output, args.tracked, args.original, args.edits):
            return 2
        Path(args.output).write_text(out, encoding="utf-8")
        print("저장: %s (검증 %s)" % (args.output, "통과" if vcode == 0 else "실패"))
    else:
        sys.stdout.write(out)
    return 0 if vcode == 0 else 1

# ---------------------------------------------------------------- CLI

# ---------------------------------------------------------------- 자체 시험(스킬만 받아도, LibreOffice 없이)
def _st_docx(path, paras):
    """시험용 최소 DOCX."""
    W_ = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    R_ = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="%s"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>' % R_)
    drels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="%s"><Relationship Id="rId1" '
             'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/></Relationships>' % R_)
    body = "".join('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % t for t in paras)
    docx = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="%s"><w:body>%s<w:sectPr/></w:body></w:document>' % (W_, body)
    sett = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings xmlns:w="%s"/>' % W_
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in (("[Content_Types].xml", ct), ("_rels/.rels", rels), ("word/_rels/document.xml.rels", drels),
                     ("word/document.xml", docx), ("word/settings.xml", sett)):
            z.writestr(n, c)


def cmd_selftest(args):
    """엔진 회귀 시험: 고친 쪽 고르기, 삽입만·삭제만·메모만 편집의 검증, 원본 덮어쓰기 방지."""
    import contextlib
    import io
    res = []
    pages = ["페이지%d공통문구테스트문장%d" % (i, i) for i in range(1, 21)]
    pages[19] = "페이지20공통문구테스트시험문장20"
    want, changed, _lost, _om = pick_pages(pages, [pages[19]])
    res.append(("쪽 고르기: 흔한 조각이 모든 쪽에 있어도 고친 쪽(20)을 찾음", changed == [20] and 20 in want))
    many = ["쪽%02d고유한문단글자" % i for i in range(1, 31)]
    want, changed, _lost, om = pick_pages(many, [many[i] for i in range(0, 30, 2)], max_pages=12)
    res.append(("쪽 고르기: 고친 쪽이 제한보다 많으면 고친 쪽부터 담고 못 담은 쪽을 알림", len(want) == 12 and set(want) <= set(changed) and len(om) == 3))

    def run(argv, strict=False):  # 엔진 동작 시험은 이유 검사를 끄고, 이유 강제는 strict=True로 따로 시험한다
        if argv and argv[0] == "apply" and not strict:
            argv = argv[:1] + ["--allow-missing-reasons"] + argv[1:]
        if argv and argv[0] in ("apply", "verify", "report") and not strict:
            argv = argv[:1] + ["--no-gate"] + argv[1:]  # 엔진 동작 시험: 게이트는 따로 시험한다
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(argv)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.docx")
        _st_docx(src, ["첫 문단은 그대로 둔다.", "지난 6개월간 도구를 운영했다.", "표 뒤 문단이다."])
        ep = os.path.join(td, "e.json")
        for label, edits in (("삭제만", [{"para": 1, "old": "지난 6개월간 ", "new": ""}]),
                             ("삽입만", [{"para": 2, "old": "표 뒤", "new": "표 바로 뒤"}]),
                             ("메모만", [{"op": "comment", "para": 0, "text": "확인"}])):
            open(ep, "w", encoding="utf-8").write(json.dumps({"edits": edits}, ensure_ascii=False))
            out = os.path.join(td, "o.docx")
            a = run(["apply", src, ep, "-o", out])
            v = run(["verify", out, "--original", src, "--edits", ep]) if a == 0 else 1
            res.append(("적용·검증: %s 편집" % label, a == 0 and v == 0))
        for label, edits in (("새 문단 뒤에", [{"op": "insert_paragraph", "para": 1, "text": "새로 넣은 문단이다."}]),
                             ("새 문단 앞에", [{"op": "insert_paragraph", "para": 0, "position": "before", "text": "맨 앞 문단이다."}]),
                             ("마지막 문단 뒤에 두 문단", [{"op": "insert_paragraph", "para": 2, "text": "끝 하나.\n끝 둘."}]),
                             ("새 문단과 같은 문단 수정", [{"op": "insert_paragraph", "para": 1, "text": "사이 문단."},
                                                        {"para": 1, "old": "지난 6개월간 ", "new": ""},
                                                        {"op": "split", "para": 1, "before": "운영했다"}])):
            open(ep, "w", encoding="utf-8").write(json.dumps({"edits": edits}, ensure_ascii=False))
            out = os.path.join(td, "np.docx")
            a = run(["apply", src, ep, "-o", out])
            v = run(["verify", out, "--original", src, "--edits", ep]) if a == 0 else 1
            res.append(("적용·검증: %s" % label, a == 0 and v == 0))
        open(ep, "w", encoding="utf-8").write(json.dumps({"edits": [{"para": 1, "old": "지난 6개월간 ", "new": ""}]}, ensure_ascii=False))
        out = os.path.join(td, "ok.docx")
        run(["apply", src, ep, "-o", out])
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            main(["verify", "--no-gate", out, "--original", src, "--edits", ep])  # 적용과 같은 게이트 설정
        res.append(("정상 파일 검증에 INFO가 없음(추적 켜짐은 PASS)", "[INFO]" not in buf.getvalue() and "[FAIL]" not in buf.getvalue()))
        open(ep, "w", encoding="utf-8").write(json.dumps({"mode": "강한 편집", "edits": [
            {"para": 1, "old": "지난 6개월간 도구를", "new": "도구를", "rule": "L2 홍보 수식어", "reason": "사실 문장에 붙은 장식"}]}, ensure_ascii=False))
        out = os.path.join(td, "decor.docx")
        a = run(["apply", src, ep, "-o", out])
        rp = os.path.join(td, "r.md")
        r_ = run(["report", out, "--original", src, "--edits", ep, "-o", rp]) if a == 0 else 1
        body = open(rp, encoding="utf-8").read() if r_ == 0 else ""
        res.append(("옛 편집 목록의 mode 메타데이터는 동작을 바꾸지 않고, 장식 삭제 보고는 그대로 작동", a == 0 and "## 지운 저정보량 장식 표현" in body
                    and "지난 6개월간" in body and "자동 추정" in body))
        res.append(("decor: false는 자동 추정보다 우선(압축은 장식 삭제가 아님)",
                    not is_decor({"rule": "L1 거창한 의미 부여", "old": "매우 중요한 의미를 가진다", "new": "매우 중요하다", "decor": False})
                    and decor_state({"rule": "L2", "old": "혁신적인 기능", "new": "기능", "decor": True}) == "명시"))
        open(ep, "w", encoding="utf-8").write(json.dumps({"edits": [{"para": 1, "old": "지난 ", "new": ""}]}, ensure_ascii=False))
        no_reason = run(["apply", src, ep, "-o", os.path.join(td, "nr.docx")], strict=True)
        open(ep, "w", encoding="utf-8").write(json.dumps({"edits": [{"para": 1, "old": "지난 ", "new": "", "rule": "D2 반복·중복", "reason": "앞 문단과 같은 기간 표현"}]}, ensure_ascii=False))
        with_reason = run(["apply", src, ep, "-o", os.path.join(td, "wr.docx")], strict=True)
        res.append(("이유 강제: rule·reason 없는 권장 수정은 거부, 있으면 적용", no_reason == 2 and with_reason == 0))
        g5 = os.path.join(td, "g5.docx")
        _st_docx(g5, ["마당에는 낙엽이 쌓여 있었다.", "창고 앞에서는 준호가 버린 나뭇가지 세 묶음을 벽 옆에 모아 두고 있었다.", "바람이 불었다."])
        def _g5(edit, dec, one=False):
            es = [dict(dict(edit, rule="W2", reason="x", tier="권장"), **dec)]
            if not one:
                es.append({"para": 2, "old": "바람이 불었다", "new": "바람이 세게 불었다", "rule": "S", "reason": "리듬", "tier": "권장"})
            open(ep, "w", encoding="utf-8").write(json.dumps({"genre": "웹소설", "edits": es}, ensure_ascii=False))
            a_ = run(["apply", g5, ep, "-o", os.path.join(td, "g5_t.docx")], strict=True)
            with contextlib.redirect_stdout(io.StringIO()):
                v_ = main(["verify", os.path.join(td, "g5_t.docx"), "--original", g5, "--edits", ep])
            run(["accept", os.path.join(td, "g5_t.docx"), "-o", os.path.join(td, "g5_a.docx")])
            with contextlib.redirect_stdout(io.StringIO()) as b_:
                main(["text", os.path.join(td, "g5_a.docx"), "--plain"])
            return a_, v_, b_.getvalue()
        num = {"para": 1, "old": "세 묶음", "new": "다섯 묶음"}
        act = {"para": 1, "old": "버린", "new": "버려진"}
        k1 = _g5(num, {"decision": "KEEP", "decision_reason": "앞 문장과 같은 뜻이다"})
        k2 = _g5(num, {"decision": "KEEP", "override": "user", "override_evidence": "작가가 '다섯 묶음으로 고쳐 달라'고 했다"}, one=True)
        k3 = _g5(act, {}, one=True)
        k4 = _g5(act, {"decision": "REVERT", "decision_reason": "버려진 것은 준호의 행동이 아니게 된다"}, one=True)
        res.append(("위험 수정 게이트: 사실 변경은 사용자·출처 근거 없이는 되돌리고, 확인 항목은 기본 적용하되 REVERT 판정이면 되돌림",
                    k1[0] == 0 and "세 묶음" in k1[2] and "세게" in k1[2] and k2[0] == 0 and "다섯 묶음" in k2[2]
                    and k3[0] == 0 and "버려진" in k3[2] and k4[0] == 0 and k4[1] == 0 and "버린 나뭇가지" in k4[2]))
        legacy = os.path.join(td, "legacy.docx")
        _st_docx(legacy, ["첫 문장은 길다.", "둘째 문장도 길다."])
        open(ep, "w", encoding="utf-8").write(json.dumps({"mode": "보수적", "edits": [
            {"para": 0, "old": "길다", "new": "짧다", "rule": "어색한 말", "tier": "권장", "reason": "표현 정리",
             "decision": "KEEP", "decision_reason": "사용자 요청 범위의 일반 윤문"},
            {"para": 1, "old": "길다", "new": "간결하다", "rule": "어색한 말", "tier": "권장", "reason": "표현 정리",
             "decision": "KEEP", "decision_reason": "사용자 요청 범위의 일반 윤문"}]}, ensure_ascii=False))
        c_legacy = run(["apply", legacy, ep, "-o", os.path.join(td, "legacy_t.docx")], strict=True)
        a_legacy = run(["accept", os.path.join(td, "legacy_t.docx"), "-o", os.path.join(td, "legacy_a.docx")]) if c_legacy == 0 else 1
        txt_legacy = ""
        if a_legacy == 0:
            with contextlib.redirect_stdout(io.StringIO()) as buf_:
                main(["text", os.path.join(td, "legacy_a.docx"), "--plain"])
            txt_legacy = buf_.getvalue()
        res.append(("옛 mode=보수적 메타데이터가 권장 수정을 막지 않음",
                    c_legacy == 0 and "첫 문장은 짧다" in txt_legacy and "둘째 문장도 간결하다" in txt_legacy))
        protected_doc = os.path.join(td, "protected.docx")
        _st_docx(protected_doc, ["세린은 창가에 섰다.", "승인 문구입니다."])
        open(ep, "w", encoding="utf-8").write(json.dumps({
            "genre": "소설", "terms": ["세린"], "protected_text": ["승인 문구입니다."], "edits": [
                {"para": 0, "old": "세린", "new": "세릭", "rule": "어색한 말", "tier": "권장", "reason": "표현 통일"},
                {"para": 1, "old": "문구", "new": "문장", "rule": "어색한 말", "tier": "권장", "reason": "표현 통일"}
            ]}, ensure_ascii=False))
        protected_tr = os.path.join(td, "protected_t.docx")
        cp_ = run(["apply", protected_doc, ep, "-o", protected_tr], strict=True)
        protected_ac = os.path.join(td, "protected_a.docx")
        ca_ = run(["accept", protected_tr, "-o", protected_ac]) if cp_ == 0 else 1
        protected_txt = ""
        if ca_ == 0:
            with contextlib.redirect_stdout(io.StringIO()) as pb_:
                main(["text", protected_ac, "--plain"])
            protected_txt = pb_.getvalue()
        res.append(("보호 목록: terms·protected_text를 파일 게이트가 받아 확정 이름·보호 문구 수정을 되돌림",
                    cp_ == 0 and "세린은" in protected_txt and "세릭은" not in protected_txt
                    and "승인 문구입니다" in protected_txt and "승인 문장입니다" not in protected_txt))
        before = open(src, "rb").read()
        res.append(("원본 덮어쓰기 방지(apply·accept·text의 -o가 입력이면 거부)",
                    run(["apply", src, ep, "-o", src]) == 2 and run(["accept", src, "-o", src]) == 2
                    and run(["text", src, "-o", src]) == 2 and open(src, "rb").read() == before))
    for name, ok in res:
        print("%s  %s" % ("PASS" if ok else "FAIL", name))
    fails = sum(not ok for _n, ok in res)
    print("변경 추적 엔진 시험 %d건: 통과 %d, 실패 %d" % (len(res), len(res) - fails, fails))
    return 1 if fails else 0

def main(argv=None):
    ap = argparse.ArgumentParser(description="TYPODER Korean Editor - Word 변경 내용 추적 엔진")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("extract", help="편집 대상 문단 목록")
    a.add_argument("input")
    a.add_argument("--json", action="store_true")
    a.add_argument("-o", "--output")
    a.set_defaults(func=cmd_extract)

    a = sub.add_parser("apply", help="편집 목록을 변경 내용 추적으로 적용")
    a.add_argument("--no-gate", action="store_true", help="위험 수정 게이트를 끈다(엔진 동작 시험용, 권하지 않음)")
    a.add_argument("--allow-missing-reasons", action="store_true", help="rule·reason이 빠진 항목도 적용(옛 편집 목록용, 권하지 않음)")
    a.add_argument("input")
    a.add_argument("edits")
    a.add_argument("-o", "--output", required=True)
    a.add_argument("--author")
    a.add_argument("--initials", default=DEFAULT_INITIALS)
    a.add_argument("--date", help="ISO 8601 UTC (기본: 현재 시각)")
    a.add_argument("--granularity", choices=["word", "edit"])
    a.add_argument("--no-track-flag", action="store_true", help="settings.xml에 변경 내용 추적 켜기를 넣지 않음")
    a.add_argument("--comment-reasons", action="store_true", help="모든 수정에 이유 메모(= --reasons all)")
    a.add_argument("--reasons", choices=["auto", "all", "none"], help="수정 이유 메모: auto(기본, '확실'이 아닌 수정만)·all·none")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--skip-errors", action="store_true")
    a.set_defaults(func=cmd_apply)

    a = sub.add_parser("selftest", help="엔진 회귀 시험(LibreOffice 없이)")
    a.set_defaults(func=cmd_selftest)
    a = sub.add_parser("verify", help="변경 내용 추적 파일 검증")
    a.add_argument("--no-gate", action="store_true", help="적용 때 게이트를 껐으면 검증도 끈다")
    a.add_argument("tracked")
    a.add_argument("--original")
    a.add_argument("--edits")
    a.add_argument("--author")
    a.add_argument("--allow-no-revisions", "--allow-missing", dest="allow_missing", action="store_true",
                   help="변경 요소가 하나도 없어도 존재 검사를 통과시킨다(삽입만·삭제만·메모만 있는 편집은 이 옵션 없이도 통과)")
    a.add_argument("--expect-clean", action="store_true", help="수락본 검사: 변경 요소가 하나도 없어야 통과")
    a.add_argument("--render", action="store_true", help="LibreOffice·Poppler로 PDF를 만들어 글자 대조와 고친 쪽 이미지 확인")
    a.add_argument("--max-pages", type=int, default=12, help="--render가 남길 쪽 이미지 수의 상한(고친 쪽을 먼저 담는다)")
    a.set_defaults(func=cmd_verify)

    a = sub.add_parser("accept", help="모든 변경 수락본(읽기용) 생성")
    a.add_argument("tracked")
    a.add_argument("-o", "--output", required=True)
    a.add_argument("--author")
    a.add_argument("--keep-comments", action="store_true")
    a.set_defaults(func=cmd_accept)

    a = sub.add_parser("text", help="대조용 텍스트")
    a.add_argument("docx")
    a.add_argument("--view", choices=["accept", "reject"], default="accept")
    a.add_argument("--parts")
    a.add_argument("--plain", action="store_true", help="개체 표식을 지우고 탭만 남김")
    a.add_argument("-o", "--output")
    a.set_defaults(func=cmd_text)

    a = sub.add_parser("report", help="편집 보고서 초안(editor_report.md)")
    a.add_argument("--no-gate", action="store_true", help="적용 때 게이트를 껐으면 보고서도 끈다")
    a.add_argument("tracked")
    a.add_argument("--original", required=True)
    a.add_argument("--edits", required=True)
    a.add_argument("--author")
    a.add_argument("--render", action="store_true")
    a.add_argument("-o", "--output")
    a.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (DocxError, EditError) as exc:
        print("오류: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
