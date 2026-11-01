"""
Test suite for mermaid_to_png.py.

Validates pure logic (measurement, extraction, wrapping, config builders,
size parsing, PNG header parsing) without requiring mermaid-cli or Node.

Integration rendering tests are gated on mmdc availability.

 - Author: Gino Bogo
 - Date: 2026-11
 - Version: 1.1.0
"""

# Standard library imports
import shutil
import sys
from pathlib import Path

# Third-party imports
import pytest

# Local application imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mermaid_to_png as m2p

# ------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------


@pytest.fixture
def meter():
    return m2p.TextMeter(16)


# ------------------------------------------------------------------------------
# TextMeter
# ------------------------------------------------------------------------------


class TestTextMeter:
    def test_width_empty(self, meter):
        assert meter.width("") == 0

    def test_width_single_line_positive(self, meter):
        assert meter.width("Hello") > 0

    def test_width_br_takes_max(self, meter):
        w_long = meter.width("BBBBBBBB")
        w_combined = meter.width("AAAA<br/>BBBBBBBB")
        assert w_combined == w_long

    def test_width_strips_html(self, meter):
        assert meter.width("<b>x</b>") == meter.width("x")

    def test_lines_counts_breaks(self, meter):
        assert meter.lines("a<br/>b\\nc") == 3
        assert meter.lines("single") == 1

    def test_fallback_estimate_without_font(self):
        m = m2p.TextMeter(10)
        m.font = None
        assert m.width("abc") == len("abc") * 10 * 0.62 * m2p.SAFETY

    def test_safety_factor_applied(self, meter):
        if meter.font:
            raw = meter.font.getlength("X")
            assert meter.width("X") == raw * m2p.SAFETY
        else:
            assert meter.width("X") > 0


# ------------------------------------------------------------------------------
# extract_diagrams
# ------------------------------------------------------------------------------


class TestExtractDiagrams:
    def test_single_block(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("text\n```mermaid\nflowchart TD\nA-->B\n```\n")
        assert m2p.extract_diagrams(str(p)) == ["flowchart TD\nA-->B"]

    def test_multiple_blocks(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("```mermaid\nA\n```\n```mermaid\nB\n```")
        res = m2p.extract_diagrams(str(p))
        assert res == ["A", "B"]

    def test_mmd_file(self, tmp_path):
        p = tmp_path / "a.mmd"
        p.write_text("graph TD\nX-->Y")
        assert m2p.extract_diagrams(str(p)) == ["graph TD\nX-->Y"]

    def test_no_block_exits(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("no diagram here")
        with pytest.raises(SystemExit):
            m2p.extract_diagrams(str(p))

    def test_case_insensitive_fence(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("```MERMAID\nX\n```")
        assert m2p.extract_diagrams(str(p)) == ["X"]

    def test_utf8_bom(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_bytes(b"\xef\xbb\xbf```mermaid\nflowchart TD\nA-->B\n```\n")
        assert m2p.extract_diagrams(str(p)) == ["flowchart TD\nA-->B"]

    def test_mmd_utf8_bom_type(self, tmp_path):
        p = tmp_path / "a.mmd"
        p.write_bytes(b"\xef\xbb\xbfflowchart TD\nA-->B\n")
        assert m2p.diagram_type(m2p.extract_diagrams(str(p))[0]) == "flowchart"

    def test_nested_mermaid_example_is_skipped(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text(
            "```markdown\nExample:\n```mermaid\nflowchart TD\nA-->B\n```\n```\n"
        )
        with pytest.raises(SystemExit):
            m2p.extract_diagrams(str(p))

    def test_unterminated_fence_exits(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("```mermaid\nflowchart TD\nA-->B\n")
        with pytest.raises(SystemExit):
            m2p.extract_diagrams(str(p))


# ------------------------------------------------------------------------------
# diagram_type
# ------------------------------------------------------------------------------


class TestDiagramType:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("sequenceDiagram\nA->>B: hi", "sequenceDiagram"),
            ("flowchart TD\nA-->B", "flowchart"),
            ("graph LR\nA-->B", "graph"),
            ("gantt\ntitle x", "gantt"),
            ("classDiagram\nclass A", "classDiagram"),
            ("erDiagram\nA ||--o{ B : rel", "erDiagram"),
            ("stateDiagram-v2\n[*] --> S", "stateDiagram-v2"),
            ("mindmap\nroot", "mindmap"),
            ("journey\ntitle x", "journey"),
            ("timeline\ntitle x", "timeline"),
        ],
    )
    def test_types(self, code, expected):
        assert m2p.diagram_type(code) == expected

    def test_front_matter_ignored(self):
        code = "---\ntitle: x\n---\nflowchart TD\nA-->B"
        assert m2p.diagram_type(code) == "flowchart"

    def test_comments_ignored(self):
        code = "%% init\n%%{init: {}}%%\ngraph TD\nA-->B"
        assert m2p.diagram_type(code) == "graph"

    def test_bom_ignored(self):
        assert m2p.diagram_type("\ufeffflowchart TD\nA-->B") == "flowchart"


# ------------------------------------------------------------------------------
# wrap_text
# ------------------------------------------------------------------------------


class TestWrapText:
    def test_fits_unchanged(self, meter):
        t = "hi"
        assert m2p.wrap_text(t, meter, 1000) == t

    def test_wraps_long(self, meter):
        t = "this is a very long message that should wrap"
        out = m2p.wrap_text(t, meter, 60)
        assert "<br/>" in out

    def test_existing_br_dropped_kept_when_fits(self, meter):
        t = "a<br/>b"
        out = m2p.wrap_text(t, meter, 1000)
        assert out == t

    def test_single_word_longer_than_max(self, meter):
        t = "supercalifragilistic"
        out = m2p.wrap_text(t, meter, 10)
        assert "<br/>" not in out


# ------------------------------------------------------------------------------
# Wrappers smoke
# ------------------------------------------------------------------------------


class TestWrappers:
    def test_wrap_sequence(self, meter):
        code = "A->>B: hello world this is long\nNote over A,B: note text here"
        out = m2p.wrap_sequence(code, meter, 40)
        assert "A->>B:" in out

    def test_wrap_flowchart(self, meter):
        code = "A --> B: a very long edge label that needs wrapping"
        out = m2p.wrap_flowchart(code, meter, 40)
        assert "A" in out and "B" in out

    def test_wrap_state_diagram(self, meter):
        code = "Still --> Moving : a very long transition label"
        out = m2p.wrap_state_diagram(code, meter, 40)
        assert "Still" in out

    def test_wrap_mindmap(self, meter):
        code = "mindmap\n  root((Root))\n    A very long node label"
        out = m2p.wrap_mindmap(code, meter, 40)
        assert "mindmap" in out

    def test_wrap_journey(self, meter):
        code = "journey\ntitle My day\n    Make tea: 5: Me"
        out = m2p.wrap_journey(code, meter, 40)
        assert "journey" in out

    def test_wrappers_preserve_nonmatching_lines(self, meter):
        code = "%% comment\nfooter"
        for fn in (
            m2p.wrap_sequence,
            m2p.wrap_state_diagram,
            m2p.wrap_journey,
        ):
            assert fn(code, meter, 40) == code


# ------------------------------------------------------------------------------
# Config builders
# ------------------------------------------------------------------------------


class TestConfigs:
    @pytest.mark.parametrize(
        "builder",
        [
            m2p.sequence_config,
            m2p.flowchart_config,
            m2p.gantt_config,
            m2p.class_config,
            m2p.er_config,
            m2p.state_config,
            m2p.mindmap_config,
            m2p.journey_config,
            m2p.timeline_config,
        ],
    )
    def test_common_key(self, builder, meter):
        code = "x"
        cfg = builder(code, meter)
        assert "useMaxWidth" in cfg
        assert cfg["useMaxWidth"] is False

    def test_flowchart_wrapping_min(self, meter):
        cfg = m2p.flowchart_config("A[x] --> B[y]", meter)
        assert cfg["wrappingWidth"] >= 200

    def test_gantt_leftpadding_nonnegative(self, meter):
        code = "gantt\nsection S\nTask :a, 1d"
        cfg = m2p.gantt_config(code, meter)
        assert cfg["leftPadding"] >= 0

    def test_sequence_has_actor_margin(self, meter):
        code = "sequenceDiagram\nparticipant A\nparticipant B\nA->>B: hi"
        cfg = m2p.sequence_config(code, meter)
        assert "actorMargin" in cfg

    def test_build_config_dispatch(self, meter):
        cases = {
            "sequenceDiagram\nA->>B: hi": "sequence",
            "flowchart TD\nA-->B": "flowchart",
            "graph LR\nA-->B": "flowchart",
            "gantt\ntitle x": "gantt",
            "classDiagram\nclass A": "class",
            "erDiagram\nA ||--o{ B : r": "er",
            "stateDiagram-v2\n[*]-->S": "state",
            "mindmap\nroot": "mindmap",
            "journey\ntitle x": "journey",
            "timeline\ntitle x": "timeline",
        }
        for code, key in cases.items():
            cfg = m2p.build_config(code, meter)
            assert key in cfg, (code, cfg)

    def test_build_config_unknown_only_common(self, meter):
        cfg = m2p.build_config("pie title x", meter)
        assert set(cfg.keys()) == {"theme", "fontSize", "themeVariables"}


# ------------------------------------------------------------------------------
# parse_size
# ------------------------------------------------------------------------------


class TestParseSize:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("100x200", (100.0, 200.0)),
            ("100x?", (100.0, None)),
            ("?x200", (None, 200.0)),
            ("100", (100.0, None)),
            ("  50 x 60  ", (50.0, 60.0)),
        ],
    )
    def test_valid(self, text, expected):
        assert m2p.parse_size(text) == expected

    def test_invalid_raises(self):
        with pytest.raises(m2p.argparse.ArgumentTypeError):
            m2p.parse_size("not-a-size")


# ------------------------------------------------------------------------------
# png_size
# ------------------------------------------------------------------------------


class TestPngSize:
    def test_reads_dimensions(self, tmp_path):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        p = tmp_path / "t.png"
        Image.new("RGB", (37, 19), color="white").save(p)
        assert m2p.png_size(str(p)) == (37, 19)

    def test_no_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            m2p.png_size(str(tmp_path / "missing.png"))


# ------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------


class TestCLI:
    def test_help_exits_zero(self):
        import sys as _sys

        old = _sys.argv
        _sys.argv = ["mermaid_to_png.py", "--help"]
        try:
            with pytest.raises(SystemExit) as exc:
                m2p.main()
        finally:
            _sys.argv = old
        assert exc.value.code == 0

    def test_missing_source_exits(self, tmp_path):
        import sys as _sys

        old = _sys.argv
        _sys.argv = ["mermaid_to_png.py", str(tmp_path / "does-not-exist.md")]
        try:
            with pytest.raises(SystemExit):
                m2p.main()
        finally:
            _sys.argv = old


# ------------------------------------------------------------------------------
# Integration (gated)
# ------------------------------------------------------------------------------


def _has_mmdc():
    return shutil.which("mmdc") is not None


@pytest.mark.skipif(not _has_mmdc(), reason="mermaid-cli/npx not available")
class TestIntegrationRender:
    def test_render_flowchart(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA[Hello world]-->B\n```")
        out = tmp_path / "out.png"
        import sys as _sys

        old = _sys.argv
        _sys.argv = ["mermaid_to_png.py", str(src), str(out)]
        try:
            m2p.main()
        finally:
            _sys.argv = old
        assert out.exists()
        w, h = m2p.png_size(str(out))
        assert w > 0 and h > 0


# ------------------------------------------------------------------------------
# Review-fix regressions
# ------------------------------------------------------------------------------


class TestReviewFixes:
    def test_tilde_fence(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("~~~mermaid\nX\n~~~\n")
        assert m2p.extract_diagrams(str(p)) == ["X"]

    def test_indented_fence(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("  ```mermaid\nX\n  ```\n")
        assert m2p.extract_diagrams(str(p)) == ["X"]

    def test_invalid_utf8_exits(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_bytes(b"\xff\xfe\x00")
        with pytest.raises(SystemExit):
            m2p.extract_diagrams(str(p))

    def test_parse_size_rejects_comma(self):
        with pytest.raises(m2p.argparse.ArgumentTypeError):
            m2p.parse_size("100,200")

    def test_journey_config_measures_task_title(self, meter):
        code = "journey\n    A very long task title: 5: Me"
        cfg = m2p.journey_config(code, meter)
        expected = max(150, round(meter.width("A very long task title") + 20))
        assert cfg["width"] == expected
        assert cfg["taskFontSize"] == meter.size
        assert cfg["textPlacement"] == "tspan"

    def test_wrap_journey_wraps_title(self, meter):
        code = "journey\n    A very long title: 5: Me"
        out = m2p.wrap_journey(code, meter, 40)
        assert "5: Me" in out
        assert "<br/>" in out

    def test_font_path_missing_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MERMAID_TO_PNG_FONT", raising=False)
        monkeypatch.setattr(m2p, "FONT_CANDIDATES", [])
        monkeypatch.setattr(m2p, "FONT_SEARCH_ROOTS", [])
        meter = m2p.TextMeter(16, str(tmp_path / "missing.ttf"))
        assert meter.font is None


# ------------------------------------------------------------------------------
# Additional review-fix coverage
# ------------------------------------------------------------------------------


class TestRemovedWrappers:
    def test_removed_wrapper_functions_are_gone(self):
        for name in ("wrap_class_diagram", "wrap_er_diagram", "wrap_timeline"):
            assert not hasattr(m2p, name)


class TestSupportedConfigKeys:
    def test_class_config_keys(self, meter):
        cfg = m2p.class_config("classDiagram\nclass A", meter)
        assert set(cfg) == {"useMaxWidth", "padding"}
        assert cfg["useMaxWidth"] is False

    def test_er_config_keys(self, meter):
        code = (
            "erDiagram\nCUSTOMER {\n    string name\n}\nCUSTOMER ||--o{ ORDER : places"
        )
        cfg = m2p.er_config(code, meter)
        assert set(cfg) == {
            "useMaxWidth",
            "minEntityWidth",
            "entityPadding",
            "fontSize",
        }
        assert cfg["fontSize"] == meter.size
        assert cfg["minEntityWidth"] >= 100

    def test_journey_config_keys(self, meter):
        code = "journey\n    A very long task title: 5: Me"
        cfg = m2p.journey_config(code, meter)
        assert set(cfg) == {"useMaxWidth", "textPlacement", "taskFontSize", "width"}
        assert cfg["textPlacement"] == "tspan"
        assert cfg["taskFontSize"] == meter.size

    def test_timeline_config_keys(self, meter):
        code = "timeline\n    2004 : Facebook : Social network"
        cfg = m2p.timeline_config(code, meter)
        assert set(cfg) == {"useMaxWidth", "padding", "taskFontSize", "width"}
        assert cfg["taskFontSize"] == meter.size

    def test_mindmap_config_keys(self, meter):
        code = "mindmap\n  root((Root))\n    A very long node label"
        cfg = m2p.mindmap_config(code, meter)
        assert set(cfg) == {"useMaxWidth", "padding", "maxNodeWidth"}
        assert cfg["maxNodeWidth"] >= 200

    def test_sequence_font_sizes(self, meter):
        code = "sequenceDiagram\nparticipant A\nparticipant B\nA->>B: hi"
        cfg = m2p.sequence_config(code, meter)
        assert cfg["actorFontSize"] == meter.size
        assert cfg["messageFontSize"] == meter.size
        assert cfg["noteFontSize"] == meter.size

    def test_state_font_size(self, meter):
        cfg = m2p.state_config("stateDiagram-v2\n[*] --> S : hi", meter)
        assert cfg["fontSize"] == meter.size

    def test_gantt_font_sizes(self, meter):
        cfg = m2p.gantt_config("gantt\nsection S\nTask :a, 1d", meter)
        assert cfg["fontSize"] == meter.size
        assert cfg["sectionFontSize"] == meter.size

    def test_er_ignores_relationship_lines_for_min_width(self, meter):
        code = "erDiagram\nCUSTOMER {\n    string name\n}\nCUSTOMER ||--o{ ORDER : a very long relationship label"
        cfg = m2p.er_config(code, meter)
        assert cfg["minEntityWidth"] < meter.width("a very long relationship label")


class TestValidation:
    @pytest.mark.parametrize("text", ["0", "0x0", "0x?", "?x0", "-1", "1x0", "0x1"])
    def test_parse_size_rejects_nonpositive(self, text):
        with pytest.raises(m2p.argparse.ArgumentTypeError):
            m2p.parse_size(text)

    def test_png_size_invalid_signature(self, tmp_path):
        p = tmp_path / "bad.png"
        p.write_bytes(b"not a png file")
        with pytest.raises(OSError):
            m2p.png_size(str(p))

    def _run_cli(self, argv):
        import sys as _sys

        old = _sys.argv
        _sys.argv = argv
        try:
            with pytest.raises(SystemExit):
                m2p.main()
        finally:
            _sys.argv = old

    def test_cli_invalid_dpi_exits(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA-->B\n```\n")
        self._run_cli(["mermaid_to_png.py", str(src), "--dpi", "0"])

    def test_cli_invalid_font_size_exits(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA-->B\n```\n")
        self._run_cli(["mermaid_to_png.py", str(src), "--font-size", "0"])

    def test_cli_negative_max_text_width_exits(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA-->B\n```\n")
        self._run_cli(["mermaid_to_png.py", str(src), "--max-text-width", "-1"])

    def test_cli_invalid_compress_exits(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA-->B\n```\n")
        self._run_cli(["mermaid_to_png.py", str(src), "--compress", "10"])

    def test_cli_invalid_font_path_exits(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA-->B\n```\n")
        self._run_cli(
            [
                "mermaid_to_png.py",
                str(src),
                "--font-path",
                str(tmp_path / "missing.ttf"),
            ]
        )

    def test_cli_dest_svg_exits(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA-->B\n```\n")
        self._run_cli(["mermaid_to_png.py", str(src), str(tmp_path / "out.svg")])

    def test_cli_dest_directory_exits(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA-->B\n```\n")
        outdir = tmp_path / "outdir"
        outdir.mkdir()
        self._run_cli(["mermaid_to_png.py", str(src), str(outdir)])

    def test_cli_nan_dpi_exits(self, tmp_path):
        src = tmp_path / "d.md"
        src.write_text("```mermaid\nflowchart TD\nA-->B\n```\n")
        self._run_cli(["mermaid_to_png.py", str(src), "--dpi", "nan"])
