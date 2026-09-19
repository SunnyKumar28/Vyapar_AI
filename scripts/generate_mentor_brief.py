from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "pdf" / "Vyapaar_AI_Mentor_Brief.pdf"
W, H = A4

NAVY = HexColor("#0B1220")
INK = HexColor("#162238")
MUTED = HexColor("#5D6B7D")
PAPER = HexColor("#F7F8FC")
LINE = HexColor("#DDE3EC")
VIOLET = HexColor("#635BFF")
VIOLET_LIGHT = HexColor("#F0EFFF")
TEAL = HexColor("#0E9F8A")
TEAL_LIGHT = HexColor("#E8F8F4")
ORANGE = HexColor("#E9762B")
ORANGE_LIGHT = HexColor("#FFF2E8")
RED = HexColor("#D3485E")
RED_LIGHT = HexColor("#FFF0F3")


def box(c, x, y, w, h, fill=colors.white, stroke=LINE, radius=12, width=1):
    c.setLineWidth(width)
    c.setStrokeColor(stroke)
    c.setFillColor(fill)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)


def text(c, x, y, value, size=10, color=INK, font="Helvetica", align="left"):
    c.setFillColor(color)
    c.setFont(font, size)
    if align == "center":
        c.drawCentredString(x, y, value)
    elif align == "right":
        c.drawRightString(x, y, value)
    else:
        c.drawString(x, y, value)


def wrap(c, value, width, font="Helvetica", size=10):
    words = value.split()
    lines, line = [], ""
    for word in words:
        trial = word if not line else line + " " + word
        if stringWidth(trial, font, size) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def paragraph(c, x, y, value, width, size=10, leading=14, color=MUTED, font="Helvetica"):
    for line in wrap(c, value, width, font, size):
        text(c, x, y, line, size, color, font)
        y -= leading
    return y


def header(c, num, title, eyebrow="VYAPAAR AI / MENTOR BRIEF"):
    c.setFillColor(PAPER)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(NAVY)
    c.rect(0, H - 68, W, 68, fill=1, stroke=0)
    text(c, 42, H - 29, eyebrow, 8.5, HexColor("#C6D0E2"), "Helvetica-Bold")
    text(c, 42, H - 51, title, 17, colors.white, "Helvetica-Bold")
    text(c, W - 42, H - 47, f"{num} / 4", 9, HexColor("#C6D0E2"), "Helvetica-Bold", "right")
    c.setStrokeColor(LINE)
    c.line(42, 35, W - 42, 35)
    text(c, 42, 19, "Vyapaar AI - multilingual decision-to-action platform for Indian SMBs", 8, MUTED)
    text(c, W - 42, 19, "Mentor brief", 8, MUTED, "Helvetica", "right")


def chip(c, x, y, label, fill, color, width=None):
    width = width or stringWidth(label, "Helvetica-Bold", 8) + 18
    c.setFillColor(fill)
    c.roundRect(x, y, width, 20, 10, fill=1, stroke=0)
    text(c, x + width / 2, y + 6.5, label, 8, color, "Helvetica-Bold", "center")
    return width


def arrow(c, x1, y1, x2, y2, color=VIOLET, dashed=False):
    c.setStrokeColor(color)
    c.setLineWidth(1.5)
    c.setDash(4, 3) if dashed else c.setDash()
    c.line(x1, y1, x2, y2)
    c.setDash()
    import math
    angle = math.atan2(y2 - y1, x2 - x1)
    length = 7
    for delta in (2.55, -2.55):
        c.line(x2, y2, x2 - length * math.cos(angle + delta), y2 - length * math.sin(angle + delta))


def node(c, x, y, w, h, title, subtitle, fill, accent):
    box(c, x, y, w, h, fill, LINE, 11)
    c.setFillColor(accent)
    c.roundRect(x + 12, y + h - 21, 26, 7, 3.5, fill=1, stroke=0)
    text(c, x + 12, y + h - 38, title, 9, INK, "Helvetica-Bold")
    text(c, x + 12, y + 13, subtitle, 7.5, MUTED)


def page_one(c):
    header(c, 1, "From a confusing sales dip to a safe, measured action")
    text(c, 42, 688, "The problem", 11, VIOLET, "Helvetica-Bold")
    text(c, 42, 655, "Indian small-business owners have data, but not an", 24, INK, "Helvetica-Bold")
    text(c, 42, 625, "operating system for acting on it.", 24, INK, "Helvetica-Bold")
    paragraph(c, 42, 590,
              "A merchant can see sales, customers and inventory, yet still cannot answer: Why did sales fall? Which customers should I reach? Is the offer affordable? Did it actually work? Most tools stop at charts or generic recommendations.",
              500, 11, 16)

    box(c, 42, 437, 511, 104, colors.white)
    text(c, 60, 512, "A typical merchant question", 9, VIOLET, "Helvetica-Bold")
    text(c, 60, 481, '"Why were sales lower yesterday, and what should I do next?"', 15, INK, "Helvetica-Bold")
    chip(c, 60, 449, "Language and voice first", VIOLET_LIGHT, VIOLET, 144)
    chip(c, 214, 449, "Evidence, not guesswork", TEAL_LIGHT, TEAL, 150)

    text(c, 42, 404, "What Vyapaar AI changes", 11, VIOLET, "Helvetica-Bold")
    cards = [
        (42, "1", "Understands the signal", "Finds sales anomalies, lapsed regulars, stock risks and peer gaps from the merchant ledger.", VIOLET_LIGHT, VIOLET),
        (218, "2", "Recommends a bounded action", "Builds a costed campaign using grounded tools, guardrails and a measurable success condition.", ORANGE_LIGHT, ORANGE),
        (394, "3", "Closes the loop", "Requires approval, records the action, then measures an outcome against a control group.", TEAL_LIGHT, TEAL),
    ]
    for x, n, title, body, fill, accent in cards:
        box(c, x, 248, 159, 130, colors.white)
        c.setFillColor(fill)
        c.circle(x + 25, 344, 15, fill=1, stroke=0)
        text(c, x + 25, 338.5, n, 10, accent, "Helvetica-Bold", "center")
        text(c, x + 16, 313, title, 10, INK, "Helvetica-Bold")
        paragraph(c, x + 16, 292, body, 127, 8.3, 11.5)

    box(c, 42, 94, 511, 121, NAVY, NAVY)
    text(c, 60, 185, "The product promise", 9, HexColor("#B9C6DA"), "Helvetica-Bold")
    text(c, 60, 158, "SENSE  >  DIAGNOSE  >  PRESCRIBE  >  APPROVE", 13, colors.white, "Helvetica-Bold")
    text(c, 60, 135, "EXECUTE  >  MEASURE  >  LEARN", 13, colors.white, "Helvetica-Bold")
    paragraph(c, 60, 112, "The merchant stays in control. The system makes decisions explainable, approval-gated and traceable.", 440, 9.3, 13, HexColor("#C6D0E2"))
    c.showPage()


def page_two(c):
    header(c, 2, "How Vyapaar AI works")
    text(c, 42, 684, "One merchant question becomes an accountable growth loop.", 12, MUTED)

    steps = [
        ("01", "Sense", "Scheduled detectors scan the transaction ledger for day-of-week dips, customer churn risk, stock signals and peer gaps.", VIOLET, VIOLET_LIGHT),
        ("02", "Diagnose", "The agent calls allowlisted data tools to retrieve sales, customers, inventory and historical context. It does not invent business numbers.", TEAL, TEAL_LIGHT),
        ("03", "Prescribe", "A playbook estimates audience, spend cap, expected impact and success criteria using code-based estimators.", ORANGE, ORANGE_LIGHT),
        ("04", "Approve", "A signed merchant approval is required before any customer-facing action. Policy code checks audience, frequency, spend and quiet hours.", RED, RED_LIGHT),
        ("05", "Execute and measure", "A campaign connector records delivery and redemption events. Outcome logic compares the treated group with matched never-messaged customers.", VIOLET, VIOLET_LIGHT),
    ]
    y = 604
    for i, (num, title, body, accent, light) in enumerate(steps):
        box(c, 42, y - 72, 511, 72, colors.white)
        c.setFillColor(light)
        c.circle(73, y - 36, 18, fill=1, stroke=0)
        text(c, 73, y - 40, num, 8.5, accent, "Helvetica-Bold", "center")
        text(c, 106, y - 25, title, 11, INK, "Helvetica-Bold")
        paragraph(c, 106, y - 44, body, 420, 8.8, 12)
        if i < len(steps) - 1:
            arrow(c, 73, y - 77, 73, y - 88, HexColor("#AAB5C5"))
        y -= 92

    box(c, 42, 70, 511, 72, HexColor("#FFFDF6"), HexColor("#F4E0B3"))
    text(c, 60, 119, "Language and voice are part of the workflow, not an afterthought.", 10, INK, "Helvetica-Bold")
    paragraph(c, 60, 101, "The dashboard sends the selected language code with chat, speech-to-text and text-to-speech requests. Sarvam services provide multilingual AI and voice where configured; browser voice is the fallback. The UI supports English plus Hindi, Bengali, Tamil, Telugu, Kannada, Malayalam, Marathi, Gujarati, Punjabi and Odia.", 466, 8.0, 10.5)
    c.showPage()


def page_three(c):
    header(c, 3, "Architecture: grounded AI with a human control point")
    text(c, 42, 684, "Every component serves an explicit responsibility in the decision-to-action loop.", 11, MUTED)

    # Main request path
    node(c, 42, 522, 80, 58, "Merchant", "web / voice", VIOLET_LIGHT, VIOLET)
    node(c, 137, 522, 92, 58, "Dashboard", "selector + chat", VIOLET_LIGHT, VIOLET)
    node(c, 244, 522, 90, 58, "FastAPI", "chat + voice APIs", VIOLET_LIGHT, VIOLET)
    node(c, 349, 522, 92, 58, "Agent loop", "allowlisted tools", VIOLET_LIGHT, VIOLET)
    node(c, 456, 522, 98, 58, "Data store", "ledger + memory", VIOLET_LIGHT, VIOLET)
    arrow(c, 122, 551, 137, 551)
    arrow(c, 229, 551, 244, 551)
    arrow(c, 334, 551, 349, 551)
    arrow(c, 441, 551, 456, 551)
    text(c, 82, 493, "Question + approval", 6.7, MUTED, "Helvetica", "center")
    text(c, 183, 493, "HTTPS / SSE", 6.7, MUTED, "Helvetica", "center")
    text(c, 289, 493, "Language code", 6.7, MUTED, "Helvetica", "center")
    text(c, 395, 493, "Grounded reads", 6.7, MUTED, "Helvetica", "center")

    # Supporting services
    node(c, 244, 368, 90, 58, "Sarvam AI", "LLM + STT + TTS", TEAL_LIGHT, TEAL)
    node(c, 349, 368, 110, 58, "Policy + approval", "caps + signed consent", RED_LIGHT, RED)
    node(c, 472, 368, 82, 58, "Audit trail", "prompt to outcome", RED_LIGHT, RED)
    arrow(c, 289, 522, 289, 426, TEAL)
    arrow(c, 395, 522, 404, 426, RED)
    arrow(c, 441, 551, 513, 426, RED)
    text(c, 302, 462, "chat / speech", 6.7, TEAL)
    text(c, 417, 462, "approval gate", 6.7, RED)

    # Execution / learning path
    node(c, 349, 220, 110, 58, "Campaign connector", "WhatsApp mock in MVP", ORANGE_LIGHT, ORANGE)
    node(c, 472, 220, 82, 58, "Outcomes", "control + ROI", TEAL_LIGHT, TEAL)
    arrow(c, 404, 368, 404, 278, RED)
    arrow(c, 459, 249, 472, 249, ORANGE, True)
    arrow(c, 513, 278, 505, 522, TEAL, True)
    text(c, 416, 318, "approved action", 6.7, RED)
    text(c, 466, 228, "events", 6.7, ORANGE, "Helvetica", "center")
    text(c, 535, 361, "measured", 6.7, TEAL, "Helvetica", "center")

    box(c, 42, 90, 511, 85, colors.white)
    text(c, 60, 150, "Design principles", 9.5, VIOLET, "Helvetica-Bold")
    principles = [
        ("Grounded", "Business facts are retrieved by tools from merchant data."),
        ("Controlled", "Policy and signed approval are code gates, outside the LLM prompt."),
        ("Auditable", "Questions, tools, decisions and outcomes are retained as a trace."),
        ("Measured", "Campaign results use a matched control group, not only raw sales lift."),
    ]
    x = 60
    for title, body in principles:
        c.setFillColor(VIOLET)
        c.circle(x + 3, 126, 3, fill=1, stroke=0)
        text(c, x + 12, 122, title, 8.3, INK, "Helvetica-Bold")
        paragraph(c, x + 12, 108, body, 100, 7.1, 9.3)
        x += 120
    c.showPage()


def page_four(c):
    header(c, 4, "Why this is more than a chatbot demo")
    text(c, 42, 684, "Vyapaar AI is designed to turn recommendations into accountable operating decisions.", 11, MUTED)

    left = [
        ("Detect with code", "Sales anomalies and churn signals are produced by deterministic detectors with fallbacks."),
        ("Ground every number", "The agent uses registered tools and a code-based impact estimator for figures and projections."),
        ("Keep the merchant in control", "No outreach occurs before signed approval. Cost, frequency and quiet-hour limits are enforced in policy code."),
        ("Learn from impact", "Delivery, redemption and financial outcome data feed back into the next decision."),
    ]
    y = 610
    for title, body in left:
        box(c, 42, y - 70, 300, 70, colors.white)
        c.setFillColor(VIOLET)
        c.circle(62, y - 24, 7, fill=1, stroke=0)
        text(c, 79, y - 26, title, 10, INK, "Helvetica-Bold")
        paragraph(c, 79, y - 43, body, 240, 8.2, 11)
        y -= 84

    box(c, 368, 248, 185, 362, NAVY, NAVY)
    text(c, 388, 580, "Demo scenario", 9, HexColor("#C6D0E2"), "Helvetica-Bold")
    text(c, 388, 548, "What the prototype", 13, colors.white, "Helvetica-Bold")
    text(c, 388, 530, "can demonstrate", 13, colors.white, "Helvetica-Bold")
    metrics = [
        ("-18%", "Sunday sales dip detected"),
        ("212", "lapsed regulars identified"),
        ("Rs 4.8k-6.5k", "expected GMV from estimator"),
        ("4.84x", "illustrative measured ROI"),
    ]
    yy = 487
    for value, label in metrics:
        text(c, 388, yy, value, 15, HexColor("#B9C6FF"), "Helvetica-Bold")
        paragraph(c, 388, yy - 15, label, 140, 7.8, 10.2, HexColor("#D5DDEA"))
        yy -= 57
    paragraph(c, 388, 276, "These are seeded, reproducible demonstration metrics. They are not claims about a live merchant deployment.", 145, 6.5, 8, HexColor("#B9C6DA"))

    text(c, 42, 224, "MVP status and credible next step", 11, VIOLET, "Helvetica-Bold")
    box(c, 42, 101, 511, 101, colors.white)
    text(c, 60, 176, "Working now", 9, TEAL, "Helvetica-Bold")
    paragraph(c, 60, 158, "Sharma Tea Stall is the default demo. Import a sales CSV to replace the active workspace; chat, signals and forecasts then use your ledger. Restore the demo anytime.", 205, 8.0, 10.5)
    text(c, 317, 176, "Production path", 9, ORANGE, "Helvetica-Bold")
    paragraph(c, 317, 158, "Move durable merchant imports from temporary SQLite to Postgres and secure object storage; replace the mock connector with an approved WhatsApp BSP; add authentication and consent management.", 210, 8.2, 11)
    text(c, 60, 75, "Live app: vyapaar-ai-phi.vercel.app", 8.5, VIOLET, "Helvetica-Bold")
    text(c, 60, 57, "Source: github.com/SunnyKumar28/Vyapar_AI", 8.5, VIOLET, "Helvetica-Bold")
    c.showPage()


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(OUT), pagesize=A4, pageCompression=1)
    c.setTitle("Vyapaar AI - Mentor Brief")
    c.setAuthor("Sunny Kumar")
    c.setSubject("Problem, workflow and architecture of Vyapaar AI")
    page_one(c)
    page_two(c)
    page_three(c)
    page_four(c)
    c.save()
    print(OUT)


if __name__ == "__main__":
    main()
