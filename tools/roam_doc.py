#!/usr/bin/env python3
"""roam_doc.py -- общая модель документа для экспортов (roam_tex.py → LaTeX,
roam_html.py → сайт).  Только stdlib.

Здесь всё, что не зависит от выходного формата: разбор тела заметки на блоки
(Para / Rem / ListBlock / Quote), защита математики и ссылок токенами,
разметка (курсив, жирный, тире), структура заголовков, план лекции и план курса.
Рендереры импортируют это и добавляют только вывод.

Коды предупреждений (общие для всех экспортов):
  X01 неизвестный заголовок       X02 непарная математика
  X03 битая ссылка                X04 внешняя ссылка
  X05 юникод-символ в прозе       X06 пустой регион
  X07 ссылка на stub/неэкспорт.   X08 странный отступ списка
  X09 неподдерживаемая конструкция X10 пункт плана без ссылки
  X11 нет тега типа               X12 заметка курса вне плана
  X13 номер лекции ≠ порядку      X14 нет lecture-заметок
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import roam
from roam import LINK_RE, Note, Vault, scan_math, blank_ranges

# Заголовок второго уровня → роль раздела (по заголовку без «.»/«:» в конце).

HEADING_KIND = {"доказательство": "proof", "пример": "example", "примеры": "example",
               "замечание": "remark", "следствие": "corollary"}

UNICODE_MATH = {
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "→": r"\to", "←": r"\leftarrow", "↦": r"\mapsto",
    "⟨": r"\langle", "⟩": r"\rangle", "∈": r"\in", "∉": r"\notin",
    "≤": r"\le", "≥": r"\ge", "≠": r"\ne", "∀": r"\forall", "∃": r"\exists",
    "∞": r"\infty", "×": r"\times", "∅": r"\varnothing", "⊂": r"\subset",
    "⊆": r"\subseteq", "∩": r"\cap", "∪": r"\cup", "∇": r"\nabla", "±": r"\pm",
    "·": r"\cdot", "∑": r"\sum", "∫": r"\int",
}

BULLET_RE = re.compile(r"^([ \t]*)(-|\+|\d+[.)])[ \t]+(.*)$")
REM_RE = re.compile(r"^[ \t]*/Rem/:?[ \t]*")
TOK_RE = re.compile("[\uE000\uE002](\\d+)\uE001")

@dataclass
class DocWarning:
    note_id: str
    note: str            # file or title for display
    line: int            # 1-based org line, 0 if n/a
    code: str
    message: str

    def fmt(self) -> str:
        loc = f"L{self.line}" if self.line else "--"
        return f"{self.code} {loc:>5}  {self.message}"


def rel_of(note: Note) -> str:
    try:
        return note.rel
    except ValueError:            # note outside the vault (tests)
        return note.path.name


@dataclass
class Ctx:
    note: Note
    vault: Vault
    exported: set[str]
    warnings: list[DocWarning] = field(default_factory=list)

    def warn(self, code: str, line: int, message: str) -> None:
        w = DocWarning(self.note.id, rel_of(self.note), line, code, message)
        if not any(x.code == w.code and x.line == w.line and x.message == w.message
                   for x in self.warnings):
            self.warnings.append(w)


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------

@dataclass
class Para:
    text: str
    line: int


@dataclass
class Rem:
    text: str
    line: int


@dataclass
class Item:
    lines: list[str]
    line: int
    children: list["ListBlock"] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class ListBlock:
    kind: str            # itemize | enumerate
    indent: int
    items: list[Item] = field(default_factory=list)
    first_number: int = 1
    line: int = 0


@dataclass
class Quote:
    blocks: list
    line: int


def indent_of(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


def bullet(masked_line: str):
    m = BULLET_RE.match(masked_line)
    if not m:
        return None
    marker = m.group(2)
    kind = "itemize" if marker in ("-", "+") else "enumerate"
    return len(m.group(1)), kind, marker, m.end(2) + 1


def segment(lines: list[str], base_line: int, ctx: Ctx) -> list:
    """Split a region of org lines into Para / Rem / ListBlock / Quote blocks.
    base_line: 0-based index of lines[0] in the note (for warnings)."""
    lines = list(lines)
    for k in range(len(lines)):
        if base_line + k in ctx.note.excluded:
            low = lines[k].strip().lower()
            if low.startswith("#+begin_") or low.startswith("|"):
                ctx.warn("X09", base_line + k + 1, f"неподдерживаемая конструкция: {lines[k].strip()[:40]}")
            lines[k] = ""
    text = "\n".join(lines)
    frags, problems = scan_math(text)
    for rule, off, msg in problems:
        ctx.warn("X02", base_line + text.count("\n", 0, off) + 1, f"математика: {msg}")
    masked = blank_ranges(text, frags).split("\n")
    # lines touched by a display/env fragment
    starts, pos = [], 0
    for l in lines:
        starts.append(pos)
        pos += len(l) + 1
    in_disp = [False] * len(lines)
    for f in frags:
        if f.kind in ("display", "env", "ddollar"):
            a = text.count("\n", 0, f.start)
            b = text.count("\n", 0, max(f.end - 1, f.start))
            for k in range(a, b + 1):
                in_disp[k] = True

    blocks: list = []
    i, n = 0, len(lines)
    while i < n:
        m = masked[i]
        if not lines[i].strip() and not in_disp[i]:
            i += 1
            continue
        low = m.strip().lower()
        if low.startswith("#+begin_quote"):
            j = i + 1
            while j < n and not masked[j].strip().lower().startswith("#+end_quote"):
                j += 1
            inner = segment(lines[i + 1:j], base_line + i + 1, ctx)
            blocks.append(Quote(inner, base_line + i))
            i = j + 1
            continue
        if bullet(m) and not in_disp[i]:
            lb, i = parse_list(lines, masked, in_disp, i, base_line, ctx)
            blocks.append(lb)
            continue
        if low.startswith("|") or low.startswith("[fn:"):
            ctx.warn("X09", base_line + i + 1, f"неподдерживаемая конструкция: {m.strip()[:40]}")
        j = i + 1
        while j < n:
            mj = masked[j]
            if in_disp[j]:
                j += 1
                continue
            if (not lines[j].strip() or bullet(mj) or REM_RE.match(mj)
                    or mj.strip().lower().startswith("#+begin_quote")):
                break
            j += 1
        para = lines[i:j]
        rm = REM_RE.match(masked[i])
        if rm:
            # cut on the original line: in `masked` math is blanked to spaces,
            # and the trailing [ \t]* would swallow a leading formula
            para[0] = para[0][REM_RE.match(lines[i]).end():]
            blocks.append(Rem("\n".join(para), base_line + i))
        else:
            blocks.append(Para("\n".join(para), base_line + i))
        i = j
    return blocks


def parse_list(lines, masked, in_disp, i, base_line, ctx) -> tuple[ListBlock, int]:
    n = len(lines)
    ind, kind, marker, content_at = bullet(masked[i])
    num = int(re.sub(r"\D", "", marker) or 1) if kind == "enumerate" else 1
    top = ListBlock(kind, ind, first_number=num, line=base_line + i)
    stack: list[ListBlock] = [top]

    def add_item(lb: ListBlock, k: int) -> None:
        _, _, _, at = bullet(masked[k])
        content = lines[k][at:]
        if " :: " in masked[k][at:]:
            ctx.warn("X09", base_line + k + 1, "описательный список (term :: desc) выводится как обычный пункт")
        lb.items.append(Item([content], base_line + k))

    add_item(top, i)
    i += 1
    while i < n:
        m = masked[i]
        cur = stack[-1]
        if in_disp[i]:
            cur.items[-1].lines.append(lines[i].lstrip(" \t") if not in_disp[i - 1] or True else lines[i])
            i += 1
            continue
        if not lines[i].strip():
            j = i + 1
            if j < n and bullet(masked[j]) and not in_disp[j] and bullet(masked[j])[0] >= top.indent:
                i += 1
                continue
            break
        b = bullet(m)
        if b:
            bind, bkind, bmarker, _ = b
            if bind > cur.indent:
                nb = ListBlock(bkind, bind, first_number=int(re.sub(r"\D", "", bmarker) or 1)
                               if bkind == "enumerate" else 1, line=base_line + i)
                cur.items[-1].children.append(nb)
                stack.append(nb)
                add_item(nb, i)
            else:
                while len(stack) > 1 and stack[-1].indent > bind:
                    stack.pop()
                cur = stack[-1]
                if cur.indent != bind:
                    ctx.warn("X08", base_line + i + 1, "отступ пункта не совпадает ни с одним уровнем списка")
                if cur.kind != bkind:
                    if len(stack) == 1:
                        break
                    stack.pop()
                    parent = stack[-1]
                    nb = ListBlock(bkind, bind, line=base_line + i)
                    parent.items[-1].children.append(nb)
                    stack.append(nb)
                    add_item(nb, i)
                else:
                    add_item(cur, i)
            i += 1
            continue
        if indent_of(m) > cur.indent:
            cur.items[-1].lines.append(lines[i].lstrip(" \t"))
            i += 1
            continue
        break
    return top, i


# --------------------------------------------------------------------------
# inline
# --------------------------------------------------------------------------

def protect(text: str, ctx: Ctx, line0: int) -> tuple[str, list]:
    frags, problems = scan_math(text)
    for rule, off, msg in problems:
        ctx.warn("X02", line0 + text.count("\n", 0, off) + 1, f"математика: {msg}")
    masked = blank_ranges(text, frags)
    spans = [(f.start, f.end, "math", f.kind) for f in frags]
    spans += [(m.start(), m.end(), "link", None) for m in LINK_RE.finditer(masked)]
    spans.sort()
    out, tokens, pos = [], [], 0
    for s, e, kind, sub in spans:
        if s < pos:
            continue
        out.append(text[pos:s])
        idx = len(tokens)
        tokens.append((kind, sub, text[s:e], line0 + text.count("\n", 0, s) + 1))
        disp = kind == "math" and sub in ("display", "env", "ddollar")
        out.append(("\uE002" if disp else "\uE000") + str(idx) + "\uE001")
        pos = e
    out.append(text[pos:])
    return "".join(out), tokens


EMPH_RE = re.compile(r"(?<![\w/\\])/(?=[^\s)\]])([^/\n\uE002]*?[^\s(\[])/(?![\w/])")
BOLD_RE = re.compile(r"(?<![\w*\\])\*(?=[^\s)\]])([^*\n\uE002]*?[^\s(\[])\*(?![\w*])")
DASH_RE = re.compile(r"(?<=\S)[ \t](?:--|—)(?=[ \t\n]|$)")


BREAK_END = tuple(".,;:!?(") + ("---", "\\\\")
UPPER_RE = re.compile(r"[А-ЯЁA-Z]")


def sentence_breaks(s: str, br: str = " \\\\") -> str:
    """Notes often end a phrase at a line end without a period and start the
    next phrase with a capital letter; joined into one paragraph this reads as
    «...формулу Итого».  Keep such a break visible with `\\\\`.
    Runs on protected text: math/links are tokens, a display is \uE002n\uE001."""
    lines = s.split("\n")
    for k in range(len(lines) - 1):
        prev, nxt = lines[k].rstrip(), lines[k + 1].lstrip()
        if (prev and nxt and not prev.endswith(BREAK_END)
                and not re.search("\uE002\\d+\uE001$", prev)
                and UPPER_RE.match(nxt)):
            lines[k] = prev + br
    return "\n".join(lines)




# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------

def own_body(note: Note, h) -> tuple[list[str], int]:
    kids = [c for c in note.headings if c.parent is h]
    end = kids[0].line if kids else h.end
    return note.lines[h.body_start:end], h.body_start


def children(note: Note, h) -> list:
    return [c for c in note.headings if c.parent is h and not c.noexport()]




# --------------------------------------------------------------------------
# lecture plan
# --------------------------------------------------------------------------

@dataclass
class PlanItem:
    id: str
    title: str
    annot: str | None
    line: int


@dataclass
class Section:
    title: str | None
    ref: str | None
    items: list[PlanItem] = field(default_factory=list)


@dataclass
class Plan:
    note: Note
    number: int | None
    title: str
    date: str | None
    sections: list[Section] = field(default_factory=list)
    appendix: list[PlanItem] = field(default_factory=list)

    def ids(self) -> list[str]:
        return [i.id for s in self.sections for i in s.items]


def find_heading(note: Note, key: str):
    for h in note.headings:
        if h.title.strip().casefold() == key:
            return h
    return None


def item_links(it: Item) -> list[tuple[str, str, str]]:
    """(id, desc, annot) for every id: link in an item's own text."""
    out = []
    text = it.text
    ms = [m for m in LINK_RE.finditer(text) if m.group(1).lower().startswith("id:")]
    for k, m in enumerate(ms):
        nid = m.group(1)[3:].strip().upper()
        tail_end = ms[k + 1].start() if k + 1 < len(ms) else len(text)
        annot = text[m.end():tail_end].strip()
        annot = re.sub(r"^[\s—–,-]+", "", annot).strip() or None
        out.append((nid, m.group(2) or "", annot))
    return out


def lecture_plan(note: Note, vault: Vault) -> tuple[Plan, list[DocWarning]]:
    ctx = Ctx(note, vault, set())
    m = re.search(r"Лекция\s*(\d+)", note.title)
    number = int(m.group(1)) if m else None
    title = note.title
    if m:
        title = re.sub(r"^.*?Лекция\s*\d+\s*[:.]?\s*", "", title).strip() or title
    else:
        for c in vault.courses.values():
            pre = c.get("prefix")
            if pre and title.startswith(pre):
                title = title[len(pre):].strip()
    plan = Plan(note, number, title, note.date)

    h = find_heading(note, "план лекции")
    if h is None:
        ctx.warn("X10", 0, "нет раздела «План лекции»")
        return plan, ctx.warnings
    lines, base = own_body(note, h)
    blocks = segment(lines, base, ctx)
    for b in blocks:
        if not isinstance(b, ListBlock):
            continue
        for it in b.items:
            direct = item_links(it)
            if b.kind == "enumerate" or it.children:
                first = it.lines[0].strip()
                rm = re.search(r"\s*\(([^()]*)\)\s*$", first)
                sec = Section(re.sub(r"\s*\([^()]*\)\s*$", "", first).strip() or None,
                              rm.group(1) if rm else None)
                for ch in it.children:
                    for sub in ch.items:
                        links = item_links(sub)
                        if not links:
                            ctx.warn("X10", sub.line + 1, f"пункт плана без ссылки: {sub.lines[0].strip()[:60]}")
                            continue
                        nid, desc, annot = links[0]
                        sec.items.append(PlanItem(nid, desc, annot, sub.line + 1))
                if direct and not it.children:
                    nid, desc, annot = direct[0]
                    sec.items.append(PlanItem(nid, desc, annot, it.line + 1))
                plan.sections.append(sec)
            else:
                if not plan.sections or plan.sections[-1].title is not None:
                    plan.sections.append(Section(None, None))
                if not direct:
                    ctx.warn("X10", it.line + 1, f"пункт плана без ссылки: {it.lines[0].strip()[:60]}")
                    continue
                nid, desc, annot = direct[0]
                plan.sections[-1].items.append(PlanItem(nid, desc, annot, it.line + 1))

    h = find_heading(note, "вне конспекта")
    if h is not None:
        lines, base = own_body(note, h)
        for b in segment(lines, base, ctx):
            if isinstance(b, ListBlock):
                for it in b.items:
                    for nid, desc, annot in item_links(it):
                        plan.appendix.append(PlanItem(nid, desc, annot, it.line + 1))
    return plan, ctx.warnings


# --------------------------------------------------------------------------
# course
# --------------------------------------------------------------------------

@dataclass
class Course:
    tag: str
    cfg: dict
    lectures: list[Plan]
    exported: set[str]
    excluded: set[str]
    appendix: list[tuple[Plan, list[PlanItem]]]
    warnings: list[DocWarning]
    title: str
    subtitle: str


def lectures_of(vault: Vault, course: str) -> list[Note]:
    ls = [n for n in vault.notes if "lecture" in n.tags and course in n.tags]
    return sorted(ls, key=lambda n: (n.date or "", n.created or ""))


def course_plan(vault: Vault, course: str, local: dict | None = None) -> Course:
    """local: ручные настройки экспорта (exclude, extra_aux, title, subtitle),
    например handbook.toml курса."""
    cfg = vault.courses.get(course)
    if cfg is None:
        roam.die(f"курс «{course}» не описан в roam.toml")
    local = dict(local or {})
    excluded = {x.upper() for x in local.get("exclude", [])}
    warnings: list[DocWarning] = []
    lectures: list[Plan] = []
    for k, n in enumerate(lectures_of(vault, course), 1):
        plan, w = lecture_plan(n, vault)
        warnings += w
        if plan.number is None:
            plan.number = k
        elif plan.number != k:
            warnings.append(DocWarning(n.id, rel_of(n), 0, "X13",
                                       f"в заголовке «Лекция {plan.number}», по дате это глава {k}"))
        lectures.append(plan)
    if not lectures:
        warnings.append(DocWarning("", course, 0, "X14", f"нет lecture-заметок с тегами lecture+{course}"))

    def ok(nid: str, where: str, line: int, plan: Plan) -> bool:
        n = vault.by_id.get(nid)
        if n is None:
            warnings.append(DocWarning(plan.note.id, rel_of(plan.note), line, "X03", f"{where}: битая ссылка id:{nid[:8]}"))
            return False
        if nid in excluded:
            return False
        if vault.is_stub(n):
            warnings.append(DocWarning(plan.note.id, rel_of(plan.note), line, "X07", f"{where}: заглушка «{n.title}» пропущена"))
            return False
        return True

    exported: set[str] = set()
    for plan in lectures:
        for s in plan.sections:
            s.items = [i for i in s.items if ok(i.id, "план", i.line, plan)]
            exported.update(i.id for i in s.items)
    appendix: list[tuple[Plan, list[PlanItem]]] = []
    seen = set(exported)
    for plan in lectures:
        items = []
        for i in plan.appendix:
            if i.id in seen or not ok(i.id, "вне конспекта", i.line, plan):
                continue
            seen.add(i.id)
            items.append(i)
        for x in local.get("extra_aux", []):
            x = x.upper()
            if x not in seen and x in vault.by_id and not vault.is_stub(vault.by_id[x]):
                seen.add(x)
                items.append(PlanItem(x, vault.by_id[x].title, None, 0))
        # transitive prerequisites: notes of this course linked only from
        # the appendix (or the plan) but not listed anywhere
        queue = [i.id for i in items] + plan.ids()
        while queue:
            src = vault.by_id.get(queue.pop(0))
            if src is None:
                continue
            for oid in src.out_ids():
                o = vault.by_id.get(oid)
                if (o is None or oid in seen or oid in excluded or vault.course_of(o) != course
                        or vault.is_stub(o) or set(o.tags) & {"lecture", "hub", "image"}):
                    continue
                seen.add(oid)
                items.append(PlanItem(oid, o.title, None, 0))
                queue.append(oid)
                warnings.append(DocWarning(o.id, rel_of(o), 0, "X12",
                                           f"«{o.title}» не в плане, добавлена в приложение по ссылке из «{src.title}»"))
        if items:
            appendix.append((plan, items))
    exported = set(seen)
    for n in vault.notes:
        if (vault.course_of(n) == course and n.id not in exported and n.id not in excluded
                and not (set(n.tags) & {"lecture", "hub", "image"}) and not vault.is_stub(n)):
            warnings.append(DocWarning(n.id, rel_of(n), 0, "X12", f"заметка курса вне плана: «{n.title}»"))
    return Course(course, cfg, lectures, exported, excluded, appendix, warnings,
                  local.get("title") or cfg.get("handbook_title") or cfg.get("name") or course,
                  local.get("subtitle") or "Конспект к экзамену по заметкам")
