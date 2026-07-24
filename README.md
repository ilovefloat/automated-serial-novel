# 자동 연재 소설 파이프라인

Gemini API로 다음 화를 생성하고, GitHub Actions가 Markdown 파일을 커밋한 뒤
GitHub Pages에서 연재하는 최소 프로젝트입니다.

## 1. 저장소 설정

### GitHub Secret
- `GEMINI_API_KEY`: Google AI Studio에서 발급한 API 키

### GitHub Variable
- `GEMINI_MODEL`: 선택 사항
  - 지정하면 `models.list()` 결과에서 `generateContent` 지원 여부를 검증한 뒤 사용합니다.
  - 생략하면 API가 반환한 생성 가능 모델 중 선호 모델을 우선 선택하고, 없으면 자동 대체합니다.
  - 조회 결과는 `state/model_catalog.json`에 기록됩니다.

## 2. GitHub Pages 설정

Repository → Settings → Pages에서:

- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/docs`

생성된 에피소드는 `docs/episodes/`에 저장됩니다.

## 3. 실행 방식

- 자동: 매주 일요일 한국시간 오전 3시
- 수동: Actions → Generate serial episode → Run workflow
- 미리보기: `dry_run=true`로 실행하면 커밋하지 않고 Actions 로그와 artifact만 생성

GitHub Actions의 cron은 UTC입니다.
현재 설정 `0 18 * * 6`은 한국시간 일요일 03:00입니다.

## 4. 핵심 파일

- `prompts/system.md`: 작품 전체의 시스템 프롬프트
- `prompts/episode.md`: 매 화 생성 지시
- `state/story_state.json`: 인물·복선·진행 상태
- `state/continuity.md`: 사람이 직접 관리하는 설정집
- `scripts/generate_episode.py`: 사용 가능 생성 모델 탐색, Gemini 호출 및 파일 생성
- `scripts/rebuild_index.py`: 연재 목록 HTML 갱신
- `.github/workflows/generate.yml`: 주기 실행·커밋
- `docs/index.html`: 연재 페이지

## 5. 로컬 실행

```bash
python -m pip install -r requirements.txt
export GEMINI_API_KEY="..."
export GEMINI_MODEL="gemini-2.5-flash"
python scripts/generate_episode.py
```

Windows PowerShell:

```powershell
python -m pip install -r requirements.txt
$env:GEMINI_API_KEY="..."
$env:GEMINI_MODEL="gemini-2.5-flash"
python scripts/generate_episode.py
```

## 6. 안전장치

기본 코드는 다음 조건에서 게시용 파일 생성을 거부합니다.

- API 응답이 비어 있음
- 본문이 지나치게 짧음
- 에피소드 번호가 중복됨
- 필수 구분자가 누락됨
- 모델이 Markdown 코드펜스로 전체 원고를 감쌈

완전 자동 공개 전에는 수동 실행으로 몇 차례 품질을 확인하는 것이 좋습니다.
