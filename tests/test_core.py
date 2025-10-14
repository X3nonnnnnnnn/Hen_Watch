from hen_watch.core import Config, _extract_items

def _cfg(**kw):
    defaults = dict(
        search_url="",
        authors=[],
        result_selector="",
        title_selector="",
        link_selector="",
        telegram_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
    )
    defaults.update(kw)
    return Config(**defaults)

def test_extract_items_container_and_anchor():
    html = """
    <div class="entry">
      <span class="title">A</span>
      <a class="link" href="/g/123">Gallery A</a>
    </div>
    <a href="https://e-hentai.org/g/456"><b>Gallery B</b></a>
    """
    items = _extract_items(html, _cfg(result_selector=".entry, a"))
    assert len(items) == 2
    assert items[0]["title"] == "A"
    assert items[1]["url"].startswith("https://e-hentai.org/g/")
