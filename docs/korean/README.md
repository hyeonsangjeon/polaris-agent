# Polaris Agent 한국어 안내

Polaris Agent는 프로세스 종료·절전·재부팅 이후에도 **복구 판단을 기록하고
검사할 수 있게 하는 로컬 우선 에이전트 런타임**입니다. Python CLI
`polaris`, 데몬 `polarisd`, 독립적인 Tauri macOS 운영 콘솔을 제공합니다.

> 현재 알파 버전입니다. 임의의 외부 작업에 대한 exactly-once를 보장하지
> 않습니다. 실행 여부를 확인할 수 없는 셸/원격 부작용은 자동 재시도하지
> 않고 `uncertain` 상태로 멈춰 승인을 기다립니다.

[English README](../../README.md) ·
[내구성 계약](../durability.md) ·
[보안 모델](../security.md)

## 제공 모드

- **single**: 모델과 도구를 사용하는 단일 내구성 실행
- **fan-out**: Polaris K-worker 엔진이 여러 Ollama 역할을 병렬 실행한 뒤
  검증·종합하며, 이견과 근거 산출물을 보존
- **foundry-router**: Microsoft Foundry의 `model-router` 배포를 Responses
  API로 호출하는 얇은 전략. 실제 하위 모델 선택/장애 조치는 Foundry가
  담당하며 Polaris는 `response.model`, 저널, 예산, 근거, 재생을 기록

## 3분 Ollama 빠른 시작

Python 3.11+, uv, Ollama가 필요합니다.

```bash
git clone https://github.com/hyeonsangjeon/polaris-agent.git
cd polaris-agent
uv sync --dev
ollama pull llama3.2
uv run polaris setup --root "$PWD"
```

첫 번째 터미널에서 데몬을 계속 실행합니다.

```bash
uv run polarisd
```

이 터미널을 닫으면 데몬도 종료됩니다. macOS launchd를 쓰려면
`uv run polaris daemon install` 후 `uv run polaris daemon start`를
실행합니다.

두 번째 터미널:

```bash
uv run polaris doctor
uv run polaris run "README.md의 핵심을 요약해 줘." \
  --provider ollama --call-limit 8 --token-limit 12000 --wait
uv run polaris runs
```

기본 설정은 `llama3.2`, `http://127.0.0.1:11434`, 데몬
`127.0.0.1:8765`입니다. 읽기 전용 도구는 자동 승인되지만 파일 쓰기와
셸 명령은 기본적으로 일시 정지됩니다. 운영 콘솔 또는
`GET /v1/runs/{run_id}/approvals?pending=true`에서 인자를 확인한 뒤:

```bash
uv run polaris runs --status paused
uv run polaris approve APPROVAL_ID --reason "경로와 명령을 검토함"
# 거부:
uv run polaris deny APPROVAL_ID --reason "허용 범위 밖"
```

`--wait`로 기다리는 CLI는 승인 대기 중에도 연결된 상태이므로 다른
터미널이나 운영 콘솔에서 결정해야 합니다.

## 로컬 fan-out

```bash
uv run polaris run "이 저장소의 복구 위험을 역할별로 검토해 줘." \
  --mode fan-out \
  --worker ollama:recovery \
  --worker ollama:security \
  --worker ollama:operations \
  --verifier ollama \
  --synthesizer ollama \
  --call-limit 24 --token-limit 32000 --wait
```

Ollama가 fan-out을 제공한다고 주장하지 않습니다. 작업 동시성, 고정 예산
배분, 검증·종합 단계는 Polaris 엔진이 담당합니다.

## 중단 후 복구와 재생

실행 중 데몬을 종료한 뒤 다시 `uv run polarisd`로 시작하면 만료된 lease의
복구 가능한 작업만 이어갑니다. 커밋된 단계는 다시 실행하지 않고, 활성
lease는 빼앗지 않으며, 불명확한 opaque 부작용은 승인을 기다립니다.

```bash
uv run polaris resume RUN_ID
uv run polaris replay RUN_ID
```

`replay`는 기록된 결과와 해시 산출물을 읽을 뿐 모델/도구를 다시 호출하지
않습니다. 새 실행은 비용과 부작용이 생길 수 있는 rerun입니다.

## 다음 문서

- [Ollama 설정, context/tool probe, 오프라인 프로필](../providers/ollama.md)
- [Foundry Model Router 설정](../providers/foundry-model-router.md)
- [아키텍처](../architecture.md)
- [기여 방법](../../CONTRIBUTING.md)
