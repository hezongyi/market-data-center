"""Check local Markdown links in changed documents without fetching external URLs."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def without_fences(text: str) -> str:
    return re.sub(r"^(`{3,}|~{3,})[^\n]*\n.*?^\1\s*$", "", text, flags=re.MULTILINE | re.DOTALL)


def anchors(text: str) -> set[str]:
    counts: dict[str, int] = {}
    result = set(re.findall(r'<a\s+(?:name|id)=["\']([^"\']+)', text))
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", without_fences(text), flags=re.MULTILINE):
        slug = re.sub(r"[^\w\-\s]", "", title.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


def check_links(paths: list[Path]) -> list[str]:
    errors = []
    for source in paths:
        if not source.is_file():
            continue  # Deleted documents have no outgoing links.
        text = without_fences(source.read_text())
        links = re.findall(r'\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)(?:\s+"[^"\n]*")?\)', text)
        for raw in links:
            href = raw.strip("<>")
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc:
                continue
            target = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
            if not target.exists():
                errors.append(f"{source}: missing target {href}")
            elif parsed.fragment and target.suffix == ".md" and unquote(parsed.fragment) not in anchors(target.read_text()):
                errors.append(f"{source}: missing anchor {href}")
    return errors


def changed_documents(repo: Path, base: str | None) -> list[Path]:
    paths: set[str] = set()
    commands = [["diff", "--name-only", "-z", "HEAD"],
                ["ls-files", "--others", "--exclude-standard", "-z"]]
    if base:
        commands.append(["diff", "--name-only", "-z", f"{base}...HEAD"])
    for command in commands:
        data = subprocess.check_output(["git", *command], cwd=repo)
        paths.update(os.fsdecode(item) for item in data.split(b"\0") if item)
    return [repo / name for name in sorted(paths) if name.endswith(".md")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--base", default=os.getenv("DATACENTER_DOCS_BASE"))
    args = parser.parse_args()
    paths = args.paths or changed_documents(ROOT, args.base)
    errors = check_links(paths)
    for error in errors:
        print(error)
    print(f"docs-links: {'failed' if errors else 'pass'} ({len(paths)} documents)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
