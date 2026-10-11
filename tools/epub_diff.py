"""Show what changed between two conversions of the same PDF.

The corpus gate says whether metrics moved; this says where. It prints a diff
of the reading flow (one line per block: tag, class, PDF page, text with image
alt texts in brackets) and, with ``--out``, saves every image that is new or
changed so old and new crops can be compared by eye.

    python tools/epub_diff.py demo/attention_is_all_you_need-1706.03762.epub /tmp/new/attention_is_all_you_need-1706.03762.epub
    python tools/epub_diff.py old.epub new.epub --out /tmp/diff --context 1

Typical use after ``python tools/corpus_gate.py --keep /tmp/new``: diff each
committed EPUB against its new version before accepting with ``--update``.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import re
import sys
import zipfile
from pathlib import Path

BLOCK_RE = re.compile(r"<(p|h[1-6]|li|div)\b([^>]*)>(.*?)</\1>", re.S)
IMG_RE = re.compile(r"<img\b[^>]*>")


def _attr(tag: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return html.unescape(m.group(1)) if m else ""


def flow(epub: Path) -> tuple[list[str], dict[str, bytes]]:
    """Blocks as one-line summaries, plus image bytes keyed by 'page/alt/n'."""
    lines: list[str] = []
    images: dict[str, bytes] = {}
    with zipfile.ZipFile(epub) as z:
        names = sorted(n for n in z.namelist() if re.search(r"/ch\d+\.xhtml$", n))
        for name in names:
            text = z.read(name).decode("utf-8")
            for m in BLOCK_RE.finditer(text):
                tag, attrs, inner = m.groups()
                page = _attr(attrs, "data-page")
                if not page:
                    continue
                cls = _attr(attrs, "class")
                parts = []
                for piece in re.split(r"(<img\b[^>]*>)", inner):
                    if piece.startswith("<img"):
                        alt = _attr(piece, "alt")
                        parts.append(f"[{alt}]")
                        src = _attr(piece, "src")
                        path = (Path(name).parent / src).as_posix()
                        path = re.sub(r"[^/]+/\.\./", "", path)
                        if path in z.namelist():
                            key = f"p{page}/{alt}"
                            n = 1
                            while f"{key}/{n}" in images:
                                n += 1
                            images[f"{key}/{n}"] = z.read(path)
                    else:
                        parts.append(html.unescape(re.sub(r"<[^>]+>", "", piece)))
                body = re.sub(r"\s+", " ", "".join(parts)).strip()
                lines.append(f"{tag}.{cls or '-'} p{page}: {body}")
    return lines, images


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    ap.add_argument("--out", type=Path, help="save new/changed images here as old-*.png / new-*.png")
    ap.add_argument("--context", type=int, default=0, help="unchanged blocks shown around each change")
    ap.add_argument("--width", type=int, default=220, help="truncate block lines to this many characters")
    args = ap.parse_args(argv)

    a_lines, a_imgs = flow(args.old)
    b_lines, b_imgs = flow(args.new)
    cut = (lambda s: s if len(s) <= args.width else s[: args.width - 1] + "…")
    changed = 0
    for line in difflib.unified_diff([cut(x) for x in a_lines], [cut(x) for x in b_lines], "old", "new", n=args.context, lineterm=""):
        print(line)
        changed += line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    img_changes = []
    for key in sorted(set(a_imgs) | set(b_imgs)):
        a, b = a_imgs.get(key), b_imgs.get(key)
        if a is not None and b is not None and hashlib.sha1(a).digest() == hashlib.sha1(b).digest():
            continue
        img_changes.append(key)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", key)[:80]
            if a is not None:
                (args.out / f"old-{safe}.png").write_bytes(a)
            if b is not None:
                (args.out / f"new-{safe}.png").write_bytes(b)
    print(f"\n{changed} block line(s) changed; {len(img_changes)} image(s) new, removed or changed", file=sys.stderr)
    for key in img_changes:
        state = "new" if key not in a_imgs else "removed" if key not in b_imgs else "changed"
        print(f"  image {state}: {key}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
