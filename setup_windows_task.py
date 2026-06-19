"""
Signal Infomer — Windows Task Scheduler Setup

Registers TWO Windows Task Scheduler jobs that run headless (no terminal) on
weekday mornings and WhatsApp you the results:

  - SignalInfomer\\NewsPipeline   — news + AI picks   (default 07:00 IST)
  - SignalInfomer\\DailyPipeline  — technical signals (default 08:00 IST)

Times come from .env (NEWS_SCHEDULE_HOUR/MINUTE and SCHEDULE_HOUR/MINUTE).

Why each property is set the way it is:
  - Command runs through cmd.exe so ">> log 2>&1" redirection actually works.
    Task Scheduler's <Exec> has NO shell, so running python.exe with redirection
    in the arguments passes ">>"/"2>&1" as literal argv — argparse then rejects
    them and the task exits with code 2 (the historical failure).
  - WakeToRun=true + StartWhenAvailable=true. This class of laptop uses Modern
    Standby (S0 Low Power Idle): locked / screen-off is NOT classic sleep, yet
    Windows still defers Task Scheduler triggers. WakeToRun asks the OS to wake
    for the scheduled run; if the wake timer is suppressed, StartWhenAvailable
    runs it on the next wake/unlock. register_all() also enables "Allow wake
    timers" on the active power plan.
  - RestartOnFailure: retries up to 3x at 5-min intervals on a non-zero exit.
  - Runs as the current logged-in user (no password needed).

The bridge (notifications/whatsapp_bridge) is auto-started headless by the
pipelines when the first message sends, so no terminal is ever required — only
the one-time QR link.

Usage:
    python setup_windows_task.py          # register BOTH tasks
    python setup_windows_task.py --remove # unregister both
    python setup_windows_task.py --status # show status of both
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from config import (
    SCHEDULE_HOUR, SCHEDULE_MINUTE,
    NEWS_SCHEDULE_HOUR, NEWS_SCHEDULE_MINUTE,
)

PROJECT_DIR  = Path(__file__).parent.resolve()
LISTENER     = PROJECT_DIR / "research_listener.py"
LAUNCHER     = PROJECT_DIR / "run_task.py"
LOG_DIR      = PROJECT_DIR / "logs"
PYTHON_EXE   = sys.executable
# Windowless interpreter next to python.exe — every task runs through it so NO
# console window flashes on the desktop (the pipelines go via run_task.py, which
# redirects output to their log file since there's no shell to do ">> log").
PYTHONW_EXE  = str(Path(PYTHON_EXE).with_name("pythonw.exe"))
# "Allow wake timers" power setting GUID (subgroup: sleep). 1 = Enable.
_WAKE_TIMER_GUID = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"


# ── Task specifications ───────────────────────────────────────────────────────
# One entry per scheduled job. Calendar pipelines run windowless via run_task.py
# (`kind` selects which pipeline); the logon listener runs research_listener.py
# directly. The working directory is the project root so imports resolve.

TASKS = [
    {
        "name"   : "SignalInfomer\\NewsPipeline",
        "desc"   : "Signal Infomer - News + AI stock picks (sends WhatsApp)",
        "hour"   : NEWS_SCHEDULE_HOUR,
        "minute" : NEWS_SCHEDULE_MINUTE,
        "kind"   : "news",
        "log"    : "news_task_output.log",
    },
    {
        "name"   : "SignalInfomer\\DailyPipeline",
        "desc"   : "Signal Infomer - Technical market data + setup signals (sends WhatsApp)",
        "hour"   : SCHEDULE_HOUR,
        "minute" : SCHEDULE_MINUTE,
        "kind"   : "daily",
        "log"    : "task_output.log",
    },
    {
        "name"   : "SignalInfomer\\ResearchListener",
        "desc"   : "Signal Infomer - On-demand 'Search SYMBOL' WhatsApp research listener",
        "trigger": "logon",   # long-running watcher, started at logon (not a daily time)
        "py_args": f'"{LISTENER}"',
        "log"    : "research_listener.log",
    },
]


# ── XML task definition ───────────────────────────────────────────────────────

_TASK_XML = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <Author>SignalInfomer</Author>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start_date}T{hour:02d}:{minute:02d}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday/>
          <Tuesday/>
          <Wednesday/>
          <Thursday/>
          <Friday/>
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Priority>7</Priority>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <Hidden>false</Hidden>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{pythonw_exe}</Command>
      <Arguments>-u "{launcher}" {kind} "{log_path}"</Arguments>
      <WorkingDirectory>{work_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# Persistent watcher (research listener) — runs via pythonw.exe (windowless) so NO
# console window appears on the desktop while it runs forever. With no cmd shell
# there's no ">> log" redirection, so research_listener.py redirects its own
# stdout/stderr to logs/research_listener.log when started windowless.
# Persistent watcher (research listener): starts at logon and runs forever, so it
# uses a LogonTrigger (not a daily calendar time) and an UNLIMITED execution time
# limit (PT0S) — the daily-pipeline 2-hour cap would otherwise kill the loop. It
# restarts on crash and refuses to launch a second copy if one is already running.
_LOGON_TASK_XML = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <Author>SignalInfomer</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user_id}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_id}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>10</Count>
    </RestartOnFailure>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <Hidden>false</Hidden>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{pythonw_exe}</Command>
      <Arguments>-u {py_args}</Arguments>
      <WorkingDirectory>{work_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def _enable_wake_timers() -> None:
    """
    Best-effort: enable 'Allow wake timers' on the active power plan (AC + DC) so
    WakeToRun can actually wake a Modern Standby laptop for the scheduled runs.
    Does not fail registration if it can't apply (e.g. needs admin / policy).
    """
    cmds = [
        ["powercfg", "/setacvalueindex", "scheme_current", "sub_sleep", _WAKE_TIMER_GUID, "1"],
        ["powercfg", "/setdcvalueindex", "scheme_current", "sub_sleep", _WAKE_TIMER_GUID, "1"],
        ["powercfg", "/setactive", "scheme_current"],
    ]
    ok = True
    for c in cmds:
        rc, _, err = _run(c)
        if rc != 0:
            ok = False
            print(f"  [warn] could not set wake timers ({' '.join(c[1:3])}): {err.strip() or rc}")
    if ok:
        print("  [OK] 'Allow wake timers' enabled on the active power plan (AC + DC)")


def _register_one(spec: dict) -> bool:
    log_path = LOG_DIR / spec["log"]
    if spec.get("trigger") == "logon":
        domain = os.environ.get("USERDOMAIN", "")
        user   = os.environ.get("USERNAME", "")
        user_id = f"{domain}\\{user}" if domain else user
        xml_content = _LOGON_TASK_XML.format(
            description = spec["desc"],
            user_id     = user_id,
            pythonw_exe = str(PYTHONW_EXE),
            py_args     = spec["py_args"],
            work_dir    = str(PROJECT_DIR),
        )
    else:
        xml_content = _TASK_XML.format(
            description = spec["desc"],
            start_date  = datetime.now().strftime("%Y-%m-%d"),
            hour        = spec["hour"],
            minute      = spec["minute"],
            pythonw_exe = str(PYTHONW_EXE),
            launcher    = str(LAUNCHER),
            kind        = spec["kind"],
            log_path    = str(log_path),
            work_dir    = str(PROJECT_DIR),
        )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-16", suffix=".xml", delete=False
    ) as f:
        f.write(xml_content)
        xml_file = f.name

    when = ("at logon" if spec.get("trigger") == "logon"
            else f"@ {spec['hour']:02d}:{spec['minute']:02d}")
    print(f"  {spec['name']}  {when}  ->  log: {log_path.name}")
    rc, out, err = _run(["schtasks", "/Create", "/TN", spec["name"], "/XML", xml_file, "/F"])
    Path(xml_file).unlink(missing_ok=True)
    if rc != 0:
        print(f"    [FAIL] rc={rc}  {err or out}")
        return False
    return True


# ── Public actions ────────────────────────────────────────────────────────────

def register_all() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    print("\nRegistering Windows Tasks")
    print(f"  Python : {PYTHON_EXE}")
    print(f"  Dir    : {PROJECT_DIR}\n")

    results = [_register_one(spec) for spec in TASKS]

    if not all(results):
        print("\n[FAIL] One or more tasks failed to register.")
        print("If you see 'Access denied', run this script as Administrator.")
        sys.exit(1)

    print("\n[OK] All tasks registered.")
    print("\nEnabling wake timers (for Modern Standby wake at the scheduled times)...")
    _enable_wake_timers()
    print()
    print("How it works:")
    print(f"  - News pipeline   fires Mon-Fri at {NEWS_SCHEDULE_HOUR:02d}:{NEWS_SCHEDULE_MINUTE:02d}")
    print(f"  - Technical       fires Mon-Fri at {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}")
    print("  - Both run headless through cmd.exe; output -> logs/*_output.log")
    print("  - WakeToRun=true wakes the laptop even from Modern Standby (locked /")
    print("    screen-off); StartWhenAvailable catches it on next wake if suppressed")
    print("  - RestartOnFailure retries 3x at 5-min intervals")
    print("  - Each pipeline auto-starts the headless WhatsApp bridge as it sends,")
    print("    so no terminal is needed (only the one-time QR link)")
    print()
    print("Run one now to test (sends real WhatsApp messages):")
    print('  schtasks /Run /TN "SignalInfomer\\DailyPipeline"')
    print('  schtasks /Run /TN "SignalInfomer\\NewsPipeline"')


def remove_all() -> None:
    for spec in TASKS:
        rc, out, err = _run(["schtasks", "/Delete", "/TN", spec["name"], "/F"])
        print(f"  {spec['name']}: " + ("removed" if rc == 0 else f"[FAIL] {err or out}"))


def show_status() -> None:
    for spec in TASKS:
        print(f"\n=== {spec['name']} ===")
        rc, out, err = _run(["schtasks", "/Query", "/TN", spec["name"], "/V", "/FO", "LIST"])
        if rc == 0:
            for line in out.splitlines():
                for key in ("Status", "Next Run Time", "Last Run Time",
                            "Last Result", "Run As User", "Logon Mode"):
                    if line.strip().startswith(key):
                        print("  " + line.strip())
        else:
            print(f"  not found / error: {err or out}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--remove" in sys.argv:
        remove_all()
    elif "--status" in sys.argv:
        show_status()
    else:
        register_all()
