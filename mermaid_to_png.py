#!/usr/bin/env python3
"""
mermaid_to_png.py - convert the mermaid diagram(s) of a markdown file to PNG.

The script measures every label of the diagram (participants, messages, notes,
node labels) and generates a mermaid configuration whose box widths, margins
and padding are large enough for the text, so nothing overflows or is clipped.

It is shared tooling: any of the four Vue workspaces can hand it a diagram.
Paths are relative to the repository root, and the PNG is written beside the
source unless a destination is given:

    python tools/mermaid_to_png.py <file>.md \\
        --size-cm "20x?"

A markdown file holding several mermaid blocks yields one numbered PNG per
block (_1, _2, ...). Without --size/--size-cm the diagram is drawn at its
natural size; the committed figures are all 20 cm wide, which at the default
300 dpi is the 2362 px every *_fig.png shares.

Requirements
------------
    - Node.js and mermaid-cli:   npm install -g @mermaid-js/mermaid-cli
    - Pillow (pip install pillow): required for --size (checked up front);
      otherwise optional, used for exact text measurement (without it a
      conservative character-width estimate is used)

- Author: Gino Bogo
- Date: 2026-09
- Version: 1.0.0
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
GANTT_FONT_SIZE = 11  # mermaid gantt default sectionFontSize (px)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


# --------------------------------------------------------------------------- #
# text measurement
# --------------------------------------------------------------------------- #
class TextMeter:
    """Measures text widths for the font size used by a diagram.

    Uses Pillow's ImageFont when a TTF is available on the system; falls
    back to a conservative character-width estimate when Pillow or a font
    is missing, so the script still runs (just with less-tight sizing).
    """

    def __init__(self, size=FONT_SIZE):
        self.size = size
        self.font = None
        try:
            from PIL import ImageFont

            for path in FONT_CANDIDATES:
                if os.path.exists(path):
                    self.font = ImageFont.truetype(path, size)
                    break
        except ImportError:
            pass

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

    A `.mmd`/`.mermaid` file is treated as a single diagram verbatim. Exits
    with an error if no diagram can be found.
    """
    text = Path(path).read_text(encoding="utf-8")
    blocks = re.findall(r"```\s*mermaid[^\n]*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if blocks:
        return [b.strip("\n") for b in blocks]
    if path.lower().endswith((".mmd", ".mermaid")):
        return [text]
    sys.exit(f"error: no ```mermaid block found in {path}")


def diagram_type(code):
    """First keyword of the diagram, ignoring YAML front matter, %% comments
    and %%{init}%% directives."""
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
    """Re-flow `text` so no line exceeds `max_w` px.

    Existing <br/> markers are dropped and re-inserted only where a line would
    otherwise overflow, which avoids orphan words. If the text already fits on
    one line it is returned unchanged so the author's line breaks are kept.
    """
    flat = re.sub(r"\s*<br\s*/?>\s*", " ", text).strip()
    if meter.width(flat) <= max_w:
        return text  # fits on one line: keep author's text
    lines, cur = [], ""
    for word in flat.split(" "):
        trial = (cur + " " + word) if cur else word
        if cur and meter.width(trial) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    lines.append(cur)
    return "<br/>".join(lines)


def wrap_sequence(code, meter, max_w):
    """Wrap the text of every message and multi-actor note in a sequenceDiagram.

    Only the trailing text (after the arrow and the `:`) is re-flowed; the
    participant names and arrow syntax are left untouched.
    """
    msg = re.compile(r"^(\s*\S+?\s*(?:-->>|->>|--x|-x|-->|->|--\)|-\))\s*[+-]?\S+?\s*:\s*)(.+)$")
    note = re.compile(r"^(\s*Note\s+(?:over|left of|right of)\s+[^:]+?\s*:\s*)(.+)$", re.IGNORECASE)
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
    note = re.compile(r"^(\s*Note\s+(?:left of|right of)\s+\S+?\s*:\s*)(.+)$", re.IGNORECASE)
    lines = []
    for line in code.splitlines():
        m = trans.match(line) or note.match(line)
        if m:
            lines.append(m.group(1) + wrap_text(m.group(2), meter, max_w))
        else:
            lines.append(line)
    return "\n".join(lines)


def wrap_class_diagram(code, meter, max_w):
    """Wrap long attribute/method lines inside classDiagram blocks."""
    lines = []
    in_class = False
    brace_depth = 0
    for line in code.splitlines():
        # detect class start
        if re.match(r"^\s*class\s+\S+", line):
            in_class = True
            brace_depth = line.count("{") - line.count("}")
            lines.append(line)
            continue
        if in_class:
            brace_depth += line.count("{") - line.count("}")
            # wrap lines that look like attributes/methods (contain : or ())
            stripped = line.strip()
            if stripped and not stripped.startswith("}") and (":" in stripped or "(" in stripped):
                # wrap the whole content after indentation
                indent = re.match(r"^(\s*)", line).group(1)
                wrapped = indent + wrap_text(stripped, meter, max_w).replace("\n", "<br/>")
                # if wrap_text added breaks, handle
                lines.append(wrapped)
                if brace_depth <= 0 and "}" in line:
                    in_class = False
                continue
            if "}" in line and brace_depth <= 0:
                in_class = False
            lines.append(line)
            continue
        lines.append(line)
    return "\n".join(lines)


def wrap_er_diagram(code, meter, max_w):
    """erDiagram attribute syntax does not allow <br/> inside lines, so
    wrapping is intentionally a no-op; sizing relies on er_config() only.
    """
    return code


def wrap_mindmap(code, meter, max_w):
    """Wrap mindmap node text while preserving indentation."""
    lines = []
    for line in code.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("%%"):
            lines.append(line)
            continue
        indent = re.match(r"^(\s*)", line).group(1)
        wrapped = wrap_text(stripped, meter, max_w)
        lines.append(indent + wrapped)
    return "\n".join(lines)


def wrap_journey(code, meter, max_w):
    """Wrap journey task titles."""
    task = re.compile(r"^(\s*.+?:\s*)(.+)$")
    lines = []
    for line in code.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("%%")
            or stripped.lower().startswith("title")
            or ":" not in stripped
        ):
            lines.append(line)
            continue
        m = task.match(line)
        if m and len(m.group(2)) > 10:  # only wrap longer texts
            lines.append(m.group(1) + wrap_text(m.group(2), meter, max_w))
        else:
            lines.append(line)
    return "\n".join(lines)


def wrap_timeline(code, meter, max_w):
    """Wrap timeline event text."""
    lines = []
    for line in code.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("%%")
            or stripped.lower().startswith(("timeline", "title", "section"))
        ):
            lines.append(line)
            continue
        # event lines often just text
        indent = re.match(r"^(\s*)", line).group(1)
        if indent and len(stripped) > 20:
            wrapped = wrap_text(stripped, meter, max_w)
            if wrapped != stripped:
                lines.append(indent + wrapped.replace("\n", "<br/>"))
                continue
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
        r"^\s*(\S+?)\s*(?:-->>|->>|--x|-x|-->|->|--\)|-\))\s*[+-]?(\S+?)\s*:\s*(.+)$",
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
    }


# edge labels:  A -- text --> B   |   A -.text.-> B   |   A ==text==> B   |   A -->|text| B
EDGE_RES = [
    re.compile(r"(--|-\.|==)[ \t]+([^->=|\n \t][^\n]*?)[ \t]+(-->|\.->|==>|---|-\.-|===)"),
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

    code = EDGE_RES[0].sub(lambda m: f"{m.group(1)} {fix(m.group(2))} {m.group(3)}", code)
    code = EDGE_RES[1].sub(
        lambda m: (
            m.group(0).replace(m.group(2), fix(m.group(2)))
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
NODE_SHAPE_RE = re.compile(r'(?:[\[\(\{]+|(?<![-.=])>)[\[\(\{]*"?([^\n\]\)\}"]+)"?[\]\)\}]+')


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

    Gantt renders section titles at its own sectionFontSize (default 11),
    not the global fontSize, so the section text is measured at the actual
    rendered size. Task ids are drawn on the bars themselves and must not
    widen the column.
    """
    g = TextMeter(GANTT_FONT_SIZE)
    widest = 0
    # Only section titles occupy the left column; task ids (the ":id, start,
    # duration" tail) are drawn on the bars themselves, so they must not
    # widen it.
    for m in re.finditer(r"^\s*section\s+(.+)$", code, re.MULTILINE):
        widest = max(widest, g.width(m.group(1)))
    return {
        "useMaxWidth": False,
        "leftPadding": max(0, round(widest + 24)),
        "rightPadding": 24,
        "barHeight": 24,
    }


def class_config(code, meter):
    """Config for classDiagram to improve text fitting."""
    widest = 0
    # measure class names and content inside blocks
    for m in re.finditer(r"class\s+(\S+)", code):
        widest = max(widest, meter.width(m.group(1)))
    for m in re.finditer(r"^\s*([^\{\n]+):\s*(.+)$", code, re.MULTILINE):
        widest = max(widest, meter.width(m.group(2)))
    for m in re.finditer(r"^\s*([^\(\n]+)\([^\)]*\)([^\n]*)$", code, re.MULTILINE):
        if m.group(2):
            widest = max(widest, meter.width(m.group(2)))
    return {
        "useMaxWidth": False,
        "padding": 16,
        "wrappingWidth": max(200, round(widest + 30)),
    }


def er_config(code, meter):
    """Config for erDiagram."""
    widest = 0
    for m in re.finditer(r"^\s*(\S+)\s*\{", code, re.MULTILINE):
        widest = max(widest, meter.width(m.group(1)))
    for m in re.finditer(r"^\s*([^\n\}]+)$", code, re.MULTILINE):
        s = m.group(1).strip()
        if s and not s.startswith(("}", "{")):
            widest = max(widest, meter.width(s))
    return {
        "useMaxWidth": False,
        "padding": 16,
        "wrappingWidth": max(200, round(widest + 30)),
    }


def state_config(code, meter):
    """Config for stateDiagram."""
    widest = 0
    for m in re.finditer(r"state\s+\"([^\"]+)\"", code):
        widest = max(widest, meter.width(m.group(1)))
    for m in re.finditer(r"^\s*\S+\s*-->\s*\S+\s*:\s*(.+)$", code, re.MULTILINE):
        widest = max(widest, meter.width(m.group(1)))
    return {
        "useMaxWidth": False,
        "padding": 14,
        "wrappingWidth": max(180, round(widest + 25)),
    }


def mindmap_config(code, meter):
    """Config for mindmap."""
    widest = 0
    for line in code.splitlines():
        s = line.strip()
        if not s or s.startswith(("mindmap", "%%")):
            continue
        widest = max(widest, meter.width(s))
    return {
        "useMaxWidth": False,
        "padding": 12,
        "wrappingWidth": max(160, round(widest + 20)),
    }


def journey_config(code, meter):
    """Config for journey."""
    widest = 0
    for m in re.finditer(r":\s*(.+)$", code, re.MULTILINE):
        widest = max(widest, meter.width(m.group(1)))
    return {
        "useMaxWidth": False,
        "padding": 14,
        "wrappingWidth": max(180, round(widest + 25)),
    }


def timeline_config(code, meter):
    """Config for timeline."""
    widest = 0
    for line in code.splitlines():
        s = line.strip()
        if not s or s.lower().startswith(("timeline", "title", "section", "%%")):
            continue
        widest = max(widest, meter.width(s))
    return {
        "useMaxWidth": False,
        "padding": 14,
        "wrappingWidth": max(180, round(widest + 25)),
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
    return struct.unpack(">II", head[16:24])


def render(code, out_png, cfg, size, tmp, compress=6, optimize=False):
    """Render `code` to `out_png` using mermaid-cli with the given config.

    Without `size`, the diagram is drawn at scale 1 (natural size). With
    `size`, a 2x probe render measures the natural dimensions, the diagram is
    re-rendered at the smallest integer scale >= the needed one, and the
    result is resampled with Pillow to the exact target size. The Pillow
    save uses `compress` (zlib level) and `optimize` (filter/strategy
    analysis); both only apply to the resample step.
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
        return

    # natural size (probed at scale 2), then render at the smallest integer scale that
    # is >= the needed one (mermaid-cli only takes integers) and resample
    probe = Path(tmp) / "probe.png"
    run(2, probe)  # scale 1 renders ~2% taller than 2x/2, so probe at 2
    nw, nh = (v / 2 for v in png_size(probe))
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
        img = Image.open(out_png)
        new = (
            max(1, round(img.width * s / render_scale)),
            max(1, round(img.height * s / render_scale)),
        )
        img.resize(new, Image.LANCZOS).save(out_png, compress_level=compress, optimize=optimize)


def parse_size(text):
    """Parse a size string into a (width, height) pair in pixels.

    Accepted forms: `WIDTHxHEIGHT`, `WIDTHx?` (width only), `?xHEIGHT`
    (height only), or a bare `WIDTH`. Either dimension may be omitted, in
    which case it is derived from the natural image size at render time.
    Raises argparse.ArgumentTypeError on an unrecognized form.
    """
    m = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*[xX*,]\s*(\d+(?:\.\d+)?)"
        r"|(\d+(?:\.\d+)?)\s*[xX]\s*\?"
        r"|\?\s*[xX]\s*(\d+(?:\.\d+)?)"
        r"|(\d+(?:\.\d+)?)",
        text.strip(),
    )
    if not m:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT, WIDTHx?, ?xHEIGHT, or WIDTH")
    if m.group(4) is not None:  # ?xHEIGHT -> height only
        return (None, float(m.group(4)))
    if m.group(3) is not None:  # WIDTHx? -> width only
        return (float(m.group(3)), None)
    if m.group(2) is not None:  # WIDTHxHEIGHT -> both
        return (float(m.group(1)), float(m.group(2)))
    return (float(m.group(5)), None)  # bare WIDTH -> width only


def main():
    ap = argparse.ArgumentParser(description="Convert mermaid diagrams in a markdown file to PNG.")
    ap.add_argument("source", help="markdown file with a ```mermaid block")
    ap.add_argument("dest", nargs="?", help="output PNG (default: next to source)")
    ap.add_argument(
        "--size",
        type=parse_size,
        metavar="WxH",
        help="target image size in pixels, e.g. 2400x1800, 2400x? (width only) "
        "or ?x1800 (height only)",
    )
    ap.add_argument(
        "--size-cm",
        type=parse_size,
        metavar="WxH",
        help="target image size in centimeters, e.g. 20x15, 20x? (width only) "
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
        help="long messages/notes are wrapped above this width in px "
        "(default 280, 0 = no wrapping)",
    )
    ap.add_argument(
        "--compress",
        type=int,
        default=6,
        metavar="N",
        help="PNG zlib compression level 0-9 for the Pillow resample step "
        "(default 6, 9 = smallest/slowest)",
    )
    ap.add_argument(
        "--optimize",
        action="store_true",
        help="run Pillow's extra filter/strategy analysis on the resample step "
        "(smaller PNG, slower)",
    )
    args = ap.parse_args()

    if args.size and args.size_cm:
        sys.exit("error: --size and --size-cm are mutually exclusive")

    if not 0 <= args.compress <= 9:
        sys.exit(f"error: --compress must be 0-9, got {args.compress}")

    # cm -> px: pixels = cm * dpi / 2.54
    if args.size_cm:
        factor = args.dpi / 2.54
        cm_w, cm_h = args.size_cm
        args.size = (
            round(cm_w * factor) if cm_w else None,
            round(cm_h * factor) if cm_h else None,
        )

    if args.size:
        try:
            import PIL  # noqa: F401
        except ImportError:
            sys.exit("error: --size/--size-cm needs Pillow (pip install pillow)")

    src = Path(args.source)
    if not src.is_file():
        sys.exit(f"error: {src} not found")
    dest = Path(args.dest) if args.dest else src.with_suffix(".png")
    dest.parent.mkdir(parents=True, exist_ok=True)

    diagrams = extract_diagrams(str(src))
    meter = TextMeter(args.font_size)
    if not meter.font:
        print(
            "note: Pillow/font not found, using estimated text widths "
            "(pip install pillow for exact fit)",
            file=sys.stderr,
        )

    with tempfile.TemporaryDirectory() as tmp:
        for i, code in enumerate(diagrams, 1):
            kind = diagram_type(code)
            if args.max_text_width and kind == "sequenceDiagram":
                code = wrap_sequence(code, meter, args.max_text_width)
            elif args.max_text_width and kind in ("flowchart", "graph"):
                code = wrap_flowchart(code, meter, args.max_text_width)
            elif args.max_text_width and kind.startswith("stateDiagram"):
                code = wrap_state_diagram(code, meter, args.max_text_width)
            elif args.max_text_width and kind.startswith("classDiagram"):
                code = wrap_class_diagram(code, meter, args.max_text_width)
            elif args.max_text_width and kind.startswith("erDiagram"):
                code = wrap_er_diagram(code, meter, args.max_text_width)
            elif args.max_text_width and kind.startswith("mindmap"):
                code = wrap_mindmap(code, meter, args.max_text_width)
            elif args.max_text_width and kind.startswith("journey"):
                code = wrap_journey(code, meter, args.max_text_width)
            elif args.max_text_width and kind.startswith("timeline"):
                code = wrap_timeline(code, meter, args.max_text_width)
            out = dest if len(diagrams) == 1 else dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
            render(
                code,
                str(out),
                build_config(code, meter),
                args.size,
                tmp,
                args.compress,
                args.optimize,
            )
            w, h = png_size(out)
            print(f"wrote {out} ({w}x{h}px)")


if __name__ == "__main__":
    main()
