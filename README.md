# 자동 연재 소설 파이프라인

Gemini API로 한국어 장편소설의 다음 화 한 편을 생성하고, GitHub Actions가
결과를 `main` 브랜치에 커밋해 GitHub Pages의 `/docs`에서 연재하는 프로젝트다.
작품명은 **승인되지 않은 문장**이다.

## 작품과 생성 원칙

가까운 미래에 AI가 지적·창의적 노동 대부분을 맡고, 인간에게는 청소, 오염물
처리, 현장 예외 대응, 감정노동, 최종 승인과 책임이 남는다. 소설가를 꿈꾸는
하층 노동자는 낮에는 자동화 시설을 청소하고 밤에는 AI 문서에 도장을 찍으며,
AI로 자신의 일기이자 사회고발 소설을 쓴다. 독자가 읽는 외부 소설은 곧
주인공이 쓰는 내부 소설이다.

AI를 악당이나 자아를 얻은 존재로 만들지 않는다. 자동화의 방향을 정하는
수익성과 책임 전가의 경제구조가 중심 갈등이다. 세부 원칙은
`prompts/system.md`, 사람이 관리하는 연속성은 `state/continuity.md`에서
수정한다.

## 저장소 구성

- `scripts/generate_episode.py`: 모델 조회·선택, 다음 화 생성, 검증과 원자적 저장
- `scripts/rebuild_index.py`: front matter 검증, 목록과 `episodes.json` 생성
- `prompts/system.md`: 작품 전체의 고정 지침
- `prompts/episode.md`: 회차별 입력과 JSON 출력 형식
- `state/story_state.json`: 압축된 인물·사실·복선·연속성 상태
- `state/model_catalog.json`: 실행 시 조회된 생성 모델과 선택 결과
- `docs/episodes/`: 공개 Markdown 원고
- `docs/index.html`, `docs/episode.html`: GitHub Pages 연재 화면
- `.github/workflows/generate.yml`: 수동·주간 생성과 커밋
- `tests/`: API 호출이 없는 단위 테스트

프롬프트에는 최근 20편의 압축 요약, 최대 60개의 확정 사실, 현재 복선·연속성
상태와 최근 3편 원문만 전달한다. 따라서 연재가 길어져도 모든 과거 원문을
매번 전송하지 않는다.

## GitHub 설정

### 1. Gemini API 키

Repository의 **Settings → Secrets and variables → Actions → Secrets**에
다음 repository secret을 등록한다.

- 이름: `GEMINI_API_KEY`
- 값: Google AI Studio에서 발급한 Gemini API 키

워크플로는 키를 `${{ secrets.GEMINI_API_KEY }}`로만 참조하고, Python은
`GEMINI_API_KEY` 환경변수로만 읽는다. 키를 variable, 파일, 로그, artifact,
커밋 또는 Pages에 넣지 않는다.

모델을 고정하려면 같은 화면의 **Variables**에 선택 사항인
`GEMINI_MODEL`을 등록한다. `models/` 접두사는 있어도 된다.

### 2. GitHub Pages

Repository의 **Settings → Pages**에서 다음과 같이 설정한다.

- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/docs`

Actions의 자동 커밋을 허용하려면 **Settings → Actions → General →
Workflow permissions**에서 `Read and write permissions`를 선택한다. 조직
정책이 이를 막는다면 관리자에게 `GITHUB_TOKEN`의 contents 쓰기 권한을
요청해야 한다.

## 실행 방식

### 예약 실행

매주 **일요일 오전 3시(한국 표준시, KST/UTC+9)**에 한 편을 생성한다.
워크플로 cron은 UTC 기준 토요일 18시인 `0 18 * * 6`이다. GitHub Actions의
예약 작업은 부하에 따라 실제 시작이 지연될 수 있다.

정상 실행은 다음 파일을 커밋하고 `origin/main`에 push한다.

- 새 `docs/episodes/NNN.md`
- `state/story_state.json`
- `state/model_catalog.json`
- `docs/episodes.json`
- `docs/index.html`

`concurrency.group`을 고정하고 진행 중 실행을 취소하지 않아, 수동 실행과 예약
실행이 겹쳐 같은 번호를 동시에 생성하는 것을 막는다.

### 수동 실행과 preview

**Actions → Generate serial episode → Run workflow**에서 실행한다.

- `dry_run=false`: 정상 생성 후 커밋·push
- `dry_run=true`: `serial-episode-preview` artifact만 생성

preview 결과는 `preview/episodes`, `preview/story_state.json`,
`preview/model_catalog.json`에서 확인한다. 저장소 원본과 Git 이력은 바뀌지
않는다.

## 로컬 실행

Python 3.11 이상을 권장한다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:GEMINI_API_KEY = "발급한 키"
# 선택 사항
$env:GEMINI_MODEL = "gemini-2.5-flash"
python scripts/generate_episode.py --preview-dir preview
```

검토 후 실제 저장소 상태를 갱신하려면 `--preview-dir` 없이 실행하고 인덱스를
재생성한다.

```powershell
python scripts/generate_episode.py
python scripts/rebuild_index.py
```

macOS/Linux에서는 `export GEMINI_API_KEY="..."` 형식을 사용한다.

## 모델 자동 선택

매 실행마다 공식 `google-genai` SDK의 `client.models.list()`를 호출한다.
각 모델의 `supported_actions`에 `generateContent`가 명시되어 있어야 후보가
되지만, 목록에 있다는 사실만으로 실제 사용 가능하다고 판단하지 않는다.
에피소드를 생성하기 전에 후보마다 짧은 입력과 출력 토큰 1개의 저비용
`generateContent` probe를 실행하며, 실제 호출에 성공한 첫 모델만 선택한다.

기본 stable 우선순위는 다음과 같다.

1. `gemini-3.5-flash`
2. `gemini-3.5-flash-lite`
3. `gemini-3.1-flash-lite`

이 세 모델이 없거나 실제 probe에 실패하면 `gemini-flash-latest`,
`gemini-flash-lite-latest` alias를 순서대로 검사한다. 그 뒤 이름에서 식별한
다른 stable Flash 계열을 버전 내림차순으로 검사하고, 마지막에만
preview/experimental 모델을 검사한다. API의 목록 반환 순서는 선택에 영향을
주지 않는다.

이미지, TTS, robotics, computer-use, deep-research, Lyria, nano-banana,
antigravity, embedding, audio/live 계열은 자동 fallback에서 제외한다.
`GEMINI_MODEL`이 있으면 먼저 목록과 `generateContent` 지원 여부를 확인하고
probe한다. 명시 모델이 없거나 404/403/접근 불가이면 경고 후 동일한 자동
fallback을 계속한다.

로그에는 각 probe의 성공·실패 모델, 상태 코드와 비밀값을 제거한 사유를 남긴다.
`state/model_catalog.json` 또는 preview catalog에는 `listed_models`,
`generate_content_models`, `probe_succeeded_models`, `probe_failed_models`,
`selected_model`을 분리해 기록한다.

## 오류 처리

- probe의 `404 NOT_FOUND`, `403 PERMISSION_DENIED`, 모델 비호환
  `400 INVALID_ARGUMENT`는 해당 후보만 제외하고 다음 후보를 검사한다.
- `429 RESOURCE_EXHAUSTED`, timeout, 연결 오류, `500`, `502`, `503`, `504`는
  같은 후보에서 최대 3회만 지수 간격으로 재시도한다. 계속 실패하면 모델
  부재로 오판해 다른 후보를 고르지 않고 상태 코드가 포함된 오류로 종료한다.
- 잘못된 API 키와 probe로 분류할 수 없는 영구 오류는 즉시 종료한다.
- 빈 응답, JSON 오류, 짧은 본문, 코드펜스 오염, 잘못된 제목, 상태 필드·타입
  오류는 한 번만 다시 생성하며, 다시 실패하면 파일을 공개하지 않는다.
- 기존 에피소드 파일은 덮어쓰지 않는다. 상태의 `next_episode`와 실제 파일
  번호가 다르면 생성 전에 실패한다.
- 원고·상태·catalog·인덱스는 같은 디렉터리의 임시 파일을 거쳐 원자적으로
  교체한다. 생성 후 검증이 끝나기 전에는 공개 파일을 쓰지 않는다.

문제 해결 순서:

1. Actions 로그에서 `GEMINI_API_KEY 환경변수가 없습니다`가 나오면 secret의
   이름과 저장소 범위를 확인한다.
2. 명시한 `GEMINI_MODEL`의 probe가 실패하면 로그의 경고와 catalog의
   `probe_failed_models`를 확인한다. 자동 fallback은 계속 진행된다.
3. `state`와 `files` 번호 불일치는 누락·중복된 `docs/episodes/*.md`와
   `state/story_state.json`의 `next_episode`를 함께 확인한다.
4. API rate limit이 반복되면 잠시 뒤 수동 실행한다. 워크플로는 무한 재시도하지
   않는다.
5. push가 거부되면 branch protection과 Actions workflow permission을 확인한다.

## 검증

API 키가 필요 없는 검증:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python scripts/validate_repository.py
python -m py_compile scripts/generate_episode.py scripts/rebuild_index.py
```

실제 API 연결은 로컬에 키가 있을 때 preview로 짧게 확인한다. 테스트와 검증
스크립트는 API 키의 값이나 환경 전체를 출력하지 않는다.
