"""
Regression: the generated keywords-file name must be stable across processes.

The wake-word list is compiled into a `winvoice_keywords_<digest>.txt` next to
the model and reused when present. The digest used to be `abs(hash(tuple(...)))`,
and Python salts string hashing per interpreter run — so the "cache" missed on
every launch and the model directory had accumulated 48 identical copies.
"""

from __future__ import annotations

import hashlib

from winvoice.audio.kws import _keywords_digest


def test_digest_is_a_pure_function_of_the_keyword_content() -> None:
    keywords = ["assistant", "小助手", "你好助手"]

    assert _keywords_digest(keywords) == hashlib.sha1(
        "assistant\n小助手\n你好助手".encode("utf-8")
    ).hexdigest()[:8]
    # Same words, different list object: same name.
    assert _keywords_digest(list(keywords)) == _keywords_digest(keywords)


def test_different_keyword_lists_get_different_files() -> None:
    assert _keywords_digest(["assistant"]) != _keywords_digest(["小助手"])
    assert _keywords_digest(["a", "b"]) != _keywords_digest(["ab"])
