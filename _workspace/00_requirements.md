# 요구사항 — 문서 기반 골드셋 생성 (인덱싱 변경에 robust)

## 배경

- 현재 chunk 모드는 `load_candidate_chunks`(chunks 테이블)에서 후보를 뽑고
  `chunk["content"]`로 질의를 생성한다. chunk_size 등 인덱싱 설정이 바뀌면
  후보 청크 집합·source_text_anchor가 달라져 **재생성 재현성/추적성이 깨진다.**
- 채점은 이미 doc 단위(`relevant_doc_groups`/`relevant_doc_ids`, PR#65)라 robust.
  취약점은 **생성 단계의 청크 의존**뿐.
- 이 작업은 PR#65(answer equivalence + cross-doc) 위에 스택으로 올린다
  (`relevant_doc_groups` 스키마 사용).

## 사용자 확정 결정

1. **대체** — 기존 chunk 모드(청크 기반 생성)를 **문서 기반 생성으로 대체**한다.
   청크 기반 생성 경로는 제거/치환. (모드 추가가 아니라 교체.)
2. **통째** — 큰 문서도 **섹션 분할 없이 문서 원본 전체**를 generator 입력으로 넣는다.
   (단, generator LLM 입력 한도를 넘는 극단적 문서는 안전 가드 필요 — 무한정
   넣을 수 없으므로 designer가 한도 초과 처리 정책을 명시.)
3. **그래프 보강** — graph 모드 질의 생성 시, 1-hop 서브그래프 스니펫에 더해
   **엔티티 소유 문서의 원문(`original_content`)**도 입력으로 보강한다.

## 요구사항

### R1 — 문서 기반 후보 로딩 (chunk 모드 대체)
- `load_candidate_chunks`(chunks 테이블 의존)를 **문서 기반 로더**로 대체한다.
  - `documents.original_content`를 소스로 사용 (chunks 테이블 비의존).
  - source_type 필터, min/max 크기 필터(문자 기준)는 문서 단위로 재정의.
  - 정답은 그대로 doc 단위(`relevant_doc_ids=[doc]` + `relevant_doc_groups`).
- 질의 생성은 문서 원문 **통째**를 generator에 입력.
- distractor(유일성 게이트)는 **다른 문서** 단위로 변경 (다른 청크가 아니라).
- `source_text_anchor`는 문서 원문 기준으로 재정의(추적성 유지).

### R2 — generator 입력 한도 가드 (통째 정책의 안전장치)
- 문서 원문이 generator 입력 한도를 초과하면 어떻게 처리할지 정책 명시
  (예: skip + 통계 기록 / 앞부분 truncate). 기본 동작은 "통째"이되 극단값만 가드.

### R3 — 그래프 질의 소유 문서 원문 보강
- `_process_subgraph_item`(graph 질의 생성)에서 서브그래프 스니펫 + **primary
  소유 문서의 original_content**를 함께 generator 입력으로 제공.
- 정답·식별 스키마는 불변(노드 소유 문서 → OR 그룹). 보강은 **생성 입력에만** 영향.
- 보강도 입력 한도 가드 적용(R2와 일관).

## 비기능 요구사항

- **하위 호환**: GoldItem 스키마 불변(PR#65 그대로). 골드셋 파일 포맷 YAML 유지.
- **graph/cross_doc 모드 비침범**: 후보 로딩 외 graph/cross_doc 생성 로직은
  R3 보강 외에는 건드리지 않는다 (코드 경로 분리 확인).
- **결정성**: 문서 단위 deterministic seed 유지(재현성).
- **테스트**: 문서 기반 로더 + distractor + 한도 가드 + graph 보강 단위 테스트.
  pytest + ruff 통과. PR#65에서 확인된 선재 실패 5건은 본 작업과 무관(건드리지 않음).

## 만족 조건 (Definition of Done)

- [ ] R1: 문서 기반 로더로 chunk 모드 대체 (chunks 테이블 비의존)
- [ ] R1: doc 단위 distractor 유일성 게이트
- [ ] R2: generator 입력 한도 가드 정책 구현
- [ ] R3: graph 질의에 소유 문서 원문 보강
- [ ] 하위 호환 (스키마/포맷 불변, graph/cross_doc 비침범)
- [ ] 단위 테스트 + pytest/ruff 통과
- [ ] 변경 요약 + 새 CLI/동작 예시
