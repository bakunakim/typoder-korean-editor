# Word 변경 내용 추적(Track Changes) 작업 설명서

.docx 원고를 편집할 때 읽는다. 결과는 Word의 [검토] → [변경 내용 추적]에서 하나씩 수락·거부할 수 있는 실제 수정 기록이어야 한다. 색·밑줄·취소선으로 흉내 내거나, 텍스트로 뽑아 새 문서를 만들지 않는다.

## 목차

1. 산출물과 원칙
2. 작업 순서
3. 편집 목록(EDITS.json) 형식
4. 편집 단위를 정하는 법
5. 개체 표식
6. 오류 메시지와 대처
7. 보존되는 것과 한계
8. 편집 보고서(editor_report.md)

## 1. 산출물과 원칙

- 원본은 읽기만 한다. 엔진은 원본 경로로 저장하지 않는다.
- `[원본 이름]_tracked.docx`: 변경 내용 추적 파일(필수). 작성자 `TYPODER Korean Editor`, 머리글자 `TKE`. 이름을 바꾸기 전(스킬 이름이 TYPODER Korean Editorial Pro이던 때)에 만든 파일은 작성자가 `TYPODER Korean Editorial Pro`, 1.0 버전으로 만든 파일은 `Korean Editorial Pro`이므로 `verify`·`accept`·`report`에 `--author`로 그 이름을 준다.
- `[원본 이름]_clean.docx`: 모든 변경을 수락하고 편집자 메모를 뺀 읽기용(요청 시).
- `[원본 이름]_editor_report.md`: 편집 보고서(요청 시, 또는 수정이 많을 때 권장).
- 서식(스타일, 굵게·기울임, 목록, 표, 각주, 하이퍼링크, 머리글·바닥글)은 XML 그대로 유지된다. 바뀐 글자만 `w:del`(삭제)과 `w:ins`(삽입)로 감싼다.
- 문서를 열면 변경 내용 추적이 켜져 있다(`w:trackRevisions`). 원치 않으면 `--no-track-flag`.

## 2. 작업 순서

스크립트 경로는 이 스킬 폴더 기준이다(`scripts/track_changes.py`).

```bash
# 1) 문단 목록 뽑기: 주소 [part para], 스타일, 목록·표 위치, 경고
python scripts/track_changes.py extract 원고.docx -o /tmp/paras.txt
# 대조·스캔용 텍스트
python scripts/track_changes.py text 원고.docx > /tmp/before.txt

# 2) SKILL.md대로 작업 종류와 고칠 폭을 정하고 고칠 곳을 정한 뒤(긴 문서는 스타일 시트), 편집 목록 작성 → /tmp/edits.json

# 3) 미리보기: 수정 묶음이 편집자 표기처럼 작은 단위인지 확인
python scripts/track_changes.py apply 원고.docx /tmp/edits.json -o 원고_tracked.docx --dry-run

# 4) 적용
python scripts/track_changes.py apply 원고.docx /tmp/edits.json -o 원고_tracked.docx

# 5) 검증: 모든 항목 PASS여야 전달
python scripts/track_changes.py verify 원고_tracked.docx --original 원고.docx --edits /tmp/edits.json --render

# 6) 검수: 수락본 텍스트로 SKILL.md의 검수(semantic-review.md)를 한다. 짚은 문장은 편집 목록에서 고쳐 4)부터 다시
python scripts/track_changes.py text 원고_tracked.docx --view accept > /tmp/after.txt
python scripts/fidelity_check.py facts /tmp/before.txt /tmp/after.txt --genre 보고서 [--terms 이름.txt] [--protected-text 보호문구.txt]

# 6-1) 편집 보고서 초안(검증을 내부에서 다시 돌리고 종류별 표·메모 목록을 채운다)
python scripts/track_changes.py report 원고_tracked.docx --original 원고.docx --edits /tmp/edits.json -o 원고_editor_report.md

# 7) 요청 시 읽기용 완성본
python scripts/track_changes.py accept 원고_tracked.docx -o 원고_clean.docx
python scripts/track_changes.py verify 원고_clean.docx --expect-clean
```

- `verify`는 ZIP 구조, XML 파싱, 네임스페이스, 변경 요소 존재(편집 목록에 실제 수정이 있으면 w:ins·w:del·문단 표시가 하나 이상. 삽입만·삭제만도 정상, 메모만 있는 목록은 0개여도 정상), 수정 ID 중복, 변경 요소 구조, 메모 연결, **모두 거부 = 원문**, **모두 수락 = 편집 목록의 기대 결과**를 검사한다. `--render`는 LibreOffice로 PDF를 만들어 쪽 수, 표시본(지운 글자·넣은 글자를 문서 순서대로 이은 글)의 어절이 PDF에 빠짐없이 있는지(98% 이상), 고친 쪽과 그 앞뒤 쪽 이미지(`[파일]_render/`)를 남긴다. 고친 쪽은 지운·넣은 조각이 아니라 고친 문단 전체의 글로 찾고(흔한 조각이 여러 쪽에 걸리지 않게), 고친 쪽을 앞뒤 쪽보다 먼저 담는다. 상한(`--max-pages`, 기본 12장) 때문에 못 담은 고친 쪽은 번호를 결과에 적는다. 레이아웃은 판정하지 않으니 이미지를 열어 눈으로 확인한다. 쪽 수는 pdfinfo로, 없으면 pdftotext의 쪽 구분으로 센다. 쪽 수·글자 대조·고친 쪽 전부의 이미지가 모두 확인돼야 PASS다. 도구가 없거나, 고친 쪽 일부를 이미지로 만들지 못했으면(`--max-pages` 상한, 쪽을 못 찾음) '통과'가 아니라 INFO로 표시하고 결과 줄에 '확인하지 못했거나 알릴 항목'으로 적는다. 변경 요소가 하나도 없는 파일을 억지로 통과시키는 옵션은 `--allow-no-revisions`(옛 이름 `--allow-missing`)다.
- docx 공식 스킬의 검증기가 있는 환경이면 교차 검사한다: `python validate.py 원고_tracked.docx --original 원고.docx --author "TYPODER Korean Editor"`.
- 긴 문서는 나눠서 적용할 수 있다. 2차 적용은 1차 결과 파일에 다시 `extract`한 뒤 한다(1차 삽입 구간은 보호되어 다시 고칠 수 없다).

## 3. 편집 목록 형식

무엇을 고칠지는 SKILL.md만 따른다. 이 절은 편집 목록을 쓰는 법만 다룬다.

문단 번호는 `extract`가 보인 주소를 그대로 쓴다(`document 3` → `"part": "document", "para": 3`). 번호가 헷갈리면 `"find"`(그 문단에만 있는 문구)로 가리킨다. 확인이 필요한 곳은 고치지 말고 `comment`로 남긴다.

JSON 배열 또는 `{"genre": "보고서", "dialogue": "auto", "terms": ["확정 이름·용어"], "protected_text": ["글자 그대로 지킬 문구"], "edits": [...]}`. `terms`와 `protected_text`는 선택이며 스타일 시트·사용자 보호 지시에서 확정된 항목만 넣는다. 파일 엔진의 위험 수정 게이트도 이 목록을 사용한다. `part` 기본값은 `document`(각주 `footnotes`, 미주 `endnotes`, 머리글 `header1` 등). 옛 편집 목록의 `mode` 필드는 호환을 위해 읽히더라도 현재 엔진 동작에는 영향을 주지 않는다.

| op | 필드 | 뜻 |
|---|---|---|
| `rewrite` (기본: `new_text`가 있으면) | `para`, `new_text` | 문단 전체의 새 글. 엔진이 원문과 비교해 바뀐 곳만 기록 |
| `replace` (기본: `old`가 있으면) | `old`, `new`, 선택 `para`·`occurrence`(1부터)·`all` | 문단 안 일부 교체. `para`가 없으면 파트 전체에서 찾되 하나여야 함 |
| `insert` | `para`, `after` 또는 `before`, `text` | 앵커 글자 앞뒤에 삽입 |
| `delete` | `old`, 선택 `para` | 글자 삭제 |
| `comment` | `para`, 선택 `anchor`, `text` | 메모. `anchor`가 없으면 문단 전체 |
| `split` | `para`, `before`, 선택 `delete_space`(기본 true) | `before` 글자 앞에서 문단 나누기 |
| `merge_next` | `para`, 선택 `joiner` | 다음 문단과 합치기(문단 표시 삭제). `joiner`는 이어 붙일 글자, 기본은 필요하면 공백 |
| `delete_paragraph` | `para` | 문단 통째 삭제 |
| `insert_paragraph` | `para`, `text`, 선택 `position`(`after` 기본, `before`) | 그 문단 뒤(앞)에 새 문단 넣기. 서식은 그 문단을 따른다. 여러 문단이면 `text`를 목록이나 줄바꿈으로 준다 |

항목마다 적는 것:

- `rule`: 수정의 종류를 짧게(예: `"맞춤법"`, `"띄어쓰기"`, `"호응"`, `"어색한 말"`, `"범용 포장"`, `"다시 설명"`). 보고서의 종류별 표가 된다.
- `tier`: 늘 적는다. 맞춤법·띄어쓰기·문장부호·호응처럼 틀린 것을 바로잡은 수정은 `확실`, 그 밖의 수정은 `권장`.
- `reason`: `확실`이 아닌 수정과 문단 다시 쓰기·삭제·합치기·나누기에 짧은 이유. '더 매끄럽게'는 이유가 아니다.
- `rule`이나 필요한 `reason`이 빠지면 `apply`가 적용하지 않고 빠진 항목을 알린다(종료 코드 2).
- 엔진은 `확실`이 아닌 수정에 `[권장] 종류 — 이유` 한 줄 메모를 자동으로 단다(같은 문단의 같은 이유는 한 번). 모두 달려면 `--reasons all`, 보고서로만 설명하려면 `--reasons none`.
- 장식이나 범용 포장을 지운 수정에는 `"decor": true`를 단다. 보고서의 '지운 장식 표현' 구역에 모인다(대조 도구가 일부러 경고하지 않는 삭제라 보고서가 마지막 확인 자리다).
- 표시만 하는 표기 문제(실제 인용, 사용자가 대사를 편집 범위에서 뺐을 때의 대사)는 하나하나 메모로 달지 않는다. 보고서에 모으고 원고에는 첫 자리에 '대사 표기 N곳: 보고서 참조' 메모 하나만 단다. 인물·내용 확인 메모는 자리마다 단다.

```json
{"genre": "보고서", "edits": [
  {"para": 3, "new_text": "AI 활용의 가장 큰 장점은 작업 효율을 높인다는 점이다.", "rule": "어색한 말", "tier": "권장", "reason": "'의'·'것'이 겹쳐 읽기 어렵다"},
  {"para": 26, "old": "방법에 대한 내용은", "new": "방법은", "rule": "번역투", "tier": "권장", "reason": "'에 대한 내용'이 뜻 없이 길다"},
  {"op": "merge_next", "para": 6, "joiner": " ", "rule": "문단 합치기", "tier": "권장", "reason": "한 문장짜리 문단이 이어진다"},
  {"op": "comment", "para": 27, "anchor": "업계 전문가들은", "text": "[확인 필요] 출처를 밝혀 주세요."},
  {"part": "footnotes", "para": 0, "old": "포함되어진", "new": "포함된", "rule": "이중 피동", "tier": "확실"}
]}
```

엔진의 안전장치: `apply`는 수정마다 그 수정 하나만 적용한 문단을 앞뒤 문단과 함께 판정하고, 판정을 출력과 보고서의 판정 표에 남긴다(`verify`·`report`도 같다).

- **사실 항목(자동 되돌림):** 숫자·단위·날짜·인용·식별자·확정한 이름의 변화, 부정 뒤집힘, 시간·상태어(`이미`·`아직` 등)를 지우거나 보탬, 범위어(`모두`·`오직`, 그것뿐임을 말하는 `뿐` 등) 변화, 두 이름의 역할 맞바꿈, 한 문장 안 원인·결과 맞바꿈. 파일에 쓰지 않는다. 사용자가 직접 지시했거나 출처에서 확인한 경우에만 `"decision": "KEEP"`, `"override": "user"`(또는 `"source"`), `"override_evidence"`(지시·출처를 적은 8자 이상의 근거)로 유지한다. 메모·초안으로 글을 쓰면서(대필·각색) 작가 메모(`아직 확인 안 됨` 등)를 본문에서 지워 사실 항목이 되면 `override: user`와 근거(요청과 메모 원문, 확인 필요로 옮긴 사실)를 적는다.
- **확인 항목(기본 적용):** 행위자·관계·인과 연결의 변화, 새 정보, 고유 정보 삭제, 낱말 바꿈, 표지가 든 문장을 통째로 지우거나 새로 쓴 것 등 도구의 추정. 원문과 수정문을 읽어 뜻이 바뀌었으면 `"decision": "REVERT"`와 `"decision_reason"`(무엇이 바뀌었는지 한 줄)을 적어 되돌린다. 뜻이 같은 수정(맞춤법·겹말·높임 교정, 장식 걷기)은 그대로 둔다.
- **위험이 아닌 것:** 같은 부류 안의 바꿈(`이미 → 벌써`, `모두 → 전부`), 같은 뜻으로 남는 말(`오직 민수만 → 민수만`), 띄어쓰기·오타 교정, 뜻을 바꾸지 않는 문체 수정.
- 되돌림으로 변경 요소가 0개여도 검증은 통과하고, 보고서에 '편집 후보 / 게이트 되돌림 / 최종 적용' 수를 적는다. 편집 목록 맨 위의 `"genre"`(웹소설·소설·보고서 등), `"dialogue"`(auto·edit·protect), `"terms"`, `"protected_text"`를 따른다. `--no-gate`는 엔진 시험용이다.

## 4. 편집 단위를 정하는 법

- **바꿀 곳만 넣는다.** 정상 문단은 편집 목록에 넣지 않는다. 한 글자도 바뀌지 않는 `rewrite`는 경고만 남는다.
- 한 문단에 수정이 여러 곳이면 `rewrite` 하나로 문단 전체를 주는 편이 안전하다(범위 겹침 오류가 없다). 엔진이 낱말 단위로 비교해 `AI~~를~~ 활용~~하는 것~~의 가장 ~~중요한~~큰 …`처럼 편집자 표기와 같은 작은 수정 묶음으로 기록한다.
- 원문과 거의 다른 문장으로 새로 쓰면 한 덩어리 삭제+삽입으로 기록된다. 문장 통째 교체는 작업 종류와 고칠 폭이 허락할 때만 하고, 이유를 보고서에 적는다.
- `--dry-run` 미리보기에서 수정 묶음이 부자연스럽게 쪼개졌으면(`--granularity edit`로 문단당 한 덩어리) 방식을 바꾼다.
- 한 줄 문단 여러 개를 합칠 때: 각 문단의 글을 먼저 고치고(`rewrite`/`replace`), 앞 문단마다 `merge_next`를 준다. 연쇄 병합(6→7→8→9)도 된다.
- 확신이 없는 수정, 사실 확인이 필요한 곳, 작성자 판단이 필요한 곳(출처 없는 권위, 중의적 문장, 표기 선택)은 고치지 말고 `comment`로 남긴다.
- 표 셀, 목록, 각주, 머리글도 같은 방식이다. 개조식 목록의 명사형 종결은 고치지 않는다.
- 새 글에 줄바꿈(`\n`)을 넣지 않는다. 문단을 나누려면 `split`, 없던 문단을 넣으려면 `insert_paragraph`.

## 5. 개체 표식

문단 안의 글자가 아닌 요소는 `extract`에서 한 칸짜리 표식으로 보인다. 새 글에도 **같은 순서와 개수로 그대로** 둔다. 표식을 지우거나 옮기면 엔진이 거부한다.

| 표식 | 뜻 | 표식 | 뜻 |
|---|---|---|---|
| ⟦TAB⟧ | 탭 | ⟦IMG⟧ | 그림·도형·개체 |
| ⟦BR⟧ | 줄바꿈(같은 문단 안) | ⟦MATH⟧ | 수식 |
| ⟦FN⟧ / ⟦EN⟧ | 각주·미주 번호 | ⟦SYM⟧ | 기호 글꼴 문자 |
| ⟦FNREF⟧ / ⟦ENREF⟧ | 각주·미주 본문 앞 번호 | ⟦CMT⟧ | 기존 메모 표시 |
| ⟦FLD⟧ | 필드(목차, 상호 참조, 쪽 번호, 날짜) | ⟦OBJ⟧ | 기타 요소 |

필드 결과 글자(목차 항목 등)는 편집 대상에서 빠진다. 필드를 고치려면 Word에서 필드를 업데이트해야 한다.

## 6. 오류 메시지와 대처

`apply`·`verify`가 오류를 내면 word-troubleshooting.md 1절.

## 7. 보존되는 것과 한계

보존되는 서식과 편집하지 않는 곳(텍스트 상자·수식·필드 등)은 word-troubleshooting.md 2절.

## 8. 편집 보고서

`[원본 이름]_editor_report.md`는 `report` 명령으로 초안을 만든 뒤 빈칸('고치지 않은 것', '스타일 시트 요약')을 채우고 표를 다듬는다. 짧고 쓸모 있게 쓴다.

```markdown
# 편집 보고서: [원본 파일명]

- 요청: 다듬기 / 장르: 사내 보고서(한다체) / 작성자 표기: TYPODER Korean Editor
- 결과: 수정 N곳(삽입 N, 삭제 N), 문단 병합 N, 문단 삭제 N, 메모 N
- 검증: verify 전 항목 통과, 모두 거부 시 원문과 동일

## 주요 수정 (종류별)
| 종류 | 곳 | 예 |
|---|---|---|
| 이중 피동 | 3 | 작성되어졌다 → 작성되었다 |

## 확인이 필요한 곳 (메모로 남김)
1. 27문단 "업계 전문가들은 …": 출처가 없습니다.

## 고치지 않은 것
- 28·29문단의 피동·'것'·반복은 뜻에 필요한 표현이라 유지.

## 스타일 시트 요약 (긴 문서)
```
