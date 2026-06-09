"""
html_report.py
--------------
Generates a single self-contained HTML coverage report from:
  - Original source files
  - coverage.log  (one flag name per line, written by the instrumented binary)
  - block_map per file (produced by Analyser)

Colouring rules:
  - Blocks whose flag was hit     → green background
  - Blocks whose flag was never hit → red background
  - Inner block colour always wins over outer (most-specific wins)
  - Comments, blank lines, #define, #include → no highlight (neutral)
  - Hit count shown on right side of FIRST line of each block only
"""

import re
import html
from collections import Counter
from pathlib import Path


# ---------------------------------------------------------------------------
# Non-executable line detector
# ---------------------------------------------------------------------------

_BLANK_RE       = re.compile(r"^\s*$")
_LINE_COMMENT   = re.compile(r"^\s*//")
_BLOCK_COMMENT  = re.compile(r"^\s*/\*")
_PREPROC        = re.compile(r"^\s*#")

def is_neutral_line(line: str) -> bool:
    """Returns True for lines that can never be 'executed'."""
    return bool(
        _BLANK_RE.match(line)
        or _LINE_COMMENT.match(line)
        or _BLOCK_COMMENT.match(line)
        or _PREPROC.match(line)
    )


# ---------------------------------------------------------------------------
# Per-line colour resolver
# ---------------------------------------------------------------------------

def resolve_line_colours(
    source_lines: list[str],
    block_map: list[dict],
    counts: Counter,
) -> dict[int, dict]:
    """
    Returns a dict:  line_number (1-based) → {colour, hit_count, flag, is_first}

    colour    : "green" | "red" | None
    hit_count : int (only meaningful when colour == "green")
    flag      : flag name responsible for the colour
    is_first  : True if this is the first line of the block (shows the count)
    """
    total_lines = len(source_lines)

    # line_info[lineno] = list of (priority, colour, hit_count, flag, is_first)
    # priority = block length (shorter = more specific = higher priority)
    line_info: dict[int, list] = {i: [] for i in range(1, total_lines + 1)}

    for entry in block_map:
        flag       = entry["flag"]
        start      = entry["start_line"]
        end        = entry["end_line"]
        hit_count  = counts.get(flag, 0)
        colour     = "green" if hit_count > 0 else "red"
        span       = end - start + 1          # shorter span = higher specificity

        for lineno in range(start, end + 1):
            if lineno < 1 or lineno > total_lines:
                continue
            is_first = (lineno == start)
            line_info[lineno].append((span, colour, hit_count, flag, is_first))

    result: dict[int, dict] = {}
    for lineno, candidates in line_info.items():
        if not candidates:
            continue
        line_text = source_lines[lineno - 1]
        if is_neutral_line(line_text):
            continue   # never colour neutral lines
        # Sort by span ascending → shortest (most specific) block wins
        candidates.sort(key=lambda c: c[0])
        span, colour, hit_count, flag, is_first = candidates[0]

        # is_first: only mark as first if THIS entry is the shortest-span one
        # re-check: is this lineno the start_line of the winning block?
        winning_flag = flag
        winning_start = next(
            e["start_line"] for e in block_map if e["flag"] == winning_flag
        )
        is_first = (lineno == winning_start)

        result[lineno] = {
            "colour":    colour,
            "hit_count": hit_count,
            "flag":      flag,
            "is_first":  is_first,
        }

    return result


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: #fff;
    color: #111;
    font-family: Arial, sans-serif;
    padding: 24px;
}
h1 {
    font-size: 1.2rem;
    font-weight: bold;
    margin-bottom: 4px;
}
.subtitle {
    font-size: 0.82rem;
    color: #555;
    margin-bottom: 16px;
}
.summary {
    font-size: 0.9rem;
    margin-bottom: 24px;
}
.file-section { margin-bottom: 36px; }
.file-header {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 4px;
}
.file-title {
    font-size: 0.95rem;
    font-weight: bold;
}
.coverage-pct { font-size: 0.82rem; color: #444; }

.code-table {
    width: 100%;
    border-collapse: collapse;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 0.8rem;
    line-height: 1.5;
    border: 1px solid #ddd;
}

td.ln {
    width: 40px;
    text-align: right;
    padding: 0 8px;
    color: #aaa;
    user-select: none;
    border-right: 1px solid #ddd;
    vertical-align: top;
}
td.code {
    padding: 0 8px;
    white-space: pre;
    width: 100%;
}
td.hits {
    padding: 0 10px;
    text-align: right;
    white-space: nowrap;
    font-size: 0.75rem;
    color: #333;
    vertical-align: top;
    min-width: 60px;
}

/* Row colours — light green / light red */
tr.green td { background: #e6ffed; }
tr.red   td { background: #fff0f0; }

/* Legend */
.legend {
    display: flex;
    gap: 16px;
    margin-bottom: 8px;
    font-size: 0.78rem;
}
.legend-dot {
    width: 12px; height: 12px;
    display: inline-block;
    margin-right: 4px;
    vertical-align: middle;
    border: 1px solid #ccc;
}
.dot-green { background: #e6ffed; }
.dot-red   { background: #fff0f0; }
.dot-none  { background: #fff; }
"""


def _html_line(lineno: int, raw_line: str, info: dict | None) -> str:
    colour    = info["colour"]    if info else None
    hit_count = info["hit_count"] if info else 0
    is_first  = info["is_first"]  if info else False

    row_class = f' class="{colour}"' if colour else ""

    # hits cell
    if colour == "green" and is_first:
        hits_html = f'<span title="{info["flag"]}">×{hit_count}</span>'
    elif colour == "red" and is_first:
        hits_html = '<span title="never executed">✗</span>'
    else:
        hits_html = ""

    code_html = html.escape(raw_line.rstrip("\n"))

    return (
        f'<tr{row_class}>'
        f'<td class="ln">{lineno}</td>'
        f'<td class="code">{code_html}</td>'
        f'<td class="hits">{hits_html}</td>'
        f'</tr>\n'
    )


def build_html(
    file_results: list[dict],   # [{filename, source, block_map, counts_for_file}]
    total_counts: Counter,
    log_path: str,
) -> str:

    # Global stats
    all_flags   = sum(len(r["block_map"]) for r in file_results)
    hit_flags   = sum(1 for r in file_results for b in r["block_map"] if total_counts.get(b["flag"], 0) > 0)
    pct_overall = int(100 * hit_flags / all_flags) if all_flags else 0

    sections_html = []

    for fr in file_results:
        fname      = fr["filename"]
        source     = fr["source"]
        block_map  = fr["block_map"]
        lines      = source.splitlines(keepends=True)

        line_colours = resolve_line_colours(lines, block_map, total_counts)

        # Per-file stats
        file_flags     = len(block_map)
        file_hit_flags = sum(1 for b in block_map if total_counts.get(b["flag"], 0) > 0)
        file_pct       = int(100 * file_hit_flags / file_flags) if file_flags else 0

        rows = []
        for i, raw in enumerate(lines, start=1):
            info = line_colours.get(i)
            rows.append(_html_line(i, raw, info))

        section = f"""
<div class="file-section">
  <div class="file-header">
    <span class="file-title">{html.escape(fname)}</span>
    <span class="coverage-pct">{file_pct}% covered ({file_hit_flags}/{file_flags})</span>
  </div>
  <table class="code-table">
    <tbody>
{''.join(rows)}    </tbody>
  </table>
</div>"""
        sections_html.append(section)

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Coverage Report</title>
<style>
{CSS}
</style>
</head>
<body>
<h1>Coverage Report</h1>
<div class="subtitle">Log: {html.escape(log_path)} &nbsp;·&nbsp; {len(file_results)} file(s)</div>

<div class="summary">Blocks covered: <strong>{pct_overall}%</strong> ({hit_flags}/{all_flags})</div>

<div class="legend">
  <span><span class="legend-dot dot-green"></span>Executed</span>
  <span><span class="legend-dot dot-red"></span>Never executed</span>
  <span><span class="legend-dot dot-none"></span>Non-executable</span>
</div>

{''.join(sections_html)}
</body>
</html>"""

    return body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def generate_report(
    source_files: list[str],
    log_path: str = "coverage.log",
    output_html: str = "coverage_report.html",
):
    from instrumenter import Analyser

    log = Path(log_path)
    if not log.exists():
        print(f"[ERROR] {log_path} not found. Run the instrumented binary first.")
        return

    counts = Counter(l.strip() for l in log.read_text().splitlines() if l.strip())

    file_results = []
    for src in source_files:
        p = Path(src)
        if not p.exists():
            print(f"[WARN] {src} not found, skipping.")
            continue
        source = p.read_text(encoding="utf-8")
        analyser = Analyser(source, p.stem)
        analyser.run()   # populate block_map without writing files
        file_results.append({
            "filename":  p.name,
            "source":    source,
            "block_map": analyser.block_map,
        })

    html_out = build_html(file_results, counts, log_path)
    Path(output_html).write_text(html_out, encoding="utf-8")
    print(f"✅  HTML report written: {output_html}")


if __name__ == "__main__":
    import argparse, sys

    ap = argparse.ArgumentParser(description="Generate HTML coverage report.")
    ap.add_argument("sources", nargs="+", metavar="FILE",
                    help="Original (non-instrumented) source files")
    ap.add_argument("--log",    default="coverage.log",         help="coverage.log path")
    ap.add_argument("--output", default="coverage_report.html", help="Output HTML path")
    args = ap.parse_args()

    generate_report(args.sources, args.log, args.output)
