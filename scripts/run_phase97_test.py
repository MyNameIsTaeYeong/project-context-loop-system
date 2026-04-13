#!/usr/bin/env python3
"""Phase 9.7 수동 테스트 스크립트 — git_code 저장 + document_sources 연결 검증.

LLM 엔드포인트 없이 Mock Agent로 전체 파이프라인을 실행하여
Phase 9.7의 핵심 메커니즘을 검증한다.

검증 항목:
1. git_code 문서가 DB에 올바르게 저장되는가
2. code_summary ↔ git_code document_sources 연결이 생성되는가
3. code_doc ↔ git_code document_sources 연결이 생성되는가
4. 역방향 조회 (git_code → 참조 문서)가 동작하는가
5. 멱등성 — 재실행 시 중복이 발생하지 않는가
6. context_assembler의 원본 코드 첨부 기능이 동작하는가

사용법:
    python scripts/run_phase97_test.py

    # 사용자 Git 레포로 테스트 (LLM 없이 Mock Agent 사용)
    python scripts/run_phase97_test.py --repo /path/to/local/repo --product myapp

    # 특정 확장자만 수집
    python scripts/run_phase97_test.py --repo ./my-project --product svc --ext .py,.go
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.ingestion.coordinator import (  # noqa: E402
    CategoryDocument,
    CoordinatorAgent,
    DirectorySummary,
    FileSummary,
    PipelineResult,
    ProductResult,
    _collect_git_code_ids,
)
from context_loop.ingestion.git_config import (  # noqa: E402
    CategoryConfig,
    GitSourceConfig,
    load_git_source_config,
)
from context_loop.ingestion.git_repository import (  # noqa: E402
    FileInfo,
    compute_content_hash,
    store_git_code,
)
from context_loop.mcp.context_assembler import (  # noqa: E402
    _fetch_and_format_source_code,
)
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402


# ---------------------------------------------------------------------------
# 콘솔 출력 헬퍼
# ---------------------------------------------------------------------------

_OK = "\033[92m✓\033[0m"
_FAIL = "\033[91m✗\033[0m"
_SECTION = "\033[96m"
_RESET = "\033[0m"
_DIM = "\033[90m"


def section(title: str) -> None:
    print(f"\n{_SECTION}{'='*64}")
    print(f"  {title}")
    print(f"{'='*64}{_RESET}\n")


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = _OK if ok else _FAIL
    line = f"  {mark} {label}"
    if detail:
        line += f"  {_DIM}({detail}){_RESET}"
    print(line)


def info(label: str, value: object) -> None:
    print(f"  {_DIM}{label}:{_RESET} {value}")


# ---------------------------------------------------------------------------
# Mock Worker / Category Agent
# ---------------------------------------------------------------------------


class MockWorker:
    """Worker Agent Mock — 파일 내용의 첫 줄을 요약으로 사용."""

    async def process_directory(
        self,
        directory: str,
        product: str,
        files: list[FileInfo],
    ) -> DirectorySummary:
        file_summaries = [
            FileSummary(
                relative_path=f.relative_path,
                summary=f"[요약] {f.relative_path}: {f.content[:80]}",
            )
            for f in files
        ]
        doc = (
            f"# {directory}\n\n"
            f"상품: {product}, 파일 {len(files)}개\n\n"
            + "\n".join(f"- {f.relative_path}" for f in files)
        )
        return DirectorySummary(
            directory=directory,
            product=product,
            file_summaries=file_summaries,
            document=doc,
        )


class MockCategoryAgent:
    """Category Agent Mock — 디렉토리 요약을 합쳐서 관점 문서 생성."""

    async def generate_document(
        self,
        product: str,
        category: CategoryConfig,
        directory_summaries: list[DirectorySummary],
    ) -> CategoryDocument:
        dirs = [ds.directory for ds in directory_summaries]
        doc = (
            f"# [{product}] {category.display_name}\n\n"
            f"분석 대상: {len(directory_summaries)}개 디렉토리\n"
            f"대상 독자: {category.target_audience}\n\n"
            + "\n".join(f"- {d}" for d in dirs)
        )
        return CategoryDocument(
            product=product,
            category=category.name,
            document=doc,
            source_directories=dirs,
        )


# ---------------------------------------------------------------------------
# Git 레포 생성 헬퍼
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }
    subprocess.run(
        ["git"] + args, cwd=cwd, check=True,
        capture_output=True, env=env,
    )


def create_sample_repo(base_dir: Path) -> Path:
    """테스트용 샘플 Git 레포지토리를 생성한다."""
    repo_dir = base_dir / "sample-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "services/vpc/main.go": (
            "package main\n\n"
            "import \"fmt\"\n\n"
            "func main() {\n"
            '    fmt.Println("VPC Service")\n'
            "}\n"
        ),
        "services/vpc/handler.go": (
            "package main\n\n"
            "import \"net/http\"\n\n"
            "func handleCreateVPC(w http.ResponseWriter, r *http.Request) {\n"
            "    // VPC 생성 핸들러\n"
            "}\n\n"
            "func handleDeleteVPC(w http.ResponseWriter, r *http.Request) {\n"
            "    // VPC 삭제 핸들러\n"
            "}\n"
        ),
        "services/vpc/model.go": (
            "package main\n\n"
            "type VPC struct {\n"
            "    ID     string\n"
            "    Name   string\n"
            "    CIDR   string\n"
            "    Region string\n"
            "}\n\n"
            "type Subnet struct {\n"
            "    ID    string\n"
            "    VPCID string\n"
            "    CIDR  string\n"
            "    Zone  string\n"
            "}\n"
        ),
    }

    _git(["init", "-b", "main"], cwd=repo_dir)
    _git(["config", "user.email", "test@test.com"], cwd=repo_dir)
    _git(["config", "user.name", "Test"], cwd=repo_dir)
    _git(["config", "commit.gpgsign", "false"], cwd=repo_dir)

    for path, content in files.items():
        fp = repo_dir / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    _git(["add", "."], cwd=repo_dir)
    _git(["commit", "-m", "init: sample VPC service"], cwd=repo_dir)

    return repo_dir


# ---------------------------------------------------------------------------
# 검증 테스트
# ---------------------------------------------------------------------------


async def test_full_pipeline(tmp_dir: Path, repo_url: str) -> bool:
    """전체 파이프라인을 실행하고 Phase 9.7 핵심 메커니즘을 검증한다."""

    all_ok = True

    # --- 설정 ---
    config = Config(config_path=tmp_dir / "config.yaml")
    config.set("app.data_dir", str(tmp_dir / "data"))
    config.set("sources.git.enabled", True)
    config.set("sources.git.supported_extensions", [".go", ".py"])
    config.set("sources.git.file_size_limit_kb", 500)
    config.set("sources.git.repositories", [
        {
            "url": repo_url,
            "branch": "main",
            "products": {
                "vpc": {
                    "display_name": "VPC",
                    "paths": ["services/vpc/**"],
                    "exclude": [],
                },
            },
        },
    ])

    git_cfg = load_git_source_config(config)
    store = MetadataStore(tmp_dir / "test.db")
    await store.initialize()

    worker = MockWorker()
    cat_agent = MockCategoryAgent()
    coord = CoordinatorAgent(
        store, config, git_config=git_cfg,
        worker=worker, category_agent=cat_agent,
    )

    # =========================================================
    # 테스트 1: run() — ProductResult에 files/repo_url 전달
    # =========================================================
    section("1. run() — ProductResult에 files/repo_url 전달")

    result = await coord.run()

    pr = result.product_results[0]
    ok = pr.repo_url == repo_url
    check("repo_url이 ProductResult에 채워짐", ok, pr.repo_url)
    all_ok &= ok

    ok = len(pr.files) == 3
    check(f"files에 수집된 파일 {len(pr.files)}개 보존", ok, "기대: 3개")
    all_ok &= ok

    file_paths = sorted(f.relative_path for f in pr.files)
    for p in file_paths:
        info("  파일", p)

    ok = len(pr.directory_summaries) >= 1
    check(f"directory_summaries {len(pr.directory_summaries)}개 생성", ok)
    all_ok &= ok

    ok = len(pr.category_documents) == 5
    check(f"category_documents {len(pr.category_documents)}개 생성", ok, "기본 5 카테고리")
    all_ok &= ok

    # =========================================================
    # 테스트 2: run_and_store() — git_code 저장
    # =========================================================
    section("2. run_and_store() — git_code DB 저장")

    result2 = await coord.run_and_store()

    git_codes = await store.list_documents(source_type="git_code")
    ok = len(git_codes) == 3
    check(f"git_code 문서 {len(git_codes)}개 저장", ok, "기대: 3개")
    all_ok &= ok

    for doc in git_codes:
        info("  git_code", f"id={doc['id']}, source_id={doc['source_id']}, "
             f"title={doc['title']}")

    # 원본 코드 내용 확인
    main_doc = next((d for d in git_codes if "main.go" in d["source_id"]), None)
    if main_doc:
        ok = "package main" in main_doc["original_content"]
        check("원본 코드 내용 저장됨", ok, "main.go에 'package main' 포함")
        all_ok &= ok

    # =========================================================
    # 테스트 3: document_sources — code_summary ↔ git_code
    # =========================================================
    section("3. document_sources — code_summary ↔ git_code 연결")

    summaries = await store.list_documents(source_type="code_summary")
    ok = len(summaries) >= 1
    check(f"code_summary 문서 {len(summaries)}개 저장", ok)
    all_ok &= ok

    total_summary_links = 0
    for s in summaries:
        sources = await store.get_document_sources(s["id"])
        total_summary_links += len(sources)
        info("  code_summary", f"id={s['id']}, title={s['title']}")
        for src in sources:
            info("    → git_code", f"id={src['source_doc_id']}, "
                 f"file_path={src.get('file_path', 'N/A')}")

    ok = total_summary_links == 3
    check(f"code_summary → git_code 연결 {total_summary_links}개", ok, "기대: 3개")
    all_ok &= ok

    # =========================================================
    # 테스트 4: document_sources — code_doc ↔ git_code
    # =========================================================
    section("4. document_sources — code_doc ↔ git_code 연결")

    code_docs = await store.list_documents(source_type="code_doc")
    ok = len(code_docs) == 5
    check(f"code_doc 문서 {len(code_docs)}개 저장", ok, "기본 5 카테고리")
    all_ok &= ok

    for cd in code_docs:
        sources = await store.get_document_sources(cd["id"])
        ok_link = len(sources) >= 1
        check(
            f"  [{cd['source_id']}] → git_code {len(sources)}개 연결",
            ok_link,
        )
        all_ok &= ok_link

    # =========================================================
    # 테스트 5: 역방향 조회 — git_code → 참조 문서
    # =========================================================
    section("5. 역방향 조회 — git_code → 참조 문서 (code_summary + code_doc)")

    for gc in git_codes:
        referencing = await store.get_documents_by_source(gc["id"])
        ref_types = {r["source_type"] for r in referencing if "source_type" in r}
        info(
            f"  {gc['source_id']}",
            f"참조 문서 {len(referencing)}개 ({', '.join(ref_types)})",
        )
        ok = len(referencing) >= 1
        check(f"  git_code id={gc['id']} 역참조 {len(referencing)}개", ok)
        all_ok &= ok

    # =========================================================
    # 테스트 6: 멱등성 — 재실행 시 중복 없음
    # =========================================================
    section("6. 멱등성 — 재실행 시 중복 없음")

    await coord.run_and_store()

    git_codes_after = await store.list_documents(source_type="git_code")
    ok = len(git_codes_after) == 3
    check(f"재실행 후 git_code {len(git_codes_after)}개 (변화 없음)", ok, "기대: 3개")
    all_ok &= ok

    summaries_after = await store.list_documents(source_type="code_summary")
    ok = len(summaries_after) == len(summaries)
    check(f"재실행 후 code_summary {len(summaries_after)}개 (변화 없음)", ok)
    all_ok &= ok

    code_docs_after = await store.list_documents(source_type="code_doc")
    ok = len(code_docs_after) == 5
    check(f"재실행 후 code_doc {len(code_docs_after)}개 (변화 없음)", ok)
    all_ok &= ok

    # =========================================================
    # 테스트 7: _collect_git_code_ids 헬퍼
    # =========================================================
    section("7. _collect_git_code_ids 헬퍼 함수")

    test_map = {
        "services/vpc/main.go": 10,
        "services/vpc/handler.go": 11,
        "services/auth/login.go": 12,
    }

    ids = _collect_git_code_ids(["services/vpc"], test_map)
    ok = set(ids) == {10, 11}
    check("services/vpc 매칭", ok, f"결과: {ids}")
    all_ok &= ok

    ids = _collect_git_code_ids(["."], test_map)
    ok = set(ids) == {10, 11, 12}
    check('"." 루트 매칭 (전체)', ok, f"결과: {ids}")
    all_ok &= ok

    ids = _collect_git_code_ids(["lib"], test_map)
    ok = ids == []
    check("매칭 없음 → 빈 리스트", ok)
    all_ok &= ok

    # =========================================================
    # 테스트 8: context_assembler 원본 코드 첨부
    # =========================================================
    section("8. context_assembler — 원본 소스 코드 첨부")

    # code_doc의 document_sources로 연결된 git_code 원본 코드를 포맷팅
    code_doc_ids = {cd["id"] for cd in code_docs}
    source_text = await _fetch_and_format_source_code(code_doc_ids, store)

    ok = source_text is not None
    check("원본 소스 코드 섹션 생성됨", ok)
    all_ok &= ok

    if source_text:
        ok = "원본 소스 코드" in source_text
        check("섹션 헤더 '원본 소스 코드' 포함", ok)
        all_ok &= ok

        ok = "package main" in source_text
        check("main.go 원본 코드 포함", ok)
        all_ok &= ok

        ok = "handler.go" in source_text
        check("handler.go 파일명 포함", ok)
        all_ok &= ok

        ok = "```go" in source_text
        check("언어 힌트 ```go 포함", ok)
        all_ok &= ok

        # 미리보기 출력
        print(f"\n{_DIM}--- 원본 소스 코드 섹션 미리보기 (첫 500자) ---{_RESET}")
        print(source_text[:500])
        if len(source_text) > 500:
            print(f"{_DIM}... (총 {len(source_text)}자){_RESET}")

    # code_doc이 아닌 일반 문서 → 소스 코드 없음
    manual_id = await store.create_document(
        source_type="manual", title="일반 문서",
        original_content="일반 내용", content_hash="h_manual",
    )
    empty_result = await _fetch_and_format_source_code({manual_id}, store)
    ok = empty_result is None
    check("일반 문서 → 소스 코드 없음 (None)", ok)
    all_ok &= ok

    # =========================================================
    # 테스트 9: DB 통계 요약
    # =========================================================
    section("9. DB 통계 요약")

    stats = await store.get_stats()
    info("전체 문서 수", stats.get("document_count", 0))
    info("  git_code", len(await store.list_documents(source_type="git_code")))
    info("  code_summary", len(await store.list_documents(source_type="code_summary")))
    info("  code_doc", len(await store.list_documents(source_type="code_doc")))

    await store.close()
    return all_ok


# ---------------------------------------------------------------------------
# 사용자 레포 모드
# ---------------------------------------------------------------------------


async def test_with_user_repo(
    tmp_dir: Path,
    repo_path: str,
    product: str,
    extensions: list[str],
) -> bool:
    """사용자가 지정한 로컬 Git 레포로 Phase 9.7을 테스트한다."""

    section("사용자 레포로 Phase 9.7 테스트")

    repo_dir = Path(repo_path).resolve()
    if not (repo_dir / ".git").is_dir():
        print(f"  {_FAIL} Git 레포지토리가 아닙니다: {repo_dir}")
        return False

    info("레포 경로", repo_dir)
    info("상품명", product)
    info("확장자", extensions)

    config = Config(config_path=tmp_dir / "config.yaml")
    config.set("app.data_dir", str(tmp_dir / "data"))
    config.set("sources.git.enabled", True)
    config.set("sources.git.supported_extensions", extensions)
    config.set("sources.git.file_size_limit_kb", 500)
    config.set("sources.git.repositories", [
        {
            "url": str(repo_dir),
            "branch": "main",
            "products": {
                product: {
                    "display_name": product,
                    "paths": ["**"],
                    "exclude": ["**/vendor/**", "**/node_modules/**", "**/.git/**"],
                },
            },
        },
    ])

    git_cfg = load_git_source_config(config)
    store = MetadataStore(tmp_dir / "test.db")
    await store.initialize()

    worker = MockWorker()
    cat_agent = MockCategoryAgent()
    coord = CoordinatorAgent(
        store, config, git_config=git_cfg,
        worker=worker, category_agent=cat_agent,
    )

    all_ok = True

    # 파이프라인 실행
    print("\n  파이프라인 실행 중...")
    start = time.time()
    result = await coord.run_and_store()
    elapsed = time.time() - start

    info("실행 시간", f"{elapsed:.1f}초")
    info("처리된 파일", result.total_files_processed)
    info("디렉토리", result.total_directories)
    info("카테고리 문서", result.total_documents_generated)

    if result.errors:
        print(f"\n  오류 {len(result.errors)}개:")
        for err in result.errors[:5]:
            print(f"    - {err}")
    print()

    # git_code 확인
    git_codes = await store.list_documents(source_type="git_code")
    ok = len(git_codes) > 0
    check(f"git_code 문서 {len(git_codes)}개 저장", ok)
    all_ok &= ok

    if git_codes:
        for gc in git_codes[:10]:
            info("  git_code", f"{gc['source_id']} ({gc['title']})")
        if len(git_codes) > 10:
            info("  ...", f"외 {len(git_codes) - 10}개")

    # document_sources 확인
    summaries = await store.list_documents(source_type="code_summary")
    code_docs = await store.list_documents(source_type="code_doc")

    total_links = 0
    for s in summaries:
        sources = await store.get_document_sources(s["id"])
        total_links += len(sources)
    for cd in code_docs:
        sources = await store.get_document_sources(cd["id"])
        total_links += len(sources)

    ok = total_links > 0
    check(f"document_sources 연결 총 {total_links}개", ok)
    all_ok &= ok

    info("code_summary", f"{len(summaries)}개")
    info("code_doc", f"{len(code_docs)}개")

    # 원본 코드 첨부 테스트
    if code_docs:
        code_doc_ids = {cd["id"] for cd in code_docs[:3]}
        source_text = await _fetch_and_format_source_code(code_doc_ids, store)
        ok = source_text is not None
        check("원본 소스 코드 첨부 동작", ok)
        all_ok &= ok

        if source_text:
            print(f"\n{_DIM}--- 원본 소스 코드 미리보기 (첫 400자) ---{_RESET}")
            print(source_text[:400])

    await store.close()
    return all_ok


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 9.7 수동 테스트 — git_code 저장 + document_sources 연결 검증",
    )
    parser.add_argument(
        "--repo", "-r",
        default="",
        help="테스트할 로컬 Git 레포 경로 (미지정 시 샘플 레포 자동 생성)",
    )
    parser.add_argument(
        "--product", "-p",
        default="myapp",
        help="상품명 (--repo 모드에서 사용, 기본: myapp)",
    )
    parser.add_argument(
        "--ext", "-e",
        default=".go,.py",
        help="수집할 확장자 (쉼표 구분, 기본: .go,.py)",
    )
    args = parser.parse_args()

    extensions = [
        e.strip() if e.strip().startswith(".") else f".{e.strip()}"
        for e in args.ext.split(",") if e.strip()
    ]

    with tempfile.TemporaryDirectory(prefix="phase97_") as tmp:
        tmp_dir = Path(tmp)

        if args.repo:
            ok = asyncio.run(
                test_with_user_repo(tmp_dir, args.repo, args.product, extensions)
            )
        else:
            # 샘플 레포 생성 후 전체 검증
            print(f"{_SECTION}Phase 9.7 검증 스크립트{_RESET}")
            print(f"{_DIM}LLM 없이 Mock Agent로 전체 파이프라인을 검증합니다.{_RESET}")

            repo_dir = create_sample_repo(tmp_dir)
            info("샘플 레포", repo_dir)

            start = time.time()
            ok = asyncio.run(test_full_pipeline(tmp_dir, str(repo_dir)))
            elapsed = time.time() - start

        # 최종 결과
        section("최종 결과")
        if ok:
            print(f"  {_OK} 모든 검증 통과")
        else:
            print(f"  {_FAIL} 일부 검증 실패")

        if not args.repo:
            info("총 소요 시간", f"{elapsed:.1f}초")

    return sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
