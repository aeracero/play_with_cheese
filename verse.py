"""
verse.py - 短歌モード / 詩人モードの判定ロジック（Discord に依存しない純粋な関数）

音（モーラ）の数え方:
  - 漢字は MeCab(fugashi + unidic-lite) の読みで仮名に変換して数える
  - 小書き文字（ャュョァィゥェォヮ）は前の音と合わせて1音、ッ・ン・ー は1音
  - 句読点や括弧などは数えない
  - 読みを指定したいときはルビ記法: 漢字《かな》 / ｜任意の文字《かな》
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import fugashi

_tagger = fugashi.Tagger()

# ===== 形式（ここを書き換えてください） =====
TANKA_PATTERN = [5, 7, 5, 7, 7]      # 短歌モード: 1メッセージ = 1句
POET_PATTERN = [7, 5]                 # 詩人モード: 1メッセージ = 1行（七五調）
POET_STANZA = 4                       # 詩人モード: 何行で1連か
# ============================================


@dataclass(frozen=True)
class Rules:
    label: str
    tolerance: int            # 字余り・字足らずの許容音数（各句・各半行ごと）
    allow_symbols: bool       # 句読点・括弧（数えない）
    allow_emoji: bool         # 絵文字・カスタム絵文字（数えない）
    allow_extras: bool        # 添付ファイル・スタンプ
    allow_mentions: bool      # メンション（数えない）
    allow_alnum: bool         # 英数字（ルビ付きなら可）
    poet_split: bool          # 詩人: 七音と五音の間に空白が必須
    rhyme_len: int            # 詩人: 脚韻でそろえる行末の音数（0で脚韻なし）
    no_repeat: bool           # 一首・一連の中で同じ句（同じ読み）を禁止
    time_limit_min: int       # 前の句から何分以内に次を詠むか（0で無制限）。超えると未完成の歌は破棄
    edit_policy: str          # "delete": 編集禁止 / "recheck": 編集後も形式を守っていればOK


STRICTNESS: dict[str, Rules] = {
    "very_strict": Rules("非常に厳しい", 0, False, False, False, False, False,
                         True, 2, True, 10, "delete"),
    "strict":      Rules("厳しい",       0, True,  False, False, False, True,
                         True, 1, False, 0, "delete"),
    "normal":      Rules("普通",         1, True,  True,  False, False, True,
                         True, 1, False, 0, "recheck"),
    "gentle":      Rules("優しめ",       2, True,  True,  True,  True,  True,
                         False, 0, False, 0, "recheck"),
}
DEFAULT_STRICTNESS = "strict"

_PUNCT = set("、。，．,.！？!?「」『』（）()…‥・〜～-")
_SMALL = set("ァィゥェォャュョヮ")
_VOWEL_ROWS = {
    "a": "アカサタナハマヤラワガザダバパヵァャヮ",
    "i": "イキシチニヒミリギジヂビピィ",
    "u": "ウクスツヌフムユルグズヅブプヴゥュ",
    "e": "エケセテネヘメレゲゼデベペヶェ",
    "o": "オコソトノホモヨロヲゴゾドボポォョ",
}
_VOWEL = {c: v for v, row in _VOWEL_ROWS.items() for c in row}
_VOWEL.update({"ン": "n", "ッ": "q"})
_VOWEL_LABEL = {"a": "あ段", "i": "い段", "u": "う段", "e": "え段", "o": "お段",
                "n": "ん", "q": "っ"}

_RUBY = re.compile(r"(?:｜([^《》｜\s]+)|([\u3400-\u9fff々〆ヶA-Za-z0-9０-９]+))《([^》]+)》")
_URL = re.compile(r"https?://")
_MENTION = re.compile(r"<(?:@[!&]?|#)\d+>")
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
_ALNUM = re.compile(r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]")


def to_katakana(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


class VerseError(Exception):
    pass


def reading(text: str) -> str:
    """テキストをカタカナの読みに変換（記号は除去）。読めない語があれば VerseError"""
    def ruby(m: re.Match) -> str:
        r = to_katakana(m.group(3))
        if not re.fullmatch(r"[ァ-ヺー]+", r):
            raise VerseError(f"ルビ《{m.group(3)}》は仮名で書いてください")
        return r

    text = _RUBY.sub(ruby, text)
    out = []
    for w in _tagger(text):
        s = w.surface
        if all(c in _PUNCT for c in s):
            continue
        kana = getattr(w.feature, "kana", None)
        if kana in (None, "*", ""):
            if re.fullmatch(r"[ァ-ヺー]+", to_katakana(s)):
                kana = to_katakana(s)
            else:
                raise VerseError(f"「{s}」の読みがわかりません（例: {s}《よみ》 とルビを付けてください）")
        out.append(kana)
    return "".join(out)


def count_mora(kana: str) -> int:
    return sum(1 for c in kana if c not in _SMALL and c != " ")


def mora_vowels(kana: str) -> list[str]:
    """1音ごとの母音の列（キャ→a、ー→直前の母音）"""
    vs: list[str] = []
    for c in kana:
        if c == " ":
            continue
        if c in _SMALL and vs:
            vs[-1] = _VOWEL[c]
        elif c == "ー" and vs:
            vs.append(vs[-1])
        else:
            vs.append(_VOWEL.get(c, "?"))
    return vs


def rhyme_label(key: tuple[str, ...]) -> str:
    return "・".join(_VOWEL_LABEL.get(v, "?") for v in key)


@dataclass
class Result:
    ok: bool
    errors: list[str] = field(default_factory=list)
    reading: str = ""
    rhyme: Optional[tuple[str, ...]] = None


def _preprocess(content: str, has_extras: bool, rules: Rules) -> tuple[str, list[str]]:
    """禁止要素のチェックと、許可されている要素（数えないもの）の除去"""
    errs = []
    text = content.strip()
    if has_extras and not rules.allow_extras:
        errs.append("添付ファイル・スタンプは使えません")
    if "\n" in text:
        errs.append("1メッセージに書けるのは1行（1句）だけです")
    if _URL.search(text):
        errs.append("URLは使えません")
    if _MENTION.search(text):
        if rules.allow_mentions:
            text = _MENTION.sub("", text)
        else:
            errs.append("メンションは使えません")
    has_emoji = bool(_CUSTOM_EMOJI.search(text)) or any(
        unicodedata.category(c) == "So" for c in text)
    if has_emoji:
        if rules.allow_emoji:
            text = _CUSTOM_EMOJI.sub("", text)
            text = "".join(c for c in text if unicodedata.category(c) != "So")
        else:
            errs.append("絵文字は使えません")
    if not rules.allow_symbols and any(c in _PUNCT for c in text):
        errs.append("句読点・記号は使えません（仮名と漢字だけで詠んでください）")
    if not rules.allow_alnum and _ALNUM.search(text):
        errs.append("英数字は使えません（ルビ付きでも不可）")
    if not text.strip():
        errs.append("本文がありません")
    return text.strip(), errs


def _count_err(name: str, need: int, kana: str, rules: Rules) -> Optional[str]:
    n = count_mora(kana)
    if abs(n - need) <= rules.tolerance:
        return None
    allow = f"{need}音" if rules.tolerance == 0 else f"{need}音（±{rules.tolerance}まで可）"
    return f"{name}は{allow}です（{n}音: {kana}）"


def check_tanka(content: str, index: int, rules: Rules, prev_readings: list[str],
                has_extras: bool = False) -> Result:
    text, errs = _preprocess(content, has_extras, rules)
    if errs:
        return Result(False, errs)
    try:
        kana = reading(text)
    except VerseError as e:
        return Result(False, [str(e)])
    e = _count_err(f"第{index + 1}句", TANKA_PATTERN[index], kana, rules)
    if e:
        return Result(False, [e], kana)
    if rules.no_repeat and kana in prev_readings:
        return Result(False, ["同じ一首の中で同じ句は使えません"], kana)
    return Result(True, reading=kana)


def check_poet(content: str, index: int, rules: Rules, rhyme_with: Optional[tuple],
               prev_readings: list[str], has_extras: bool = False) -> Result:
    text, errs = _preprocess(content, has_extras, rules)
    if errs:
        return Result(False, errs)
    try:
        if rules.poet_split:
            parts = text.split()
            if len(parts) != len(POET_PATTERN):
                return Result(False, ["七五調で、七音と五音の間に空白を入れて書いてください"
                                      "（例: しずかなよるに ほしがふる）"])
            kanas = [reading(p) for p in parts]
            errs = [e for e in (_count_err(n, need, k, rules) for n, need, k
                                in zip(["前半", "後半"], POET_PATTERN, kanas)) if e]
            kana = " ".join(kanas)
        else:
            kana = reading(text)
            e = _count_err("1行", sum(POET_PATTERN), kana, rules)
            errs = [e] if e else []
    except VerseError as e:
        return Result(False, [str(e)])
    if errs:
        return Result(False, errs, kana)
    if rules.no_repeat and kana in prev_readings:
        return Result(False, ["同じ連の中で同じ行は使えません"], kana)
    key = tuple(mora_vowels(kana)[-rules.rhyme_len:]) if rules.rhyme_len else None
    if key and rhyme_with and key != rhyme_with:
        n = "行末" if rules.rhyme_len == 1 else f"行末{rules.rhyme_len}音"
        return Result(False, [f"脚韻: この連は{n}を「{rhyme_label(rhyme_with)}」でそろえてください"
                              f"（今は「{rhyme_label(key)}」）"], kana, key)
    return Result(True, reading=kana, rhyme=key)


def cycle_length(mode: str) -> int:
    return len(TANKA_PATTERN) if mode == "tanka" else POET_STANZA


def check(mode: str, content: str, index: int, rules: Rules, rhyme_with: Optional[tuple],
          prev_readings: list[str], has_extras: bool = False) -> Result:
    if mode == "tanka":
        return check_tanka(content, index, rules, prev_readings, has_extras)
    return check_poet(content, index, rules, rhyme_with, prev_readings, has_extras)


def expected_hint(mode: str, index: int, rhyme_with: Optional[tuple]) -> str:
    if mode == "tanka":
        return f"次は第{index + 1}句（{TANKA_PATTERN[index]}音）です"
    hint = f"次は{index + 1}行目（七五調）です"
    if rhyme_with:
        hint += f"。行末は「{rhyme_label(rhyme_with)}」"
    return hint


def describe_rules(mode: str, rules: Rules) -> list[str]:
    """アナウンス用のルール説明"""
    out = []
    if mode == "tanka":
        out.append("1メッセージ1句、5・7・5・7・7の順に詠む（5句で一首）")
    else:
        sep = "（七音と五音の間に空白）" if rules.poet_split else ""
        out.append(f"1メッセージ1行の七五調{sep}、{POET_STANZA}行で1連")
        if rules.rhyme_len:
            out.append("連の中は行末の母音をそろえる（脚韻）" if rules.rhyme_len == 1
                       else f"連の中は行末{rules.rhyme_len}音の母音をそろえる（脚韻）")
    out.append("字余り・字足らずは一切なし" if rules.tolerance == 0
               else f"字余り・字足らずは±{rules.tolerance}音まで")
    if not rules.allow_symbols:
        out.append("句読点・記号も禁止（仮名と漢字のみ）")
    if not rules.allow_alnum:
        out.append("英数字は禁止")
    if not rules.allow_emoji:
        out.append("絵文字禁止")
    if rules.no_repeat:
        out.append("同じ句の使い回し禁止")
    if rules.time_limit_min:
        out.append(f"前の句から{rules.time_limit_min}分以内に次を詠まないと、未完成の歌は消える")
    out.append("編集禁止（編集すると削除）" if rules.edit_policy == "delete"
               else "編集してもよいが、形式を外れたら削除")
    out.append("読みの指定は 漢字《かな》、守らない投稿は即削除")
    return out
