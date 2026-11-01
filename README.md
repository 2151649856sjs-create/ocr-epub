# ocr-epub

把**扫描版中文书**转成可重排、可搜索的 **EPUB 3**。

不是把每页截图塞进电子书，也不是把 OCR 出来的纯文本直接拼接：工具会读取每个文字框的
坐标，按版面还原段落、标题和章节，再清掉页眉、页码和扫描水印，最后打包成流式 EPUB。

A layout-aware OCR pipeline for horizontally typeset Chinese scanned books:
RapidOCR for text plus box coordinates, geometric reflow for paragraphs and
headings, hand-built EPUB 3 output. CLI only, CPU only, resumable.

## 它解决什么问题

普通 OCR 输出的是一行行文字，丢掉了「哪一行结束一段、哪一行是标题」的信息。直接喂给
电子书工具，结果往往是整章一大段，或者每行都成一段。这个工具用坐标补回版面：

- **行重建**：同一基线上的碎片框按 x 坐标缝合，避免后半句跑到前半句前面。
- **段落重排**：行末明显短于本栏右边界 → 段落结束；下一行有缩进 → 新段落开始。
  页末没写满不算结束，跨页断句要看上一行有没有句末标点。
- **页眉页脚**：页眉按「在多页顶部重复出现」判定，页码只丢弃落在页边距里的纯数字行；
  正文里的年份（参考文献的 `2012.`）会保留。
- **结构识别**：`第X章` / `第X节`、目录页、章前大字隔页、竖排书名、内容提要、版权页。
  章名在个别页缺字时，按全书出现次数最多的写法统一。
- **文献列表**：条目以 `[n]` 开头时强制另起一段，避免等宽续行把相邻条目粘在一起。
- **字符修复**：破折号 `——` 是扫描里最烂的字形（会变成 `~`、`-`、`一`），工具结合
  量词与上下文判定；同时清理省略号、中文行尾半角 `?`/`"`、数字区间等。
- **人工兜底**：剩下的真歧义用每本书自己的纠错表逐条修，支持正则锚定。

## 安装

需要 Python 3.9–3.12（在 3.11 上测试）。

```bash
git clone https://github.com/2151649856sjs-create/ocr-epub.git
cd ocr-epub
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

> `onnxruntime 1.17` 与 `numpy 2.x` 不兼容，识别时会直接段错误。`requirements.txt`
> 已固定 `numpy<2`，不要手动升级。

首次运行会用 RapidOCR 自带的 ONNX 模型，之后可离线使用。

## 用法

### 1. OCR：PDF → 带坐标的 JSONL

```bash
python ocr-epub.py ocr 书.pdf -o 书.jsonl --dpi 220
```

- 输出每页一条 JSON：页号、图像尺寸、耗时，以及每个文字框的 `text/score/x0/y0/x1/y1`。
- 已完成的页会跳过，中断后重跑同一条命令即可续跑。
- `--dpi 220` 对中文小字够用；CPU 上约 4–10 秒/页，300 页的书约半小时。
- 中间页面图默认存到 `书_pages/`，可用 `--pages-dir` 改。
- `--first` / `--last` 可只跑一段，适合先试几页。
- 方向分类器默认关闭（`--cls` 才开）：它会把平直裁切的整行误判成倒转，导致识别为空。

### 2. 重排：JSONL → EPUB

```bash
python ocr-epub.py epub 书.jsonl -o 书.epub \
    --title "书名" --author "作者" \
    --cover 封面.jpg
```

先看稿子再打包：

```bash
python ocr-epub.py epub 书.jsonl -o 书.epub --dry-run
```

`--dry-run` 只写两个文件，方便通读校对：

- `书.report.txt`：页数、章数、段数、字数，以及每章统计。
- `书.preview.txt`：按章节排好的纯文本全稿。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--title` / `--author` / `--translator` | 元数据；书名默认取 JSONL 文件名 |
| `--date` / `--subject` / `--lang` | `dc:date` / `dc:subject` / 语言，默认 `zh-CN` |
| `--cover` | 封面 JPG |
| `--cover-from-pdf` | 不给封面时，从该 PDF 首页渲染一张 |
| `--identifier` | 默认按书名生成稳定 UUID |
| `--fixes` | 本书专有纠错表，见下 |
| `--report` / `--preview` | 自定义两个稿子的输出路径 |
| `--dry-run` | 不打包，只出稿子 |

### 3. 校验

```bash
python ocr-epub.py check 书.epub
```

检查 `mimetype` 顺序与压缩方式、`container.xml`、OPF、manifest/spine 引用、必需元数据、
XHTML/XML 可解析性，并输出章数与正文字数。它不能替代 [EPUBCheck](https://github.com/w3c/epubcheck)，
但能抓住手工打包最常犯的结构错误。

## 每本书的纠错表

扫描造成的歧义不可能靠通用规则全修，剩下的用 `--fixes` 定点处理。文件是 UTF-8 纯文本，
每行 `错=>对`，`#` 开头为注释：

```text
# 字形混淆
毕竞=>毕竟

# 标题行尾的引号被扫描漏掉；目录里同名标题是完整的，用 ^$ 锚定，避免误伤正文
re:^第3章 “复盘$=>第3章 “复盘”

# 替换里可以用捕获组
re:^(\s*)\[(\d{1,3})[」］]=>\1[\2]
```

左侧以 `re:` 开头时按正则处理；右侧支持反向引用。只作用于该书的段落与标题。

示例见 [`examples/fixes.example.txt`](examples/fixes.example.txt)。

## 输出结构

```text
OEBPS/
  content.opf
  nav.xhtml
  toc.ncx
  style.css
  cover.xhtml
  images/cover.jpg
  chapter001.xhtml … chapterNNN.xhtml
```

- 一级标题切成独立的 XHTML，二级/小节标题留在章内。
- 内置 EPUB 3 导航与 NCX 兼容目录，带封面页。
- 不使用第三方 EPUB 库，打包逻辑只用标准库 `zipfile`。

## 已知限制

- 目标版式是**横排、单栏**的中文扫描书。双栏、竖排正文、大量表格/公式的书会需要较多
  人工校对；公式不会被识别成数学排版，只按普通文字处理。
- OCR 质量取决于扫描分辨率与字形。建议先用 `--dry-run` 通读 `preview.txt`，把该书的
  系统性错字补进 `--fixes`。
- 版面规则基于常见书籍版式；异形版式（杂志、画册）可能需要改代码。
- 只在 CPU 上测试；未适配 GPU 推理。
- 仅支持 PDF 输入。

## 环境与许可

Python 3.11.9 / numpy 1.26.4 / onnxruntime 1.17.3 / rapidocr-onnxruntime 1.3.24 /
PyMuPDF 1.24.10 / opencv-python 4.10.0.84 下完成实书验证。

[MIT](LICENSE)
