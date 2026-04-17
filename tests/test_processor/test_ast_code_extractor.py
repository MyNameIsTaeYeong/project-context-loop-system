"""AST 기반 코드 심볼 추출 모듈 테스트."""

from __future__ import annotations

import textwrap

import pytest

from context_loop.processor.ast_code_extractor import (
    CodeExtraction,
    CodeSymbol,
    extract_code_symbols,
    to_chunks,
    to_graph_data,
)
from context_loop.processor.graph_extractor import Entity, GraphData, Relation


# ---------------------------------------------------------------------------
# Python extractor
# ---------------------------------------------------------------------------


class TestPythonExtraction:
    """Python 코드 심볼 추출 테스트."""

    _PYTHON_CODE = textwrap.dedent("""\
        import os
        from pathlib import Path

        class MyService:
            \"\"\"서비스 클래스.\"\"\"

            def __init__(self, name: str) -> None:
                self.name = name

            def run(self) -> None:
                pass

        async def handle_request(ctx: dict, req: str) -> str:
            \"\"\"요청을 처리한다.\"\"\"
            return "ok"

        def helper() -> int:
            return 42
    """)

    def test_extracts_symbols_with_method_level_chunking(self) -> None:
        """클래스 메서드를 개별 심볼로 추출한다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        assert result.language == "python"
        names = {s.name for s in result.symbols}
        # 클래스 내부 메서드가 개별 심볼로 추출됨
        assert "__init__" in names
        assert "run" in names
        # 최상위 함수도 추출
        assert "handle_request" in names
        assert "helper" in names
        # 메서드가 있는 클래스는 클래스 자체가 심볼로 추출되지 않음
        assert "MyService" not in names

    def test_symbol_types(self) -> None:
        """심볼 유형이 올바르게 설정된다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        types = {s.name: s.symbol_type for s in result.symbols}
        assert types["__init__"] == "method"
        assert types["run"] == "method"
        assert types["handle_request"] == "function"
        assert types["helper"] == "function"

    def test_extracts_imports(self) -> None:
        """import를 추출한다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        assert "os" in result.imports
        assert "pathlib" in result.imports

    def test_function_signature(self) -> None:
        """함수 시그니처가 올바르게 생성된다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        handle = next(s for s in result.symbols if s.name == "handle_request")
        assert "async def" in handle.signature
        assert "ctx: dict" in handle.signature
        assert "-> str" in handle.signature

    def test_docstring_extraction(self) -> None:
        """독스트링을 추출한다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        handle = next(s for s in result.symbols if s.name == "handle_request")
        assert "요청을 처리" in handle.docstring

    def test_method_parent_info(self) -> None:
        """메서드에 부모 클래스 정보가 설정된다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        init = next(s for s in result.symbols if s.name == "__init__")
        assert init.parent_name == "MyService"
        assert init.parent_signature == "class MyService"
        assert init.symbol_type == "method"

        run = next(s for s in result.symbols if s.name == "run")
        assert run.parent_name == "MyService"

    def test_method_body_contains_own_code(self) -> None:
        """메서드 body에 해당 메서드의 코드만 포함된다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        init = next(s for s in result.symbols if s.name == "__init__")
        assert "def __init__" in init.body
        assert "self.name = name" in init.body
        # 다른 메서드의 코드는 포함하지 않음
        assert "def run" not in init.body

    def test_class_without_methods_stays_single_symbol(self) -> None:
        """메서드가 없는 클래스는 전체가 단일 심볼로 추출된다."""
        code = textwrap.dedent("""\
            class Config:
                \"\"\"설정 클래스.\"\"\"
                DEBUG = True
                PORT = 8080
        """)
        result = extract_code_symbols(code, "config.py")

        assert len(result.symbols) == 1
        assert result.symbols[0].name == "Config"
        assert result.symbols[0].symbol_type == "class"
        assert result.symbols[0].parent_name == ""

    def test_empty_content(self) -> None:
        """빈 내용은 빈 결과를 반환한다."""
        result = extract_code_symbols("", "empty.py")
        assert result.symbols == []
        assert result.imports == []

    def test_syntax_error_fallback(self) -> None:
        """파싱 실패 시 전체 파일로 대체한다."""
        broken = "def broken(\nclass what:"
        result = extract_code_symbols(broken, "broken.py")
        assert len(result.symbols) == 1
        assert result.symbols[0].symbol_type == "module"


# ---------------------------------------------------------------------------
# Go extractor
# ---------------------------------------------------------------------------


class TestGoExtraction:
    """Go 코드 심볼 추출 테스트."""

    _GO_CODE = textwrap.dedent("""\
        package main

        import (
            "context"
            "fmt"
            "github.com/example/vpc/service"
        )

        // HandleRequest handles incoming HTTP requests.
        func HandleRequest(ctx context.Context, req *Request) (*Response, error) {
            svc := service.New()
            return svc.Process(req)
        }

        type VPCService struct {
            repo Repository
            name string
        }

        // Create creates a new VPC.
        func (s *VPCService) Create(req CreateRequest) (*VPC, error) {
            if req.Name == "" {
                return nil, fmt.Errorf("name required")
            }
            return s.repo.Save(req)
        }

        type Repository interface {
            Save(req CreateRequest) (*VPC, error)
            Find(id string) (*VPC, error)
        }
    """)

    def test_extracts_functions(self) -> None:
        """Go 함수와 리시버 메서드를 추출한다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        assert result.language == "go"
        names = {s.name for s in result.symbols}
        assert "HandleRequest" in names
        assert "Create" in names

    def test_receiver_method_has_parent(self) -> None:
        """Go 리시버 메서드에 부모 타입 정보가 설정된다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        create = next(s for s in result.symbols if s.name == "Create")
        assert create.symbol_type == "method"
        assert create.parent_name == "VPCService"

        handle = next(s for s in result.symbols if s.name == "HandleRequest")
        assert handle.symbol_type == "function"
        assert handle.parent_name == ""

    def test_extracts_types(self) -> None:
        """Go struct/interface를 추출한다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        types = {s.name: s.symbol_type for s in result.symbols}
        assert types["VPCService"] == "struct"
        assert types["Repository"] == "interface"

    def test_extracts_imports(self) -> None:
        """Go import를 추출한다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        assert "context" in result.imports
        assert "fmt" in result.imports
        assert "github.com/example/vpc/service" in result.imports

    def test_preceding_comment(self) -> None:
        """함수 직전 주석을 docstring으로 추출한다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        handle = next(s for s in result.symbols if s.name == "HandleRequest")
        assert "incoming HTTP requests" in handle.docstring

    def test_body_contains_full_code(self) -> None:
        """심볼 body에 전체 코드가 포함된다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        handle = next(s for s in result.symbols if s.name == "HandleRequest")
        assert "func HandleRequest" in handle.body
        assert "svc.Process(req)" in handle.body


# ---------------------------------------------------------------------------
# TypeScript extractor
# ---------------------------------------------------------------------------


class TestTypeScriptExtraction:
    """TypeScript 코드 심볼 추출 테스트."""

    _TS_CODE = textwrap.dedent("""\
        import { Request, Response } from 'express';
        import axios from 'axios';

        export class UserService {
            private baseUrl: string;

            constructor(url: string) {
                this.baseUrl = url;
            }

            async getUser(id: string): Promise<User> {
                return axios.get(`${this.baseUrl}/users/${id}`);
            }
        }

        export async function handleRequest(req: Request, res: Response) {
            const svc = new UserService("http://api");
            const user = await svc.getUser(req.params.id);
            res.json(user);
        }
    """)

    def test_extracts_symbols(self) -> None:
        """TypeScript 클래스 메서드와 함수를 추출한다."""
        result = extract_code_symbols(self._TS_CODE, "user.ts")

        assert result.language == "typescript"
        names = {s.name for s in result.symbols}
        # 클래스 메서드가 개별 심볼로 추출됨
        assert "constructor" in names or "getUser" in names
        assert "handleRequest" in names

    def test_extracts_imports(self) -> None:
        """TypeScript import를 추출한다."""
        result = extract_code_symbols(self._TS_CODE, "user.ts")

        assert "express" in result.imports
        assert "axios" in result.imports


# ---------------------------------------------------------------------------
# JavaScript extractor
# ---------------------------------------------------------------------------


class TestJavaScriptExtraction:
    """JavaScript 코드 심볼 추출 테스트."""

    def test_extracts_function_and_class_methods(self) -> None:
        code = textwrap.dedent("""\
            const express = require('express');

            class Router {
                constructor() {
                    this.routes = [];
                }
            }

            function createApp() {
                return new Router();
            }
        """)
        result = extract_code_symbols(code, "app.js")

        assert result.language == "javascript"
        names = {s.name for s in result.symbols}
        # 클래스 메서드가 개별로 추출됨
        assert "constructor" in names
        assert "createApp" in names
        assert "express" in result.imports

        # constructor는 Router의 메서드
        ctor = next(s for s in result.symbols if s.name == "constructor")
        assert ctor.parent_name == "Router"
        assert ctor.symbol_type == "method"


# ---------------------------------------------------------------------------
# Java extractor
# ---------------------------------------------------------------------------


class TestJavaExtraction:
    """Java 코드 심볼 추출 테스트."""

    def test_extracts_class(self) -> None:
        code = textwrap.dedent("""\
            import java.util.List;
            import com.example.service.VPCService;

            public class VPCController {
                private final VPCService service;

                public VPCController(VPCService service) {
                    this.service = service;
                }

                public List<VPC> listVPCs() {
                    return service.findAll();
                }
            }
        """)
        result = extract_code_symbols(code, "VPCController.java")

        assert result.language == "java"
        names = {s.name for s in result.symbols}
        assert "VPCController" in names
        assert "java.util.List" in result.imports
        assert "com.example.service.VPCService" in result.imports


# ---------------------------------------------------------------------------
# Fallback (unknown language)
# ---------------------------------------------------------------------------


class TestFallback:
    """알 수 없는 언어의 폴백 테스트."""

    def test_unknown_extension_returns_whole_file(self) -> None:
        """알 수 없는 확장자는 전체 파일을 단일 심볼로 반환한다."""
        content = "SELECT * FROM users WHERE id = 1;"
        result = extract_code_symbols(content, "query.sql")

        assert result.language == "unknown"
        assert len(result.symbols) == 1
        assert result.symbols[0].symbol_type == "module"
        assert result.symbols[0].body == content

    def test_yaml_returns_whole_file(self) -> None:
        content = "key: value\nlist:\n  - item1"
        result = extract_code_symbols(content, "config.yaml")
        assert len(result.symbols) == 1


# ---------------------------------------------------------------------------
# to_chunks
# ---------------------------------------------------------------------------


class TestToChunks:
    """CodeExtraction → Chunk 변환 테스트."""

    def test_converts_symbols_to_chunks(self) -> None:
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[
                CodeSymbol(
                    name="HandleRequest",
                    symbol_type="function",
                    signature="func HandleRequest(ctx context.Context)",
                    body="func HandleRequest(ctx context.Context) {\n    // ...\n}",
                    line_start=1,
                    line_end=3,
                ),
            ],
            imports=["context"],
        )
        chunks, embed_texts = to_chunks(extraction, "handler.go")

        assert len(chunks) == 1
        assert chunks[0].index == 0
        assert "# File: handler.go" in chunks[0].content
        assert "func HandleRequest" in chunks[0].content
        assert chunks[0].section_path == "handler.go > HandleRequest"
        assert chunks[0].token_count > 0

        # 임베딩 텍스트는 이름+시그니처 (코드 본문 제외)
        assert len(embed_texts) == 1
        assert "HandleRequest" in embed_texts[0]
        assert "func HandleRequest(ctx context.Context)" in embed_texts[0]
        assert "// ..." not in embed_texts[0]  # 코드 본문은 포함하지 않음

    def test_docstring_included_in_embed_text(self) -> None:
        """docstring이 있으면 임베딩 텍스트에 포함된다."""
        extraction = CodeExtraction(
            file_path="service.py",
            language="python",
            symbols=[
                CodeSymbol(
                    name="create_vpc",
                    symbol_type="function",
                    signature="def create_vpc(name: str) -> VPC",
                    body="def create_vpc(name: str) -> VPC:\n    ...",
                    line_start=1,
                    line_end=2,
                    docstring="VPC를 생성한다.",
                ),
            ],
            imports=[],
        )
        _, embed_texts = to_chunks(extraction, "service.py")

        assert "VPC를 생성한다." in embed_texts[0]

    def test_method_chunk_includes_parent_info(self) -> None:
        """메서드 청크에 부모 클래스 정보가 포함된다."""
        extraction = CodeExtraction(
            file_path="service.py",
            language="python",
            symbols=[
                CodeSymbol(
                    name="run",
                    symbol_type="method",
                    signature="def run(self) -> None",
                    body="    def run(self) -> None:\n        pass",
                    line_start=5,
                    line_end=6,
                    parent_name="MyService",
                    parent_signature="class MyService",
                ),
            ],
            imports=[],
        )
        chunks, embed_texts = to_chunks(extraction, "service.py")

        assert len(chunks) == 1
        # 헤더에 부모 클래스 정보 포함
        assert "# class MyService" in chunks[0].content
        # section_path에 부모 클래스 포함
        assert chunks[0].section_path == "service.py > MyService > run"
        # 임베딩 텍스트에 부모 클래스 이름 포함
        assert "MyService" in embed_texts[0]

    def test_empty_symbols_returns_empty(self) -> None:
        extraction = CodeExtraction(file_path="empty.py", language="python")
        chunks, embed_texts = to_chunks(extraction, "empty.py")
        assert chunks == []
        assert embed_texts == []


# ---------------------------------------------------------------------------
# to_graph_data
# ---------------------------------------------------------------------------


class TestToGraphData:
    """CodeExtraction → GraphData 변환 테스트."""

    def test_creates_module_entity(self) -> None:
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[],
            imports=[],
        )
        graph = to_graph_data(extraction, "handler.go")

        assert len(graph.entities) == 1
        assert graph.entities[0].name == "handler.go"
        assert graph.entities[0].entity_type == "module"

    def test_creates_symbol_entities(self) -> None:
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[
                CodeSymbol(
                    name="HandleRequest",
                    symbol_type="function",
                    signature="func HandleRequest()",
                    body="...",
                    line_start=1,
                    line_end=3,
                ),
            ],
            imports=[],
        )
        graph = to_graph_data(extraction, "handler.go")

        assert len(graph.entities) == 2  # module + function
        # 심볼은 파일 범위 FQN으로 등록된다
        func_entity = next(
            e for e in graph.entities if e.name == "handler.go::HandleRequest"
        )
        assert func_entity.entity_type == "function"
        assert func_entity.description == "func HandleRequest()"

    def test_creates_import_relations(self) -> None:
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[],
            imports=["context", "fmt"],
        )
        graph = to_graph_data(extraction, "handler.go")

        assert len(graph.relations) == 2
        targets = {r.target for r in graph.relations}
        assert "context" in targets
        assert "fmt" in targets
        for r in graph.relations:
            assert r.source == "handler.go"
            assert r.relation_type == "imports"

    def test_creates_import_module_entities(self) -> None:
        """import된 모듈이 엔티티로 등록되어 save_graph_data가 관계를 resolve할 수 있다."""
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[],
            imports=["context", "fmt", "context"],  # 중복 허용 입력
        )
        graph = to_graph_data(extraction, "handler.go")

        entity_names = {e.name for e in graph.entities}
        # 파일 엔티티와 각 import 모듈이 모두 엔티티로 등록되어야 한다
        assert "handler.go" in entity_names
        assert "context" in entity_names
        assert "fmt" in entity_names

        # import 모듈 엔티티는 type="module"
        import_entities = [
            e for e in graph.entities
            if e.name in ("context", "fmt")
        ]
        assert all(e.entity_type == "module" for e in import_entities)

        # 중복 import는 한 번만 엔티티로 추가된다
        assert sum(1 for e in graph.entities if e.name == "context") == 1

        # 모든 imports 관계의 source/target이 엔티티 이름에 존재한다
        for r in graph.relations:
            if r.relation_type == "imports":
                assert r.source in entity_names
                assert r.target in entity_names

    def test_import_matching_file_title_not_duplicated(self) -> None:
        """import 이름이 파일 타이틀과 같으면 자기 자신 엔티티를 덮지 않는다."""
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[],
            imports=["handler.go"],
        )
        graph = to_graph_data(extraction, "handler.go")

        # handler.go 엔티티는 정확히 하나 (파일 엔티티)
        assert sum(1 for e in graph.entities if e.name == "handler.go") == 1

    def test_same_method_name_in_different_files_has_distinct_fqn(self) -> None:
        """서로 다른 파일의 동명 메서드는 FQN으로 구분되어야 한다.

        __init__처럼 흔한 메서드명이 graph_store의 canonical 병합으로
        전역 단일 노드로 합쳐지는 것을 방지하기 위함.
        """
        def make_extraction(file: str, class_name: str) -> CodeExtraction:
            return CodeExtraction(
                file_path=file,
                language="python",
                symbols=[
                    CodeSymbol(
                        name="__init__", symbol_type="method",
                        signature="def __init__(self)", body="...",
                        line_start=2, line_end=3,
                        parent_name=class_name,
                        parent_signature=f"class {class_name}",
                    ),
                ],
                imports=[],
            )

        graph_a = to_graph_data(
            make_extraction("user_service.py", "UserService"),
            "user_service.py",
        )
        graph_b = to_graph_data(
            make_extraction("order_service.py", "OrderService"),
            "order_service.py",
        )

        names_a = {e.name for e in graph_a.entities}
        names_b = {e.name for e in graph_b.entities}

        # FQN이 파일/클래스를 포함하므로 서로 겹치지 않아야 한다
        method_fqns = {n for n in names_a | names_b if "__init__" in n}
        assert method_fqns == {
            "user_service.py::UserService.__init__",
            "order_service.py::OrderService.__init__",
        }
        # 두 그래프의 심볼 엔티티 교집합은 비어 있어야 한다 (canonical 병합 차단)
        assert (names_a & names_b) - {""} == set()

    def test_same_method_name_in_different_classes_same_file(self) -> None:
        """같은 파일 내 서로 다른 클래스가 동명 메서드를 가질 때 구분된다."""
        extraction = CodeExtraction(
            file_path="service.py",
            language="python",
            symbols=[
                CodeSymbol(
                    name="run", symbol_type="method",
                    signature="def run(self)", body="...",
                    line_start=3, line_end=4,
                    parent_name="Worker", parent_signature="class Worker",
                ),
                CodeSymbol(
                    name="run", symbol_type="method",
                    signature="def run(self)", body="...",
                    line_start=7, line_end=8,
                    parent_name="Scheduler", parent_signature="class Scheduler",
                ),
            ],
            imports=[],
        )
        graph = to_graph_data(extraction, "service.py")

        entity_names = {e.name for e in graph.entities}
        assert "service.py::Worker.run" in entity_names
        assert "service.py::Scheduler.run" in entity_names

        # contains 관계도 각각 올바른 parent에 연결된다
        contains_pairs = {
            (r.source, r.target)
            for r in graph.relations
            if r.relation_type == "contains"
        }
        assert ("service.py::Worker", "service.py::Worker.run") in contains_pairs
        assert ("service.py::Scheduler", "service.py::Scheduler.run") in contains_pairs

    def test_go_struct_and_parent_dedup(self) -> None:
        """Go struct가 top-level symbol과 parent로 동시에 등장해도 엔티티 중복 없음."""
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[
                CodeSymbol(
                    name="Server", symbol_type="struct",
                    signature="type Server struct", body="...",
                    line_start=1, line_end=3,
                ),
                CodeSymbol(
                    name="Handle", symbol_type="method",
                    signature="func (s *Server) Handle()", body="...",
                    line_start=5, line_end=7,
                    parent_name="Server",
                    parent_signature="type Server struct",
                ),
            ],
            imports=[],
        )
        graph = to_graph_data(extraction, "handler.go")

        # Server는 정확히 한 번만 엔티티로 등록되고, symbol_type은 "struct" 유지
        server_entities = [
            e for e in graph.entities if e.name == "handler.go::Server"
        ]
        assert len(server_entities) == 1
        assert server_entities[0].entity_type == "struct"

        # contains 관계는 struct → method FQN으로 연결된다
        contains = [r for r in graph.relations if r.relation_type == "contains"]
        assert len(contains) == 1
        assert contains[0].source == "handler.go::Server"
        assert contains[0].target == "handler.go::Server.Handle"

    def test_contains_relations_for_methods(self) -> None:
        """메서드가 있으면 클래스 → 메서드 contains 관계가 FQN으로 생성된다."""
        extraction = CodeExtraction(
            file_path="service.py",
            language="python",
            symbols=[
                CodeSymbol(
                    name="run", symbol_type="method",
                    signature="def run(self)", body="...",
                    line_start=3, line_end=5,
                    parent_name="MyClass", parent_signature="class MyClass",
                ),
                CodeSymbol(
                    name="helper", symbol_type="function",
                    signature="def helper()", body="...",
                    line_start=7, line_end=9,
                ),
            ],
            imports=["os"],
        )
        graph = to_graph_data(extraction, "service.py")

        relation_types = {r.relation_type for r in graph.relations}
        assert "imports" in relation_types
        assert "contains" in relation_types

        contains_rel = next(r for r in graph.relations if r.relation_type == "contains")
        assert contains_rel.source == "service.py::MyClass"
        assert contains_rel.target == "service.py::MyClass.run"

        # 관계의 source/target은 반드시 엔티티 이름으로 resolve되어야 한다
        entity_names = {e.name for e in graph.entities}
        assert contains_rel.source in entity_names
        assert contains_rel.target in entity_names

    def test_no_calls_relations(self) -> None:
        """calls 등 LLM 전용 관계는 생성하지 않는다."""
        extraction = CodeExtraction(
            file_path="service.py",
            language="python",
            symbols=[
                CodeSymbol(
                    name="helper", symbol_type="function",
                    signature="def helper()", body="...",
                    line_start=7, line_end=9,
                ),
            ],
            imports=["os"],
        )
        graph = to_graph_data(extraction, "service.py")

        relation_types = {r.relation_type for r in graph.relations}
        assert relation_types == {"imports"}


# ---------------------------------------------------------------------------
# 통합: extract → to_chunks → to_graph_data
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """추출 → 변환 통합 테스트."""

    def test_python_end_to_end(self) -> None:
        code = textwrap.dedent("""\
            from pathlib import Path

            class FileReader:
                def read(self, path: str) -> str:
                    return Path(path).read_text()

            def main() -> None:
                reader = FileReader()
                print(reader.read("test.txt"))
        """)
        extraction = extract_code_symbols(code, "reader.py")
        chunks, embed_texts = to_chunks(extraction, "reader.py")
        graph = to_graph_data(extraction, "reader.py")

        # 심볼 2개 (FileReader.read 메서드, main 함수)
        assert len(extraction.symbols) == 2
        names = {s.name for s in extraction.symbols}
        assert "read" in names
        assert "main" in names

        # read 메서드는 FileReader의 메서드
        read_sym = next(s for s in extraction.symbols if s.name == "read")
        assert read_sym.parent_name == "FileReader"
        assert read_sym.symbol_type == "method"

        # 청크 2개
        assert len(chunks) == 2
        # 메서드 청크에 부모 클래스 정보가 포함됨
        read_chunk = next(c for c in chunks if "def read" in c.content)
        assert "# class FileReader" in read_chunk.content
        assert "reader.py > FileReader > read" == read_chunk.section_path

        # 임베딩 텍스트에 코드 본문 없음
        assert len(embed_texts) == 2
        assert not any("read_text()" in t for t in embed_texts)
        # 임베딩 텍스트에 부모 클래스 이름 포함
        read_embed = next(t for t in embed_texts if "read" in t and "main" not in t)
        assert "FileReader" in read_embed

        # 그래프: module + pathlib import + FileReader class + 2 symbols = 5 entities
        assert len(graph.entities) == 5
        # 1 import + 1 contains (FileReader→read)
        assert len(graph.relations) == 2
        assert any(r.target == "pathlib" for r in graph.relations)
        assert any(
            r.source == "reader.py::FileReader"
            and r.target == "reader.py::FileReader.read"
            and r.relation_type == "contains"
            for r in graph.relations
        )

    def test_go_end_to_end(self) -> None:
        code = textwrap.dedent("""\
            package main

            import "fmt"

            func Hello(name string) string {
                return fmt.Sprintf("Hello, %s!", name)
            }
        """)
        extraction = extract_code_symbols(code, "hello.go")
        chunks, embed_texts = to_chunks(extraction, "hello.go")
        graph = to_graph_data(extraction, "hello.go")

        assert len(extraction.symbols) >= 1
        assert any("Hello" in c.content for c in chunks)
        assert any("Hello" in t for t in embed_texts)
        assert any(r.target == "fmt" for r in graph.relations)
