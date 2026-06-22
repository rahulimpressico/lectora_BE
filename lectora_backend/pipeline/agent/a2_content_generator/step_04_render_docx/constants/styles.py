"""
DOCX style definitions matching the reference document:
  IAR_3940_SG AssAll as052225f ACCEPTED (1).docx

Color palette:
  Deep purple  #3A0A5A  — title, FD question borders
  Medium purple #9B85B5 — Heading 1 background + borders
  Light lavender #DDD6E6 — "Important" callout box shading
  Dark navy    #052A65  — Heading 2/3/4, FD answer, TOC
  Navy blue    #002060  — "Important" text color
  White        #FFFFFF  — Heading 1 text on purple bg
"""

from docx.shared import Pt, Inches, RGBColor, Cm, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml


# ── Colors ──────────────────────────────────────────────────────────────────

DEEP_PURPLE   = RGBColor(0x3A, 0x0A, 0x5A)
MEDIUM_PURPLE = RGBColor(0x9B, 0x85, 0xB5)
LIGHT_LAVENDER = RGBColor(0xDD, 0xD6, 0xE6)
DARK_NAVY     = RGBColor(0x05, 0x2A, 0x65)
NAVY_BLUE     = RGBColor(0x00, 0x20, 0x60)
WHITE         = RGBColor(0xFF, 0xFF, 0xFF)
BLACK         = RGBColor(0x00, 0x00, 0x00)

# ── Fonts ───────────────────────────────────────────────────────────────────

BODY_FONT    = "Palatino Linotype"
HEADING_FONT = "Antique Olive"
TITLE_FONT   = "Antique Olive Roman"
QUOTE_FONT   = "Segoe UI"
TOC_FONT     = "Calibri"

# ── Sizes ───────────────────────────────────────────────────────────────────

BODY_SIZE     = Pt(11)
H1_SIZE       = Pt(14)   # Major section (N.0) — purple box, white text
H2_SIZE       = Pt(13)   # Sub-section (N.M) — navy, purple left-border accent
H3_SIZE       = Pt(12)
H4_SIZE       = Pt(11)
TITLE_SIZE    = Pt(26)
QUOTE_SIZE    = Pt(9)
TOC_SIZE      = Pt(11)

# ── Indents ─────────────────────────────────────────────────────────────────

BODY_LEFT_INDENT = Inches(2.0)
H3_LEFT_INDENT   = Inches(0.32)
H4_LEFT_INDENT   = Inches(1.0)


def setup_styles(doc):
    """
    Create or configure custom styles in a Document object to match
    the reference study guide formatting.
    """
    styles = doc.styles

    # ── Normal ──────────────────────────────────────────────────────────
    normal = styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = BODY_SIZE
    normal.font.color.rgb = BLACK
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.space_before = Pt(0)

    # ── Bar Text (main body — 2in left indent) ─────────────────────────
    bar_text = styles.add_style("Bar Text", 1)  # WD_STYLE_TYPE.PARAGRAPH = 1
    bar_text.base_style = normal
    bar_text.font.name = BODY_FONT
    bar_text.font.size = BODY_SIZE
    bar_text.paragraph_format.left_indent = BODY_LEFT_INDENT
    bar_text.paragraph_format.space_before = Pt(8)
    bar_text.paragraph_format.space_after = Pt(4)

    # ── Bar Text - Important (lavender callout box) ────────────────────
    important = styles.add_style("Bar Text - Important", 1)
    important.base_style = bar_text
    important.font.color.rgb = NAVY_BLUE
    important.paragraph_format.space_before = Pt(3)
    important.paragraph_format.space_after = Pt(3)

    # ── Heading styles ─────────────────────────────────────────────────

    # We use built-in Heading 1/2/3 and restyle them
    h1 = styles["Heading 1"]
    h1.font.name = HEADING_FONT
    h1.font.size = H1_SIZE
    h1.font.bold = True
    h1.font.color.rgb = WHITE
    h1.paragraph_format.space_before = Pt(18)
    h1.paragraph_format.space_after = Pt(12)

    h2 = styles["Heading 2"]
    h2.font.name = HEADING_FONT
    h2.font.size = H2_SIZE      # 13pt — visually smaller than H1 (14pt)
    h2.font.bold = True
    h2.font.color.rgb = DARK_NAVY
    h2.paragraph_format.left_indent = Inches(0.18)
    h2.paragraph_format.space_before = Pt(12)
    h2.paragraph_format.space_after = Pt(6)

    h3 = styles["Heading 3"]
    h3.font.name = HEADING_FONT
    h3.font.size = H3_SIZE
    h3.font.bold = True
    h3.font.color.rgb = DARK_NAVY
    h3.paragraph_format.left_indent = H3_LEFT_INDENT
    h3.paragraph_format.space_before = Pt(10)
    h3.paragraph_format.space_after = Pt(6)

    return styles


def apply_heading1_shading(paragraph):
    """
    Apply purple background (#9B85B5) with 1.5pt purple border to a Heading 1 paragraph.
    Matches the reference doc's 'Contents' -> 'Heading 1 New' chain.
    """
    pPr = paragraph._p.get_or_add_pPr()

    # Shading
    shading = parse_xml(
        f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="9B85B5"/>'
    )
    pPr.append(shading)

    # Borders (all sides: 1.5pt solid #9B85B5)
    borders = parse_xml(
        f'<w:pBdr {nsdecls("w")}>'
        f'  <w:top w:val="single" w:sz="12" w:space="1" w:color="9B85B5"/>'
        f'  <w:left w:val="single" w:sz="12" w:space="4" w:color="9B85B5"/>'
        f'  <w:bottom w:val="single" w:sz="12" w:space="1" w:color="9B85B5"/>'
        f'  <w:right w:val="single" w:sz="12" w:space="4" w:color="9B85B5"/>'
        f'</w:pBdr>'
    )
    pPr.append(borders)


def apply_heading2_accent(paragraph):
    """
    Apply a medium-purple left border to a Heading 2 paragraph.

    Visually signals sub-section status: clearly subordinate to the H1 purple box
    but sharing the same purple color family.
    """
    pPr = paragraph._p.get_or_add_pPr()
    borders = parse_xml(
        f'<w:pBdr {nsdecls("w")}>'
        f'  <w:left w:val="thick" w:sz="12" w:space="6" w:color="9B85B5"/>'
        f'</w:pBdr>'
    )
    pPr.append(borders)


def apply_important_shading(paragraph):
    """
    Apply light lavender (#DDD6E6) background shading + borders to a paragraph.
    Used for 'Bar Text - Important' callout boxes.
    """
    pPr = paragraph._p.get_or_add_pPr()

    shading = parse_xml(
        f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="DDD6E6"/>'
    )
    pPr.append(shading)

    borders = parse_xml(
        f'<w:pBdr {nsdecls("w")}>'
        f'  <w:top w:val="single" w:sz="4" w:space="1" w:color="DDD6E6"/>'
        f'  <w:left w:val="single" w:sz="4" w:space="4" w:color="DDD6E6"/>'
        f'  <w:bottom w:val="single" w:sz="4" w:space="1" w:color="DDD6E6"/>'
        f'  <w:right w:val="single" w:sz="4" w:space="4" w:color="DDD6E6"/>'
        f'</w:pBdr>'
    )
    pPr.append(borders)


def apply_fd_question_borders(paragraph, position="top"):
    """
    Apply decorative border to Focus/Discussion question blocks.
    position: 'top' for question start, 'bottom' for question end.
    """
    pPr = paragraph._p.get_or_add_pPr()

    if position == "top":
        borders = parse_xml(
            f'<w:pBdr {nsdecls("w")}>'
            f'  <w:top w:val="thinThickSmallGap" w:sz="24" w:space="1" w:color="3A0A5A"/>'
            f'</w:pBdr>'
        )
    else:
        borders = parse_xml(
            f'<w:pBdr {nsdecls("w")}>'
            f'  <w:bottom w:val="thickThinSmallGap" w:sz="24" w:space="1" w:color="3A0A5A"/>'
            f'</w:pBdr>'
        )
    pPr.append(borders)
