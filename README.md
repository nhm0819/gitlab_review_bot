# gitlab_review_bot

내부 vLLM 서비스(OpenAI 호환 API)를 이용해 GitLab 머지 리퀘스트(MR)를 자동으로
리뷰하는 봇입니다. GitLab Duo(Premium/Ultimate 전용)를 사용할 수 없는 GitLab CE
환경을 위해 만들어졌으며, **외부 API를 호출하지 않고 사내 vLLM 엔드포인트만
사용**합니다.

## 동작 방식

1. GitLab CI/CD 파이프라인이 MR 이벤트(`merge_request_event`)에서 트리거됩니다.
2. 봇이 GitLab REST API로 MR의 diff를 가져옵니다.
3. diff를 내부 vLLM 서비스(`/v1/chat/completions`)에 보내 구조화된(JSON) 리뷰를 받습니다.
4. 문제가 있는 라인에는 인라인 코멘트를, 전체 요약은 MR에 노트로 등록합니다.
5. 같은 커밋(head SHA)에 대해서는 중복으로 리뷰하지 않습니다.

## 저장소 구조

```
review_bot/
├── cli.py            # 진입점: 전체 리뷰 플로우 오케스트레이션
├── config.py          # 환경변수 기반 설정 로딩
├── gitlab_client.py   # GitLab REST API 래퍼 (MR 조회, diff, 노트/디스커션 게시)
├── diff_parser.py      # unified diff 파싱 → 인라인 코멘트 가능한 라인 계산
├── exclude_rules.py    # .gitlab/review-bot.yml 예외 규칙 로딩/매칭
├── reviewer.py         # vLLM(OpenAI 호환) 호출 및 JSON 응답 파싱
└── prompts.py          # 시스템/유저 프롬프트 템플릿

.gitlab-ci.yml           # 리뷰 대상 프로젝트에 붙여 넣거나 include할 CI job 템플릿
examples/review-bot.example.yml  # 프로젝트별 예외 규칙 설정 예시
```

## 설치 및 설정

### 1. 내부 vLLM 서비스 준비

vLLM을 OpenAI 호환 서버 모드로 실행합니다. 예:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-Coder-32B-Instruct \
  --served-model-name Qwen2.5-Coder-32B-Instruct \
  --host 0.0.0.0 --port 8000
```

이 경우 엔드포인트는 `http://<host>:8000/v1` 이 되고, 모델명은
`Qwen2.5-Coder-32B-Instruct` 입니다. GitLab Runner가 이 엔드포인트에
네트워크로 접근 가능해야 합니다.

### 2. GitLab 토큰 준비

리뷰 대상 프로젝트(또는 그룹)에 `api` scope를 가진 Project/Group Access Token을
발급합니다. (Settings > Access Tokens)

### 3. 리뷰 대상 GitLab 프로젝트에 CI/CD 변수 등록

`Settings > CI/CD > Variables`에서 아래 값을 등록합니다.

| 변수명 | 필수 | 설명 |
|---|---|---|
| `GITLAB_TOKEN` | ✅ | GitLab access token (`api` scope). Masked + Protected 권장 |
| `VLLM_BASE_URL` | ✅ | 내부 vLLM 엔드포인트 (예: `http://vllm.internal.svc:8000/v1`) |
| `VLLM_MODEL` | ✅ | vLLM에 서빙된 모델명 |
| `VLLM_API_KEY` | | 게이트웨이가 키를 요구할 때만. 기본 `not-needed` |

### 4. 리뷰 대상 프로젝트의 `.gitlab-ci.yml`에 job 추가

이 저장소의 `.gitlab-ci.yml`을 통째로 include 하는 방법(권장):

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/nhm0819/gitlab_review_bot/main/.gitlab-ci.yml'
```

또는 job 정의를 직접 복사해서 사용해도 됩니다. 두 경우 모두 `pip install`이
GitHub 저장소에서 직접 패키지를 설치하므로, GitLab Runner가 `github.com`에
아웃바운드 접근이 가능해야 합니다. 사내망이라 접근이 막혀 있다면 이 저장소를
GitLab 미러/사내 패키지 레지스트리에 복제해 두고 `pip install` 경로만
바꿔주면 됩니다.

### 5. (선택) 프로젝트별 예외/커스텀 지시사항 설정

리뷰 대상 프로젝트 루트에 `.gitlab/review-bot.yml` 파일을 추가하면 특정
브랜치·작성자·경로를 리뷰에서 제외하거나, 리뷰 시 참고할 커스텀 지시사항을
넣을 수 있습니다. 예시는 [`examples/review-bot.example.yml`](examples/review-bot.example.yml)
참고.

## 로컬에서 테스트하기

```bash
git clone https://github.com/nhm0819/gitlab_review_bot.git
cd gitlab_review_bot
pip install -e .

export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
export CI_PROJECT_ID=123
export CI_MERGE_REQUEST_IID=45
export VLLM_BASE_URL=http://vllm.internal.svc.cluster.local:8000/v1
export VLLM_MODEL=Qwen2.5-Coder-32B-Instruct

python -m review_bot.cli
```

`.env.example`을 참고해 필요한 값을 채워 넣으세요.

## 설정 가능한 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VLLM_BASE_URL` | (필수) | vLLM OpenAI 호환 엔드포인트 (`/v1` 포함) |
| `VLLM_MODEL` | (필수) | 서빙된 모델명 |
| `VLLM_API_KEY` | `not-needed` | 인증이 필요한 게이트웨이일 때만 사용 |
| `VLLM_TIMEOUT` | `120` | 요청 타임아웃(초) |
| `VLLM_MAX_TOKENS` | `4096` | 응답 최대 토큰 수 |
| `VLLM_TEMPERATURE` | `0.2` | 샘플링 temperature |
| `MAX_DIFF_CHARS` | `60000` | 한 번의 리뷰 요청에 포함할 diff 총 글자수 상한 |
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
