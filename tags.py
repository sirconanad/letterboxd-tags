#!/usr/bin/env python3
"""Letterboxd diary tag analysis: groups, rankings and charts.

Reads a fresh Letterboxd data export (``letterboxd-*.zip``, from
https://letterboxd.com/settings/data/) and analyses the diary tags:
  * a ranked tag list grouped by frequency tier (and optional custom rules)
  * a horizontal bar chart (terminal + SVG)
  * per-tag monthly trends (terminal table + SVG multi-line chart)
  * a co-occurrence matrix (which tags are used together) and top pairs

No HTML scraping and no Letterboxd API -- the export is the only data source.

Examples:
    python3 tags.py letterboxd-....zip
    python3 tags.py letterboxd-....zip --top 20 --year 2026
    python3 tags.py <export> --group "cinema|kino=cinemas" --svg out.svg
    python3 tags.py <export> --all-tags --json tags.json --svg out.svg
    python3 tags.py <export> --tag schauburg --tag cinema --svg out.svg
"""

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date
from html import escape as html_escape
from pathlib import Path

BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"  # ▁..█
MONTH_ABBR = "JFMAMJJASOND"
MONTHS_FULL = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PALETTE = ["#216e39", "#9be9a8", "#30a14e", "#40c463", "#7a9e4f",
           "#1e6b4e", "#8fd4a8", "#55a868"]


# ---------------------------------------------------------------- reading


def find_export() -> Path | None:
    """Locate the newest Letterboxd backup zip near the repo."""
    hits = []
    for d in (Path.cwd(), Path.cwd().parent):
        hits.extend(d.glob("letterboxd-*.zip"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def diary_rows(path: Path) -> list[dict]:
    if not zipfile.is_zipfile(path):
        raise ValueError(f"{path} is not a Letterboxd export zip")
    with zipfile.ZipFile(path) as zf:
        raw = zf.read("diary.csv").decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(raw)))


def entry_tags(e: dict) -> list[str]:
    return [t.strip() for t in (e.get("Tags") or "").split(",") if t.strip()]


def fetch_tag_list(rows: list[dict]) -> list[tuple[str, str, int]]:
    """Return [(tag, label, count), ...] sorted by count (label == tag)."""
    counts = Counter(t for e in rows for t in entry_tags(e))
    return [(tag, tag, n) for tag, n in counts.most_common()]


def fetch_tag_keys(rows: list[dict], label: str) -> list[tuple[date, str]]:
    """Diary entries carrying a tag -> [(watched date, film key), ...]."""
    keys = []
    want = label.lower()
    for e in rows:
        if not any(t.lower() == want for t in entry_tags(e)):
            continue
        when_text = (e.get("Watched Date") or "").strip()
        name = (e.get("Name") or "?").strip()
        if not when_text:
            continue
        try:
            when = date.fromisoformat(when_text)
        except ValueError:
            continue
        keys.append((when, name.lower()))
    return keys


# --------------------------------------------------------------- analysis


def frequency_tier(count: int, max_count: int) -> tuple[str, int]:
    """Return (tier name, sort order) for a tag given the top count."""
    if max_count <= 0:
        return "Long tail", 3
    share = count / max_count
    if share >= 0.5:
        return "Top tier", 1
    if share >= 0.2:
        return "Mid tier", 2
    return "Long tail", 3


def apply_group_rules(tag_label: str,
                      rules: list[tuple[re.Pattern, str]]) -> str | None:
    for pattern, label in rules:
        if pattern.search(tag_label):
            return label
    return None


def monthly_series(keys: list[tuple[date, str]], year: int) -> list[int]:
    counts = [0] * 12
    for d, _ in keys:
        if d.year == year:
            counts[d.month - 1] += 1
    return counts


# ------------------------------------------------------------- ASCII charts


def bar_chart_ascii(items: list[tuple[str, int]], width: int = 36) -> str:
    """Horizontal bar chart: [(label, count), ...] into ASCII art."""
    if not items:
        return "(no data)\n"
    maxv = max(c for _, c in items)
    return "\n".join(
        f"{label:<24}{count:>6} |" + "\u2588" * int(count / maxv * width)
        for label, count in items)


def sparkline(counts: list[int]) -> str:
    maxv = max(counts) if counts else 0
    if maxv == 0:
        return " " * 12
    return "".join(BLOCKS[min(round(c / maxv * (len(BLOCKS) - 1)),
                              len(BLOCKS) - 1)] for c in counts[:12])


# --------------------------------------------------------------- SVG charts


def _bar_chart_svg(items: list[tuple[str, str, int]], x: int, y: int,
                   chart_w: int, chart_h: int, title: str,
                   show_group: bool) -> list[str]:
    out = [f'<text x="{x}" y="{y}" font-size="14" font-weight="700" '
           f'fill="#222">{html_escape(title)}</text>']
    if not items:
        return out
    maxv = max(c for _, _, c in items)
    row_h = chart_h / len(items)
    label_w = 200 if show_group else 140
    for i, (label, group, count) in enumerate(items):
        by = y + 20 + i * row_h
        out.append(f'<text x="{x}" y="{by}" font-size="11" fill="#333">'
                   f'{html_escape(label)}</text>')
        if show_group:
            out.append(f'<text x="{x + label_w - 40}" y="{by}" font-size="10"'
                       f' fill="#999" text-anchor="end">'
                       f'{html_escape(group)}</text>')
        w = max(0, int(count / maxv * chart_w)) if maxv else 0
        out.append(f'<rect x="{x + label_w}" y="{by - 9}" width="{w}" '
                   f'height="9" rx="2" fill="{PALETTE[i % len(PALETTE)]}">'
                   f'<title>{html_escape(label)}: {count}</title></rect>')
        out.append(f'<text x="{x + label_w + w + 4}" y="{by}" font-size="10"'
                   f' fill="#666">{count}</text>')
    return out


def _trends_svg(items: list[tuple[str, list[int]]], year: int,
                x: int, y: int, chart_w: int, chart_h: int) -> list[str]:
    """Two-column month bar charts, one per tag."""
    out = [f'<text x="{x}" y="{y}" font-size="14" font-weight="700" '
           f'fill="#222">Monthly usage, {year} (entries per month)</text>']
    rows = (len(items) + 1) // 2
    row_h = chart_h / (rows if rows else 1)
    col_w = (chart_w - 20) / 2
    bar_w = max(3, int(col_w / 13))
    for i, (label, counts) in enumerate(items):
        cx = x + (i % 2) * (col_w + 20)
        cy = y + 26 + (i // 2) * row_h
        out.append(f'<text x="{cx}" y="{cy + 10}" font-size="11" '
                   f'font-weight="700" fill="{PALETTE[i % len(PALETTE)]}">'
                   f'{html_escape(label)}</text>')
        maxs = max(counts) if counts else 0
        hgt = max(2, row_h - 26)
        for j, c in enumerate(counts):
            bh = int(c / maxs * hgt) if maxs else 2
            out.append(f'<rect x="{cx + j * bar_w}" y="{cy + hgt + 10 - bh}" '
                       f'width="{max(2, bar_w - 1)}" height="{max(2, bh)}" '
                       f'rx="1" fill="{PALETTE[(i + j) % len(PALETTE)]}" '
                       f'opacity="0.9"><title>{MONTHS_FULL[j]}: '
                       f'{c}</title></rect>')
        for j, m in enumerate("JFMAMJJASOND"):
            out.append(f'<text x="{cx + j * bar_w + bar_w / 2}" '
                       f'y="{cy + hgt + 22}" font-size="7" fill="#aaa" '
                       f'text-anchor="middle">{m}</text>')
    return out


def _matrix_svg(tags: list[str], matrix: list[list[int]],
                x: int, y: int, cell: int) -> list[str]:
    out = [f'<text x="{x}" y="{y}" font-size="14" font-weight="700" '
           f'fill="#222">Tag co-occurrence (entries sharing both tags)'
           f'</text>']
    n = len(tags)
    maxv = max(max(row) for row in matrix) if matrix else 0
    gap = 2
    label_y = y + 24
    for i, t in enumerate(tags):
        tx = x + i * (cell + gap) + cell / 2
        out.append(f'<text x="{tx}" y="{label_y}" font-size="8" '
                   f'fill="#555" text-anchor="end" transform="rotate(-55 '
                   f'{tx:.1f} {label_y})">{html_escape(t[:14])}</text>')
        out.append(f'<text x="{x + 20}" y="{label_y + 3 + i * (cell + gap)}" '
                   f'font-size="8" fill="#555" text-anchor="end">'
                   f'{html_escape(t[:14])}</text>')
    for i in range(n):
        for j in range(n):
            gx = x + j * (cell + gap)
            gy = label_y + 6 + i * (cell + gap)
            v = matrix[i][j] if i != j else 0
            ratio = v / maxv if maxv else 0
            if i == j:
                color = "#e9ecf1"
            else:
                color = PALETTE[int(ratio * (len(PALETTE) - 1))]
            out.append(f'<rect x="{gx}" y="{gy}" width="{cell}" '
                       f'height="{cell}" rx="2" fill="{color}" '
                       f'opacity="{0.25 + 0.75 * ratio}">'
                       f'<title>{html_escape(tags[i])} + '
                       f'{html_escape(tags[j])}: {v}</title></rect>')
    return out


def build_svg(title_note: str, year: int, tags, rules, want, labels,
              keys_by_tag, matrix_n: int) -> str:
    """Assemble every chart into one SVG, stacked top to bottom."""
    W = 960
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'height="PLACEHOLDER" width="{W}" '
           f'viewBox="0 0 {W} PLACEHOLDER">',
           '<rect width="100%" height="100%" fill="#ffffff"/>']
    x = 30
    y = 12

    out += _bar_chart_svg(
        [(label, None, c) for _s, label, c in tags[:15]],
        x, y, 460, 300, f"Most-used diary tags — {title_note}", False)
    y += 340

    if rules:
        buckets = defaultdict(lambda: [0, 0])
        for slug, label, count in tags:
            g = apply_group_rules(label, rules) or "Other"
            buckets[g][0] += 1
            buckets[g][1] += count
        ordered = sorted(buckets.items(), key=lambda kv: -kv[1][1])
        out += _bar_chart_svg(
            [(label, f"{n} tags", c) for label, (n, c) in ordered],
            520, y, 420, 300, "Custom groups", True)
        y += 340

    trends = [(labels.get(s, s), monthly_series(keys, year))
              for s, keys in keys_by_tag.items()]
    if trends:
        out += _trends_svg(trends, year, x, y, W - 2 * x, 340)
        y += 390

    if matrix_n and matrix_n > 1 and len(want) >= 2:
        mtags = want[:matrix_n]
        mkeys = [set(keys_by_tag[s]) for s, _l, _c in mtags]
        mtx = [[len(a & b) for b in mkeys] for a in mkeys]
        cell = max(10, min(22, int((W - 160) / len(mtags))))
        out += _matrix_svg([labels.get(s, s) for s, _l, _c in mtags],
                           mtx, x + 80, y, cell)
        y += len(mtags) * (cell + 2) + 60

    out.append("</svg>")
    svg = "\n".join(out)
    return svg.replace("PLACEHOLDER", str(y + 10))


# ------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Analyse a Letterboxd diary export's tags.")
    parser.add_argument("export", nargs="?",
                        help="Path to a Letterboxd data export zip "
                             "(auto-detected next to the repo)")
    parser.add_argument("--top", type=int, default=10,
                        help="Number of top tags to fetch in detail "
                             "(default 10)")
    parser.add_argument("--tag", action="append", metavar="LABEL",
                        help="Explicit tag label(s) to analyse instead of "
                             "the top-N")
    parser.add_argument("--year", type=int,
                        help="Year for monthly trends "
                             "(default: current year)")
    parser.add_argument("--min-count", type=int, default=0,
                        help="Ignore tags used fewer than N times")
    parser.add_argument("--group", action="append", metavar="REGEX=LABEL",
                        help="Custom grouping rule, e.g. "
                             "--group 'cinema|kino=cinemas'. Repeatable. "
                             "Unmatched tags group as 'Other'.")
    parser.add_argument("--matrix", type=int, default=0,
                        help="Co-occurrence matrix of the top M tags in the "
                             "SVG (default 0 = off)")
    parser.add_argument("--all-tags", action="store_true",
                        help="Fetch diary entries for every tag (slower)")
    parser.add_argument("--svg", metavar="FILE",
                        help="Write charts to an SVG file")
    parser.add_argument("--json", metavar="FILE",
                        help="Write analysis data to a JSON file")
    args = parser.parse_args(argv)

    path = Path(args.export) if args.export else find_export()
    if path is None or not path.exists():
        print("error: no Letterboxd data export found; download one from "
              "https://letterboxd.com/settings/data/ and pass its path or "
              "place it next to the repo", file=sys.stderr)
        return 1

    year = args.year or date.today().year

    try:
        rows = diary_rows(path)
    except (ValueError, OSError, KeyError, csv.Error) as exc:
        print(f"error: could not read export: {exc}", file=sys.stderr)
        return 1
    print(f"{path.name}: {len(rows)} diary entry/entries", file=sys.stderr)

    tags = fetch_tag_list(rows)
    tags = [t for t in tags if t[2] >= args.min_count]
    if not tags:
        print("no tags found in this export", file=sys.stderr)
        return 1

    if args.tag:
        by_label = {t[1].lower(): t for t in tags}
        missing = [s for s in args.tag if s.lower() not in by_label]
        if missing:
            print(f"note: unknown tag(s): {', '.join(missing)}",
                  file=sys.stderr)
        want = [by_label[s.lower()] for s in args.tag if s.lower() in by_label]
    else:
        want = tags if args.all_tags else tags[:args.top]

    labels = {t[0]: t[1] for t in tags}
    keys_by_tag: dict[str, list[tuple[date, str]]] = {}

    tags_by_lower = {t[1].lower(): t[1] for t in tags}
    print(f"Fetching diary entries for {len(want)} tag(s) ...",
          file=sys.stderr)
    for idx, (code, label, _count) in enumerate(want, 1):
        print(f"  [{idx}/{len(want)}] {label} ({code})", file=sys.stderr)
        keys_by_tag[code] = fetch_tag_keys(rows, tags_by_lower[label.lower()])

    entry_tags: dict[tuple[date, str], set] = defaultdict(set)
    for slug, keys in keys_by_tag.items():
        for key in keys:
            entry_tags[key].add(slug)
    union = set().union(*keys_by_tag.values()) if keys_by_tag else set()
    multi = [k for k, s in entry_tags.items() if len(s) > 1]
    pairs = Counter()
    for s in entry_tags.values():
        sl = sorted(s)
        for i in range(len(sl)):
            for j in range(i + 1, len(sl)):
                pairs[(sl[i], sl[j])] += 1

    max_count = tags[0][2]

    # ---- terminal report
    print("\n=== Diary tags by frequency tier ===")
    tiers: dict[str, list[str]] = {"Top tier": [], "Mid tier": [],
                                   "Long tail": []}
    for slug, label, count in tags:
        tiers[frequency_tier(count, max_count)[0]].append(label)
    print(f"Total: {len(tags)} distinct tags")
    for name in ("Top tier", "Mid tier", "Long tail"):
        names = tiers.get(name, [])
        if names:
            total = sum(c for s, l, c in tags if l in names)
            print(f"\n{name} ({len(names)} tags, {total} entries):")
            print("  " + ", ".join(names[:15]) +
                  (" ..." if len(names) > 15 else ""))

    rules = []
    for rule in args.group or []:
        if "=" not in rule:
            print(f"note: skipping group rule without '=': {rule}",
                  file=sys.stderr)
            continue
        pattern, label = rule.rsplit("=", 1)
        try:
            rules.append((re.compile(pattern, re.I), label))
        except re.error as exc:
            print(f"note: bad regex in group rule: {exc}", file=sys.stderr)

    if rules:
        print("\n=== Custom groups ===")
        buckets = defaultdict(lambda: [0, 0])
        for slug, label, count in tags:
            g = apply_group_rules(label, rules) or "Other"
            buckets[g][0] += 1
            buckets[g][1] += count
        ordered = sorted(buckets.items(), key=lambda kv: -kv[1][1])
        print(bar_chart_ascii([(label, c) for label, (n, c) in ordered]))

    print("\n=== Most-used diary tags (top 20) ===")
    print(bar_chart_ascii([(label, c) for _s, label, c in tags[:20]]))

    print(f"\n=== Monthly usage, {year} (entries per month) ===")
    print(f"{'Tag':<20}", *[f"{m:>3}" for m in MONTH_ABBR], f"{'Total':>6}",
          " Trend", sep="")
    for slug, label, _count in want:
        counts = monthly_series(keys_by_tag[slug], year)
        print(f"{label:<20}", *[f"{c:>3}" for c in counts],
              f"{sum(counts):>6}", " " + sparkline(counts), sep="")

    print("\n=== Co-occurrence ===")
    total_entries = len(entry_tags)
    print(f"Analysed {total_entries} tagged diary entry/entries across "
          f"{len(want)} tag(s); {len(multi)} entry/entries carry more than "
          f"one tag ({100 * len(multi) / total_entries:.0f}%) "
          f"of the analysed set." if total_entries else
          "No analysed entries.")
    if pairs:
        print("Most common tag pairs:")
        for (a, b), n in pairs.most_common(10):
            print(f"  {labels.get(a, a):<16}+ {labels.get(b, b):<16} {n}")

    # ---- JSON / SVG
    if args.json:
        data = {
            "export": str(path),
            "year": year,
            "tags": [{"tag": label, "count": c}
                     for slug, label, c in tags],
            "monthly": {slug: {"label": labels.get(slug, slug),
                               "months": monthly_series(keys, year)}
                        for slug, keys in keys_by_tag.items()},
            "co_occurrence": {f"{labels.get(a, a)} + {labels.get(b, b)}": n
                              for (a, b), n in pairs.most_common()},
        }
        try:
            with open(args.json, "w") as fh:
                json.dump(data, fh, indent=2)
            print(f"Wrote JSON: {args.json}")
        except OSError as exc:
            print(f"error: could not write JSON: {exc}", file=sys.stderr)
            return 1

    if args.svg:
        try:
            svg = build_svg(str(path.name), year, tags, rules, want, labels,
                            keys_by_tag, args.matrix)
            with open(args.svg, "w") as fh:
                fh.write(svg)
            print(f"Wrote SVG: {args.svg}")
        except OSError as exc:
            print(f"error: could not write SVG: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
