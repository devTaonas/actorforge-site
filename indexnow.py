#!/usr/bin/env python3
"""Submit site URLs to IndexNow (Bing, Yandex, Seznam, Naver...). No account needed.

    python indexnow.py            # every URL in sitemap.xml
    python indexnow.py --changed  # only URLs changed by the last build (.cache/changed.txt)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import BASE_URL, INDEXNOW_KEY, ROOT  # noqa: E402

ENDPOINTS = ["https://api.indexnow.org/indexnow", "https://www.bing.com/indexnow"]


def urls():
    if "--changed" in sys.argv:
        p = os.path.join(ROOT, ".cache", "changed.txt")
        if not os.path.exists(p):
            return []
        return [u.strip() for u in open(p, encoding="utf-8") if u.strip()]
    sm = open(os.path.join(ROOT, "sitemap.xml"), encoding="utf-8").read()
    return re.findall(r"<loc>([^<]+)</loc>", sm)


def main():
    lst = urls()
    if not lst:
        print("nothing to submit")
        return 0
    host = re.match(r"https://([^/]+)/", BASE_URL).group(1)
    body = json.dumps({"host": host, "key": INDEXNOW_KEY, "keyLocation": f"{BASE_URL}{INDEXNOW_KEY}.txt",
                       "urlList": lst[:10000]}).encode()
    ok = True
    for ep in ENDPOINTS:
        req = urllib.request.Request(ep, data=body, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"{ep}: HTTP {r.status} ({len(lst)} URLs)")
        except urllib.error.HTTPError as e:
            print(f"{ep}: HTTP {e.code} {e.read()[:200]!r}")
            ok = ok and e.code in (200, 202)
        except Exception as e:  # network trouble should not fail the deploy
            print(f"{ep}: error {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
