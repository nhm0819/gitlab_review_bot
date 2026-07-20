# gitlab_review_bot

Anthropic Claude API를 이용해 GitLab 머지 리퀘스트(MR)를 자동으로 리뷰하는 봇입니다.
GitLab Duo(Premium/Ultimate 전용)를 사용할 수 없는 GitLab CE 환경을 위해 만들어졌습니다.

## 동작 방식

1. GitLab CI/CD 파이프라인이 MR 이벤트(`merge_request_event`)에서 트리거됩니다.
2. 봇이 GitLab REST API로 MR의 diff를 가져옵니다.
3. diff를 Anthropic Claude API에 보내 구조화된(JSON) 리뷰를 받습니다.
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
├── reviewer.py         # Claude API 호출 및 JSON 응답 파싱
└── prompts.py          # 시스템/유저 프롬프트 템플릿

.gitlab-ci.yml           # 리뷰 대상 프로젝트에 붙여 넣거나 include할 CI job 템플릿
examples/review-bot.example.yml  # 프로젝트별 예외 규칙 설정 예시
```

## 설치 및 설정

### 1. 필요한 자격 증명 준비

- **GitLab 토큰**: 리뷰 대상 프로젝트(또는 그룹)에 `api` scope를 가진
  Project/Group Access Token을 발급합니다. (Settings > Access Tokens)
- **Anthropic API 키**: https://console.anthropic.com 에서 발급합니다.

### 2. 리뷰 대상 GitLab 프로젝트에 CI/CD 변수 등록

`Settings > CI/CD > Variables`에서 아래 두 값을 **Masked + Protected**로 등록합니다.

| 변수명 | 설명 |
|---|---|
| `GITLAB_TOKEN` | 위에서 발급한 GitLab access token (`api` scope) |
| `ANTHROPIC_API_KEY` | Anthropic API 키 |

### 3. 리뷰 대상 프로젝트의 `.gitlab-ci.yml`에 job 추가

이 저장소의 `.gitlab-ci.yml`을 통째로 include 하는 방법(권장):

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/nhm0819/gitlab_review_bot/main/.gitlab-ci.yml'
```

또는 job 정의를 직접 복사해서 사용해도 됩니다. 두 경우 모두 `pip install`이
GitHub 저장소에서 직접 패키지를 설치하므로, 리뷰 대상 GitLab Runner가
`github.com`에 아웃바운드 접근이 가능해야 합니다. 사내망이라 접근이 막혀
있다면 이 저장소를 GitLab 미러/사내 패키지 레지스트리에 복제해 두고
`pip install` 경로만 바꿔주면 됩니다.

### 4. (선택) 프로젝트별 예외/커스텀 지시사항 설정

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
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx

python -m review_bot.cli
```

`.env.example`을 참고해 필요한 값을 채워 넣으세요.

## 설정 가능한 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | 사용할 Claude 모델 |
| `MAX_DIFF_CHARS` | `60000` | 한 번의 리뷰 요청에 포함할 diff 총 글자수 상한 |
| `MAX_COMMENTS` | `25` | MR 하나당 게시할 최대 인라인 코멘트 수 |
| `POST_INLINE_COMMENTS` | `true` | 인라인 코멘트 게시 여부 |
| `POST_SUMMARY_COMMENT` | `true` | 요약 노트 게시 여부 |
| `REVIEW_BOT_CONFIG_PATH` | `.gitlab/review-bot.yml` | 예외 규칙 파일 경로 (프로젝트 루트 기준) |

## 보안 참고사항

- `GITLAB_TOKEN`과 `ANTHROPIC_API_KEY`는 반드시 CI/CD 변수(Masked, Protected)로만
  등록하고, 코드나 커밋에 절대 포함하지 마세요.
- 이 봇은 MR의 diff, 제목, 설명을 Anthropic API로 전송합니다. 민감한 코드베이스라면
  사내 정책상 외부 LLM API 호출이 허용되는지 먼저 확인하세요.
- 발급받은 토큰이 노출되었다고 판단되면 즉시 재발급(rotate)하세요.

## 라이선스

MIT License. [LICENSE](LICENSE) 참고.
