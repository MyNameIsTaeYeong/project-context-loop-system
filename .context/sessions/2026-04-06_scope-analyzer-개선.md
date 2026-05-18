# scope_analyzer 개선 — Config 기반 상품명 → 파일 경로 자동 탐지

- **일시**: 2026-04-06
- **목적**: 상품 식별 기능을 LLM 기반에서 config 기반으로 전환. 사용자가 config에 상품명을 정의하면 시스템이 레포 전체에서 해당 상품 관련 파일 경로를 자동 탐지.

---

## 1. 배경 및 문제

### 1.1 기존 방식의 한계 (LLM 기반)
- `scope_analyzer.py`가 956줄의 LLM 기반 2-pass 분석 코드로 구성
- 대규모 레포에서 타임아웃 발생
- 레이어형 구조(`controller/vpc/`, `service/vpc/`)에서 LLM이 레이어를 상품으로 오식별
- `_detect_layered_products()` 코드 레벨 감지 추가했으나 정확도 미달
- 프롬프트 튜닝으로는 일관된 결과 보장 불가

### 1.2 결정: Config 기반 전환
- 상품명은 사용자가 가장 잘 아는 정보 → config에 직접 정의
- 시스템은 파일명 매칭만 담당 → LLM 의존 완전 제거
- 단순하고 결정적(deterministic)인 로직으로 재현성 보장

---

## 2. 새로운 아키텍처

### 2.1 Config 정의
```yaml
sources:
  git:
    repositories:
      - url: "git@github.com:company/repo.git"
        branch: "main"
        products:
          vpc:
            display_name: "VPC 서비스"
            exclude:
              - "tests/**"
              - "vendor/**"
          subnet:
            display_name: "Subnet 서비스"
```

- `products.<name>`: 상품명 (파일명 매칭 키)
- `paths`: 수동 지정 시 자동 탐지 건너뜀 (하위 호환)
- `exclude`: fnmatch glob 패턴으로 제외할 경로

### 2.2 파일명 토큰 매칭
```
vpc_controller.go → tokens: {"vpc", "controller"} → "vpc" 매칭 ✓
cloud-vpc-config.yaml → tokens: {"cloud", "vpc", "config"} → "vpc" 매칭 ✓
evpc_handler.go → tokens: {"evpc", "handler"} → "vpc" 매칭 ✗ (경계 인식)
```

- 파일명 stem을 `_`와 `-`로 분리하여 토큰화
- 토큰 단위 정확 매칭 → substring 오탐 방지

### 2.3 복수형 변형 생성
```
vpc → {vpc, vpcs}
policy → {policy, policys, policies}  (자음+y → ies)
address → {address, addresss, addresses}  (sibilant → es)
batch → {batch, batchs, batches}
box → {box, boxs, boxes}
```

### 2.4 처리 흐름
```
config yaml → product_names
  ↓
resolve_product_paths(clone_dir, product_names, extensions, exclude)
  ↓
BFS 레포 전체 순회 (1회)
  ↓ 각 파일에 대해
파일명 토큰화 → variants 매칭 → 상품별 경로 수집
  ↓
dict[상품명, list[상대경로]]
```

---

## 3. 핵심 함수

### 3.1 `_plural_variants(name)` → `set[str]`
- 상품명의 복수형 변형 생성
- 규칙: 기본 +s, 자음+y → ies, sibilant(s/sh/ch/x/z) → es

### 3.2 `_filename_matches_product(filename, variants)` → `bool`
- 파일명 stem을 `_`/`-`로 토큰화
- 토큰 집합과 variants 교집합이 있으면 매칭

### 3.3 `resolve_product_paths(clone_dir, product_names, ...)` → `dict`
- BFS로 레포 전체 순회
- skip_dirs: `.git`, `node_modules`, `vendor`, `__pycache__`, `.venv`, `venv`
- 확장자 필터, exclude 패턴 필터 적용
- 상품명 → 매칭 파일 상대 경로 리스트 반환

### 3.4 `parse_product_scopes()` (git_repository.py)
- config의 `paths`가 비어있으면 `resolve_product_paths()` 호출
- 수동 `paths` 지정 시 자동 탐지 건너뜀
- 모든 상품의 exclude 패턴을 수집하여 전달

---

## 4. 삭제된 코드

LLM 기반 상품 식별 관련 코드 **전량 삭제** (~830줄):
- `_analyze_single_pass()`, `_analyze_two_pass()`
- `_detect_layered_products()`, `_extract_product_from_filename()`
- `_collect_subtrees()`, `build_directory_tree()`
- `_parse_proposals()`, `_parse_areas()`, `_parse_single_proposal()`
- `_AreaInfo`, `ProductScopeProposal`, `ScopeAnalysisResult`
- LLM 프롬프트 템플릿 전체
- `analyze_repository_scope()` (LLM 호출 진입점)

---

## 5. 테스트 스크립트

### `scripts/run_product_paths.py`
- config yaml에서 git URL, 상품명, 확장자를 읽어 자동 실행
- 아무 파라미터 없이 실행 가능
- `clone_or_pull()` → `parse_product_scopes()` → 결과 출력
- `--products`, `--ext`, `--config` 옵션으로 오버라이드 가능

---

## 6. 변경 파일

| 파일 | 변경 내용 |
|------|----------|
| `src/context_loop/ingestion/scope_analyzer.py` | 956줄 → ~120줄 전면 재작성. LLM 코드 전량 삭제, config 기반 매칭 3함수만 유지. |
| `src/context_loop/ingestion/git_repository.py` | `parse_product_scopes()`에 `clone_dir`, `supported_extensions` 파라미터 추가. 자동 탐지 연동. |
| `tests/test_ingestion/test_scope_analyzer.py` | 973줄 → ~210줄 전면 재작성. 24개 테스트 (복수형 6 + 파일매칭 8 + 경로탐지 10). |
| `tests/test_ingestion/test_git_repository.py` | 4개 테스트 추가 (자동탐지, 수동우선, clone_dir 없음, exclude). |
| `scripts/run_product_paths.py` | 신규. Config 기반 상품 경로 탐지 실행 스크립트. |

---

## 7. 테스트 현황

- **scope_analyzer 테스트**: 24개 전체 통과
  - `TestPluralVariants` (6): 기본, 자음+y, 모음+y, sibilant s/ch/x
  - `TestFilenameMatchesProduct` (8): 토큰 경계, 복수형, 대소문자, 혼합 구분자
  - `TestResolveProductPaths` (10): 기본, 복수형, 깊은 디렉토리, 확장자 필터, skip_dirs, 오탐 방지, 다중 상품, exclude 패턴
- **git_repository 테스트**: 기존 + 4개 추가, 전체 통과
- **전체 테스트**: 366+ 통과 (기존 web 테스트 실패 제외)

---

## 8. 관련 이슈

- **I-026**: scope_analyzer 대규모 레포 타임아웃 → LLM 제거로 근본 해결
- **I-027**: 상품 식별 정확도 → config 기반 전환으로 해결

---

## 9. 다음 작업

- **9.5 Worker Agent 구현**: Level 1 파일 요약 + Level 2 디렉토리 문서 생성 (D-027)
- **9.6 Category Agent 구현**: Level 3 상품×카테고리별 관점 문서 생성 (D-027, D-028)
- **Coordinator 연동**: `parse_product_scopes()` 결과를 Coordinator에서 활용하도록 연결
- **증분 처리**: git diff 기반 변경 파일만 재탐지
