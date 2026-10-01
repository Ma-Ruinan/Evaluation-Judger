"""Import the fixed Markdown structure emitted by Rubric-Council."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

METRICS = ("completion", "coverage", "accuracy", "format", "structure", "hallucination")
LABELS = dict(zip(METRICS, ("任务完成率", "内容覆盖度", "准确率·忠实度", "格式合规度", "结构完整度", "幻觉／自洽性")))


@dataclass(frozen=True)
class Atom:
    id: str
    metric: str
    weight: Decimal
    layer: str
    purpose: str
    rule: str
    evidence: str
    provenance: str
    kind: str


@dataclass(frozen=True)
class ErrorRule:
    id: str
    trigger: str
    effects: str
    caps: dict[str, Decimal]


@dataclass(frozen=True)
class Rubric:
    path: Path
    text: str
    atoms: tuple[Atom, ...]
    errors: tuple[ErrorRule, ...]

    def by_metric(self, metric: str) -> tuple[Atom, ...]:
        return tuple(a for a in self.atoms if a.metric == metric)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def load_rubric(path: Path) -> Rubric:
    data = path.read_text(encoding="utf-8-sig")
    metric = None
    in_errors = False
    atoms: list[Atom] = []
    errors: list[ErrorRule] = []
    for line in data.splitlines():
        if re.match(r"^## 6\.", line):
            metric, in_errors = "completion", False
        elif re.match(r"^## 7\.", line):
            metric = None
        elif re.match(r"^### 7\.([1-5])", line):
            metric = METRICS[int(re.match(r"^### 7\.([1-5])", line).group(1))]
        elif re.match(r"^## 8\.", line):
            metric, in_errors = None, True
        elif re.match(r"^## 9\.", line):
            in_errors = False
        if not line.startswith("|") or re.match(r"^\|\s*---", line):
            continue
        cells = _cells(line)
        if metric and len(cells) >= 7 and re.fullmatch(r"[TCAFSH]\d+", cells[0]):
            kind_match = re.search(r"\*\*(BIN|RATIO|COUNT|CLAIM-RATIO)\*\*", cells[4])
            if not kind_match:
                raise ValueError(f"Missing status type: {path} {cells[0]}")
            atoms.append(Atom(cells[0], metric, Decimal(cells[2]), cells[1], cells[3], cells[4], cells[5], cells[6], kind_match.group(1)))
        elif in_errors and len(cells) >= 3 and re.fullmatch(r"E\d+", cells[0]):
            caps = {}
            for segment in re.split(r"[；;]", cells[2]):
                match = re.search(r"^\s*(.+?)：上限\s*([\d.]+)", segment)
                if not match:
                    continue
                label, value = match.groups()
                label = label.strip()
                found = next((m for m, name in LABELS.items() if name == label), None)
                if found:
                    caps[found] = Decimal(value)
            if cells[2].count("上限") != len(caps):
                raise ValueError(f"Unparsed critical-error cap: {path} {cells[0]}")
            errors.append(ErrorRule(cells[0], cells[1], cells[2], caps))
    if not atoms or set(a.metric for a in atoms) != set(METRICS):
        raise ValueError(f"Incomplete rubric metrics: {path}")
    ids = [a.id for a in atoms]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate atom ID: {path}")
    for m in METRICS:
        total = sum((a.weight for a in atoms if a.metric == m), Decimal(0))
        if total != 100:
            raise ValueError(f"{path}: {m} weights sum to {total}, expected 100")
    return Rubric(path, data, tuple(atoms), tuple(errors))
