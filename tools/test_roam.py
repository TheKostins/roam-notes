"""python3 -m unittest tools/test_roam.py  (или: cd tools && python3 -m unittest)"""
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
