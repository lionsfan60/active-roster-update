"""Read/write Axis Football ROSTER.CSV without assuming a fixed schema.

Axis has changed this file between releases (2026 added the KickReturnerIndex /
PuntReturnerIndex directive lines). So: the header row of the *installed* game's file
defines the columns, any `Key=Value` directive lines are preserved verbatim, and we
only rewrite the player rows. That keeps the tool working across Axis versions.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RosterFile:
    header: list[str]
    directives: list[str] = field(default_factory=list)   # raw lines like "KickReturnerIndex=6,,,,"
    rows: list[dict[str, str]] = field(default_factory=list)
    width: int = 0

    def column(self, *names: str) -> str | None:
        """First header column matching any of `names` (case/space-insensitive)."""
        flat = {h.replace(" ", "").upper(): h for h in self.header}
        for n in names:
            hit = flat.get(n.replace(" ", "").upper())
            if hit:
                return hit
        return None


def _is_directive(fields: list[str]) -> bool:
    return bool(fields) and "=" in fields[0] and not fields[0].strip().isdigit()


def parse(path: Path) -> RosterFile:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError(f"{path} is empty")

    reader = list(csv.reader(io.StringIO("\n".join(lines))))
    header = [h.strip() for h in reader[0]]
    rf = RosterFile(header=header, width=len(header))
    for raw_line, fields in zip(lines[1:], reader[1:]):
        if _is_directive(fields):
            rf.directives.append(raw_line)
            continue
        row = {header[i]: (fields[i] if i < len(fields) else "") for i in range(len(header))}
        rf.rows.append(row)
    return rf


def dump(rf: RosterFile, rows: list[dict[str, str]]) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(rf.header)
    out = buf.getvalue()
    for d in rf.directives:
        out += d + "\n"
    buf2 = io.StringIO(newline="")
    w2 = csv.writer(buf2, lineterminator="\n")
    for row in rows:
        w2.writerow([str(row.get(h, "")) for h in rf.header])
    return out + buf2.getvalue()


def write(path: Path, rf: RosterFile, rows: list[dict[str, str]]) -> None:
    Path(path).write_text(dump(rf, rows), encoding="utf-8", newline="")
