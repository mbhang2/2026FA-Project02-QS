#!/usr/bin/env python3
"""Stage 0b: the whole paper, not its abstract.

    python scripts/fetch_fulltext.py --paper 2605.04004v3
    python scripts/fetch_fulltext.py --survivors          # everything stage 1 kept
    python scripts/fetch_fulltext.py --survivors --dry-run

**Why.** Stages 1, 2 and 3 all read `abstract`, which for `2605.04004v3` is
1,274 characters. A hypothesis distilled from an abstract is a hypothesis about
an abstract: the signal definitions, the friction model and the exact rules are
all in the body, and none of them reach the distiller. The grounding check
(`scripts/grounding.py`) is weakened the same way -- it can only ground a number
against text we hold, so a figure that is in the paper but not its abstract
reads as unsupported.

## Three sources, in order, and why this order

| order | source | needs |
|---|---|---|
| 1 | `arxiv.org/html/<id>` | stdlib only |
| 2 | `arxiv.org/e-print/<id>`, LaTeX | stdlib only |
| 3 | `arxiv.org/pdf/<id>` | the tools venv, below |

Measured over the 22 papers stage 1 kept: **18 have HTML, 4 are PDF-only.** The
first two cover most of the corpus for free. The four that do not include
`2605.04004v3` and `2605.17724v1`, which stage 2 rated **high** on the gate axis
-- so the expensive path is the one that matters, which is usually how it goes.

## The tools venv, and why the PDF reader is not in the main one

`integrity.environment_fingerprint()` hashes **every distribution importable by
the running interpreter**, not the lockfile. Installing a PDF reader into
`.venv` would therefore move the environment fingerprint of every subsequent
scored run, for a library the engine never calls.

It also breaks the separation the repo enforces: `scripts/` is build-time,
`src/core/` is run-time, and a test asserts nothing under `src.core` imports
anything under `scripts`. A PDF parser has no business being importable from the
engine's process.

So PDF extraction runs in a **separate interpreter**, `.venv-tools`, which is
gitignored and created on demand:

    python3 -m venv .venv-tools && .venv-tools/bin/pip install pypdf

`--setup-tools` does exactly that. If it is absent, the paper is recorded as
`pdf-only-not-extracted` and the pipeline keeps using its abstract, which is the
status quo rather than a failure.

## What it writes

`data/papers/text/<id>.txt`, and three fields on the registry row: which source
it came from, how many characters, and when. **Committed**, because the
grounding check reads it: a reviewer who cannot see the text a number was
checked against cannot check the check.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import pathlib
import re
import subprocess
import sys
import tarfile
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
TEXT_DIR = ROOT / "data" / "papers" / "text"
TOOLS_VENV = ROOT / ".venv-tools"
UA = {"User-Agent": "practicum-research/1.0 (mbhang2@illinois.edu)"}

# Under this, extraction produced something too thin to be a paper -- a stub
# page, a redirect, an empty LaTeX shell. The shortest real body in the corpus
# is several times this.
MIN_CHARS = 4_000


class _Text(HTMLParser):
    """Stdlib HTML to text. Drops script, style and math; keeps block breaks.

    Not a general-purpose converter and does not need to be: arXiv's HTML is
    generated from LaTeX and is regular. Math is dropped rather than mangled --
    a rendered formula becomes a run of single letters that adds noise to the
    prompt and false numbers to the grounding check.
    """

    SKIP = {"script", "style", "math", "svg", "figure"}
    BREAK = {"p", "div", "section", "h1", "h2", "h3", "h4", "li", "tr", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)

    def text(self) -> str:
        return _tidy("".join(self.out))


def _tidy(s: str) -> str:
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return "\n".join(line.strip() for line in s.splitlines()).strip()


def extract_html(html: str) -> str:
    p = _Text()
    p.feed(html)
    return p.text()


# LaTeX commands whose ARGUMENT is prose we want (`\textbf{...}` -> `...`), and
# environments whose contents are not (figures, tables of numbers we cannot
# attribute, the bibliography).
_KEEP_ARG = re.compile(r"\\(?:text(?:bf|it|rm|sc|sf|tt)|emph|underline|"
                       r"section|subsection|subsubsection|title|caption)"
                       r"\*?\{([^{}]*)\}")
_DROP_ENV = re.compile(r"\\begin\{(figure|table|tabular|equation|align|thebibliography)\*?\}"
                       r".*?\\end\{\1\*?\}", re.DOTALL)
_COMMAND = re.compile(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})?")
_COMMENT = re.compile(r"(?<!\\)%.*$", re.MULTILINE)


def extract_latex(blob: bytes) -> str:
    """The `.tex` files from an arXiv e-print tarball, stripped to prose.

    Deliberately crude. The goal is the claims and the numbers, not a faithful
    render: a number inside `\\textbf{}` must survive, a `\\citep{}` must not
    become a stray word, and a table of unlabelled figures is noise that would
    let the grounding check pass anything.
    """
    texts: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
            names = [m.name for m in t.getmembers()
                     if m.isfile() and m.name.endswith(".tex")]
            # `main.tex` first if present; otherwise the biggest, which is the
            # body in a multi-file submission.
            names.sort(key=lambda n: (not n.endswith("main.tex"), n))
            for n in names:
                f = t.extractfile(n)
                if f is not None:
                    texts.append(f.read().decode("utf-8", errors="replace"))
    except tarfile.ReadError:
        try:
            import gzip
            texts.append(gzip.decompress(blob).decode("utf-8", errors="replace"))
        except Exception:                                        # noqa: BLE001
            return ""
    if not texts:
        return ""
    s = "\n\n".join(texts)
    s = _COMMENT.sub("", s)
    s = _DROP_ENV.sub(" ", s)
    for _ in range(3):                      # nested \textbf{\emph{...}}
        s = _KEEP_ARG.sub(r"\1", s)
    s = _COMMAND.sub(" ", s)
    s = s.replace("{", " ").replace("}", " ").replace("\\", " ")
    return _tidy(s)


def tools_python() -> pathlib.Path | None:
    p = TOOLS_VENV / "bin" / "python"
    return p if p.exists() else None


def setup_tools() -> int:
    """Create `.venv-tools` and install the PDF reader there. Never in `.venv`."""
    print(f"creating {TOOLS_VENV.relative_to(ROOT)} (gitignored, build-time only)")
    subprocess.run([sys.executable, "-m", "venv", str(TOOLS_VENV)], check=True)
    r = subprocess.run([str(TOOLS_VENV / "bin" / "pip"), "install", "-q", "pypdf"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-800:], r.stderr[-800:])
        return 1
    print("  pypdf installed in the tools venv; the run-time environment is "
          "untouched and environment_fingerprint does not move")
    return 0


def extract_pdf(blob: bytes) -> str:
    """Text from a PDF, via the tools venv. Empty string when it is absent."""
    py = tools_python()
    if py is None:
        return ""
    tmp = TEXT_DIR / ".tmp.pdf"
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(blob)
    try:
        r = subprocess.run(
            [str(py), "-c",
             "import sys;from pypdf import PdfReader;"
             "print('\\n'.join((p.extract_text() or '') "
             "for p in PdfReader(sys.argv[1]).pages))", str(tmp)],
            capture_output=True, text=True, timeout=300)
        return _tidy(r.stdout) if r.returncode == 0 else ""
    except Exception:                                            # noqa: BLE001
        return ""
    finally:
        tmp.unlink(missing_ok=True)


def fetch(pid: str) -> tuple[str, str]:
    """`(text, source)`. Source is html, latex, pdf, or a reason it is empty."""
    import requests

    try:
        r = requests.get(f"https://arxiv.org/html/{pid}", timeout=90, headers=UA)
        if r.status_code == 200:
            text = extract_html(r.text)
            if len(text) >= MIN_CHARS:
                return text, "html"
    except Exception:                                            # noqa: BLE001
        pass

    try:
        r = requests.get(f"https://arxiv.org/e-print/{pid}", timeout=120, headers=UA)
        if r.status_code == 200:
            if r.content[:2] == b"\x1f\x8b":
                text = extract_latex(r.content)
                if len(text) >= MIN_CHARS:
                    return text, "latex"
            elif r.content[:4] == b"%PDF":
                text = extract_pdf(r.content)
                if len(text) >= MIN_CHARS:
                    return text, "pdf"
                if tools_python() is None:
                    return "", "pdf-only-not-extracted"
    except Exception:                                            # noqa: BLE001
        pass
    return "", "unavailable"


def fetch_url(url: str) -> tuple[str, str]:
    """`(text, source)` for an arbitrary open-access link.

    `fetch()` above is arXiv-only: it knows arxiv.org/html and arxiv.org/e-print.
    `extract_html` and `extract_pdf` were already generic -- they take text and
    bytes, not ids -- so the non-arXiv corpus gets real bodies for the cost of
    choosing between them on the content type.

    Added 2026-10-04 with `fetch_scholar.py`: OpenAlex gives an `oa_url` for the
    open-access half of what it knows about, and a screen reading an abstract
    alone is the weaker path this repo already documents.
    """
    import requests

    if not url:
        return "", "no-oa-url"
    try:
        r = requests.get(url, timeout=120, headers=UA, allow_redirects=True)
        if r.status_code != 200:
            return "", f"http-{r.status_code}"
        ctype = (r.headers.get("content-type") or "").lower()
        if r.content[:4] == b"%PDF" or "pdf" in ctype:
            text = extract_pdf(r.content)
            if len(text) >= MIN_CHARS:
                return text, "oa-pdf"
            if tools_python() is None:
                return "", "pdf-only-not-extracted"
            return "", "oa-pdf-too-short"
        if "html" in ctype or r.content[:1] == b"<":
            text = extract_html(r.text)
            if len(text) >= MIN_CHARS:
                return text, "oa-html"
            # A publisher landing page that is all navigation and an abstract is
            # the common case here, and it is not a body.
            return "", "oa-html-too-short"
    except Exception as exc:                                     # noqa: BLE001
        return "", f"error-{type(exc).__name__}"
    return "", "unavailable"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paper", help="a single registry id")
    ap.add_argument("--survivors", action="store_true",
                    help="every paper stage 1 judged direct or adaptable")
    ap.add_argument("--infeasible", action="store_true",
                    help="papers stage 1 REJECTED, so the screen's "
                         "false-negative rate can be measured at all (Task 71). "
                         "A rejected paper never gets a body, so nothing can "
                         "discover that its body would have kept it.")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after this many; with --infeasible the subset is "
                         "a SEEDED random sample, not the first N, because the "
                         "registry is in arXiv fetch order and that correlates "
                         "with search term and date")
    ap.add_argument("--sample-seed", type=int, default=71,
                    help="the seed for that sample; 71 is Task 71's")
    ap.add_argument("--setup-tools", action="store_true",
                    help="create .venv-tools and install the PDF reader there")
    ap.add_argument("--force", action="store_true", help="refetch what is present")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.setup_tools:
        return setup_tools()
    if not (a.paper or a.survivors or a.infeasible):
        sys.exit("give --paper <id>, --survivors, --infeasible, or --setup-tools")
    if a.survivors and a.infeasible:
        sys.exit("--survivors and --infeasible are different populations and "
                 "mixing them makes the measurement uninterpretable")

    reg = json.loads(REGISTRY.read_text())
    if a.paper:
        if a.paper not in reg["papers"]:
            sys.exit(f"{a.paper} is not in the registry")
        todo = [a.paper]
    elif a.infeasible:
        todo = sorted(v["id"] for v in reg["papers"].values()
                      if v.get("feasibility") == "infeasible")
    else:
        todo = sorted(v["id"] for v in reg["papers"].values()
                      if v.get("feasibility") in ("direct", "adaptable"))
    if not a.force:
        todo = [p for p in todo if not (TEXT_DIR / f"{p}.txt").exists()]
    if a.limit is not None and len(todo) > a.limit:
        if a.infeasible:
            # **Seeded random, not the first N.** The registry is in arXiv fetch
            # order, which follows the search term and the submission date, so
            # the first 40 would be 40 papers about whatever was searched first.
            # A measurement of a rate has to sample the population.
            import random
            todo = sorted(random.Random(a.sample_seed).sample(todo, a.limit))
            print(f"sampled {a.limit} of the infeasible set with seed "
                  f"{a.sample_seed}\n")
        else:
            todo = todo[:a.limit]
    if not todo:
        print("every requested paper already has full text; --force to refetch")
        return 0

    if tools_python() is None:
        print("note: no .venv-tools, so PDF-only papers will be skipped "
              "(--setup-tools creates it)\n")
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for pid in todo:
        # **The arXiv path is only for arXiv ids.** `fetch()` asks
        # `arxiv.org/html/<id>`, and for an OpenAlex row that is
        # `arxiv.org/html/openalex-W1030441327`, which 404s -- so before this,
        # running the full-text stage over a non-arXiv survivor could only fail.
        # `fetch_url` has existed since 2026-10-04 and handled exactly this, but
        # nothing in THIS file called it: the only caller was
        # `screen_papers.py`, which pulls a body as a side effect of screening.
        # So non-arXiv coverage depended on whether screening happened to run
        # with that code present, which is why 122 OpenAlex rows carry
        # `fulltext_source: null` -- never attempted rather than attempted and
        # refused.
        row = reg["papers"][pid]
        text, source = "", "no-path"
        if row.get("source") == "arxiv":
            text, source = fetch(pid)
        if not text and row.get("oa_url"):
            text, source = fetch_url(row["oa_url"])
        elif not text and not row.get("oa_url") and row.get("source") != "arxiv":
            # Neither an arXiv id nor an open-access link. Counted out loud
            # rather than silently skipped: these are the papers a DOI-based
            # lookup would be for, and "we have no address for the body" is a
            # different fact from "we fetched it and it was too short".
            source = "no-oa-url" + ("-has-doi" if row.get("doi") else "")
        counts[source] = counts.get(source, 0) + 1
        raw = ROOT / "data" / "papers" / "raw" / f"{pid}.json"
        abstract = len(str(
            (json.loads(raw.read_text()).get("abstract") if raw.exists() else "")
            or reg["papers"][pid].get("abstract") or ""))
        ratio = f"{len(text) / abstract:.0f}x" if abstract and text else "-"
        print(f"  {pid}  {source:24} {len(text):>8,} chars  ({ratio} the abstract)")
        if a.dry_run or not text:
            continue
        (TEXT_DIR / f"{pid}.txt").write_text(text)
        reg["papers"][pid].update(
            fulltext_source=source, fulltext_chars=len(text),
            fulltext_fetched=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"))
    print(f"\n  {counts}")
    if a.dry_run:
        print("  dry run: nothing written")
        return 0
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {TEXT_DIR.relative_to(ROOT)}/ and the registry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
