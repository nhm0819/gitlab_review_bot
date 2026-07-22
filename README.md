# gitlab_review_bot

내부 vLLM 서비스(OpenAI 호환 API)를 이용해 GitLab 머지 리퀘스트(MR)를 자동으로
리뷰하는 봇입니다. GitLab Duo(Premium/Ultimate 전용)를 사용할 수 없는 GitLab CE
환경을 위해 만들어졌으며, **외부 API를 호출하지 않고 사내 vLLM 엔드포인트만
사용**합니다.

## 기능

두 개의 CLI(=CI job)를 제공합니다.

| 명령 | 하는 일 |
|---|---|
| `gitlab-review-bot` | MR diff를 리뷰해 **인라인 코멘트 + 요약 노트** 게시 |
| `gitlab-mr-describe` | MR diff로 **제목/설명 자동 생성** |

## 동작 방식

1. GitLab CI/CD 파이프라인이 MR 이벤트(`merge_request_event`)에서 트리거됩니다.
2. 봇이 GitLab REST API로 MR의 diff를 가져옵니다.
3. diff를 내부 vLLM 서비스(`/v1/chat/completions`)에 보내 구조화된(JSON) 응답을 받습니다.
4. 리뷰는 인라인 코멘트 + 요약 노트로, 제목/설명은 MR 업데이트로 반영됩니다.
5. 리뷰는 같은 커밋(head SHA)에 대해 중복 실행되지 않습니다.

## 저장소 구조

```
review_bot/
├── cli.py             # 진입점(리뷰): gitlab-review-bot
├── describe_cli.py     # 진입점(제목/설명): gitlab-mr-describe + 덮어쓰기 정책
├── config.py           # 환경변수 기반 설정 로딩
├── gitlab_client.py    # GitLab REST API 래퍼 (MR 조회/수정, diff, 코멘트)
├── diff_parser.py      # unified diff 파싱 → 인라인 코멘트 가능한 라인 계산
├── exclude_rules.py    # .gitlab/review-bot.yml 예외 규칙 로딩/매칭
├── llm.py              # vLLM 공통 전송 (샘플링 파라미터, thinking 처리, JSON 파싱)
├── batching.py         # 큰 diff 처리 (우선순위 랭킹 / 잘라내기 / 배치 분할)
├── reviewer.py         # 리뷰 생성
├── describe.py         # 제목/설명 생성
└── prompts.py          # 시스템/유저 프롬프트 템플릿

.gitlab-ci.yml           # 리뷰 대상 프로젝트에 붙여 넣거나 include할 CI job 템플릿
examples/review-bot.example.yml  # 프로젝트별 예외 규칙 설정 예시
```

## 설치 및 설정

### 1. 내부 vLLM 서비스 준비

vLLM을 OpenAI 호환 서버 모드로 실행합니다. 예:

```bash
vllm serve Qwen/Qwen3.6-35B-A3B-FP8 \
  --port 8000 --tensor-parallel-size 8 \
  --max-model-len 262144 --reasoning-parser qwen3
```

엔드포인트는 `http://<host>:8000/v1`, 모델명은 `Qwen/Qwen3.6-35B-A3B-FP8`
입니다. GitLab Runner가 이 엔드포인트에 네트워크로 접근 가능해야 합니다.

`--reasoning-parser qwen3`를 권장합니다. Qwen3.6은 기본적으로 thinking
모드로 동작해 응답 앞에 `<think>...</think>` 블록을 생성하는데, 이 옵션을
주면 추론 내용이 `reasoning_content`로 분리됩니다. 옵션을 주지 않아도
봇이 `<think>` 블록을 제거하고 파싱하므로 양쪽 모두 동작합니다.

### 2. GitLab 토큰 준비

리뷰 대상 프로젝트(또는 그룹)에 `api` scope를 가진 Project/Group Access Token을
발급합니다. (Settings > Access Tokens)

### 3. 리뷰 대상 GitLab 프로젝트에 CI/CD 변수 등록

`Settings > CI/CD > Variables`에서 아래 값을 등록합니다.

| 변수명 | 필수 | 설명 |
|---|---|---|
| `GITLAB_TOKEN` | ✅ | GitLab access token (`api` scope). Masked + Protected 권장 |
| `VLLM_BASE_URL` | ✅ | 내부 vLLM 엔드포인트 (예: `http://vllm.internal.svc:8000/v1`) |
| `VLLM_MODEL` | | 서빙된 모델명. 기본 `Qwen/Qwen3.6-35B-A3B-FP8` |
| `VLLM_API_KEY` | | 게이트웨이가 키를 요구할 때만. 기본 `not-needed` |

### 4. 컨테이너 이미지 빌드 및 harbor 레지스트리에 push

CI job은 `pip install` 없이 **미리 패키지가 설치된 이미지**를 사용합니다.
저장소의 `Dockerfile`(Python 3.12 기반)로 이미지를 빌드해 사내 harbor에 올립니다.

```bash
docker build -t harbor-ai.kodata.co.kr/library/gitlab-review-bot:0.1.0 .
docker push harbor-ai.kodata.co.kr/library/gitlab-review-bot:0.1.0
```

GitLab Runner가 harbor에서 이 이미지를 pull할 수 있어야 합니다. (기존 GitLab
이미지들이 이미 harbor에서 정상 pull되고 있으므로 imagePullSecret이 구성돼
있을 가능성이 높습니다. pull이 실패하면 Runner 파드의 pull secret을 확인하세요.)

### 5. 리뷰 대상 프로젝트의 `.gitlab-ci.yml`에 job 추가

job 정의를 프로젝트 `.gitlab-ci.yml`에 복사해서 사용합니다.

```yaml
.review_bot_base: &review_bot_base
  image:
    name: harbor-ai.kodata.co.kr/library/gitlab-review-bot:0.1.0
    entrypoint: [""]
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
  variables:
    GITLAB_URL: "$CI_SERVER_URL"
  allow_failure: true

ai_code_review:
  <<: *review_bot_base
  script:
    - gitlab-review-bot

ai_mr_describe:
  <<: *review_bot_base
  script:
    - gitlab-mr-describe
```

harbor에서 이미지만 pull해서 바로 실행 → 종료합니다. MR 이벤트에서만
트리거됩니다. 이미지에 `ENTRYPOINT`가 정의돼 있으므로 `entrypoint: [""]`로
덮어써야 `script:` 블록이 정상 실행됩니다.

필요한 job만 골라서 넣어도 됩니다 (리뷰만 원하면 `ai_code_review`만).

### 6. (선택) 프로젝트별 예외/커스텀 지시사항 설정

리뷰 대상 프로젝트 루트에 `.gitlab/review-bot.yml` 파일을 추가하면 특정
브랜치·작성자·경로를 리뷰에서 제외하거나, 리뷰 시 참고할 커스텀 지시사항을
넣을 수 있습니다. 예시는 [`examples/review-bot.example.yml`](examples/review-bot.example.yml)
참고.

## MR 제목/설명 자동 생성 정책

`gitlab-mr-describe`는 **사람이 쓴 내용을 함부로 덮어쓰지 않습니다.**

| 상황 | 동작 |
|---|---|
| 제목이 아직 자동 생성 상태 (브랜치명 유래, 또는 단일 커밋 제목과 동일) | 생성된 제목으로 교체 |
| 제목을 사람이 직접 작성함 | **그대로 둠** |
| 설명이 비어있음 | 생성된 설명으로 채움 |
| 설명을 사람이 작성함 | **그대로 둠** (기본 모드) |
| `ai:describe` 라벨 또는 설명에 `/ai-describe` 포함 | 기존 설명 **뒤에 이어서** AI 섹션 추가 |

- 추가되는 섹션은 `<!-- ai-describe:start -->` / `<!-- ai-describe:end -->`
  마커로 감싸지므로, 재실행 시 **중복 추가되지 않고 해당 블록만 갱신**됩니다.
- `Draft:` / `WIP:` 접두사는 제목 교체 시에도 유지됩니다.
- 설명 생성 언어는 `DESCRIBE_LANGUAGE` (기본 `Korean`)로 바꿀 수 있습니다.
- `.gitlab/review-bot.yml`의 브랜치/작성자/경로 제외 규칙이 동일하게 적용됩니다.

## 로컬에서 테스트하기 (Python 3.12)

```bash
git clone https://github.com/nhm0819/gitlab_review_bot.git
cd gitlab_review_bot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .

export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
export CI_PROJECT_ID=123
export CI_MERGE_REQUEST_IID=45
export VLLM_BASE_URL=http://vllm.internal.svc.cluster.local:8000/v1
export VLLM_MODEL=Qwen/Qwen3.6-35B-A3B-FP8

# 리뷰 실행
python -m review_bot.cli

# MR 제목/설명 생성 실행
python -m review_bot.describe_cli
```

`.env.example`을 참고해 필요한 값을 채워 넣으세요.

## 큰 diff 처리 방식

파일이 많거나 대규모 교체가 일어나 diff가 컨텍스트를 넘으면, **남은 파일을
버리지 않고** 아래 순서로 처리합니다.

1. **우선순위 랭킹** — 소스 코드를 lockfile·minified 번들·생성 코드
   (`*.lock`, `*.min.js`, `*_pb2.py`, `vendor/`, `dist/` 등)보다 앞에 둡니다.
   점수는 추가된 줄 수 기반이되 상한이 있어서, 거대한 파일 하나가 예산을
   독차지하지 못합니다.
2. **파일 단위 잘라내기** — 파일 하나가 `MAX_FILE_DIFF_CHARS`를 넘으면
   통째로 버리는 대신 **hunk 경계에서 잘라내고** 잘렸다는 사실을 표시합니다.
3. **배치 분할 (map)** — 남은 파일들을 컨텍스트에 맞는 여러 요청으로 나눠
   각각 리뷰합니다. 인라인 코멘트는 배치별로 그대로 게시됩니다.
4. **결과 병합 (reduce)** — 배치별 요약을 한 번 더 호출해 **하나의 일관된
   요약으로 합칩니다.** 제목/설명 생성도 동일하게 배치별 노트를 만든 뒤
   최종 합성(hierarchical summarization)합니다.
5. **누락 보고** — `MAX_BATCHES`까지 써도 담지 못한 파일은 조용히 무시하지
   않고 요약 코멘트의 **Coverage** 항목에 명시합니다.

인라인 코멘트는 **모델이 실제로 본 diff**에서 계산한 줄 번호에만 달리므로,
잘린 파일에서 보지 못한 줄에 코멘트가 달리는 일은 없습니다.

한 번에 다 들어가는 크기라면 배치·병합 단계는 건너뛰므로 **불필요한 LLM
호출이 추가되지 않습니다.**

## 설정 가능한 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VLLM_BASE_URL` | (필수) | vLLM OpenAI 호환 엔드포인트 (`/v1` 포함) |
| `VLLM_MODEL` | `Qwen/Qwen3.6-35B-A3B-FP8` | 서빙된 모델명 |
| `VLLM_API_KEY` | `not-needed` | 인증이 필요한 게이트웨이일 때만 사용 |
| `VLLM_TIMEOUT` | `600` | 요청 타임아웃(초). **최소 300** — 그 이하는 300으로 올림 |
| `VLLM_MAX_TOKENS` | `32768` | 응답 최대 토큰 수 (thinking 토큰 포함) |
| `VLLM_TEMPERATURE` | `0.6` | 모델 카드의 "정밀 코딩 작업" 프리셋 |
| `VLLM_TOP_P` | `0.95` | 〃 |
| `VLLM_TOP_K` | `20` | 〃 (`extra_body`로 전달) |
| `VLLM_PRESENCE_PENALTY` | `0.0` | 〃 |
| `VLLM_ENABLE_THINKING` | `true` | `false`면 thinking 비활성화(빠름/저비용) |
| `DESCRIBE_LANGUAGE` | `Korean` | 생성되는 MR 제목/설명의 언어 |
| `MAX_DIFF_CHARS` | `200000` | **요청 1회당** diff 글자수 예산 |
| `MAX_FILE_DIFF_CHARS` | `40000` | 파일 1개당 상한. 초과 시 hunk 경계에서 잘림 |
| `MAX_BATCHES` | `8` | 최대 배치(=LLM 호출) 수. 초과분은 보고됨 |
| `MAX_COMMENTS` | `25` | MR 하나당 게시할 최대 인라인 코멘트 수 |
| `POST_INLINE_COMMENTS` | `true` | 인라인 코멘트 게시 여부 |
| `POST_SUMMARY_COMMENT` | `true` | 요약 노트 게시 여부 |
| `REVIEW_BOT_CONFIG_PATH` | `.gitlab/review-bot.yml` | 예외 규칙 파일 경로 (프로젝트 루트 기준) |

## 보안 참고사항

- `GITLAB_TOKEN`은 반드시 CI/CD 변수(Masked, Protected)로만 등록하고, 코드나
  커밋에 절대 포함하지 마세요.
- 이 봇은 MR의 diff, 제목, 설명을 내부 vLLM 서비스로만 전송하며 외부로 나가지
  않습니다. vLLM 엔드포인트가 사내망 내부에 있는지 확인하세요.
- 발급받은 토큰이 노출되었다고 판단되면 즉시 재발급(rotate)하세요.

## 라이선스

MIT License. [LICENSE](LICENSE) 참고.
