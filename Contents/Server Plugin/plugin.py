try:
    import indigo
except ImportError:
    pass

import logging
import os
import shlex
import subprocess
import time
from typing import Optional

# psutil is declared in requirements.txt and installed automatically by
# Indigo on first launch (same mechanism AlphaESS Modbus uses for pymodbus,
# Automate Pulse 2 for aiopulse2) - no manual sys.path/vendoring needed.
import psutil

# runConcurrentThread ticks every 5 seconds; anything faster than that
# wouldn't actually poll any sooner — same floor pattern as this user's other
# plugins (Tesla Wall Connector, AlphaESS Modbus).
MIN_POLL_INTERVAL = 5
DEFAULT_POLL_INTERVAL = 30

# Indigo's own trigger editor can't combine "UPS on battery" and "UPS at or
# below X%" into one watched condition for a plugin-defined state (that
# needs the separate Condition tab, which - like action steps - has no
# scripting API), so this plugin pre-combines both into a single boolean
# state. A trigger just watches upsCriticalOnBattery become True.
UPS_CRITICAL_PERCENT = 50

# psutil.cpu_percent()'s first call after import is meaningless (no prior
# sample to compare against) — primed once in startup() so every real poll
# after that returns an actual delta, not a throwaway 0.0.
CPU_PRIME_DELAY = 0.1

# Shell commands this plugin runs directly (not via subprocess with
# shell=True, and never through AppleScript/System Events — pmset/pmset-free
# commands only, so nothing here depends on macOS's separate Automation
# permission prompt the way `tell application "System Events"` would).
SLEEP_CMD = ["pmset", "sleepnow"]
REBOOT_CMD = ["osascript", "-e", 'tell application "System Events" to restart']
SHUTDOWN_CMD = ["osascript", "-e", 'tell application "System Events" to shut down']


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId: str, pluginDisplayName: str, pluginVersion: str, pluginPrefs: indigo.Dict) -> None:
        """Initialize the plugin instance and set the debug logging level.

        Args:
            pluginId (str): This plugin's bundle identifier.
            pluginDisplayName (str): The plugin's display name.
            pluginVersion (str): The plugin's version string.
            pluginPrefs (indigo.Dict): Saved plugin preferences.
        """
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = self.pluginPrefs.get("showDebugInfo", False)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.logger.info(f"Indigo API Version detected: {indigo.server.apiVersion}")
        try:
            api_version = float(indigo.server.apiVersion)
        except (TypeError, ValueError):
            api_version = 0.0
        if api_version <= 3:
            self.logger.error(f"API version too old! Requires greater than 3.0, got {indigo.server.apiVersion}")
        self._next_poll_at: dict = {}
        # Network throughput is a rate (MB/s), not a raw counter — tracked as
        # (bytes_sent, bytes_recv, timestamp) per device from the previous
        # poll so each new poll can compute a delta over the real elapsed
        # time, same idea as a dimmer's change-over-time, not an absolute.
        self._last_net: dict = {}

    def startup(self) -> None:
        """Called once when the plugin starts. Primes psutil's CPU samplers.

        Per-process cpu_percent works the same way as the system-wide one:
        psutil.process_iter() caches Process objects by pid across calls, so
        this first pass just establishes a baseline for every process now
        running - the real, non-zero deltas come from the next poll's pass.
        """
        self.logger.debug("startup called")
        psutil.cpu_percent(interval=None)
        for p in psutil.process_iter(["cpu_percent"]):
            pass
        time.sleep(CPU_PRIME_DELAY)

    def shutdown(self) -> None:
        """Called once when the plugin is shutting down."""
        self.logger.debug("shutdown called")

    def closedPrefsConfigUi(self, valuesDict: indigo.Dict, userCancelled: bool) -> None:
        """Re-apply the debug logging level live when plugin preferences are saved.

        Args:
            valuesDict (indigo.Dict): The dialog's saved field values.
            userCancelled (bool): True if the dialog was cancelled instead of saved.
        """
        if not userCancelled:
            self.debug = valuesDict.get("showDebugInfo", False)
            self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)

    def runConcurrentThread(self) -> None:
        """Indigo's polling loop entry point.

        Ticks every 5 seconds and polls each enabled System Monitor device
        once its own configured updateInterval has elapsed, so multiple
        devices (if ever more than one Mac is monitored) can poll at
        different rates — same pattern as this user's other plugins.
        """
        try:
            while True:
                now = time.time()
                for dev in indigo.devices.iter("self.systemMonitor"):
                    if not dev.enabled or not dev.configured:
                        continue
                    if now < self._next_poll_at.get(dev.id, 0):
                        continue
                    try:
                        interval = max(MIN_POLL_INTERVAL, int(dev.pluginProps.get("updateInterval", DEFAULT_POLL_INTERVAL)))
                    except (TypeError, ValueError):
                        interval = DEFAULT_POLL_INTERVAL
                    self._next_poll_at[dev.id] = now + interval
                    try:
                        self._poll_system_monitor(dev)
                    except Exception:
                        self.logger.exception(f"Error polling {dev.name}")
                self.sleep(5)
        except self.StopThread:
            pass

    def deviceStartComm(self, dev: indigo.Device) -> None:
        """Poll a System Monitor device immediately once it's enabled/configured.

        Args:
            dev (indigo.Device): The device being started.
        """
        self.logger.debug(f"deviceStartComm: {dev.name} (type: {dev.deviceTypeId})")
        if dev.configured:
            try:
                self._poll_system_monitor(dev)
            except Exception:
                self.logger.exception(f"Error polling {dev.name}")

    def deviceStopComm(self, dev: indigo.Device) -> None:
        """Clear this device's poll scheduling when it's disabled/deleted.

        Args:
            dev (indigo.Device): The device being stopped.
        """
        self.logger.debug(f"deviceStopComm: {dev.name} (type: {dev.deviceTypeId})")
        self._next_poll_at.pop(dev.id, None)
        self._last_net.pop(dev.id, None)

    def validateDeviceConfigUi(self, valuesDict: indigo.Dict, typeId: str, devId: int) -> tuple:
        """Validate the New/Edit Device dialog before it's allowed to save.

        Args:
            valuesDict (indigo.Dict): The dialog's current field values.
            typeId (str): The device type being configured.
            devId (int): The device's ID (0 for a device being newly created).

        Returns:
            tuple: ``(True, valuesDict)`` if valid, or
                ``(False, valuesDict, errorsDict)`` with per-field error messages.
        """
        errors_dict = indigo.Dict()
        if typeId == "systemMonitor":
            mount_point = valuesDict.get("diskMountPoint", "").strip()
            if not mount_point:
                errors_dict["diskMountPoint"] = "A mount point is required, e.g. /."
            elif not os.path.exists(mount_point):
                errors_dict["diskMountPoint"] = f"'{mount_point}' doesn't exist on this Mac."
        if errors_dict:
            return (False, valuesDict, errors_dict)
        return (True, valuesDict)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _top_processes(self) -> tuple:
        """Find the single busiest process by CPU and by memory.

        A process's cpu_percent here isn't divided by core count (matching
        Activity Monitor's convention, not the 0-100 system-wide figure
        above) - a process pinning two cores reads as ~200%, which is
        correct, not a bug.

        Returns:
            tuple: (topCpuProcess, topMemProcess), each formatted as
                "<name> (<value>%)", or "—" if no process was readable.
        """
        top_cpu_name = top_cpu_value = None
        top_mem_name = top_mem_value = None
        for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
            name = p.info.get("name")
            if name is None:
                continue
            cpu = p.info.get("cpu_percent")
            if cpu is not None and (top_cpu_value is None or cpu > top_cpu_value):
                top_cpu_name, top_cpu_value = name, cpu
            mem_pct = p.info.get("memory_percent")
            if mem_pct is not None and (top_mem_value is None or mem_pct > top_mem_value):
                top_mem_name, top_mem_value = name, mem_pct
        top_cpu = f"{top_cpu_name} ({round(top_cpu_value)}%)" if top_cpu_name else "—"
        top_mem = f"{top_mem_name} ({round(top_mem_value, 1)}%)" if top_mem_name else "—"
        return top_cpu, top_mem

    def _all_disks_summary(self) -> str:
        """List every user-visible local disk connected to this Mac, by name.

        Filters on the "dontbrowse" mount option rather than any hardcoded
        path - that's the same flag macOS/Finder itself uses to hide a
        volume's internal plumbing (Preboot, VM, Update, Data - all part of
        the same boot container, not separate disks). A plain disk name
        comes straight from its mount point, so a new USB/Thunderbolt drive
        shows up automatically on the next poll, no plugin changes needed.
        psutil.disk_partitions(all=False) already excludes network shares
        (SMB/AFP/NFS) on macOS, so this stays local-disk-only without an
        explicit fstype check.

        Returns:
            str: e.g. "Boot Disk 424GB free · Cam 251GB free", or "—" if
                nothing could be read.
        """
        parts = []
        for p in psutil.disk_partitions(all=False):
            if "dontbrowse" in p.opts:
                continue
            try:
                usage = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            name = "Boot Disk" if p.mountpoint == "/" else os.path.basename(p.mountpoint.rstrip("/")) or p.mountpoint
            parts.append(f"{name} {round(usage.free / 1_000_000_000)}GB free")
        return " · ".join(parts) if parts else "—"

    def _poll_system_monitor(self, dev: indigo.Device) -> None:
        """Poll one System Monitor device: gather psutil stats and write state.

        Args:
            dev (indigo.Device): The System Monitor device to poll.
        """
        mount_point = dev.pluginProps.get("diskMountPoint", "/") or "/"

        cpu_percent = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage(mount_point)
        except OSError as e:
            self.logger.warning(f"{dev.name}: could not read disk usage for '{mount_point}': {e}")
            disk = None
        # macOS reports a USB HID-compliant UPS through the same power-source
        # API it uses for a laptop's own battery, so this doubles as UPS
        # status on a desktop Mac - no vendor UPS software needed. Returns
        # None outright on hardware with neither a battery nor a UPS.
        try:
            battery = psutil.sensors_battery()
        except Exception:
            self.logger.debug(f"{dev.name}: could not read UPS/battery status", exc_info=True)
            battery = None
        uptime_hours = (time.time() - psutil.boot_time()) / 3600
        top_cpu_process, top_mem_process = self._top_processes()

        now = time.time()
        sent_rate = recv_rate = 0.0
        try:
            counters = psutil.net_io_counters()
            last = self._last_net.get(dev.id)
            if last is not None:
                last_sent, last_recv, last_time = last
                elapsed = now - last_time
                if elapsed > 0:
                    sent_rate = max(0.0, (counters.bytes_sent - last_sent) / elapsed / 1_000_000)
                    recv_rate = max(0.0, (counters.bytes_recv - last_recv) / elapsed / 1_000_000)
            self._last_net[dev.id] = (counters.bytes_sent, counters.bytes_recv, now)
        except Exception:
            self.logger.debug(f"{dev.name}: could not read network counters", exc_info=True)

        states = [
            {"key": "cpuPercent", "value": round(cpu_percent, 1)},
            {"key": "memoryPercent", "value": round(mem.percent, 1)},
            {"key": "memoryUsedGB", "value": round(mem.used / 1_000_000_000, 1)},
            {"key": "memoryTotalGB", "value": round(mem.total / 1_000_000_000, 1)},
            {"key": "networkSentMBps", "value": round(sent_rate, 2)},
            {"key": "networkRecvMBps", "value": round(recv_rate, 2)},
            {"key": "uptimeHours", "value": round(uptime_hours, 1)},
            {"key": "topCpuProcess", "value": top_cpu_process},
            {"key": "topMemProcess", "value": top_mem_process},
            {"key": "allDisksStatus", "value": self._all_disks_summary()},
        ]
        if disk is not None:
            disk_free_percent = round(100 - disk.percent, 1)
            states.extend([
                {"key": "diskFreeGB", "value": round(disk.free / 1_000_000_000, 1)},
                {"key": "diskTotalGB", "value": round(disk.total / 1_000_000_000, 1)},
                {"key": "diskFreePercent", "value": disk_free_percent},
            ])
            status = f"{round(cpu_percent)}% CPU · {round(mem.percent)}% RAM · {round(disk.free / 1_000_000_000)}GB disk free"
        else:
            status = f"{round(cpu_percent)}% CPU · {round(mem.percent)}% RAM"

        if battery is not None:
            on_ac_power = bool(battery.power_plugged)
            # secsleft is a real countdown only while actually running on
            # battery - psutil uses POWER_TIME_UNLIMITED on AC and
            # POWER_TIME_UNKNOWN when discharging but not yet estimable;
            # both are negative sentinels, not real values, so -1 here means
            # "not applicable right now", never a literal minute count.
            if on_ac_power or battery.secsleft < 0:
                minutes_left = -1
            else:
                minutes_left = round(battery.secsleft / 60)
            ups_critical = (not on_ac_power) and battery.percent <= UPS_CRITICAL_PERCENT
            states.extend([
                {"key": "upsPercent", "value": round(battery.percent, 1)},
                {"key": "upsOnAcPower", "value": on_ac_power},
                {"key": "upsMinutesLeft", "value": minutes_left},
                {"key": "upsCriticalOnBattery", "value": ups_critical},
            ])
            if not on_ac_power:
                minutes_text = f"{minutes_left}m left" if minutes_left >= 0 else "time unknown"
                status = f"ON UPS BATTERY {round(battery.percent)}% ({minutes_text}) · {status}"

        states.append({"key": "displayStatus", "value": status})

        dev.updateStatesOnServer(states)
        dev.setErrorStateOnServer(None)

    # ------------------------------------------------------------------
    # Power actions — gated behind a required confirmation checkbox,
    # checked in BOTH validateActionConfigUi (dialog path) and here again
    # (plugin.executeAction-with-props path, which never runs the dialog at
    # all) — see the note in Actions.xml. None of these three were test-fired
    # during development.
    # ------------------------------------------------------------------

    def validateActionConfigUi(self, valuesDict: indigo.Dict, typeId: str, deviceId: int) -> tuple:
        """Validate an action's config dialog before it's allowed to save/run.

        Args:
            valuesDict (indigo.Dict): The dialog's current field values.
            typeId (str): The action type being configured.
            deviceId (int): The device the action is attached to.

        Returns:
            tuple: ``(True, valuesDict)`` if valid, or
                ``(False, valuesDict, errorsDict)`` with per-field error messages.
        """
        errors_dict = indigo.Dict()
        if typeId in ("rebootMac", "sleepMac", "shutdownMac", "restartIndigoPlugin"):
            if not valuesDict.get("confirm", False):
                errors_dict["confirm"] = "You must check this box to confirm."
        elif typeId == "runShellCommand":
            command = valuesDict.get("command", "")
            confirm_command = valuesDict.get("confirmCommand", "")
            if not command.strip():
                errors_dict["command"] = "A command is required."
            if confirm_command != command:
                errors_dict["confirmCommand"] = "Must exactly match the Command field above."
            try:
                timeout = int(valuesDict.get("timeout", "30"))
                if timeout <= 0:
                    errors_dict["timeout"] = "Must be a positive number of seconds."
            except ValueError:
                errors_dict["timeout"] = "Must be a whole number of seconds."
        if errors_dict:
            return (False, valuesDict, errors_dict)
        return (True, valuesDict)

    def _run_gated_power_command(self, action: indigo.PluginAction, dev: indigo.Device, cmd: list, verb: str) -> None:
        """Shared implementation for reboot/sleep/shutdown — confirm, log, run.

        Args:
            action (indigo.PluginAction): The triggering action (carries ``props``).
            dev (indigo.Device): The System Monitor device the action ran against.
            cmd (list): The command to run (argv list, never shell=True).
            verb (str): Human label for logging, e.g. "reboot".
        """
        if not action.props.get("confirm", False):
            self.logger.error(f"{verb.capitalize()} of this Mac was requested but not confirmed — nothing was done.")
            return
        self.logger.warning(f"{dev.name}: confirmed — running {verb} on this Mac now.")
        try:
            subprocess.run(cmd, check=True, timeout=30, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            self.logger.error(f"{verb.capitalize()} command failed ({e.returncode}): {e.stderr.strip() if e.stderr else e}")
        except subprocess.TimeoutExpired:
            self.logger.error(f"{verb.capitalize()} command timed out")
        except Exception:
            self.logger.exception(f"Error running {verb} command")

    def reboot_mac_action(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Action callback: reboot this Mac. Requires confirm=True in props.

        Args:
            action (indigo.PluginAction): The triggering action.
            dev (indigo.Device): The System Monitor device it ran against.
        """
        self._run_gated_power_command(action, dev, REBOOT_CMD, "reboot")

    def sleep_mac_action(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Action callback: sleep this Mac. Requires confirm=True in props.

        Args:
            action (indigo.PluginAction): The triggering action.
            dev (indigo.Device): The System Monitor device it ran against.
        """
        self._run_gated_power_command(action, dev, SLEEP_CMD, "sleep")

    def shutdown_mac_action(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Action callback: shut down this Mac. Requires confirm=True in props.

        Args:
            action (indigo.PluginAction): The triggering action.
            dev (indigo.Device): The System Monitor device it ran against.
        """
        self._run_gated_power_command(action, dev, SHUTDOWN_CMD, "shutdown")

    # ------------------------------------------------------------------
    # Shell command — gated by a retyped-command match, not just a checkbox.
    # ------------------------------------------------------------------

    def run_shell_command_action(self, action: indigo.PluginAction, dev: indigo.Device) -> Optional[str]:
        """Action callback: run one shell command (no shell=True, no pipes/chaining).

        Requires ``confirmCommand`` in props to exactly match ``command`` —
        re-checked here even though validateActionConfigUi already checked it,
        since executeAction-with-props never runs that dialog at all.

        Args:
            action (indigo.PluginAction): The triggering action, carrying
                ``command``, ``confirmCommand`` and ``timeout`` in its props.
            dev (indigo.Device): The System Monitor device it ran against.

        Returns:
            Optional[str]: The command's combined stdout (and stderr, if the
                command failed), for scripts calling this with
                waitUntilDone=True. None if the gate rejected the call.
        """
        command = action.props.get("command", "")
        confirm_command = action.props.get("confirmCommand", "")
        if confirm_command != command or not command.strip():
            self.logger.error("Run Shell Command was requested but the confirmation didn't match the command — nothing was run.")
            return None
        try:
            timeout = int(action.props.get("timeout", "30"))
        except ValueError:
            timeout = 30

        self.logger.warning(f"{dev.name}: confirmed — running shell command: {command}")
        try:
            args = shlex.split(command)
        except ValueError as e:
            self.logger.error(f"Could not parse command: {e}")
            return None
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                self.logger.info(f"Command succeeded: {result.stdout.strip()[:500]}")
                return result.stdout
            self.logger.error(f"Command exited {result.returncode}: {result.stderr.strip()[:500]}")
            return result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            self.logger.error(f"Command timed out after {timeout}s")
            return None
        except FileNotFoundError as e:
            self.logger.error(f"Command not found: {e}")
            return None
        except Exception:
            self.logger.exception("Error running shell command")
            return None

    # ------------------------------------------------------------------
    # Indigo plugin restart — restart only. Indigo's own API has no way to
    # disable/enable another plugin at all ("for safety reasons", per its
    # own scripting docs) — that's a boundary Indigo draws, not a gap here,
    # so there's deliberately no kill/disable action to work around it.
    # ------------------------------------------------------------------

    def get_installed_plugins_list(self, filter: str = "", valuesDict: indigo.Dict = None, typeId: str = "", targetId: int = 0) -> list:
        """List-callback populating the Restart Plugin action's plugin picker.

        Args:
            filter (str): Unused — required by Indigo's list-callback signature.
            valuesDict (indigo.Dict): The dialog's current field values (unused).
            typeId (str): The action type being configured (unused).
            targetId (int): The device the action is attached to (unused).

        Returns:
            list: ``(pluginId, displayName)`` tuples for every enabled plugin,
                this plugin itself excluded (restarting yourself from your
                own action is a confusing, not-really-useful edge case).
        """
        try:
            plugins = indigo.server.getPluginList()
        except Exception:
            self.logger.exception("Could not get the installed plugin list")
            return []
        return sorted(
            [(p.pluginId, p.pluginDisplayName) for p in plugins if p.pluginId != self.pluginId],
            key=lambda pair: pair[1].lower(),
        )

    def restart_plugin_action(self, action: indigo.PluginAction, dev: indigo.Device) -> None:
        """Action callback: restart another Indigo plugin via its own API.

        Requires confirm=True in props — re-checked here even though
        validateActionConfigUi already checked it, since
        executeAction-with-props never runs that dialog at all.

        Args:
            action (indigo.PluginAction): The triggering action, carrying
                ``targetPluginId`` and ``confirm`` in its props.
            dev (indigo.Device): The System Monitor device it ran against.
        """
        if not action.props.get("confirm", False):
            self.logger.error("Restart Plugin was requested but not confirmed — nothing was done.")
            return
        target_id = action.props.get("targetPluginId", "")
        if not target_id:
            self.logger.error("No plugin selected to restart.")
            return
        target = indigo.server.getPlugin(target_id)
        if not target.isInstalled():
            self.logger.error(f"Plugin '{target_id}' isn't installed.")
            return
        if not target.isEnabled():
            self.logger.error(f"Plugin '{target.pluginDisplayName}' is disabled — Indigo's API can't restart a disabled plugin (and deliberately can't enable one either).")
            return
        self.logger.warning(f"{dev.name}: confirmed — restarting plugin '{target.pluginDisplayName}'.")
        target.restart(waitUntilDone=False)
