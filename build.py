#!/usr/bin/env python3
"""ActorForge static site generator.

Regenerates the whole site from the public Apify Store API plus the
hand-written editorial files in content/.  Standard library only.

    python build.py            # fetch live data from Apify and build
    python build.py --offline  # build from .cache/ (last fetched data)
    python build.py --check    # validate the generated site only

New public actors get a page automatically (from their README) even before
an editorial file exists in content/actors/.
"""
import datetime as _dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(ROOT, "content")
CACHE = os.path.join(ROOT, ".cache")
STATE = os.path.join(ROOT, ".build")

USERNAME = "actor-forge"
BASE_URL = "https://devtaonas.github.io/actorforge-site/"
BASE_PATH = "/actorforge-site/"
STORE_URL = "https://apify.com/" + USERNAME
API = "https://api.apify.com/v2"
INDEXNOW_KEY = "34fe8e4ca4aa7d818ce3141830ef220b"
BRAND = "ActorForge"

CATEGORIES = [
    ("business-leads", "Business Leads & Company Data",
     "B2B lead lists, company registers, funding rounds, hiring signals and planning permissions, from official and open sources."),
    ("documents-ai", "Documents, OCR & AI-Ready Text",
     "Turn PDFs, scans, images and web pages into clean text and Markdown for search, RAG pipelines and AI agents."),
    ("seo-marketing", "SEO & Marketing Intelligence",
     "Technical SEO audits, keyword ideas, Google Trends, competitor ads, archived page history and trademark checks."),
    ("ecommerce-apps", "E-commerce & App Stores",
     "Product catalogues from Shopify stores and app data, charts and reviews from the App Store and Google Play."),
    ("video-social", "YouTube & Video Data",
     "Videos, channels, comments and search results from the official YouTube Data API, using your own free API key."),
    ("china-trends", "China Social Trends",
     "Real-time hot-search lists from Weibo, Douyin and Baidu, plus Bilibili videos and comments, for trend monitoring and market research on China."),
    ("more", "More Data Tools", "Other ActorForge data tools."),
]
CAT = {c[0]: c for c in CATEGORIES}
APIFY_CAT_MAP = [
    ("VIDEOS", "video-social"), ("ECOMMERCE", "ecommerce-apps"), ("SEO_TOOLS", "seo-marketing"),
    ("MARKETING", "seo-marketing"), ("LEAD_GENERATION", "business-leads"), ("BUSINESS", "business-leads"),
    ("JOBS", "business-leads"), ("REAL_ESTATE", "business-leads"), ("AI", "documents-ai"),
]


# --------------------------------------------------------------------------- data

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "actorforge-site-builder"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def load_data(offline):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "data.json")
    if offline:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    items, offset = [], 0
    while True:
        page = fetch_json(f"{API}/store?username={USERNAME}&limit=100&offset={offset}")["data"]
        items += page["items"]
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            break
    actors = []
    for it in items:
        if it.get("username") != USERNAME:
            continue
        build = {}
        try:
            build = fetch_json(f"{API}/acts/{USERNAME}~{it['name']}/builds/default")["data"]
        except Exception as e:  # README is optional: fall back to store text
            print(f"  ! no build for {it['name']}: {e}", file=sys.stderr)
        ad = build.get("actorDefinition") or {}
        input_schema = ad.get("input")
        if not input_schema and build.get("inputSchema"):
            try:
                input_schema = json.loads(build["inputSchema"])
            except ValueError:
                input_schema = None
        actors.append({
            "name": it["name"], "title": it["title"], "description": it.get("description") or "",
            "categories": it.get("categories") or [], "pricing": it.get("currentPricingInfo") or {},
            "readme": build.get("readme") or ad.get("readme") or "", "input": input_schema or {},
        })
    actors.sort(key=lambda a: a["name"])
    data = {"actors": actors}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data


def load_content(kind):
    out = {}
    d = os.path.join(CONTENT, kind)
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    out[fn[:-5]] = json.load(f)
    return out


# --------------------------------------------------------------------------- README parsing

def readme_sections(md):
    """Return (lead paragraphs, {heading: body}) for a README."""
    lead, sections, cur = [], {}, None
    for line in md.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            sections[cur] = []
        elif line.startswith("# "):
            continue
        elif cur is None:
            lead.append(line)
        else:
            sections[cur].append(line)
    paras = [p.strip() for p in "\n".join(lead).split("\n\n") if p.strip()]
    return paras, {k: "\n".join(v).strip() for k, v in sections.items()}


def find_section(sections, *needles):
    for k, v in sections.items():
        if any(n.lower() in k.lower() for n in needles):
            return v
    return ""


def md_table(text):
    """First markdown table in text -> (header, rows)."""
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", s[1:-1])]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            rows.append([c.replace("\\|", "|") for c in cells])
        elif rows:
            break
    if not rows:
        return None, []
    return rows[0], rows[1:]


def md_bullets(text):
    return [l.strip()[2:].strip() for l in text.splitlines() if l.strip().startswith(("- ", "* "))]


# --------------------------------------------------------------------------- inline markup

class Ctx:
    """Rendering context for the current page (for relative links)."""
    page = ""
    actor_names = set()
    guide_slugs = set()


def rel(target):
    depth = len([p for p in Ctx.page.split("/") if p])
    return ("../" * depth + target) or "./"


def resolve_link(target):
    if target.startswith("actor:"):
        n = target[6:]
        if n in Ctx.actor_names:
            return rel(f"actors/{n}/"), False
        return f"{STORE_URL}/{n}", True
    if target.startswith("guide:"):
        s = target[6:]
        if s not in Ctx.guide_slugs:
            raise ValueError(f"unknown guide link {target} on {Ctx.page}")
        return rel(f"guides/{s}/"), False
    m = re.fullmatch(r"https://apify\.com/actor-forge/([a-z0-9-]+)/?", target)
    if m and m.group(1) in Ctx.actor_names:
        return rel(f"actors/{m.group(1)}/"), False
    return target, True


def inline(text):
    """Very small Markdown-inline renderer: `code`, **bold**, [text](url)."""
    out, pos = [], 0
    pat = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)")
    for m in pat.finditer(text):
        out.append(html.escape(text[pos:m.start()], quote=False))
        if m.group(1) is not None:
            out.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        elif m.group(2) is not None:
            out.append(f"<strong>{inline(m.group(2))}</strong>")
        else:
            href, ext = resolve_link(m.group(4))
            attrs = ' rel="noopener"' if ext else ""
            out.append(f'<a href="{html.escape(href)}"{attrs}>{inline(m.group(3))}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def plain(text):
    """Strip inline markup for meta tags / JSON-LD."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"`|\*\*", "", text)


def esc(s):
    return html.escape(str(s), quote=True)


def clip(s, n):
    s = re.sub(r"\s+", " ", plain(s)).strip()
    if len(s) <= n:
        return s
    cut = s[: n - 1].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"


# --------------------------------------------------------------------------- pricing

def money(x):
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    if "." in s and len(s.split(".")[1]) == 1:
        s += "0"
    return "$" + s


def pricing_rows(actor):
    p = actor["pricing"]
    model = p.get("pricingModel")
    rows = []
    if model == "PAY_PER_EVENT":
        events = p.get("pricingPerEvent", {}).get("actorChargeEvents", {})
        for key, e in events.items():
            price = e.get("eventPriceUsd")
            if price is None:
                continue
            rows.append({"key": key, "title": e.get("eventTitle") or key,
                         "desc": e.get("eventDescription") or "", "price": price,
                         "primary": bool(e.get("isPrimaryEvent")), "start": key == "apify-actor-start"})
        rows.sort(key=lambda r: (r["start"], not r["primary"]))
    elif model == "PRICE_PER_DATASET_ITEM":
        price = (p.get("pricePerUnitUsd") or 0)
        rows.append({"key": "result", "title": "Result", "desc": "Per dataset item", "price": price,
                     "primary": True, "start": False})
    return model, rows


def headline_price(actor):
    model, rows = pricing_rows(actor)
    main = [r for r in rows if not r["start"]]
    if not main:
        return "Free to try" if model in (None, "FREE") else ""
    r = main[0]
    return f"{money(r['price'] * 1000)} per 1,000 · {r['title'].lower()}"


# --------------------------------------------------------------------------- HTML building blocks

CSS = """
:root{--navy:#14213d;--navy2:#0a1628;--orange:#c2410c;--orange2:#f26430;--ink:#1b2430;--muted:#4f5967;--line:#e2e6ed;--bg2:#f6f8fb;--code:#0f172a}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);background:#fff}
a{color:#1d4ed8}a:hover{color:var(--orange)}
:focus-visible{outline:3px solid var(--orange2);outline-offset:2px}
.skip{position:absolute;left:-999px;top:0;background:#fff;padding:8px 12px;z-index:9}.skip:focus{left:8px}
.wrap{max-width:960px;margin:0 auto;padding:0 20px}
.top{background:var(--navy2);color:#fff}
.top .wrap{display:flex;flex-wrap:wrap;align-items:center;gap:6px 20px;padding-top:12px;padding-bottom:12px}
.logo{color:#fff;font-weight:800;font-size:1.15rem;text-decoration:none;letter-spacing:-.01em}.logo span{color:var(--orange2)}
.top nav{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:.95rem}.top nav a{color:#dbe3ef;text-decoration:none}.top nav a:hover{color:#fff;text-decoration:underline}
.hero{background:linear-gradient(180deg,var(--navy),var(--navy2));color:#fff;padding:40px 0 36px}
.hero h1{font-size:clamp(1.6rem,4vw,2.3rem);line-height:1.2;margin:0 0 12px;letter-spacing:-.02em}
.hero p{color:#cfd8e6;max-width:44em;margin:0 0 8px;font-size:1.08rem}
.crumbs{font-size:.85rem;color:#aab6c8;margin-bottom:14px}.crumbs a{color:#cfd8e6}.crumbs ol{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:4px}.crumbs li+li::before{content:"›";margin:0 6px 0 2px}
main{padding:28px 0 16px}
h2{font-size:1.45rem;margin:40px 0 10px;letter-spacing:-.01em;line-height:1.3}
h3{font-size:1.1rem;margin:24px 0 6px}
p,li{max-width:46em}
.muted{color:var(--muted)}
.btn{display:inline-block;background:var(--orange);color:#fff!important;text-decoration:none;font-weight:700;border-radius:8px;padding:11px 20px;margin:10px 10px 0 0}
.btn:hover{background:#9a3412}
.btn.ghost{background:transparent;border:2px solid #fff}
.cta{border:2px solid var(--orange2);background:#fff7f2;border-radius:12px;padding:18px 20px;margin:28px 0}
.cta p{margin:0 0 4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin:14px 0}
.card{border:1px solid var(--line);border-radius:12px;padding:16px 18px;background:#fff}
.card h3{margin:0 0 6px;font-size:1.02rem;line-height:1.35}.card h3 a{text-decoration:none}
.card p{margin:0;color:var(--muted);font-size:.93rem}
.card .price{display:block;margin-top:8px;font-size:.85rem;font-weight:600;color:var(--ink)}
.tablewrap{overflow-x:auto;margin:12px 0;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;vertical-align:top;padding:9px 12px;border-bottom:1px solid var(--line)}
th{background:var(--bg2);font-weight:650}
tr:last-child td{border-bottom:0}
td.cut{max-width:22em;overflow-wrap:anywhere}
code{font:.88em/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--bg2);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
pre{background:var(--code);color:#e2e8f0;padding:14px 16px;border-radius:10px;overflow-x:auto;font:.85rem/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}
pre code{background:none;padding:0;color:inherit;overflow-wrap:normal}
.uc{border-left:4px solid var(--orange2);padding:2px 0 2px 14px;margin:14px 0}
.uc h3{margin:0 0 4px}.uc p{margin:0}
details{border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:10px 0}
summary{font-weight:650;cursor:pointer}
details p{margin:8px 0 2px}
ol.steps{padding-left:0;list-style:none;counter-reset:s}
ol.steps>li{counter-increment:s;position:relative;padding-left:46px;margin:22px 0}
ol.steps>li::before{content:counter(s);position:absolute;left:0;top:0;width:32px;height:32px;border-radius:50%;background:var(--navy);color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center}
ol.steps h3{margin:2px 0 6px}
.tldr{background:var(--bg2);border-radius:10px;padding:14px 18px;margin:0 0 10px}
.pill{display:inline-block;font-size:.78rem;font-weight:650;border:1px solid var(--line);border-radius:999px;padding:2px 10px;margin:0 6px 6px 0;color:var(--muted);text-decoration:none}
footer{border-top:1px solid var(--line);margin-top:48px;padding:22px 0 40px;color:var(--muted);font-size:.88rem}
footer a{color:var(--navy)}
@media (max-width:560px){body{font-size:15.5px}ol.steps>li{padding-left:40px}}
"""


def head(title, desc, path, og_type="website", jsonld=None):
    canon = BASE_URL + path
    ld = ""
    if jsonld:
        ld = '<script type="application/ld+json">' + json.dumps(
            jsonld, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + "</script>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(canon)}">
<meta name="robots" content="index,follow">
<meta property="og:type" content="{og_type}">
<meta property="og:site_name" content="{BRAND}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(canon)}">
<meta name="twitter:card" content="summary">
<meta name="theme-color" content="#14213d">
<style>{CSS.strip()}</style>
{ld}
</head>
"""


def topbar():
    links = [("", "Home"), ("actors/", "All actors"), ("guides/", "Guides")]
    nav = "".join(f'<a href="{rel(p)}">{t}</a>' for p, t in links)
    return (f'<body>\n<a class="skip" href="#main">Skip to content</a>\n<header class="top"><div class="wrap">'
            f'<a class="logo" href="{rel("")}">Actor<span>Forge</span></a><nav aria-label="Main">{nav}'
            f'<a href="{STORE_URL}" rel="noopener">On Apify ↗</a></nav></div></header>\n')


def footer(used_cats):
    cats = " · ".join(f'<a href="{rel("categories/" + c + "/")}">{esc(CAT[c][1])}</a>' for c in used_cats)
    return (f'<footer><div class="wrap"><p>{cats}</p><p><strong>{BRAND}</strong> builds pay-per-result data tools '
            f'on the Apify platform. All actors: <a href="{STORE_URL}" rel="noopener">apify.com/actor-forge</a>. '
            f'{BRAND} is independent and not affiliated with the data sources named on this site.</p>'
            f'<p><a href="{rel("sitemap.xml")}">Sitemap</a></p></div></footer>\n</body>\n</html>\n')


def crumbs_html(items):
    lis = []
    for i, (path, name) in enumerate(items):
        if i == len(items) - 1:
            lis.append(f'<li aria-current="page">{esc(name)}</li>')
        else:
            lis.append(f'<li><a href="{rel(path)}">{esc(name)}</a></li>')
    return f'<nav class="crumbs" aria-label="Breadcrumb"><ol>{"".join(lis)}</ol></nav>'


def crumbs_ld(items):
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": name, "item": BASE_URL + path}
        for i, (path, name) in enumerate(items)]}


def table_html(header, rows, cut_cols=()):
    th = "".join(f'<th scope="col">{inline(h)}</th>' for h in header)
    body = []
    for r in rows:
        tds = []
        for i, c in enumerate(r):
            cls = ' class="cut"' if i in cut_cols else ""
            tds.append(f"<td{cls}>{inline(c)}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def code_block(code, lang=""):
    cls = f' class="language-{esc(lang)}"' if lang else ""
    return f"<pre><code{cls}>{html.escape(code, quote=False)}</code></pre>"


def faq_html(faq):
    return "".join(f"<details><summary>{inline(f['q'])}</summary><p>{inline(f['a'])}</p></details>" for f in faq)


def faq_ld(faq):
    return {"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": plain(f["q"]),
         "acceptedAnswer": {"@type": "Answer", "text": plain(f["a"])}} for f in faq]}


def api_snippets(name, run_input):
    body = json.dumps(run_input, ensure_ascii=False, indent=2)
    url = f"{API}/acts/{USERNAME}~{name}/run-sync-get-dataset-items"
    curl = (f'curl -X POST "{url}" \\\n  -H "Authorization: Bearer $APIFY_TOKEN" \\\n'
            f'  -H "Content-Type: application/json" \\\n  -d \'{json.dumps(run_input, ensure_ascii=False)}\'')
    js = (f"// Node.js 18+ (built-in fetch). Set APIFY_TOKEN in your environment.\n"
          f"const input = {body};\n\n"
          f"const res = await fetch(\n  \"{url}\",\n"
          f"  {{\n    method: \"POST\",\n    headers: {{\n      Authorization: `Bearer ${{process.env.APIFY_TOKEN}}`,\n"
          f"      \"Content-Type\": \"application/json\",\n    }},\n    body: JSON.stringify(input),\n  }}\n);\n"
          f"const items = await res.json();\nconsole.log(items.length, items[0]);")
    pyinput = json.dumps(run_input, ensure_ascii=False, indent=4)
    pyinput = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", pyinput)))
    py = (f"# pip install apify-client   (set APIFY_TOKEN in your environment)\nimport os\n"
          f"from apify_client import ApifyClient\n\nclient = ApifyClient(os.environ[\"APIFY_TOKEN\"])\n"
          f"run_input = {pyinput}\n\nrun = client.actor(\"{USERNAME}/{name}\").call(run_input=run_input)\n"
          f"for item in client.dataset(run[\"defaultDatasetId\"]).iterate_items():\n    print(item)")
    return (f"<h3>cURL</h3>{code_block(curl, 'bash')}<h3>JavaScript (Node.js)</h3>{code_block(js, 'javascript')}"
            f"<h3>Python</h3>{code_block(py, 'python')}"
            f'<p class="muted">The <code>run-sync-get-dataset-items</code> endpoint waits for the run (up to 5 minutes) '
            f"and returns the dataset rows directly. For longer runs, start the run asynchronously and read the "
            f"dataset when it finishes, as the Python example does. Replace <code>APIFY_TOKEN</code> with your own "
            f'token from the Apify console.</p>')


def auto_input(actor):
    props = (actor["input"] or {}).get("properties") or {}
    req = set((actor["input"] or {}).get("required") or [])
    out = {}
    for k, v in props.items():
        if "prefill" in v:
            out[k] = v["prefill"]
        elif k in req and "default" in v:
            out[k] = v["default"]
    return out


def input_fields_table(actor):
    props = (actor["input"] or {}).get("properties") or {}
    if not props:
        return ""
    req = set((actor["input"] or {}).get("required") or [])
    rows = []
    for k, v in props.items():
        if v.get("editor") == "hidden":
            continue
        d = v.get("default")
        dflt = "" if d is None else "`" + json.dumps(d, ensure_ascii=False)[:60] + "`"
        rows.append([f"`{k}`", v.get("type", ""), "yes" if k in req else "no", dflt,
                     clip(v.get("description") or v.get("title") or "", 220)])
    return table_html(["Field", "Type", "Required", "Default", "What it does"], rows, cut_cols=(4,))


def output_sample(actor):
    _, secs = readme_sections(actor["readme"])
    text = find_section(secs, "What data does it return", "Output")
    header, rows = md_table(text)
    if not header:
        return ""
    rows = [[clip(c, 160) for c in r] for r in rows[:4]]
    return table_html(header, rows, cut_cols=tuple(range(len(header))))


def readme_worked_examples(actor):
    _, secs = readme_sections(actor["readme"])
    header, rows = md_table(find_section(secs, "How much does it cost", "Pricing"))
    out = {}
    if header and rows:
        hl = [h.lower() for h in header]
        wi = next((i for i, h in enumerate(hl) if "example" in h), None)
        for r in rows:
            key = r[0].strip("` ")
            if wi is not None and wi < len(r):
                out[key] = r[wi]
    return out


def pricing_table(actor):
    model, rows = pricing_rows(actor)
    if not rows:
        return f'<p>Pricing is shown on the <a href="{STORE_URL}/{actor["name"]}" rel="noopener">Apify listing</a>.</p>'
    ex = readme_worked_examples(actor)
    body = []
    for r in rows:
        per1k = "—" if r["start"] else money(r["price"] * 1000)
        body.append([r["title"] + (" (platform start fee)" if r["start"] else ""), r["desc"],
                     money(r["price"]), per1k, ex.get(r["key"], "")])
    return table_html(["Charged for", "When", "Price", "Per 1,000", "Worked example"], body, cut_cols=(1, 4))


# --------------------------------------------------------------------------- pages

def actor_category(actor, ed):
    if ed.get("category") in CAT:
        return ed["category"]
    for ac, c in APIFY_CAT_MAP:
        if ac in actor["categories"]:
            return c
    return "more"


def card_actor(a):
    return (f'<div class="card"><h3><a href="{rel("actors/" + a["name"] + "/")}">{esc(a["title"])}</a></h3>'
            f'<p>{esc(clip(a["description"], 150))}</p><span class="price">{esc(headline_price(a))}</span></div>')


def card_guide(g):
    return (f'<div class="card"><h3><a href="{rel("guides/" + g["slug"] + "/")}">{inline(g["title"])}</a></h3>'
            f'<p>{esc(clip(g["summary"], 150))}</p></div>')


def render_actor(a, ed, ctx):
    name = a["name"]
    path = f"actors/{name}/"
    Ctx.page = path
    cat = ctx["actor_cat"][name]
    lead, secs = readme_sections(a["readme"])
    h1 = ed.get("h1") or a["title"]
    title = ed.get("metaTitle") or clip(a["title"], 60)
    desc = ed.get("metaDescription") or clip(a["description"], 160)
    run_url = f"{STORE_URL}/{name}"
    items = [("", "Home"), (f"categories/{cat}/", CAT[cat][1]), (path, a["title"])]

    model, prows = pricing_rows(a)
    offers = [{"@type": "Offer", "name": r["title"], "description": r["desc"], "price": f"{r['price']:.5f}".rstrip("0"),
               "priceCurrency": "USD", "url": run_url} for r in prows if not r["start"]]
    app = {"@type": "SoftwareApplication", "name": a["title"], "description": plain(desc),
           "applicationCategory": "BusinessApplication", "applicationSubCategory": CAT[cat][1],
           "operatingSystem": "Web (Apify platform)", "url": BASE_URL + path, "sameAs": run_url,
           "publisher": {"@type": "Organization", "name": BRAND, "url": BASE_URL}}
    if offers:
        app["offers"] = offers
    graph = [app, crumbs_ld(items)]
    faq = ed.get("faq") or []
    if faq:
        graph.append(faq_ld(faq))

    parts = [head(title, desc, path, "website", {"@context": "https://schema.org", "@graph": graph}), topbar()]
    tagline = ed.get("tagline") or a["description"]
    parts.append(f'<div class="hero"><div class="wrap">{crumbs_html(items)}<h1>{inline(h1)}</h1>'
                 f'<p>{inline(tagline)}</p>'
                 f'<a class="btn" href="{run_url}" rel="noopener">Run on Apify →</a>'
                 f'<a class="btn ghost" href="#pricing">See pricing</a></div></div>\n')
    parts.append('<main id="main"><div class="wrap">')

    intro = ed.get("intro") or lead[1:3] or [a["description"]]
    parts.append("<h2>What it does</h2>" + "".join(f"<p>{inline(p)}</p>" for p in intro))
    if ed.get("howItWorks"):
        parts.append("<h3>How it works</h3><ol>" + "".join(f"<li>{inline(s)}</li>" for s in ed["howItWorks"]) + "</ol>")

    run_input = ed.get("inputExample") or auto_input(a)
    parts.append("<h2>Input example</h2><p>A typical run input (JSON). Every field is also available as a form in the Apify console.</p>")
    parts.append(code_block(json.dumps(run_input, ensure_ascii=False, indent=2), "json"))
    fields = input_fields_table(a)
    if fields:
        parts.append(f"<details><summary>All input fields</summary>{fields}</details>")

    sample = output_sample(a)
    if sample:
        parts.append("<h2>Output example</h2><p>" + inline(ed.get("outputNote") or
                     "Sample rows from a real run (long text shortened). Export as JSON, CSV, Excel or via the API.") + "</p>" + sample)

    parts.append(f'<h2 id="pricing">Pricing</h2><p>Pay per event: you are charged only for the results you receive, '
                 f'with no subscription. Set a maximum total charge on any run and it stops cleanly at the cap.</p>'
                 f'{pricing_table(a)}<p class="muted">Prices are read from the live Apify Store listing when this page '
                 f'is built. The listing is always authoritative.</p>')

    cta = (f'<div class="cta"><p><strong>Try {esc(a["title"].split(" — ")[0])} on Apify.</strong> '
           f'Pay only for the results you get.</p><a class="btn" href="{run_url}" rel="noopener">Run on Apify →</a></div>')
    parts.append(cta)

    if ed.get("useCases"):
        parts.append("<h2>Use cases</h2>" + "".join(
            f'<div class="uc"><h3>{inline(u["title"])}</h3><p>{inline(u["text"])}</p></div>' for u in ed["useCases"]))

    comp = ed.get("comparison")
    if comp and comp.get("rows"):
        parts.append("<h2>How it compares</h2>" + table_html(comp["columns"], comp["rows"]))
        if ed.get("comparisonNote"):
            parts.append(f'<p class="muted">{inline(ed["comparisonNote"])}</p>')

    limits = md_bullets(find_section(secs, "Limits"))
    if limits:
        parts.append("<h2>Limits and honest notes</h2><ul>" + "".join(f"<li>{inline(l)}</li>" for l in limits) + "</ul>")
    lic = find_section(secs, "licence", "license", "attribution")
    if lic:
        parts.append("<h2>Data licence and attribution</h2>" + "".join(
            f"<p>{inline(p)}</p>" for p in lic.split("\n\n") if p.strip()))

    if faq:
        parts.append("<h2>FAQ</h2>" + faq_html(faq))

    parts.append(f"<h2>Use it from code</h2><p>Run the actor through the Apify API with the input above.</p>{api_snippets(name, run_input)}")

    guides = ctx["actor_guides"].get(name, [])
    if guides:
        parts.append("<h2>Step-by-step guides</h2><div class=\"grid\">" + "".join(card_guide(g) for g in guides) + "</div>")
    related = [r for r in (ed.get("related") or []) if r in ctx["actors"] and r != name]
    if not related:
        related = [x for x in ctx["by_cat"].get(cat, []) if x != name][:4]
    if related:
        parts.append("<h2>Related actors</h2><div class=\"grid\">" + "".join(card_actor(ctx["actors"][r]) for r in related) + "</div>")
    parts.append(cta)
    parts.append(f'<p><a href="{rel("categories/" + cat + "/")}">More {esc(CAT[cat][1])} tools</a> · '
                 f'<a href="{rel("actors/")}">All actors</a></p>')
    parts.append("</div></main>\n" + footer(ctx["used_cats"]))
    return path, "".join(parts)


def render_guide(g, ctx):
    slug = g["slug"]
    path = f"guides/{slug}/"
    Ctx.page = path
    a = ctx["actors"][g["actor"]]
    cat = g.get("category") if g.get("category") in CAT else ctx["actor_cat"][a["name"]]
    items = [("", "Home"), ("guides/", "Guides"), (path, plain(g["title"]))]
    run_url = f"{STORE_URL}/{a['name']}"
    steps = g.get("steps") or []
    faq = g.get("faq") or []
    howto = {"@type": "HowTo", "name": plain(g["title"]), "description": plain(g["metaDescription"]),
             "tool": [{"@type": "HowToTool", "name": a["title"]}],
             "step": [{"@type": "HowToStep", "position": i + 1, "name": plain(s["name"]), "text": clip(s["text"], 500),
                       "url": f"{BASE_URL}{path}#step-{i + 1}"} for i, s in enumerate(steps)]}
    article = {"@type": "TechArticle", "headline": clip(g["title"], 110), "description": plain(g["metaDescription"]),
               "url": BASE_URL + path, "mainEntityOfPage": BASE_URL + path,
               "author": {"@type": "Organization", "name": BRAND, "url": BASE_URL},
               "publisher": {"@type": "Organization", "name": BRAND, "url": BASE_URL},
               "about": {"@type": "SoftwareApplication", "name": a["title"], "url": BASE_URL + f"actors/{a['name']}/"}}
    if ctx["lastmod"].get(path):
        article["dateModified"] = ctx["lastmod"][path]
    graph = [article, howto, crumbs_ld(items)]
    if faq:
        graph.append(faq_ld(faq))
    parts = [head(g["metaTitle"], g["metaDescription"], path, "article", {"@context": "https://schema.org", "@graph": graph}), topbar()]
    parts.append(f'<div class="hero"><div class="wrap">{crumbs_html(items)}<h1>{inline(g["title"])}</h1>'
                 f'<p>Uses <a href="{rel("actors/" + a["name"] + "/")}" style="color:#fff">{esc(a["title"])}</a></p></div></div>\n')
    parts.append('<main id="main"><div class="wrap">')
    parts.append(f'<div class="tldr"><strong>Short answer:</strong> {inline(g["summary"])}</div>')
    parts.append("".join(f"<p>{inline(p)}</p>" for p in g.get("intro") or []))
    parts.append("<h2>Step by step</h2><ol class=\"steps\">")
    for i, s in enumerate(steps):
        code = s.get("code")
        cb = code_block(code["code"], code.get("lang", "")) if code and code.get("code") else ""
        paras = "".join(f"<p>{inline(p)}</p>" for p in s["text"].split("\n\n") if p.strip())
        parts.append(f'<li id="step-{i + 1}"><h3>{inline(s["name"])}</h3>{paras}{cb}</li>')
    parts.append("</ol>")
    sample_in = g.get("sampleInput") or auto_input(a)
    parts.append("<h2>Sample input</h2>" + code_block(json.dumps(sample_in, ensure_ascii=False, indent=2), "json"))
    sample = output_sample(a)
    if sample:
        parts.append("<h2>Sample output</h2><p>" + inline(g.get("sampleOutputNote") or "Sample rows from the actor's README.") + "</p>" + sample)
    parts.append(f"<h2>Run it from code</h2>{api_snippets(a['name'], sample_in)}")
    if g.get("costExample"):
        parts.append(f"<h2>What it costs</h2><p>{inline(g['costExample'])}</p>{pricing_table(a)}")
    parts.append(f'<div class="cta"><p><strong>Ready to try it?</strong> {esc(a["title"])} runs on Apify, '
                 f'pay per result.</p><a class="btn" href="{run_url}" rel="noopener">Run on Apify →</a> '
                 f'<a href="{rel("actors/" + a["name"] + "/")}">Read the full actor page</a></div>')
    if g.get("tips"):
        parts.append("<h2>Tips and pitfalls</h2><ul>" + "".join(f"<li>{inline(t)}</li>" for t in g["tips"]) + "</ul>")
    if faq:
        parts.append("<h2>FAQ</h2>" + faq_html(faq))
    tools = [a["name"]] + [x for x in g.get("alsoActors") or [] if x in ctx["actors"] and x != a["name"]]
    parts.append("<h2>Actors used in this guide</h2><div class=\"grid\">" + "".join(card_actor(ctx["actors"][t]) for t in tools) + "</div>")
    rg = [ctx["guides"][s] for s in g.get("related") or [] if s in ctx["guides"] and s != slug]
    if rg:
        parts.append("<h2>Related guides</h2><div class=\"grid\">" + "".join(card_guide(x) for x in rg) + "</div>")
    parts.append(f'<p><a href="{rel("guides/")}">All guides</a></p></div></main>\n' + footer(ctx["used_cats"]))
    return path, "".join(parts)


def render_category(c, ctx):
    slug, name, blurb = CAT[c]
    path = f"categories/{slug}/"
    Ctx.page = path
    items = [("", "Home"), (path, name)]
    actors = [ctx["actors"][n] for n in ctx["by_cat"].get(c, [])]
    guides = [g for g in ctx["guides"].values() if ctx["guide_cat"][g["slug"]] == c]
    title = clip(f"{name} Tools | {BRAND}", 60)
    desc = clip(f"{blurb} {len(actors)} pay-per-result Apify actors by {BRAND}, with step-by-step guides.", 160)
    lst = {"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "url": BASE_URL + f"actors/{a['name']}/", "name": a["title"]}
        for i, a in enumerate(actors)]}
    parts = [head(title, desc, path, "website", {"@context": "https://schema.org", "@graph": [
        {"@type": "CollectionPage", "name": name, "url": BASE_URL + path, "description": blurb}, lst, crumbs_ld(items)]}), topbar()]
    parts.append(f'<div class="hero"><div class="wrap">{crumbs_html(items)}<h1>{esc(name)}</h1><p>{esc(blurb)}</p></div></div>\n')
    parts.append('<main id="main"><div class="wrap"><h2>Actors</h2><div class="grid">' + "".join(card_actor(a) for a in actors) + "</div>")
    if guides:
        parts.append('<h2>Guides</h2><div class="grid">' + "".join(card_guide(g) for g in guides) + "</div>")
    others = " · ".join(f'<a href="{rel("categories/" + o + "/")}">{esc(CAT[o][1])}</a>' for o in ctx["used_cats"] if o != c)
    parts.append(f"<h2>Other categories</h2><p>{others}</p></div></main>\n" + footer(ctx["used_cats"]))
    return path, "".join(parts)


def render_actor_index(ctx):
    path = "actors/"
    Ctx.page = path
    items = [("", "Home"), (path, "All actors")]
    n = len(ctx["actors"])
    title = f"All {n} ActorForge Data Actors on Apify"
    desc = clip(f"Browse all {n} ActorForge actors: business leads, OCR and Markdown conversion, SEO, YouTube, "
                f"e-commerce and China trends. Pay per result on Apify, no subscription.", 160)
    lst = {"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "url": BASE_URL + f"actors/{a}/", "name": ctx["actors"][a]["title"]}
        for i, a in enumerate(sorted(ctx["actors"]))]}
    parts = [head(title, desc, path, "website", {"@context": "https://schema.org", "@graph": [lst, crumbs_ld(items)]}), topbar()]
    parts.append(f'<div class="hero"><div class="wrap">{crumbs_html(items)}<h1>All ActorForge actors</h1>'
                 f'<p>{n} data tools on the Apify platform, grouped by what they are for.</p></div></div>\n<main id="main"><div class="wrap">')
    for c in ctx["used_cats"]:
        parts.append(f'<h2><a href="{rel("categories/" + c + "/")}">{esc(CAT[c][1])}</a></h2><div class="grid">'
                     + "".join(card_actor(ctx["actors"][a]) for a in ctx["by_cat"][c]) + "</div>")
    parts.append("</div></main>\n" + footer(ctx["used_cats"]))
    return path, "".join(parts)


def render_guide_index(ctx):
    path = "guides/"
    Ctx.page = path
    items = [("", "Home"), (path, "Guides")]
    title = "Data Extraction Guides & Tutorials | ActorForge"
    desc = ("Step-by-step guides with sample input, output and API code: business leads, PDF to Markdown, OCR, "
            "Google Trends, trademarks, job boards, ads and more.")
    lst = {"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "url": BASE_URL + f"guides/{g}/", "name": plain(ctx["guides"][g]["title"])}
        for i, g in enumerate(ctx["guides"])]}
    parts = [head(title, desc, path, "website", {"@context": "https://schema.org", "@graph": [lst, crumbs_ld(items)]}), topbar()]
    parts.append(f'<div class="hero"><div class="wrap">{crumbs_html(items)}<h1>Guides and tutorials</h1>'
                 f'<p>Practical walkthroughs for common data jobs, each with a sample input, sample output and '
                 f'copy-paste API code.</p></div></div>\n<main id="main"><div class="wrap">')
    for c in ctx["used_cats"]:
        gs = [g for g in ctx["guides"].values() if ctx["guide_cat"][g["slug"]] == c]
        if gs:
            parts.append(f"<h2>{esc(CAT[c][1])}</h2><div class=\"grid\">" + "".join(card_guide(g) for g in gs) + "</div>")
    parts.append("</div></main>\n" + footer(ctx["used_cats"]))
    return path, "".join(parts)


def render_home(ctx):
    path = ""
    Ctx.page = path
    n = len(ctx["actors"])
    title = "ActorForge — Pay-Per-Result Data Tools on Apify"
    desc = ("Data APIs for leads, OCR, PDF to Markdown, SEO audits, Google Trends, YouTube and more. "
            "Official and open sources, pay only per result, no subscription.")
    graph = [{"@type": "Organization", "name": BRAND, "url": BASE_URL, "sameAs": [STORE_URL]},
             {"@type": "WebSite", "name": BRAND, "url": BASE_URL},
             {"@type": "ItemList", "name": "ActorForge actors", "itemListElement": [
                 {"@type": "ListItem", "position": i + 1, "url": BASE_URL + f"actors/{a}/", "name": ctx["actors"][a]["title"]}
                 for i, a in enumerate(sorted(ctx["actors"]))]}]
    parts = [head(title, desc, path, "website", {"@context": "https://schema.org", "@graph": graph}), topbar()]
    parts.append(f'<div class="hero"><div class="wrap"><h1>Data tools that pay their way: {n} actors, pay per result</h1>'
                 f'<p>{BRAND} builds data extraction and conversion tools for the Apify platform. Each one is built on an '
                 f'official API or an open dataset wherever one exists, prices every result up front, and respects the '
                 f'spending cap you set on a run.</p>'
                 f'<a class="btn" href="{rel("actors/")}">Browse all actors</a>'
                 f'<a class="btn ghost" href="{rel("guides/")}">Read the guides</a></div></div>\n')
    parts.append('<main id="main"><div class="wrap">')
    parts.append("<h2>What is ActorForge?</h2><p>An Apify actor is a cloud program you run from the Apify console, on a "
                 "schedule, or through an API call. You give it an input (URLs, keywords, a city, a company number) and it "
                 "returns a dataset you can download as JSON, CSV or Excel, or pipe into Make, n8n, Zapier or your own code.</p>"
                 f"<p>{BRAND} actors are priced per event: you pay for each row or page delivered, at the price printed on the "
                 "listing, with no monthly subscription. Failed lookups are not charged, and every actor stops cleanly when it "
                 "reaches the maximum charge you set for a run. Every actor is checked by an automated nightly self-test.</p>")
    parts.append("<h2>Browse by category</h2><div class=\"grid\">")
    for c in ctx["used_cats"]:
        parts.append(f'<div class="card"><h3><a href="{rel("categories/" + c + "/")}">{esc(CAT[c][1])}</a></h3>'
                     f'<p>{esc(CAT[c][2])}</p><span class="price">{len(ctx["by_cat"][c])} actors</span></div>')
    parts.append("</div><h2>Popular guides</h2><div class=\"grid\">" + "".join(card_guide(g) for g in ctx["guides"].values()) + "</div>")
    parts.append("<h2>All actors</h2>")
    for c in ctx["used_cats"]:
        parts.append(f'<h3>{esc(CAT[c][1])}</h3><div class="grid">' + "".join(card_actor(ctx["actors"][a]) for a in ctx["by_cat"][c]) + "</div>")
    parts.append(f'<div class="cta"><p><strong>Every actor lives on the Apify Store.</strong> Open one, paste an input and '
                 f'run it, or call it from your code.</p><a class="btn" href="{STORE_URL}" rel="noopener">ActorForge on Apify →</a></div>')
    parts.append("</div></main>\n" + footer(ctx["used_cats"]))
    return path, "".join(parts)


def render_404(ctx):
    # Served for any missing path, so all links are root-absolute.
    nav = "".join(f'<li><a href="{BASE_PATH}categories/{c}/">{esc(CAT[c][1])}</a></li>' for c in ctx["used_cats"])
    return (head("Page not found | ActorForge", "This page does not exist. Browse ActorForge data actors and guides.", "404.html")
            .replace('<meta name="robots" content="index,follow">', '<meta name="robots" content="noindex">')
            + f'<body><header class="top"><div class="wrap"><a class="logo" href="{BASE_PATH}">Actor<span>Forge</span></a>'
            f'<nav aria-label="Main"><a href="{BASE_PATH}actors/">All actors</a><a href="{BASE_PATH}guides/">Guides</a></nav></div></header>'
            f'<main id="main"><div class="wrap"><h1>Page not found</h1><p>The page you asked for does not exist or has moved.</p>'
            f'<p><a class="btn" href="{BASE_PATH}">Go to the home page</a></p><h2>Categories</h2><ul>{nav}</ul>'
            f'</div></main></body>\n</html>\n')


# --------------------------------------------------------------------------- build

def write(path, text):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def build(offline):
    data = load_data(offline)
    eds = load_content("actors")
    guides_raw = load_content("guides")
    actors = {a["name"]: a for a in data["actors"]}
    Ctx.actor_names = set(actors)
    guides = {s: g for s, g in guides_raw.items() if g.get("actor") in actors}
    for s, g in guides_raw.items():
        if s not in guides:
            print(f"  ! skipping guide {s}: actor {g.get('actor')} is not public", file=sys.stderr)
    Ctx.guide_slugs = set(guides)

    actor_cat = {n: actor_category(a, eds.get(n, {})) for n, a in actors.items()}
    by_cat = {}
    for n in sorted(actors):
        by_cat.setdefault(actor_cat[n], []).append(n)
    used_cats = [c[0] for c in CATEGORIES if c[0] in by_cat]
    guide_cat = {s: (g.get("category") if g.get("category") in by_cat else actor_cat[g["actor"]]) for s, g in guides.items()}
    actor_guides = {}
    for g in guides.values():
        for n in [g["actor"]] + (g.get("alsoActors") or []):
            if n in actors:
                actor_guides.setdefault(n, []).append(g)

    os.makedirs(STATE, exist_ok=True)
    lm_path = os.path.join(STATE, "lastmod.json")
    old_lm = {}
    if os.path.exists(lm_path):
        with open(lm_path, encoding="utf-8") as f:
            old_lm = json.load(f)
    ctx = {"actors": actors, "guides": guides, "actor_cat": actor_cat, "by_cat": by_cat, "used_cats": used_cats,
           "guide_cat": guide_cat, "actor_guides": actor_guides,
           "lastmod": {k: v["date"] for k, v in old_lm.items()}}

    pages = [render_home(ctx), render_actor_index(ctx), render_guide_index(ctx)]
    pages += [render_category(c, ctx) for c in used_cats]
    pages += [render_actor(actors[n], eds.get(n, {}), ctx) for n in sorted(actors)]
    pages += [render_guide(g, ctx) for g in guides.values()]

    # remove pages of actors / guides / categories that no longer exist
    keep = {p for p, _ in pages}
    for sub in ("actors", "guides", "categories"):
        d = os.path.join(ROOT, sub)
        if os.path.isdir(d):
            for e in os.listdir(d):
                if f"{sub}/{e}/" not in keep and os.path.isfile(os.path.join(d, e, "index.html")):
                    os.remove(os.path.join(d, e, "index.html"))
                    os.rmdir(os.path.join(d, e))

    today = _dt.date.today().isoformat()
    new_lm = {}
    for path, text in pages:
        # hash ignores dateModified so that the date itself does not cause churn
        h = hashlib.sha256(re.sub(r'"dateModified":"[^"]*",?', "", text).encode()).hexdigest()[:16]
        prev = old_lm.get(path)
        new_lm[path] = prev if prev and prev["hash"] == h else {"hash": h, "date": today}
        write(path + "index.html", text)
    with open(lm_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(new_lm, f, indent=1, sort_keys=True)

    write("404.html", render_404(ctx))
    urls = "".join(f"<url><loc>{BASE_URL}{p}</loc><lastmod>{new_lm[p]['date']}</lastmod></url>\n" for p, _ in pages)
    write("sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n")
    write("robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL}sitemap.xml\n")
    write(f"{INDEXNOW_KEY}.txt", INDEXNOW_KEY)
    write(".nojekyll", "")
    changed = [BASE_URL + p for p in new_lm if old_lm.get(p, {}).get("hash") != new_lm[p]["hash"]]
    write(os.path.join(".cache", "changed.txt"), "\n".join(changed) + ("\n" if changed else ""))
    missing = sorted(n for n in actors if n not in eds)
    print(f"built {len(pages)} pages ({len(actors)} actors, {len(guides)} guides); {len(changed)} changed")
    if missing:
        print("  actors without editorial content (auto page from README):", ", ".join(missing))


# --------------------------------------------------------------------------- checks

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.links, self.ids, self.ld = [], [], [], set(), []
        self.title, self.desc, self.canon, self.h1 = None, None, None, 0
        self._in_ld = self._in_title = False
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in VOID:
            self.stack.append(tag)
        if "id" in a:
            if a["id"] in self.ids:
                self.errors.append(f"duplicate id {a['id']}")
            self.ids.add(a["id"])
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "link" and a.get("rel") == "canonical":
            self.canon = a.get("href")
        if tag == "meta" and a.get("name") == "description":
            self.desc = a.get("content")
        if tag == "h1":
            self.h1 += 1
        if tag == "script" and a.get("type") == "application/ld+json":
            self._in_ld, self._buf = True, ""
        if tag == "title":
            self._in_title, self._buf = True, ""

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> (open: {self.stack[-3:]})")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld.append(self._buf)
        if tag == "title":
            self._in_title = False
            self.title = self._buf

    def handle_data(self, d):
        if self._in_ld or self._in_title:
            self._buf += d


def check():
    problems, n = [], 0
    files = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if not d.startswith(".") and d != "content"]
        files += [os.path.join(dp, f) for f in fn if f.endswith(".html")]
    sitemap = open(os.path.join(ROOT, "sitemap.xml"), encoding="utf-8").read()
    for f in files:
        n += 1
        relp = os.path.relpath(f, ROOT).replace(os.sep, "/")
        c = Checker()
        c.feed(open(f, encoding="utf-8").read())
        c.close()
        p = lambda m: problems.append(f"{relp}: {m}")
        for e in c.errors:
            p(e)
        if c.stack:
            p(f"unclosed tags {c.stack}")
        if not c.title or len(c.title) > 60:
            p(f"title length {len(c.title or '')}: {c.title}")
        if not c.desc or len(c.desc) > 160:
            p(f"description length {len(c.desc or '')}")
        if c.h1 != 1:
            p(f"{c.h1} h1 elements")
        if relp != "404.html":
            if not c.canon:
                p("no canonical")
            elif c.canon not in sitemap:
                p(f"canonical not in sitemap: {c.canon}")
        for blob in c.ld:
            try:
                json.loads(blob)
            except ValueError as e:
                p(f"invalid JSON-LD: {e}")
        if relp != "404.html" and not c.ld:
            p("no JSON-LD")
        for href in c.links:
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            target = href.split("#")[0]
            if relp == "404.html":
                if not target.startswith(BASE_PATH):
                    p(f"404 link not absolute: {href}")
                    continue
                dest = os.path.join(ROOT, target[len(BASE_PATH):])
            else:
                if not target:
                    frag = href[1:]
                    if frag and frag not in c.ids:
                        p(f"missing anchor {href}")
                    continue
                dest = os.path.normpath(os.path.join(os.path.dirname(f), target))
            if os.path.isdir(dest):
                dest = os.path.join(dest, "index.html")
            if not os.path.exists(dest):
                p(f"broken link {href}")
    for loc in re.findall(r"<loc>([^<]+)</loc>", sitemap):
        local = os.path.join(ROOT, loc[len(BASE_URL):], "index.html")
        if not os.path.exists(local):
            problems.append(f"sitemap URL has no file: {loc}")
    print(f"checked {n} HTML files: {len(problems)} problems")
    for pr in problems:
        print("  -", pr)
    return not problems


if __name__ == "__main__":
    if "--check" not in sys.argv:
        build("--offline" in sys.argv)
    sys.exit(0 if check() else 1)
