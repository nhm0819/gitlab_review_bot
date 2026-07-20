# Prebuilt image with the review bot and all Python dependencies installed,
# so the GitLab CI/CD job can run without any pip install at job time.
#
# Build & push to the internal harbor registry:
#   docker build -t harbor-ai.kodata.co.kr/library/gitlab-review-bot:0.1.0 .
#   docker push harbor-ai.kodata.co.kr/library/gitlab-review-bot:0.1.0
FROM python:3.12-slim

# Avoid interactive prompts and .pyc clutter; unbuffer stdout for CI logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (better layer caching), then the package itself.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./
RUN pip install --no-cache-dir .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 botuser
USER botuser

ENTRYPOINT ["gitlab-review-bot"]
