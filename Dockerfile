# Prebuilt image with the review bot and all Python dependencies installed,
# so the GitLab CI/CD job can run without any pip install at job time.
#
# Build & push to the internal harbor registry:
#   docker build -t harbor-ai.kodata.co.kr/library/gitlab-review-bot:0.1.0 .
#   docker push harbor-ai.kodata.co.kr/library/gitlab-review-bot:0.1.0
#
# The build runs on a runner with no internet access, so dependencies are
# installed from the wheels vendored under wheels/ instead of PyPI (--no-index).
# Regenerate wheels/ with scripts/vendor-wheels.sh whenever requirements.txt
# or pyproject.toml dependencies change (run that script from a machine that
# does have internet access, then commit the updated wheels/ directory).
FROM harbor-ai.kodata.co.kr/library/python:3.12-slim

# Avoid interactive prompts and .pyc clutter; unbuffer stdout for CI logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (better layer caching), then the package itself.
# --no-index forces pip to use only the vendored wheels, never the network.
COPY wheels/ /tmp/wheels/
COPY requirements.txt ./
RUN pip install --no-index --find-links=/tmp/wheels/ -r requirements.txt

COPY . ./
RUN pip install --no-index --find-links=/tmp/wheels/ . \
    && rm -rf /tmp/wheels/

# Run as a non-root user.
RUN useradd --create-home --uid 10001 botuser
USER botuser

ENTRYPOINT ["gitlab-review-bot"]
