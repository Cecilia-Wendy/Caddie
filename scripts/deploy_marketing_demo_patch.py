#!/usr/bin/env python3
"""Install the native marketing-demo renderer into an existing public Demo tree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


STANDALONE_STYLES = """  #marketingDemoRoot{display:none}
  body.marketing-demo-mode{height:auto;min-height:100vh;overflow:auto;background:#f2f5fa}
  body.marketing-demo-mode>.app,body.marketing-demo-mode>.app-splash,body.marketing-demo-mode>.public-demo-bar,body.marketing-demo-mode>.public-demo-mobile{display:none!important}
  body.marketing-demo-mode #marketingDemoRoot{display:block;min-height:100vh;padding:24px}
  body.marketing-demo-mode .caddie-demo-section{width:min(1180px,100%);margin:0 auto;background:transparent;box-shadow:none}
  body.marketing-demo-mode .caddie-demo-section-head{padding:0 2px}
  @media(max-width:820px){body.marketing-demo-mode #marketingDemoRoot{padding:0}body.marketing-demo-mode .caddie-demo-section{border-radius:0}}
"""

MARKETING_ROUTE = '''

@app.get("/marketing-demo", response_class=HTMLResponse)
def marketing_demo():
    """Render the native Caddie UI as a controlled, API-free website demo."""
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")
'''


def replace_between(text: str, start: str, end: str, replacement: str) -> str:
    left = text.find(start)
    right = text.find(end, left)
    if left < 0 or right < 0:
        raise RuntimeError(f"Missing replacement anchors: {start!r}, {end!r}")
    return text[:left] + replacement.rstrip() + "\n" + text[right:]


def install(
    app_dir: Path,
    demo_block_path: Path,
    demo_styles_path: Path | None = None,
    marketing_styles_path: Path | None = None,
) -> None:
    index_path = app_dir / "static" / "index.html"
    server_path = app_dir / "server.py"
    index = index_path.read_text(encoding="utf-8")
    demo_block = demo_block_path.read_text(encoding="utf-8")
    demo_styles = demo_styles_path.read_text(encoding="utf-8") if demo_styles_path else ""
    marketing_styles = marketing_styles_path.read_text(encoding="utf-8") if marketing_styles_path else ""

    if "const CADDIE_DEMO_STAGES=" in index:
        index = replace_between(
            index,
            "const CADDIE_DEMO_STAGES=",
            "async function renderGuide(v){",
            demo_block,
        )
    elif "async function renderGuide(v){" in index:
        index = index.replace(
            "async function renderGuide(v){",
            demo_block.rstrip() + "\nasync function renderGuide(v){",
            1,
        )
    else:
        raise RuntimeError("Could not locate the guide renderer anchor")
    if "#marketingDemoRoot{display:none}" not in index:
        index = index.replace(
            "  .guide-entry-rail{",
            STANDALONE_STYLES + "  .guide-entry-rail{",
            1,
        )
    if demo_styles and ".caddie-demo-brand img{" not in index:
        style_anchor = "  #marketingDemoRoot{display:none}"
        if style_anchor not in index:
            raise RuntimeError("Could not locate the marketing demo style anchor")
        index = index.replace(style_anchor, demo_styles.rstrip() + "\n" + style_anchor, 1)
    if marketing_styles:
        index = replace_between(
            index,
            "  #marketingDemoRoot{display:none}",
            "  .guide-entry-rail{",
            marketing_styles,
        )
    if 'id="marketingDemoRoot"' not in index:
        index = index.replace(
            "<body>\n",
            '<body>\n<main id="marketingDemoRoot" aria-label="Caddie 产品演示"></main>\n',
            1,
        )
    if "if(initMarketingDemo())return" not in index:
        index, count = re.subn(
            r"\(async\(\)=>\{\s*await initPublicDemo\(\);",
            "(async()=>{ if(initMarketingDemo())return;await initPublicDemo();",
            index,
            count=1,
        )
        if count != 1:
            index, count = re.subn(
                r"\(async\(\)=>\{\s*await refreshProviders\(\);",
                "(async()=>{ if(initMarketingDemo())return;await refreshProviders();",
                index,
                count=1,
            )
        if count != 1:
            raise RuntimeError("Could not patch the Caddie boot sequence")
    demo_targets = {
        '<button class="btn" onclick="addExperience()">＋ 新增经历</button>':
            '<button class="btn" data-demo-target="add-experience" onclick="addExperience()">＋ 新增经历</button>',
        '<button class="btn ghost sm" onclick="addProject(${exp.id})">＋ 项目</button>':
            '<button class="btn ghost sm" data-demo-target="add-project" onclick="addProject(${exp.id})">＋ 项目</button>',
        '<button class="btn sm" onclick="openFollowupForm()">＋ 加一条</button>':
            '<button class="btn sm" data-demo-target="add-followup" onclick="openFollowupForm()">＋ 加一条</button>',
    }
    for original, patched in demo_targets.items():
        if patched not in index:
            if original not in index:
                raise RuntimeError(f"Could not locate native Demo control: {original}")
            index = index.replace(original, patched, 1)
    index_path.write_text(index, encoding="utf-8")

    server = server_path.read_text(encoding="utf-8")
    if '@app.get("/marketing-demo"' not in server:
        anchor = '''@app.get("/health")
def health():'''
        if anchor not in server:
            raise RuntimeError("Could not locate the health route anchor")
        server = server.replace(anchor, MARKETING_ROUTE + "\n\n" + anchor, 1)
        server_path.write_text(server, encoding="utf-8")

    print("MARKETING_DEMO_PATCHED")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", default="/opt/caddie-app")
    parser.add_argument("--demo-block", required=True)
    parser.add_argument("--demo-styles")
    parser.add_argument("--marketing-styles")
    args = parser.parse_args()
    install(
        Path(args.app_dir),
        Path(args.demo_block),
        Path(args.demo_styles) if args.demo_styles else None,
        Path(args.marketing_styles) if args.marketing_styles else None,
    )


if __name__ == "__main__":
    main()
