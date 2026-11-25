"""Couleurs des labels Gmail.

L'API Gmail n'accepte qu'une palette fixe de couleurs. Pour une couleur de `labels.yaml` hors
palette, on prend la couleur acceptée la plus proche (et le journal le signale). La couleur du
texte est choisie d'après le fond : blanc sur un fond foncé, gris très foncé sur un fond clair.
"""

from __future__ import annotations

from dataclasses import dataclass

GMAIL_PALETTE: tuple[str, ...] = (
    "#000000", "#434343", "#666666", "#999999", "#cccccc", "#efefef", "#f3f3f3", "#ffffff",
    "#fb4c2f", "#ffad47", "#fad165", "#16a766", "#43d692", "#4a86e8", "#a479e2", "#f691b3",
    "#f6c5be", "#ffe6c7", "#fef1d1", "#b9e4d0", "#c6f3de", "#c9daf8", "#e4d7f5", "#fcdee8",
    "#efa093", "#ffd6a2", "#fce8b3", "#89d3b2", "#a0eac9", "#a4c2f4", "#d0bcf1", "#fbc8d9",
    "#e66550", "#ffbc6b", "#fcda83", "#44b984", "#68dfa9", "#6d9eeb", "#b694e8", "#f7a7c0",
    "#cc3a21", "#eaa041", "#f2c960", "#149e60", "#3dc789", "#3c78d8", "#8e63ce", "#e07798",
    "#ac2b16", "#cf8933", "#d5ae49", "#0b804b", "#2a9c68", "#285bac", "#653e9b", "#b65775",
    "#822111", "#a46a21", "#aa8831", "#076239", "#1a764d", "#1c4587", "#41236d", "#83334c",
    "#464646", "#e7e7e7", "#0d3472", "#b6cff5", "#0d3b44", "#98d7e4", "#3d188e", "#e3d7ff",
    "#711a36", "#fbd3e0", "#8a1c0a", "#f2b2a8", "#7a2e0b", "#ffc8af", "#7a4706", "#ffdeb5",
    "#594c05", "#fbe983", "#684e07", "#fdedc1", "#0b4f30", "#b3efd3", "#04502e", "#a2dcc1",
    "#c2c2c2", "#4986e7", "#2da2bb", "#b99aff", "#994a64", "#f691b2", "#ff7537", "#ffad46",
    "#662e37", "#ebdbde", "#cca6ac", "#094228", "#42d692", "#16a765",
)  # fmt: skip
"""Couleurs acceptées par l'API Gmail (fond comme texte)."""

LIGHT_TEXT = "#ffffff"
DARK_TEXT = "#434343"


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _lab(color: str) -> tuple[float, float, float]:
    """sRGB → CIELAB (illuminant D65) : des distances proches de celles que voit l'œil."""

    def linear(u: float) -> float:
        return u / 12.92 if u <= 0.04045 else ((u + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v / 255) for v in _rgb(color))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _distance(a: str, b: str) -> float:
    """Écart perçu entre deux couleurs (ΔE 1976)."""
    return sum((p - q) ** 2 for p, q in zip(_lab(a), _lab(b), strict=True)) ** 0.5


def nearest(color: str) -> str:
    """Couleur de la palette Gmail la plus proche (la couleur elle-même si elle y est)."""
    color = color.lower()
    if color in GMAIL_PALETTE:
        return color
    return min(GMAIL_PALETTE, key=lambda candidate: _distance(color, candidate))


def text_color(background: str) -> str:
    """Blanc sur un fond foncé, gris très foncé sur un fond clair."""
    r, g, b = _rgb(background)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return LIGHT_TEXT if luminance < 150 else DARK_TEXT


@dataclass(frozen=True)
class LabelColor:
    requested: str
    background: str
    text: str

    @property
    def adjusted(self) -> bool:
        return self.requested.lower() != self.background


def label_color(requested: str) -> LabelColor:
    background = nearest(requested)
    return LabelColor(requested=requested, background=background, text=text_color(background))
