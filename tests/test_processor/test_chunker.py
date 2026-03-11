"""텍스트 청킹 모듈 테스트."""

from __future__ import annotations

from context_loop.processor.chunker import Chunk, chunk_text, count_tokens


def test_count_tokens_basic() -> None:
    """토큰 수를 반환한다."""
    count = count_tokens("Hello world")
    assert count > 0


def test_chunk_text_empty() -> None:
    """빈 텍스트는 빈 목록을 반환한다."""
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunk_text_small_document() -> None:
    """chunk_size보다 작은 문서는 단일 청크로 반환한다."""
    text = "This is a short document."
    chunks = chunk_text(text, chunk_size=512)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].index == 0


def test_chunk_text_multiple_paragraphs() -> None:
    """여러 단락이 chunk_size 내에 있으면 합쳐진다."""
    paragraphs = ["First paragraph.", "Second paragraph.", "Third paragraph."]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, chunk_size=512)
    # 모두 하나의 청크에 들어갈 만큼 작다
    assert len(chunks) >= 1
    combined = " ".join(c.content for c in chunks)
    assert "First paragraph" in combined
    assert "Second paragraph" in combined


def test_chunk_text_respects_chunk_size() -> None:
    """chunk_size를 초과하지 않는다."""
    # 각 단어가 약 1토큰이므로 긴 텍스트를 만든다
    text = " ".join([f"word{i}" for i in range(2000)])
    chunks = chunk_text(text, chunk_size=100, chunk_overlap=10)
    assert len(chunks) > 1
    for chunk in chunks:
        # 각 청크의 토큰 수는 chunk_size + overlap 근처여야 함
        assert chunk.token_count <= 120  # 약간의 여유


def test_chunk_text_index_sequential() -> None:
    """청크 인덱스가 순서대로 증가한다."""
    text = "\n\n".join([f"Paragraph {i} " + "content " * 50 for i in range(10)])
    chunks = chunk_text(text, chunk_size=50, chunk_overlap=5)
    for i, chunk in enumerate(chunks):
        assert chunk.index == i


def test_chunk_text_has_unique_ids() -> None:
    """각 청크는 고유한 ID를 가진다."""
    text = "\n\n".join([f"Paragraph {i} " + "content " * 50 for i in range(5)])
    chunks = chunk_text(text, chunk_size=50, chunk_overlap=5)
    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids))


def test_chunk_text_large_paragraph() -> None:
    """chunk_size를 초과하는 단락도 올바르게 처리된다."""
    # 500토큰짜리 단락 하나
    long_para = "word " * 500
    chunks = chunk_text(long_para, chunk_size=100, chunk_overlap=10)
    assert len(chunks) > 1
