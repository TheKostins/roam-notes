#!/usr/bin/env python3
"""roam.py -- тулинг агента для vault org-roam (только stdlib).

Источник правды -- сами .org файлы (sqlite-база org-roam устаревает, когда
файлы правятся вне Emacs).  Команды: list, pending, show, context, lint, fix,
spell, mentions, new, macros, expand, cards, guard, sync, state, pdfmap, grep-pdf,
handbook (plan/export/build -- PDF-конспект, см. roam_tex.py),
site (plan/export/check/serve -- HTML-сайт, см. roam_html.py).

Коды возврата: 0 -- ок, 1 -- есть находки / отказ, 2 -- ошибка использования.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
STATE_PATH = VAULT / ".audit" / "state.json"
CONFIG_PATH = VAULT / "roam.toml"
KNOWN_MACROS_PATH = Path(__file__).resolve().parent / "known_macros.txt"

SEVERITY_ORDER = {"E": 0, "W": 1, "I": 2, "M": 3}


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as fh:
        cfg = tomllib.load(fh)
    cfg.setdefault("vault", {})
    cfg.setdefault("courses", {})
    cfg.setdefault("anki", {})
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "notes": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


# --------------------------------------------------------------------------
# slug (порт org-roam-node-slugify)
# --------------------------------------------------------------------------

_SLUG_STRIP = set(
    list(range(0x0300, 0x0305)) + list(range(0x0306, 0x030D))
    + [0x031B, 0x0323, 0x0324, 0x0325, 0x0327, 0x032D, 0x032E, 0x0330, 0x0331])


def _emacs_alnum(c: str) -> bool:
    cat = unicodedata.category(c)
    return cat[0] in "LM" or cat in ("Nd", "Nl")


def slugify(title: str) -> str:
    s = unicodedata.normalize("NFD", title)
    s = "".join(c for c in s if ord(c) not in _SLUG_STRIP)
    s = unicodedata.normalize("NFC", s)
    s = "".join(c if _emacs_alnum(c) else "_" for c in s)
    s = re.sub(r"__+", "_", s)
    s = re.sub(r"^_", "", s)
    s = re.sub(r"_$", "", s)
    return s.lower()


# --------------------------------------------------------------------------
# org parsing
# --------------------------------------------------------------------------

HEADING_RE = re.compile(r"^(\*+)[ \t]+(.*?)(?:[ \t]+(:[\w@#%:]+:))?[ \t]*$")
KEYWORD_RE = re.compile(r"^#\+([A-Za-z_]+):[ \t]*(.*?)[ \t]*$")
LINK_RE = re.compile(r"\[\[([^\]\[]+)\](?:\[(.*?)\])?\]")
UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
FILENAME_RE = re.compile(r"^(\d{14})-(.*)\.org$")


@dataclass
class Heading:
    level: int
    title: str
    tags: list[str]
    line: int                 # 0-based index of the heading line
    end: int = 0              # exclusive end of the subtree
    props: dict = field(default_factory=dict)
    body_start: int = 0       # first line after heading + drawer
    parent: "Heading | None" = None

    def inherited(self, prop: str):
        h = self
        while h is not None:
            if prop in h.props:
                return h.props[prop]
            h = h.parent
        return None

    def noexport(self) -> bool:
        h = self
        while h is not None:
            if "noexport" in h.tags:
                return True
            h = h.parent
        return False


@dataclass
class Link:
    line: int
    start: int                # offset in the line
    end: int
    target: str
    desc: str | None

    @property
    def id(self) -> str | None:
        return self.target[3:].strip().upper() if self.target.lower().startswith("id:") else None


class Note:
    def __init__(self, path: Path):
        self.path = path
        self.raw = path.read_text(encoding="utf-8")
        self.lines = self.raw.split("\n")
        self.props: dict[str, str] = {}
        self.keywords: list[tuple[int, str, str]] = []   # (line, key, value)
        self.drawer_end = -1
        self._parse_header()
        self._parse_headings()
        self._collect_excluded()
        self._parse_links()

    # -- header -----------------------------------------------------------
    def _parse_header(self) -> None:
        L = self.lines
        i = 0
        if L and L[0].strip() == ":PROPERTIES:":
            i = 1
            while i < len(L) and L[i].strip() != ":END:":
                m = re.match(r"^:([^:\s]+):[ \t]*(.*?)[ \t]*$", L[i])
                if m:
                    self.props[m.group(1).upper()] = m.group(2)
                i += 1
            self.drawer_end = i
            i += 1
        last_kw = i - 1
        while i < len(L):
            m = KEYWORD_RE.match(L[i])
            if m:
                self.keywords.append((i, m.group(1).lower(), m.group(2)))
                last_kw = i
            elif L[i].strip():
                break
            i += 1
        self.body_start = last_kw + 1

    def kw(self, key: str) -> str | None:
        for _, k, v in self.keywords:
            if k == key:
                return v
        return None

    def kw_line(self, key: str) -> int | None:
        for ln, k, _ in self.keywords:
            if k == key:
                return ln
        return None

    @property
    def id(self) -> str:
        return self.props.get("ID", "").upper()

    @property
    def title(self) -> str:
        return (self.kw("title") or "").strip()

    @property
    def tags(self) -> list[str]:
        return [t for t in (self.kw("filetags") or "").split(":") if t.strip()]

    @property
    def date(self) -> str | None:
        return self.kw("date")

    @property
    def file_props(self) -> dict[str, str]:
        out = {}
        for _, k, v in self.keywords:
            if k == "property" and v:
                parts = v.split(None, 1)
                out[parts[0].upper()] = parts[1] if len(parts) > 1 else ""
        return out

    @property
    def created(self) -> str | None:
        m = FILENAME_RE.match(nfc(self.path.name))
        return m.group(1) if m else None

    @property
    def file_slug(self) -> str | None:
        m = FILENAME_RE.match(nfc(self.path.name))
        return m.group(2) if m else None

    @property
    def rel(self) -> str:
        return nfc(str(self.path.relative_to(VAULT)))

    # -- outline ----------------------------------------------------------
    def _parse_headings(self) -> None:
        self.headings: list[Heading] = []
        stack: list[Heading] = []
        in_block = False
        for i, line in enumerate(self.lines):
            low = line.lower().lstrip()
            if low.startswith("#+begin_"):
                in_block = True
            elif low.startswith("#+end_"):
                in_block = False
            if in_block:
                continue
            m = HEADING_RE.match(line)
            if not m:
                continue
            h = Heading(level=len(m.group(1)), title=m.group(2).strip(),
                        tags=[t for t in (m.group(3) or "").split(":") if t], line=i)
            while stack and stack[-1].level >= h.level:
                stack.pop().end = i
            h.parent = stack[-1] if stack else None
            stack.append(h)
            self.headings.append(h)
            j = i + 1
            # planning line (SCHEDULED etc.) may precede the drawer
            if j < len(self.lines) and re.match(r"^\s*(SCHEDULED|DEADLINE|CLOSED):", self.lines[j]):
                j += 1
            if j < len(self.lines) and self.lines[j].strip() == ":PROPERTIES:":
                j += 1
                while j < len(self.lines) and self.lines[j].strip() != ":END:":
                    pm = re.match(r"^\s*:([^:\s]+):[ \t]*(.*?)[ \t]*$", self.lines[j])
                    if pm:
                        h.props[pm.group(1).upper()] = pm.group(2)
                    j += 1
                j += 1
            h.body_start = j
        for h in stack:
            h.end = len(self.lines)
        for h in self.headings:
            if not h.end:
                h.end = len(self.lines)

    def _collect_excluded(self) -> None:
        """Lines that are not note content: header, noexport subtrees,
        drawers, keywords, src/example blocks."""
        ex: set[int] = set(range(0, self.body_start))
        for h in self.headings:
            if "noexport" in h.tags:
                ex.update(range(h.line, h.end))
        in_drawer = in_block = False
        for i, line in enumerate(self.lines):
            s = line.strip()
            low = s.lower()
            if low.startswith("#+begin_src") or low.startswith("#+begin_example"):
                in_block = True
            if in_block:
                ex.add(i)
                if low.startswith("#+end_src") or low.startswith("#+end_example"):
                    in_block = False
                continue
            if not in_drawer and re.match(r"^:[A-Z_]+:$", s) and s != ":END:":
                in_drawer = True
            if in_drawer:
                ex.add(i)
                if s == ":END:":
                    in_drawer = False
                continue
            if KEYWORD_RE.match(line) or re.match(r"^\s*(SCHEDULED|DEADLINE|CLOSED):", line):
                ex.add(i)
        self.excluded = ex

    def noexport_ranges(self) -> list[tuple[int, int]]:
        return [(h.line, h.end) for h in self.headings
                if "noexport" in h.tags and not (h.parent and h.parent.noexport())]

    # -- links ------------------------------------------------------------
    def _parse_links(self) -> None:
        self.links: list[Link] = []
        for i, line in enumerate(self.lines):
            for m in LINK_RE.finditer(line):
                self.links.append(Link(i, m.start(), m.end(), m.group(1), m.group(2)))

    def content_links(self) -> list[Link]:
        return [l for l in self.links if l.line not in self.excluded]

    def out_ids(self) -> list[str]:
        seen, out = set(), []
        for l in self.content_links():
            if l.id and l.id not in seen:
                seen.add(l.id)
                out.append(l.id)
        return out

    # -- derived texts ------------------------------------------------------
    def content_lines(self, links: str = "desc") -> list[str]:
        """Note lines with non-content lines blanked (numbering preserved).
        links: 'desc' keeps link descriptions (offset-preserving mask),
               'blank' removes links entirely, 'raw' leaves them alone."""
        out = []
        for i, line in enumerate(self.lines):
            if i in self.excluded:
                out.append("")
                continue
            if links != "raw":
                line = mask_links(line, keep_desc=(links == "desc"))
            out.append(line)
        return out

    def statement_lines(self, cap: int | None = 40) -> list[str]:
        """Body before the first heading."""
        end = self.headings[0].line if self.headings else len(self.lines)
        body = [l for l in self.lines[self.body_start:end]]
        while body and not body[-1].strip():
            body.pop()
        while body and not body[0].strip():
            body.pop(0)
        if cap and len(body) > cap:
            body = body[:cap] + [f"… (+{len(body) - cap} строк)"]
        return body

    def body_chars(self) -> int:
        return len("".join(l.strip() for i, l in enumerate(self.lines) if i not in self.excluded
                           and not HEADING_RE.match(l)))

    def body_hash(self) -> str:
        skip: set[int] = set()
        for a, b in self.noexport_ranges():
            skip.update(range(a, b))
        kept = []
        for i, line in enumerate(self.lines):
            if i in skip:
                continue
            if re.match(r"^#\+property:\s*ANKI_", line, re.I):
                continue
            kept.append(line.rstrip())
        while kept and not kept[-1]:
            kept.pop()
        return sha("\n".join(kept))

    def file_hash(self) -> str:
        return sha(self.raw, 12)

    # -- cards --------------------------------------------------------------
    def cards(self) -> list[dict]:
        out = []
        for h in self.headings:
            if "ANKI_NOTE_TYPE" not in h.props:
                continue
            fields = []
            for c in self.headings:
                if c.parent is h:
                    text = "\n".join(self.lines[c.body_start:c.end]).strip("\n")
                    fields.append({"name": c.title, "text": text, "line": c.body_start + 1})
            deck = h.inherited("ANKI_DECK") or self.file_props.get("ANKI_DECK")
            out.append({"label": h.title, "type": h.props["ANKI_NOTE_TYPE"], "line": h.line + 1,
                        "deck": deck, "noexport": h.noexport(),
                        "note_id": h.props.get("ANKI_NOTE_ID"),
                        "failure": h.props.get("ANKI_FAILURE_REASON"),
                        "fields": fields})
        return out


def mask_links(line: str, keep_desc: bool) -> str:
    def repl(m: re.Match) -> str:
        whole = m.group(0)
        if keep_desc and m.group(2) is not None:
            a, b = m.start(2) - m.start(0), m.end(2) - m.start(0)
            return " " * a + whole[a:b] + " " * (len(whole) - b)
        return " " * len(whole)
    return LINK_RE.sub(repl, line)


class Vault:
    def __init__(self):
        self.cfg = load_config()
        self.state = load_state()
        self.notes: list[Note] = []
        for p in sorted(VAULT.glob("*.org")):
            if p.name.startswith("theme-") or p.name.startswith(".") or p.name.startswith("#"):
                continue
            self.notes.append(Note(p))
        self.by_id = {}
        for n in self.notes:
            if n.id:
                self.by_id.setdefault(n.id, n)
        v = self.cfg["vault"]
        self.workflow_tags = set(v.get("workflow_tags", []))
        self.type_tags = set(v.get("type_tags", []))
        self.courses: dict = self.cfg["courses"]
        self.stub_chars = int(v.get("stub_chars", 40))

    # -- classification ---------------------------------------------------
    def is_stub(self, n: Note) -> bool:
        if "backfill" in n.tags:
            return True
        return self.looks_stub(n)

    def looks_stub(self, n: Note) -> bool:
        body = " ".join(l.strip() for i, l in enumerate(n.lines) if i not in n.excluded).strip()
        return len(body) < self.stub_chars or body.lower().startswith("backfill")

    def course_of(self, n: Note) -> str | None:
        for t in n.tags:
            if t in self.courses:
                return t
        return None

    def status(self, n: Note) -> str:
        if self.is_stub(n):
            return "stub"
        st = self.state["notes"].get(n.id, {}).get("audited")
        if not st:
            return "pending"
        return "audited" if st.get("body_hash") == n.body_hash() else "pending"

    def pending_reason(self, n: Note) -> str | None:
        st = self.state["notes"].get(n.id, {}).get("audited")
        if not st:
            return "new"
        return None if st.get("body_hash") == n.body_hash() else "changed"

    def backlinks(self, n: Note) -> list[tuple[Note, Link]]:
        out = []
        for other in self.notes:
            if other is n:
                continue
            for l in other.content_links():
                if l.id == n.id:
                    out.append((other, l))
        return out

    # -- refs -------------------------------------------------------------
    def resolve(self, ref: str) -> Note:
        r = nfc(ref).strip()
        up = r.upper()
        if up in self.by_id:
            return self.by_id[up]
        cands: list[Note] = []
        if re.fullmatch(r"[0-9A-Fa-f-]{4,}", r):
            cands = [n for n in self.notes if n.id.startswith(up)]
            if len(cands) == 1:
                return cands[0]
        base = nfc(Path(r).name)
        for n in self.notes:
            if nfc(n.path.name) == base:
                return n
        exact = [n for n in self.notes if n.title.casefold() == r.casefold()]
        if len(exact) == 1:
            return exact[0]
        cands = [n for n in self.notes
                 if r.casefold() in n.title.casefold() or r.casefold() in nfc(n.path.name).casefold()]
        if len(cands) == 1:
            return cands[0]
        if not cands:
            die(f"заметка не найдена: {ref}")
        die("неоднозначная ссылка «%s»:\n%s" % (
            ref, "\n".join(f"  {n.id[:8]}  {n.title}" for n in cands)))

    def resolve_many(self, refs: list[str]) -> list[Note]:
        if not refs:
            return list(self.notes)
        out, seen = [], set()
        for r in refs:
            n = self.resolve(r)
            if n.id not in seen:
                seen.add(n.id)
                out.append(n)
        return out


def die(msg: str, code: int = 2):
    print(f"roam.py: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# math scanner
# --------------------------------------------------------------------------

MATH_ENVS = {"equation", "align", "gather", "multline", "alignat", "flalign",
             "eqnarray", "displaymath", "math"}
BEGIN_RE = re.compile(r"\\begin\{([A-Za-z]+\*?)\}")


@dataclass
class Frag:
    kind: str         # inline | paren | display | env | ddollar
    start: int
    end: int
    inner: str
    inner_start: int


def scan_math(text: str) -> tuple[list[Frag], list[tuple[str, int, str]]]:
    """Return (fragments, problems); problem = (rule, offset, message)."""
    frags: list[Frag] = []
    problems: list[tuple[str, int, str]] = []
    dollars: list[int] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            two = text[i:i + 2]
            if two == "\\[":
                j = text.find("\\]", i + 2)
                if j < 0:
                    problems.append(("M02", i, "нет закрывающей \\]"))
                    i += 2
                    continue
                frags.append(Frag("display", i, j + 2, text[i + 2:j], i + 2))
                i = j + 2
                continue
            if two == "\\(":
                j = text.find("\\)", i + 2)
                if j < 0:
                    problems.append(("M02", i, "нет закрывающей \\)"))
                    i += 2
                    continue
                frags.append(Frag("paren", i, j + 2, text[i + 2:j], i + 2))
                i = j + 2
                continue
            if two in ("\\]", "\\)"):
                problems.append(("M02", i, f"закрывающая {two} без открывающей"))
                i += 2
                continue
            m = BEGIN_RE.match(text, i)
            if m and m.group(1).rstrip("*") in MATH_ENVS:
                close = "\\end{%s}" % m.group(1)
                j = text.find(close, m.end())
                if j < 0:
                    problems.append(("M02", i, f"нет {close}"))
                    i = m.end()
                    continue
                frags.append(Frag("env", i, j + len(close), text[m.end():j], m.end()))
                i = j + len(close)
                continue
            i += 2
            continue
        if c == "$":
            if text.startswith("$$", i):
                j = text.find("$$", i + 2)
                if j < 0:
                    problems.append(("M01", i, "нет закрывающих $$"))
                    i += 2
                    continue
                frags.append(Frag("ddollar", i, j + 2, text[i + 2:j], i + 2))
                i = j + 2
                continue
            dollars.append(i)
        i += 1

    # pair single dollars inside paragraphs
    para_breaks = [m.start() for m in re.finditer(r"\n[ \t]*\n", text)]
    groups: dict[int, list[int]] = {}
    for d in dollars:
        groups.setdefault(bisect.bisect_right(para_breaks, d), []).append(d)
    for ds in groups.values():
        if len(ds) % 2:
            # blame the lines holding an odd number of dollars
            per_line: dict[int, list[int]] = {}
            for d in ds:
                per_line.setdefault(text.count("\n", 0, d), []).append(d)
            for ln, lst in per_line.items():
                if len(lst) % 2:
                    problems.append(("M01", lst[-1], "нечётное число $ в строке/абзаце"))
            ds = ds[:-1]
        for a, b in zip(ds[0::2], ds[1::2]):
            frags.append(Frag("inline", a, b + 1, text[a + 1:b], a + 1))
    frags.sort(key=lambda f: f.start)
    return frags, problems


def blank_ranges(text: str, frags: list[Frag], ch: str = " ") -> str:
    out = list(text)
    for f in frags:
        for k in range(f.start, f.end):
            if out[k] != "\n":
                out[k] = ch
    return "".join(out)


def strip_groups(s: str, commands: tuple[str, ...]) -> str:
    """Blank balanced {…} groups following any of COMMANDS (offset-preserving)."""
    out = list(s)
    for m in re.finditer(r"\\(%s)\s*\{" % "|".join(commands), s):
        depth, k = 1, m.end()
        while k < len(s) and depth:
            if s[k] == "\\":
                k += 2
                continue
            if s[k] == "{":
                depth += 1
            elif s[k] == "}":
                depth -= 1
            k += 1
        for t in range(m.start(), min(k, len(s))):
            if out[t] != "\n":
                out[t] = " "
    return "".join(out)


CYR_RE = re.compile(r"[А-Яа-яЁё]+")

STANDARD_COMMANDS = set("""
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa varkappa lambda mu nu xi
pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi
Sigma Upsilon Phi Psi Omega digamma aleph beth hbar ell wp Re Im partial nabla infty emptyset varnothing
forall exists nexists neg lnot land lor wedge vee cap cup bigcap bigcup bigsqcup bigvee bigwedge bigoplus
bigotimes bigodot biguplus sqcap sqcup uplus setminus smallsetminus complement in notin ni subset supset
subseteq supseteq subsetneq supsetneq nsubseteq nsupseteq sqsubset sqsupset sqsubseteq sqsupseteq
le leq ge geq leqslant geqslant ll gg neq ne equiv approx sim simeq cong propto asymp doteq prec succ
preceq succeq parallel perp mid nmid models vdash dashv top bot angle measuredangle triangle square
to gets mapsto longmapsto rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow
longrightarrow longleftarrow longleftrightarrow Longrightarrow Longleftarrow Longleftrightarrow
hookrightarrow hookleftarrow uparrow downarrow updownarrow Uparrow Downarrow Updownarrow nearrow searrow
swarrow nwarrow rightharpoonup rightharpoondown leftharpoonup leftharpoondown rightleftharpoons
rightrightarrows leftleftarrows rightsquigarrow leadsto implies impliedby iff nRightarrow nLeftarrow
nrightarrow nleftarrow xrightarrow xleftarrow xRightarrow xLeftarrow
pm mp times div cdot cdots ldots dots vdots ddots dotsc dotsb dotsm ast star circ bullet oplus ominus
otimes oslash odot dagger ddagger amalg diamond wr bigcirc triangleleft triangleright lhd rhd
sum prod coprod int iint iiint oint limits nolimits lim limsup liminf sup inf max min arg argmax argmin
sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth ln log lg exp det dim ker deg gcd hom
Pr mod bmod pmod pod operatorname DeclareMathOperator
frac dfrac tfrac cfrac binom dbinom tbinom sqrt root over choose stackrel overset underset substack
overline underline widehat widetilde hat tilde bar vec dot ddot dddot acute grave breve check mathring
overbrace underbrace overrightarrow overleftarrow overleftrightarrow underrightarrow
left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr bigm Bigm biggm Biggm
langle rangle lvert rvert lVert rVert vert Vert lfloor rfloor lceil rceil lbrace rbrace lbrack rbrack
backslash lgroup rgroup ulcorner urcorner llcorner lrcorner
mathbb mathbf mathcal mathfrak mathit mathrm mathsf mathtt mathscr mathds mathbbm mathnormal boldsymbol
bm pmb text textrm textit textbf textsf texttt textup textnormal mbox hbox emph intertext shortintertext
displaystyle textstyle scriptstyle scriptscriptstyle
quad qquad enspace enskip thinspace medspace thickspace negthinspace negmedspace negthickspace hspace
vspace hfill phantom hphantom vphantom smash mathstrut strut kern mkern mskip hskip
begin end label tag notag nonumber ref eqref cite
prime backprime sharp flat natural clubsuit diamondsuit heartsuit spadesuit checkmark
therefore because blacksquare qed Box lozenge blacktriangleright blacktriangleleft
cancel bcancel xcancel cancelto centernot not
color textcolor boxed fbox colorbox
mathop mathbin mathrel mathord mathopen mathclose mathpunct mathinner
colon ldotp cdotp vartriangle triangleq coloneqq eqqcolon coloneq eqcolon
lesssim gtrsim lessapprox gtrapprox lessgtr gtrless succsim precsim
dagger S P copyright pounds
hline cline multicolumn newline tfrac allowbreak
iddots varsubsetneq varsupsetneq eth mho Finv Game Bbbk imath jmath
bigtriangleup bigtriangledown diamond circledast circledcirc circleddash boxplus boxminus boxtimes boxdot
dotplus ltimes rtimes leftthreetimes rightthreetimes curlywedge curlyvee barwedge veebar
upharpoonleft upharpoonright downharpoonleft downharpoonright
gtrdot lessdot lll ggg between pitchfork backsim backsimeq eqsim approxeq thicksim thickapprox
varlimsup varliminf varinjlim varprojlim injlim projlim
lt gt
""".split())


def known_commands(macros: dict | None = None) -> set[str]:
    known = set(STANDARD_COMMANDS)
    if KNOWN_MACROS_PATH.exists():
        for line in KNOWN_MACROS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip().lstrip("\\")
            if line:
                known.add(line)
    for name in (macros or {}):
        known.add(name.lstrip("\\"))
    return known


# --------------------------------------------------------------------------
# macros (my/notes-latex-macros из my-org.el)
# --------------------------------------------------------------------------

_NOTEDEF_RE = re.compile(r"^\\notedef\{\\([A-Za-z]+)\}(?:\[(\d)\])?\{(.*)\}$")
_DMO_RE = re.compile(r"^\\DeclareMathOperator\*?\{\\([A-Za-z]+)\}\{(.*)\}$")


def elisp_unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s, flags=re.S)


def parse_macros_source(src: str) -> dict[str, dict]:
    start = src.find("(defvar my/notes-latex-macros")
    if start < 0:
        return {}
    nxt = re.search(r"^\((defun|defvar|defmacro|use-package)\b", src[start + 10:], re.M)
    region = src[start: start + 10 + nxt.start()] if nxt else src[start:]
    # drop ;; comments so a quote inside a comment cannot open a string
    region = "\n".join(re.sub(r"^\s*;.*$", "", l) for l in region.split("\n"))
    macros: dict[str, dict] = {}
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', region, re.S):
        s = elisp_unescape(m.group(1)).strip()
        d = _NOTEDEF_RE.match(s)
        if d:
            macros["\\" + d.group(1)] = {"args": int(d.group(2) or 0), "body": d.group(3)}
            continue
        o = _DMO_RE.match(s)
        if o:
            macros["\\" + o.group(1)] = {"args": 0, "body": "\\operatorname{%s}" % o.group(2)}
    return macros


def parse_macros_tex(src: str) -> dict[str, dict]:
    """Тот же набор из handbook/common/macros.tex (строки \\notedef / \\DeclareMathOperator)."""
    macros: dict[str, dict] = {}
    for line in src.splitlines():
        s = line.strip()
        d = _NOTEDEF_RE.match(s)
        if d:
            macros["\\" + d.group(1)] = {"args": int(d.group(2) or 0), "body": d.group(3)}
            continue
        o = _DMO_RE.match(s)
        if o:
            macros["\\" + o.group(1)] = {"args": 0, "body": "\\operatorname{%s}" % o.group(2)}
    return macros


def load_macros(cfg: dict) -> dict[str, dict]:
    """Макросы из my-org.el; без Emacs (CI) -- из handbook/common/macros.tex."""
    p = Path(cfg["vault"].get("emacs_macros", "")).expanduser()
    if p.is_file():
        return parse_macros_source(p.read_text(encoding="utf-8"))
    fallback = VAULT / "handbook" / "common" / "macros.tex"
    if fallback.is_file():
        return parse_macros_tex(fallback.read_text(encoding="utf-8"))
    return {}


def _read_arg(s: str, i: int) -> tuple[str, int]:
    while i < len(s) and s[i] in " \t\n":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] == "{":
        depth, k = 1, i + 1
        while k < len(s) and depth:
            if s[k] == "\\":
                k += 2
                continue
            if s[k] == "{":
                depth += 1
            elif s[k] == "}":
                depth -= 1
            k += 1
        return s[i + 1:k - 1], k
    m = re.compile(r"\\([A-Za-z]+|.)").match(s, i)
    if m:
        return m.group(0), m.end()
    return s[i], i + 1


def expand_once(s: str, macros: dict[str, dict]) -> str:
    out, i = [], 0
    cmd = re.compile(r"\\([A-Za-z]+)")
    while i < len(s):
        if s[i] != "\\":
            out.append(s[i])
            i += 1
            continue
        m = cmd.match(s, i)
        if not m:
            out.append(s[i:i + 2])
            i += 2
            continue
        name = m.group(0)
        mac = macros.get(name)
        if not mac:
            out.append(name)
            i = m.end()
            continue
        i = m.end()
        args = []
        for _ in range(mac["args"]):
            a, i = _read_arg(s, i)
            args.append(a)
        body = mac["body"]
        for k, a in enumerate(args, 1):
            body = body.replace("#%d" % k, a)
        out.append(body)
        # keep a separator when the expansion ends in a letter and text follows
        if i < len(s) and s[i].isalpha() and body[-1:].isalpha():
            out.append(" ")
    return "".join(out)


def expand_macros(s: str, macros: dict[str, dict], rounds: int = 12) -> str:
    for _ in range(rounds):
        new = expand_once(s, macros)
        if new == s:
            break
        s = new
    return s


def cardify(s: str, macros: dict[str, dict]) -> str:
    """Prepare text for an Anki field: macros expanded, MathJax-safe."""
    s = expand_macros(s, macros)
    s = re.sub(r"\\mathds\b", r"\\mathbb", s)
    frags, _ = scan_math(s)
    for f in reversed(frags):
        inner = re.sub(r"[ \t]*<[ \t]*", r" \\lt ", f.inner)
        inner = re.sub(r"[ \t]*>[ \t]*", r" \\gt ", inner)
        if f.kind == "inline":
            rep = "\\(" + inner + "\\)"
        elif f.kind == "ddollar":
            rep = "\\[" + inner + "\\]"
        else:
            rep = s[f.start:f.inner_start] + inner + s[f.inner_start + len(f.inner):f.end]
        s = s[:f.start] + rep + s[f.end:]
    return s


# --------------------------------------------------------------------------
# lint
# --------------------------------------------------------------------------

@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    id: str
    line: int
    snippet: str
    message: str
    fixable: bool = False

    @property
    def fp(self) -> str:
        norm = re.sub(r"\s+", " ", self.snippet).strip()
        return hashlib.sha1(f"{self.id or self.file}|{self.rule}|{norm}".encode()).hexdigest()[:10]

    def as_dict(self) -> dict:
        return {"fp": self.fp, "rule": self.rule, "severity": self.severity, "file": self.file,
                "id": self.id, "line": self.line, "snippet": self.snippet,
                "message": self.message, "fixable": self.fixable}


def _snip(s: str, n: int = 90) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


class Linter:
    def __init__(self, vault: Vault):
        self.v = vault
        self.macros = load_macros(vault.cfg)
        self.known = known_commands(self.macros)
        self.out: list[Finding] = []

    def add(self, n: Note | None, rule, sev, line, snippet, message, fixable=False, file=None):
        self.out.append(Finding(rule, sev, file or (n.rel if n else ""), n.id if n else "",
                                line, _snip(snippet), message, fixable))

    # -- per note -----------------------------------------------------------
    def lint_note(self, n: Note) -> None:
        self._header(n)
        self._tags(n)
        self._links(n)
        self._math_and_text(n)
        self._cards(n)

    def _header(self, n: Note) -> None:
        L = n.lines
        if n.drawer_end < 0 or not n.id:
            self.add(n, "H01", "E", 1, L[0] if L else "", "нет property drawer с :ID: в начале файла")
        elif not UUID_RE.match(n.id) or n.props.get("ID") != n.id:
            self.add(n, "H01", "E", 2, n.props.get("ID", ""), ":ID: не является UUID в верхнем регистре")
        tl = n.kw_line("title")
        if tl is None or not n.title:
            self.add(n, "H03", "E", 1, "", "нет #+title")
        elif tl != n.drawer_end + 1:
            self.add(n, "H03", "E", tl + 1, L[tl], "#+title не сразу после :END:")
        if n.keywords:
            first, last = n.keywords[0][0], n.keywords[-1][0]
            blanks = [i for i in range(first, last) if not L[i].strip()]
            if blanks:
                self.add(n, "H04", "M", blanks[0] + 1, "пустая строка среди #+keywords",
                         "пустая строка между #+title и остальными keywords", fixable=True)
        if n.title and n.file_slug is not None and slugify(n.title) != n.file_slug:
            self.add(n, "H05", "I", (tl or 0) + 1, f"{n.title} ≠ {n.file_slug}",
                     f"заголовок и имя файла разошлись (ожидался slug «{slugify(n.title)}»); "
                     "чинится `git mv`, ссылки идут по ID")
        if n.title != (n.kw("title") or "") or (tl is not None and L[tl] != L[tl].rstrip()):
            pass  # trailing whitespace in the title is covered by H06
        trailing = [i for i, l in enumerate(L) if l != l.rstrip()]
        if trailing:
            self.add(n, "H06", "M", trailing[0] + 1, f"хвостовые пробелы ×{len(trailing)}",
                     "хвостовые пробелы в строках: " + ", ".join(str(i + 1) for i in trailing[:12]),
                     fixable=True)
        if not n.raw.endswith("\n"):
            self.add(n, "H06", "M", len(L), "нет перевода строки в конце файла",
                     "файл не заканчивается переводом строки", fixable=True)
        else:
            k = len(L) - 1          # L[-1] == "" is the final newline
            extra = 0
            while k - 1 - extra >= 0 and not L[k - 1 - extra].strip():
                extra += 1
            if extra:
                self.add(n, "H06", "M", len(L) - 1 - extra + 1, f"пустые строки в конце ×{extra}",
                         "лишние пустые строки в конце файла", fixable=True)

    def _tags(self, n: Note) -> None:
        v = self.v
        ln = (n.kw_line("filetags") or 0) + 1
        tags = n.tags
        raw = n.kw("filetags")
        if not tags:
            self.add(n, "T01", "W", ln, "#+filetags: " + (raw or ""), "пустые filetags")
        else:
            subject = [t for t in tags if t not in v.workflow_tags]
            course = v.course_of(n)
            if subject and subject[0] not in v.courses:
                if course:
                    self.add(n, "T02", "W", ln, "#+filetags: " + (raw or ""),
                             f"первый не-workflow тег «{subject[0]}», а не курс «{course}»: "
                             f"дека Anki получится Notes::{subject[0]}", fixable=True)
                else:
                    self.add(n, "T02", "W", ln, "#+filetags: " + (raw or ""),
                             "нет тега курса (зарегистрированы: %s)" % ", ".join(v.courses))
            if not any(t in v.type_tags for t in tags) and not (set(tags) & {"lecture", "hub", "image"}):
                self.add(n, "T03", "I", ln, "#+filetags: " + (raw or ""), "нет тега типа (definition/theorem/…)")
            for t in tags:
                if t not in v.workflow_tags and t not in v.type_tags and t not in v.courses:
                    self.add(n, "T05", "I", ln, t, f"тег «{t}» не зарегистрирован в roam.toml")
        looks = v.looks_stub(n)
        if looks and "backfill" not in tags:
            self.add(n, "T04", "W", ln, "#+filetags: " + (raw or ""), "заглушка без тега :backfill:")
        if not looks and "backfill" in tags:
            self.add(n, "T04", "W", ln, "#+filetags: " + (raw or ""), ":backfill: на заметке с реальным телом")
        if "lecture" in tags and (not n.date or not v.course_of(n)):
            self.add(n, "C01", "W", ln, "#+filetags: " + (raw or ""), "lecture-заметка без #+date или без тега курса")

    def _links(self, n: Note) -> None:
        v = self.v
        links = n.content_links()
        for l in links:
            src = n.lines[l.line][l.start:l.end]
            if l.id:
                if l.id not in v.by_id:
                    self.add(n, "K01", "E", l.line + 1, src, "битая id-ссылка")
                elif l.id == n.id:
                    self.add(n, "K07", "I", l.line + 1, src, "ссылка на саму себя")
                if l.desc is None:
                    self.add(n, "K06", "I", l.line + 1, src, "ссылка без описания")
            elif l.target.lower().startswith("file:") or l.target.startswith(("./", "../", "/")):
                target = re.sub(r"^file:", "", l.target, flags=re.I).split("::")[0]
                p = (n.path.parent / Path(target).expanduser()).resolve() if not target.startswith(("/", "~")) \
                    else Path(target).expanduser()
                if not p.exists():
                    self.add(n, "K05", "E", l.line + 1, src, "файл по ссылке не существует")
        exempt = v.is_stub(n) or bool(set(n.tags) & {"lecture", "hub", "image"})
        if not exempt and not n.out_ids():
            if not v.backlinks(n):
                self.add(n, "K03", "I", 1, n.title, "изолированная заметка: нет ни исходящих, ни входящих ссылок")
            else:
                self.add(n, "K02", "I", 1, n.title, "нет исходящих ссылок")

    def _math_and_text(self, n: Note) -> None:
        lines = n.content_lines(links="desc")
        text = "\n".join(lines)
        starts = [0]
        for l in lines:
            starts.append(starts[-1] + len(l) + 1)

        def line_of(off: int) -> int:
            return bisect.bisect_right(starts, off)

        frags, problems = scan_math(text)
        for rule, off, msg in problems:
            ln = line_of(off)
            self.add(n, rule, "E", ln, n.lines[ln - 1], msg)

        for f in frags:
            src = text[f.start:f.end]
            ln = line_of(f.start)
            inner = f.inner
            if not inner.strip():
                self.add(n, "M05", "W", ln, src, "пустая формула")
                continue
            # M03 braces
            depth, bad = 0, False
            for m in re.finditer(r"\\.|[{}]", inner, re.S):
                if m.group(0) == "{":
                    depth += 1
                elif m.group(0) == "}":
                    depth -= 1
                    if depth < 0:
                        bad = True
                        break
            if bad or depth:
                self.add(n, "M03", "E", ln, src, "несбалансированные { } в формуле")
            # M04 \left / \right
            nl = len(re.findall(r"\\left(?![A-Za-z])", inner))
            nr = len(re.findall(r"\\right(?![A-Za-z])", inner))
            if nl != nr:
                self.add(n, "M04", "E", ln, src, f"\\left ×{nl} ≠ \\right ×{nr}")
            # M06 unknown commands
            body = re.sub(r"\\(begin|end)\{[^}]*\}", " ", inner)
            for m in re.finditer(r"\\([A-Za-z]+)", body):
                if m.group(1) not in self.known:
                    self.add(n, "M06", "W", line_of(f.inner_start + m.start()), m.group(0),
                             f"неизвестная команда {m.group(0)} (опечатка? иначе добавить в tools/known_macros.txt)")
            # M07 cyrillic in math
            stripped = strip_groups(inner, ("text", "textrm", "textit", "textbf", "textnormal", "mbox", "intertext"))
            for m in CYR_RE.finditer(stripped):
                before = stripped[max(0, m.start() - 40):m.start()]
                inside = re.search(r"\\(texttt|mathrm|operatorname|mathit|mathbf)\s*\{[^{}]*$", before)
                where = f"внутри \\{inside.group(1)}{{}}" if inside else "вне \\text{}"
                self.add(n, "M07", "W", line_of(f.inner_start + m.start()),
                         stripped[max(0, m.start() - 12):m.end() + 12],
                         f"кириллица в формуле {where}: сломается в MathJax (Anki/HTML); используй \\text{{…}}")
                break
            # M08 / M12 org parsing of $…$
            if f.kind == "inline":
                why = None
                if inner[0] in " \t\n,.;":
                    why = "после открывающего $ идёт пробел или знак препинания"
                elif inner[-1] in " \t\n,.":
                    why = "перед закрывающим $ стоит пробел или знак препинания"
                elif f.end < len(text) and (text[f.end].isalnum() or text[f.end] == "$"):
                    why = "сразу после закрывающего $ идёт буква/цифра"
                elif inner.count("\n") > 2:
                    why = "больше двух переводов строки внутри $…$"
                if why:
                    self.add(n, "M08", "W", ln, text[max(0, f.start - 3):f.end + 3],
                             f"Org не распознает это как формулу: {why}")
                if f.start > 0 and text[f.start - 1].isalnum():
                    self.add(n, "M12", "W", ln, text[max(0, f.start - 6):f.start + 14],
                             "буква/цифра вплотную перед открывающим $ (опечатка?)")
            # M09 \begin at bol inside \[ \]
            if f.kind == "display" and re.search(r"(?m)^[ \t]*\\begin\{", inner):
                self.add(n, "M09", "W", ln, src, "\\begin{…} в начале строки внутри \\[ \\]: Org разорвёт фрагмент")
            # M10 \prime
            for m in re.finditer(r"\\prime(?![A-Za-z])", inner):
                pre = inner[:m.start()].rstrip()
                if not (pre.endswith("^") or pre.endswith("^{") or re.search(r"\^\{(\\prime\s*)+$", pre)):
                    self.add(n, "M10", "W", line_of(f.inner_start + m.start()),
                             inner[max(0, m.start() - 8):m.end() + 6], "\\prime не под ^ (нужно ^{\\prime} или ')")
            # M11 parens
            p = re.sub(r"\\.", "  ", inner)
            p = re.sub(r"\[[^\[\]()]*[;,][^\[\]()]*\)|\([^\[\]()]*[;,][^\[\]()]*\]", "", p)
            if p.count("(") != p.count(")"):
                self.add(n, "M11", "I", ln, src, f"скобки ( ×{p.count('(')} ≠ ) ×{p.count(')')}")

        # prose rules
        prose = blank_ranges(text, frags, " ").split("\n")
        solid = blank_ranges(text, frags, "§").split("\n")
        for i, pl in enumerate(prose):
            for m in re.finditer(r"[A-Za-zА-Яа-яЁё]+", pl):
                w = m.group(0)
                if re.search(r"[A-Za-z]", w) and re.search(r"[А-Яа-яЁё]", w):
                    marked = "".join(f"[{c}]" if re.match(r"[A-Za-z]", c) else c for c in w)
                    self.add(n, "X01", "W", i + 1, w, f"латиница внутри кириллического слова: {marked}")
        for i, sl in enumerate(solid):
            if not sl.strip() or HEADING_RE.match(n.lines[i]):
                continue
            # raw line: masked links would look like extra spaces after the marker
            if re.match(r"^\s*(?:[-+]|\d+[.)])\s{2,}\S", n.lines[i]):
                self.add(n, "X02", "M", i + 1, n.lines[i], "двойной пробел после маркера списка", fixable=True)
            m = re.search(r"\S ([,.;:!?])(?=\s|$)", sl)
            if m and not re.search(r"\s--\s*$", sl[:m.start() + 1]):
                self.add(n, "X02", "M", i + 1, n.lines[i][max(0, m.start() - 15):m.end() + 5],
                         f"пробел перед «{m.group(1)}»")
        # title is prose too
        for m in re.finditer(r"[A-Za-zА-Яа-яЁё]+", n.title):
            w = m.group(0)
            if re.search(r"[A-Za-z]", w) and re.search(r"[А-Яа-яЁё]", w):
                self.add(n, "X01", "W", (n.kw_line("title") or 0) + 1, w, "латиница внутри кириллического слова в заголовке")

    def _cards(self, n: Note) -> None:
        cards = n.cards()
        if not cards:
            return
        v = self.v
        anki = v.cfg.get("anki", {})
        by_type = {a["type"]: a for a in anki.values() if "type" in a}
        course = v.course_of(n)
        want_deck = v.courses.get(course, {}).get("deck") if course else None
        st = v.status(n)
        if st != "audited":
            self.add(n, "A07", "I", cards[0]["line"], cards[0]["label"],
                     "карточки на непроверенной/изменённой заметке" if st == "pending" else "карточки на заглушке")
        for c in cards:
            ln = c["line"]
            spec = by_type.get(c["type"])
            names = [f["name"] for f in c["fields"]]
            if spec and names != spec["fields"]:
                self.add(n, "A01", "E", ln, c["label"],
                         f"поля {names} ≠ ожидаемым {spec['fields']} для типа {c['type']}")
            if not spec:
                self.add(n, "A01", "I", ln, c["label"], f"тип «{c['type']}» не описан в roam.toml [anki.*]")
            alltext = "\n".join(f["text"] for f in c["fields"])
            is_cloze = bool(spec) and spec is anki.get("cloze") or c["type"].lower().startswith("cloze")
            if is_cloze and not re.search(r"\{\{c\d+::", alltext):
                self.add(n, "A01", "E", ln, c["label"], "Cloze-карточка без {{cN::…}}")
            if not c["deck"]:
                self.add(n, "A03", "W", ln, c["label"], "нет ANKI_DECK (ни на родителе, ни в #+property)")
            elif want_deck and c["deck"] != want_deck:
                self.add(n, "A03", "W", ln, c["label"], f"дека «{c['deck']}» ≠ деке курса «{want_deck}»")
            if not c["noexport"]:
                self.add(n, "A04", "W", ln, c["label"], "карточка не под :noexport:")
            if c["failure"]:
                self.add(n, "A05", "E", ln, c["label"], f"ANKI_FAILURE_REASON: {c['failure']}")
            for f in c["fields"]:
                t = f["text"]
                for name in self.macros:
                    if re.search(re.escape(name) + r"(?![A-Za-z])", t):
                        self.add(n, "A02", "W", f["line"], f"{c['label']} / {f['name']}: {name}",
                                 f"макрос {name} из конфига Emacs: Anki его не знает (roam.py expand --card)")
                if re.search(r"(?<!\\)\$", t):
                    self.add(n, "A02", "W", f["line"], f"{c['label']} / {f['name']}: $",
                             "в карточке $-разделители; нужны \\(…\\) / \\[…\\]")
                if re.search(r"\[\[id:", t):
                    self.add(n, "A02", "W", f["line"], f"{c['label']} / {f['name']}: id-ссылка",
                             "id-ссылка в карточке (в Anki она мёртвая)")
                frs, _ = scan_math(t)
                for fr in frs:
                    inner = re.sub(r"\\(lt|gt)\b", "", fr.inner)
                    if re.search(r"[<>]", inner):
                        self.add(n, "A02", "W", f["line"], f"{c['label']} / {f['name']}: {_snip(fr.inner, 40)}",
                                 "сырой < или > в формуле: anki-editor не экранирует HTML; используй \\lt / \\gt")
                for m in re.finditer(r"\{\{c\d+::", t):
                    depth, k = 2, m.end()
                    while k < len(t) and depth:
                        if t[k] == "\\":
                            k += 2
                            continue
                        depth += {"{": 1, "}": -1}.get(t[k], 0)
                        k += 1
                    first = t.find("}}", m.end())
                    if first != -1 and first + 2 < k:
                        self.add(n, "A02", "W", f["line"], f"{c['label']} / {f['name']}: {_snip(t[m.start():k], 50)}",
                                 "внутри cloze встречается «}}» раньше его конца: Anki закроет cloze досрочно (разбей «} }»)")

    # -- vault level ----------------------------------------------------------
    def lint_vault(self) -> None:
        seen: dict[str, Note] = {}
        for n in self.v.notes:
            if n.id and n.id in seen:
                self.add(n, "H02", "E", 2, n.id, f"дубль ID (также в {seen[n.id].rel})")
            seen.setdefault(n.id, n)
        img_dir = VAULT / "images"
        if img_dir.is_dir():
            blob = "\n".join(nfc(n.raw) for n in self.v.notes)
            for p in sorted(img_dir.iterdir()):
                if p.is_file() and not p.name.startswith(".") and nfc(p.name) not in blob:
                    self.add(None, "K04", "I", 0, p.name, "картинка не используется ни в одной заметке",
                             file=nfc(str(p.relative_to(VAULT))))
        firsts: dict[str, tuple[Note, dict]] = {}
        for n in self.v.notes:
            for c in n.cards():
                if not c["fields"]:
                    continue
                key = re.sub(r"\s+", " ", c["fields"][0]["text"]).strip()
                if key in firsts and key:
                    self.add(n, "A06", "W", c["line"], c["label"],
                             f"первое поле дублирует карточку в {firsts[key][0].rel}")
                firsts.setdefault(key, (n, c))

    def run(self, notes: list[Note], whole_vault: bool, rules: list[str] | None) -> list[Finding]:
        for n in notes:
            self.lint_note(n)
        if whole_vault:
            self.lint_vault()
        res = self.out
        if rules:
            pats = [re.compile("^" + r.strip().replace("*", ".*") + "$") for r in rules if r.strip()]
            res = [f for f in res if any(p.match(f.rule) for p in pats)]
        dismissed = {d.get("fp") for st in self.v.state["notes"].values() for d in st.get("dismissed", [])}
        res = [f for f in res if f.fp not in dismissed]
        res.sort(key=lambda f: (f.file, f.line, SEVERITY_ORDER.get(f.severity, 9), f.rule))
        return res


def format_findings(findings: list[Finding], vault: Vault) -> str:
    out, cur = [], None
    for f in findings:
        if f.file != cur:
            cur = f.file
            n = vault.by_id.get(f.id)
            out.append(f"\n{f.file}" + (f" — {n.title}" if n else ""))
        fix = " [fix]" if f.fixable else ""
        out.append(f"  L{f.line:<4} {f.severity} {f.rule}{fix}  {f.message}\n         ⟨{f.snippet}⟩  fp={f.fp}")
    return "\n".join(out).lstrip("\n")


# --------------------------------------------------------------------------
# mechanical fixes
# --------------------------------------------------------------------------

def fix_note(n: Note, rules: set[str], vault: Vault) -> str:
    L = list(n.lines)
    if "H04" in rules and n.keywords:
        first, last = n.keywords[0][0], n.keywords[-1][0]
        L = [l for i, l in enumerate(L) if not (first < i < last and not l.strip())]
    if "T02" in rules:
        course = vault.course_of(n)
        for i, l in enumerate(L):
            m = re.match(r"^(#\+filetags:[ \t]*)(:.*:)[ \t]*$", l, re.I)
            if m and course:
                tags = [t for t in m.group(2).split(":") if t]
                wf = [t for t in tags if t in vault.workflow_tags]
                courses = [t for t in tags if t in vault.courses]
                rest = [t for t in tags if t not in wf and t not in courses]
                # workflow tags keep their place at the front: they do not affect the deck
                L[i] = m.group(1) + ":" + ":".join(wf + courses + rest) + ":"
                break
    if "X02" in rules:
        for i, l in enumerate(L):
            if i in n.excluded and i >= n.body_start:
                continue
            L[i] = re.sub(r"^(\s*(?:[-+]|\d+[.)]))\s{2,}(?=\S)", r"\1 ", l)
    if "H06" in rules:
        L = [l.rstrip() for l in L]
        while L and not L[-1]:
            L.pop()
        L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# mentions / spell
# --------------------------------------------------------------------------

_ENDINGS = sorted("""иями ями ами иях ией ого ему ому ыми ими его ая яя ое ее ые ие ый ий ой ую юю ых их ым им
ом ем ах ях ов ев ей ам ям ию ью а я о е ы и у ю ь й""".split(), key=len, reverse=True)


def stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    for suf in _ENDINGS:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


def mention_regex(title: str) -> re.Pattern | None:
    words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]+", title) if len(w) >= 4]
    if not words or len(words) > 4:
        return None
    parts = [re.escape(stem(w)) + r"\w*" for w in words]
    return re.compile(r"\b" + r"(?:\W+\w+){0,2}?\W+".join(parts), re.I)


def find_mentions(target: Note, source: Note, skip_if_linked: bool = True) -> list[tuple[int, str]]:
    """Lines of SOURCE mentioning TARGET's title without linking to it."""
    rx = mention_regex(target.title)
    if rx is None or target is source:
        return []
    if skip_if_linked and target.id in source.out_ids():
        return []
    out = []
    for i, line in enumerate(source.content_lines(links="blank")):
        if not line.strip():
            continue
        if target.id and ("id:" + target.id) in source.lines[i].upper().replace("ID:", "id:"):
            continue
        if rx.search(line.replace("ё", "е").replace("Ё", "Е")):
            out.append((i + 1, source.lines[i].strip()))
    return out


def prose_lines(n: Note) -> list[str]:
    lines = n.content_lines(links="desc")
    text = "\n".join(lines)
    frags, _ = scan_math(text)
    out = blank_ranges(text, frags, " ").split("\n")
    tl = n.kw_line("title")
    if tl is not None:
        out[tl] = n.title
    return out


def run_spell(notes: list[Note]) -> list[dict] | None:
    if not shutil.which("hunspell"):
        return None
    occurrences: dict[str, list[tuple[Note, int]]] = {}
    for n in notes:
        for i, line in enumerate(prose_lines(n)):
            line = re.sub(r"(?<!\w)[/*_=~]+|[/*_=~]+(?!\w)", " ", line)
            for w in re.findall(r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)*", line):
                if len(w) >= 3:
                    occurrences.setdefault(w, []).append((n, i + 1))
    if not occurrences:
        return []
    proc = subprocess.run(["hunspell", "-d", "ru_RU,en_US", "-i", "utf-8", "-l"],
                          input="\n".join(occurrences), capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stdout:
        print("roam.py: hunspell: " + proc.stderr.strip(), file=sys.stderr)
        return None
    bad = [w for w in dict.fromkeys(proc.stdout.split()) if w in occurrences]
    out = []
    for w in bad:
        for n, ln in occurrences[w]:
            out.append({"word": w, "file": n.rel, "id": n.id, "line": ln, "context": _snip(n.lines[ln - 1], 100)})
    out.sort(key=lambda d: (d["file"], d["line"]))
    return out


# --------------------------------------------------------------------------
# Emacs (через emacsclient; нужен server-start в конфиге)
# --------------------------------------------------------------------------

def emacs_eval(expr: str, timeout: int = 60) -> str | None:
    """Evaluate EXPR in the running Emacs; None when no server answers."""
    exe = shutil.which("emacsclient")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--eval", expr], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def emacs_modified_notes() -> set[str] | None:
    """Timestamp prefixes of vault notes with unsaved changes in Emacs.
    Prefixes are ASCII, so the answer survives any output encoding."""
    root = str(VAULT).replace("\\", "\\\\").replace('"', '\\"')
    expr = ('(let (out) (dolist (b (buffer-list)) (let ((f (buffer-file-name b))) '
            '(when (and f (buffer-modified-p b) '
            f'(string-prefix-p "{root}/" (file-truename f))) '
            '(push (car (split-string (file-name-nondirectory f) "-")) out)))) out)')
    res = emacs_eval(expr, timeout=10)
    if res is None:
        return None
    return set(re.findall(r'"([^"]*)"', res))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def note_meta(v: Vault, n: Note) -> dict:
    return {"id": n.id, "title": n.title, "file": n.rel, "tags": n.tags, "created": n.created,
            "date": n.date, "status": v.status(n), "links_out": len(n.out_ids()),
            "links_in": len({o.id for o, _ in v.backlinks(n)}),
            "lines": len(n.lines), "has_cards": bool(n.cards())}


def _norm_ts(s: str | None, end: bool) -> str | None:
    if not s:
        return None
    d = re.sub(r"\D", "", s)
    return d.ljust(14, "9" if end else "0")[:14]


def cmd_list(a, v: Vault):
    since, until = _norm_ts(a.since, False), _norm_ts(a.until, True)
    rows = []
    for n in v.notes:
        if a.tag and not all(t in n.tags for t in a.tag):
            continue
        c = n.created or ""
        if since and c < since or until and c > until:
            continue
        m = note_meta(v, n)
        if a.status != "all" and m["status"] != a.status:
            continue
        rows.append(m)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    for m in rows:
        print(f"{m['id'][:8]}  {m['created'] or '-':14}  {m['status']:8} out={m['links_out']:<2} in={m['links_in']:<2} "
              f"{'A' if m['has_cards'] else ' '} :{':'.join(m['tags'])}:  {m['title']}")
    print(f"-- {len(rows)} заметок")
    return 0


def cmd_pending(a, v: Vault):
    pend, stubs = [], []
    for n in v.notes:
        if set(n.tags) & {"lecture", "hub", "image"} and not a.all:
            continue
        if v.is_stub(n):
            stubs.append(n)
            continue
        r = v.pending_reason(n)
        if r:
            m = note_meta(v, n)
            m["reason"] = r
            m["last_audit"] = v.state["notes"].get(n.id, {}).get("audited", {}).get("date")
            pend.append(m)
    if a.json:
        print(json.dumps({"pending": pend, "stubs": [note_meta(v, s) for s in stubs]}, ensure_ascii=False, indent=1))
        return 0
    print(f"К проверке: {len(pend)}")
    for m in pend:
        last = f" (проверялась {m['last_audit']})" if m["last_audit"] else ""
        print(f"  {m['id'][:8]}  {m['created'] or '-':14}  {m['reason']:7} {m['lines']:>3} стр.  {m['title']}{last}")
    print(f"Заглушки (backfill): {len(stubs)}")
    for s in stubs:
        print(f"  {s.id[:8]}  {s.title}")
    return 0


def _numbered(n: Note) -> str:
    lines = n.lines[:-1] if n.lines and n.lines[-1] == "" else n.lines
    return "\n".join(f"{i + 1:>4}  {l}" for i, l in enumerate(lines))


def _meta_block(v: Vault, n: Note) -> str:
    return (f"# {n.title}\nid: {n.id}\nfile: {n.rel}\ntags: {(':' + ':'.join(n.tags) + ':') if n.tags else '(нет)'}\n"
            f"status: {v.status(n)}   file_hash: {n.file_hash()}   body_hash: {n.body_hash()}")


def cmd_show(a, v: Vault):
    n = v.resolve(a.ref)
    print(_meta_block(v, n))
    print()
    print(_numbered(n))
    return 0


def cmd_context(a, v: Vault):
    n = v.resolve(a.ref)
    P = print
    P(_meta_block(v, n))
    st = v.state["notes"].get(n.id, {})
    if st.get("audited"):
        au = st["audited"]
        P(f"last_audit: {au.get('date')} report={au.get('report')} open={au.get('open', 0)}")
    P("\n## Тело (с номерами строк)\n")
    P(_numbered(n))

    linter = Linter(v)
    linter.lint_note(n)
    findings = linter.run([], False, None)
    P("\n## Lint\n")
    P(format_findings(findings, v) or "(чисто)")
    sp = run_spell([n])
    P("\n## Spell (кандидаты hunspell — подтверждай, не доверяй слепо)\n")
    if sp is None:
        P("(hunspell недоступен)")
    else:
        P("\n".join(f"  L{d['line']:<4} {d['word']}   ⟨{d['context']}⟩" for d in sp) or "(нет)")

    P("\n## Исходящие заметки (формулировки)\n")
    out_ids = n.out_ids()
    for oid in out_ids:
        o = v.by_id.get(oid)
        if not o:
            P(f"### [БИТАЯ ССЫЛКА] {oid}\n")
            continue
        P(f"### {o.title}   :{':'.join(o.tags)}:   ({o.id[:8]}, {v.status(o)})")
        P("\n".join(o.statement_lines(None if a.full_linked else 40)) or "(пусто)")
        P("")
    if not out_ids:
        P("(нет)")

    P("\n## Бэклинки\n")
    bl = v.backlinks(n)
    for o, l in bl:
        lo, hi = max(o.body_start, l.line - 1), min(len(o.lines), l.line + 2)
        P(f"### {o.title}  ({o.id[:8]}) L{l.line + 1}")
        P("\n".join("    " + x for x in o.lines[lo:hi] if x.strip()))
    if not bl:
        P("(нет)")

    P("\n## Несвязанные упоминания этой заметки в других\n")
    any_m = False
    for o in v.notes:
        for ln, txt in find_mentions(n, o):
            any_m = True
            P(f"  {o.title} ({o.id[:8]}) L{ln}: {_snip(txt, 110)}")
    if not any_m:
        P("(нет)")

    P("\n## Кандидаты в ссылки: заметки, упомянутые здесь без ссылки\n")
    any_c = False
    linked = set(out_ids)
    for o in v.notes:
        hits = find_mentions(o, n, skip_if_linked=False)
        if hits:
            any_c = True
            mark = " (ссылка на неё уже есть в другом месте)" if o.id in linked else ""
            P(f"  → {o.title} ({o.id}){mark}")
            for ln, txt in hits[:3]:
                P(f"      L{ln}: {_snip(txt, 100)}")
    if not any_c:
        P("(нет)")

    P("\n## Ранее отклонённые находки (не поднимать снова)\n")
    dis = st.get("dismissed", [])
    for d in dis:
        if d.get("fp"):
            P(f"  lint {d.get('rule')} ⟨{d.get('snippet', '')}⟩")
        else:
            P(f"  [{d.get('category', '?')}] {d.get('text', '')}")
    if not dis:
        P("(нет)")
    return 0


def cmd_lint(a, v: Vault):
    notes = v.resolve_many(a.refs)
    findings = Linter(v).run(notes, whole_vault=not a.refs, rules=a.rules.split(",") if a.rules else None)
    if a.json:
        print(json.dumps([f.as_dict() for f in findings], ensure_ascii=False, indent=1))
    else:
        print(format_findings(findings, v) or "(чисто)")
        by = {}
        for f in findings:
            by[f.severity] = by.get(f.severity, 0) + 1
        print("\n-- " + "  ".join(f"{k}:{by[k]}" for k in sorted(by, key=SEVERITY_ORDER.get)) if by else "")
    return 1 if findings else 0


FIXABLE = {"H04", "H06", "T02", "X02"}


def cmd_fix(a, v: Vault):
    rules = set(a.rules.split(",")) if a.rules else set(FIXABLE)
    bad = rules - FIXABLE
    if bad:
        die(f"автоисправление есть только для {sorted(FIXABLE)}; не умею: {sorted(bad)}")
    if not a.refs:
        die("fix требует явного списка заметок")
    changed = 0
    for n in v.resolve_many(a.refs):
        new = fix_note(n, rules, v)
        if new == n.raw:
            continue
        changed += 1
        diff = difflib.unified_diff(n.raw.splitlines(), new.splitlines(), n.rel, n.rel, lineterm="", n=1)
        print("\n".join(diff))
        if a.apply:
            n.path.write_text(new, encoding="utf-8")
    print(f"-- {'изменено' if a.apply else 'изменилось бы (dry-run; добавь --apply)'}: {changed}")
    return 0


def cmd_spell(a, v: Vault):
    res = run_spell(v.resolve_many(a.refs))
    if res is None:
        print("hunspell недоступен — пропуск", file=sys.stderr)
        return 0
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for d in res:
            print(f"{d['file']}:{d['line']}: {d['word']}   ⟨{d['context']}⟩")
        print(f"-- {len(res)} кандидатов ({len({d['word'] for d in res})} слов)")
    return 0


def cmd_mentions(a, v: Vault):
    n = v.resolve(a.ref)
    rows = []
    for o in v.notes:
        for ln, txt in find_mentions(n, o):
            rows.append({"file": o.rel, "id": o.id, "title": o.title, "line": ln, "text": txt})
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            print(f"{r['title']} ({r['id'][:8]}) L{r['line']}: {_snip(r['text'], 120)}")
        print(f"-- {len(rows)}")
    return 0


def cmd_new(a, v: Vault):
    title = a.title.strip()
    if any(n.title.casefold() == title.casefold() for n in v.notes):
        die(f"заметка с заголовком «{title}» уже есть", 1)
    tags = [t for t in re.split(r"[,:\s]+", a.tags or "") if t]
    nid = str(uuid.uuid4()).upper()
    name = f"{dt.datetime.now():%Y%m%d%H%M%S}-{slugify(title)}.org"
    body = ""
    if a.body_file:
        body = (sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text(encoding="utf-8"))
    text = f":PROPERTIES:\n:ID:       {nid}\n:END:\n#+title: {title}\n#+filetags: " + \
           (":" + ":".join(tags) + ":" if tags else "") + "\n"
    if a.date:
        text += f"#+date: {a.date}\n"
    if body.strip():
        text += "\n" + body.strip("\n") + "\n"
    path = VAULT / name
    if a.dry_run:
        print(text)
    else:
        if path.exists():
            die(f"файл уже существует: {name}", 1)
        path.write_text(text, encoding="utf-8")
    print(json.dumps({"id": nid, "file": name, "dry_run": bool(a.dry_run)}, ensure_ascii=False))
    return 0


def cmd_macros(a, v: Vault):
    macros = load_macros(v.cfg)
    if not macros:
        die(f"макросы не найдены в {v.cfg['vault'].get('emacs_macros')}", 1)
    if a.latex:
        import roam_tex
        sys.stdout.write(roam_tex.macros_latex(v.cfg))
        return 0
    if a.mathjax:
        mj = {}
        for name, m in macros.items():
            body = re.sub(r"\\mathds\b", r"\\mathbb", m["body"])
            mj[name.lstrip("\\")] = [body, m["args"]] if m["args"] else body
        print(json.dumps({"macros": mj}, ensure_ascii=False, indent=1))
    elif a.json:
        print(json.dumps(macros, ensure_ascii=False, indent=1))
    else:
        for name, m in macros.items():
            print(f"{name}[{m['args']}] = {m['body']}")
    return 0


def cmd_expand(a, v: Vault):
    macros = load_macros(v.cfg)
    src = sys.stdin.read()
    sys.stdout.write(cardify(src, macros) if a.card else expand_macros(src, macros))
    return 0


def cmd_cards(a, v: Vault):
    if a.stale:
        rows = []
        for n in v.notes:
            st = v.state["notes"].get(n.id, {}).get("cards")
            if st and st.get("body_hash") != n.body_hash():
                rows.append({"id": n.id, "title": n.title, "file": n.rel, "cards_date": st.get("date")})
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(f"{r['id'][:8]}  {r['title']}  (карточки от {r['cards_date']})")
            print(f"-- заметок, изменившихся после генерации карточек: {len(rows)}")
        return 0
    notes = v.resolve_many(a.refs)
    if a.check:
        findings = Linter(v).run(notes, whole_vault=not a.refs, rules=["A*"])
        print(format_findings(findings, v) or "(карточки чистые)")
        return 1 if findings else 0
    rows = []
    for n in notes:
        cs = n.cards()
        if cs or a.refs:
            rows.append({"id": n.id, "title": n.title, "file": n.rel, "status": v.status(n), "cards": cs})
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    for r in rows:
        print(f"{r['title']} ({r['id'][:8]}, {r['status']}) — {len(r['cards'])} карточек")
        for c in r["cards"]:
            pushed = f"anki={c['note_id']}" if c["note_id"] else "не отправлена"
            print(f"   L{c['line']:<4} [{c['type']}] {c['label']}  ({pushed}, deck={c['deck']})")
            if a.verbose:
                for f in c["fields"]:
                    print(f"        {f['name']}: {_snip(f['text'], 140)}")
    without = [n for n in v.notes if not n.cards() and v.status(n) == "audited"]
    if not a.refs:
        print(f"-- проверенных заметок без карточек: {len(without)}")
        for n in without:
            print(f"   {n.id[:8]}  {n.title}")
    return 0


def cmd_guard(a, v: Vault):
    ok = True
    notes = v.resolve_many(a.refs)
    if a.expect_hash and len(notes) != 1:
        die("--expect-hash работает с одной заметкой")
    unsaved = emacs_modified_notes()
    if unsaved is None:
        print("(Emacs-сервер недоступен: несохранённые буферы не проверены — попроси сохранить, C-x s)")
    for n in notes:
        if unsaved and (n.created or nfc(n.path.name)) in unsaved:
            ok = False
            print(f"ОТКАЗ {n.rel}: в Emacs открыт буфер с несохранёнными изменениями")
        for side in (n.path.parent / f"#{n.path.name}#", n.path.parent / f".#{n.path.name}"):
            if side.exists() or side.is_symlink():
                ok = False
                print(f"ОТКАЗ {n.rel}: есть {side.name} — в Emacs несохранённые изменения")
        if a.expect_hash and n.file_hash() != a.expect_hash:
            ok = False
            print(f"ОТКАЗ {n.rel}: file_hash {n.file_hash()} ≠ ожидаемому {a.expect_hash} — файл менялся, перечитай его")
    if ok:
        print("ok: " + ", ".join(f"{n.id[:8]}={n.file_hash()}" for n in notes))
    return 0 if ok else 1


def cmd_sync(a, v: Vault):
    res = emacs_eval("(progn (require 'org-roam) (org-roam-db-sync) (caar (org-roam-db-query [:select (funcall count id) :from nodes])))")
    if res is None:
        print("Emacs-сервер недоступен: выполни в Emacs M-x org-roam-db-sync")
        return 1
    print(f"org-roam-db-sync: узлов в базе {res}, заметок в vault {len(v.notes)}")
    return 0


def _git_head() -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(VAULT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
        return r.stdout.strip() or None
    except OSError:
        return None


def cmd_state(a, v: Vault):
    st = v.state
    today = dt.date.today().isoformat()
    if a.action == "show":
        if a.refs:
            print(json.dumps({n.id: st["notes"].get(n.id, {}) for n in v.resolve_many(a.refs)},
                             ensure_ascii=False, indent=1))
        else:
            print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0
    if not a.refs:
        die(f"state {a.action} требует заметку")
    notes = v.resolve_many(a.refs)
    for n in notes:
        e = st["notes"].setdefault(n.id, {})
        e["title"] = n.title
        if a.action == "mark":
            e["audited"] = {"body_hash": n.body_hash(), "date": today, "commit": _git_head(),
                            "report": a.report, "open": a.open}
        elif a.action == "cards":
            e["cards"] = {"body_hash": n.body_hash(), "date": today, "count": len(n.cards())}
        elif a.action == "dismiss":
            if not a.fp and not a.text:
                die("dismiss требует --fp или --text")
            d = e.setdefault("dismissed", [])
            if a.fp:
                linter = Linter(v)
                linter.lint_note(n)
                linter.lint_vault()
                match = [f for f in linter.out if f.fp == a.fp]
                rec = {"fp": a.fp, "date": today}
                if match:
                    rec.update(rule=match[0].rule, snippet=match[0].snippet)
                if not any(x.get("fp") == a.fp for x in d):
                    d.append(rec)
            else:
                d.append({"category": a.category or "?", "text": a.text, "date": today})
    save_state(st)
    print(f"state {a.action}: " + ", ".join(n.title for n in notes))
    return 0


def _pdf_pages(v: Vault, course: str) -> tuple[dict, list[str]]:
    c = v.courses.get(course)
    if not c:
        die(f"курс «{course}» не описан в roam.toml")
    if not c.get("pdf"):
        die(f"у курса «{course}» нет pdf в roam.toml", 1)
    pdf = (VAULT / c["pdf"]).resolve()
    if not pdf.exists():
        die(f"нет файла {pdf}", 1)
    if not shutil.which("pdftotext"):
        die("pdftotext не установлен (brew install poppler)", 1)
    r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True)
    pages = r.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return c, pages


def cmd_pdfmap(a, v: Vault):
    c, pages = _pdf_pages(v, a.course)
    rx = re.compile(c.get("lecture_header_re", r"^\s*Пара (\d+)\.\s+(.+?)\s{2,}"))
    off = int(c.get("page_offset", 0))
    lectures: list[dict] = []
    for p, text in enumerate(pages, 1):
        head = [l for l in text.split("\n") if l.strip()][:3]
        for l in head:
            m = rx.match(l)
            if m:
                num, title = int(m.group(1)), m.group(2).strip()
                if lectures and lectures[-1]["lecture"] == num:
                    lectures[-1]["pages"][1] = p
                else:
                    lectures.append({"lecture": num, "title": title, "pages": [p, p]})
                break
    for l in lectures:
        l["logical_pages"] = [l["pages"][0] - off, l["pages"][1] - off]
    if a.json:
        print(json.dumps(lectures, ensure_ascii=False, indent=1))
    else:
        print(f"{c['pdf']} — {len(pages)} стр. (физ. = лог. + {off}); для Read используй физические")
        for l in lectures:
            print(f"  Пара {l['lecture']:>2}: физ. {l['pages'][0]}–{l['pages'][1]}  "
                  f"(лог. {l['logical_pages'][0]}–{l['logical_pages'][1]})  {l['title']}")
    return 0


def cmd_grep_pdf(a, v: Vault):
    _, pages = _pdf_pages(v, a.course)
    rx = re.compile(a.regex, re.I)
    hits = 0
    for p, text in enumerate(pages, 1):
        for l in text.split("\n"):
            if rx.search(l):
                hits += 1
                print(f"p{p}: {_snip(l, 140)}")
    print(f"-- {hits} (страницы физические; формулы в pdftotext искажены — читай страницу через Read pages)")
    return 0 if hits else 1


def cmd_handbook(a, v: Vault):
    import roam_tex
    if a.action == "plan":
        c = roam_tex.course_plan(v, a.course)
        if a.json:
            print(json.dumps({
                "title": c.title, "exported": sorted(c.exported),
                "lectures": [{"number": p.number, "title": p.title, "date": p.date, "file": p.note.rel,
                              "sections": [{"title": s.title, "ref": s.ref,
                                            "items": [{"id": i.id, "title": v.by_id[i.id].title, "annot": i.annot}
                                                      for i in s.items]} for s in p.sections]}
                             for p in c.lectures],
                "appendix": [{"lecture": p.number, "items": [{"id": i.id, "title": v.by_id[i.id].title,
                                                              "annot": i.annot} for i in items]}
                             for p, items in c.appendix],
                "warnings": roam_tex.warnings_json(c.warnings)}, ensure_ascii=False, indent=1))
        else:
            roam_tex.print_plan(c, v)
            roam_tex.print_warnings(c.warnings)
        return 0
    if a.action == "export":
        if a.note:
            n = v.resolve(a.note)
            c = roam_tex.course_plan(v, a.course)
            tex, w = roam_tex.note_to_tex(n, v, c.exported | {n.id})
            sys.stdout.write(tex)
            roam_tex.print_warnings(w)
            return 0
        c, files, w = roam_tex.render_course(v, a.course)
        summary = roam_tex.write_files(files, a.course, dry_run=a.dry_run)
        pre = "dry-run: " if a.dry_run else ""
        print(f"{pre}создано {len(summary['created'])}, обновлено {len(summary['updated'])}, "
              f"удалено {len(summary['deleted'])}, без изменений {len(summary['unchanged'])}")
        for k in ("created", "updated", "deleted"):
            for rel in summary[k]:
                print(f"  {k[:3]}  {rel}")
        roam_tex.print_warnings(w)
        return 0
    if a.action == "build":
        c, files, w = roam_tex.render_course(v, a.course)
        summary = roam_tex.write_files(files, a.course)
        print(f"export: создано {len(summary['created'])}, обновлено {len(summary['updated'])}, "
              f"удалено {len(summary['deleted'])}; предупреждений {len(w)}")
        return roam_tex.build(a.course, open_pdf=a.open)
    return 2


def cmd_site(a, v: Vault):
    import roam_html
    site = roam_html.Site(v)
    out = (Path(a.out) if a.out else VAULT / site.cfg["out"]).resolve()
    if a.action == "plan":
        roam_html.print_plan(site)
        roam_html.print_warnings(site.warnings)
        return 0
    if a.action == "check":
        files, w = roam_html.render_site(site)
        print(f"страниц {sum(1 for f in files if f.endswith('.html'))}, файлов {len(files)}")
        roam_html.print_warnings(w)
        return 1 if w else 0
    if a.action == "export" and a.note:
        n = v.resolve(a.note)
        page = site.pages.get(n.id)
        if page is None:
            die(f"«{n.title}» не публикуется (курс вне [site].publish или exclude)")
        article, toc, w = roam_html.note_article(page, site)
        sys.stdout.write(article + "\n")
        roam_html.print_warnings(w)
        return 0
    if a.action in ("export", "serve"):
        files, w = roam_html.render_site(site)
        summary = roam_html.write_site(files, out, dry_run=a.dry_run)
        pre = "dry-run: " if a.dry_run else ""
        print(f"{pre}{out}: создано {len(summary['created'])}, обновлено {len(summary['updated'])}, "
              f"удалено {len(summary['deleted'])}, без изменений {len(summary['unchanged'])}")
        if a.dry_run or a.verbose:
            for k in ("created", "updated", "deleted"):
                for rel in summary[k]:
                    print(f"  {k[:3]}  {rel}")
        roam_html.print_warnings(w)
        if a.action == "serve":
            import http.server
            import functools
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
            print(f"\nhttp://localhost:{a.port}/  (Ctrl-C чтобы остановить)")
            with http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler) as srv:
                try:
                    srv.serve_forever()
                except KeyboardInterrupt:
                    pass
        return 0
    return 2


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roam.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="инвентарь заметок")
    s.add_argument("--tag", action="append")
    s.add_argument("--since", help="YYYYMMDD[HHMMSS] по timestamp в имени файла")
    s.add_argument("--until")
    s.add_argument("--status", choices=["pending", "audited", "stub", "all"], default="all")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("pending", help="новые/изменённые с последней проверки")
    s.add_argument("--all", action="store_true", help="включая lecture/hub/image")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_pending)

    s = sub.add_parser("show", help="метаданные + тело с номерами строк")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("context", help="бандл для проверки заметки")
    s.add_argument("ref")
    s.add_argument("--full-linked", action="store_true")
    s.set_defaults(fn=cmd_context)

    s = sub.add_parser("lint", help="детерминированные проверки")
    s.add_argument("refs", nargs="*")
    s.add_argument("--rules", help="напр. M*,T02")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_lint)

    s = sub.add_parser("fix", help="механические правки (H04,H06,T02,X02); по умолчанию dry-run")
    s.add_argument("refs", nargs="*")
    s.add_argument("--rules")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_fix)

    s = sub.add_parser("spell", help="кандидаты в опечатки (hunspell)")
    s.add_argument("refs", nargs="*")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_spell)

    s = sub.add_parser("mentions", help="несвязанные упоминания заметки")
    s.add_argument("ref")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_mentions)

    s = sub.add_parser("new", help="создать заметку")
    s.add_argument("--title", required=True)
    s.add_argument("--tags", default="")
    s.add_argument("--date")
    s.add_argument("--body-file", help="файл с телом, «-» = stdin")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("macros", help="LaTeX-макросы из конфига Emacs")
    s.add_argument("--json", action="store_true")
    s.add_argument("--mathjax", action="store_true")
    s.add_argument("--latex", action="store_true", help="блок макросов как есть (handbook/common/macros.tex)")
    s.set_defaults(fn=cmd_macros)

    s = sub.add_parser("expand", help="stdin→stdout: раскрыть макросы")
    s.add_argument("--card", action="store_true", help="подготовить поле Anki ($→\\(, <→\\lt)")
    s.set_defaults(fn=cmd_expand)

    s = sub.add_parser("cards", help="Anki-карточки в заметках")
    s.add_argument("refs", nargs="*")
    s.add_argument("--check", action="store_true")
    s.add_argument("--stale", action="store_true")
    s.add_argument("--verbose", "-v", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_cards)

    s = sub.add_parser("guard", help="проверка перед записью в заметку")
    s.add_argument("refs", nargs="+")
    s.add_argument("--expect-hash")
    s.set_defaults(fn=cmd_guard)

    s = sub.add_parser("sync", help="org-roam-db-sync в запущенном Emacs")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("state", help="состояние проверки")
    s.add_argument("action", choices=["mark", "cards", "dismiss", "show"])
    s.add_argument("refs", nargs="*")
    s.add_argument("--report")
    s.add_argument("--open", type=int, default=0)
    s.add_argument("--fp")
    s.add_argument("--text")
    s.add_argument("--category")
    s.set_defaults(fn=cmd_state)

    s = sub.add_parser("pdfmap", help="лекции → страницы PDF-конспекта")
    s.add_argument("course")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_pdfmap)

    s = sub.add_parser("grep-pdf", help="поиск по тексту PDF-конспекта")
    s.add_argument("course")
    s.add_argument("regex")
    s.set_defaults(fn=cmd_grep_pdf)

    s = sub.add_parser("handbook", help="PDF-конспект курса: plan | export | build")
    s.add_argument("action", choices=["plan", "export", "build"])
    s.add_argument("course")
    s.add_argument("--json", action="store_true", help="plan: JSON")
    s.add_argument("--dry-run", action="store_true", help="export: только сводка")
    s.add_argument("--note", help="export: tex одной заметки в stdout")
    s.add_argument("--open", action="store_true", help="build: открыть PDF")
    s.set_defaults(fn=cmd_handbook)

    s = sub.add_parser("site", help="HTML-сайт из заметок: plan | export | check | serve")
    s.add_argument("action", choices=["plan", "export", "check", "serve"])
    s.add_argument("--out", help="каталог вывода (по умолчанию [site].out из roam.toml)")
    s.add_argument("--dry-run", action="store_true", help="export: только сводка")
    s.add_argument("--note", help="export: HTML статьи одной заметки в stdout")
    s.add_argument("-v", "--verbose", action="store_true", help="export: перечислить файлы")
    s.add_argument("--port", type=int, default=8788, help="serve: порт (8788)")
    s.set_defaults(fn=cmd_site)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args, Vault()) or 0


if __name__ == "__main__":
    sys.exit(main())
