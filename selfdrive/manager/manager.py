#!/usr/bin/env python3
import datetime
import os
import signal
import subprocess
import sys
import traceback

import cereal.messaging as messaging
import selfdrive.crash as crash
from common.basedir import BASEDIR
from common.params import Params
from common.text_window import TextWindow
from selfdrive.hardware import EON, HARDWARE
from selfdrive.hardware.eon.apk import (pm_apply_packages, update_apks)
from selfdrive.manager.helpers import unblock_stdout
from selfdrive.manager.process import ensure_running
from selfdrive.manager.process_config import managed_processes
from selfdrive.registration import register
from selfdrive.swaglog import cloudlog, add_file_handler
from selfdrive.version import dirty, version


def manager_init():
  params = Params()
  params.manager_start()

  default_params = [
    ("CommunityFeaturesToggle", "0"),
    ("EndToEndToggle", "0"),
    ("CompletedTrainingVersion", "0"),
    ("IsRHD", "0"),
    ("IsMetric", "1"),
    ("RecordFront", "0"),
    ("HasAcceptedTerms", "0"),
    ("HasCompletedSetup", "0"),
    ("IsUploadRawEnabled", "1"),
    ("IsLdwEnabled", "0"),
    ("LastUpdateTime", datetime.datetime.utcnow().isoformat().encode('utf8')),
    ("OpenpilotEnabledToggle", "1"),
    ("VisionRadarToggle", "0"),
    ("IsDriverViewEnabled", "0"),
    ("IsOpenpilotViewEnabled", "0"),
    ("OpkrAutoShutdown", "2"),
    ("OpkrAutoScreenDimming", "0"),
    ("OpkrUIBrightness", "0"),
    ("OpkrUIBrightness", "0"),
    ("OpkrUIVolumeBoost", "0"),
    ("OpkrEnableDriverMonitoring", "1"),
    ("OpkrEnableLogger", "0"),
    ("OpkrEnableGetoffAlert", "1"),
    ("OpkrAutoResume", "1"),
    ("OpkrLaneChangeSpeed", "45"),
    ("OpkrAutoLaneChangeDelay", "0"),
    ("OpkrSteerAngleCorrection", "0"),
    ("PutPrebuiltOn", "0"),
    ("FingerprintIssuedFix", "0"),
    ("LdwsCarFix", "0"),
    ("LateralControlMethod", "0"),
    ("InnerLoopGain", "35"),
    ("OuterLoopGain", "20"),
    ("TimeConstant", "14"),
    ("ActuatorEffectiveness", "20"),
    ("Scale", "1750"),
    ("LqrKi", "10"),
    ("DcGain", "30"),
    ("IgnoreZone", "1"),
    ("PidKp", "20"),
    ("PidKi", "40"),
    ("PidKd", "150"),
    ("PidKf", "5"),
    ("CameraOffsetAdj", "60"),
    ("SteerRatioAdj", "150"),
    ("SteerRatioMaxAdj", "180"),
    ("SteerActuatorDelayAdj", "0"),
    ("SteerRateCostAdj", "45"),
    ("SteerLimitTimerAdj", "40"),
    ("TireStiffnessFactorAdj", "85"),
    ("SteerMaxAdj", "500"),
    ("SteerMaxBaseAdj", "385"),
    ("SteerDeltaUpAdj", "3"),
    ("SteerDeltaDownAdj", "6"),
    ("SteerMaxvAdj", "10"),
    ("OpkrBatteryChargingControl", "1"),
    ("OpkrBatteryChargingMin", "70"),
    ("OpkrBatteryChargingMax", "80"),
    ("LeftCurvOffsetAdj", "0"),
    ("RightCurvOffsetAdj", "0"),
    ("DebugUi1", "0"),
    ("DebugUi2", "0"),
    ("OpkrBlindSpotDetect", "1"),
    ("OpkrMaxAngleLimit", "90"),
    ("OpkrSpeedLimitOffset", "0"),
    ("LimitSetSpeedCamera", "0"),
    ("LimitSetSpeedCameraDist", "0"),
    ("OpkrLiveSteerRatio", "1"),
    ("OpkrVariableSteerMax", "1"),
    ("OpkrVariableSteerDelta", "0"),
    ("FingerprintTwoSet", "1"),
    ("OpkrLatMode", "0"),
    ("OpkrAccMode", "0"),
    ("OpkrCruiseGapSet", "4"),
    ("OpkrLiveTune", "0"),
    ("OpkrDrivingRecord", "0"),
    ("OpkrTurnSteeringDisable", "0"),
    ("CarModel", ""),
    ("OpkrHotspotOnBoot", "0"),
    ("OpkrSSHLegacy", "1"),
    ("ShaneFeedForward", "0"),
    ("JustDoGearD", "0"),
    ("LanelessMode", "0"),
    ("ComIssueGone", "0"),
  ]

  if params.get("RecordFrontLock", encoding='utf-8') == "1":
    params.put("RecordFront", "1")

  # set unset params
  for k, v in default_params:
    if params.get(k) is None:
      params.put(k, v)

  # is this dashcam?
  if os.getenv("PASSIVE") is not None:
    params.put("Passive", str(int(os.getenv("PASSIVE"))))

  if params.get("Passive") is None:
    raise Exception("Passive must be set to continue")

  if EON:
    update_apks()

  os.umask(0)  # Make sure we can create files with 777 permissions

  # Create folders needed for msgq
  try:
    os.mkdir("/dev/shm")
  except FileExistsError:
    pass
  except PermissionError:
    print("WARNING: failed to make /dev/shm")

  # set dongle id
  reg_res = register(show_spinner=True)
  if reg_res:
    dongle_id = reg_res
  else:
    raise Exception("server registration failed")
  os.environ['DONGLE_ID'] = dongle_id  # Needed for swaglog and loggerd

  if not dirty:
    os.environ['CLEAN'] = '1'

  cloudlog.bind_global(dongle_id=dongle_id, version=version, dirty=dirty,
                       device=HARDWARE.get_device_type())
  crash.bind_user(id=dongle_id)
  crash.bind_extra(version=version, dirty=dirty, device=HARDWARE.get_device_type())

  # ensure shared libraries are readable by apks
  if EON:
    os.chmod(BASEDIR, 0o755)
    os.chmod("/dev/shm", 0o777)
    os.chmod(os.path.join(BASEDIR, "cereal"), 0o755)
    os.chmod(os.path.join(BASEDIR, "cereal", "libmessaging_shared.so"), 0o755)


def manager_prepare():
  for p in managed_processes.values():
    p.prepare()


def manager_cleanup():
  if EON:
    pm_apply_packages('disable')

  for p in managed_processes.values():
    p.stop()

  cloudlog.info("everything is dead")


def manager_thread():
  cloudlog.info("manager start")
  cloudlog.info({"environ": os.environ})

  # save boot log
  #subprocess.call("./bootlog", cwd=os.path.join(BASEDIR, "selfdrive/loggerd"))

  ignore = []
  if os.getenv("NOBOARD") is not None:
    ignore.append("pandad")
  if os.getenv("BLOCK") is not None:
    ignore += os.getenv("BLOCK").split(",")

  if EON:
    pm_apply_packages('enable')

  ensure_running(managed_processes.values(), started=False, not_run=ignore)

  started_prev = False
  params = Params()
  sm = messaging.SubMaster(['deviceState'])
  pm = messaging.PubMaster(['managerState'])

  while True:
    sm.update()
    not_run = ignore[:]

    if sm['deviceState'].freeSpacePercent < 5:
      not_run.append("loggerd")

    started = sm['deviceState'].started
    driverview = params.get("IsDriverViewEnabled") == b"1"
    ensure_running(managed_processes.values(), started, driverview, not_run)

    # trigger an update after going offroad
    if started_prev and not started and 'updated' in managed_processes:
      os.sync()
      managed_processes['updated'].signal(signal.SIGHUP)

    started_prev = started

    running_list = ["%s%s\u001b[0m" % ("\u001b[32m" if p.proc.is_alive() else "\u001b[31m", p.name)
                    for p in managed_processes.values() if p.proc]
    cloudlog.debug(' '.join(running_list))

    # send managerState
    msg = messaging.new_message('managerState')
    msg.managerState.processes = [p.get_process_state_msg() for p in managed_processes.values()]
    pm.send('managerState', msg)

    # Exit main loop when uninstall is needed
    if params.get("DoUninstall", encoding='utf8') == "1":
      break


def main():
  prepare_only = os.getenv("PREPAREONLY") is not None

  manager_init()

  # Start ui early so prepare can happen in the background
  if not prepare_only:
    managed_processes['ui'].start()

  manager_prepare()

  if prepare_only:
    return

  # SystemExit on sigterm
  signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(1))

  try:
    manager_thread()
  except Exception:
    traceback.print_exc()
    crash.capture_exception()
  finally:
    manager_cleanup()

  if Params().get("DoUninstall", encoding='utf8') == "1":
    cloudlog.warning("uninstalling")
    HARDWARE.uninstall()


if __name__ == "__main__":
  unblock_stdout()

  try:
    main()
  except Exception:
    add_file_handler(cloudlog)
    cloudlog.exception("Manager failed to start")

    # Show last 3 lines of traceback
    error = traceback.format_exc(-3)
    error = "Manager failed to start\n\n" + error
    with TextWindow(error) as t:
      t.wait_for_exit()

    raise

  # manual exit because we are forked
  sys.exit(0)
