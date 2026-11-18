"""The guest invoice as a PDF file, sent on WhatsApp as a document (owner, 2026-11-18).

Built with fpdf2 (pure Python, no system packages, so it runs the same on Railway). The core PDF fonts have no rupee
sign, so amounts are written "Rs 9,000". Text outside Latin-1 (a name typed in Malayalam script) is shown as '?'
rather than breaking the file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fpdf import FPDF

from app.core.clock import IST
from app.resort.paying import method_label

TEAL = (15, 76, 92)
GOLD = (195, 148, 70)
INK = (28, 43, 47)
MUTED = (106, 120, 124)
LINE = (233, 227, 217)
SOFT = (250, 248, 244)


def _t(text: Any) -> str:
    return str(text or "").encode("latin-1", "replace").decode("latin-1")


def _rs(amount: int) -> str:
    return f"Rs {amount:,}" if amount >= 0 else f"- Rs {-amount:,}"


def _day(value: datetime | None, fmt: str = "%d %b %Y") -> str:
    return value.astimezone(IST).strftime(fmt) if value else ""


def invoice_filename(invoice_no: str) -> str:
    return f"Invoice-{invoice_no}.pdf"


def build_invoice_pdf(ctx: dict[str, Any]) -> bytes:
    """ctx is the dashboard's invoice context: b, prop, rt, f (folio), payments, refund_list, invoice_no, issued."""
    b, prop, rt, f = ctx["b"], ctx["prop"], ctx["rt"], ctx["f"]
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(16, 16, 16)
    width = pdf.w - 32

    # header band
    pdf.set_fill_color(*TEAL)
    pdf.rect(0, 0, pdf.w, 42, "F")
    pdf.set_fill_color(*GOLD)
    pdf.rect(0, 42, pdf.w, 1.4, "F")
    pdf.set_xy(16, 11)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 19)
    pdf.cell(width * 0.62, 9, _t(prop.name if prop else "Resort"))
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(width * 0.38, 9, "TAX INVOICE" if f.gst_pct else "INVOICE", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(214, 230, 230)
    place = ", ".join(x for x in ((prop.address if prop else ""), (prop.city if prop else "")) if x)
    pdf.cell(width * 0.62, 5, _t(place))
    pdf.cell(width * 0.38, 5, _t(ctx["invoice_no"]), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(width * 0.62, 5, _t(f"GSTIN: {prop.gstin}") if prop and prop.gstin else "")
    pdf.cell(width * 0.38, 5, _t(f"Date: {_day(ctx['issued'])}   Booking: {b.id}"), align="R", new_x="LMARGIN", new_y="NEXT")

    # billed to / stay
    pdf.set_y(52)
    col = width / 2
    top = pdf.get_y()
    _label(pdf, "BILLED TO")
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*INK)
    pdf.cell(col, 6, _t(b.guest_name or "Guest"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(col, 5, _t(b.guest_phone), new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(16 + col, top)
    _label(pdf, "STAY", x=16 + col)
    pdf.set_x(16 + col)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*INK)
    nights = f"{b.nights} night{'s' if b.nights != 1 else ''}"
    pdf.cell(col, 6, _t(f"{b.check_in:%d %b %Y} to {b.check_out:%d %b %Y} ({nights})"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(16 + col)
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*MUTED)
    room = f"{b.rooms} x {rt.name if rt else 'Room'}  |  {b.adults} adults, {b.children} children"
    pdf.multi_cell(col, 5, _t(room + (f"  |  room {b.room_numbers}" if b.room_numbers else "")),
                   new_x="LMARGIN", new_y="NEXT")

    # bill lines
    pdf.ln(6)
    pdf.set_fill_color(*SOFT)
    pdf.set_draw_color(*LINE)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*MUTED)
    pdf.cell(width * 0.75, 8, "  DESCRIPTION", border="TB", fill=True)
    pdf.cell(width * 0.25, 8, "AMOUNT  ", border="TB", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    for line in f.lines:
        y = pdf.get_y()
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*INK)
        pdf.set_x(18)
        pdf.cell(width * 0.75 - 2, 6, _t(line.label), new_x="LMARGIN", new_y="NEXT")
        if line.detail:
            pdf.set_x(18)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(*MUTED)
            pdf.multi_cell(width * 0.75 - 2, 4.5, _t(line.detail), new_x="LMARGIN", new_y="NEXT")
        end = pdf.get_y() + 1.5
        pdf.set_xy(16 + width * 0.75, y)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*INK)
        pdf.cell(width * 0.25, 6, _rs(line.amount) + "  ", align="R")
        pdf.set_y(end)
        pdf.line(16, end, 16 + width, end)
        pdf.ln(1)

    # totals
    rows = []
    if f.gst_pct:
        half = round(f.gst_pct / 2, 1)
        rows += [("Taxable value", f.subtotal), (f"CGST @ {half}%", f.cgst), (f"SGST @ {half}%", f.sgst)]
    for label, amount in rows:
        _total_row(pdf, width, label, amount)
    pdf.set_fill_color(*TEAL)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_x(16 + width * 0.45)
    pdf.cell(width * 0.30, 9, "  Total", fill=True)
    pdf.cell(width * 0.25, 9, _rs(f.bill_total) + "  ", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    # payments and balance
    pdf.ln(7)
    top = pdf.get_y()
    _label(pdf, "PAYMENTS RECEIVED")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*INK)
    payments = ctx.get("payments") or []
    for p in payments:
        pdf.cell(col, 5, _t(f"{_day(p.at)}  |  {method_label(p.method)}  |  {_rs(p.amount)}"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*MUTED)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(col, 4, _t(f"Ref {p.ref}"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)
        pdf.set_font("Helvetica", "", 9.5)
    if not payments:
        pdf.cell(col, 5, "No payment received yet.", new_x="LMARGIN", new_y="NEXT")
    for r in ctx.get("refund_list") or []:
        pdf.cell(col, 5, _t(f"Refund {_day(r.at)}  |  {_rs(r.amount)}"), new_x="LMARGIN", new_y="NEXT")
    after = pdf.get_y()
    box_x, box_w = 16 + col + 6, col - 6
    pdf.set_fill_color(*SOFT)
    pdf.set_draw_color(*LINE)
    pdf.rect(box_x, top, box_w, 26, "DF")
    pdf.set_xy(box_x + 5, top + 4)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(box_w - 10, 4, "BALANCE PAYABLE AT CHECK-IN" if f.balance else "BALANCE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(box_x + 5, top + 10)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*(TEAL if f.balance else (31, 122, 82)))
    pdf.cell(box_w - 10, 10, _rs(f.balance))
    if b.status == "cancelled":
        pdf.set_xy(box_x + 5, top + 21)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(194, 65, 59)
        pdf.cell(box_w - 10, 4, "This booking was cancelled.")
    pdf.set_y(max(after, top + 26) + 8)

    if prop and prop.policies:
        _label(pdf, "TERMS")
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(width, 4.5, _t(prop.policies), new_x="LMARGIN", new_y="NEXT")

    # footer
    pdf.set_y(-26)
    pdf.set_draw_color(*LINE)
    pdf.line(16, pdf.get_y(), 16 + width, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*MUTED)
    contact = "  |  ".join(prop.contact_numbers) if prop and prop.contact_numbers else ""
    pdf.cell(width, 5, _t(f"Thank you for choosing {prop.name if prop else 'us'}." + (f"  {contact}" if contact else "")),
             align="C")
    return bytes(pdf.output())


def _label(pdf: FPDF, text: str, x: float | None = None) -> None:
    if x is not None:
        pdf.set_x(x)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*GOLD)
    pdf.cell(80, 5, text, new_x="LMARGIN", new_y="NEXT")


def _total_row(pdf: FPDF, width: float, label: str, amount: int) -> None:
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*MUTED)
    pdf.set_x(16 + width * 0.45)
    pdf.cell(width * 0.30, 7, "  " + _t(label))
    pdf.set_text_color(*INK)
    pdf.cell(width * 0.25, 7, _rs(amount) + "  ", align="R", new_x="LMARGIN", new_y="NEXT")
