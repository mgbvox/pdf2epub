"""Font-name heuristics: style (bold/italic/mono) and math detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# Fonts that only ever carry mathematics.
_MATH_ONLY = re.compile(
    r"^(CMMI|CMMIB|CMSY|CMBSY|CMEX|CMSYB|MSAM|MSBM|EUFM|EUFB|EUSM|EUSB|EURM|EURB|EUEX|"
    r"RSFS|STMARY|WASY|ESINT|MNSYMBOL|BBOLD|BBM|DSROM|TXMI|TXSY|TXEX|PXMI|PXSY|PXEX|"
    r"RTXMI|RTXSY|NTXMI|NTXSY|NTXEXX|NEWTXMI|NEWPXMI|ZXMI|ZXSY|LMMATH|LATINMODERNMATH|"
    r"LMMI|LMSY|LMEX|STIXMATH|STIX2MATH|STIXTWOMATH|XITSMATH|CAMBRIAMATH|ASANAMATH|"
    r"TEXGYRE\w*MATH|LIBERTINUSMATH|FIRAMATH|GARAMONDMATH|DEJAVUMATHTEX|MTMI|MTSY|MTEX|"
    r"MTEXTRA|EUCLID|MATHJAX|KATEX_MATH|KATEX_SIZE|KATEX_AMS|KATEX_CALIGRAPHIC|"
    r"KATEX_FRAKTUR|KATEX_SCRIPT|MSCB|MSBMB|STIXGENERAL|STIXNONUNICODE|STIXSIZE|STIXINTEGRALS|"
    r"STIXVARIANTS|SYMBOL$|SYMBOLMT|STANDARDSYMBOL|FONTAWESOME)",
    re.I,
)
_MATH_ANYWHERE = re.compile(r"MATH|SYMBOL", re.I)

# TeX text families that are also used inside math mode (digits, operators,
# \mathrm, \mathbf, \mathtt ...).  Whether they count as math in a given
# document depends on whether the body text uses them (see analysis.py).
_TEX_TEXT_FAMILY = re.compile(
    r"^(CMR|CMBX|CMTT|CMSS|CMTI|CMSL|CMBXTI|CMBXSL|CMB\d|CMSSBX|CMSSI|CMDUNH|CMFIB|CMU|"
    r"LMROMAN|LMSANS|LMMONO|LMR|LMBX|LMTT|SFRM|SFBX|SFTT|SFSS|SFTI|SFSL|EC[A-Z]{2}\d|"
    r"TXR|TXB|PXR|PXB)",
    re.I,
)

_BOLD = re.compile(
    r"bold|black|heavy|semibold|demi|medi(um)?\b|-medi|\bbd\b|-b$|^cmbx|^cmb\d|^sfbx|^lmbx|"
    r"^cmssbx|^eurb|^cmmib|^cmbsy|extrabold|ultrabold|,bold",
    re.I,
)
_ITALIC = re.compile(
    r"italic|oblique|ital\b|ital$|-it$|-it[a-z]*$|^cmti|^cmmi|^cmsl|^sfti|^sfsl|^cmbxti|"
    r"^cmssi|slant|kursiv|,italic",
    re.I,
)
_MONO = re.compile(
    r"mono|courier|^cmtt|^sftt|^lmtt|consol|inconsolata|typewriter|menlo|source\s?code|"
    r"code|fixed|^txtt|^pxtt|^zi4|beramono|dejavusansmono|lucidaconsole|^ectt|cmvtt",
    re.I,
)


@dataclass(frozen=True)
class FontInfo:
    name: str  # normalised name (subset prefix removed)
    bold: bool
    italic: bool
    mono: bool
    math_only: bool  # font is exclusively used for mathematics
    tex_family: bool  # TeX text family that may appear in math mode


def normalise_font_name(name: str) -> str:
    """Strip the ``ABCDEF+`` subset prefix PDF producers add to embedded fonts."""
    if len(name) > 7 and name[6] == "+" and name[:6].isalpha() and name[:6].isupper():
        name = name[7:]
    return name


@lru_cache(maxsize=4096)
def font_info(name: str, flags: int = 0) -> FontInfo:
    n = normalise_font_name(name)
    compact = re.sub(r"[\s_]", "", n)
    math_only = bool(_MATH_ONLY.match(compact)) or bool(_MATH_ANYWHERE.search(compact))
    tex_family = bool(_TEX_TEXT_FAMILY.match(compact)) and not math_only
    bold = bool(_BOLD.search(compact)) or bool(flags & 16)
    italic = bool(_ITALIC.search(compact)) or (bool(flags & 2) and not math_only)
    mono = bool(_MONO.search(compact)) or (bool(flags & 8) and not math_only)
    return FontInfo(n, bold, italic, mono, math_only, tex_family)


def family_key(name: str) -> str:
    """A coarse family key used to compare fonts ("NimbusRomNo9L-Medi" -> "nimbusromno9l")."""
    n = normalise_font_name(name).lower()
    n = re.split(r"[-,]", n)[0]
    n = re.sub(r"\d+$", "", n)  # CMR10 -> cmr
    return n
