"""
instrumenter.py
---------------
Parses a C/C++ source file and:
  1. Injects  LOG_FLAG("construct_N_filename");  at every execution-block boundary
  2. Records  block_map: list of (flag_name, start_line, end_line)
     so the HTML reporter can colour-code the original source.

Flag naming:
    func1_main    – 1st function body in main.c
    if2_sensor    – 2nd if-block in sensor.c
    else1_main    – 1st else/else-if block
    for3_motor    – 3rd for-loop
    while1_utils  – 1st while-loop
    switch1_main  – 1st switch body
    case1_main    – 1st case-break inside a switch
    do1_main      – 1st do-while body
"""

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

TOKEN_SPEC = [
    ("BLOCK_COMMENT",  r"/\*[\s\S]*?\*/"),
    ("LINE_COMMENT",   r"//[^\n]*"),
    ("STRING",         r'"(?:[^"\\]|\\.)*"'),
    ("CHAR_LIT",       r"'(?:[^'\\]|\\.)*'"),
    ("PREPROCESSOR",   r"^\s*#[^\n]*"),
    ("LBRACE",         r"\{"),
    ("RBRACE",         r"\}"),
    ("SEMICOLON",      r";"),
    ("COLON",          r":"),
    ("KEYWORD",        r"\b(?:if|else|while|for|do|switch|case|default|return|break|continue)\b"),
    ("LPAREN",         r"\("),
    ("RPAREN",         r"\)"),
    ("NEWLINE",        r"\n"),
    ("WHITESPACE",     r"[ \t]+"),
    ("OTHER",          r"."),
]

MASTER_RE = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in TOKEN_SPEC),
    re.MULTILINE,
)


def tokenise(source: str) -> list[dict]:
    tokens = []
    for m in MASTER_RE.finditer(source):
        tokens.append({
            "type":  m.lastgroup,
            "value": m.group(),
            "start": m.start(),
            "end":   m.end(),
        })
    return tokens


def char_to_line(source: str, pos: int) -> int:
    """Return 1-based line number for a character offset."""
    return source[:pos].count("\n") + 1


# ---------------------------------------------------------------------------
# Edit helpers
# ---------------------------------------------------------------------------

def apply_edits(source: str, edits: list) -> str:
    for _, pos, text in sorted(edits, key=lambda e: e[1], reverse=True):
        source = source[:pos] + text + source[pos:]
    return source


# ---------------------------------------------------------------------------
# Analyser
# ---------------------------------------------------------------------------

class Analyser:
    """
    Walks the token stream, identifies execution blocks, records
    (flag_name, start_line, end_line) in self.block_map, and collects
    LOG_FLAG insertions for the instrumented source.
    """

    def __init__(self, source: str, file_stem: str):
        self.source    = source
        self.stem      = file_stem
        self.tokens    = tokenise(source)
        self.idx       = 0
        self.edits: list = []
        self._counters: dict[str, int] = {}

        # List of dicts: {flag, start_line, end_line}
        # start_line = line of opening { (or keyword line for return)
        # end_line   = line of closing }
        self.block_map: list[dict] = []

    # ------------------------------------------------------------------ #
    # Naming
    # ------------------------------------------------------------------ #

    def _next(self, construct: str) -> str:
        n = self._counters.get(construct, 0) + 1
        self._counters[construct] = n
        return f"{construct}{n}_{self.stem}"

    # ------------------------------------------------------------------ #
    # Token navigation
    # ------------------------------------------------------------------ #

    def cur(self) -> dict | None:
        return self.tokens[self.idx] if self.idx < len(self.tokens) else None

    def adv(self) -> dict | None:
        t = self.cur()
        self.idx += 1
        return t

    def skip_trivia(self):
        while self.cur() and self.cur()["type"] in (
            "WHITESPACE", "NEWLINE", "LINE_COMMENT", "BLOCK_COMMENT", "PREPROCESSOR"
        ):
            self.adv()

    def read_parens(self):
        assert self.cur() and self.cur()["type"] == "LPAREN"
        self.adv()
        depth = 1
        while self.cur() and depth > 0:
            if self.cur()["type"] == "LPAREN":
                depth += 1
            elif self.cur()["type"] == "RPAREN":
                depth -= 1
            self.adv()

    def consume_to_semicolon(self):
        while self.cur():
            if self.adv()["type"] == "SEMICOLON":
                return

    # ------------------------------------------------------------------ #
    # Flag insertion
    # ------------------------------------------------------------------ #

    def _log_call(self, flag_name: str, indent: str) -> str:
        return f'{indent}LOG_FLAG("{flag_name}");\n'

    def _line_start_of(self, pos: int) -> int:
        return self.source.rfind("\n", 0, pos) + 1

    def _indent_at(self, pos: int) -> str:
        ls = self._line_start_of(pos)
        return re.match(r"[ \t]*", self.source[ls:]).group()

    def insert_flag_before_rbrace(self, rbrace_pos: int, flag_name: str):
        ls = self._line_start_of(rbrace_pos)
        indent = re.match(r"[ \t]*", self.source[ls:]).group()
        self.edits.append(("insert", ls, self._log_call(flag_name, indent)))

    def insert_flag_before_pos(self, pos: int, flag_name: str):
        ls = self._line_start_of(pos)
        indent = re.match(r"[ \t]*", self.source[ls:]).group()
        self.edits.append(("insert", ls, self._log_call(flag_name, indent)))

    def _record(self, flag: str, open_pos: int, close_pos: int):
        """Save flag → (start_line, end_line) into block_map."""
        self.block_map.append({
            "flag":       flag,
            "start_line": char_to_line(self.source, open_pos),
            "end_line":   char_to_line(self.source, close_pos),
        })

    def _record_return(self, flag: str, return_pos: int, close_pos: int):
        """For return statements: block spans from return line to func closing }."""
        self.block_map.append({
            "flag":       flag,
            "start_line": char_to_line(self.source, return_pos),
            "end_line":   char_to_line(self.source, close_pos),
        })

    # ------------------------------------------------------------------ #
    # Block parsers
    # ------------------------------------------------------------------ #

    def parse_braced_block(self, context: str) -> tuple[int, int]:
        open_tok = self.cur()
        assert open_tok and open_tok["type"] == "LBRACE"
        open_pos = open_tok["start"]
        self.adv()   # consume {

        has_return   = False
        return_pos   = None

        while self.cur():
            t = self.cur()

            if t["type"] == "RBRACE":
                close_pos = t["start"]
                self.adv()

                if context == "function":
                    if not has_return:
                        flag = self._next("func")
                        self.insert_flag_before_rbrace(close_pos, flag)
                        self._record(flag, open_pos, close_pos)
                    else:
                        # extend the return-flag block to include the closing }
                        for entry in reversed(self.block_map):
                            if entry["flag"].startswith("func") and entry["start_line"] == char_to_line(self.source, return_pos):
                                entry["end_line"] = char_to_line(self.source, close_pos)
                                break

                elif context in ("if", "else", "while", "for", "do"):
                    flag = self._next(context)
                    self.insert_flag_before_rbrace(close_pos, flag)
                    self._record(flag, open_pos, close_pos)

                elif context == "switch":
                    flag = self._next("switch")
                    self.insert_flag_before_rbrace(close_pos, flag)
                    self._record(flag, open_pos, close_pos)

                return open_pos, close_pos

            elif t["type"] == "LBRACE":
                self.parse_braced_block("generic")

            elif t["type"] == "KEYWORD":
                kw = t["value"]
                self.adv()

                if kw == "if":
                    self.parse_if_chain()
                elif kw == "while":
                    self.parse_while()
                elif kw == "for":
                    self.parse_for()
                elif kw == "do":
                    self.parse_do_while()
                elif kw == "switch":
                    self.parse_switch()
                elif kw == "return":
                    if context == "function":
                        flag = self._next("func")
                        self.insert_flag_before_pos(t["start"], flag)
                        has_return = True
                        return_pos = t["start"]
                        # record tentatively — end_line updated at closing }
                        self.block_map.append({
                            "flag":       flag,
                            "start_line": char_to_line(self.source, t["start"]),
                            "end_line":   char_to_line(self.source, t["start"]),
                        })
                    self.consume_to_semicolon()
                elif kw in ("break", "continue", "case", "default"):
                    self.consume_to_semicolon()
            else:
                self.adv()

        raise SyntaxError(f"Unexpected EOF inside {context} block")

    # ---- if / else if / else ------------------------------------------

    def parse_if_chain(self):
        self.skip_trivia()
        if not (self.cur() and self.cur()["type"] == "LPAREN"):
            return
        self.read_parens()
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LBRACE":
            self.parse_braced_block("if")
        else:
            self.consume_to_semicolon()
        self._try_parse_else()

    def _try_parse_else(self):
        saved = self.idx
        self.skip_trivia()
        t = self.cur()
        if t and t["type"] == "KEYWORD" and t["value"] == "else":
            self.adv()
            self.skip_trivia()
            t2 = self.cur()
            if t2 and t2["type"] == "KEYWORD" and t2["value"] == "if":
                self.adv()
                self.parse_if_chain()
            elif t2 and t2["type"] == "LBRACE":
                self.parse_braced_block("else")
            else:
                self.consume_to_semicolon()
        else:
            self.idx = saved

    # ---- while --------------------------------------------------------

    def parse_while(self):
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LPAREN":
            self.read_parens()
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LBRACE":
            self.parse_braced_block("while")
        else:
            self.consume_to_semicolon()

    # ---- for ----------------------------------------------------------

    def parse_for(self):
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LPAREN":
            self.read_parens()
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LBRACE":
            self.parse_braced_block("for")
        else:
            self.consume_to_semicolon()

    # ---- do-while -----------------------------------------------------

    def parse_do_while(self):
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LBRACE":
            self.parse_braced_block("do")
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "KEYWORD" and self.cur()["value"] == "while":
            self.adv()
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LPAREN":
            self.read_parens()
        self.consume_to_semicolon()

    # ---- switch -------------------------------------------------------

    def parse_switch(self):
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LPAREN":
            self.read_parens()
        self.skip_trivia()
        if self.cur() and self.cur()["type"] == "LBRACE":
            self.parse_switch_body()

    def parse_switch_body(self):
        open_tok = self.cur()
        assert open_tok and open_tok["type"] == "LBRACE"
        open_pos = open_tok["start"]
        self.adv()

        # track current case start for recording
        case_start_pos = open_pos

        while self.cur():
            t = self.cur()

            if t["type"] == "RBRACE":
                close_pos = t["start"]
                self.adv()
                flag = self._next("switch")
                self.insert_flag_before_rbrace(close_pos, flag)
                self._record(flag, open_pos, close_pos)
                return open_pos, close_pos

            elif t["type"] == "KEYWORD" and t["value"] in ("case", "default"):
                case_start_pos = t["start"]
                self.adv()
                while self.cur():
                    tok = self.adv()
                    if tok["type"] == "COLON" or tok["value"] == ":":
                        break

            elif t["type"] == "KEYWORD" and t["value"] == "break":
                flag = self._next("case")
                self.insert_flag_before_pos(t["start"], flag)
                break_pos = t["start"]
                self.consume_to_semicolon()
                self._record(flag, case_start_pos, break_pos)

            elif t["type"] == "KEYWORD" and t["value"] == "return":
                flag = self._next("case")
                self.insert_flag_before_pos(t["start"], flag)
                ret_pos = t["start"]
                self.consume_to_semicolon()
                self._record(flag, case_start_pos, ret_pos)

            elif t["type"] == "KEYWORD" and t["value"] == "switch":
                self.adv()
                self.parse_switch()
            elif t["type"] == "KEYWORD" and t["value"] == "if":
                self.adv()
                self.parse_if_chain()
            elif t["type"] == "KEYWORD" and t["value"] == "while":
                self.adv()
                self.parse_while()
            elif t["type"] == "KEYWORD" and t["value"] == "for":
                self.adv()
                self.parse_for()
            elif t["type"] == "LBRACE":
                self.parse_braced_block("generic")
            else:
                self.adv()

        raise SyntaxError("Unexpected EOF inside switch body")

    # ------------------------------------------------------------------ #
    # Top-level
    # ------------------------------------------------------------------ #

    def _looks_like_function_def(self) -> bool:
        i = self.idx
        paren_found = False
        while i < len(self.tokens):
            t = self.tokens[i]
            if t["type"] in ("WHITESPACE", "NEWLINE", "LINE_COMMENT",
                              "BLOCK_COMMENT", "PREPROCESSOR"):
                i += 1
                continue
            if t["type"] == "LPAREN":
                paren_found = True
                depth = 1
                i += 1
                while i < len(self.tokens) and depth > 0:
                    if self.tokens[i]["type"] == "LPAREN":
                        depth += 1
                    elif self.tokens[i]["type"] == "RPAREN":
                        depth -= 1
                    i += 1
                continue
            if paren_found and t["type"] == "LBRACE":
                return True
            if t["type"] == "SEMICOLON":
                return False
            i += 1
        return False

    def run(self):
        TYPE_KEYWORDS = {
            "void", "int", "char", "float", "double", "bool",
            "uint8_t", "uint16_t", "uint32_t", "uint64_t",
            "int8_t",  "int16_t",  "int32_t",  "int64_t",
            "static", "inline", "extern", "volatile", "const", "unsigned", "signed",
        }
        while self.cur():
            t = self.cur()
            if t["type"] in ("WHITESPACE", "NEWLINE", "LINE_COMMENT",
                              "BLOCK_COMMENT", "PREPROCESSOR"):
                self.adv()
                continue
            if t["type"] == "KEYWORD":
                kw = t["value"]
                self.adv()
                if kw == "if":        self.parse_if_chain()
                elif kw == "while":   self.parse_while()
                elif kw == "for":     self.parse_for()
                elif kw == "do":      self.parse_do_while()
                elif kw == "switch":  self.parse_switch()
                continue
            if t["value"] in TYPE_KEYWORDS or t["type"] == "OTHER":
                if self._looks_like_function_def():
                    while self.cur() and self.cur()["type"] != "LBRACE":
                        self.adv()
                    if self.cur() and self.cur()["type"] == "LBRACE":
                        self.parse_braced_block("function")
                    continue
            self.adv()

    def get_instrumented(self) -> str:
        self.run()
        return apply_edits(self.source, self.edits)
