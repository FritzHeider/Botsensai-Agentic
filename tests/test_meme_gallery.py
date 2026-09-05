"""Unit tests for meme lineage and perceptual hash gallery."""

from datetime import UTC, datetime

from botsensai.media.gallery import build_meme_lineage, render_meme_gallery_html
from botsensai.models import Platform, SocialPost
from botsensai.store.db import Database


def test_build_and_render_meme_gallery(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    token_key = "solana:testmint123"

    # Insert social posts with perceptual image hashes
    # Hash 1: original root
    # Hash 2: near duplicate of root (hamming distance 1)
    # Hash 3: completely distinct meme
    root_hash = "p:0000000000000000"
    near_hash = "p:0000000000000001"
    other_hash = "p:ffffffffffffffff"

    db.insert_posts(
        [
            SocialPost(
                platform=Platform.X,
                post_id="post_1",
                token_key=token_key,
                author="author_1",
                text="Great meme launch!",
                media_hashes=[root_hash],
                as_of=now,
                observed_at=now,
            ),
            SocialPost(
                platform=Platform.X,
                post_id="post_2",
                token_key=token_key,
                author="author_2",
                text="Remixing this meme!",
                media_hashes=[near_hash],
                as_of=now,
                observed_at=now,
            ),
            SocialPost(
                platform=Platform.X,
                post_id="post_3",
                token_key=token_key,
                author="author_3",
                text="Completely new community meme!",
                media_hashes=[other_hash],
                as_of=now,
                observed_at=now,
            ),
        ]
    )

    report = build_meme_lineage(token_key, db)
    assert report.total_images == 3
    assert report.distinct_clusters == 2  # (root+near) in one cluster, (other) in second
    assert report.originality_index > 0.5
    assert len(report.nodes) == 3

    html = render_meme_gallery_html(report)
    assert "<!doctype html>" in html
    assert "Meme Lineage" in html
    assert token_key in html

    db.close()
