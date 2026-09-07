from bot.manual_publication_links import platform_identity
from bot.vk_direct_metrics import extract_stats_from_html, parse_vk_clip_identity


def test_manual_youtube_identity():
    assert platform_identity("youtube", "https://www.youtube.com/shorts/XnJ5OVMksw8") == "XnJ5OVMksw8"


def test_manual_vk_clip_identity_from_clips_query():
    url = "https://vk.ru/clips/got_ball?z=clip-202211208_456262542&feedType=ownerFeed&owner=-202211208"
    assert platform_identity("vk", url) == "-202211208_456262542"
    assert parse_vk_clip_identity(url) == "-202211208_456262542"


def test_extract_vk_clip_views_from_prefetch_payload():
    html = '''<script>;window.cur = Object.assign(window.cur || {}, {"apiPrefetchCache":[{"method":"video.get","response":{"count":1,"items":[{"owner_id":-202211208,"id":456262542,"views":12345,"likes":{"count":77},"comments":8,"date":1780000000}]}}]});</script>'''
    stats = extract_stats_from_html(html, "-202211208_456262542")
    assert stats.views == 12345
    assert stats.likes == 77
    assert stats.comments == 8
