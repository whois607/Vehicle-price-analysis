"""The shared code names no website - and works without being told any.

    python -m tests.check_neutral

* every file git tracks or would add (as it stands in the working tree) is scanned for
  each word in YOUR private site file (data/sites.json: labels, domains,
  addresses, id prefixes, photo server, private_words). Skipped, saying so,
  when there is no private file;
* the packager refuses a zip holding a private word or any data file;
* with NO site file the app still runs neutrally: pages are 'other', no
  advert link, no photo, no site id, a picker offering only 'other';
* with the test site file, each setting does its one job.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def main() -> int:
    from vpa import sites
    import make_package

    print("\n== the shared files name no site ==")
    words = sites.private_words()
    if not words:
        print("  (no private data/sites.json - nothing to scan for)")
    else:
        names = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True,
                               check=True).stdout.decode().split("\0")
        hits = []
        for n in filter(None, names):
            p = ROOT / n
            if not p.is_file():
                continue
            text = p.read_bytes().decode("utf-8", errors="ignore").casefold()
            found = [w for w in words if w.casefold() in text or w.casefold() in n.casefold()]
            if found:
                hits.append(f"{n} ({', '.join(found)})")
        check(f"no tracked file holds any of the {len(words)} private words",
              not hits, "; ".join(hits[:8]) or f"{len([n for n in names if n])} files")
        priv = subprocess.run(["git", "check-ignore", "-q", "data/sites.json"], cwd=ROOT)
        check("the private site file is ignored by git", priv.returncode == 0)

    print("\n== the packager refuses what must not be shared ==")
    def zipped(files):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for k, v in files.items():
                z.writestr(k, v)
        return zipfile.ZipFile(io.BytesIO(b.getvalue()))
    check("a clean zip passes", make_package.problems(zipped({"vpa/a.py": "x = 1"}), ["zzsite"]) == [])
    bad = make_package.problems(zipped({"vpa/a.py": "see ZZSite.example", "data/vpa.db": "",
                                        "data/.gitkeep": "", "page.html": "<p>"}), ["zzsite"])
    check("a private word, a database and a saved page are each refused",
          len(bad) == 3 and any("names zzsite" in b for b in bad), "; ".join(bad))

    print("\n== with no site file, everything is neutral ==")
    code = r"""
import os, sys, json
sys.path.insert(0, sys.argv[1])
from vpa import sites, links, merge, photos
from vpa.adapters import htmlpage
html = '<html><a href="https://www.examplecars.example/cars/x/EXA-AD-1/">x</a></html>'
print(json.dumps([sites.config(), htmlpage.detect_source(html), links.ad_url("/cars/x/EXA-AD-1/", "examplecars"),
                  links.ad_url("https://a.example/x", "other"), merge._is_site_id("EXA-AD-1"),
                  photos.photo_url("examplecars/cars/private/abcdefghijkl.jpg"),
                  photos.clean("https://examplecars.photos.example/cars/private/abcdefghijkl.jpg"),
                  sites.options_html(), sites.default_source(), sites.link_rules()]))
"""
    env = dict(os.environ, VPA_SITES=str(ROOT / "tests" / "no-such-sites.json"))
    r = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, env=env)
    import json
    got = json.loads(r.stdout) if r.returncode == 0 else None
    check("runs with no site file", got is not None, r.stderr[-300:])
    if got:
        check("a saved page is 'other', a bare path gets no link, a full one keeps its own",
              got[0] == {} and got[1] == "other" and got[2] is None and got[3] == "https://a.example/x", str(got[:4]))
        check("no id is a site id, no photo is read or shown",
              got[4] is False and got[5] is None and got[6] is None, str(got[4:7]))
        check("the source picker offers only 'other'; a single advert is 'other'",
              got[7] == '<option value="other">other</option>' and got[8] == "other"
              and got[9] == {"unlinked": [], "words": [], "paths": []}, str(got[7:]))

    print("\n== with the test site file, each setting does its job ==")
    from vpa import links, merge, photos
    from vpa.adapters import htmlpage
    check("pages are recognised in the file's order",
          htmlpage.detect_source("x examplebikes.example y examplecars.example") == "examplecars"
          and htmlpage.detect_source("exampleads.example") == "exampleads"
          and htmlpage.detect_source("nothing here") == "other")
    check("a bare path gets its site's address; another source's does not",
          links.ad_url("/cars/d/EXA-AD-1/", "examplecars") == "https://www.examplecars.example/cars/d/EXA-AD-1/"
          and links.ad_url("/cars/d/EXA-AD-1/", "examplebikes") is None)
    check("only the configured prefixes are site ids",
          merge._is_site_id("EXA-AD-1") and merge._is_site_id("EXB-AD-1")
          and not merge._is_site_id("EXCD-AD-1") and not merge._is_site_id("IMG-1"))
    p = photos.clean("https://examplecars.photos.example/examplecars/cars/private/abcdefghijkl.jpg")
    check("a photo on the configured server is kept and sized by its query",
          p == "examplecars/cars/private/abcdefghijkl.jpg"
          and photos.photo_url(p, 160, 107) == "https://examplecars.photos.example/" + p + "?fit=pad&size=160,107"
          and photos.clean("https://examplecars.elsewhere.example/cars/private/abcdefghijkl.jpg") is None)
    rules = sites.link_rules()
    check("links: one site is marked unlinked, the others' words and paths are known",
          rules["unlinked"] == ["exampleads.example", "ea.example"]
          and "examplecars" in rules["words"] and rules["paths"] == ["/cars/", "/bikes/"])

    print(f"\n== {PASS}/{PASS + FAIL} checks passed ==")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
