# D97 실제 데이터 고정·두 manifest 게시 — 2026-10-30

상태: **PREPARED_NOT_COMPUTED**. PR #34의 [Claude 승인](https://github.com/JJang-Ah/RichLetsGo/pull/34#issuecomment-6498231374)과 병합(`477b0e5`)을 확인한 뒤, 사전 등록된 실제 데이터를 한 번 수집하고 `paired --prepare-only`를 실행했다. 학습·확인 성과 계산은 아직 0회다. 이 manifest 게시 PR의 검토·병합을 확인한 뒤에만 계산한다.

## 고정 연결

- 등록 ID: `D97-2026-10-29`
- [사전 등록](D97-preregistration.json) 본문 SHA-256: `6fd3465c0f0752bf7a2a0331d598f72eecbad0aacc3f0ae215ff31e1c9e9a84a`
- 실행 코드: `0deebf3e5ec6ca7c1ec32f4720cfab110132eaec`. 현재 브랜치의 src·pyproject.toml이 이 코드와 같음을 확인했다. 고정된 두 TOML의 전체 파싱 값·등록 링크·해시도 재검증했다.
- 두 판정 공통 `data_hash`: `3d7ce8dc9827ebd939f6042c019e0b026eb97a4ba8de1100e1c8d40ba1cdb167`
- [주 manifest](D97-primary-manifest.json), [부 manifest](D97-secondary-manifest.json), [pair 원본](D97-pair.json)은 실행기 산출물과 바이트가 같다.
- [수집 감사 JSON](D97-collection-audit.json) 본문 해시: `0628bcb05988d56776589d2219aca66eacf6cdb89c32224ffd870ec07f98a8fd`. 수집 시도 시각·URL·시도 번호를 포함한 `ingest_runs`, 파일 해시, 건수와 검증 결과를 보존한다.

| 모드 | 캠페인 ID (`digest(identity)`) | 게시 manifest 파일 SHA-256 |
|---|---|---|
| 주 S1B | `96a61ade192fd1d1e9c6986ba8b21af61de78f0219643df57c53ab46872dff44` | `b2c7ae34481b383ecd91c460fa0c2c2a661a644d35fd1c1cae018d1d1ecf2ed0` |
| 부 S1B-S2P | `de7c9612f48185a2673b2deeb5d07797f9fa1eff4dd5034c17836fae46655f98` | `8e21f7b8d6cac4a53ba831a530f98f34da3ffa419090a260bbe794ec9b8b7d7b` |

## 수집과 무결성

수집은 **2026-10-30 18:05:52~18:08:05 KST**(09:05:52~09:08:05 UTC), 약 133.5초였다. 범위는 UTC `[2022-01-01, 2026-10-01)`, 종목은 선정된 SOL·ZEC·XRP·NEAR로 유지했다. 총 HTTP 요청 293회, 재시도 0회, 수집 실행 기록 1개다. 현재 상품·tier의 실제 captured_at를 보존했으며 과거 증거금 규칙으로 간주하지 않는다.

| 종목 | 일봉 | 1시간봉 | 펀딩 |
|---|---:|---:|---:|
| SOLUSDT | 1,734 | 41,616 | 5,563 |
| ZECUSDT | 1,734 | 41,616 | 5,203 |
| XRPUSDT | 1,734 | 41,616 | 5,203 |
| NEARUSDT | 1,734 | 41,616 | 5,203 |

수집기 16개 시리즈 모두 OK: 누락·충돌·거절·미완성 봉 제거 0건. 두 준비 manifest의 `validation=[]`, `conflicts=[]`이며 워밍업과 펀딩 look-behind 검사를 통과했다. SQLite `integrity_check=ok`. 가격 패턴이나 성과를 살펴 전략·조건을 바꾸는 작업은 하지 않았다.

준비 뒤 원본과 두 고정 스냅샷에 `verify_inputs`를 각각 적용해 공통 fingerprint를 재현했다. 두 스냅샷의 파일 해시는 모두 `32af1ea771505149d3209947bd50fea15acd134425e3fcedb21b759ba5b0d831`이다. 원본 DB의 물리 파일 해시는 `8ace095716fb60448c894b34f7530edc20db394ad89260676a5d1927be3abbf6`로 다르지만, SQLite backup의 물리 배치 차이와 무관하게 의미적 `data_hash`는 동일하다. SQLite 파일은 로컬의 설정 경로에 보존하고 Git에는 감사 기록과 manifest만 첨부한다.

## 다음 실행

이 PR이 병합되기 전에는 계산하지 않는다. 병합 확인 뒤 등록·코드·두 TOML·manifest·원본 및 스냅샷 해시를 다시 확인하고, 같은 인수에서 `--prepare-only`만 제거해 주 → 부 순서로 한 번 계산한다. 두 manifest의 `artifacts={}`, `operations=[]`를 확인했으며 아직 계산 산출물이 없다.

```powershell
python -m richletsgo.backtest.paired --primary config.d97-primary.toml --secondary config.d97-secondary.toml --commit 0deebf3e5ec6ca7c1ec32f4720cfab110132eaec
```

중단 시 같은 manifest로만 재개한다. 원본 DB·수집 이력·상품 스냅샷을 다시 수집하거나 덮어쓰지 않는다. 예산·격자·비용·판정 기준은 등록 그대로 유지하며 결과 보고서 2개는 계산 이후 별도 게시한다.
