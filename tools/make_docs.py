"""Build the submission bundle: PDFs of every write-up, plus dashboard snapshots.

The brief asks for the write-ups "in doc/pdf with a clean snapshot". This is
that step, written as a script rather than done by hand so the bundle can be
regenerated from a clean clone and cannot drift from the Markdown it came from.

    python tools/make_docs.py

Outputs into `docs/pdf/` and `docs/img/`:

    docs/pdf/SUBMISSION.pdf       the one-page summary
    docs/pdf/README.pdf           setup and usage
    docs/pdf/DESIGN_NOTES.pdf     reasoning, trade-offs, AI disclosure
    docs/pdf/RUN_REPORT.pdf       execution walkthrough with captured output
    docs/pdf/dashboard.pdf        the dashboard itself, printed
    docs/img/dashboard-*.png      section screenshots for embedding

Rendering goes through headless Chrome (or Edge), because both ship on every
machine this needs to run on and neither needs a 150 MB toolchain installed to
turn one HTML file into one PDF. `--virtual-time-budget` gives Chart.js time to
draw before the page is captured; without it the charts print blank.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PDF_DIR = DOCS / "pdf"
IMG_DIR = DOCS / "img"

#: Order matters: this is the order an evaluator should read them in.
DOCUMENTS = ["SUBMISSION.md", "README.md", "DESIGN_NOTES.md", "RUN_REPORT.md"]

BROWSERS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


def find_browser() -> Path:
    for candidate in BROWSERS:
        if candidate.is_file():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise SystemExit(
        "No Chrome or Edge found. Install one, or open the HTML files and use "
        "the browser's Print -> Save as PDF."
    )


# --------------------------------------------------------------------------
# Markdown -> print-ready HTML
# --------------------------------------------------------------------------

PAGE_CSS = """
@page { size: A4; margin: 16mm 14mm; }
* { box-sizing: border-box; }
body {
  font: 10.5pt/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #14171f; max-width: 100%; margin: 0; padding: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1 { font-size: 20pt; letter-spacing: -0.3pt; margin: 0 0 4pt;
     border-bottom: 2px solid #1f4e79; padding-bottom: 5pt; color: #1f4e79; }
h2 { font-size: 14pt; margin: 16pt 0 5pt; color: #1f4e79;
     border-bottom: 1px solid #e3e6ec; padding-bottom: 3pt;
     break-after: avoid; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 11pt 0 3pt; break-after: avoid; page-break-after: avoid; }
h4 { font-size: 10.5pt; margin: 9pt 0 2pt; color: #4a5261; }
p, li { orphans: 3; widows: 3; }
ul, ol { padding-left: 18pt; }
li { margin-bottom: 2pt; }
code { background: #f0f2f5; padding: 0.5pt 3pt; border-radius: 3px;
       font-family: "Cascadia Mono", Consolas, monospace; font-size: 9pt; }
pre { background: #f6f7f9; border: 1px solid #e3e6ec; border-radius: 5px;
      padding: 7pt 9pt; overflow-x: auto; break-inside: avoid; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8.5pt; line-height: 1.4; }
table { border-collapse: collapse; width: 100%; margin: 7pt 0; font-size: 9pt;
        break-inside: auto; }
th, td { border: 1px solid #e3e6ec; padding: 4pt 6pt; text-align: left;
         vertical-align: top; }
th { background: #f0f3f7; font-weight: 600; font-size: 8.5pt;
     text-transform: uppercase; letter-spacing: 0.3pt; color: #4a5261; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
blockquote { border-left: 3px solid #1f4e79; margin: 7pt 0; padding: 3pt 0 3pt 11pt;
             color: #4a5261; background: #f8fafc; }
a { color: #1f4e79; text-decoration: none; }
hr { border: none; border-top: 1px solid #e3e6ec; margin: 14pt 0; }
img { max-width: 100%; break-inside: avoid; page-break-inside: avoid;
      border: 1px solid #e3e6ec; border-radius: 4px; }
strong { color: #14171f; }
.footer { margin-top: 18pt; padding-top: 6pt; border-top: 1px solid #e3e6ec;
          font-size: 8pt; color: #78808f; }
"""

HTML_SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title><style>{css}</style></head>
<body>{body}
<div class="footer">{footer}</div>
</body></html>
"""


def render_markdown(path: Path) -> str:
    """One Markdown file into a self-contained, print-styled HTML page."""
    text = path.read_text(encoding="utf-8")

    # Cross-links between the write-ups point at their PDF siblings.
    for name in DOCUMENTS:
        text = text.replace(f"]({name})", f"]({name[:-3]}.pdf)")

    # Images must become absolute file URIs. The page is rendered from a
    # temporary directory, so any relative path would resolve against the temp
    # folder and the image would silently vanish from the PDF.
    for image in (sorted(IMG_DIR.glob("*.png")) if IMG_DIR.is_dir() else []):
        text = text.replace(f"](docs/img/{image.name})", f"]({image.resolve().as_uri()})")

    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "codehilite"],
        extension_configs={"codehilite": {"noclasses": True, "pygments_style": "friendly"}},
    )
    return HTML_SHELL.format(
        title=path.stem.replace("_", " ").title(),
        css=PAGE_CSS,
        body=body,
        footer=(
            f"{path.name} &middot; GitHub Organization Security &amp; Access-Hygiene "
            f"Scanner &middot; generated by tools/make_docs.py"
        ),
    )


# --------------------------------------------------------------------------
# Headless browser
# --------------------------------------------------------------------------

def _run(browser: Path, args: list[str], label: str) -> bool:
    command = [
        str(browser), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--disable-extensions", "--run-all-compositor-stages-before-draw",
        *args,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        print(f"    ! {label} failed: {(result.stderr or '').strip()[:200]}")
        return False
    return True


def html_to_pdf(browser: Path, html: Path, pdf: Path, *, budget: int = 4000) -> bool:
    pdf.parent.mkdir(parents=True, exist_ok=True)
    ok = _run(
        browser,
        [
            "--no-pdf-header-footer",
            f"--virtual-time-budget={budget}",
            f"--print-to-pdf={pdf}",
            html.resolve().as_uri(),
        ],
        f"PDF {pdf.name}",
    )
    if ok and pdf.is_file():
        print(f"    {pdf.relative_to(ROOT)}  ({pdf.stat().st_size / 1024:.0f} KB)")
        return True
    return False


def html_to_png(
    browser: Path, html: Path, png: Path, *, width: int, height: int, budget: int = 4000
) -> bool:
    png.parent.mkdir(parents=True, exist_ok=True)
    ok = _run(
        browser,
        [
            f"--virtual-time-budget={budget}",
            f"--window-size={width},{height}",
            f"--screenshot={png}",
            html.resolve().as_uri(),
        ],
        f"PNG {png.name}",
    )
    if ok and png.is_file():
        print(f"    {png.relative_to(ROOT)}  ({png.stat().st_size / 1024:.0f} KB)")
        return True
    return False


# --------------------------------------------------------------------------
# Dashboard snapshots
# --------------------------------------------------------------------------

#: Each screenshot isolates one dashboard section by hiding the others, so the
#: images are legible in a document instead of being one unreadable strip.
SECTION_SHOTS = [
    ("overview", "Headline summary and KPI tiles - the first thing a reader sees",
     1400, 500),
    ("advisories", "Section 1 - security advisories by severity, status and age",
     1500, 1500),
    ("access", "Section 2 - per-repo member table with contribution scores",
     1500, 1250),
    ("suggestions", "Section 3 - suggested removals, ranked by risk, with evidence",
     1800, 1250),
    ("exclusions", "Section 4 - everyone excluded from the suggestions, and why",
     1400, 900),
]


#: The live organization has far less content than the fixture, so the same
#: heights leave half a page of grey. Overrides keep both sets tight.
SIZE_OVERRIDES: dict[str, dict[str, tuple[int, int]]] = {
    "live": {
        "overview": (1400, 500),
        "advisories": (1500, 1500),
        "access": (1500, 1500),
        "suggestions": (1800, 640),
        "exclusions": (1400, 1250),
    },
}


def section_screenshots(
    browser: Path, dashboard: Path, prefix: str = "dashboard"
) -> list[tuple[str, str]]:
    """Screenshot each dashboard section separately, via a temporary copy.

    `prefix` keeps the demo and live sets apart in docs/img/ so a rebuild of
    one never silently overwrites the other.
    """
    html = dashboard.read_text(encoding="utf-8")
    captured: list[tuple[str, str]] = []

    scripts = {
        "overview": "hideFrom(0);",
        "advisories": "keepSection(1);",
        "access": "keepSection(2); openRepos(1);",
        "suggestions": "keepSection(3);",
        "exclusions": "keepSection(4);",
    }

    helper = """
<script>
window.addEventListener('load', function () {
  var heads = Array.from(document.querySelectorAll('h2'));
  function nodesOf(i) {
    var out = [], n = heads[i];
    while (n) { out.push(n); n = n.nextElementSibling;
      if (n && n.tagName === 'H2') break; }
    return out;
  }
  window.hideFrom = function (i) {
    for (var k = i; k < heads.length; k++) nodesOf(k).forEach(function (n) {
      n.style.display = 'none'; });
    var f = document.querySelector('footer'); if (f) f.style.display = 'none';
  };
  window.keepSection = function (num) {
    var card = document.querySelector('.bottom-line');
    if (card) card.style.display = 'none';
    var k = document.querySelector('.kpis'); if (k) k.style.display = 'none';
    var g = document.querySelector('.gap-warning'); if (g) g.style.display = 'none';
    for (var i = 0; i < heads.length; i++)
      if (i !== num - 1) nodesOf(i).forEach(function (n) { n.style.display = 'none'; });
    var f = document.querySelector('footer'); if (f) f.style.display = 'none';
  };
  window.openRepos = function (limit) {
    var d = document.querySelectorAll('details.repo');
    d.forEach(function (el, i) { if (i >= limit) el.style.display = 'none';
                                 else el.open = true; });
  };
  __SHOT__
});
</script>
"""

    with tempfile.TemporaryDirectory() as tmp:
        for key, caption, width, height in SECTION_SHOTS:
            width, height = SIZE_OVERRIDES.get(prefix, {}).get(key, (width, height))
            injected = helper.replace("__SHOT__", scripts[key])
            page = Path(tmp) / f"{key}.html"
            page.write_text(html.replace("</body>", injected + "</body>"), encoding="utf-8")
            png = IMG_DIR / f"{prefix}-{key}.png"
            if html_to_png(browser, page, png, width=width, height=height):
                captured.append((f"docs/img/{png.name}", caption))

    return captured


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--only", help="build a single document, e.g. RUN_REPORT.md")
    args = parser.parse_args(argv)

    browser = find_browser()
    print(f"Renderer: {browser.name}")

    demo = DOCS / "demo_dashboard.html"
    if not demo.is_file():
        print(f"! {demo} missing - run `python main.py --demo` first, then copy "
              f"out/dashboard.html into docs/.")
        return 1

    # (source, screenshot prefix, pdf name). The live dashboard is optional: it
    # exists only once a real scan has been copied in, and it is the one
    # artifact here that carries real organization data.
    dashboards = [(demo, "dashboard", "dashboard.pdf")]
    live = DOCS / "live" / "dashboard.html"
    if live.is_file():
        dashboards.append((live, "live", "live_dashboard.pdf"))

    if not args.skip_images:
        for source, prefix, pdf_name in dashboards:
            print(f"\nSnapshots of {source.relative_to(ROOT)}:")
            section_screenshots(browser, source, prefix)
            html_to_pdf(browser, source, PDF_DIR / pdf_name, budget=6000)

    print("\nWrite-ups:")
    targets = [args.only] if args.only else DOCUMENTS
    with tempfile.TemporaryDirectory() as tmp:
        for name in targets:
            source = ROOT / name
            if not source.is_file():
                print(f"    - {name} not found, skipped")
                continue
            page = Path(tmp) / f"{source.stem}.html"
            page.write_text(render_markdown(source), encoding="utf-8")
            html_to_pdf(browser, page, PDF_DIR / f"{source.stem}.pdf")

    print(f"\nBundle in {PDF_DIR.relative_to(ROOT)} and {IMG_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
