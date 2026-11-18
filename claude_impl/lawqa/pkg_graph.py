"""PKG-v2 ① 명제 그래프 구축(오프라인, 결정적). API를 쓰지 않는다.

명제 = 독립된 법적 효과 + 그 원문 구간. 구조(조·항·호·단서·표 행·비고)는 코드가 정확한 구간으로 자르고,
속성(주체·양태·값·위임·외부 의존·지정 전제·조건·예외)은 원문 규칙으로 뽑는다. 속성은 검색·카드용
주석이며 법적 정본은 원문 구간이다(카드와 원문이 충돌하면 원문 우선).

명제 종류
  SINGLE  : 호가 없는 항/조의 본문(단서가 있으면 MAIN + PROVISO로 분리)
  LEAD    : 각 호를 거느리는 도입 문장(공통 효과)
  ITEM    : 호(그 아래 목 포함). 검색 표현에는 도입 문장을 붙인다(목록 구성원 + 공통 효과)
  ROW/NOTE: 별표 표의 행 / 비고·주석 문단
  FIGURE  : 그림 서식(남은 텍스트만, 이미지 미검토)
간선(필수 근거, 방향 = 해석할 명제 → 함께 읽어야 할 원문)
  COMMON(ITEM→LEAD), EXCEPTION(MAIN→PROVISO), REFERENCE(명시 조문 참조), DEFINITION(정의 호),
  TABLE(조문→별표 / 별표→근거 조문), REVIEWED:*(corpus 검토 간선)
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from .corpus import Corpus, Unit
from .graph import EvidenceGraph
from .search import alias_of

_HO = re.compile(r"^\s*(\d+(?:의\d+)?)\.\s*")  # 가지 번호 호(7의3.) 포함
_MOK = re.compile(r"^\s*([가-하])\.\s*")
_SUB = re.compile(r"^\s*(\(\d+\)|\d+\))\s*")
_META = re.compile(r"^\s*(\[(?:전문개정|본조신설|제목개정|종전|시행일|조번호|본조|제목)[^\]]*\]|<[^>]*>)\s*$")
_DELETED = re.compile(r"^\s*(?:\d+\.|[가-하]\.)?\s*삭제\s*(?:<[^>]*>)?\s*$")
_DELETED_PARA = re.compile(r"^\s*[①-⑳]?\s*삭제\s*(?:<[^>]*>)?\s*$")  # G03: 항 전체 삭제
_PROVISO_AT = re.compile(r"(?:(?<=[.。])|(?<=[.。]\s))\s*다만,")       # 문장 첫머리의 '다만,'
_MOK_LINE = re.compile(r"^\s*(?:[가-하]\.|\(\d+\))")
_ART_PREFIX = re.compile(r"^제\d+조(?:의\d+)?\([^)]*\)\s*")
_TAGS = re.compile(r"<[^>]{0,150}>")

ROLES = [
    ("SEOUL_MAYOR_OR_GU", r"시장\s*또는\s*구청장|시장ㆍ구청장|시장·구청장"),
    ("SEOUL_MAYOR", r"서울특별시장|시장"),
    ("GU_HEAD", r"구청장|자치구청장"),
    ("ANYONE", r"누구든지"),
    ("OWNER", r"소유자등|소유자|보호자"),
    ("FACILITY_MANAGER", r"관리자|운영자|설치ㆍ관리자|사업시행자|관리청"),
    ("COMMITTEE", r"위원회|위원장|위원"),
    ("PUBLIC_BODY", r"공단|공사|센터의\s*장|센터|기관의\s*장|기관"),
    ("CITIZEN", r"시민|청년|이용자|주민"),
]
_SUBJ = re.compile(r"(?:^|[\s①-⑳])((?:서울특별시장|시장\s*또는\s*구청장|시장ㆍ구청장|자치구청장|시장|구청장|누구든지|부원장|"
                   r"[가-힣ㆍ·]{0,10}(?:수탁자|대행자|운전자)|[가-힣\s]{2,20}?(?:고자\s*하는|려는)\s*자|"
                   r"[가-힣ㆍ·]{0,14}(?:소유자등|소유자|보호자|관리자|운영자|관리청|위원회|위원장|공단|센터의\s*장|"
                   r"센터|기관의\s*장|이용자|시민|청년|사업자)))\s*(?:은|는)\s")
MODALITY = [
    ("DEFINITION", r"[\"“'‘][^\"”'’]{1,30}[\"”'’]\s*(?:(?:이)?란|(?:이)?라\s*함은).*(?:말한다|뜻한다|말하며|뜻하며)"),  # F18·F22
    ("PROHIBITION", r"(?:하여서는|해서는|되어서는)\s*(?:아니\s*된다|아니된다|안\s*된다)|금지한다|할\s*수\s*없다"),
    ("EFFORT", r"노력하여야\s*한다|노력해야\s*한다|노력한다"),
    ("DELEGATION", r"(?:규칙|조례)으로\s*정한다|규칙으로\s*정하는|(?:따로|별도로)\s*정한다|정하는\s*바에\s*따른다|시장이\s*정한다"),
    ("STANDARD", r"별표\s*\d*\s*(?:와|과)\s*같다|다음\s*각\s*호와\s*같다|기준은\s"),
    ("DISCRETION", r"할\s*수\s*있다"),
    ("DUTY", r"하여야\s*한다|해야\s*한다"),
    ("EFFECT", r"과태료|할인한다|면제한다|감면한다|징수한다|부과한다|반환한다|지급한다|지원한다"),
]
_VALUE = re.compile(r"(?:100분의\s*\d+|\d+분의\s*\d+|\d[\d,\.]*\s*(?:[십백천만억]{1,3}\s*)?(?:원|퍼센트|%|％|일|개월|년(?!도)|시간|분(?!의)|미터|m|㎡|킬로미터|km|"
                    r"대(?![학상])|명|세(?!대)|회(?![계의])|면(?![적제])|층|급지|배|부(?![터서과])|건(?![축물설])))")
# G01: 단위 뒤 조사('500원을', '3년으로')는 허용하고, 단위가 아닌 낱말(1년도·10분의·1세대·1부터 등)만 막는다.
_RANGE = re.compile(r"\d\s*[~～]\s*\d|범위")
_PROV = (r"(?:\s*제\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?"
         r"(?:\s*(?:및|또는|ㆍ|,)\s*(?:법\s*)?제\d+(?:조(?:의\d+)?|항|호)(?:제\d+항)?(?:제\d+호)?)*)?")
_FOLLOW = r"\s*(?:에\s*따른다|을\s*따른다|를\s*따른다|을\s*준용|를\s*준용|준용한다|에\s*의한다)"
_DELEG = [
    ("ORDINANCE_RULE", r"규칙으로\s*정|규칙이\s*정하는|규칙에서\s*정|시행규칙(?:으로|에서)\s*정"),
    ("MAYOR", r"시장이\s*(?:따로|별도로)?\s*정|시장이\s*정하는"),
    # 다른 서울시 조례: 내용이 그 조례를 '따르는' 경우와 수당·여비 등 값을 그 조례에 맡긴 경우만.
    ("OTHER_ORDINANCE", r"「서울특별시[^」]*조례」" + _PROV + r"(?:" + _FOLLOW + r"|\s*에\s*따라[^.]{0,30}(?:수당|여비|보수|비용))"),
    # 외부 법령: 법령 이름(또는 '법·영·규칙' 약칭)+조문 바로 뒤에 '따른다/준용'. 조례 내부 조문 준용은 제외.
    ("EXTERNAL_LAW", r"(?:「[^」]+」|(?<![가-힣])(?:법|영|시행령|시행규칙))" + _PROV + _FOLLOW
                     + r"|「[^」]+」[^「」.]{0,90}에\s*따른다"
                     + r"|(?:관련\s*법령|관계\s*법령|다른\s*법령)[^.]{0,40}(?:준용|에\s*따른다)"),
]
# W1(빌드 옵션, 최종 PKG11.1에서는 끔): 기준표 전체를 외부 법령 별표에 맡김(예: '기준은 영 제51조제1항 별표 4와 같다').
# 켜면 선택 카드 입력이 바뀌는데(449 46문항 등) 실제 실행으로 확인하지 않았다. design/WEAK_LINKS_PROTOCOL.md 참고.
_EXT_LAW_W1 = r"|(?:「[^」]+」|(?<![가-힣])(?:법|영|시행령|시행규칙))" + _PROV + r"\s*별표\s*\d+\s*(?:와|과)\s*같다"


_LIST_TAIL = re.compile(r"\s*(?:의\s*(?:어느\s*하나에\s*해당하는\s*)?[가-힣]+|에\s*해당하는\s*[가-힣]+|에\s*따라)?")
_LAW_REF = re.compile(r"(?:「[^」]+」(?:\([^)]*\))?|(?<![가-힣])(?:법|영|시행령|시행규칙))" + _PROV + r"(?:의\d+)?")


def _external_list(text: str):
    """'각 호'가 외부 법령 조문의 목록을 가리키는지. '다음 각 호'는 조례 내부 목록이다.
    PKG11: 원문 참조 구간(span)도 돌려준다(예: '법 제49조제1항 및 제2항 각 호의 행위'). 표시는 이 구간만 인용한다."""
    for m in re.finditer(r"각\s*(?:호|목)", text):
        before = text[max(0, m.start() - 60):m.start()]
        if re.search(r"다음\s*$", before):
            continue
        ref = re.search(r"(?:「[^」]+」|(?<![가-힣])(?:법|영|시행령|시행규칙))" + _PROV + r"\s*$", before)
        if ref:
            tail = _LIST_TAIL.match(text, m.end())
            span = (before[ref.start():] + m.group(0) + (tail.group(0) if tail else "")).strip()
            span = re.sub(r"(?<=[가-힣])(?:을|를)$", "", span)  # '각 호의 행위를' → '각 호의 행위'(조사 제거)
            return m.group(0), before[-50:] + m.group(0), span
    return None


_EXT_DEF = re.compile(r"(?:이)?란\s*「[^」]+」[^.]{0,80}(?:에\s*따른|에\s*의한|을\s*말한다|를\s*말한다)")
# W1(빌드 옵션, 최종 PKG11.1에서는 끔): 정의 대상 자체를 외부 법령 조문이 정하는 것으로 맡김
# (예: '"반려동물"이란 … 「동물보호법 시행규칙」(이하 …) 제3조에서 정하는 동물을 말한다')
_EXT_DEF2 = re.compile(r"(?:이)?란\s[^.]{0,80}?(?:「[^」]+」(?:\([^)]*\))?|(?<![가-힣])(?:법|영|시행령|시행규칙))"
                       r"\s*제\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?\s*(?:에서|으로|로)\s*정하는\s*[^.]{0,20}?(?:을|를)\s*말한다")
_DESIGNATION = re.compile(r"(?:지정된|지정한|지정ㆍ고시된|지정ㆍ고시한|지정ㆍ고시하는|고시된)\s*[가-힣ㆍ·\s]{0,12}"
                          r"(?:구역|장소|거리|시설|지역|기관|시범기관|학교|지구)|"
                          r"(?:으로|로)\s*지정한\s*경우|(?:을|를)\s*지정한\s*경우|"
                          r"(?:구역|장소)으로\s*지정된|"
                          # PKG11: '시장이 지정하는 공원', '금연구역으로 지정되는 장소'(절차 규정 '지정하는 경우'는 제외)
                          r"(?:지정하는|지정되는)\s*(?:공원|구역|장소|거리|시설|지역|기관|학교|지구)|"
                          r"(?:으로|로)\s*지정되는")
_COND = re.compile(r"(?:[^.,;]|(?<=\d)[.,](?=\d)){2,80}?(?:경우|때)(?:에는|에도|에|에만)?")  # 소수점·천 단위는 경계 아님(F23)
_EXC = re.compile(r"다만,[^.]*\.|[^.,]{2,60}(?:은|는|을|를)?\s*제외(?:한다|하고|하며)")


def _clean(t: str) -> str:
    t = _TAGS.sub("", t)
    return re.sub(r"[ \t　]+", " ", re.sub(r"\n\s*", " ", t)).strip()


def _role(subject: str) -> str:
    for role, pat in ROLES:
        if re.search(pat, subject):
            return role
    return "OTHER" if subject else "NONE"


_REF_IN = r"(?:제\d+조(?:의\d+)?(?:제\d+항)?|제\d+항)"
_JUNYONG = re.compile(r"(?:" + _REF_IN + r"\s*(?:과|와|및|,|ㆍ)\s*)*" + _REF_IN
                      + r"(?:(?!제\d+조|제\d+항)[^.「」]){0,40}?준용[가-힣]*")


def _junyong_phrase(own: str) -> str:
    """조례 내부 조문을 준용하는 원문 구절(PKG11 후보 C). 외부 법령 조문의 준용('법 제N조', '「…」 제N조')은 제외."""
    for m in _JUNYONG.finditer(own):
        if re.search(r"(?:법|」|령|영|규칙)\s*$", own[max(0, m.start() - 8):m.start()]):
            continue
        return m.group(0).strip()[:120]
    return ""


def _designation_phrase(own: str, effect: str) -> str:
    """지정 관련 구절: 일치 구간을 낱말 경계까지 넓히고, 바로 앞의 주어('시장이' 등)가 있으면 포함한다."""
    for src, mark in ((own, ""), (effect, " (도입문)")):
        m = _DESIGNATION.search(src)
        if m:
            s0 = m.start()
            while s0 > 0 and not re.match(r"[\s,.(「」]", src[s0 - 1]):
                s0 -= 1
            sm = re.search(r"(\S+(?:이|가))\s+$", src[:s0])
            if sm:
                s0 = sm.start(1)
            return src[s0:m.end()].strip()[:80] + mark
    return ""


def attributes(effect_text: str, own_text: str, w1: bool = False) -> dict:
    """effect_text: 도입문+호(공통 효과 포함), own_text: 이 명제 고유 구간(값·조건은 여기서).
    w1: W1 외부 참조 표시 보완(외부 법령 별표 위임, 외부 조문이 정하는 정의). 기본 끔(최종 PKG11.1)."""
    t = _clean(effect_text)
    own = _clean(own_text)
    m = _SUBJ.search(t)
    subject = m.group(1).strip() if m else ""
    modality = "OTHER"
    for name, pat in MODALITY:
        if re.search(pat, own) or (name not in ("DEFINITION",) and re.search(pat, t) and modality == "OTHER"
                                     and not re.search(r"(?:한다|된다|있다|같다|말한다)\.?\s*$", own)):
            modality = name
            if re.search(pat, own):
                break
    delegation = {"to": "NONE", "text": ""}
    for name, pat in _DELEG:
        if w1 and name == "EXTERNAL_LAW":
            pat += _EXT_LAW_W1
        mm = re.search(pat, own)
        if mm:
            delegation = {"to": name, "text": mm.group(0)[:120]}
            break
    ext = None
    el = _external_list(own)
    if el:
        ext = {"what": "LIST", "text": el[1].strip()[:120], "span": el[2][:120]}
    elif _EXT_DEF.search(own) or (w1 and _EXT_DEF2.search(own)):
        first = _EXT_DEF.search(own)
        dm = first or _EXT_DEF2.search(own)
        refs = list(_LAW_REF.finditer(dm.group(0)))
        lr = (refs[0] if first else refs[-1]) if refs else None  # EXT_DEF2: 정의를 맡긴 마지막 법령 조문
        ext = {"what": "DEFINITION", "text": dm.group(0)[:120], "span": (lr.group(0).strip() if lr else "")[:120]}
    values = [v for v in dict.fromkeys(v.strip() for v in _VALUE.findall(own))
              if not re.match(r"^\d{4}\s*년|^\d+\.\s", v)]  # 연도·항목 번호('3. 대')는 값이 아님
    if values:
        status = "RANGE" if _RANGE.search(own) else "DIRECT"
    elif delegation["to"] != "NONE":
        status = "DELEGATED"
    else:
        status = "NONE"
    conds = [c.strip() for c in _COND.findall(own)][:3]
    excs = [c.strip() for c in _EXC.findall(own)][:3]
    return {"subject": subject, "subject_role": _role(subject), "modality": modality,
            "values": values, "value_status": status, "delegation": delegation,
            "external": ext, "designation_required": bool(_DESIGNATION.search(own) or _DESIGNATION.search(t)),
            # PKG11: 지정 관련 '구절'만 보존한다. 긍정 조건·제외·분기·지정 후보 중 무엇인지는 판정하지 않는다.
            "designation_phrase": _designation_phrase(own, t),
            "junyong": _junyong_phrase(own),
            "conditions": conds, "exceptions": excs}


# --------------------------------------------------------------------- 분할
def _lines_with_offsets(text: str, start: int = 0):
    out, pos = [], 0
    for ln in text.split("\n"):
        if pos >= start:
            out.append((pos, ln))
        elif pos + len(ln) > start:
            out.append((start, ln[start - pos:]))
        pos += len(ln) + 1
    return out


def _span(raw: str, s: int, e: int) -> tuple[int, int]:
    while s < e and raw[s] in " \t\n":
        s += 1
    while e > s and raw[e - 1] in " \t\n":
        e -= 1
    return s, e


def _split_proviso(raw: str, s: int, e: int):
    """G05: 한 명제 구간 안의 문장 첫머리 '다만,'을 본문/단서로 나눈다.
    단서 뒤에 목(가.·(1)) 줄이 이어지면 목이 본문에 속하므로 나누지 않는다(구간이 끊기기 때문)."""
    seg = raw[s:e]
    m = _PROVISO_AT.search(seg)
    if not m:
        return None
    k = s + m.start() + seg[m.start():].index("다만,")
    if any(_MOK_LINE.match(ln) for ln in raw[k:e].split("\n")[1:]):
        return None
    s1, e1 = _span(raw, s, k)
    s2, e2 = _span(raw, k, e)
    return (s1, e1), (s2, e2)


def segment_body(u: Unit) -> list[dict]:
    raw = u.raw_text
    start = raw.index("\n") + 1 if raw.startswith("[") and "\n" in raw else 0
    m = _ART_PREFIX.match(raw[start:])
    if m:
        start += m.end()
    lines = [(o, ln) for o, ln in _lines_with_offsets(raw, start) if ln.strip() and not _META.match(ln)]
    lead, groups = [], []
    for o, ln in lines:
        if _HO.match(ln):
            groups.append([(o, ln)])
        elif groups:
            groups[-1].append((o, ln))
        else:
            lead.append((o, ln))
    props = []
    alias = alias_of(u)
    if lead:
        s, e = _span(raw, lead[0][0], lead[-1][0] + len(lead[-1][1]))
        lead_text = raw[s:e]
        if groups:
            sp = _split_proviso(raw, s, e)
            if sp:  # 도입문 안의 단서(예: 가산금 4배. 다만, 유예기간)
                props.append({"kind": "LEAD", "id": alias, "start": sp[0][0], "end": sp[0][1]})
                props.append({"kind": "PROVISO", "id": alias + "단서", "start": sp[1][0], "end": sp[1][1]})
            else:
                props.append({"kind": "LEAD", "id": alias, "start": s, "end": e})
        elif _DELETED_PARA.match(_TAGS.sub("", lead_text)):
            props.append({"kind": "DELETED", "id": alias, "start": s, "end": e})
        else:
            k = lead_text.find("다만,")
            if k > 0:
                s1, e1 = _span(raw, s, s + k)
                s2, e2 = _span(raw, s + k, e)
                props.append({"kind": "MAIN", "id": alias, "start": s1, "end": e1})
                props.append({"kind": "PROVISO", "id": alias + "단서", "start": s2, "end": e2})
            else:
                props.append({"kind": "SINGLE", "id": alias, "start": s, "end": e})
    for g in groups:
        no = _HO.match(g[0][1]).group(1)
        no_id = no.replace("의", "호의") if "의" in no else no + "호"
        s, e = _span(raw, g[0][0], g[-1][0] + len(g[-1][1]))
        if _DELETED.match(raw[s:e]):
            continue
        pid = f"{alias}제{no_id}"
        if any(p["id"] == pid for p in props):
            pid += f"#{len(props)}"
        sp = _split_proviso(raw, s, e)
        if sp:  # 호 안의 단서(예: 80% 할인. 다만, 환승주차장은 3시간 면제)
            props.append({"kind": "ITEM", "id": pid, "start": sp[0][0], "end": sp[0][1], "item_no": no})
            props.append({"kind": "PROVISO", "id": pid + "단서", "start": sp[1][0], "end": sp[1][1],
                          "item_no": no})
        else:
            props.append({"kind": "ITEM", "id": pid, "start": s, "end": e, "item_no": no})
    if not props:
        s, e = _span(raw, start, len(raw))
        props.append({"kind": "SINGLE", "id": alias, "start": s, "end": e})
    return props


_ROW_NO = re.compile(r"^\d+(?:-\d+)?\s*\.")       # 표 첫 칸의 항목 번호(1. / 2-1.)
_SUB_ROW = re.compile(r"^[가-하]\.")               # 앞 번호 행에 딸린 목 행
_NOTE_START = re.compile(r"^(?:\d+\s*\.|※)")  # 비고의 새 항목(목·(1)은 그 항목에 포함)
_NOTE_MARK = ("<비고>", "비고", "<비 고>", "※ 비고")


# F29: 원본 HWP·넘겨받은 Markdown에는 있으나 동결 원문(corpus.json)에 빠진 표 밖 단위 문구.
# 동결 원문은 바꾸지 않고, 출처와 함께 보충 문구로만 더한다(검색·카드·전달에 표의 공통 단위로 붙음).
SUPPLEMENT_CAPTIONS = {
    "unit:2171767:별표": {"text": "(단위 : 원/구획)",
                          "source": "handoff_r9/materials/annex/2171767_별표.md:7 (원본 HWP Section0 표 앞 문단)"},
}


def _pieces(o: int, ln: str):
    """비고 줄을 ' / '로 이어진 조각으로 나눈다(표 렌더링이 여러 줄을 한 줄로 이은 경우). (오프셋, 조각)."""
    out, pos = [], 0
    for m in re.finditer(r"\s/\s", ln):
        out.append((o + pos, ln[pos:m.start()]))
        pos = m.end()
    out.append((o + pos, ln[pos:]))
    return [(po, pc) for po, pc in out if pc.strip()]


def segment_table(u: Unit) -> list[dict]:
    """표 단위 분할(F02·F03, PKG7: G04·G20 수정).

    - 표의 첫 행은 머리글이다. 데이터 행이 나오기 전에 첫 칸이 머리글 첫 칸과 같은 행(병합 셀 반복)만
      다단 머리글로 잇는다. 숫자가 셀 첫머리에 있는지는 행 판별에 쓰지 않는다.
    - 목(가.) 행은 바로 앞 번호 행의 첫 칸을, 표 사이의 짧은 번호 제목(2. 휴양시설)은 소제목으로
      머리글 앞에 붙인다. 머리글 앞의 표 제목·단위 줄도 행 머리글 앞에 붙인다.
    - 표 안팎의 비고는 번호(N.)·※ 항목별 NOTE 명제로 둔다. ' / '로 이어진 한 줄도 조각으로 나눠 같은 규칙을
      쓴다. 목(가.)·(1)은 그 항목에 포함한다.
    - 행의 '〃'(위와 같음)는 바로 위 행의 같은 칸 값으로 풀어 resolved에 둔다(원문 구간은 그대로).
    """
    text = u.text
    alias = alias_of(u)
    props, k_row, k_note = [], 0, 0
    header = header_first = None
    data_seen, seen_table, in_table = False, False, False
    section, parent, caption = "", "", ""
    prev_cells = None
    note_buf = []
    sup = SUPPLEMENT_CAPTIONS.get(u.id)

    def flush_notes():
        nonlocal note_buf, k_note
        if note_buf:
            s, e = note_buf[0][0], note_buf[-1][0] + len(note_buf[-1][1])
            k_note += 1
            props.append({"kind": "NOTE", "id": f"{alias}#비고{k_note}", "start": s, "end": e})
            note_buf = []
    for o, ln in _lines_with_offsets(text):
        s = ln.strip()
        if s.startswith("[표 ") and "행×" in s:
            flush_notes()
            in_table, seen_table = True, True
            header = header_first = None
            data_seen, parent, caption, prev_cells = False, "", "", None
            continue
        if s == "[표 끝]":
            flush_notes()
            in_table = False
            continue
        if not s:
            continue
        if in_table and s.startswith("|"):
            flush_notes()
            cells = [c.strip() for c in s.strip("|").split("|")]
            if header is None:
                header, header_first = s, cells[0]
                continue
            if not data_seen and cells[0] == header_first:
                header = header + " " + s  # 병합 셀이 반복된 다단 머리글
                continue
            data_seen = True
            if _ROW_NO.match(cells[0]):
                parent = cells[0][:150]
            ctx = section
            if _SUB_ROW.match(cells[0]) and parent:
                ctx = f"{section} {parent}".strip()
            unit_cap = " ".join(x for x in (caption, sup["text"] if sup else "") if x)
            k_row += 1
            rec = {"kind": "ROW", "id": f"{alias}#행{k_row}", "start": o, "end": o + len(ln),
                   "header": " ".join(x for x in (ctx, unit_cap, header) if x)}
            if "〃" in cells and prev_cells and len(prev_cells) == len(cells):
                cells = [prev_cells[i] if c == "〃" else c for i, c in enumerate(cells)]
                rec["resolved"] = "| " + " | ".join(cells) + " |"
            prev_cells = cells
            props.append(rec)
            continue
        # 표 밖·표 안의 비표 행
        if not in_table and len(s) <= 30 and _ROW_NO.match(s) and not s.endswith("다."):
            flush_notes()
            section = s  # 표 사이 소제목
            continue
        if not seen_table:
            continue  # 첫 표 앞의 제목 줄
        if in_table and header is None and not caption:
            caption = s  # 머리글 앞의 표 제목·단위 줄
        for po, pc in _pieces(o, ln):
            t = pc.strip()
            if t in _NOTE_MARK:
                flush_notes()
                continue
            if _NOTE_START.match(t) and note_buf:
                flush_notes()
            note_buf.append((po, pc))
    flush_notes()
    if not props:
        props.append({"kind": "FIGURE" if u.kind == "DIAGRAM" else "SINGLE", "id": alias,
                      "start": 0, "end": len(text)})
    if sup:
        props.append({"kind": "NOTE", "id": f"{alias}#보충단위", "start": 0, "end": 0, "supplement": sup})
    return props


# --------------------------------------------------------------------- 구축
def build(corpus: Corpus, draft_path: Path | None = None, w1: bool = False) -> dict:
    graph = EvidenceGraph(corpus)
    props: list[dict] = []
    by_unit: dict[str, list[dict]] = defaultdict(list)
    for u in corpus.units:
        segs = segment_body(u) if u.kind == "BODY" else segment_table(u)
        base = u.raw_text if u.kind == "BODY" else u.text
        lead = next((p for p in segs if p["kind"] == "LEAD"), None)
        for p in segs:
            if p.get("supplement"):  # F29: 동결 원문에 없는 표 밖 단위 문구(출처 기록)
                sup = p["supplement"]
                rec = {"id": p["id"], "kind": "NOTE", "unit": u.id, "label": u.label, "law": u.law,
                       "span": [0, 0], "span_basis": "supplement", "text": sup["text"], "title": u.title,
                       "supplement_source": sup["source"], "attrs": attributes(sup["text"], sup["text"], w1)}
                props.append(rec)
                by_unit[u.id].append(rec)
                continue
            own = base[p["start"]:p["end"]]
            effect = (base[lead["start"]:lead["end"]] + " " + own) if (lead and p["kind"] == "ITEM") else own
            if p["kind"] == "ROW":
                effect = p["header"] + " " + own
            rec = {"id": p["id"], "kind": p["kind"], "unit": u.id, "label": u.label, "law": u.law,
                   "span": [p["start"], p["end"]], "span_basis": "raw" if u.kind == "BODY" else "rendered_table",
                   "text": own, "title": u.title, "attrs": attributes(effect, own, w1)}
            if p["kind"] == "ITEM":
                rec["lead_text"] = base[lead["start"]:lead["end"]] if lead else ""
                rec["item_no"] = p["item_no"]
            if p["kind"] == "ROW":
                rec["header"] = p["header"]
                if p.get("resolved"):
                    rec["resolved"] = p["resolved"]  # G20: '〃'를 위 행 값으로 푼 행
                cells = [c.strip() for c in (p.get("resolved") or own).strip().strip("|").split("|")]
                # F19: 숫자가 있다고 값이 아니다. 항목 번호·법령 참조 셀은 값에서 빼고, '준용·공고' 셀은 위임 값이다.
                # F21: 법령 참조와 직접 기준이 섞인 셀은 참조로만 보지 않고, 셀 안의 직접 수치도 값으로 둔다.
                refs = [c for c in cells if _CELL_LAWREF.search(c)]
                vals = [c for c in cells if re.search(r"\d", c) and c not in refs and not _ROW_NO.match(c)]
                mixed = [v for c in refs for v in _VALUE.findall(_CELL_LAWREF.sub(" ", c))]
                if refs:
                    rec["cell_refs"] = list(dict.fromkeys(refs))[:6]
                if mixed:
                    vals = vals + list(dict.fromkeys(m.strip() for m in mixed))
                if vals:
                    rec["attrs"]["values"] = list(dict.fromkeys(vals))[:12]
                    rec["attrs"]["value_status"] = "RANGE" if any(re.search(r"[~～]", v) for v in vals) else "DIRECT"
                else:
                    rec["attrs"]["values"] = []
                    adopt = [c for c in refs if re.search(r"준용|공고|에\s*따른다", c)]
                    if adopt:
                        to = "OTHER_ORDINANCE" if re.search(r"「서울특별시[^」]*조례」", adopt[0]) else "EXTERNAL_LAW"
                        rec["attrs"]["value_status"] = "DELEGATED"
                        rec["attrs"]["delegation"] = {"to": to, "text": adopt[0][:120]}
                    elif rec["attrs"]["value_status"] in ("DIRECT", "RANGE"):
                        rec["attrs"]["value_status"] = "NONE"
            if u.kind == "DIAGRAM":
                rec["extraction_status"] = "IMAGE_UNREVIEWED"
            if p["kind"] == "DELETED":
                rec["attrs"] = attributes("", "")  # G03: 삭제된 항. 원문 포함 검사용으로만 남기고 검색에서 뺀다
            if u.kind == "BODY" and _EFFECT_ADJUST.search(own):
                cl = _clean(own)
                scope = [f"{(m.group(1) or '').strip()}제{m.group(2)}항에 따른 {m.group(3).strip()}"
                         for m in _APPLIES_TO.finditer(cl)
                         if not re.search(r"(?:」|법|영|규칙|같은\s*법)\s*$", cl[max(0, m.start() - 14):m.start()])]
                if scope:
                    rec["attrs"]["applies_to"] = list(dict.fromkeys(scope))[:3]
            props.append(rec)
            by_unit[u.id].append(rec)
    ids = [p["id"] for p in props]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_prop_id")
    # G19: '이 조 제N항(제M호)은 YYYY년 M월 D일까지 유효함' 안내를 대상 명제의 효력 속성으로 옮긴다(원문은 보존).
    for p in props:
        u = corpus.by_id[p["unit"]]
        if u.kind != "BODY":
            continue
        for m in _VALID_UNTIL.finditer(_TAGS.sub("", p["text"])):
            until = f"{int(m.group(3)):04d}-{int(m.group(4)):02d}-{int(m.group(5)):02d}"
            for t in graph._resolve_article(u.doc_id, u.article, int(m.group(1))):
                for q in by_unit[t]:
                    if m.group(2) and q.get("item_no") != m.group(2):
                        continue  # 호를 지정한 안내는 그 호(와 그 단서)에만
                    q["attrs"]["valid_until"] = until
                    q["attrs"]["expired"] = until < (corpus.by_id[t].effective_date or "")

    # 정의 호: 용어(와 그 정의 안에서 선언한 약칭) → 그 호 명제
    def_props, def_kind = {}, {}
    for p in props:
        if p["kind"] in ("ITEM", "SINGLE", "MAIN") and p["attrs"]["modality"] == "DEFINITION":
            for term in definition_terms(p["text"]):
                def_props.setdefault((corpus.by_id[p["unit"]].doc_id, term), p["id"])
                def_kind.setdefault((corpus.by_id[p["unit"]].doc_id, term), "DEFINITION")
    # G11: 정의 조항 밖의 약칭 선언 '(이하 "X"라 한다)'도 같은 조례 안의 정의로 쓴다(외부 법령·주체 약칭 제외).
    for p in props:
        if p["kind"] in ("DELETED",) or p["attrs"]["modality"] == "DEFINITION":
            continue
        for a in _BODY_ALIAS.findall(_TAGS.sub("", p["text"])):
            a = a.strip()
            if len(a) >= 2 and a not in _ALIAS_SKIP:
                key = (corpus.by_id[p["unit"]].doc_id, a)
                if key not in def_props:
                    def_props[key], def_kind[key] = p["id"], "ALIAS"
    # F22: 용어 빈도는 명제 본문에서 센다(원문 단위에 반복 삽입된 조례명·조 제목 제외).
    body_text = defaultdict(str)
    for p in props:
        body_text[p["unit"]] += "\n" + _TAGS.sub("", p["text"])
    unit_count = defaultdict(int)
    for u in corpus.units:
        unit_count[u.doc_id] += 1
    term_units = defaultdict(set)
    for (doc, term) in def_props:
        for u in corpus.units:
            if u.doc_id == doc and term in body_text[u.id]:
                term_units[(doc, term)].add(u.id)
    by_pid = {q["id"]: q for q in props}
    unit_order = {u.id: u.order for u in corpus.units}

    def scope_definition(pid):  # G13: 연령·기간·금액을 정하는 짧은 정의는 흔한 용어라도 연결
        q = by_pid[pid]
        return len(_clean(q["text"])) <= 120 and bool(q["attrs"]["values"])

    edges, limits = [], []

    def add(src, dst, role, basis, dst_kind="unit", required=True):
        edges.append({"from": src, "to": dst, "to_kind": dst_kind, "role": role, "basis": basis[:120],
                      "required": required, "review": "AI_RULE"})
    for p in props:
        u = corpus.by_id[p["unit"]]
        if p["kind"] == "ITEM":
            lead = next((q for q in by_unit[u.id] if q["kind"] == "LEAD"), None)
            if lead:
                add(p["id"], lead["id"], "COMMON", "각 호의 공통 효과", "prop")
        if p["kind"] == "MAIN":
            prov = next((q for q in by_unit[u.id] if q["id"] == p["id"] + "단서"), None)
            if prov:
                add(p["id"], prov["id"], "EXCEPTION", "다만,", "prop")
        if p["kind"] in ("ITEM", "LEAD") and p["id"] + "단서" in by_pid:  # G05: 호·도입문 안의 단서
            add(p["id"], p["id"] + "단서", "EXCEPTION", "다만,", "prop")
        if u.kind == "BODY":
            rt = ref_text(p["text"])
            for t, basis in graph.find_refs(rt, u.doc_id, u.article):
                if t != u.id:
                    role = "TABLE" if corpus.by_id[t].kind != "BODY" else "REFERENCE"
                    add(p["id"], t, role, basis)
            # F16: 같은 조례 안의 '제A항부터 제B항까지'는 중간 항도 참조한다(양 끝은 위에서 연결됨).
            for m in _PARA_RANGE.finditer(rt):
                if _EXT_BEFORE.search(rt[max(0, m.start() - 12):m.start()]):
                    continue
                art = m.group(1) or u.article
                for k in range(int(m.group(2)) + 1, int(m.group(3))):
                    for t in graph._resolve_article(u.doc_id, art, k):
                        if t != u.id:
                            add(p["id"], t, "REFERENCE", m.group(0))
            # F17: 다른 항이 '제N항에도 불구하고'로 이 원칙을 제한하면, 원칙 → 제한 항으로 읽기 간선을 둔다.
            for m in _NOTWITH.finditer(rt):
                if _EXT_BEFORE.search(rt[max(0, m.start() - 12):m.start()]):
                    continue
                art = m.group(1) or u.article
                for para in [m.group(2), m.group(3)]:
                    if not para:
                        continue
                    for t in graph._resolve_article(u.doc_id, art, int(para)):
                        if t != u.id:
                            limits.append((t, u.id, rt[m.start():m.end() + 80]))
            # F26: '제N조의 규정에도 불구하고'는 그 조의 모든 항을 제한한다.
            for m in _NOTWITH_ART.finditer(rt):
                if _EXT_BEFORE.search(rt[max(0, m.start() - 12):m.start()]) or "항" in rt[m.end(1):m.end(1) + 3]:
                    continue
                for t in graph._resolve_article(u.doc_id, m.group(1), None):
                    if t != u.id:
                        limits.append((t, u.id, rt[m.start():m.end() + 80]))
            ext_refs = [{"law": a, "alias": b or "", "article": c} for a, b, c in _EXT_REF.findall(_TAGS.sub("", p["text"]))]
            if ext_refs:
                p["external_refs"] = ext_refs  # F13: 외부 법령 조문은 내부 조례로 잇지 않고 선언 원문으로 보존
        doc = u.doc_id
        for (d, term), pid in def_props.items():
            if d != doc or pid == p["id"] or term not in p["text"]:
                continue
            if len(term_units[(d, term)]) > 0.25 * unit_count[d] and not scope_definition(pid):
                continue  # 조례 전반에 흔한 용어는 정의를 매번 붙이지 않음(값을 정하는 짧은 정의는 예외)
            if p["kind"] == "DELETED" or by_pid[pid]["unit"] == p["unit"]:
                continue
            if def_kind.get((d, term)) == "ALIAS":
                # F30: 본문 약칭은 선언 이후('이하')에만, 더 긴 낱말의 일부가 아닐 때만 연결한다.
                decl = by_pid[pid]
                if unit_order[p["unit"]] < unit_order[decl["unit"]]:
                    continue
                txt = _TAGS.sub("", p["text"])
                if not any(i == 0 or not re.match(r"[가-힣]", txt[i - 1]) for i in
                           [m.start() for m in re.finditer(re.escape(term), txt)]):
                    continue
            # 정규식으로 찾은 용어 연결은 읽기 문맥(전달)용이다. 인용 필수 관계로 쓰지 않는다(F04).
            add(p["id"], pid, "DEFINITION", term, "prop", required=False)
    for principle_unit, limiting_unit, basis in limits:
        # 읽기용(전달) 간선. 인용 닫힘의 역할 목록에는 넣지 않는다(최종 주장에 쓴 경우에만 인용).
        for q in by_unit[principle_unit]:
            add(q["id"], limiting_unit, "LIMITED_BY", basis)
    for src, dst, role, basis in _REVIEWED_EXTRA:
        if src in {q["id"] for q in props} and dst in {q["id"] for q in props}:
            add(src, dst, role, basis, "prop")
            edges[-1]["review"] = "AI_REVIEWED"
    for e in corpus.edges:  # 검토된 corpus 간선(원문 단위)
        if e.get("enabled") and e.get("required") and e.get("review") == "REVIEWED" \
                and e["from_id"] in corpus.by_id and e["to_id"] in corpus.by_id and e["from_id"] != e["to_id"]:
            for p in by_unit[e["from_id"]]:
                b = (e.get("basis") or [{}])[0]
                add(p["id"], e["to_id"], "REVIEWED:" + e["role"], b.get("quote") or b.get("text") or "")
    # 중복 간선 제거
    seen, uniq = set(), []
    for e in edges:
        k = (e["from"], e["to"], e["role"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return {"schema": "PKG-v2/1", "corpus_sha256": corpus_sha(corpus), "propositions": props, "edges": uniq}


# F25(PKG8): '제N항에 따른 X'로 효과의 적용 대상을 좁힌 조정·감면·준용 규정. 전달·카드에 적용 대상을 표시한다.
_APPLIES_TO = re.compile(r"(제\d+조(?:의\d+)?)?\s*제(\d+)항(?:제\d+호)?에\s*따른\s*([가-힣ㆍ·\s]{1,16}?)(?:의|을|를|은|는|이|가|에)\s")
_EFFECT_ADJUST = re.compile(r"감경|가중|줄이거나|늘릴|면제|감면|준용|산정|부과|할인")
_BODY_ALIAS = re.compile(r"\(이하\s*[\"“'‘]([^\"”'’]{1,20})[\"”'’]\s*(?:이)?라\s*한다\)")
_ALIAS_SKIP = {"법", "영", "시행령", "시행규칙", "법 시행령", "법 시행규칙", "규칙", "시", "시장", "구청장", "위원회",
               "위원장", "조례"}
_HISTORY = re.compile(r"\[[^\[\]]*(?:이동|종전|부칙)[^\[\]]*\]")  # F20: 부칙 조문 번호는 본칙 참조가 아니다
_ALIAS_DECL = re.compile(r"\s*\(이하\s*[\"“'‘][^\"”'’]{1,20}[\"”'’]\s*(?:이)?라\s*한다\)")
_EXT_BEFORE = re.compile(r"(?:」|(?<![가-힣])(?:법|영|시행령|시행규칙|규칙))\s*$")
_PARA_RANGE = re.compile(r"(?:(제\d+조(?:의\d+)?)\s*)?제(\d+)항부터\s*제(\d+)항까지")
_NOTWITH = re.compile(r"(?:(제\d+조(?:의\d+)?)\s*)?제(\d+)항(?:\s*(?:및|ㆍ|,|또는)\s*제(\d+)항)?(?:\s*본문)?에도\s*불구하고")
_NOTWITH_ART = re.compile(r"(제\d+조(?:의\d+)?)(?:의\s*규정|의\s*본문)?에도\s*불구하고")  # F26: 조 전체를 제한
_VALID_UNTIL = re.compile(r"이\s*조\s*제(\d+)항(?:제(\d+)호)?(?:은|는)\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일까지\s*유효")
_EXT_REF = re.compile(r"「([^」]+)」(?:\s*\(이하\s*[\"“'‘]([^\"”'’]{1,20})[\"”'’]\s*(?:이)?라\s*한다\))?\s*"
                      r"(제\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호(?:의\d+)?)?)")
_CELL_LAWREF = re.compile(r"「[^」]+」|(?<![가-힣])(?:법|영|시행령|시행규칙)\s*제\d+조|조례\s*제\d+조|제\d+조(?:의\d+)?")
# 원문 대조로 확인한 계산 규칙(F17). (원칙 명제, 적용 규칙 명제, 역할, 근거 원문)
_REVIEWED_EXTRA = [
    ("주차장:제7조제1항", "주차장:제7조제2항", "CALCULATION",
     "주차요금의 할인 등에 따라 100원 미만의 단수가 발생할 경우 그 단수는 계산하지 아니한다"),
]


def ref_text(text: str) -> str:
    """참조 추출용 본문. 명제 원문(text)은 그대로 두고, 여기서만 다음을 뺀다.
    - 태그(<개정 …>), 이동·종전 이력([제26조에서 이동 …]): 과거 조문 번호는 현행 참조가 아니다(F14).
    - 약칭 선언((이하 "시행규칙"이라 한다)): 법령명과 조문 사이에 끼어 외부 법령 판정을 막는다(F13)."""
    t = _TAGS.sub("", text)
    t = _HISTORY.sub(" ", t)
    return _ALIAS_DECL.sub("", t)


_DEF_TERM = re.compile(r"[\"“'‘]((?:[^\"“”'‘’\n()]|\((?:[^()\n])*\)){2,60}?)[\"”'’]\s*(?:(?:이)?란|(?:이)?라\s*함은)")


def definition_terms(text: str) -> list[str]:
    """정의 문장의 정의 대상어. 괄호 안 약칭 선언("…(이하 "유기동물"이라 한다)"이란)은 따옴표가
    중첩되므로 괄호를 한 덩어리로 읽고, 본래 용어와 약칭을 모두 돌려준다."""
    out = []
    for full in _DEF_TERM.findall(text):
        main = re.sub(r"\(.*\)\s*$", "", full).strip()
        if main:
            out.append(main)
        out += [a.strip() for a in re.findall(r"이하\s*[\"“'‘]([^\"”'’]{1,30})[\"”'’]", full)]
    return list(dict.fromkeys(out))


def corpus_sha(corpus: Corpus) -> str:
    return hashlib.sha256("".join(u.raw_text for u in corpus.units).encode()).hexdigest()


EMBED_MODEL = "solar-embedding-2-passage"
EMBED_MAX_CHARS = 1800


def view_sha(view: str) -> str:
    """임베딩 실제 입력(한도까지 자른 검색 표현)의 해시."""
    return hashlib.sha256(view[:EMBED_MAX_CHARS].encode()).hexdigest()


def search_view(p: dict) -> str:
    """검색·임베딩용 표현: 조례·조 제목 + (도입문) + 명제 원문 (+표 머리글). 개정 표시는 제외."""
    head = f"{p['label']} {p['title']}".strip()
    if p["kind"] == "ITEM":
        return _clean(f"{head} {p.get('lead_text', '')} {p['text']}")
    if p["kind"] == "ROW":
        return _clean(f"{head} {p.get('header', '')} {p.get('resolved') or p['text']}")
    return _clean(f"{head} {p['text']}")


def save(graph: dict, path: Path):
    Path(path).write_text(json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
