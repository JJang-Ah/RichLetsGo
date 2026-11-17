# M3-06 서비스 설치·운영 확인

이 문서의 명령은 운영자가 직접 실행한다. 구현 검증에서는 실행하지 않았다. PowerShell 7.3 이상, 운영 가상환경의 Python, 사용자가 설치한 NSSM 2.24 이상을 사용한다. NSSM 실행 파일은 저장소에 포함하지 않는다.

## 사전 준비

1. 같은 사용자 계정의 Windows 자격 증명 관리자에 기존 Trader control token과 Telegram bot 항목을 준비한다. Watchdog은 Bybit 키를 읽지 않는다. 토큰 값을 인자/파일에 넣지 않는다.
2. 기존 설정에 `config.example.toml`의 `[watchdog]` 항목을 명시한다. 경로는 설정 파일 부모 기준이다. `state_dir`는 해당 환경 전용 로컬 폴더로 둔다. 예시는 10초 점검, 120초 지속 후 알림, 900초 반복, 봉 3900초, 여유 5GiB, 600초 안 최대 5회 시작, 재시작 간격 30초다. 운용자가 확정할 운영 값이며 위험 한도는 아니다.
3. Watchdog은 `control.port`(AppConfig의 trader_port)의 `127.0.0.1 /watchdog`만 GET한다. 리디렉션·환경 프록시를 사용하지 않는다. DB는 mode=ro/query_only로 meta.environment 한 행만 확인한다. DB 장애가 Bot 경로를 막지 않는다. 주기적 감시 알림은 메모리 상태이며 Watchdog 재시작 후 지속 시간부터 다시 센다. Telegram 전달 exactly-once는 보장하지 않는다.
4. Trader와 Dashboard를 사용할 Python 환경에 패키지를 설치한다. 서비스 계정에 코드 읽기, 설정 읽기, log/state/DB/backup 폴더 권한을 준다. 시크릿 값을 로그에 붙여넣지 않는다. 저장소에 Dashboard 코드가 있어도 배포 경로를 명시하지 않으면 해당 서비스만 생략한다.

## 등록 (관리자 PowerShell)

아래 변수는 운영자가 자신의 절대 경로·계정으로 채운다. 비밀번호는 NSSM의 Log on UI에만 입력한다.

```powershell
& ./ops/windows/register.ps1 -Nssm $NssmPath -Python $VenvPython -Config $ConfigPath -Account $AccountId -ServiceUser $WindowsUser -LogDirectory $LogPath -DashboardPath $DashboardDeployPath
```

스크립트는 서비스를 disabled로 구성한 후 NSSM 편집 화면을 연다. Log on 탭에 자격 증명을 소유한 사용자를 지정하고 저장한다. StartName이 요청한 계정과 일치할 때만 자동 시작으로 전환한다. 스크립트는 서비스를 시작하지 않는다. 기존 서비스가 있으면 덮어쓰지 않는다. 세 서비스 사이 SCM 의존성은 없으며 Trader 내부 부팅·동기화 게이트가 진입을 제어한다.

서비스는 `python -m richletsgo_watchdog.service <역할> --config <절대 경로> --account <계좌>`를 실행한다. 이 래퍼는 시작 기록을 남기고 **같은 프로세스에서** 원래 모듈을 실행한다. 자식 프로세스를 띄우거나 프로세스를 종료하는 코드가 없다. NSSM 2.24에는 `AppKillProcessTree` 설정이 없으므로 기본 동작을 쓴다. 강제 종료 때 NSSM은 **그 서비스가 띄운 프로세스와 그 자식만** 끝내고, 다른 python·cmd 프로세스(D103 등)는 건드리지 않는다. 가상환경 `python.exe`는 실제 인터프리터를 자식으로 띄우는 런처이므로, 런처 PID만 끝내면 실제 Trader가 남아 재시작 때 두 개가 뜰 수 있다. 그래서 트리 종료 끄기를 쓰지 않는다(2026-11-17 Claude, NSSM 2.24 실제 등록 중 확인). 정상 종료 0과 재시작 한도 종료 78은 Exit, 다른 종료 코드는 설정 간격 후 Restart다. 공식 근거: [NSSM 사용법](https://nssm.cc/usage).

시작 횟수에는 최초 시작·운영자 재시작도 포함된다. 창 안 한도에 닿은 뒤 다음 시작은 실제 모듈을 실행하지 않고 blocked를 기록하며 78로 종료한다. 파일 오류/손상도 시작을 거절한다. 제한은 시간이 지나도 자동 해제하지 않는다. 종료 전에 DB와 무관한 Telegram sendMessage를 제한된 시간으로 시도하며 실패는 고정 문구로 stdout에 남긴다. 별도 Watchdog도 Trader/Dashboard 제한 기록을 감시한다. Watchdog 자체가 멈추거나 네트워크/봇도 실패하면 Telegram 전달을 보장할 수 없으므로 SCM 상태와 서비스 로그를 함께 확인한다.

한도 해제: 서비스가 Stopped인지 확인 → 실패 원인 조사 → 해당 역할의 `<state_dir>/<역할>.json`을 운영 기록과 함께 별도 보관 → 그 파일만 제거 → 해당 서비스 시작. 실행 중 파일을 지우거나 모든 역할 기록을 일괄 제거하지 않는다. 시작 래퍼를 SCM 밖에서 중복 실행하지 않는다.

매일 03:15 로컬 시각의 `RichLetsGo-SQLiteBackup` 작업은 동일 사용자 S4U로 기존 `richletsgo.storage backup`을 실행한다. 키/네트워크 로그온이 필요 없는 로컬 작업이다. 기존 온라인 백업·무결성 검사·14일 초과 파일 정리를 그대로 사용한다. 작업 중복은 IgnoreNew이며 missed schedule은 StartWhenAvailable이다. 수동 첫 실행 후 LastTaskResult와 실제 백업 파일을 확인한다.

## 수동 검증 (사용자·Claude, 아직 미완료)

- 등록 내용을 확인하고 Watchdog→Trader→설치한 경우 Dashboard를 시작한다. 최초 `/watchdog` ready=false가 동기화/펀딩/평가액 준비 뒤 true로 바뀌는지 기록한다. 별도 운영자 정지 유지, 확인 없는 재개/주문 없음도 확인한다.
- PC 재부팅: 같은 사용자의 자격 증명 읽기, 자동 시작, 로그, Watchdog DB 장애 알림 및 복구를 확인한다. Dashboard를 건너뛰었으면 N/A로 명시한다.
- 합성/안전한 demo에서 **대상 Trader PID를 확인해 그 PID만** 강제 종료한다. 이름/프로세스 트리 종료는 하지 않는다. 재시작 후 준비 전 진입 차단과 동기화 뒤 준비, Watchdog 알림/복구를 첨부한다. 짧은 정상 재시작은 grace 안이면 알림 없이 끝나는 것이 정상이다.
- 반복 실패 시 설정 횟수 뒤 78/Stopped 및 알림/로그를 확인한다. 시험 뒤 위 수동 해제 절차를 따른다. 실 포지션이나 D103을 시험 대상으로 삼지 않는다.
- `./ops/windows/check.ps1 -DiskPath $DataPath -TimePeer $ApprovedNtpPeer` 출력 첨부: 절전 정책, w32tm 상태/source/마지막 동기화, 지정 peer와 대략 오프셋, 유선 연결, 서비스·백업 상태, 디스크 여유. peer 생략 시 오프셋은 미측정으로 표시한다. 시스템 설정은 변경하지 않는다.
- 이 PC의 사용자 관측: Free-running System Clock, 하루 약 +1.2초, demo 로컬 clock_limit_ms=5000. PR #75 뒤 부팅 약 6초에 ready=true, Telegram 폭주 없음. 해당 관측은 24시간 또는 M3-07 PASS를 뜻하지 않는다. D107 데모 전용 계정 지정 후에도 PASS 전략 보고서 조건은 남는다.

해제는 먼저 SCM에서 대상 서비스를 정상 중지하고 확인한 뒤 `./ops/windows/unregister.ps1 -Nssm $NssmPath`를 실행한다. 데이터·로그·백업·시작 기록은 남는다.
