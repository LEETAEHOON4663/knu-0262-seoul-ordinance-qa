"""① 문서 준비·구조화.

동결 자료(data/corpus.json의 원문 525단위 + data/ordinances.json 메타데이터)를
불변 Unit 목록으로 읽는다. 라벨은 그대로 제출 인용 문자열이 된다.

- 본문: corpus 원문 텍스트를 그대로 쓴다(조 머리말 포함).
- 별표: corpus의 HWP 셀 선형화 텍스트를 다시 파싱해, 행·열 병합을 복원한
  격자 표로 렌더링한다. 셀 문자열은 하나도 버리지 않는다(테스트로 확인).
- 그림 서식(별도): 남아 있는 부분 텍스트만 전달하고 flags로 표시한다.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CORPUS_SHA256 = "66904d7be02ba9d69cc8c58354fad2b29875cd65524373ecb95e7be072945e8f"
ORDINANCES_SHA256 = "17902a62fe52562092d47db975a78c472462f03a6cae22d6fe589feb7c3e39cb"


class CorpusError(RuntimeError):
    pass


@dataclass(frozen=True)
class Unit:
    id: str
    order: int
    law: str
    doc_id: str
    label: str
    kind: str  # BODY | TABLE | DIAGRAM
    group: str  # 조 또는 별표 단위 묶음 키
    article: str | None
    para: int | None
    title: str
    text: str  # 모델에 전달하는 표현
    raw_text: str  # 동결 원문
    source_url: str
    effective_date: str
    flags: tuple = field(default_factory=tuple)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- 표 렌더링
_TABLE_START = re.compile(r"^\[표: (\d+)행 × (\d+)열[^\]]*\]$")
_CELL = re.compile(r"^\[셀 행(\d+) 열(\d+); 행병합(\d+) 열병합(\d+)\]$")
_TABLE_END = "[표 끝]"


def _clean(text: str) -> str:
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in text.split("\n")]
    return " / ".join(ln for ln in lines if ln)


def render_linearized_tables(raw: str) -> tuple[str, list[str]]:
    """HWP 선형화 텍스트 → 사람이 읽는 표. (렌더링, 원래 셀 문자열 목록)."""
    out: list[str] = []
    cells_seen: list[str] = []
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        m = _TABLE_START.match(lines[i].strip())
        if not m:
            if lines[i].strip():
                out.append(lines[i].rstrip())
            i += 1
            continue
        rows, cols = int(m.group(1)), int(m.group(2))
        i += 1
        cells = []  # (r, c, rs, cs, text)
        cur = None
        buf: list[str] = []
        while i < len(lines) and lines[i].strip() != _TABLE_END:
            cm = _CELL.match(lines[i].strip())
            if cm:
                if cur is not None:
                    cells.append((*cur, "\n".join(buf)))
                cur = tuple(int(x) for x in cm.groups())
                buf = []
            elif cur is not None:
                buf.append(lines[i])
            elif lines[i].strip():
                out.append(lines[i].rstrip())
            i += 1
        if cur is not None:
            cells.append((*cur, "\n".join(buf)))
        i += 1  # [표 끝]
        out.append(f"[표 {rows}행×{cols}열]")
        grid: dict[tuple[int, int], str] = {}
        full_rows: dict[int, list[str]] = {}
        for r, c, rs, cs, text in cells:
            t = _clean(text)
            cells_seen.append(t)
            if cs >= cols and c == 1:
                full_rows.setdefault(r, []).append(t)
                continue
            for rr in range(r, r + rs):
                for cc in range(c, c + cs):
                    grid[(rr, cc)] = t
        for r in range(1, rows + 1):
            if r in full_rows:
                for t in full_rows[r]:
                    if t:
                        out.append(t)
                continue
            vals = [grid.get((r, c), "") for c in range(1, cols + 1)]
            if any(vals):
                out.append("| " + " | ".join(vals) + " |")
        out.append("[표 끝]")
    return "\n".join(out), cells_seen


# ---------------------------------------------------------------- 로딩
def _load_json(path: Path, expected_sha: str | None):
    if expected_sha is not None and sha256_file(path) != expected_sha:
        raise CorpusError(f"frozen_asset_hash_mismatch:{path.name}")
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Corpus:
    def __init__(self, data_dir: Path | str = DATA_DIR, verify_hash: bool = True):
        data_dir = Path(data_dir)
        raw = _load_json(data_dir / "corpus.json", CORPUS_SHA256 if verify_hash else None)
        ords = _load_json(data_dir / "ordinances.json", ORDINANCES_SHA256 if verify_hash else None)
        self.laws = {o["mst"]: {"name": o["name"], "source_url": o["source_url"],
                                "effective_date": o["effective_date"]} for o in ords}
        self.ordinances = ords
        units: list[Unit] = []
        for order, s in enumerate(raw["sources"]):
            meta = self.laws.get(s["doc_id"])
            if meta is None or meta["name"] != s["law"]:
                raise CorpusError("law_metadata_mismatch:" + s["id"])
            loc = s["location"]
            if s["kind"] == "BODY":
                kind, article, para = "BODY", loc["article"], loc["para"]
                group = f"{s['doc_id']}:{article}"
                text, title = s["text"], ""
                m = re.match(r"^\[[^\]]*제\d+조(?:의\d+)?\(([^)]*)\)\]", text) or \
                    re.match(r"^제\d+조(?:의\d+)?\(([^)]*)\)", text)
                if m:
                    title = m.group(1)
            else:
                kind = "TABLE" if s["kind"] == "TABLE_REGION" else "DIAGRAM"
                article, para = None, None
                group = f"{s['doc_id']}:{loc['annex']}"
                title = loc.get("title", "")
                text = render_linearized_tables(s["text"])[0] if kind == "TABLE" else s["text"]
            flags = tuple(s.get("flags", []))
            if kind == "DIAGRAM":
                flags = flags + ("diagram_original_unavailable",)
            units.append(Unit(
                id=s["id"], order=order, law=s["law"], doc_id=s["doc_id"], label=s["label"],
                kind=kind, group=group, article=article, para=para, title=title,
                text=text, raw_text=s["text"],
                # URL·시행일은 고정 조례 메타데이터(ordinances.json)를 정본으로 쓴다.
                source_url=meta["source_url"], effective_date=meta["effective_date"],
                flags=flags))
        self.units = units
        self.by_id = {u.id: u for u in units}
        if len(self.by_id) != len(units):
            raise CorpusError("duplicate_unit_id")
        self.groups: dict[str, list[Unit]] = {}
        for u in units:
            self.groups.setdefault(u.group, []).append(u)
        self.edges = raw["edges"]
        self.raw_sources = raw["sources"]
        self.law_names = sorted({u.law for u in units})

    def group_units(self, unit_id: str) -> list[Unit]:
        return self.groups[self.by_id[unit_id].group]
