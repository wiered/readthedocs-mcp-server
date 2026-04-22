"""Tests for readdocsserver.crawl URL helpers and HTML extraction."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup, Tag

from readdocsserver.crawl import (
    _parse_sitemap_urls,
    canonical_page_url,
    extract_page_toc,
    extract_structured_entities,
    extract_text_and_title,
    normalize_doc_root,
    same_site_links,
    under_prefix,
)
from readdocsserver.crawl.render import _find_main_content, _render_blocks
from readdocsserver.crawl.runner import _decompose_soup


def _parsed_page(html: bytes) -> tuple[BeautifulSoup, Tag]:
    soup = _decompose_soup(html)
    return soup, _find_main_content(soup)


def _extract_text(html: bytes, page_url: str) -> tuple[str, str, Tag]:
    soup, main = _parsed_page(html)
    title, text = extract_text_and_title(soup, main, page_url)
    return title, text, main


@pytest.mark.parametrize(
    ("url", "expected_prefix"),
    [
        ("https://rtd.io/en/stable/index.html", "https://rtd.io/en/stable/"),
        ("https://rtd.io/en/stable/", "https://rtd.io/en/stable/"),
        ("https://rtd.io/en/stable", "https://rtd.io/en/stable/"),
    ],
)
def test_normalize_doc_root(url: str, expected_prefix: str) -> None:
    assert normalize_doc_root(url) == expected_prefix


def test_canonical_strips_highlight() -> None:
    u = "https://x.com/doc/page.html?highlight=foo&other=1"
    c = canonical_page_url(u)
    assert "highlight" not in c
    assert "other=1" in c


def test_canonical_index_html() -> None:
    c = canonical_page_url("https://x.com/en/index.html")
    assert c.endswith("/en/")
    assert "index.html" not in c


def test_under_prefix() -> None:
    origin = "https://docs.python.org"
    prefix = "/3/"
    assert (
        under_prefix("https://docs.python.org/3/library/os.html", origin, prefix)
        is True
    )
    assert under_prefix("https://docs.python.org/2/", origin, prefix) is False
    assert under_prefix("https://evil.com/3/", origin, prefix) is False


def test_parse_sitemap_urlset() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ex.com/a</loc></url>
  <url><loc>https://ex.com/b</loc></url>
</urlset>"""
    pages, nested = _parse_sitemap_urls(xml)
    assert pages == ["https://ex.com/a", "https://ex.com/b"]
    assert nested == []


def test_parse_sitemap_index() -> None:
    xml = b"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://ex.com/sm1.xml</loc></sitemap>
</sitemapindex>"""
    pages, nested = _parse_sitemap_urls(xml)
    assert pages == []
    assert nested == ["https://ex.com/sm1.xml"]


def test_extract_text_rst_content() -> None:
    html = b"""<!doctype html><html><head><title>API</title></head>
<body><div class="rst-content"><p>Hello</p><script>removed()</script></div></body></html>"""
    title, text, _ = _extract_text(html, "https://x/")
    assert title == "API"
    assert "Hello" in text
    assert "removed" not in text


def test_extract_page_toc_nested_sections_and_summaries() -> None:
    html = b"""<!doctype html><html><body>
<div class="rst-content">
  <section id="main-section">
    <h1>Main section<a class="headerlink" href="#main-section">&#182;</a></h1>
    <p>This is one sentence summary of section.</p>
    <p>Second paragraph is not a TOC summary.</p>
    <section id="sub-section-1">
      <h2>Sub section 1<a class="headerlink" href="#sub-section-1">&#182;</a></h2>
      <p>Sub summary.</p>
    </section>
    <section id="sub-section-2">
      <h3>Sub section 2<a class="headerlink" href="#sub-section-2">&#182;</a></h3>
    </section>
  </section>
</div></body></html>"""

    _, main = _parsed_page(html)
    toc = extract_page_toc(main, "https://docs.example/page.html")

    assert toc == [
        {
            "id": "main-section",
            "title": "Main section",
            "summary": "This is one sentence summary of section.",
            "level": 1,
            "url": "https://docs.example/page.html#main-section",
            "children": [
                {
                    "id": "sub-section-1",
                    "title": "Sub section 1",
                    "summary": "Sub summary.",
                    "level": 2,
                    "url": "https://docs.example/page.html#sub-section-1",
                    "children": [],
                },
                {
                    "id": "sub-section-2",
                    "title": "Sub section 2",
                    "summary": "",
                    "level": 2,
                    "url": "https://docs.example/page.html#sub-section-2",
                    "children": [],
                },
            ],
        }
    ]


def test_extract_page_toc_ignores_missing_id_heading_and_nested_paragraph() -> None:
    html = b"""<!doctype html><html><body>
<article>
  <div>
    <section id="asynctelebot">
      <h1>AsyncTeleBot<a class="headerlink" href="#asynctelebot">&#182;</a></h1>
      <section id="module-telebot.async_telebot">
        <h2>AsyncTeleBot methods</h2>
        <p>Nested summary only.</p>
      </section>
      <section><h2>No id</h2></section>
      <section id="no-heading"><p>No heading.</p></section>
    </section>
  </div>
</article></body></html>"""

    _, main = _parsed_page(html)
    toc = extract_page_toc(main, "https://docs.example/async.html")

    assert toc[0]["id"] == "asynctelebot"
    assert toc[0]["summary"] == ""
    assert [child["id"] for child in toc[0]["children"]] == [
        "module-telebot.async_telebot"
    ]
    assert toc[0]["children"][0]["summary"] == "Nested summary only."


def test_extract_text_sphinx_signature_and_field_list() -> None:
    html = b"""<!doctype html><html><head><title>API</title></head>
<body>
  <article role="main">
    <dl class="py method">
      <dt class="sig sig-object py" id="mod.send_photo">
        <span class="sig-name descname"><span class="pre">send_photo</span></span>
        <span class="sig-paren">(</span>
        <em class="sig-param"><span class="n"><span class="pre">chat_id</span></span><span class="p"><span class="pre">:</span></span><span class="w"> </span><span class="n"><span class="pre">int</span><span class="w"> </span><span class="p"><span class="pre">|</span></span><span class="w"> </span><span class="pre">str</span></span></em>,
        <em class="sig-param"><span class="n"><span class="pre">photo</span></span><span class="p"><span class="pre">:</span></span><span class="w"> </span><span class="n"><span class="pre">Any</span><span class="w"> </span><span class="p"><span class="pre">|</span></span><span class="w"> </span><span class="pre">str</span></span></em>
        <span class="sig-paren">)</span>
        <span class="sig-return"><span class="sig-return-icon">&#8594;</span> <span class="sig-return-typehint"><span class="pre">Message</span></span></span>
        <a class="headerlink" href="#mod.send_photo" title="Link to this definition">&#182;</a>
      </dt>
      <dd>
        <p>Use this method to send photos.</p>
        <dl class="field-list simple">
          <dt class="field-odd">Parameters<span class="colon">:</span></dt>
          <dd class="field-odd">
            <ul class="simple">
              <li><p><strong>chat_id</strong> (<code>int</code> or <code>str</code>) - Chat id.</p></li>
              <li><p><strong>business_connection_id</strong> (<code>str</code>) - Business connection.</p></li>
            </ul>
          </dd>
          <dt class="field-even">Returns<span class="colon">:</span></dt>
          <dd class="field-even"><p>On success, the sent Message is returned.</p></dd>
          <dt class="field-odd">Return type<span class="colon">:</span></dt>
          <dd class="field-odd"><p><code>telebot.types.Message</code></p></dd>
        </dl>
      </dd>
    </dl>
  </article>
</body></html>"""
    title, text, _ = _extract_text(html, "https://x/")
    assert title == "API"
    assert "send_photo(chat_id: int | str, photo: Any | str) → Message" in text
    assert "chat_id(int or str) – Chat id." in text
    assert "business_connection_id(str) – Business connection." in text
    assert "Returns:\nOn success, the sent Message is returned." in text
    assert "Return type:\ntelebot.types.Message" in text


def test_extract_structured_entities_class_method_params_and_notes() -> None:
    html = b"""<!doctype html><html><head><title>API</title></head>
<body>
  <article role="main">
    <dl class="py class">
      <dt class="sig sig-object py" id="views.LayoutView">
        <span class="sig-name descname"><span class="pre">LayoutView</span></span>
      </dt>
      <dd>
        <p>Layout container.</p>
        <div class="versionadded"><p>Added in version 1.2: Initial class.</p></div>
        <dl class="py method">
          <dt class="sig sig-object py" id="views.LayoutView.edit_message">
            <span class="sig-name descname"><span class="pre">edit_message</span></span>
            <span class="sig-paren">(</span>
            <em class="sig-param"><span class="n"><span class="pre">text</span></span><span class="p"><span class="pre">:</span></span> <span class="n"><span class="pre">str</span></span></em>,
            <em class="sig-param"><span class="n"><span class="pre">silent</span></span><span class="p"><span class="pre">:</span></span> <span class="n"><span class="pre">bool</span></span><span class="o"><span class="pre">=</span></span><span class="default_value"><span class="pre">False</span></span></em>
            <span class="sig-paren">)</span>
          </dt>
          <dd>
            <p>Edit a message.</p>
            <dl class="field-list simple">
              <dt>Parameters<span class="colon">:</span></dt>
              <dd>
                <ul class="simple">
                  <li><p><strong>text</strong> (<code>str</code>) - Message text.</p></li>
                  <li><p><strong>parse_mode</strong> (<code>str</code>) - Parse mode.</p></li>
                </ul>
              </dd>
            </dl>
            <div class="admonition note"><p class="admonition-title">Note</p><p>Only works for editable messages.</p></div>
            <div class="admonition warning"><p class="admonition-title">Warning</p><p>May fail after timeout.</p></div>
            <div class="versionchanged"><p>Changed in version 1.3: Supports silent.</p></div>
            <div class="deprecated"><p>Deprecated since version 1.4: Use update_message.</p></div>
            <div class="versionremoved"><p>Removed in version 2.0: No longer available.</p></div>
          </dd>
        </dl>
      </dd>
    </dl>
  </article>
</body></html>"""
    title, text, main = _extract_text(html, "https://x/api.html")
    entities = extract_structured_entities(main, "https://x/api.html", text)

    assert title == "API"
    assert len(entities) == 2
    cls, method = entities
    assert cls["kind"] == "class"
    assert cls["name"] == "LayoutView"
    assert cls["notes"][0]["kind"] == "versionadded"
    assert method["kind"] == "method"
    assert method["parent_local_id"] == cls["local_id"]
    assert method["name"] == "edit_message"
    assert [p["name"] for p in method["params"]] == ["text", "silent", "parse_mode"]
    assert method["params"][0]["description"] == "Message text."
    assert method["params"][1]["default"] == "False"
    assert {n["kind"] for n in method["notes"]} >= {
        "note",
        "warning",
        "versionchanged",
        "deprecated",
        "versionremoved",
    }
    assert method["line_start"] is not None


def test_extract_structured_entities_xref_candidates() -> None:
    html = b"""<!doctype html><html><body><article role="main">
      <dl class="py class">
        <dt class="sig sig-object py" id="pkg.Message">
          <span class="sig-name descname"><span class="pre">Message</span></span>
        </dt>
        <dd>
          <p>Returns <a class="reference internal" href="#pkg.User">User</a>.</p>
          <dl class="field-list simple">
            <dt>See also<span class="colon">:</span></dt>
            <dd><p><a class="reference internal" href="#pkg.Channel">Channel</a></p></dd>
          </dl>
          <div class="admonition note"><p>Use with <a href="#pkg.View">View</a>.</p></div>
          <div class="admonition warning"><p>Avoid <a href="#pkg.Legacy">Legacy</a>.</p></div>
        </dd>
      </dl>
    </article></body></html>"""

    _, text, main = _extract_text(html, "https://docs.example/api.html")
    entities = extract_structured_entities(main, "https://docs.example/api.html", text)
    xrefs = entities[0]["xrefs"]

    assert {xref["edge_type"] for xref in xrefs} >= {
        "references",
        "see_also",
        "mentioned_in_note",
        "mentioned_in_warning",
    }
    assert {xref["target_anchor"] for xref in xrefs} >= {
        "pkg.User",
        "pkg.Channel",
        "pkg.View",
        "pkg.Legacy",
    }


def test_extract_structured_entities_xref_context_from_single_traversal() -> None:
    html = b"""<!doctype html><html><body><article role="main">
      <dl class="py class">
        <dt class="sig sig-object py" id="pkg.Contexts">
          <span class="sig-name descname"><span class="pre">Contexts</span></span>
        </dt>
        <dd>
          <dl class="field-list simple">
            <dt>See also<span class="colon">:</span></dt>
            <dd><div><section><p>
              Deep <span><a href="#pkg.DeepSeeAlso">DeepSeeAlso</a></span>
            </p></section></div></dd>
          </dl>
          <div class="admonition warning">
            <p>See also <span><a href="#pkg.WarningSeeAlso">WarningSeeAlso</a></span>.</p>
          </div>
          <div class="admonition note">
            <div class="admonition warning">
              <p>Avoid <a href="#pkg.WarningInsideNote">WarningInsideNote</a>.</p>
            </div>
          </div>
          <div class="admonition seealso">
            <p class="admonition-title">See also</p>
            <p>Related <a href="#pkg.SphinxSeeAlso">SphinxSeeAlso</a>.</p>
          </div>
        </dd>
      </dl>
    </article></body></html>"""

    _, text, main = _extract_text(html, "https://docs.example/api.html")
    entities = extract_structured_entities(main, "https://docs.example/api.html", text)
    edge_types = {
        xref["target_anchor"]: xref["edge_type"] for xref in entities[0]["xrefs"]
    }

    assert edge_types["pkg.DeepSeeAlso"] == "see_also"
    assert edge_types["pkg.WarningSeeAlso"] == "see_also"
    assert edge_types["pkg.WarningInsideNote"] == "mentioned_in_warning"
    assert edge_types["pkg.SphinxSeeAlso"] == "see_also"


def test_extract_structured_entities_isolates_nested_method_metadata() -> None:
    html = b"""<!doctype html><html><body><article role="main">
      <dl class="py class">
        <dt class="sig sig-object py" id="pkg.Container">
          <span class="sig-name descname"><span class="pre">Container</span></span>
        </dt>
        <dd>
          <p>Container summary.</p>
          <div class="admonition note"><p>Class note.</p></div>
          <dl class="py method">
            <dt class="sig sig-object py" id="pkg.Container.do">
              <span class="sig-name descname"><span class="pre">do</span></span>
              <span class="sig-paren">(</span>
              <em class="sig-param"><span class="n"><span class="pre">item</span></span></em>
              <span class="sig-paren">)</span>
            </dt>
            <dd>
              <p>Do work with <a href="#pkg.Worker">Worker</a>.</p>
              <dl class="field-list simple">
                <dt>Parameters<span class="colon">:</span></dt>
                <dd><ul class="simple">
                  <li><p><strong>item</strong> (<code>str</code>) - Method item.</p></li>
                </ul></dd>
              </dl>
              <div class="admonition warning"><p>Method warning.</p></div>
            </dd>
          </dl>
        </dd>
      </dl>
    </article></body></html>"""

    _, text, main = _extract_text(html, "https://docs.example/api.html")
    cls, method = extract_structured_entities(
        main, "https://docs.example/api.html", text
    )

    assert cls["local_id"] == "pkg.Container"
    assert cls["params"] == []
    assert [note["kind"] for note in cls["notes"]] == ["note"]
    assert "Class note." in cls["notes"][0]["text"]
    assert cls["xrefs"] == []
    assert method["parent_local_id"] == cls["local_id"]
    assert [param["name"] for param in method["params"]] == ["item"]
    assert method["params"][0]["description"] == "Method item."
    assert {note["kind"] for note in method["notes"]} == {"warning"}
    assert {xref["target_anchor"] for xref in method["xrefs"]} == {"pkg.Worker"}


def test_extract_structured_entities_keeps_direct_class_metadata_separate() -> None:
    html = b"""<!doctype html><html><body><article role="main">
      <dl class="py class">
        <dt class="sig sig-object py" id="pkg.Container">
          <span class="sig-name descname"><span class="pre">Container</span></span>
        </dt>
        <dd>
          <p>Container summary.</p>
          <dl class="field-list simple">
            <dt>Parameters<span class="colon">:</span></dt>
            <dd><ul class="simple">
              <li><p><strong>name</strong> (<code>str</code>) - Container name.</p></li>
            </ul></dd>
            <dt>See also<span class="colon">:</span></dt>
            <dd><p><a href="#pkg.Other">Other</a></p></dd>
          </dl>
          <div class="admonition warning"><p>Class warning.</p></div>
          <dl class="py method">
            <dt class="sig sig-object py" id="pkg.Container.do">
              <span class="sig-name descname"><span class="pre">do</span></span>
            </dt>
            <dd>
              <p>Do work with <a href="#pkg.Worker">Worker</a>.</p>
              <dl class="field-list simple">
                <dt>Parameters<span class="colon">:</span></dt>
                <dd><ul class="simple">
                  <li><p><strong>item</strong> (<code>str</code>) - Method item.</p></li>
                </ul></dd>
              </dl>
              <div class="admonition note"><p>Method note.</p></div>
            </dd>
          </dl>
        </dd>
      </dl>
    </article></body></html>"""

    _, text, main = _extract_text(html, "https://docs.example/api.html")
    cls, method = extract_structured_entities(
        main, "https://docs.example/api.html", text
    )

    assert [param["name"] for param in cls["params"]] == ["name"]
    assert cls["params"][0]["description"] == "Container name."
    assert {note["kind"] for note in cls["notes"]} == {"warning"}
    assert {xref["edge_type"] for xref in cls["xrefs"]} == {"see_also"}
    assert {xref["target_anchor"] for xref in cls["xrefs"]} == {"pkg.Other"}
    assert [param["name"] for param in method["params"]] == ["item"]
    assert {note["kind"] for note in method["notes"]} == {"note"}
    assert {xref["target_anchor"] for xref in method["xrefs"]} == {"pkg.Worker"}


def test_extract_structured_entities_handles_many_nested_methods_without_leaks() -> (
    None
):
    methods = "\n".join(
        f"""
        <dl class="py method">
          <dt class="sig sig-object py" id="pkg.Container.method_{i}">
            <span class="sig-name descname"><span class="pre">method_{i}</span></span>
          </dt>
          <dd>
            <p>Method {i} summary.</p>
            <dl class="field-list simple">
              <dt>Parameters<span class="colon">:</span></dt>
              <dd><ul class="simple">
                <li><p><strong>value_{i}</strong> (<code>str</code>) - Value {i}.</p></li>
              </ul></dd>
            </dl>
          </dd>
        </dl>
        """
        for i in range(3)
    )
    html = f"""<!doctype html><html><body><article role="main">
      <dl class="py class">
        <dt class="sig sig-object py" id="pkg.Container">
          <span class="sig-name descname"><span class="pre">Container</span></span>
        </dt>
        <dd>
          <p>Container summary.</p>
          {methods}
        </dd>
      </dl>
    </article></body></html>""".encode()

    _, text, main = _extract_text(html, "https://docs.example/api.html")
    entities = extract_structured_entities(main, "https://docs.example/api.html", text)

    cls = entities[0]
    methods_out = entities[1:]
    assert len(entities) == 4
    assert cls["params"] == []
    assert all(method["parent_local_id"] == cls["local_id"] for method in methods_out)
    assert [
        [param["name"] for param in method["params"]] for method in methods_out
    ] == [
        ["value_0"],
        ["value_1"],
        ["value_2"],
    ]


def test_extract_structured_entities_excludes_wrapped_methods_from_class_body() -> None:
    html = b"""<!doctype html><html><body><article role="main">
      <dl class="py class">
        <dt class="sig sig-object py" id="pkg.Container">
          <span class="sig-name descname"><span class="pre">Container</span></span>
        </dt>
        <dd>
          <p>Container summary.</p>
          <dl class="field-list simple">
            <dt>Parameters<span class="colon">:</span></dt>
            <dd><ul class="simple">
              <li><p><strong>name</strong> (<code>str</code>) - Container name.</p></li>
            </ul></dd>
            <dt>See also<span class="colon">:</span></dt>
            <dd><p><a href="#pkg.Other">Other</a></p></dd>
          </dl>
          <div class="admonition note"><p>Class note.</p></div>
          <div class="methods">
            <dl class="py method">
              <dt class="sig sig-object py" id="pkg.Container.do">
                <span class="sig-name descname"><span class="pre">do</span></span>
              </dt>
              <dd>
                <p>Do method summary with <a href="#pkg.Worker">Worker</a>.</p>
                <dl class="field-list simple">
                  <dt>Parameters<span class="colon">:</span></dt>
                  <dd><ul class="simple">
                    <li><p><strong>item</strong> (<code>str</code>) - Method item.</p></li>
                  </ul></dd>
                </dl>
                <div class="admonition warning"><p>Method warning.</p></div>
              </dd>
            </dl>
          </div>
        </dd>
      </dl>
    </article></body></html>"""

    _, text, main = _extract_text(html, "https://docs.example/api.html")
    cls, method = extract_structured_entities(
        main, "https://docs.example/api.html", text
    )

    assert "Container summary." in cls["body_text"]
    assert "Parameters:\nname(str) – Container name." in cls["body_text"]
    assert "Class note." in cls["body_text"]
    assert "do" not in cls["body_text"]
    assert "Do method summary" not in cls["body_text"]
    assert "Method item." not in cls["body_text"]
    assert "Method warning." not in cls["body_text"]
    assert [param["name"] for param in cls["params"]] == ["name"]
    assert {note["kind"] for note in cls["notes"]} == {"note"}
    assert {xref["target_anchor"] for xref in cls["xrefs"]} == {"pkg.Other"}

    assert method["parent_local_id"] == cls["local_id"]
    assert "Do method summary" in method["body_text"]
    assert "Parameters:\nitem(str) – Method item." in method["body_text"]
    assert [param["name"] for param in method["params"]] == ["item"]
    assert {note["kind"] for note in method["notes"]} == {"warning"}
    assert {xref["target_anchor"] for xref in method["xrefs"]} == {"pkg.Worker"}


def test_render_blocks_cache_keeps_py_object_modes_separate() -> None:
    soup = BeautifulSoup(
        b"""<div>
          <p>Container summary.</p>
          <div class="methods">
            <dl class="py method">
              <dt class="sig sig-object py" id="pkg.Container.do">
                <span class="sig-name descname"><span class="pre">do</span></span>
              </dt>
              <dd><p>Do method summary.</p></dd>
            </dl>
          </div>
        </div>""",
        "html.parser",
    )
    wrapper = soup.div
    assert wrapper is not None
    cache: dict[tuple[int, bool], tuple[str, ...]] = {}

    without_py = _render_blocks(wrapper, cache=cache, include_py_objects=False)
    without_py_again = _render_blocks(wrapper, cache=cache, include_py_objects=False)
    with_py = _render_blocks(wrapper, cache=cache, include_py_objects=True)

    assert without_py == without_py_again == ["Container summary."]
    assert "Do method summary." not in without_py
    assert "Do method summary." in with_py
    assert (id(wrapper), False) in cache
    assert (id(wrapper), True) in cache


def test_same_site_links_filters_prefix() -> None:
    html = b"""
    <html><body>
    <a href="/en/one/">one</a>
    <a href="https://docs.example.org/en/two/">two</a>
    <a href="https://other.com/x">bad</a>
    </body></html>
    """
    base = "https://docs.example.org/en/index.html"
    origin = "https://docs.example.org"
    prefix = "/en/"
    links = same_site_links(html, base, origin, prefix)
    assert any("one" in u or "two" in u for u in links)
    assert all("other.com" not in u for u in links)
