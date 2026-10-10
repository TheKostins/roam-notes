"""python3 -m unittest tools/test_roam.py  (или: cd tools && python3 -m unittest)"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roam  # noqa: E402

MACRO_SRC = r'''
(defvar my/notes-latex-macros
  (mapconcat
   #'identity
   '(;; a "quoted" comment must not confuse the parser
     "\\newcommand{\\notedef}[1]{\\providecommand{#1}{}\\renewcommand{#1}}"
     "\\notedef{\\R}{\\mathbb{R}}"
     "\\notedef{\\ind}{\\mathds{1}}"
     "\\notedef{\\abs}[1]{\\left|#1\\right|}"
     "\\notedef{\\Pb}{\\mathbb{P}}"
     "\\notedef{\\Prob}{\\Pb}"
     "\\notedef{\\given}{\\,\\middle|\\,}"
     "\\DeclareMathOperator{\\Var}{Var}"
     "\\notedef{\\Pcond}[2]{\\Pb\\!\\left(#1 \\given #2\\right)}")
   "\n")
  "Docstring with \\R inside.")

(defun next-thing () nil)
'''

NOTE = """:PROPERTIES:
:ID:       316B5BFA-8B9F-4267-9086-D6593AAB5F9B
:END:
#+title: Тестовая заметка
#+filetags: :theorem:metopt:

Пусть $f: E \\rightarrow \\R$ и j$x \\in E$, а $y$тоже.
\\[
\\textt{tr}(X) = \\left( a \\right))
\\]
\\[
\\]
Тут $g\\prime(t)$ и $g^{\\prime}(t)$; вернo.
$\\{ x \\texttt{ -- выпуклое} \\}$

* Доказательство
Текст [[id:316B5BFA-8B9F-4267-9086-D6593AAB5F9B][сама]] и [[id:00000000-0000-0000-0000-000000000000][битая]].

* Anki :noexport:
:PROPERTIES:
:ANKI_DECK: Notes::metopt
:END:
** Карточка
:PROPERTIES:
:ANKI_NOTE_TYPE: Basic
:END:
*** Front
Что такое $\\R$?
*** Back
\\(a < b\\)
"""


def make_note(text: str, name: str = "20260101000000-тестовая_заметка.org") -> roam.Note:
    d = Path(tempfile.mkdtemp())
    p = d / name
    p.write_text(text, encoding="utf-8")
    roam_vault = roam.VAULT
    try:
        roam.VAULT = d
        n = roam.Note(p)
        n.rel  # noqa: B018 -- resolve against the temp vault
    finally:
        roam.VAULT = roam_vault
    return n


class FakeVault:
    def __init__(self, notes):
        self.notes = notes
        self.by_id = {n.id: n for n in notes}
        self.cfg = {"vault": {"emacs_macros": "/nonexistent"},
                    "anki": {"basic": {"type": "Basic", "fields": ["Front", "Back"]},
                             "cloze": {"type": "Cloze", "fields": ["Text", "Back Extra"]}}}
        self.state = {"version": 1, "notes": {}}
        self.workflow_tags = {"review", "hub", "lecture", "image", "backfill"}
        self.type_tags = {"definition", "theorem"}
        self.courses = {"metopt": {"deck": "Notes::metopt"}}
        self.stub_chars = 40

    is_stub = roam.Vault.is_stub
    looks_stub = roam.Vault.looks_stub
    course_of = roam.Vault.course_of
    status = roam.Vault.status
    backlinks = roam.Vault.backlinks


def lint(text: str) -> dict[str, list[roam.Finding]]:
    n = make_note(text)
    real = roam.VAULT
    roam.VAULT = n.path.parent
    try:
        linter = roam.Linter(FakeVault([n]))
        linter.macros = roam.parse_macros_source(MACRO_SRC)
        linter.known = roam.known_commands(linter.macros)
        linter.lint_note(n)
    finally:
        roam.VAULT = real
    out: dict[str, list[roam.Finding]] = {}
    for f in linter.out:
        out.setdefault(f.rule, []).append(f)
    return out


class SlugTest(unittest.TestCase):
    def test_cyrillic(self):
        self.assertEqual(roam.slugify("Дифференциальный критерий выпуклости"),
                         "дифференциальныи_критерии_выпуклости")
        self.assertEqual(roam.slugify("Преобразования, сохраняющие выпуклость"),
                         "преобразования_сохраняющие_выпуклость")
        self.assertEqual(roam.slugify("Ёлка — L-гладкость (µ)!"), "елка_l_гладкость_µ")


class MacroTest(unittest.TestCase):
    def setUp(self):
        self.m = roam.parse_macros_source(MACRO_SRC)

    def test_parse(self):
        self.assertEqual(set(self.m), {"\\R", "\\ind", "\\abs", "\\Pb", "\\Prob", "\\given", "\\Var", "\\Pcond"})
        self.assertEqual(self.m["\\abs"], {"args": 1, "body": "\\left|#1\\right|"})
        self.assertEqual(self.m["\\Var"]["body"], "\\operatorname{Var}")

    def test_expand(self):
        self.assertEqual(roam.expand_macros("\\R^n, \\Rightarrow, \\Prob", self.m),
                         "\\mathbb{R}^n, \\Rightarrow, \\mathbb{P}")
        self.assertEqual(roam.expand_macros("\\abs{x_{i}}", self.m), "\\left|x_{i}\\right|")

    def test_card(self):
        self.assertEqual(roam.cardify("$\\Pcond{A}{B}, \\ind, a<b$", self.m),
                         "\\(\\mathbb{P}\\!\\left(A \\,\\middle|\\, B\\right), \\mathbb{1}, a \\lt b\\)")
        self.assertEqual(roam.cardify("текст a<b и \\[ x > 0 \\]", self.m), "текст a<b и \\[ x \\gt 0 \\]")


class ScannerTest(unittest.TestCase):
    def test_fragments(self):
        fr, pr = roam.scan_math("a $x$ b \\[ y \\] c \\begin{gather}z\\end{gather} \\(w\\)")
        self.assertEqual([f.kind for f in fr], ["inline", "display", "env", "paren"])
        self.assertEqual(pr, [])

    def test_unbalanced(self):
        _, pr = roam.scan_math("строка $x$ и $y\n\nдругой абзац $z$")
        self.assertEqual([p[0] for p in pr], ["M01"])
        _, pr = roam.scan_math("\\[ x")
        self.assertEqual([p[0] for p in pr], ["M02"])


class LintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = lint(NOTE)

    def test_expected_rules(self):
        for rule in ("T02", "M06", "M05", "M07", "M08", "M10", "M11", "M12", "X01", "K01", "K07", "A02", "A07"):
            self.assertIn(rule, self.f, rule)

    def test_details(self):
        self.assertEqual(self.f["M06"][0].snippet, "\\textt")
        self.assertEqual(len(self.f["M10"]), 1)          # g^{\prime} is fine
        self.assertIn("texttt", self.f["M07"][0].message)
        self.assertTrue(self.f["T02"][0].fixable)
        a02 = " | ".join(x.message for x in self.f["A02"])
        self.assertIn("\\R", a02)
        self.assertIn("$-разделители", a02)
        self.assertIn("\\lt", a02)

    def test_clean_note(self):
        clean = NOTE.split("\nПусть")[0].replace(":theorem:metopt:", ":metopt:theorem:") + \
            "\nПусть $x \\in \\R$ -- число, достаточно длинное тело заметки без ошибок [[id:316B5BFA-8B9F-4267-9086-D6593AAB5F9C][ссылка]].\n"
        f = lint(clean)
        self.assertEqual(set(f) - {"K01"}, set(), {k: [x.message for x in v] for k, v in f.items()})


class HashTest(unittest.TestCase):
    def test_body_hash_ignores_cards(self):
        a = make_note(NOTE)
        b = make_note(NOTE.replace(":ANKI_NOTE_TYPE: Basic", ":ANKI_NOTE_TYPE: Basic\n:ANKI_NOTE_ID: 123"))
        c = make_note(NOTE.replace("вернo", "верно"))
        self.assertEqual(a.body_hash(), b.body_hash())
        self.assertNotEqual(a.body_hash(), c.body_hash())

    def test_cards(self):
        cards = make_note(NOTE).cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["deck"], "Notes::metopt")
        self.assertTrue(cards[0]["noexport"])
        self.assertEqual([f["name"] for f in cards[0]["fields"]], ["Front", "Back"])


class FixTest(unittest.TestCase):
    def test_fix(self):
        src = NOTE.replace("#+title: Тестовая заметка\n", "#+title: Тестовая заметка  \n\n") + "\n\n"
        n = make_note(src)
        out = roam.fix_note(n, {"H04", "H06", "T02"}, FakeVault([n]))
        self.assertIn("#+title: Тестовая заметка\n#+filetags: :metopt:theorem:\n", out)
        self.assertTrue(out.endswith("\\(a < b\\)\n"))


class MentionTest(unittest.TestCase):
    def test_stem_regex(self):
        rx = roam.mention_regex("Выпуклая функция")
        self.assertTrue(rx.search("подграфик выпуклой функции -- выпукл"))
        self.assertTrue(rx.search("семейство выпуклых дифференцируемых функций"))
        self.assertFalse(rx.search("выпуклое множество"))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# handbook: org → LaTeX (roam_tex.py)
# --------------------------------------------------------------------------
import roam_tex  # noqa: E402

TEX_ID = "316B5BFA-8B9F-4267-9086-D6593AAB5F9B"
AAA = "AAAAAAAA-0000-0000-0000-000000000001"


def tex(body: str, tags: str = ":metopt:theorem:", exported=None, extra_notes=()):
    text = (":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Тест\n#+filetags: %s\n\n%s\n"
            % (TEX_ID, tags, body))
    n = make_note(text)
    notes = [n] + list(extra_notes)
    real = roam.VAULT
    roam.VAULT = n.path.parent
    try:
        out, warnings = roam_tex.note_to_tex(n, FakeVault(notes), set(exported or ()) | {TEX_ID})
    finally:
        roam.VAULT = real
    body_out = out.split("\n", 3)[3]      # drop the two comment lines + blank
    return body_out, [w.code for w in warnings]


def other_note():
    return make_note(":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Другая\n#+filetags: :metopt:definition:\n\nТело другой заметки достаточно длинное, чтобы не быть заглушкой.\n"
                     % AAA, name="20260101000001-другая.org")


class TexInlineTest(unittest.TestCase):
    def test_env_label(self):
        out, w = tex("Пусть $x$.")
        self.assertEqual(out, "\\begin{theorem}[{Тест}]\\label[theorem]{note:%s}\n  Пусть $x$.\n\\end{theorem}\n" % TEX_ID)
        self.assertEqual(w, [])

    def test_type_map(self):
        out, _ = tex("Пусть $x$.", tags=":metopt:property:")
        self.assertIn("\\begin{proposition}[{Тест}]\\label[proposition]", out)
        out, _ = tex("Пусть $x$.", tags=":metopt:method:")
        self.assertIn("\\begin{method}", out)
        out, w = tex("Пусть $x$ и ещё много текста, чтобы не быть заглушкой.", tags=":metopt:")
        self.assertIn("\\begin{proposition}", out)
        self.assertIn("X11", w)

    def test_dash(self):
        out, _ = tex("$E$ -- выпуклое, $\\text{ -- }$ и\n\\[\nx\n\\] -- так")
        self.assertIn("$E$~--- выпуклое, $\\text{ -- }$ и\n  \\[\n  x\n  \\] --- так", out)

    def test_escape(self):
        out, w = tex("Доля 5% и a_b & c#d ^ ~, $a_b$ и цена 5$")
        self.assertIn("Доля 5\\% и a\\_b \\& c\\#d \\textasciicircum{} \\textasciitilde{}, $a_b$ и цена 5\\$", out)
        self.assertIn("X02", w)

    def test_links(self):
        o = other_note()
        out, w = tex("[[id:%s][выпуклое_множество]] и [[id:BBBBBBBB-0000-0000-0000-000000000002][другое]]" % AAA,
                     exported={AAA}, extra_notes=[o])
        self.assertIn("\\hyperref[note:%s]{выпуклое\\_множество} и другое" % AAA, out)
        self.assertIn("X03", w)
        out, w = tex("[[id:%s][та]]" % AAA, exported=set(), extra_notes=[o])
        self.assertIn("  та\n", out)
        self.assertIn("X07", w)

    def test_emphasis(self):
        o = other_note()
        out, _ = tex("/аффинное/ и *жирный*, 2025/26, $a/b$, /см. [[id:%s][тут]]/ и (*) дважды (*)" % AAA,
                     exported={AAA}, extra_notes=[o])
        self.assertIn("\\emph{аффинное} и \\textbf{жирный}, 2025/26, $a/b$, \\emph{см. \\hyperref[note:%s]{тут}} и (*) дважды (*)" % AAA, out)

    def test_unicode_prose(self):
        out, w = tex("тогда ⇒ $⇒$ и много текста для длины.")
        self.assertIn("тогда $\\Rightarrow$ $⇒$", out)
        self.assertIn("X05", w)


class TexBlockTest(unittest.TestCase):
    def test_rem_hoisted(self):
        out, _ = tex("Пусть $x$.\n/Rem/: Текст $y$.\n\n/Rem/: Ещё.")
        head, rest = out.split("\\end{theorem}\n")
        self.assertNotIn("Rem", head)
        self.assertIn("Пусть $x$.", head)
        self.assertEqual(rest, "\n\\begin{remark}\n  Текст $y$.\n\\end{remark}\n\n\\begin{remark}\n  Ещё.\n\\end{remark}\n")

    def test_rem_leading_math_and_no_colon(self):
        out, _ = tex("Пусть $x$.\n/Rem/: $y$ велико.\n\n/Rem/ Без двоеточия.")
        head, rest = out.split("\\end{theorem}\n")
        self.assertNotIn("Rem", head)
        self.assertIn("\\begin{remark}\n  $y$ велико.\n\\end{remark}", rest)
        self.assertIn("\\begin{remark}\n  Без двоеточия.\n\\end{remark}", rest)

    def test_sentence_breaks(self):
        out, _ = tex("Используем формулу\nИтого $x$\nИ еще.\nКонец фразы.\nТогда\n$x = y$\nи $z$\n\\[\na\n\\]\nДалее.")
        self.assertIn("Используем формулу \\\\\n  Итого $x$ \\\\\n  И еще.\n  Конец фразы.\n  Тогда\n  $x = y$\n  и $z$\n  \\[\n  a\n  \\]\n  Далее.", out)

    def test_theorem_title_prefix(self):
        n = make_note(":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Теорема Ферма\n#+filetags: :calc:theorem:\n\nТекст.\n" % TEX_ID)
        real = roam.VAULT
        roam.VAULT = n.path.parent
        try:
            out, _ = roam_tex.note_to_tex(n, FakeVault([n]), {TEX_ID})
        finally:
            roam.VAULT = real
        self.assertIn("\\begin{theorem}[{Ферма}]", out)

    def test_proof_parts(self):
        out, w = tex("A.\n* Доказательство\nВезде $\\lambda$.\n** $\\Rightarrow$\nB.\n*** $g$ -- выпуклая\nC.\n** 2. Шаг.\nD.")
        self.assertIn("\\begin{proof}\n  Везде $\\lambda$.\n\n  \\proofpart{$\\Rightarrow$}\n  B.\n\n  \\proofstep{$g$~--- выпуклая}\n  C.\n\n  \\proofpart{2. Шаг}\n  D.\n\\end{proof}", out)
        self.assertEqual(w, [])

    def test_lists(self):
        out, _ = tex("Пусть:\n- a\n- b $x$\n  cont\n1. один\n   продолж.\n2. два\n   - вложенный")
        self.assertIn("Пусть:\n\n  \\begin{itemize}\n    \\item a\n    \\item b $x$\n      cont\n  \\end{itemize}\n\n  \\begin{enumerate}\n    \\item один\n      продолж.\n    \\item два\n      \\begin{itemize}\n        \\item вложенный\n      \\end{itemize}\n  \\end{enumerate}", out)

    def test_math_only_line_keeps_paragraph(self):
        out, _ = tex("Тогда\n$x = y$\nи $z$.")
        self.assertIn("  Тогда\n  $x = y$\n  и $z$.", out)

    def test_display_in_paragraph(self):
        out, _ = tex("Тогда\n\\[\nx = y\n\\]\nи $z$.\n\n\\[\na\n\nb\n\\]")
        self.assertIn("  Тогда\n  \\[\n  x = y\n  \\]\n  и $z$.\n\n  \\[\n  a\n\n  b\n  \\]", out)

    def test_noexport_dropped(self):
        n = make_note(NOTE)
        real = roam.VAULT
        roam.VAULT = n.path.parent
        try:
            out, _ = roam_tex.note_to_tex(n, FakeVault([n]), {n.id})
        finally:
            roam.VAULT = real
        for bad in ("Anki", "ANKI_", ":PROPERTIES:", "Карточка"):
            self.assertNotIn(bad, out)

    def test_property_heading(self):
        out, w = tex("Опр.\n* Свойство: X\nтекст\n** Доказательство\nдок.")
        self.assertIn("\\begin{proposition}[{Свойство: X}]\\label[proposition]{note:%s:1}\n  текст\n\\end{proposition}\n\n\\begin{proof}\n  док.\n\\end{proof}" % TEX_ID, out)
        self.assertNotIn("X01", w)

    def test_unknown_heading_paragraph(self):
        out, w = tex("Опр.\n* Почему это работает?\nтекст")
        self.assertIn("\\paragraph*{Почему это работает?}\nтекст", out)
        self.assertNotIn("proposition", out)
        self.assertIn("X01", w)

    def test_interpretation_heading_is_remark(self):
        out, w = tex("Опр.\n* Интерпретация.\nтекст")
        self.assertIn("\\begin{remark}[Интерпретация]\n  текст\n\\end{remark}", out)
        self.assertEqual(w, [])

    def test_code_block(self):
        out, w = tex("Алгоритм:\n#+begin_src python\n  x = 1  # комментарий\n  y = x\n#+end_src\nДалее.")
        self.assertIn("\\begin{Verbatim}[fontsize=\\small]\n  x = 1  # комментарий\n  y = x\n  \\end{Verbatim}", out)
        self.assertNotIn("X09", w)

    def test_table(self):
        out, w = tex("Таблица:\n| Класс | Невязка |\n|---|---|\n| $f \\in C$ | $\\norm{x}$ |\nКонец.")
        self.assertIn("\\begin{tabular}{l l}\n    \\hline\n    Класс & Невязка \\\\\n    \\hline\n    $f \\in C$ & $\\norm{x}$ \\\\\n    \\hline\n  \\end{tabular}", out)
        self.assertNotIn("X09", w)

    def test_table_not_in_display_math(self):
        out, w = tex("Пусть\n\\[\n|x| \\le 1\n\\]\nДалее.")
        self.assertNotIn("tabular", out)
        self.assertIn("|x| \\le 1", out)

    def test_image_link(self):
        out, w = tex("Текст.\n\n[[file:images/armijo.png]]\n\nДалее.")
        self.assertIn("\\begin{center}\n    \\includegraphics[width=.8\\linewidth]{../../images/armijo.png}\n  \\end{center}", out)
        self.assertNotIn("X04", w)

    def test_concept_env(self):
        out, w = tex("Понятие.", tags=":metopt:concept:")
        self.assertIn("\\begin{concept}[{Тест}]\\label[concept]{note:%s}" % TEX_ID, out)
        self.assertNotIn("X11", w)

    def test_theorem_title_prefix_kept_for_lowercase(self):
        n = make_note(":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Теорема о промежуточном значении\n#+filetags: :calc:theorem:\n\nТекст.\n" % TEX_ID)
        real = roam.VAULT
        roam.VAULT = n.path.parent
        try:
            out, _ = roam_tex.note_to_tex(n, FakeVault([n]), {TEX_ID})
        finally:
            roam.VAULT = real
        self.assertIn("\\begin{theorem}[{Теорема о промежуточном значении}]", out)

    def test_example_heading(self):
        out, _ = tex("S.\n* Пример\nПусть $C_1$.")
        self.assertIn("\\begin{example}\n  Пусть $C_1$.\n\\end{example}", out)


LECTURE = """:PROPERTIES:
:ID:       8DD500EE-1B27-4328-9B4A-3A365516213F
:END:
#+title: Метопт. Лекция 1: Выпуклые множества и функции
#+filetags: :lecture:metopt:
#+date: 2026-09-01

* Материалы
- конспект

* План лекции
1. Выпуклые множества (1.1)
   - [[id:316B5BFA-8B9F-4267-9086-D6593AAB5F9B][Выпуклое множество]] — опр. 1.1
   - [[id:08CF7113-4709-4C4F-A0E7-4A2ED595BD7F][Пересечение]] — свойство 1.1.1
   - Конус — /нет заметки/
2. Выпуклые функции (1.2)
   - [[id:1CA14F2C-637F-41DE-9E97-72075D19E97F][Выпуклая функция]], [[id:AAAAAAAA-0000-0000-0000-000000000001][Другая]] — опр. 1.4

* Вне конспекта
- [[id:14FE7B45-F304-4C15-8086-3535589FA04A][Сумма Минковского]] — для свойства 1.1.3
- [[id:5D688904-8445-47B5-9D39-8BCC39F9C354][Гильбертово пространство]], [[id:F2942E98-6115-4600-AA3F-E0F687D9C732][Скалярное произведение]] — для 1.1.2
"""


class PlanTest(unittest.TestCase):
    def test_lecture_plan(self):
        n = make_note(LECTURE, name="20260922124251-лекция.org")
        plan, w = roam_tex.lecture_plan(n, FakeVault([n]))
        self.assertEqual(plan.number, 1)
        self.assertEqual(plan.title, "Выпуклые множества и функции")
        self.assertEqual([s.title for s in plan.sections], ["Выпуклые множества", "Выпуклые функции"])
        self.assertEqual(plan.sections[0].ref, "1.1")
        self.assertEqual([i.id for i in plan.sections[0].items],
                         ["316B5BFA-8B9F-4267-9086-D6593AAB5F9B", "08CF7113-4709-4C4F-A0E7-4A2ED595BD7F"])
        self.assertEqual(plan.sections[0].items[0].annot, "опр. 1.1")
        self.assertEqual([i.id[:8] for i in plan.sections[1].items], ["1CA14F2C", "AAAAAAAA"])
        self.assertEqual(plan.sections[1].items[0].annot, None)
        self.assertEqual(plan.sections[1].items[1].annot, "опр. 1.4")
        self.assertEqual([i.id[:8] for i in plan.appendix], ["14FE7B45", "5D688904", "F2942E98"])
        self.assertEqual(plan.appendix[2].annot, "для 1.1.2")
        self.assertEqual([x.code for x in w], ["X10"])


class MacrosLatexTest(unittest.TestCase):
    def test_raw(self):
        d = Path(tempfile.mkdtemp())
        (d / "my-org.el").write_text(MACRO_SRC, encoding="utf-8")
        lines = roam_tex.macros_source_raw({"vault": {"emacs_macros": str(d / "my-org.el")}})
        self.assertEqual(lines[0], "\\newcommand{\\notedef}[1]{\\providecommand{#1}{}\\renewcommand{#1}}")
        self.assertIn("\\DeclareMathOperator{\\Var}{Var}", lines)
        self.assertEqual(len(lines), 9)


# --------------------------------------------------------------------------
# site: org → HTML (roam_html.py)
# --------------------------------------------------------------------------
import roam_html  # noqa: E402


class FakeSiteVault(FakeVault):
    def __init__(self, notes):
        super().__init__(notes)
        self.cfg["site"] = {"publish": ["metopt"], "title": "T"}
        self.courses = {"metopt": {"name": "Метопт", "deck": "Notes::metopt"}}
        self.type_tags = {"definition", "theorem", "lemma", "example", "method", "property"}


def site_of(*notes):
    real = roam.VAULT
    roam.VAULT = notes[0].path.parent
    try:
        v = FakeSiteVault(list(notes))
        return roam_html.Site(v), v
    finally:
        roam.VAULT = real


PAD = "Это тестовая заметка с достаточно длинным телом, чтобы не считаться заглушкой."


def article(body: str, tags: str = ":metopt:theorem:", extra=()):
    text = (":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Тест\n#+filetags: %s\n\n%s\n\n%s\n" % (TEX_ID, tags, PAD, body))
    n = make_note(text)
    site, v = site_of(n, *extra)
    html_out, toc, w = roam_html.note_article(site.pages[TEX_ID], site)
    return html_out, toc, [x.code for x in w]


class HtmlTest(unittest.TestCase):
    def test_inline(self):
        out, _, w = article("Пусть $x < y$ и /курсив/, *жирный*, =код=, a -- b.")
        self.assertIn("\\(x &lt; y\\)", out)
        self.assertIn("<em>курсив</em>", out)
        self.assertIn("<strong>жирный</strong>", out)
        self.assertIn("<code>код</code>", out)
        self.assertIn("a — b", out)
        self.assertEqual(w, [])

    def test_statement_box_and_proof(self):
        out, toc, w = article("Формулировка теоремы $x$.\n\n* Доказательство\nШаг один.\n** Часть\nШаг два.")
        self.assertIn('<div class="statement theorem">', out)
        self.assertIn('<details class="proof" id="s1">', out)
        self.assertIn("<h3>Часть</h3>", out)
        self.assertEqual(toc, [("s1", "Доказательство")])
        self.assertEqual(w, [])

    def test_example_not_boxed(self):
        out, _, _ = article("Длинный пример без рамки, который не является заглушкой.", tags=":metopt:example:")
        self.assertNotIn('class="statement', out)
        self.assertIn("<p>Длинный пример", out)

    def test_links(self):
        other = other_note()
        out, _, w = article("См. [[id:%s][другую]] и [[id:00000000-0000-0000-0000-000000000000][нет]] и [[https://x.y][сайт]]."
                            % AAA, extra=(other,))
        self.assertIn('<a class="id" href="../../metopt/другая/">другую</a>', out)
        self.assertIn('<span class="id missing">нет</span>', out)
        self.assertIn('class="ext" href="https://x.y"', out)
        self.assertEqual(w, ["X03"])

    def test_stub_link_and_page(self):
        stub = make_note(":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Заглушка\n#+filetags: :metopt:backfill:\n\nbackfill\n"
                         % AAA, name="20260101000002-заглушка.org")
        out, _, w = article("См. [[id:%s][заглушку]]." % AAA, extra=(stub,))
        self.assertIn('<a class="id stub" href="../../metopt/заглушка/">заглушку</a>', out)
        site, _ = site_of(stub)
        html_out, _, _ = roam_html.note_article(site.pages[AAA], site)
        self.assertIn('class="stub-note"', html_out)

    def test_src_and_table(self):
        out, _, w = article("Текст.\n#+begin_src python\n  x = 1 < 2\n#+end_src\n\n| a | b |\n|---|---|\n| 1 | $x$ |")
        self.assertIn('<pre><code class="lang-python">x = 1 &lt; 2</code></pre>', out)
        self.assertIn("<th>a</th>", out)
        self.assertIn("<td>\\(x\\)</td>", out)
        self.assertEqual(w, [])

    def test_rem_and_list(self):
        out, _, _ = article("/Rem/: замечание $y$.\n\n1. первый\n2. второй\n   - вложенный")
        self.assertIn('<aside class="rem">', out)
        self.assertIn("<ol>\n<li>первый</li>\n<li>второй\n<ul>\n<li>вложенный</li>\n</ul></li>\n</ol>", out)

    def test_noexport_dropped(self):
        out, toc, _ = article("Тело.\n* Anki :noexport:\n** Карточка\n*** Front\nсекрет")
        self.assertNotIn("секрет", out)
        self.assertEqual(toc, [])

    def test_slug_collision(self):
        a = make_note(":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Один\n#+filetags: :metopt:definition:\n\nДостаточно длинное тело заметки номер один.\n" % TEX_ID,
                      name="20260101000001-один.org")
        b = make_note(":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Один\n#+filetags: :metopt:definition:\n\nДостаточно длинное тело заметки номер два.\n" % AAA,
                      name="20260102000001-один.org")
        site, _ = site_of(a, b)
        urls = {site.pages[TEX_ID].url, site.pages[AAA].url}
        self.assertEqual(len(urls), 2)
        self.assertIn("S01", [w.code for w in site.warnings])

    def test_render_site_files(self):
        n = make_note(":PROPERTIES:\n:ID:       %s\n:END:\n#+title: Тест\n#+filetags: :metopt:definition:\n\nДостаточно длинное тело заметки $x$.\n" % TEX_ID)
        site, _ = site_of(n)
        files, w = roam_html.render_site(site)
        self.assertIn("metopt/тестовая_заметка/index.html", files)
        for f in ("index.html", "metopt/index.html", "graph/index.html", "tags/index.html", "tags/metopt/index.html",
                  "graph.json", "search.json", "id-map.json", "site.css", "site.js", ".nojekyll"):
            self.assertIn(f, files)
        page = files["metopt/тестовая_заметка/index.html"]
        self.assertIn('href="../../site.css"', page)
        self.assertIn("{{", page) if False else self.assertNotIn("{{", page)
        g = json.loads(files["graph.json"])
        self.assertEqual([n["id"] for n in g["nodes"]], [TEX_ID])
