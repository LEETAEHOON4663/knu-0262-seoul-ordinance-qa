"""PKG-v2 실행 자산: 명제 색인(BM25 + dense), 근거 카드, 필수 근거 복원, 전달 원문 구성.

전달 원칙(v2 §3·§4)
  - 선택 명제의 원문 단위(항 전체 / 표 영역 전체)를 전달한다.
  - 전달된 원문 안의 모든 명제의 필수 간선을 전이적으로 따라가 원문을 추가한다(방문 ID로 순환 종료).
  - 전달 원문에 들어 있는 모든 명제 ID를 인용 가능한 ID로 노출한다(보여 준 원문 = 쓸 수 있는 ID).
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .corpus import Corpus
from .search import BM25
from . import pkg_graph as P

_TAGS = re.compile(r"<[^>]{0,150}>")


CARD_TEXT, CARD_HEADER, CARD_PROVISO = 900, 500, 400
CUT_MARK = " …(이하 생략: 이 카드를 고르면 전체 원문이 전달됨)"


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub("", t)).strip()


def _cut(t: str, n: int) -> str:
    """카드 길이 제한. 자를 때는 잘렸다는 표시를 남긴다(F03: 조용히 자르지 않음)."""
    return t if len(t) <= n else t[:n] + CUT_MARK


@dataclass
class Delivery:
    units: list[str]                      # 전달 원문 단위 id(순서 = 선택 순 → 닫힘 순)
    prop_ids: list[str]                   # 인용 가능한 명제 id(전달 원문 안 전부)
    reasons: dict = field(default_factory=dict)  # unit → 포함 이유


def outside_reference(attrs: dict) -> str:
    """그래프 속성상 내용을 외부 법령에 맡긴 명제면 원문 구간을 담은 설명, 아니면 "".
    목록·정의 참조와 외부 법령 위임만 본다(시행규칙·시장 결정·다른 조례 위임은 조례가 범위·권한을 직접 주는 경우가 많음)."""
    out = []
    ext = attrs.get("external") or {}
    if ext.get("what") == "LIST":
        out.append(f"외부 법령 목록 참조: «{ext.get('span') or ext.get('text', '')}»")
    elif ext.get("what") == "DEFINITION":
        out.append(f"외부 법령 정의 참조: «{ext.get('span') or ext.get('text', '')}»")
    d = attrs.get("delegation") or {}
    if d.get("to") == "EXTERNAL_LAW":
        out.append(f"외부 법령에 맡김: «{d.get('text', '')}»")
    return " / ".join(out)


class PropIndex:
    def __init__(self, corpus: Corpus, data_dir: Path, use_dense: bool = True):
        self.corpus = corpus
        g = P.load(Path(data_dir) / "pkg" / "graph.json")
        self.props = g["propositions"]
        self.by_id = {p["id"]: p for p in self.props}
        self.by_unit = defaultdict(list)
        for p in self.props:
            self.by_unit[p["unit"]].append(p)
        self.out = defaultdict(list)  # prop → edges
        for e in g["edges"]:
            self.out[e["from"]].append(e)
        self.views = [P.search_view(p) for p in self.props]
        self._searchable = [p["kind"] != "DELETED" for p in self.props]  # G03: 삭제 항은 후보에서 뺀다
        self.bm25 = BM25(self.views)
        self.vecs = None
        vpath = Path(data_dir) / "pkg" / "vectors.json"
        if use_dense and vpath.exists():
            v = json.loads(vpath.read_text(encoding="utf-8"))
            # F08: id뿐 아니라 실제 임베딩 입력(검색 표현)과 모델이 같은지 대조한다.
            if v["ids"] != [p["id"] for p in self.props]:
                raise ValueError("pkg_vectors_stale:ids")
            if v.get("model") != P.EMBED_MODEL or v.get("max_chars") != P.EMBED_MAX_CHARS:
                raise ValueError("pkg_vectors_stale:model")
            if v.get("input_sha256") != [P.view_sha(view) for view in self.views]:
                raise ValueError("pkg_vectors_stale:inputs")
            self.vecs = [self._unit(x) for x in v["vectors"]]

    @staticmethod
    def _unit(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    # ---------------------------------------------------------------- 검색
    def _ranked(self, scores, depth):
        idx = [i for i, s in enumerate(scores) if s > 0 and self._searchable[i]]
        idx.sort(key=lambda i: (-scores[i], i))
        return idx[:depth]

    def rank(self, question: str, queries=(), qvec=None, qvecs=(), depth: int = 60, rrf_k: int = 60):
        lists = [(1.0, self._ranked(self.bm25.scores(question), depth))]
        if self.vecs is not None and qvec is not None:
            q = self._unit(qvec)
            lists.append((1.0, self._ranked([sum(a * b for a, b in zip(q, r)) for r in self.vecs], depth)))
        for i, text in enumerate(queries):
            lists.append((0.6, self._ranked(self.bm25.scores(text), depth)))
            if self.vecs is not None and i < len(qvecs) and qvecs[i] is not None:
                q = self._unit(qvecs[i])
                lists.append((0.6, self._ranked([sum(a * b for a, b in zip(q, r)) for r in self.vecs], depth)))
        fused = defaultdict(float)
        for w, ranked in lists:
            for r, i in enumerate(ranked):
                fused[i] += w / (rrf_k + r + 1)
        order = sorted(fused, key=lambda i: (-fused[i], i))[:depth]
        return [(self.props[i]["id"], round(fused[i], 6)) for i in order]

    # ---------------------------------------------------------------- 카드
    def card(self, pid: str) -> dict:
        p = self.by_id[pid]
        a = p["attrs"]
        bits = []
        if a["subject"]:
            bits.append(f"주체:{a['subject']}")
        bits.append(f"양태:{a['modality']}")
        if a["values"]:
            bits.append(f"값({a['value_status']}):{', '.join(a['values'][:6])}")
        elif a["value_status"] == "DELEGATED":
            bits.append("값:위임")
        if a["delegation"]["to"] != "NONE":
            bits.append(f"위임:{a['delegation']['to']}")
        # PKG11: 속성을 원문보다 강한 의미로 번역하지 않는다(GPT 검토 1.1·1.2). 원문 참조 구간·지정 관련 구절만 인용한다.
        if a["external"]:
            bits.append(f"외부 법령 참조: «{a['external'].get('span') or a['external']['text']}»")
        if a["designation_required"]:
            bits.append(f"지정 관련 구절: «{a.get('designation_phrase', '')}»")
        if a.get("expired"):
            bits.append(f"효력만료({a['valid_until']}까지 유효, 부칙)")
        if a.get("applies_to"):
            bits.append("적용 대상:" + "; ".join(a["applies_to"]))
        c = {"id": pid, "text": _cut(_clean(p.get("resolved") or p["text"]), CARD_TEXT)}
        if p["kind"] == "ITEM":
            lead = next((q for q in self.by_unit[p["unit"]] if q["kind"] == "LEAD"), None)
            if lead:
                c["lead"] = lead["id"]
        if p["kind"] in ("MAIN", "ITEM", "LEAD"):
            # 분리된 단서는 본문 효과를 뒤집을 수 있으므로 본문 카드에서 바로 보이게 한다(F09, G05: 호·도입문 포함).
            prov = self.by_id.get(pid + "단서")
            if prov:
                c["proviso"] = {"id": prov["id"], "text": _cut(_clean(prov["text"]), CARD_PROVISO)}
        if p["kind"] == "ROW":
            c["header"] = _cut(_clean(p.get("header", "")), CARD_HEADER)
        c["attrs"] = " | ".join(bits)
        if p.get("extraction_status"):
            c["status"] = p["extraction_status"]
        return c

    def candidate_cards(self, ranked: list[tuple[str, float]]) -> list[dict]:
        """순위 순서 카드. 호 카드의 도입문 카드는 한 번만 함께 넣는다."""
        cards, seen = [], set()
        for pid, _ in ranked:
            p = self.by_id[pid]
            if p["kind"] == "ITEM":
                lead = next((q for q in self.by_unit[p["unit"]] if q["kind"] == "LEAD"), None)
                if lead and lead["id"] not in seen:
                    seen.add(lead["id"])
                    cards.append(self.card(lead["id"]))
            if pid not in seen:
                seen.add(pid)
                cards.append(self.card(pid))
        return cards

    # ---------------------------------------------------------------- 복원·전달
    def deliver(self, selected: list[str]) -> Delivery:
        units, reasons = [], {}

        def add_unit(uid, why):
            if uid not in reasons:
                reasons[uid] = why
                units.append(uid)
                return True
            return False
        queue = []
        for pid in selected:
            u = self.by_id[pid]["unit"]
            if add_unit(u, f"selected:{pid}"):
                queue.append(u)
        while queue:
            u = queue.pop(0)
            for p in self.by_unit[u]:
                for e in self.out[p["id"]]:
                    tu = self.by_id[e["to"]]["unit"] if e["to_kind"] == "prop" else e["to"]
                    if add_unit(tu, f"required:{e['role']}:{p['id']}"):
                        queue.append(tu)
        pids = [p["id"] for u in units for p in self.by_unit[u]]
        return Delivery(units, pids, reasons)

    def render(self, d: Delivery, external_list_note: bool = False, designation_note: bool = False,
               definition_note: bool = False, junyong_note: bool = False, outside_note: bool = False) -> list[dict]:
        """생성·검토에 주는 원문: 원문 단위별로 명제 id가 붙은 조각 + 그 단위의 필수 연결.
        external_list_note(PKG11 후보 A): 외부 법령 목록을 참조하는 조각에 참조 구간을 메타데이터 note로 붙인다."""
        out = []
        for u in d.units:
            unit = self.corpus.by_id[u]
            parts = []
            for p in self.by_unit[u]:
                if p["kind"] == "ROW":  # 머리글(표 제목·단위 포함)과 함께, '〃'는 풀어서
                    part = {"id": p["id"], "text": _clean(p.get("header", "") + " → " + (p.get("resolved") or p["text"]))}
                else:
                    part = {"id": p["id"], "text": _clean(p["text"])}
                notes = []  # PKG11: note를 덮어쓰지 않고 누적한다
                if p["attrs"].get("expired"):
                    notes.append(f"효력 만료: {p['attrs']['valid_until']}까지 유효(부칙)")
                if p["attrs"].get("applies_to"):  # F25: 이 규정의 효과는 여기 적힌 대상에만 적용
                    notes.append("적용 대상: " + "; ".join(p["attrs"]["applies_to"]) + " (그 밖의 대상에는 적용하지 않음)")
                if p.get("span_basis") == "supplement":
                    notes.append("동결 원문에 없는 표 밖 단위 문구를 원본에서 보충: " + p.get("supplement_source", ""))
                if p.get("extraction_status"):  # PKG11: 카드의 미검토 상태(그림 등)를 생성·검토 원문에도 보존
                    part["status"] = p["extraction_status"]
                ext = p["attrs"].get("external") or {}
                if external_list_note and ext.get("what") == "LIST" and ext.get("span"):
                    notes.append(f"외부 법령 참조: «{ext['span']}» (이 조각은 그 목록을 직접 열거하지 않음)")
                # PKG11 후보 시험용(기본 끔): 원문 구절 인용만 하고 의미를 해석하지 않는다.
                if definition_note and ext.get("what") == "DEFINITION" and ext.get("span"):
                    notes.append(f"외부 법령 참조: «{ext['span']}»")
                if designation_note and p["attrs"].get("designation_required") and p["attrs"].get("designation_phrase"):
                    notes.append(f"지정 관련 구절: «{p['attrs']['designation_phrase']}»")
                if junyong_note and p["attrs"].get("junyong"):
                    notes.append(f"준용: «{p['attrs']['junyong']}»")
                # W3(PKG11.1 점검): 외부 법령에 내용을 맡긴 조각임을 생성·검토·교정에도 알린다(기본 끔).
                if outside_note and outside_reference(p["attrs"]):
                    notes.append(outside_reference(p["attrs"]) + " (맡겨진 내용 자체는 이 자료에 없음)")
                if notes:
                    part["note"] = " / ".join(notes)
                parts.append(part)
            # G02: 행·비고로 나뉜 표는 조각만 준다. 전체 원문은 행 분할이 없는 단위(그림 등)에만 넣는다.
            if unit.kind != "BODY" and not any(p["kind"] in ("ROW", "NOTE") for p in self.by_unit[u]):
                whole = {"id": None, "text": _clean(unit.text)[:6000], "note": "별표 전체"}
                status = next((p["extraction_status"] for p in self.by_unit[u] if p.get("extraction_status")), None)
                if status:
                    whole["status"] = status
                parts.insert(0, whole)
            links = []
            for p in self.by_unit[u]:
                for e in self.out[p["id"]]:
                    if e["role"] in ("COMMON",):
                        continue
                    tgt = e["to"] if e["to_kind"] == "prop" else self.corpus.by_id[e["to"]].label
                    links.append({"from": p["id"], "role": e["role"], "to": tgt})
            entry = {"unit": unit.label, "parts": parts, "why": d.reasons[u]}
            if links:
                entry["links"] = links[:12]
            out.append(entry)
        return out

    def citation_closure(self, used: list[str]) -> list[str]:
        """주장에 쓴 명제 + 그 명제가 필수로 기대는 참조·예외·표 원문의 명제 단위(인용용, 1단계)."""
        units = []
        for pid in used:
            p = self.by_id[pid]
            if p["unit"] not in units:
                units.append(p["unit"])
            for e in self.out[pid]:
                if e["role"] in ("REFERENCE", "EXCEPTION", "TABLE") or e["role"].startswith("REVIEWED:"):
                    tu = self.by_id[e["to"]]["unit"] if e["to_kind"] == "prop" else e["to"]
                    if tu not in units:
                        units.append(tu)
        return units
