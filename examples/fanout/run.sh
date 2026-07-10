#!/usr/bin/env sh
set -eu

uv run polaris run "Compare the durability risks in this repository." \
  --mode fan-out \
  --worker ollama:recovery \
  --worker ollama:security \
  --worker ollama:operations \
  --verifier ollama \
  --synthesizer ollama \
  --call-limit 24 \
  --token-limit 32000 \
  --wall-seconds-limit 900 \
  --wait
