---
name: eval-domain-knowledge
description: 골드셋 합성·검색 평가 시스템의 도메인 지식과 데이터 스키마 표준을 정의한다. 골드셋 평가, GoldItem 스키마, chunk vs document vs graph 채점 단위, source_type별 처리, LLM Generator/Judge 설정, 청크 사이즈 변경 강건성, build_synthetic_gold_set.py / eval_search.py / src/context_loop/eval/* 영역의 변경 작업에서 반드시 참조한다.
---

# Eval Domain Knowledge — 골드셋·평가 시스템 도메인 지식

이 스킬은 평가 시스템 작업 중 모든 에이전트가 공통으로 따라야 할 도메인 규칙과 용어를 정의한다.

## 시스템 구성

```
                ┌──────────────────────┐
                │   metadata.db        │
                │  - documents         │
                │  - chunks            │
                │  - graph_nodes       │
                │  - graph_edges       │
                └──────────┬───────────┘
                           │
       ┌───────────────────┼────────────────────┐
       ▼                   ▼                    ▼
┌──────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ build_       │  │ context_assembler│  │ eval_search.py  │
│ synthetic_   │──│ (검색·조립)      │──│ (평가 실행)      │
│ gold_set.py  │  └─────────────────┘  └─────────────────┘
│ (골드셋 생성) │                            │
└──────────────┘                            ▼
       │                              gold_set.yaml 채점
       └─────► gold_set.yaml ────────────────┘
```

## 핵심 개념

### 골드셋 (Gold Set)
- **정의**: `(query, relevant_*)` 페어의 컬렉션. 검색 시스템의 정답 셋.
- **포맷**: YAML. 스키마는 `src/context_loop/eval/gold_set.py` 의 `GoldItem`.
- **출처**: LLM 합성(`synthesized=True`) 또는 수동 작성(`synthesized=False`).

### 정답 매칭 단위
| 단위 | 키 | 강건성 (재인덱싱 시) |
|------|-----|----------|
| chunk | `chunk_id` (uuid) | ✗ 청크 사이즈 변경 시 깨짐 |
| document | `document_id` (int) | ✓ 문서 자체가 보존되면 유지 |
| text span | `text_anchor` (본문 일부) | ✓ 본문 내용이 보존되면 유지 |
| graph entity | `(entity_name, entity_type)` | ✓ 추출 로직이 동일하면 안정 |
| graph edge | `(source_entity, target_entity, relation_type)` | ✓ |

**원칙**: 골드셋은 청크 사이즈/임베딩 모델 등 인덱싱 파라미터에 **불변**이어야 한다. 즉 채점 기준은 document/text/graph 레벨이어야 한다.

### Source Type
현재 지원되는 `documents.source_type`:
- `confluence_mcp` — MCP 기반 Confluence (현 운영 표준, `ingestion/mcp_confluence.py` SOURCE_TYPE 상수).
- `confluence` — 구버전 REST API 직접 호출 (`ingestion/confluence.py`). 인덱싱 측 `pipeline.py:213` 이 두 type 을 함께 처리하므로 데이터에 따라 둘 다 존재할 수 있음.
- `git_code` — Git 저장소 코드 + 메타데이터. 멀티뷰(body + meta) 가능 (I-046 참고).
- `upload` — 사용자 업로드 파일.
- `manual` — 대시보드 에디터로 직접 작성.

**평가 관점**: 적어도 `confluence_mcp` 와 `git_code` 두 타입의 청크와 graph 컨텍스트를 모두 다룰 수 있어야 한다. 골드셋·평가 코드는 `documents.source_type` 값을 데이터에서 그대로 읽으므로 source_type 문자열에 하드코딩 의존성이 없다 — `--source-types` CLI 화이트리스트에 실제 값을 넘기면 된다.

### Generator / Judge 모델 분리
- `Generator`: 청크 본문 → 질문 N개 생성 (역방향 생성)
- `Judge`: 생성된 질문을 3단계 게이트로 필터 — answerable / no-leakage / not-generic
- **반드시 다른 모델 패밀리로 분리** — 자기 평가 편향 방지
- 설정 위치: `config.eval.generator.*`, `config.eval.judge.*`, CLI override 가능

### LLM 자동 생성 파이프라인
```
candidates = load_chunks(source_types=..., min_chars=..., max_chars=...)
sampled    = stratified_sample(candidates, n_total=N, key="source_type")
for chunk in sampled:
    qs       = generator.generate_questions(chunk.content, n=K)
    for q in qs:
        report = judge.filter(q, chunk.content, distractors=[...])
        if report.passed:
            items.append(GoldItem(query=q, relevant_doc_ids=[chunk.doc_id], ...))
save_gold_set(items)
```
- **graph context 평가**도 동일한 LLM 자동 생성 원리를 따라야 한다. 그래프 노드/엣지 또는 그 묶음에서 질문을 생성하고 Judge가 검증.

## 데이터 스키마 표준

### GoldItem 필드 표준 (요구사항 만족 후 도달해야 할 형태)

| 필드 | 타입 | 필수 | 용도 | 강건성 |
|------|------|------|------|-------|
| `id` | str | ✓ | 식별자 (q0001 등) | - |
| `query` | str | ✓ | 질의 텍스트 | - |
| `relevant_doc_ids` | list[int] | ⊙ (text/graph 중 최소 1) | 정답 문서 | ✓ |
| `relevant_graph_entities` | list[dict] | ⊙ | 정답 그래프 엔티티 (`{name, type}`) | ✓ |
| `source_type` | str | 권장 | 생성 출처 type | ✓ |
| `source_document_id` | int? | 권장 | 디버그용 출처 문서 | ✓ |
| `source_text_anchor` | str? | 권장 | 본문 일부 인용 (chunk_id 대체) | ✓ |
| `difficulty` | str | 선택 | easy/medium/hard | - |
| `synthesized` | bool | 선택 | LLM 합성 여부 | - |
| `source_chunk_id` | str? | **비권장** | 디버그 표시만, 채점 키 사용 금지 | ✗ |

- `relevant_doc_ids` 와 `relevant_graph_entities` 는 둘 중 최소 하나는 있어야 한다.
- 둘 다 있는 hybrid 질문도 허용 — 같은 질문이 chunk 검색과 graph 탐색 양쪽에서 정답을 찾을 수 있을 때.

### GraphEntityRef
```yaml
relevant_graph_entities:
  - name: "인증 서비스"
    type: "system"
  - name: "결제 팀"
    type: "team"
```
- 채점은 `(name, type)` 페어가 검색 결과 그래프 노드에 포함되는지로 판정.
- entity 병합 테이블이 있는 환경에서는 alias도 고려.

## 평가 메트릭 (참고)

`src/context_loop/eval/metrics.py` 에 정의된 표준:
- `recall@k`, `precision@k`, `mrr@k`, `ndcg@k` — chunk/doc 레벨
- `aggregate_with_variance` — 여러 골드셋의 mean/std 집계 (변동성 측정용)

**확장 시 원칙**: graph 채점 메트릭은 별도로 추가하되, 기존 chunk/doc 메트릭과 동일한 시그니처 (`results, gold_items → float`)로 일관성 유지.

## 변경 시 반드시 확인할 것

1. **backward compatibility**: 기존 `eval/*.yaml` 골드셋이 새 코드에서 로드되는가? 누락 필드는 기본값/None으로 처리되는가?
2. **테스트**: `tests/test_eval/` 의 기존 테스트가 깨지지 않는가? 새 동작에 테스트가 있는가?
3. **CLI 호환**: 기존 `--source-types`, `--seed`, `--n-gold-sets` 등 옵션이 그대로 작동하는가?
4. **재현성**: 같은 시드로 같은 입력을 주면 같은 골드셋이 생성되는가?
5. **Generator/Judge 분리**: 새 graph 평가에도 self-bias 방지가 유지되는가?

## 코딩 컨벤션 (이 도메인 한정 추가)

- 데이터 모델 변경 시 `to_dict` / `from_dict` 둘 다 일관되게 수정. YAML round-trip이 무손실.
- LLM 호출은 항상 `LLMClient` 인터페이스로 (직접 httpx 호출 금지).
- 새 CLI 옵션 추가 시 모듈 docstring 의 "사용법" 섹션에도 예시 추가.
- 한국어 docstring 유지 (이 프로젝트 컨벤션).
