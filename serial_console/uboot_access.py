"""
Serial console + U-Boot bootloader access for the DGS-1250-28X.

Reproducible recipe found by live experimentation against the real switch
(2026-09-11), documented here since it wasn't written down anywhere public:

1. Console is on the switch's RJ45 console port via a USB-RS232 adapter,
   115200 8N1 -- confirmed working, same settings as the Telnet CLI's banner.
2. A "warm" reboot (the `reboot` command over Telnet/CLI) does NOT go through a
   bootloader countdown an operator can interrupt -- it prints "Please wait for
   loading..." and proceeds straight through. A Ctrl+C flood at that point does
   NOT drop you into a working shell either, despite superficially looking like
   one (see next point) -- confirmed by testing: `sleep 5; echo AFTERSLEEP`
   returned instantly with no delay and no output, proving nothing was actually
   executing. What you get is character echo (CR is echoed as CRLF) with no
   command interpreter behind it -- a dead end, not the "root shell via Ctrl+C"
   some D-Link/OpenWrt community notes describe. Whether that trick requires a
   different firmware version or exact timing we don't have was never resolved;
   don't rely on it.
3. What DOES work, reliably, from a cold or warm boot: flood the '&' character
   (not Ctrl+C) starting immediately after triggering the reboot, continuing
   through "Please wait for loading...". This drops you into a real U-Boot
   prompt (`RTL9300#`) -- confirmed real by it responding "Unknown command
   '...' - try 'help'" to garbage input, and `help` listing a full real command
   set (env, mtd/flash tools, ubi/ubifs, tftpboot, etc).
4. U-Boot does NOT auto-continue booting after you've dropped to its prompt --
   it waits forever. Run `run bootcmd` (or power-cycle again) to resume a normal
   boot; don't just close the serial connection and assume it carries on, it
   won't, and the switch will sit there with no network until you do.

Flash/boot facts gathered while at the U-Boot prompt (read-only commands only,
nothing here was tested by writing to flash):
  - CPU: MIPS (U-Boot build string names a mips-switch-linux-gnu-gcc toolchain)
    -- matches OpenWrt's realtek target, not ARM.
  - MTD layout (from `bootargs` env var): nor0: 1M uboot, 512K env, 512K sys,
    remainder as a UBI-managed "fs" partition.
  - bootcmd: `mtdparts default; ubi part fs; ubifsmount fs; ubifsload 0x87000000
    uImage; bootm 0x87000000;` -- boots a ~8.8MB `uImage` (Linux 3.18.24) out of
    the UBIFS volume.
  - Inside that UBIFS volume: `/switchfs/Image1` and `/switchfs/Image2` (the
    two dual-boot application images, ~8.2MB each, matching the CLI's
    "Image1"/"Image2" boot-selection concept) plus `Config1`/`Config2` (saved
    configs) and `/root/.switch` (app state, not explored further).
  - `flshow` lists partitions (LOADER/BDINFO/SYSINFO/JFFS2_CFG/JFFS2_LOG/
    RUNTIME1/RUNTIME2) but reports bogus 0x0 sizes for all of them on this
    build -- use the bootargs mtdparts string above for real sizes instead.

Recovery value: this is a real TFTP-capable bootloader (`tftpboot`, `bootp`
commands present), independent of the application-layer firmware. If a future
firmware flash attempt goes wrong, this is the way back in -- reflash via
`tftpboot` + the appropriate flash-write command rather than being stuck.

This module only automates steps 1-4 above (getting to the prompt and running
read-only commands). It does not implement any flashing -- that's a deliberate
scope cut, do it interactively and deliberately, not via a script that could
run unattended into a mistake.
"""

from __future__ import annotations

import socket
import sys
import threading
import time

import serial


class UBootError(Exception):
    pass


class UBootConsole:
    def __init__(self, com_port: str, switch_ip: str, telnet_user: str = "admin",
                 telnet_pass: str = "admin", baudrate: int = 115200):
        self.com_port = com_port
        self.switch_ip = switch_ip
        self.telnet_user = telnet_user
        self.telnet_pass = telnet_pass
        self.baudrate = baudrate
        self.ser: serial.Serial | None = None

    def _telnet_reboot(self) -> None:
        s = socket.create_connection((self.switch_ip, 23), timeout=8)

        def tsend(line: str) -> None:
            s.sendall(line.encode() + b"\r\n")

        def trecv(wait: float = 0.5) -> bytes:
            time.sleep(wait)
            s.setblocking(False)
            data = b""
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except BlockingIOError:
                pass
            finally:
                s.setblocking(True)
            return data

        trecv(0.5)
        tsend(self.telnet_user)
        trecv(0.5)
        tsend(self.telnet_pass)
        trecv(0.8)
        tsend("reboot")
        trecv(0.5)
        tsend("y")
        s.close()

    def catch_uboot_prompt(self, timeout: float = 25.0, echo: bool = True) -> str:
        """Reboot the switch (via its existing Telnet CLI) and flood '&' until the
        U-Boot prompt appears. Returns the captured boot log. Raises UBootError if
        the prompt never showed up within `timeout` seconds."""
        self.ser = serial.Serial(self.com_port, baudrate=self.baudrate, timeout=0.05,
                                  bytesize=8, parity="N", stopbits=1)
        self.ser.read(8192)  # drain stale buffer

        got_prompt = threading.Event()
        stop_spam = threading.Event()
        buf = bytearray()
        buf_lock = threading.Lock()

        def spammer():
            while not stop_spam.is_set():
                self.ser.write(b"&")
                time.sleep(0.03)

        def reader():
            while not stop_spam.is_set() or not got_prompt.is_set():
                chunk = self.ser.read(4096)
                if chunk:
                    with buf_lock:
                        buf.extend(chunk)
                    if echo:
                        sys.stdout.write(chunk.decode("ascii", errors="replace"))
                        sys.stdout.flush()
                    if b"RTL9300#" in bytes(buf[-200:]):
                        got_prompt.set()
                        stop_spam.set()
                if got_prompt.is_set():
                    return

        t_reader = threading.Thread(target=reader, daemon=True)
        t_reader.start()

        self._telnet_reboot()

        t_spam = threading.Thread(target=spammer, daemon=True)
        t_spam.start()

        got_prompt.wait(timeout=timeout)
        stop_spam.set()
        t_spam.join(timeout=2)
        time.sleep(0.3)

        with buf_lock:
            result = bytes(buf).decode("ascii", errors="replace")
        if not got_prompt.is_set():
            raise UBootError(f"Never saw 'RTL9300#' within {timeout}s. Captured so far:\n{result}")

        # The spammer's last '&' can still be sitting unterminated in the device's
        # own input line when we stop -- flush it with a bare CR so it doesn't glue
        # onto the caller's first real command (observed: "Unknown command '&flshow'").
        self.ser.write(b"\r")
        time.sleep(0.3)
        self.ser.read(8192)
        return result

    def command(self, cmd: str, wait: float = 1.5) -> str:
        """Run one command at the U-Boot prompt and return its output. Only call
        this after `catch_uboot_prompt()` succeeded."""
        if self.ser is None:
            raise UBootError("not connected -- call catch_uboot_prompt() first")
        self.ser.read(8192)
        self.ser.write((cmd + "\r").encode())
        time.sleep(wait)
        out = b""
        while True:
            chunk = self.ser.read(4096)
            if not chunk:
                break
            out += chunk
        return out.decode("ascii", errors="replace")

    def resume_boot(self, timeout: float = 40.0) -> str:
        """U-Boot does not auto-continue -- explicitly resume normal boot via the
        bootcmd env var. Waits for the switch to come back up on the network."""
        out = self.command("run bootcmd", wait=2.0)
        deadline = time.time() + timeout
        extra = b""
        while time.time() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                extra += chunk
        return out + extra.decode("ascii", errors="replace")

    def close(self) -> None:
        if self.ser is not None:
            self.ser.close()
            self.ser = None

    def __enter__(self) -> "UBootConsole":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--com", default="COM8")
    ap.add_argument("--switch", default="10.90.90.90")
    ap.add_argument("--resume", action="store_true", help="resume normal boot afterwards")
    args = ap.parse_args()

    with UBootConsole(args.com, args.switch) as console:
        console.catch_uboot_prompt()
        print("\n\n=== flshow ===")
        print(console.command("flshow", wait=2.0))
        print("=== printenv ===")
        print(console.command("printenv", wait=2.0))
        print("=== version ===")
        print(console.command("version", wait=2.0))
        if args.resume:
            print("=== resuming normal boot ===")
            print(console.resume_boot())
