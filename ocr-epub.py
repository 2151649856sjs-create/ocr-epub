"""扫描版中文书 → 可重排 EPUB 的 OCR 流水线。

  ocr-epub.py ocr   <pdf>   -o book.jsonl [--dpi 220] [--first 1] [--last N]
  ocr-epub.py epub  <jsonl> -o book.epub  [--title ...] [--author ...] [--fixes f.txt]
  ocr-epub.py check <epub>

ocr 逐页渲染图片跑 RapidOCR，每页写一条 JSON 记录（含文字框坐标），已跑过的页会跳过，
可以中断续跑。epub 用坐标做版面重排：短行收尾、缩进开段、同基线碎片缝合，再清破折号
和省略号，最后手工打 EPUB3 包。

依赖见 requirements.txt。onnxruntime 1.17 与 numpy 2.x 不兼容，安装时务必保留
numpy<2，否则识别进程会直接段错误。
"""

import argparse
import datetime
import difflib
import html
import json
import os
import re
import sys
import xml.dom.minidom as minidom
import zipfile

HEAD_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9〇零一二三四五六七八九十百两]{1,5}\s*[章篇部节回][\s·・.、:：\-—]{0,3}\S{0,18}"
    r"|[\u4e00-\u9fff]{0,4}(?:序|序言|前言|导论|引言|绪论|后记|结语|结论|尾声|手记|跋|致谢"
    r"|附录|参考文献|目录|内容简介|内容提要|出版说明|作者简介|推荐序|自序)"
    r")[\s·・:：、]{0,3}$"
)
CIP_RE = re.compile(r"^\s*图\s*书\s*在\s*版\s*编\s*目")
# Folio numbers in these scans are always digits or romans; Chinese numerals are
# excluded because a bare "一" box is really a stray em-dash glyph.
NUM_ONLY_RE = re.compile(r"^\s*[·\-—–\s]*[\dIVXLCivxlcdm]{1,10}[\s·\-—–.]*$")
# A bibliography entry opens with its bracketed serial. Continuation lines in
# these lists are often uniformly short, so geometry alone welds neighbours.
REF_RE = re.compile(r"^\s*[\[［]\s*\d{1,3}\s*[\]］」]")
LATIN_DECOR_RE = re.compile(r"^[A-Za-z][A-Za-z .·'\-]*$")
# Whole-page watermarks from the scan-trading sites that ship these PDFs.
META_RE = re.compile(
    r"annas-blog|Anna'sArchive|Document generated|filename_decoded|\"filename\"|pdg_|"
    r"sha256|sha1|md5|crc32|total_pixels|total_pages|zip_password|uncompressed_size|pdf_generation"
)
CJK = r"[　-〿一-鿿＀-￯]"

# The em dash "——" is the most badly mangled glyph in these scans: it comes back
# as ~, -, 一 or mixtures. Worse, a plain "一" often OCRs as a two-stroke dash
# run, so a run only counts as a dash when it cannot be read as the numeral one
# -- i.e. unless a measure word follows it or 第/惟/那/另... precedes it.
MEASURE = set("种个些次片线代直切点边面双番阵遭批桩张本部件条只位家口头眼笑样起向对日瞬旦刻股缕抹汪滴名")
PREV_ONE = set("第惟唯那这每另有")
# A genuine reduplicative reading ("one by one") survives after 都/便/将/... .
ONE_ONE_AFTER = set("都便将已再逐统会")
DASH_RUN_RE = re.compile(
    r"(?<=[\u4e00-\u9fff。、，：；…”])[—–~～-]{1,4}(?=[\u4e00-\u9fff“])"
    r"|(?<=[\u4e00-\u9fff])一[—–~～-]{1,3}(?=[\u4e00-\u9fff])"
    r"|(?<=[\u4e00-\u9fff])[—–~～-]{1,3}一(?=[\u4e00-\u9fff])"
    r"|(?<=[\u4e00-\u9fff])一一(?=[\u4e00-\u9fff])"
)
LONE_HYPHEN_RE = re.compile(r"(?<=[\u4e00-\u9fff])[-–~～](?=[\u4e00-\u9fff])")
# Dialogue dashes open a paragraph or a quotation, where there is no Han char
# behind them for DASH_RUN_RE to anchor on.
LEAD_DASH_RE = re.compile(r"(?:^|(?<=[“\"‘'（(\s]))[-—–~～]{1,3}(?=[\u4e00-\u9fff])")

# Character confusions that are safe to correct anywhere in a Chinese scan.
FIXES = [
    (re.compile(r"进人"), "进入"),
    (re.compile(r"自已"), "自己"),
    (re.compile(r"死广"), "死亡"),
    (re.compile(r"[颜频]死"), "濒死"),
    (re.compile(r"濒死体检"), "濒死体验"),
    # 引入 mis-scans as 引人; 吸引人 is the one legitimate reading of that pair.
    (re.compile(r"(?<!吸)引人"), "引入"),
]

# The ellipsis glyph survives as a run of middots, real ellipses and full
# stops. Anchoring on Han keeps the CIP record's "R339.3" style numbering out
# of the way, and a stray dot before a dash is dash noise, not an ellipsis.
PUNCS = [
    (re.compile(r"(?<=[\u4e00-\u9fff。！？，、”])[·…\.]{2,}(?=[\u4e00-\u9fff“])"), "……"),
    (re.compile(r"(?<=[\u4e00-\u9fff])[·…\.]{2,}(?=[，。！？；”])"), "……"),
    (re.compile(r"(?<=[\u4e00-\u9fff])[·…\.]{2,}$"), "……"),
    (re.compile(r"(?<=[\u4e00-\u9fff])[·…]{1,3}(?=——)"), ""),
    (re.compile(r"(?<=[\u4e00-\u9fff])…(?=[\u4e00-\u9fff])"), "……"),
    (re.compile(r"[：:]{2,}(?=[“‘'\"])"), "："),
    (re.compile(r"(?<=[，。、])：(?=[\u4e00-\u9fff])"), ""),
    (re.compile(r'(?<=[\u4e00-\u9fff。！？，、’])"'), "”"),
    # A CJK paragraph never legitimately trails a half-width question mark.
    (re.compile(r"(?<=[\u4e00-\u9fff”])[?]"), "？"),
    (re.compile(r"(?<=[\u4e00-\u9fff”])[!]"), "！"),
    # A CJK paragraph never legitimately trails a comma; the scan reads the
    # sentence-final 。 as ，.
    (re.compile(r"，$"), "。"),
]


def load_fixes(path):
    """Per-book corrections, one `错=>对` pair per line.

    Some dash runs cannot be settled by context alone -- the scan turns the
    second stroke of "——" into a literal 一, so "都有—一" reads either as "都有一"
    or as "都有——". Each pair here was checked against the printed page, which
    makes them book-specific data rather than tool logic.

    A `re:` prefix makes the left side a regular expression, for repairs that
    need an anchor. A heading whose closing quote the scan dropped must not be
    fixed by a bare substring: the same title appears whole in the TOC.
    """
    pairs = []
    if not path:
        return pairs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=>" not in line:
                continue
            bad, good = line.split("=>", 1)
            if bad.startswith("re:"):
                bad = re.compile(bad[3:])
            pairs.append((bad, good))
    return pairs


class Polisher:
    def __init__(self, literal_pairs=()):
        self.pairs = list(literal_pairs)

    def _dash_repl(self, m):
        s = m.string
        pre = s[m.start() - 1] if m.start() > 0 else ""
        post = s[m.end()] if m.end() < len(s) else ""
        if m.group(0) == "一一" and pre in ONE_ONE_AFTER:
            return "一一"
        if post in MEASURE or pre in PREV_ONE:
            return "一"
        return "——"

    def _lead_repl(self, m):
        # A paragraph-opening run followed by a bare measure word is a mangled 一;
        # a real dialogue dash is followed by something else.
        post = m.string[m.end()] if m.end() < len(m.string) else ""
        if post in MEASURE and len(m.group(0)) <= 2:
            return "一"
        return "——"

    def __call__(self, text):
        t = text
        for pat, rep in FIXES:
            t = pat.sub(rep, t)
        t = DASH_RUN_RE.sub(self._dash_repl, t)
        t = LEAD_DASH_RE.sub(self._lead_repl, t)
        t = LONE_HYPHEN_RE.sub("一", t)
        # Numeric ranges print with a one-em rule, which the scan reads as 一.
        # Percentages take the same rule ("30%一50%").
        t = re.sub(r"(?<=[0-9%])一(?=[0-9])", "—", t)
        for bad, good in self.pairs:
            if isinstance(bad, str):
                t = t.replace(bad, good)
            else:
                t = bad.sub(good, t)
        for pat, rep in PUNCS:
            t = pat.sub(rep, t)
        return t


def similar(a, b):
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() > 0.72


def head_no(t):
    m = re.match(r"^第\s*([0-9〇零一二三四五六七八九十百两]{1,5})\s*[章篇部回]", t)
    return m.group(1) if m else None


def same_head(a, b):
    """In a book whose chapters share a long title stem (第八章 …马蝇效应 /
    第九章 …德西效应), similarity alone cannot tell a rerun of one chapter from
    the next chapter's opening. Chapter numbers can."""
    ka, kb = head_no(a), head_no(b)
    if ka and kb:
        return ka == kb
    return similar(a, b)


def norm(text):
    t = text.replace("\u3000", " ").strip()
    t = re.sub(r"(?<=%s) (?=%s)" % (CJK[1:-1], CJK[1:-1]), "", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t


# ---------------------------------------------------------------- ocr command


def done_pages(path):
    seen = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(json.loads(line)["page"])
                except Exception:
                    pass
    return seen


def cmd_ocr(args):
    import time

    import fitz
    from rapidocr_onnxruntime import RapidOCR

    pages_dir = args.pages_dir or os.path.splitext(args.jsonl)[0] + "_pages"
    os.makedirs(pages_dir, exist_ok=True)
    doc = fitz.open(args.pdf)
    total = doc.page_count
    first, last = args.first, min(args.last or total, total)
    want = list(range(first - 1, last))
    skip = done_pages(args.jsonl)
    todo = [p for p in want if p not in skip]
    print(
        "pages=%d range=%d..%d already=%d to_run=%d -> %s"
        % (total, first, last, len(want) - len(todo), len(todo), args.jsonl),
        flush=True,
    )
    if not todo:
        print("nothing to do", flush=True)
        return 0

    ocr = RapidOCR()
    zoom = args.dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    started = time.time()
    with open(args.jsonl, "a", encoding="utf-8") as out:
        for n, pno in enumerate(todo, 1):
            page = doc[pno]
            img_path = os.path.join(pages_dir, "p%03d.png" % (pno + 1))
            if not os.path.exists(img_path):
                page.get_pixmap(matrix=matrix).save(img_path)

            t0 = time.time()
            # text_score=0 keeps every detected box, so a line whose recognition
            # came back empty can be retried below instead of vanishing silently.
            result, _ = ocr(img_path, use_cls=args.cls, text_score=0.0)
            elapsed = round(time.time() - t0, 2)
            lines, rescued, img = [], 0, None
            for box, text, score in result or []:
                text = (text or "").strip()
                if not text:
                    # The orientation classifier occasionally flips a flat crop
                    # by 180° and the recognizer then returns nothing. Retry the
                    # line as an axis-aligned crop with det+cls off.
                    if img is None:
                        # cv2.imread chokes on non-ASCII paths; decode bytes instead.
                        import cv2
                        import numpy as np

                        buf = np.fromfile(img_path, dtype=np.uint8)
                        dec = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
                        img = dec if dec is not None else np.zeros((1, 1, 3), np.uint8)
                    bx = [pt[0] for pt in box]
                    by = [pt[1] for pt in box]
                    x0, y0 = max(0, int(min(bx)) - 8), max(0, int(min(by)) - 6)
                    crop = img[y0 : int(max(by)) + 6, x0 : int(max(bx)) + 8]
                    if crop.size:
                        retry, _ = ocr(crop, use_det=False, use_cls=False)
                        if retry:
                            text, score = retry[0][0].strip(), retry[0][1]
                            rescued += 1
                if not text:
                    continue
                xs = [pt[0] for pt in box]
                ys = [pt[1] for pt in box]
                lines.append(
                    {
                        "text": text,
                        "score": round(float(score), 4),
                        "x0": round(min(xs), 1),
                        "y0": round(min(ys), 1),
                        "x1": round(max(xs), 1),
                        "y1": round(max(ys), 1),
                    }
                )
            lines.sort(key=lambda l: (l["y0"], l["x0"]))
            out.write(
                json.dumps(
                    {
                        "page": pno,
                        "img": [int(page.rect.width * zoom), int(page.rect.height * zoom)],
                        "secs": elapsed,
                        "lines": lines,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()
            el = time.time() - started
            eta = el / n * (len(todo) - n)
            print(
                "[%d/%d] p%03d lines=%d%s %.2fs eta=%.1fm"
                % (n, len(todo), pno + 1, len(lines), (" rescued=%d" % rescued) if rescued else "", elapsed, eta / 60),
                flush=True,
            )
    print("OCR COMPLETE", flush=True)
    return 0


# ------------------------------------------------------------ layout / reflow


def read_rows(lines):
    """Rebuild printed lines from OCR boxes.

    A line that hangs below the baseline of its neighbours (an indented
    continuation, a closing clause) still belongs to that row, so sorting on y
    alone shuffles its fragments in front of the text that precedes them.
    Boxes within a fraction of the typical line pitch are treated as one row,
    ordered left to right and then welded back into a single line when they are
    near enough to touch -- the scanner often splits one printed line into a
    fragment plus a stray dash glyph.
    """
    ls = sorted(lines, key=lambda l: l["y0"])
    if len(ls) < 3:
        return ls
    # The lower quartile, not the median: a page with a display line or a big
    # top gap has three or four body lines at most, and there the median gap is
    # the gap *between* lines, which would then be treated as one row.
    gaps = sorted(ls[i + 1]["y0"] - ls[i]["y0"] for i in range(len(ls) - 1))
    pitch = gaps[len(gaps) // 4]
    tol = max(6.0, pitch * 0.4) if pitch > 0 else 6.0
    rows, row = [], []
    for l in ls:
        if row and l["y0"] - row[-1]["y0"] > tol:
            rows.append(row)
            row = []
        row.append(l)
    rows.append(row)

    out = []
    for row in sorted(rows, key=lambda r: min(l["y0"] for l in row)):
        cur = None
        for l in sorted(row, key=lambda l: l["x0"]):
            # A stray 1-2 digit box beside the text column is a folio the
            # scanner caught in the gutter, not a word fragment.
            if re.fullmatch(r"\d{1,2}", l["text"]):
                out.append(dict(l))
                continue
            if cur is not None:
                cw = (cur["x1"] - cur["x0"]) / max(1, len(cur["text"]))
                # Fragments of one printed line share a vertical band; boxes
                # stacked without overlap are consecutive lines, not fragments.
                if l["x0"] - cur["x1"] <= 1.5 * cw and l["y0"] < cur["y1"] - 2:
                    cur = dict(
                        cur,
                        text=cur["text"] + l["text"],
                        x1=max(cur["x1"], l["x1"]),
                        y1=max(cur["y1"], l["y1"]),
                        score=min(cur["score"], l["score"]),
                    )
                    continue
            if cur is not None:
                out.append(cur)
            cur = dict(l)
        if cur is not None:
            out.append(cur)
    return out


def load_pages(jsonl):
    pages = {}
    with open(jsonl, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            rec = json.loads(raw)
            rec["lines"] = read_rows(rec["lines"])
            pages[rec["page"]] = rec
    return [pages[i] for i in sorted(pages)]


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def page_frame(rec):
    """Estimate the body text column (left edge, right edge, char width)."""
    long = [ln for ln in rec["lines"] if len(ln["text"]) >= 12]
    pool = long or rec["lines"]
    if not pool:
        return None
    left = pct([ln["x0"] for ln in long], 0.2) if long else min(ln["x0"] for ln in pool)
    right = pct([ln["x1"] for ln in long], 0.8) if long else max(ln["x1"] for ln in pool)
    cw = pct([(ln["x1"] - ln["x0"]) / max(len(ln["text"]), 1) for ln in long], 0.5) or 40.0
    return left, right, cw


def join_vertical(lines, h):
    """Vertical titles (前言, 序) come back one character per box, stacked in a
    column; weld them back into one line so the head regex can see them."""
    singles = sorted((l for l in lines if len(l["text"]) == 1), key=lambda l: l["y0"])
    merged, taken = [], set()
    for i, l in enumerate(singles):
        if id(l) in taken:
            continue
        col = [l]
        for m in singles[i + 1 :]:
            if id(m) in taken:
                continue
            if abs(m["x0"] - col[-1]["x0"]) <= 12 and -10 <= m["y0"] - col[-1]["y1"] <= 45:
                col.append(m)
                taken.add(id(m))
        if len(col) >= 2 and col[0]["y1"] < h * 0.3:
            merged.append(
                dict(
                    col[0],
                    text="".join(c["text"] for c in col),
                    x1=max(c["x1"] for c in col),
                    y1=max(c["y1"] for c in col),
                    score=min(c["score"] for c in col),
                )
            )
            taken.add(id(l))
    return sorted([l for l in lines if id(l) not in taken] + merged, key=lambda l: (l["y0"], l["x0"]))


def is_toc(texts):
    """TOC pages list many folio numbers plus 第X节 entries; a body page carries
    exactly one folio."""
    compact = [t.replace(" ", "") for t in texts]
    if any(t.lower().startswith(("目录", "contents")) for t in compact):
        return True
    nums = sum(1 for t in compact if re.fullmatch(r"[0-9IVXLCivxlcdm]{1,4}", t))
    secs = sum(1 for t in compact if re.match(r"^第[一二三四五六七八九十百]{1,4}[节章]", t))
    return nums >= 3 and secs >= 1


def repeated_heads(pages):
    """A short line in the top band that recurs on many pages is a running head.
    Position alone is not enough: this one sits near the inner margin and drifts
    with the scan, so it survives any margin test."""
    cnt = {}
    for rec in pages:
        h = rec["img"][1]
        for ln in rec["lines"]:
            t = norm(ln["text"])
            if t and ln["y1"] < h * 0.14 and len(t) <= 30:
                cnt[t] = cnt.get(t, 0) + 1
    return {t for t, c in cnt.items() if c >= 3}


def is_divider(rec, body_h):
    """A chapter divider is a near-empty page set in display type."""
    lines = rec["lines"]
    if not lines or len(lines) > 6:
        return False
    # The detector sometimes welds two body lines into one tall box. Its text
    # then carries sentence punctuation and spans the full column, which display
    # type never does.
    if any(re.search(r"[。！？]", ln["text"]) for ln in lines):
        return False
    big = [ln for ln in lines if (ln["y1"] - ln["y0"]) > 1.8 * body_h]
    if len(big) >= 2:
        return True
    # "第十二章" and its title can both come back just under the 1.8 threshold;
    # on a page of at most four lines, two display-sized lines settle it.
    display = [ln for ln in lines if (ln["y1"] - ln["y0"]) > 1.5 * body_h]
    if len(lines) <= 4 and len(display) >= 2:
        return True
    # Some dividers are just the Part graphic plus a title line, where only the
    # graphic is display-sized.
    return len(lines) <= 3 and max(ln["y1"] - ln["y0"] for ln in lines) > 2.0 * body_h


def strip_furniture(pages, polish, blacklist=()):
    """Drop scanner-metadata pages, TOC pages, gutter decorations, running heads
    and folio numbers; mark chapter dividers as chapter boundaries."""
    heights = [ln["y1"] - ln["y0"] for rec in pages for ln in rec["lines"] if len(ln["text"]) >= 12]
    body_h = pct(heights, 0.5) or 30.0
    heavy = repeated_heads(pages)
    heavy_list = sorted(heavy)
    kept, dropped = [], []
    for rec in pages:
        h = rec["img"][1]
        w = rec["img"][0]
        rec = dict(rec, lines=join_vertical(rec["lines"], h))
        texts = [norm(ln["text"]) for ln in rec["lines"]]
        if sum(1 for t in texts if META_RE.search(t.replace(" ", ""))) >= 2:
            dropped.append((rec["page"], "[whole page: scanner metadata]"))
            continue
        if is_toc(texts):
            dropped.append((rec["page"], "[whole page: table of contents]"))
            continue
        if is_divider(rec, body_h):
            dropped.append((rec["page"], "[chapter divider]"))
            kept.append(dict(rec, lines=[], runhead="", divider=True))
            continue
        frame = page_frame(rec)
        out = []
        runhead = ""
        for ln, t in zip(rec["lines"], texts):
            if not t:
                continue
            if LATIN_DECOR_RE.match(t) and len(t) <= 25:
                dropped.append((rec["page"], t))
                continue
            # A bare number is a folio only in the page margins. In a
            # bibliography a year ("2012.") sits alone on the body baseline and
            # must survive, or the reference's date disappears.
            if NUM_ONLY_RE.match(t) and (ln["y1"] < h * 0.14 or ln["y0"] > h * 0.86):
                dropped.append((rec["page"], t))
                continue
            # A speck the scanner read as punctuation ("。`") is not a line.
            if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
                dropped.append((rec["page"], t))
                continue
            if frame:
                left, right, cw = frame
                if ln["x1"] < left - cw or ln["x0"] > right + cw:
                    dropped.append((rec["page"], t))
                    continue
            # Running heads hug the outer edge of the top band -- or, in this
            # scan, sit near the inner margin -- so repetition is the reliable
            # signal. The head text still marks where a chapter changes.
            if ln["y1"] < h * 0.14 and (
                t in heavy
                or any(similar(t, hh) for hh in heavy_list)
                or ln["x0"] > w * 0.6
                or ln["x1"] < w * 0.2
            ):
                # The head often carries a Latin decoration ("LifeafterLife")
                # welded to its front; keep only the Han part.
                t2 = re.sub(r"^[A-Za-z][A-Za-z .·'\-]*", "", t)
                t2 = re.sub(r"[A-Za-z][A-Za-z .·'\-]*$", "", t2).strip()
                if t2:
                    # A head with a dropped glyph ("增定律") is still the same
                    # running head; record the recurring spelling so it stays
                    # recognised as furniture on every page. Only a substring
                    # match is trusted here -- two different chapter heads share
                    # most of their characters and would otherwise swap.
                    if t2 not in heavy:
                        t2 = next((hh for hh in heavy_list if t2 in hh), t2)
                    if len(t2) <= 30 and len(t2) > len(runhead):
                        runhead = polish(t2)
                dropped.append((rec["page"], t))
                continue
            out.append(dict(ln, text=t))
        kept.append(dict(rec, lines=out, runhead=runhead))

    # OCR mangles a glyph in a running head now and then; settle each chapter on
    # the variant that recurs most.
    variants = {}
    for rec in kept:
        t = rec.get("runhead", "")
        m = re.match(r"^(第[0-9〇零一二三四五六七八九十百两]{1,5}[章篇部回])", t)
        if m:
            variants.setdefault(m.group(1), {})
            variants[m.group(1)][t] = variants[m.group(1)].get(t, 0) + 1
    canon = {k: max(v, key=lambda t: (v[t], len(t))) for k, v in variants.items()}
    for rec in kept:
        m = re.match(r"^(第[0-9〇零一二三四五六七八九十百两]{1,5}[章篇部回])", rec.get("runhead", ""))
        if m and canon.get(m.group(1)):
            rec["runhead"] = canon[m.group(1)]

    # A divider carries no usable title of its own (it is display art), so take
    # the chapter title from the running head that follows it.
    for i, rec in enumerate(kept):
        if not rec.get("divider"):
            continue
        cands = {}
        for nxt in kept[i + 1 : i + 9]:
            t = nxt.get("runhead", "")
            if t and t not in blacklist:
                cands[t] = cands.get(t, 0) + 1
        # Prefer the variant that recurs: OCR mangles one glyph now and then.
        rec["chapter_title"] = max(cands, key=lambda t: (cands[t], len(t))) if cands else ""
    return kept, dropped


def clean_head(t):
    """Tidy a heading: drop stray spaces, turn the scan's '第一章·标题' middot
    into a space, and separate '第一节' from a title OCR ran together."""
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[·・]", " ", t)
    t = re.sub(r"^(第[0-9〇零一二三四五六七八九十百两]{1,5}[章篇部节回])(?=\S)", r"\1 ", t)
    return t.strip()


def assemble(pages, polish, blacklist):
    """Reflow OCR lines into heading / paragraph tokens.

    Two geometric signals drive breaks: a line stopping well short of its own
    column's right edge closes a paragraph, and a line indented from the
    dominant left edge opens one. Narrow quote blocks share one indented left
    edge, so indentation only breaks when the previous line was not indented.
    """
    tokens = []
    buf = ""
    current_head = ""
    last_head = ""
    since_head = 0

    def flush():
        nonlocal buf, since_head
        if buf.strip():
            since_head += len(buf)
            tokens.append(("para", polish(buf.strip())))
        buf = ""

    def emit_head(title, level=1):
        nonlocal current_head, last_head, since_head
        title = clean_head(title)
        if not title:
            return
        # A chapter repeats on every recto running head, so compare against the
        # chapter itself rather than whatever subsection came last.
        ref = current_head if level <= 1 else last_head
        if ref and same_head(ref, title):
            return
        flush()
        tokens.append(("head", title, level))
        last_head = title
        if level <= 1:
            current_head = title
        since_head = 0

    for rec in pages:
        if rec.get("divider"):
            emit_head(rec.get("chapter_title") or "", 1)
            continue
        frame = page_frame(rec)
        if not frame:
            continue
        left, page_right, cw = frame
        rh = rec.get("runhead", "")
        if rh and rh not in blacklist and current_head and since_head > 400 and not same_head(rh, current_head):
            emit_head(rh, 1)
        classes = []
        for ln in rec["lines"]:
            for cls in classes:
                if abs(ln["x0"] - cls[0]) <= 1.5 * cw:
                    cls[1] = max(cls[1], ln["x1"])
                    break
            else:
                classes.append([ln["x0"], ln["x1"]])

        def class_right(x0):
            for cls in classes:
                if abs(x0 - cls[0]) <= 1.5 * cw:
                    return cls[1]
            return page_right

        # A page's last line ending short is layout, not a paragraph end; the
        # indent of the next page's first line is what actually marks a break.
        # Except when the carried text stops mid-sentence: then the page break
        # cut a paragraph in half and the indent on the next page is a lie.
        prev_indent = bool(buf.strip()) and buf.strip()[-1] not in "。！？：；…”』」》"
        prev_short = False
        prev_head = False
        ls = rec["lines"]
        if len(ls) >= 3:
            gaps = sorted(ls[i + 1]["y0"] - ls[i]["y0"] for i in range(len(ls) - 1))
            pitch = gaps[len(gaps) // 2] or 0.0
        else:
            pitch = 0.0
        for i, ln in enumerate(ls):
            t = ln["text"]
            # The copyright page opens at the CIP record itself, not at the top
            # of whatever page it happens to share with the teaser.
            if CIP_RE.match(t):
                emit_head("版权信息", 1)
            if REF_RE.match(t) and buf.strip():
                flush()
            if HEAD_RE.match(t):
                lvl = 2 if re.match(r"^\s*第\s*[0-9〇零一二三四五六七八九十百两]{1,5}\s*[节回]", t) else 1
                emit_head(polish(re.sub(r"\s+", "", t)), lvl)
                prev_indent = False
                prev_short = False
                prev_head = True
                continue
            # A short line floating between two wide gaps, after a finished
            # sentence, is a subsection title; without this it would be welded
            # onto the paragraph that follows it.
            if pitch and i and i + 1 < len(ls) and 2 <= len(t) <= 18:
                gap_before = ln["y0"] - ls[i - 1]["y1"]
                gap_after = ls[i + 1]["y0"] - ln["y1"]
                if (
                    gap_before > 1.35 * pitch
                    and gap_after > 1.35 * pitch
                    and not re.search(r"[。，、；：！？…”』」》]$", t)
                    and (prev_head or not buf.strip() or buf.strip()[-1] in "。！？…”』」》")
                ):
                    emit_head(polish(t), 3)
                    prev_indent = False
                    prev_short = False
                    prev_head = True
                    continue
            prev_head = False
            if t.startswith("*"):
                tokens.append(("para", "※ " + polish(t.lstrip("* ").strip())))
                continue
            indent = ln["x0"] > left + 1.2 * cw
            short = ln["x1"] < class_right(ln["x0"]) - 3.0 * cw
            if buf and (prev_short or (indent and not prev_indent)):
                flush()
            if buf and re.search(r"[A-Za-z0-9]$", buf) and re.match(r"^[A-Za-z0-9]", t):
                buf += " " + t
            else:
                buf += t
            prev_indent = indent
            # A one-character box that stops short is a letter-spaced label in
            # the colophon, not a paragraph ending.
            prev_short = short and len(t) > 1
    flush()
    return tokens


def to_chapters(tokens, first_title="序"):
    """Level-1 heads open a chapter file; deeper heads stay inline in it."""
    chapters = []
    cur = {"title": first_title, "level": 1, "paras": []}
    for tok in tokens:
        if tok[0] == "head":
            _, text, level = tok
            if level <= 1:
                if cur["paras"]:
                    chapters.append(cur)
                cur = {"title": text, "level": 1, "paras": []}
            else:
                cur["paras"].append(("h%d" % min(level, 4), text))
        elif tok[0] == "para":
            cur["paras"].append(("p", tok[1]))
    if cur["paras"]:
        chapters.append(cur)
    return chapters


# --------------------------------------------------------------- epub command


def xesc(s):
    return html.escape(s, quote=True)


def build_epub(chapters, meta, cover_path):
    ns = "http://www.w3.org/1999/xhtml"
    lang = meta["lang"]
    files = {}
    files["mimetype"] = ("application/epub+zip", True)
    manifest_items, spine_items = [], []

    for idx, ch in enumerate(chapters, 1):
        fn = "chapter%03d.xhtml" % idx
        body = "\n".join(
            "    <p>%s</p>" % xesc(text) if tag == "p" else "    <%s>%s</%s>" % (tag, xesc(text), tag)
            for tag, text in ch["paras"]
        )
        files[fn] = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<!DOCTYPE html>\n"
            '<html xmlns="%s" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="%s">\n'
            '<head><meta charset="utf-8"/><title>%s</title>\n'
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            '<body><section class="chapter">\n'
            '  <h2 class="ch-title">%s</h2>\n%s\n</section></body></html>'
            % (ns, lang, xesc(ch["title"]), xesc(ch["title"]), body),
            False,
        )
        manifest_items.append(
            '    <item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (idx, fn)
        )
        spine_items.append('    <itemref idref="c%d"/>' % idx)

    nav_lis = "\n".join(
        '      <li><a href="chapter%03d.xhtml">%s</a></li>' % (i, xesc(c["title"]))
        for i, c in enumerate(chapters, 1)
    )
    files["nav.xhtml"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="%s" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="%s">\n'
        '<head><meta charset="utf-8"/><title>目录</title></head>\n'
        '<body><nav epub:type="toc" id="toc"><h1>目录</h1>\n  <ol>\n%s\n  </ol></nav></body></html>'
        % (ns, lang, nav_lis),
        False,
    )

    ncx_points = "\n".join(
        '      <navPoint id="np%d" playOrder="%d">\n'
        '        <navLabel><text>%s</text></navLabel>\n'
        '        <content src="chapter%03d.xhtml"/>\n'
        "      </navPoint>" % (i, i, xesc(c["title"]), i)
        for i, c in enumerate(chapters, 1)
    )
    files["toc.ncx"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3987/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        '    <meta name="dtb:uid" content="%s"/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        "  </head>\n"
        "  <docTitle><text>%s</text></docTitle>\n"
        "  <navMap>\n%s\n  </navMap>\n</ncx>"
        % (xesc(meta["identifier"]), xesc(meta["title"]), ncx_points),
        False,
    )

    files["style.css"] = (
        "body{line-height:1.75;font-size:1em}\n"
        "section.chapter{margin:0 auto;max-width:38em;padding:0 1em}\n"
        "h2.ch-title{font-size:1.4em;text-align:center;margin:1.6em 0 1.2em;font-weight:bold}\n"
        "h3{font-size:1.15em;margin:1.4em 0 .6em;font-weight:bold}\n"
        "h4{font-size:1.05em;margin:1.2em 0 .5em;font-weight:bold}\n"
        "p{margin:0 0 .2em;text-indent:2em;text-align:justify}\n"
        "img{max-width:100%}",
        False,
    )

    cover_meta = ""
    cover_spine = ""
    if cover_path:
        with open(cover_path, "rb") as cf:
            files["images/cover.jpg"] = (cf.read(), "cover")
        cover_meta = (
            '    <item id="cover-image" href="images/cover.jpg" media-type="image/jpeg"'
            ' properties="cover-image"/>\n    <meta name="cover" content="cover-image"/>'
        )
        files["cover.xhtml"] = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<!DOCTYPE html>\n"
            '<html xmlns="%s" xml:lang="%s">\n'
            '<head><meta charset="utf-8"/><title>封面</title></head>\n'
            '<body style="margin:0;padding:0;text-align:center">\n'
            '  <img src="images/cover.jpg" alt="%s" style="max-width:100%%;max-height:100%%"/>\n'
            "</body></html>" % (ns, lang, xesc(meta["title"])),
            False,
        )
        cover_spine = '    <itemref idref="cover"/>\n'
        manifest_items.insert(
            0, '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
        )

    extra = ""
    if meta.get("contributor"):
        extra += "    <dc:contributor>%s</dc:contributor>\n" % xesc(meta["contributor"])
    if meta.get("subject"):
        extra += "    <dc:subject>%s</dc:subject>\n" % xesc(meta["subject"])
    if meta.get("date"):
        extra += "    <dc:date>%s</dc:date>\n" % xesc(meta["date"])

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        ' unique-identifier="bid" xml:lang="%s">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:identifier id="bid">%s</dc:identifier>\n'
        "    <dc:title>%s</dc:title>\n"
        "    <dc:creator id=\"creator\">%s</dc:creator>\n"
        "%s"
        "    <dc:language>%s</dc:language>\n"
        "    <meta property=\"dcterms:modified\">%s</meta>\n"
        "%s\n"
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="css" href="style.css" media-type="text/css"/>\n'
        "%s\n"
        "  </manifest>\n"
        '  <spine toc="ncx">\n'
        "%s    <itemref idref=\"nav\"/>\n"
        "%s\n"
        "  </spine>\n"
        "</package>"
        % (
            lang,
            xesc(meta["identifier"]),
            xesc(meta["title"]),
            xesc(meta["creator"]),
            extra,
            lang,
            meta["modified"],
            cover_meta,
            "\n".join(manifest_items),
            cover_spine,
            "\n".join(spine_items),
        )
    )
    files["content.opf"] = (opf, False)

    files["container.xml"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles>\n'
        "</container>",
        True,
    )

    with zipfile.ZipFile(meta["out"], "w") as z:
        with z.open("mimetype", "w") as f:
            f.write(b"application/epub+zip")
        for name, (data, _) in files.items():
            if name == "mimetype":
                continue
            arc = "META-INF/container.xml" if name == "container.xml" else "OEBPS/" + name
            info = zipfile.ZipInfo(arc)
            info.compress_type = zipfile.ZIP_STORED if isinstance(data, bytes) else zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return meta["out"]


def cmd_epub(args):
    polish = Polisher(load_fixes(args.fixes))
    title = args.title or os.path.splitext(os.path.basename(args.jsonl))[0]
    # The book's own title doubles as a verso running head on almost every page,
    # so it must not be mistaken for a section change.
    blacklist = {t for t in re.split(r"[：:·・\-—\s]+", title) if t}
    pages = load_pages(args.jsonl)
    pages, dropped = strip_furniture(pages, polish, blacklist)
    # Everything before the CIP page is cover art; the EPUB has a real cover.
    first = next(
        (i for i, r in enumerate(pages) if any(CIP_RE.match(ln["text"]) for ln in r["lines"])),
        0,
    )
    skipped = [r["page"] + 1 for r in pages[:first]]
    pages = pages[first:]

    tokens = assemble(pages, polish, blacklist)
    chapters = [c for c in to_chapters(tokens) if c["paras"]]

    total_chars = sum(len(t) for c in chapters for _, t in c["paras"])
    n_paras = sum(1 for c in chapters for tag, _ in c["paras"] if tag == "p")
    stem = os.path.splitext(args.out)[0]
    report, preview = args.report or stem + ".report.txt", args.preview or stem + ".preview.txt"
    with open(report, "w", encoding="utf-8") as rep:
        rep.write(
            "pages=%d cover_skipped=%s dropped_furniture=%d\n"
            "chapters=%d paragraphs=%d chars=%d\n"
            % (len(pages), skipped, len(dropped), len(chapters), n_paras, total_chars)
        )
        for c in chapters:
            rep.write(
                "  [%3dp %6dc] %s\n"
                % (
                    sum(1 for tag, _ in c["paras"] if tag == "p"),
                    sum(len(t) for _, t in c["paras"]),
                    c["title"],
                )
            )
    with open(preview, "w", encoding="utf-8") as f:
        for c in chapters:
            f.write("\n\n===== %s =====\n\n" % c["title"])
            for tag, text in c["paras"]:
                f.write(("## " + text + "\n\n") if tag != "p" else text + "\n\n")
    print("see %s / %s" % (report, preview), flush=True)

    if args.dry_run:
        return 0

    cover = args.cover
    if args.cover_from_pdf:
        import fitz

        doc = fitz.open(args.cover_from_pdf)
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(2.5, 2.5))
        cover = cover or stem + "_cover.jpg"
        pix.save(cover)
        print("cover -> %s" % cover, flush=True)

    meta = {
        "out": args.out,
        "title": title,
        "creator": args.author or title,
        "contributor": ("%s 译" % args.translator) if args.translator else "",
        "subject": args.subject or "",
        "date": args.date or "",
        "lang": args.lang,
        "identifier": args.identifier or "urn:uuid:%s" % _uid(title),
        "modified": args.modified or (datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
    }
    wrote = build_epub(chapters, meta, cover if cover and os.path.exists(cover) else None)
    print("wrote %s (%d bytes)" % (wrote, os.path.getsize(wrote)), flush=True)
    return check_file(wrote)


def _uid(title):
    import hashlib

    h = hashlib.md5(title.encode("utf-8")).hexdigest()
    return "-".join((h[:8], h[8:12], h[12:16], h[16:20], h[20:32]))


# -------------------------------------------------------------- check command


def check_file(name):
    issues = []
    zf = zipfile.ZipFile(name)
    if zf.testzip():
        issues.append("corrupt member: %s" % zf.testzip())
    infos = zf.infolist()
    first = infos[0]
    if first.filename != "mimetype":
        issues.append("mimetype is not the first entry")
    if first.compress_type != zipfile.ZIP_STORED:
        issues.append("mimetype is compressed")
    if zf.read("mimetype") != b"application/epub+zip":
        issues.append("bad mimetype payload")

    names = set(i.filename for i in infos)
    if "META-INF/container.xml" not in names:
        issues.append("missing META-INF/container.xml")
        print("\n".join("[错误] " + i for i in issues))
        return 1
    container = zf.read("META-INF/container.xml").decode("utf-8")
    opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""

    for n in [x for x in names if x.endswith((".xhtml", ".opf", ".ncx", ".xml"))]:
        try:
            minidom.parseString(zf.read(n))
        except Exception as e:
            issues.append("XML parse failed %s: %s" % (n, e))

    opf = zf.read(opf_path).decode("utf-8")
    manifest = re.findall(r'<item[^>]*id="([^"]+)"[^>]*href="([^"]+)"', opf)
    spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
    ids = set(i for i, _ in manifest)
    for s in spine:
        if s not in ids:
            issues.append("spine idref %s not in manifest" % s)
    for _, h in manifest:
        if base + h not in names:
            issues.append("manifest href missing file: %s" % h)
    for req in ("dc:title", "dc:creator", "dc:language", "dc:identifier"):
        if not re.search(r"<%s[\s>]" % req, opf):
            issues.append("missing metadata %s" % req)

    body = sum(
        len(re.sub(r"<[^>]+>", "", zf.read(n).decode("utf-8")))
        for n in names
        if n.endswith(".xhtml") and not n.endswith("nav.xhtml")
    )
    chapters = len([s for s in spine if s not in ("nav", "cover")])
    print(
        "entries=%d chapters=%d spine=%d body_chars=%d issues=%d"
        % (len(infos), chapters, len(spine), body, len(issues))
    )
    for i in issues:
        print("[错误] " + i)
    return 1 if issues else 0


def cmd_check(args):
    return check_file(args.epub)


# --------------------------------------------------------------------- CLI


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ocr-epub", description="扫描版中文书 OCR 转 EPUB")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ocr", help="逐页渲染 + OCR，输出带坐标的 JSONL（可续跑）")
    p.add_argument("pdf")
    p.add_argument("-o", "--jsonl", required=True, help="输出 JSONL 路径")
    p.add_argument("--pages-dir", help="中间图片目录，默认 <jsonl去后缀>_pages")
    p.add_argument("--dpi", type=int, default=220, help="渲染分辨率，默认 220（中文小字够用，约 4-10 秒/页）")
    p.add_argument("--first", type=int, default=1, help="起始页（1 起）")
    p.add_argument("--last", type=int, help="结束页，默认整本")
    p.add_argument("--cls", action="store_true", help="开启方向分类器；默认关闭，它会把平直裁切误判成倒转、导致整行识别为空")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("epub", help="JSONL 版面重排 + 清字 + 打包 EPUB3")
    p.add_argument("jsonl")
    p.add_argument("-o", "--out", required=True, help="输出 .epub 路径")
    p.add_argument("--title", help="书名，默认取 jsonl 文件名")
    p.add_argument("--author", help="作者，默认同书名")
    p.add_argument("--translator", help="译者")
    p.add_argument("--date", help="dc:date，如 2007-05")
    p.add_argument("--subject", help="dc:subject")
    p.add_argument("--lang", default="zh-CN")
    p.add_argument("--identifier", help="dc:identifier，默认按书名生成 urn:uuid")
    p.add_argument("--modified", help="dcterms:modified，默认当前 UTC 时间")
    p.add_argument("--cover", help="封面 jpg；不给则用 --cover-from-pdf 抽取")
    p.add_argument("--cover-from-pdf", help="从该 PDF 首页渲染封面")
    p.add_argument("--fixes", help="本书专有的字面纠错表，每行 `错=>对`")
    p.add_argument("--report", help="章节统计输出，默认 <out去后缀>.report.txt")
    p.add_argument("--preview", help="纯文本全稿，默认 <out去后缀>.preview.txt")
    p.add_argument("--dry-run", action="store_true", help="只出 report/preview，不打 epub")
    p.set_defaults(func=cmd_epub)

    p = sub.add_parser("check", help="校验 EPUB 结构（mimetype/OPF/spine/XML）")
    p.add_argument("epub")
    p.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
