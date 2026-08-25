"""Architecture guard for the confirmed-only synchronization boundary."""

from arxiv_digest import sync


def test_sync_does_not_expose_runtime_event_inference_helpers() -> None:
    assert not hasattr(sync, "diff_oai_article")
    assert not hasattr(sync, "associate_evidence")
