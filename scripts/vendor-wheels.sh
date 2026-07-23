#!/usr/bin/env bash
# Regenerate wheels/ so the Dockerfile and the pytest job can
# `pip install --no-index` on an air-gapped runner. Run this from a machine WITH internet access whenever
# requirements.txt or pyproject.toml dependencies change, then commit the
# resulting wheels/ directory.
#
# Targets python:3.12-slim (Debian, x86_64, CPython 3.12). If the base image
# changes, update --platform/--python-version/--abi to match.

set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf wheels
mkdir -p wheels

# pytest is included so the air-gapped pytest job can install ".[dev]".
pip download -r requirements.txt setuptools wheel pytest -d wheels \
  --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 \
  --python-version 3.12 --implementation cp --abi cp312 \
  --only-binary=:all:

echo "Wrote $(ls wheels | wc -l) wheels to wheels/ ($(du -sh wheels | cut -f1))"
