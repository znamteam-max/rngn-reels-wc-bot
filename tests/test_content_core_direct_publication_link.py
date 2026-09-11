from pathlib import Path

SOURCE=(Path(__file__).resolve().parents[1]/'bot'/'content_core_integration.py').read_text(encoding='utf-8')

def test_attach_response_persists_publication_id_without_full_metrics_refresh() -> None:
    assert '_publication_id_from_attach_text' in SOURCE
    assert '_record_attached_publication_link' in SOURCE
    assert 'publication_links = _refresh_publication_links(video)' not in SOURCE
