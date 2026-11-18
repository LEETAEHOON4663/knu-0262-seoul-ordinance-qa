"""필수근거 그래프: 후보 원문을 해석하는 데 필요한 원문을 1단계 복원한다.

간선 출처 세 가지(모두 '이 원문을 읽으려면 저 원문이 필요하다'는 방향):
  1. corpus.json의 검토 표시 필수 간선(REFERENCE/LEGAL_BASIS/DEFINITION 등).
  2. 같은 조례 안의 명시적 참조 문자열(제N조, 제N조제M항, 같은 조 제M항,
     제M항, 별표 N, 별도 N). 외부 법령(「…」, 법, 영, 규칙 뒤)의 조문은 따르지 않는다.
  3. 정의 조항: 조 제목이 '정의'인 원문의 "용어"란 정의를, 그 용어가 쓰인 원문에 붙인다.

간선은 법률 추론 규칙이 아니라 '함께 읽어야 할 원문' 표시다. 깊이는 1단계로 제한한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .corpus import Corpus, Unit

_ART = r"제(\d+)조(?:의(\d+))?"
_REF = re.compile(
    r"(?P<art>제\d+조(?:의\d+)?)(?:\s*제(?P<para>\d+)항)?"
    r"|같은\s*조\s*제(?P<same_para>\d+)항"
    r"|(?<![조\d])제(?P<lone_para>\d+)항"
    r"|(?P<annex>별표|별도)\s*(?P<annex_no>\d+)?"
)
# 직전 문맥이 외부 법령을 가리키면 그 조문 참조는 따르지 않는다.
_EXTERNAL_TAIL = re.compile(r"(」|법|영|규칙|규정|시행령|시행규칙)\s*$")
_CHAIN_GAP = re.compile(r"[\s,ㆍ·]*(?:및|또는|부터|까지|ㆍ|·|,|와|과|에서)?[\s,ㆍ·]*")
_HEADER = re.compile(r"^\[[^\]]*\]\n?")
_DEF_TERM = re.compile(r"[\"“'‘]([^\"”'’\n]{2,30})[\"”'’]\s*(?:이)?란")


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str  # REVIEWED:<role> | REF | DEFINITION
    basis: str


def _body(text: str) -> str:
    return _HEADER.sub("", text, count=1)


class EvidenceGraph:
    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self.out: dict[str, list[Edge]] = {}
        self._index = {}
        for u in corpus.units:
            if u.kind == "BODY":
                self._index[(u.doc_id, u.article, u.para)] = u.id
            else:
                key = re.sub(r"\s+", "", u.label.split(u.law, 1)[1])  # 별표1 / 별도3 / 별표
                self._index[(u.doc_id, key, None)] = u.id
        self._add_reviewed()
        for u in corpus.units:
            self._add_text_refs(u)
        self._add_definitions()

    # ------------------------------------------------------------ build
    def _add(self, src, dst, kind, basis):
        if src == dst or dst not in self.corpus.by_id:
            return
        lst = self.out.setdefault(src, [])
        if all(e.dst != dst for e in lst):
            lst.append(Edge(src, dst, kind, basis[:120]))

    def _add_reviewed(self):
        for e in self.corpus.edges:
            if e.get("enabled") and e.get("required") and e.get("review") == "REVIEWED":
                basis = ""
                if e.get("basis"):
                    b = e["basis"][0]
                    basis = b.get("quote") or b.get("text") or ""
                self._add(e["from_id"], e["to_id"], "REVIEWED:" + e["role"], basis)

    def _resolve_article(self, doc, art, para):
        if para is not None:
            uid = self._index.get((doc, art, para))
            return [uid] if uid else []
        uid = self._index.get((doc, art, None))
        if uid:
            return [uid]
        return [u.id for u in self.corpus.groups.get(f"{doc}:{art}", [])]

    def find_refs(self, text: str, doc_id: str, article: str | None = None,
                  whole_article: bool = True) -> list[tuple[str, str]]:
        """같은 조례 안의 명시적 참조 → [(unit id, 매칭 문자열)]. 외부 법령 조문은 제외.

        article: 지금 원문의 조(같은 조 제M항·제M항 해석용).
        whole_article: '제N조'처럼 항 없이 가리키면 그 조의 모든 항을 돌려줄지 여부.
        """
        out = []
        prev_end, prev_external = -1, False
        for m in _REF.finditer(text):
            before = text[max(0, m.start() - 12):m.start()]
            gap = text[prev_end:m.start()] if prev_end >= 0 else None
            # "법 제7조제1항 및 제12조제1항"처럼 접속어로 이어진 참조는 앞 참조의 법령을 따른다.
            chained = gap is not None and _CHAIN_GAP.fullmatch(gap) is not None
            external = (prev_external if chained else False) or \
                bool(_EXTERNAL_TAIL.search(before)) or "같은 법" in before
            prev_end, prev_external = m.end(), external
            if external:
                continue
            if m.group("art"):
                para = int(m.group("para")) if m.group("para") else None
                if m.group("art") == article and para is None:
                    continue
                if para is None and not whole_article:
                    uid = self._index.get((doc_id, m.group("art"), None))
                    targets = [uid] if uid else []
                else:
                    targets = self._resolve_article(doc_id, m.group("art"), para)
            elif m.group("same_para") or m.group("lone_para"):
                if article is None:
                    continue
                para = int(m.group("same_para") or m.group("lone_para"))
                targets = self._resolve_article(doc_id, article, para)
            else:
                key = m.group("annex") + (m.group("annex_no") or "")
                targets = [self._index[(doc_id, key, None)]] if (doc_id, key, None) in self._index else []
            out.extend((t, m.group(0)) for t in targets)
        return out

    def _add_text_refs(self, u: Unit):
        if u.kind == "DIAGRAM":
            return
        for t, basis in self.find_refs(_body(u.raw_text), u.doc_id,
                                       u.article if u.kind == "BODY" else None):
            self._add(u.id, t, "REF", basis)

    def _add_definitions(self):
        self.definitions: dict[str, list[tuple[str, str]]] = {}
        for u in self.corpus.units:
            if u.kind == "BODY" and u.title == "정의":
                for term in _DEF_TERM.findall(_body(u.raw_text)):
                    term = term.strip()
                    if len(term) >= 2:
                        self.definitions.setdefault(u.doc_id, []).append((term, u.id))
        # 조례 전체에 흔한 용어(예: '동물')는 모든 원문에 정의 조항을 붙여 문맥만
        # 키우므로 제외한다: 해당 조례 원문 25% 초과에 나오는 용어는 따르지 않는다.
        for doc, items in self.definitions.items():
            units = [u for u in self.corpus.units if u.doc_id == doc]
            for term, def_id in items:
                users = [u for u in units if u.id != def_id and term in _body(u.raw_text)]
                if len(users) > 0.25 * len(units):
                    continue
                for u in users:
                    self._add(u.id, def_id, "DEFINITION", term)

    # ------------------------------------------------------------ use
    def required(self, unit_id: str) -> list[Edge]:
        return list(self.out.get(unit_id, []))
