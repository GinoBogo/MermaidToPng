# MermaidToPng

Convert the Mermaid diagram(s) in a Markdown file to PNG, automatically measuring
every label (participants, messages, notes, node labels) and generating a
Mermaid config with box widths, margins and padding large enough to avoid
overflow or clipping.

## Requirements

- Node.js and mermaid-cli: `npm install -g @mermaid-js/mermaid-cli`
- Pillow (`pip install pillow`): required for `--size`/`--size-cm`; otherwise
  optional, used for exact text measurement (without it a conservative
  character-width estimate is used)

## Usage

```bash
python mermaid_to_png.py <file>.md [--size-cm "20x?"]
```

Paths are relative to the repository root. The PNG is written beside the source
unless a destination is given.

A Markdown file with several mermaid blocks yields one numbered PNG per block
(`_1`, `_2`, ...). Without `--size`/`--size-cm` the diagram is drawn at its
natural size; committed figures are 20 cm wide (2362 px at 300 dpi).

## Examples

Convert a single diagram at natural size:

```bash
python mermaid_to_png.py diagram.md
```

Convert to a 20 cm wide figure at 300 dpi:

```bash
python mermaid_to_png.py diagram.md --size-cm "20x?"
```

Convert with an explicit pixel size and tighter wrapping:

```bash
python mermaid_to_png.py diagram.md doc.png --size 2400x1800 --max-text-width 240
```

Set a custom output path and font size:

```bash
python mermaid_to_png.py diagram.md out/figure.png --font-size 18
```

Smallest PNG via maximum compression:

```bash
python mermaid_to_png.py diagram.md --size-cm "20x?" --compress 9 --optimize
```

## Options

| Flag | Description |
|---|---|
| `source` | Markdown file with a ` ```mermaid ` block |
| `dest` | Output PNG (default: next to source) |
| `--size` | Target image size in pixels, e.g. `2400x1800`, `2400x?`, `?x1800` |
| `--size-cm` | Target image size in centimeters, e.g. `20x15`, `20x?`, `?x15` (converted via `--dpi`, default 300) |
| `--dpi` | DPI used to convert `--size-cm` to pixels (default 300) |
| `--font-size` | Diagram font size in px (default 16) |
| `--max-text-width` | Long messages/notes wrapped above this width in px (default 280; 0 = no wrapping) |
| `--compress` | PNG zlib compression level 0-9 (default 6) |
| `--optimize` | Run Pillow's extra filter/strategy analysis (smaller PNG, slower) |
| `--font-path` | TTF/OTF font used for text measurement (overrides `MERMAID_TO_PNG_FONT` and built-in search) |

## License

MIT — see `LICENSE`.

- Author: Gino Bogo
- Version: 1.0.0

## Disclaimer

This tool is provided "as is", without warranty of any kind. Output quality
depends on third-party tools (Node.js, mermaid-cli, Pillow) and installed
fonts; verify generated images before use.
