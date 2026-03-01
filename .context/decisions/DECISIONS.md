# 설계 결정 기록

결정 사항을 번호순으로 기록한다. 한번 결정된 사항은 삭제하지 않고 변경 시 새 항목으로 추가한다.

---

## D-001: 배포 형태 — CLI + 웹 UI

- **일시**: 2026-03-01
- **맥락**: Desktop App, Docker, CLI+WebUI 세 가지 후보 중 선택
- **결정**: CLI + 웹 UI (pip install 배포)
- **이유**: 부서원 기술 수준이 중간 이상이고, Python 환경이 이미 있는 경우가 많아 진입 장벽이 낮음. Streamlit/Gradio로 빠르게 프로토타이핑 가능.

---

## D-002: 인증 방식 — API Token (OAuth 대신)

- **일시**: 2026-03-01
- **맥락**: Confluence 연동 시 OAuth vs API Token
- **결정**: API Token (Basic Auth for Cloud, PAT for Data Center)
- **이유**: 서버 측 OAuth 앱 등록이 불필요. 콜백/리다이렉트 처리 없이 구현이 단순. 사용자 온보딩도 "토큰 발급 → 붙여넣기"로 간단.

---

## D-003: 토큰 저장 — OS keyring

- **일시**: 2026-03-01
- **맥락**: API 토큰을 config 파일에 평문 저장 vs keyring
- **결정**: OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service)
- **이유**: 설정 파일에 시크릿 노출 방지. keyring 라이브러리로 크로스 플랫폼 지원.
