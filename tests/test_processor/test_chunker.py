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


# --- 헤딩 기반 섹션 분리 + section_path 테스트 ---


def test_section_path_single_heading() -> None:
    """단일 헤딩 문서의 청크에 section_path가 설정된다."""
    text = "# Introduction\n\nThis is the intro."
    chunks = chunk_text(text, chunk_size=512)
    assert len(chunks) >= 1
    assert chunks[0].section_path == "Introduction"
    assert "Introduction" in chunks[0].content


def test_section_path_nested_headings() -> None:
    """중첩 헤딩 구조에서 올바른 section_path가 생성된다."""
    text = (
        "# Project\n\nOverview text.\n\n"
        "## Architecture\n\nArch details.\n\n"
        "### Backend\n\nBackend info.\n\n"
        "## Deployment\n\nDeploy info."
    )
    chunks = chunk_text(text, chunk_size=512)
    paths = [c.section_path for c in chunks]
    assert "Project" in paths
    assert "Project > Architecture" in paths
    assert "Project > Architecture > Backend" in paths
    assert "Project > Deployment" in paths


def test_section_path_empty_for_no_headings() -> None:
    """헤딩이 없는 문서의 청크는 section_path가 빈 문자열이다."""
    text = "This is plain text without any headings.\n\nAnother paragraph."
    chunks = chunk_text(text, chunk_size=512)
    assert len(chunks) >= 1
    for c in chunks:
        assert c.section_path == ""


def test_heading_included_in_chunk_content() -> None:
    """청크 내용에 해당 섹션의 헤딩이 포함된다."""
    text = "# Title\n\nBody text here."
    chunks = chunk_text(text, chunk_size=512)
    assert len(chunks) >= 1
    assert "# Title" in chunks[0].content
    assert "Body text" in chunks[0].content


def test_section_split_respects_chunk_size() -> None:
    """큰 섹션은 chunk_size에 맞게 분할되며 모든 청크에 section_path가 설정된다."""
    body = " ".join([f"word{i}" for i in range(500)])
    text = f"# Big Section\n\n{body}"
    chunks = chunk_text(text, chunk_size=100, chunk_overlap=10)
    assert len(chunks) > 1
    for c in chunks:
        assert c.section_path == "Big Section"


def test_text_before_first_heading() -> None:
    """첫 번째 헤딩 이전의 텍스트도 청크로 생성된다."""
    text = "Intro text before heading.\n\n# Section 1\n\nSection body."
    chunks = chunk_text(text, chunk_size=512)
    assert len(chunks) >= 2
    # 첫 번째 청크는 헤딩 이전 텍스트
    assert chunks[0].section_path == ""
    assert "Intro text" in chunks[0].content
    # 두 번째는 섹션
    assert chunks[1].section_path == "Section 1"


def test_sibling_headings_reset_path() -> None:
    """같은 레벨의 연속 헤딩은 경로가 올바르게 리셋된다."""
    text = (
        "# Root\n\nRoot body.\n\n"
        "## A\n\nA body.\n\n"
        "### A-1\n\nA-1 body.\n\n"
        "## B\n\nB body.\n\n"
        "### B-1\n\nB-1 body."
    )
    chunks = chunk_text(text, chunk_size=512)
    path_map = {c.section_path: c.content for c in chunks}
    assert "Root > A > A-1" in path_map
    assert "Root > B" in path_map
    assert "Root > B > B-1" in path_map
    # A-1의 경로가 B에 영향 주지 않는지 확인
    assert "Root > A > B" not in path_map


def test_index_sequential_with_sections() -> None:
    """섹션이 여러 개일 때도 인덱스가 전체 순서대로 증가한다."""
    text = (
        "# Section 1\n\nBody 1.\n\n"
        "# Section 2\n\nBody 2.\n\n"
        "# Section 3\n\nBody 3."
    )
    chunks = chunk_text(text, chunk_size=512)
    for i, chunk in enumerate(chunks):
        assert chunk.index == i
