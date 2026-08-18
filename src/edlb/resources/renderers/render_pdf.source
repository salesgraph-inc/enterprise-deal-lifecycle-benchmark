from __future__ import annotations

import json
import sys
from pathlib import Path

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas


def wrapped_lines(text: str, font: str, size: float, width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and stringWidth(candidate, font, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def draw_wrapped(
    canvas: Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    font: str = "Helvetica",
    size: float = 9.5,
    leading: float = 14,
) -> float:
    canvas.setFont(font, size)
    for line in wrapped_lines(text, font, size, width):
        canvas.drawString(x, y, line)
        y -= leading
    return y


def render(spec: dict[str, object], output: Path) -> None:
    width, height = letter
    canvas = Canvas(str(output), pagesize=letter, pageCompression=1, invariant=1)
    canvas.setTitle(str(spec["title"]))
    canvas.setAuthor(str(spec["seller"]))
    canvas.setSubject("Synthetic enterprise sales proposal")
    canvas.setCreator(str(spec["renderer"]))
    navy = HexColor("#102A43")
    teal = HexColor("#147D92")
    pale = HexColor("#E8F1F5")
    muted = HexColor("#52606D")
    canvas.setFillColor(navy)
    canvas.rect(0, height - 104, width, 104, stroke=0, fill=1)
    canvas.setFillColor(HexColor("#FFFFFF"))
    canvas.setFont("Helvetica-Bold", 20)
    canvas.drawString(48, height - 52, "Commercial Proposal")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(48, height - 76, str(spec["seller"]))
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawRightString(width - 48, height - 54, "SYNTHETIC EDLB ARTIFACT")
    canvas.setFillColor(navy)
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(48, height - 142, str(spec["buyer"]))
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(muted)
    canvas.drawString(48, height - 160, str(spec["motion"]))
    canvas.drawRightString(
        width - 48, height - 142, f"Effective {spec['effective_date']}"
    )
    canvas.setFillColor(pale)
    canvas.roundRect(48, height - 248, width - 96, 60, 6, stroke=0, fill=1)
    canvas.setFillColor(navy)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(64, height - 210, "CURRENT GATE")
    canvas.drawString(260, height - 210, "PROPOSAL VALUE")
    canvas.drawString(440, height - 210, "CURRENCY")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(64, height - 230, str(spec["gate"]))
    canvas.drawString(260, height - 230, str(spec["amount_display"]))
    canvas.drawString(440, height - 230, str(spec["currency"]))
    y = height - 286
    canvas.setFillColor(teal)
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(48, y, "Scope and decision record")
    y -= 24
    canvas.setFillColor(navy)
    y = draw_wrapped(canvas, str(spec["scope"]), 48, y, width - 96)
    y -= 14
    canvas.setFillColor(teal)
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(48, y, "Required buyer confirmation")
    y -= 24
    canvas.setFillColor(navy)
    for item in spec["requirements"]:
        canvas.setFillColor(teal)
        canvas.circle(54, y + 3, 2, stroke=0, fill=1)
        canvas.setFillColor(navy)
        y = draw_wrapped(canvas, str(item), 66, y, width - 114)
        y -= 7
    canvas.setStrokeColor(Color(0.82, 0.86, 0.89))
    canvas.line(48, 92, width - 48, 92)
    canvas.setFillColor(muted)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(48, 74, f"Normalized source: {spec['normalized_source_uri']}")
    canvas.drawString(48, 60, f"Renderer: {spec['renderer']}")
    canvas.drawRightString(width - 48, 60, "Page 1 of 1")
    canvas.save()


def main() -> None:
    spec_path = Path(sys.argv[1])
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True)
    render(json.loads(spec_path.read_text()), output)


if __name__ == "__main__":
    main()
