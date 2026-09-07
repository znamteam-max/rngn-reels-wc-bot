from bot.vk_direct_metrics import extract_stats_from_html


def test_extract_vk_clip_views_from_compact_object_assign():
    html = '<script>window.cur=Object.assign(window.cur||{}, {"apiPrefetchCache":[{"method":"video.get","response":{"items":[{"owner_id":-202211208,"id":456262387,"views":45678,"likes":{"count":12},"comments":3}]}}]});</script>'
    stats = extract_stats_from_html(html, "-202211208_456262387")
    assert stats.views == 45678
    assert stats.likes == 12
    assert stats.comments == 3


def test_extract_vk_clip_views_from_direct_cur_assignment():
    html = '<script>window.cur={"apiPrefetchCache":[{"method":"video.get","response":{"items":[{"owner_id":-202211208,"id":456262433,"views":98765,"likes":44,"comments":{"count":9}}]}}]};</script>'
    stats = extract_stats_from_html(html, "-202211208_456262433")
    assert stats.views == 98765
    assert stats.likes == 44
    assert stats.comments == 9


def test_extract_vk_clip_views_from_fragment_fallback():
    html = '<html><script>var request={"videos":"-202211208_456262491"};</script><script>window.__data={"owner_id":-202211208,"id":456262491,"views":54321,"likes":{"count":7},"comments":2};</script></html>'
    stats = extract_stats_from_html(html, "-202211208_456262491")
    assert stats.views == 54321
    assert stats.likes == 7
    assert stats.comments == 2
    assert stats.raw_data["parser"] == "html_fragment_fallback"
