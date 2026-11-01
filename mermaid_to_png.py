#!/usr/bin/env python3
"""
mermaid_to_png.py - convert the mermaid diagram(s) of a markdown file to PNG.

The script measures every label of the diagram (participants, messages, notes,
node labels) and generates a mermaid configuration whose box widths, margins
and padding are large enough for the text, so nothing overflows or is clipped.

Paths are resolved relative to the current working directory, and the PNG is
written beside the source unless a destination is given:

    python mermaid_to_png.py <file>.md --size-cm "20x?"

A markdown file holding several mermaid blocks yields one numbered PNG per
block (_1, _2, ...). Without --size/--size-cm the diagram is drawn at its
natural size.

Requirements
------------
    - Node.js and mermaid-cli:   npm install -g @mermaid-js/mermaid-cli
    - Pillow (pip install pillow): required for --size/--size-cm and for
      --compress/--optimize; otherwise optional, used for exact text
      measurement (without it a conservative character-width estimate is used)

Set MERMAID_TO_PNG_FONT to a TTF/OTF path to override the font used for
measurement.

- Author: Gino Bogo
- Date: 2026-11
- Version: 1.1.0
"""

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

FONT_SIZE = 16  # mermaid default font size (px)
SAFETY = 1.12  # safety factor on measured text widths
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

FONT_SEARCH_ROOTS = [
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
]

FONT_FILENAMES = {Path(path).name for path in FONT_CANDIDATES}


def _resolve_font_path(font_path=None):
    candidates = []
    if font_path:
        candidates.append(font_path)
    env_font = os.environ.get("MERMAID_TO_PNG_FONT")
    if env_font:
        candidates.append(env_font)
    candidates.extend(FONT_CANDIDATES)

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    for root in FONT_SEARCH_ROOTS:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        try:
            for name in FONT_FILENAMES:
                for match in root_path.rglob(name):
                    if match.is_file():
                        return str(match)
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# text measurement
# --------------------------------------------------------------------------- #
class TextMeter:
    """Measures text widths for the font size used by a diagram.

    Uses Pillow's ImageFont when a TTF is available on the system; falls
    back to a conservative character-width estimate when Pillow or a font
    is missing, so the script still runs (just with less-tight sizing).
    """

    def __init__(self, size=FONT_SIZE, font_path=None):
        self.size = size
        self.font = None
        self.font_path = None
        try:
            from PIL import ImageFont
        except ImportError:
            return

        resolved = _resolve_font_path(font_path)
        self.font_path = resolved
        if not resolved:
            return
        try:
            self.font = ImageFont.truetype(resolved, size)
        except (OSError, ValueError):
            self.font = None
            return

    def at_size(self, size):
        """Return a meter for `size` using the same resolved font file."""
        return TextMeter(size, self.font_path)

    def width(self, text):
        """Width in px of the widest line of `text` (lines split on <br/>)."""
        widest = 0
        for line in re.split(r"<br\s*/?>|\\n", text):
            line = re.sub(r"<[^>]+>", "", line)  # strip html tags
            if self.font:
                w = self.font.getlength(line)
            else:
                w = len(line) * self.size * 0.62  # conservative
            widest = max(widest, w)
        return widest * SAFETY

    def lines(self, text):
        """Number of lines `text` will render (split on <br/> or \\n)."""
        return len(re.split(r"<br\s*/?>|\\n", text))


# --------------------------------------------------------------------------- #
# markdown extraction
# --------------------------------------------------------------------------- #
def extract_diagrams(path):
    """Return every ```mermaid block in `path` as a list of source strings.

    A non-mermaid fenced block is skipped as a whole, so mermaid examples
    nested inside another fence are not mistaken for diagrams. A
    `.mmd`/`.mermaid` file is treated as a single diagram verbatim. Exits with
    an error if no diagram can be found.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        sys.exit(f"error: {path} is not valid UTF-8")
    lines = text.splitlines()
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        opening = re.match(r"^([ \t]{0,3})(`{3,}|~{3,})(.*)$", lines[i])
        if not opening:
            i += 1
            continue

        marker = opening.group(2)
        close = re.compile(
            r"^([ \t]{0,3})"
            + re.escape(marker[0])
            + "{"
            + str(len(marker))
            + r",}[ \t]*$"
        )
        j = i + 1
        while j < n and not close.match(lines[j]):
            j += 1

        info = opening.group(3).strip().split()
        if info and info[0].lower() == "mermaid":
            if j >= n:
                sys.exit(f"error: unterminated mermaid fence in {path}")
            blocks.append("\n".join(lines[i + 1 : j]).strip("\n"))
        i = j + 1
    if blocks:
        return blocks
    if path.lower().endswith((".mmd", ".mermaid")):
        return [text]
    sys.exit(f"error: no ```mermaid block found in {path}")


def diagram_type(code):
    """First keyword of the diagram, ignoring YAML front matter, %% comments
    and %%{init}%% directives."""
    code = code.lstrip("\ufeff")
    body = re.sub(r"\A\s*---\s*\n.*?\n---\s*\n", "", code, flags=re.DOTALL)
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("%%"):
            return line.split()[0].rstrip(";")
    return ""


# --------------------------------------------------------------------------- #
# automatic line wrapping of long message / note texts
# --------------------------------------------------------------------------- #
def wrap_text(text, meter, max_w):
    """Re-flow `text` so no line exceeds `max_w` px where possible.

    Existing <br/> markers are dropped and re-inserted only where a line would
    otherwise overflow, which avoids orphan words. If the text already fits on
    one line it is returned unchanged so the author's line breaks are kept.
    A single word wider than `max_w` is kept intact.
    """
    flat = re.sub(r"\s*<br\s*/?>\s*", " ", text).strip()
    if meter.width(flat) <= max_w:
        return text  # fits on one line: keep author's text
    lines, cur = [], ""
    for word in flat.split():
        trial = (cur + " " + word) if cur else word
        if cur and meter.width(trial) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    lines.append(cur)
    return "<br/>".join(lines)


# arrow tokens shared by sequence wrapping and sizing; longest first
SEQUENCE_ARROWS = r"<<-->>|<<->>|-->>|->>|--x|-x|-->|->|--\)|-\)"


def wrap_sequence(code, meter, max_w):
    """Wrap the text of every message and note in a sequenceDiagram.

    Only the trailing text (after the arrow and the `:`) is re-flowed; the
    participant names and arrow syntax are left untouched.
    """
    msg = re.compile(
        r"^(\s*\S+?\s*(?:" + SEQUENCE_ARROWS + r")\s*[+-]?\S+?\s*:\s*)(.+)$"
    )
    note = re.compile(
        r"^(\s*Note\s+(?:over|left of|right of)\s+[^:]+?\s*:\s*)(.+)$",
        re.IGNORECASE,
    )
    lines = []
    for line in code.splitlines():
        m = msg.match(line) or note.match(line)
        lines.append(m.group(1) + wrap_text(m.group(2), meter, max_w) if m else line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# extended wrapping for additional diagram types
# --------------------------------------------------------------------------- #
def wrap_state_diagram(code, meter, max_w):
    """Wrap transition labels and notes in stateDiagram/stateDiagram-v2."""
    # transition: A --> B : label
    trans = re.compile(r"^(\s*\S+?\s*(?:-->|->)\s*\S+?\s*:\s*)(.+)$")
    note = re.compile(
        r"^(\s*Note\s+(?:left of|right of)\s+\S+?\s*:\s*)(.+)$", re.IGNORECASE
    )
    lines = []
    for line in code.splitlines():
        m = trans.match(line) or note.match(line)
        if m:
            lines.append(m.group(1) + wrap_text(m.group(2), meter, max_w))
        else:
            lines.append(line)
    return "\n".join(lines)


def wrap_mindmap(code, meter, max_w):
    """Wrap mindmap node text while preserving indentation."""
    lines = []
    for line in code.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith(("%%", "::"))
            or stripped.lower() == "mindmap"
        ):
            lines.append(line)
            continue
        indent = re.match(r"^(\s*)", line).group(1)
        wrapped = wrap_text(stripped, meter, max_w)
        lines.append(indent + wrapped)
    return "\n".join(lines)


def wrap_journey(code, meter, max_w):
    """Wrap journey task titles."""
    task = re.compile(r"^(\s*)(.+?)(\s*:\s*\d.*)$")
    lines = []
    for line in code.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("%%") or ":" not in stripped:
            lines.append(line)
            continue
        m = task.match(line)
        if m:
            indent, title, tail = m.groups()
            lines.append(indent + wrap_text(title, meter, max_w) + tail)
        else:
            lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# configuration building
# --------------------------------------------------------------------------- #
def sequence_config(code, meter):
    """Compute sequenceDiagram sizes so that every text fits.

    Returns the mermaid `sequence` config dict: participant box widths are
    sized from the widest label, and the spacing between neighbouring
    participants is sized from the widest message/note that spans them.
    """
    order, labels = [], {}
    for m in re.finditer(
        r"^\s*(?:participant|actor)\s+(\S+)(?:\s+as\s+(.+))?$", code, re.MULTILINE
    ):
        pid, label = m.group(1), (m.group(2) or m.group(1)).strip()
        order.append(pid)
        labels[pid] = label

    pad = 24
    box_w = max([meter.width(l) for l in labels.values()] + [100]) + 2 * pad
    box_w = max(150, round(box_w))
    idx = {p: i for i, p in enumerate(order)}

    # (first, last, text width) for every message and multi-actor note
    spans = []
    msg_re = re.compile(
        r"^\s*(\S+?)\s*(?:" + SEQUENCE_ARROWS + r")\s*[+-]?(\S+?)\s*:\s*(.+)$",
        re.MULTILINE,
    )
    for m in msg_re.finditer(code):
        a, b, txt = m.group(1), m.group(2), m.group(3)
        if a in idx and b in idx:
            lo, hi = sorted((idx[a], idx[b]))
            spans.append((lo, hi, meter.width(txt)))
    note_re = re.compile(
        r"^\s*Note\s+(?:over|left of|right of)\s+([^:]+?)\s*:\s*(.+)$",
        re.MULTILINE | re.IGNORECASE,
    )
    for m in note_re.finditer(code):
        names = [n.strip() for n in m.group(1).split(",")]
        txt_w = meter.width(m.group(2))
        known = [idx[n] for n in names if n in idx]
        if len(known) >= 2:  # note over A,B[,C...]
            spans.append((min(known), max(known), txt_w + 20))

    # distance (centre to centre) needed between neighbouring participants
    n = len(order)
    need = [0.0] * max(n - 1, 1)
    for lo, hi, w in spans:
        if hi > lo:
            req = (w + 40) / (hi - lo)  # 40 px: arrow heads + padding
            for g in range(lo, hi):
                need[g] = max(need[g], req)
    margin = max(max(need) - box_w, 40)

    line_h = meter.size * 1.25
    max_lines = max([meter.lines(l) for l in labels.values()] + [1])
    return {
        "useMaxWidth": False,
        "width": box_w,
        "height": round(max(65, max_lines * line_h + 30)),
        "actorMargin": round(margin),
        "noteMargin": 12,
        "messageMargin": 42,
        "boxMargin": 10,
        "wrap": False,
        "mirrorActors": True,
        "noteAlign": "center",
        "actorFontSize": meter.size,
        "messageFontSize": meter.size,
        "noteFontSize": meter.size,
    }


# edge labels:  A -- text --> B   |   A -.text.-> B   |   A ==text==> B   |   A -->|text| B
EDGE_RES = [
    re.compile(
        r"(--|-\.|==)[ \t]+([^->=|\n \t][^\n]*?)[ \t]+(-->|\.->|==>|---|-\.-|===)"
    ),
    re.compile(r"(-->|---|-\.->|-\.-|==>|===)[ \t]*\|[ \t]*\"?([^|\n]+?)\"?[ \t]*\|"),
]


def wrap_flowchart(code, meter, max_w):
    """Wrap over-long edge labels in a flowchart/graph.

    Node labels are wrapped by mermaid itself through `wrappingWidth`, which
    flowchart_config sizes from the node text, so only edge labels (the text
    between two nodes) are re-flowed here.
    """

    def fix(txt):
        return wrap_text(txt, meter, max_w)

    code = EDGE_RES[0].sub(
        lambda m: f"{m.group(1)} {fix(m.group(2))} {m.group(3)}", code
    )
    code = EDGE_RES[1].sub(
        lambda m: (
            f"{m.group(1)}|{fix(m.group(2))}|"
            if meter.width(m.group(2)) > max_w
            else m.group(0)
        ),
        code,
    )
    return code


# Node label shapes:  A[x]  A(x)  A{x}  A[[x]]  A[(x)]  A((x))  A{{x}}  A>x]
# Two traps this avoids:
#  - the body must not span a newline, or a match runs from one node's bracket
#    into the following line and wrappingWidth is over-estimated by an order
#    of magnitude (it then measures source code, not labels);
#  - ">" opens only the asymmetric shape A>x]; in an arrow (A --> B, A-.->B,
#    A==>B) it follows - . or = and must not open a label. Nested shapes
#    (A>[x]) keep consuming their inner bracket, which is not part of the text.
NODE_SHAPE_RE = re.compile(
    r'(?:[\[\(\{]+|(?<![-.=])>)[\[\(\{]*"?([^\n\]\)\}"]+)"?[\]\)\}]+'
)


def flowchart_config(code, meter):
    """Compute flowchart/graph sizes so that every label fits.

    Returns the mermaid `flowchart` config dict with `wrappingWidth` set to
    just wider than the widest node or edge label, so mermaid wraps long
    node text instead of overflowing it.
    """
    widest = 0
    for m in NODE_SHAPE_RE.finditer(code):
        widest = max(widest, meter.width(m.group(1)))
    for rx in EDGE_RES:  # edge labels count too
        for m in rx.finditer(code):
            widest = max(widest, meter.width(m.group(2)))
    return {
        "useMaxWidth": False,
        "htmlLabels": True,
        "padding": 18,
        "wrappingWidth": max(200, round(widest + 20)),
    }


def gantt_config(code, meter):
    """Size the left (section-label) column so nothing overflows.

    Section titles use gantt's `sectionFontSize`; it is set to the diagram
    font size so the measured and rendered sizes agree. Task ids are drawn on
    the bars themselves and must not widen the column.
    """
    widest = 0
    for m in re.finditer(r"^\s*section\s+(.+)$", code, re.MULTILINE):
        widest = max(widest, meter.width(m.group(1)))
    return {
        "useMaxWidth": False,
        "fontSize": meter.size,
        "sectionFontSize": meter.size,
        "leftPadding": max(0, round(widest + 24)),
        "rightPadding": 24,
        "barHeight": 24,
    }


def class_config(code, meter):
    """Config for classDiagram.

    Mermaid sizes class boxes from their member text and has no width/wrap
    option, so only padding is configurable here.
    """
    return {"useMaxWidth": False, "padding": 16}


def er_config(code, meter):
    """Config for erDiagram.

    Entity boxes do not grow to fit their text, so set a minimum width from
    the widest entity name or attribute line inside an entity block.
    Relationship lines are edge labels and must not widen the entities.
    """
    widest = 0
    in_entity = False
    for line in code.splitlines():
        stripped = line.strip()
        if not in_entity:
            m = re.match(r"^(\S+)\s*\{$", stripped)
            if m:
                widest = max(widest, meter.width(m.group(1)))
                in_entity = True
            continue
        if stripped == "}":
            in_entity = False
            continue
        if stripped and not stripped.startswith("%%"):
            widest = max(widest, meter.width(stripped))
    return {
        "useMaxWidth": False,
        "minEntityWidth": max(100, round(widest + 2 * 16)),
        "entityPadding": 16,
        "fontSize": meter.size,
    }


def state_config(code, meter):
    """Config for stateDiagram."""
    widest = 0
    for m in re.finditer(r"state\s+\"([^\"]+)\"", code):
        widest = max(widest, meter.width(m.group(1)))
    for m in re.finditer(r"^\s*\S+\s*(?:-->|->)\s*\S+\s*:\s*(.+)$", code, re.MULTILINE):
        widest = max(widest, meter.width(m.group(1)))
    for m in re.finditer(
        r"^\s*note\s+(?:left of|right of)\s+\S+\s*:\s*(.+)$",
        code,
        re.MULTILINE | re.IGNORECASE,
    ):
        widest = max(widest, meter.width(m.group(1)))
    return {
        "useMaxWidth": False,
        "padding": 14,
        "fontSize": meter.size,
        "wrappingWidth": max(180, round(widest + 25)),
    }


def mindmap_config(code, meter):
    """Config for mindmap.

    `maxNodeWidth` is the supported node-width setting; mindmap has no
    wrapping-width option.
    """
    widest = 0
    for line in code.splitlines():
        s = line.strip()
        if not s or s.startswith(("%%", "::")) or s.lower() == "mindmap":
            continue
        widest = max(widest, meter.width(s))
    return {
        "useMaxWidth": False,
        "padding": 12,
        "maxNodeWidth": max(200, round(widest + 20)),
    }


def journey_config(code, meter):
    """Config for journey.

    Journey task text is rendered as-is unless `textPlacement` is `tspan`,
    so the wrapper's <br/> breaks only work with that setting. `width` keeps
    the task boxes as wide as the wrapped title text.
    """
    widest = 0
    for m in re.finditer(r"^\s*(.+?)\s*:\s*\d+(?:\s*:.*)?$", code, re.MULTILINE):
        widest = max(widest, meter.width(m.group(1)))
    return {
        "useMaxWidth": False,
        "textPlacement": "tspan",
        "taskFontSize": meter.size,
        "width": max(150, round(widest + 20)),
    }


def timeline_config(code, meter):
    """Config for timeline.

    Timeline wraps event text itself at the configured `width`; its wrap
    implementation recognizes <br> but not <br/>, so no manual `<br/>` breaks
    are inserted.
    """
    widest = 0
    for line in code.splitlines():
        s = line.strip()
        if (
            not s
            or s.startswith("%%")
            or s.lower().startswith(("timeline", "title", "section"))
        ):
            continue
        widest = max(widest, meter.width(s))
    return {
        "useMaxWidth": False,
        "padding": 14,
        "taskFontSize": meter.size,
        "width": max(150, round(widest + 20)),
    }


def build_config(code, meter):
    """Build the mermaid config dict for the diagram type found in `code`.

    Common keys (theme, fontSize) are always set; the diagram-specific
    section is added by dispatching on the first keyword.
    """
    first = diagram_type(code)
    cfg = {
        "theme": "default",
        "fontSize": meter.size,
        "themeVariables": {"fontSize": f"{meter.size}px"},
    }
    if first.startswith("sequenceDiagram"):
        cfg["sequence"] = sequence_config(code, meter)
    elif first.startswith(("flowchart", "graph")):
        cfg["flowchart"] = flowchart_config(code, meter)
    elif first.startswith("gantt"):
        cfg["gantt"] = gantt_config(code, meter)
    elif first.startswith("classDiagram"):
        cfg["class"] = class_config(code, meter)
    elif first.startswith("erDiagram"):
        cfg["er"] = er_config(code, meter)
    elif first.startswith("stateDiagram"):
        cfg["state"] = state_config(code, meter)
    elif first.startswith("mindmap"):
        cfg["mindmap"] = mindmap_config(code, meter)
    elif first.startswith("journey"):
        cfg["journey"] = journey_config(code, meter)
    elif first.startswith("timeline"):
        cfg["timeline"] = timeline_config(code, meter)
    return cfg


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def find_mmdc():
    """Return the command list used to invoke mermaid-cli.

    Prefers a `mmdc` on PATH; falls back to `npx -y @mermaid-js/mermaid-cli`.
    Exits with install instructions if neither is available.
    """
    exe = shutil.which("mmdc")
    if exe:
        return [exe]
    if shutil.which("npx"):
        return ["npx", "-y", "@mermaid-js/mermaid-cli"]
    sys.exit(
        "error: mermaid-cli not found. Install it with:\n  npm install -g @mermaid-js/mermaid-cli"
    )


def png_size(path):
    """Return the (width, height) of a PNG file from its IHDR chunk."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise OSError(f"{path} is not a PNG file")
    return struct.unpack(">II", head[16:24])


def _reencode_png(path, compress, optimize):
    """Re-save an existing PNG with Pillow's compression options."""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("error: --compress/--optimize need Pillow (pip install pillow)")
    with Image.open(path) as img:
        img.load()
        copy = img.copy()
    copy.save(path, format="PNG", compress_level=compress, optimize=optimize)


def render(code, out_png, cfg, size, tmp, compress=6, optimize=False, reencode=False):
    """Render `code` to `out_png` using mermaid-cli with the given config.

    Without `size`, the diagram is drawn at scale 1 (natural size). With
    `size`, a 2x probe render measures the natural dimensions, the diagram is
    re-rendered at the smallest integer scale >= the needed one, and the
    result is resampled with Pillow to the requested bounding box. The Pillow
    save uses `compress` (zlib level) and `optimize` (filter/strategy
    analysis). If `reencode` is true, the output is saved with Pillow even
    when no resize is needed, so those options always take effect.
    """
    mmd = Path(tmp) / "diagram.mmd"
    mmd.write_text(code, encoding="utf-8")
    cfg_file = Path(tmp) / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

    puppeteer = {"args": ["--no-sandbox", "--disable-setuid-sandbox"]}
    chrome = os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    if chrome:
        puppeteer["executablePath"] = chrome
    pp_file = Path(tmp) / "puppeteer.json"
    pp_file.write_text(json.dumps(puppeteer), encoding="utf-8")

    def run(s, dest):
        cmd = find_mmdc() + [
            "-i",
            str(mmd),
            "-o",
            str(dest),
            "-c",
            str(cfg_file),
            "-p",
            str(pp_file),
            "-b",
            "white",
            "-s",
            str(s),
            "-q",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode != 0 or not Path(dest).exists():
            sys.exit(f"mermaid-cli failed:\n{r.stdout}\n{r.stderr}")

    if not size:
        run(1, out_png)
        if reencode:
            _reencode_png(out_png, compress, optimize)
        return

    # natural size (probed at scale 2), then render at the smallest integer scale that
    # is >= the needed one (mermaid-cli only takes integers) and resample
    probe = Path(tmp) / "probe.png"
    run(2, probe)  # scale 1 renders ~2% taller than 2x/2, so probe at 2
    nw, nh = (v / 2 for v in png_size(probe))
    if nw <= 0 or nh <= 0:
        sys.exit("error: mermaid-cli produced an invalid probe image")
    max_w, max_h = size
    if max_w is None:  # height only: xHEIGHT
        s = max_h / nh
    elif max_h is None:  # width only: WIDTH
        s = max_w / nw
    else:
        s = min(max_w / nw, max_h / nh)
    render_scale = max(2, math.ceil(s))
    run(render_scale, out_png)
    if abs(s - render_scale) > 1e-3:  # resample unless rendered exactly at s
        try:
            from PIL import Image
        except ImportError:
            sys.exit("error: --size needs Pillow (pip install pillow)")
        with Image.open(out_png) as img:
            img.load()
            new = (
                max(1, round(img.width * s / render_scale)),
                max(1, round(img.height * s / render_scale)),
            )
            resized = img.resize(new, Image.LANCZOS)
        resized.save(out_png, format="PNG", compress_level=compress, optimize=optimize)
    elif reencode:
        _reencode_png(out_png, compress, optimize)


def parse_size(text):
    """Parse a size string into a (width, height) pair in pixels.

    Accepted forms: `WIDTHxHEIGHT`, `WIDTHx?` (width only), `?xHEIGHT`
    (height only), or a bare `WIDTH`. Either dimension may be omitted, in
    which case it is derived from the natural image size at render time.
    Dimensions must be positive. Raises argparse.ArgumentTypeError on an
    unrecognized or non-positive form.
    """
    m = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)"
        r"|(\d+(?:\.\d+)?)\s*[xX]\s*\?"
        r"|\?\s*[xX]\s*(\d+(?:\.\d+)?)"
        r"|(\d+(?:\.\d+)?)",
        text.strip(),
    )
    if not m:
        raise argparse.ArgumentTypeError(
            "size must be WIDTHxHEIGHT, WIDTHx?, ?xHEIGHT, or WIDTH"
        )
    if m.group(4) is not None:  # ?xHEIGHT -> height only
        width, height = None, float(m.group(4))
    elif m.group(3) is not None:  # WIDTHx? -> width only
        width, height = float(m.group(3)), None
    elif m.group(2) is not None:  # WIDTHxHEIGHT -> both
        width, height = float(m.group(1)), float(m.group(2))
    else:  # bare WIDTH -> width only
        width, height = float(m.group(5)), None
    if (width is not None and width <= 0) or (height is not None and height <= 0):
        raise argparse.ArgumentTypeError("size values must be positive")
    return width, height


def main():
    ap = argparse.ArgumentParser(
        description="Convert mermaid diagrams in a markdown file to PNG."
    )
    ap.add_argument("source", help="markdown file with a ```mermaid block")
    ap.add_argument("dest", nargs="?", help="output PNG (default: next to source)")
    ap.add_argument(
        "--size",
        type=parse_size,
        metavar="WxH",
        help="maximum image size in pixels, e.g. 2400x1800, 2400x? (width only) "
        "or ?x1800 (height only); aspect ratio is preserved",
    )
    ap.add_argument(
        "--size-cm",
        type=parse_size,
        metavar="WxH",
        help="maximum image size in centimeters, e.g. 20x15, 20x? (width only) "
        "or ?x15 (height only) (converted to pixels via --dpi, default 300)",
    )
    ap.add_argument(
        "--dpi",
        type=float,
        default=300.0,
        metavar="N",
        help="DPI used to convert --size-cm to pixels (default 300)",
    )
    ap.add_argument(
        "--font-size",
        type=int,
        default=FONT_SIZE,
        metavar="N",
        help=f"diagram font size in px (default {FONT_SIZE})",
    )
    ap.add_argument(
        "--max-text-width",
        type=int,
        default=280,
        metavar="N",
        help="long labels are wrapped above this width in px for the diagram "
        "types that support it (default 280, 0 = no wrapping)",
    )
    ap.add_argument(
        "--compress",
        type=int,
        default=None,
        metavar="N",
        help="PNG zlib compression level 0-9 for Pillow re-encoding "
        "(default 6 when resampling; also applied without --size when set)",
    )
    ap.add_argument(
        "--optimize",
        action="store_true",
        help="run Pillow's extra filter/strategy analysis when re-encoding "
        "(smaller PNG, slower)",
    )
    ap.add_argument(
        "--font-path",
        metavar="PATH",
        help="TTF/OTF font used for text measurement (overrides MERMAID_TO_PNG_FONT and built-in search)",
    )
    args = ap.parse_args()

    if args.size and args.size_cm:
        sys.exit("error: --size and --size-cm are mutually exclusive")

    if not math.isfinite(args.dpi) or args.dpi <= 0:
        sys.exit("error: --dpi must be a positive number")
    if args.font_size <= 0:
        sys.exit("error: --font-size must be a positive integer")
    if args.max_text_width < 0:
        sys.exit("error: --max-text-width must be 0 or greater")
    if args.compress is not None and not 0 <= args.compress <= 9:
        sys.exit(f"error: --compress must be 0-9, got {args.compress}")

    # cm -> px: pixels = cm * dpi / 2.54
    if args.size_cm:
        factor = args.dpi / 2.54
        cm_w, cm_h = args.size_cm
        args.size = (
            round(cm_w * factor) if cm_w else None,
            round(cm_h * factor) if cm_h else None,
        )
        if any(dim is not None and dim <= 0 for dim in args.size):
            sys.exit("error: --size-cm converts to a non-positive pixel size")

    needs_pillow = bool(args.size) or args.compress is not None or args.optimize
    if needs_pillow:
        try:
            import PIL  # noqa: F401
        except ImportError:
            sys.exit(
                "error: --size/--size-cm/--compress/--optimize need Pillow "
                "(pip install pillow)"
            )

    src = Path(args.source)
    if not src.is_file():
        sys.exit(f"error: {src} not found")
    if args.font_path and not Path(args.font_path).is_file():
        sys.exit(f"error: font not found: {args.font_path}")

    dest = Path(args.dest) if args.dest else src.with_suffix(".png")
    if dest.is_dir():
        sys.exit(f"error: destination is a directory: {dest}")
    if not dest.suffix:
        dest = dest.with_suffix(".png")
    elif dest.suffix.lower() != ".png":
        sys.exit(f"error: destination must be a .png file: {dest}")
    dest = dest.with_suffix(".png")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(f"error: cannot create {dest.parent}: {exc}")
    if dest.exists():
        print(f"warning: overwriting existing file {dest}", file=sys.stderr)

    diagrams = extract_diagrams(str(src))
    meter = TextMeter(args.font_size, args.font_path)
    if args.font_path and not meter.font:
        sys.exit(f"error: could not load font {args.font_path} (is Pillow installed?)")
    if not meter.font:
        print(
            "note: Pillow/font not found, using estimated text widths "
            "(pip install pillow for exact fit)",
            file=sys.stderr,
        )
    compress = 6 if args.compress is None else args.compress
    reencode = args.compress is not None or args.optimize

    with tempfile.TemporaryDirectory() as tmp:
        for i, code in enumerate(diagrams, 1):
            kind = diagram_type(code)
            if args.max_text_width:
                if kind == "sequenceDiagram":
                    code = wrap_sequence(code, meter, args.max_text_width)
                elif kind in ("flowchart", "graph"):
                    code = wrap_flowchart(code, meter, args.max_text_width)
                elif kind.startswith("stateDiagram"):
                    code = wrap_state_diagram(code, meter, args.max_text_width)
                elif kind.startswith("mindmap"):
                    code = wrap_mindmap(code, meter, args.max_text_width)
                elif kind.startswith("journey"):
                    code = wrap_journey(code, meter, args.max_text_width)
            out = (
                dest
                if len(diagrams) == 1
                else dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
            )
            render(
                code,
                str(out),
                build_config(code, meter),
                args.size,
                tmp,
                compress,
                args.optimize,
                reencode,
            )
            w, h = png_size(out)
            print(f"wrote {out} ({w}x{h}px)")


if __name__ == "__main__":
    main()
