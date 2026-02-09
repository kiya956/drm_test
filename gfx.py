#!/usr/bin/env python3
"""
gfx_diag.py — Split test flow by nomodeset

Flow A (nomodeset):
  - Validate fbdev / firmware framebuffer path (/dev/fb0, efifb/simplefb/vesafb)
  - Optionally collect DRM render-only info as INFO (not required for display)

Flow B (normal):
  - Device registered -> driver bound -> DRM sysfs -> /dev/dri -> connectors/EDID/modes
  - Logs for link training / vblank/pageflip / power
  - Optional tools in --deep (modetest/kmsprint/drm_info)

Run:
  python3 gfx_diag.py
  sudo python3 gfx_diag.py --deep --expect-kms
"""

from __future__ import annotations
import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ------------------------- helpers -------------------------

def run(cmd: List[str], timeout: int = 10) -> Tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return p.returncode, p.stdout.strip()
    except Exception as e:
        return 127, f"<failed to run {cmd}: {e}>"

def read_text(path: Path, max_bytes: int = 200_000) -> Optional[str]:
    try:
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[:max_bytes] + b"\n<...truncated...>\n"
        return data.decode(errors="replace").strip()
    except Exception:
        return None

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def bullet(k: str, v: str) -> str:
    return f"- {k}: {v}"


def is_root() -> bool:
    return os.geteuid() == 0

def grep_lines(text: str, patterns: List[str], max_hits: int = 80) -> List[str]:
    hits: List[str] = []
    for line in text.splitlines():
        for pat in patterns:
            if re.search(pat, line, re.IGNORECASE):
                hits.append(line)
                break
        if len(hits) >= max_hits:
            hits.append("<...more matches truncated...>")
            break
    return hits

def parse_cmdline() -> Dict[str, str]:
    cmdline = read_text(Path("/proc/cmdline")) or ""
    tokens = cmdline.split()
    out: Dict[str, str] = {}
    for t in tokens:
        if "=" in t:
            k, v = t.split("=", 1)
            out[k] = v
        else:
            out[t] = "1"
    out["_raw"] = cmdline
    return out


def ensure_debugfs_ready() -> Tuple[bool, str]:
    if not DEBUGFS.is_dir():
        return False, f"{DEBUGFS} not present"
    if not DRI_DEBUGFS.is_dir():
        return False, f"{DRI_DEBUGFS} not present (is debugfs mounted? try: sudo mount -t debugfs none /sys/kernel/debug)"
    return True, "ok"

def list_dri_debug_cards() -> List[int]:
    if not DRI_DEBUGFS.is_dir():
        return []
    out = []
    for p in DRI_DEBUGFS.iterdir():
        if p.is_dir() and p.name.isdigit():
            out.append(int(p.name))
    return sorted(out)

def pick_primary_card() -> Optional[int]:
    # Simple heuristic: pick lowest card index in debugfs.
    cards = list_dri_debug_cards()
    return cards[0] if cards else None

# ----------------------------check vblank event----------------------------------

@dataclass
class VBlankResult:
    supported: bool
    deltas: Dict[str, int]   # per counter file delta
    details: str

def _read_int(p: Path) -> Optional[int]:
    t = read_text(p)
    if t is None:
        return None
    m = re.search(r"(\d+)", t)
    return int(m.group(1)) if m else None

def check_vblank_events(card: int, interval_s: float = 0.5) -> VBlankResult:
    """
    Best-effort: looks for per-CRTC vblank counters in debugfs and checks if they increase.

    It searches:
      /sys/kernel/debug/dri/N/crtc-*/vblank_count
      /sys/kernel/debug/dri/N/crtc-*/vblank
      /sys/kernel/debug/dri/N/crtc-*/vblank_event
    """
    base = DRI_DEBUGFS / str(card)
    if not base.is_dir():
        return VBlankResult(False, {}, f"Missing: {base}")

    counters: List[Path] = []
    for crtc in base.glob("crtc-*"):
        if not crtc.is_dir():
            continue
        for name in ("vblank_count", "vblank", "vblank_event"):
            p = crtc / name
            if p.exists():
                counters.append(p)

    if not counters:
        return VBlankResult(False, {}, f"No vblank counters found under {base}/crtc-*")

    before: Dict[str, int] = {}
    after: Dict[str, int] = {}
    for p in counters:
        v = _read_int(p)
        if v is not None:
            before[str(p)] = v

    time.sleep(interval_s)

    for p in counters:
        v = _read_int(p)
        if v is not None:
            after[str(p)] = v

    deltas: Dict[str, int] = {}
    for k, v0 in before.items():
        v1 = after.get(k)
        if v1 is not None:
            deltas[k] = v1 - v0

    return VBlankResult(True, deltas, "Non-zero delta usually means vblank is ticking for that CRTC")


# --------------------------- check framebuffer flip------------------------------

class FlipResult:
    supported: bool
    flips_seen: int
    samples: int
    details: str

_FB_RE = re.compile(r"\bfb=([0-9]+)\b")

def _extract_fb_ids_from_state(state_text: str) -> List[int]:
    # Very generic: collect all "fb=<id>" occurrences.
    return [int(m.group(1)) for m in _FB_RE.finditer(state_text)]

def check_framebuffer_flips(card: int, samples: int = 10, interval_s: float = 0.2) -> FlipResult:
    """
    Returns flips_seen = number of times the set of fb IDs changed between samples.

    Notes:
    - If the desktop is static, flips may legitimately be 0.
    - Move the mouse / animate a window during sampling to see flips.
    """
    state_path = DRI_DEBUGFS / str(card) / "state"
    txt0 = read_text(state_path)
    if txt0 is None:
        return FlipResult(False, 0, 0, f"Missing/unreadable: {state_path}")

    prev = sorted(set(_extract_fb_ids_from_state(txt0)))
    flips = 0
    for _ in range(samples - 1):
        time.sleep(interval_s)
        txt = read_text(state_path)
        if txt is None:
            break
        cur = sorted(set(_extract_fb_ids_from_state(txt)))
        if cur != prev:
            flips += 1
            prev = cur
    return FlipResult(True, flips, samples, f"state={state_path} (fb ids change count)")

# --------------------------check PHY power state and Panel power state-

@dataclass
class PsrAlpmResult:
    supported: bool
    psr_enabled: Optional[bool]
    psr_active: Optional[bool]
    alpm_active_hint: Optional[bool]
    raw_excerpt: str
    details: str

def _bool_from_line(line: str) -> Optional[bool]:
    # Common formats: "Enabled: yes/no", "PSR enabled: 1/0", "Active: yes/no"
    l = line.strip().lower()
    if any(x in l for x in ("yes", "enabled", ": 1", "=1")) and not any(x in l for x in ("no", ": 0", "=0", "disabled")):
        return True
    if any(x in l for x in ("no", "disabled", ": 0", "=0")):
        return False
    return None

def check_psr_alpm_state(card: int) -> PsrAlpmResult:
    """
    Best-effort, vendor-specific:
    - Strong support for Intel i915 via i915_edp_psr_status
    - For other drivers, likely unsupported unless you extend it
    """
    base = DRI_DEBUGFS / str(card)
    psr_path = base / "i915_edp_psr_status"
    txt = read_text(psr_path)
    if txt is None:
        return PsrAlpmResult(False, None, None, None, "", f"Missing/unreadable: {psr_path} (i915-only)")

    psr_enabled = None
    psr_active = None
    alpm_hint = None

    excerpt_lines: List[str] = []
    for line in txt.splitlines():
        l = line.lower()

        # PSR signals (formats differ slightly by kernel)
        if "psr" in l and ("enabled" in l or "enable" in l):
            b = _bool_from_line(line)
            if b is not None and psr_enabled is None:
                psr_enabled = b
        if "psr" in l and ("active" in l or "state" in l):
            # "Active: yes" / "PSR status: active"
            if "active" in l and ("yes" in l or "active" in l):
                psr_active = True
            if "inactive" in l or "not active" in l:
                psr_active = False

        # ALPM hints (often appears as "ALPM" string)
        if "alpm" in l:
            # If file explicitly says active/enabled, capture it
            if "enable" in l or "active" in l or "on" in l:
                alpm_hint = True
            if "disable" in l or "off" in l:
                alpm_hint = False

        # Keep a useful excerpt for reporting
        if any(k in l for k in ("psr", "alpm", "sink", "source", "dc3", "link")):
            excerpt_lines.append(line)

    excerpt = "\n".join(excerpt_lines[:60])
    return PsrAlpmResult(True, psr_enabled, psr_active, alpm_hint, excerpt, f"parsed from {psr_path}")

# ------------------------- shared DRM helpers -------------------------

def list_sys_class_drm() -> List[Path]:
    base = Path("/sys/class/drm")
    if not base.is_dir():
        return []
    return sorted([p for p in base.iterdir() if p.is_dir()])

def drm_cards() -> List[Path]:
    return [p for p in list_sys_class_drm() if re.fullmatch(r"card\d+", p.name)]

def get_driver_for_card(card: Path) -> Optional[str]:
    d = card / "device" / "driver"
    try:
        if d.is_symlink():
            return Path(os.readlink(str(d))).name
    except Exception:
        pass
    return None

def device_identity(card: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    dev = card / "device"
    for k in ["vendor", "device", "subsystem_vendor", "subsystem_device", "class"]:
        t = read_text(dev / k)
        if t:
            out[k] = t
    ue = read_text(dev / "uevent")
    if ue:
        for line in ue.splitlines():
            if line.startswith(("DRIVER=", "PCI_ID=", "MODALIAS=")):
                k, v = line.split("=", 1)
                out[k] = v
    return out

def list_dev_dri_nodes() -> List[str]:
    dri = Path("/dev/dri")
    if not dri.is_dir():
        return []
    return [p.name for p in sorted(dri.iterdir())]

def drm_connectors_for(card: Path) -> List[Path]:
    base = Path("/sys/class/drm")
    prefix = card.name + "-"
    out = []
    for p in list_sys_class_drm():
        if p.name.startswith(prefix) and (p / "status").exists():
            out.append(p)
    return out

def connector_info(conn: Path) -> Dict[str, str]:
    info: Dict[str, str] = {"name": conn.name}
    for f in ["status", "enabled", "dpms", "modes", "link_status"]:
        t = read_text(conn / f)
        if t is not None:
            info[f] = t
    edid = conn / "edid"
    if edid.exists():
        try:
            info["edid_bytes"] = str(edid.stat().st_size)
        except Exception:
            info["edid_bytes"] = "?"
    return info

def module_param(mod: str, param: str) -> Optional[str]:
    p = Path("/sys/module") / mod / "parameters" / param
    return read_text(p)

def runtime_pm_info(card: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    p = card / "device" / "power"
    if not p.is_dir():
        return out
    for k in ["runtime_status", "runtime_suspended_time", "runtime_active_time",
              "control", "autosuspend_delay_ms"]:
        t = read_text(p / k)
        if t is not None:
            out[k] = t
    return out

# ------------------------- Flow A: nomodeset / fbdev -------------------------

def run_flow_nomodeset(deep: bool) -> Tuple[int, List[str]]:
    lines: List[str] = []
    lines.append("[INFO] Flow: nomodeset (fbdev / firmware framebuffer)")

    fb0 = Path("/dev/fb0")
    if fb0.exists():
        lines.append("[OK] /dev/fb0 exists (fbdev path available)")
    else:
        lines.append("[FAIL] /dev/fb0 missing (expected with nomodeset). Check efifb/simplefb/vesafb/simpledrm.")
        # still continue to gather hints

    # sysfs fb info
    fb_sys = Path("/sys/class/graphics/fb0")
    if fb_sys.is_dir():
        for f in ["name", "modes", "virtual_size", "stride", "bits_per_pixel"]:
            t = read_text(fb_sys / f)
            if t is not None:
                lines.append(f"[INFO] fb0 {f}: {t}")
        # driver symlink if present
        drv = fb_sys / "device" / "driver"
        if drv.exists():
            try:
                if drv.is_symlink():
                    lines.append(f"[INFO] fb0 driver: {Path(os.readlink(str(drv))).name}")
            except Exception:
                pass
    else:
        lines.append("[WARN] /sys/class/graphics/fb0 not found; fbdev sysfs info missing")

    # kernel log hints for fb drivers
    klog = read_klog(deep=deep)
    fb_pats = [r"\befifb\b", r"\bvesafb\b", r"\bsimplefb\b", r"\bsimpledrm\b", r"framebuffer"]
    fb_hits = grep_lines(klog, fb_pats, max_hits=60)
    if fb_hits:
        lines.append("[INFO] Log sample (fbdev/firmware framebuffer):\n" + "\n".join(fb_hits[:60]))
    else:
        lines.append("[INFO] No obvious fbdev driver lines found in logs (may be quiet on some systems).")

    # Optional: collect DRM presence as informational only
    sys_drm = list_sys_class_drm()
    dri_nodes = list_dev_dri_nodes()
    lines.append(f"[INFO] /sys/class/drm entries: {', '.join(p.name for p in sys_drm) if sys_drm else '<none>'}")
    lines.append(f"[INFO] /dev/dri nodes: {', '.join(dri_nodes) if dri_nodes else '<none>'}")
    if any(n.startswith("renderD") for n in dri_nodes):
        lines.append("[INFO] renderD* exists even with nomodeset (compute/render may still be possible; display KMS is disabled).")

    # Exit logic: in nomodeset flow, missing /dev/fb0 is the main hard failure.
    rc = 2 if not fb0.exists() else 0
    return rc, lines


# ------------------------- Flow B: normal DRM/KMS -------------------------

def run_flow_kms(deep: bool) -> Tuple[int, List[str]]:
    lines: List[str] = []
    lines.append("[INFO] Flow: normal DRM/KMS")

    # 1) DRM registered (sysfs)
    sys_drm = list_sys_class_drm()
    if not sys_drm:
        lines.append("[FAIL] /sys/class/drm missing/empty: DRM not exporting state (driver not loaded/bound?)")
        return 2, lines
    lines.append("[INFO] /sys/class/drm entries: " + ", ".join(p.name for p in sys_drm))

    cards = drm_cards()
    if not cards:
        lines.append("[FAIL] No /sys/class/drm/cardN found: DRM device not registered (driver missing/not bound?)")
        return 2, lines
    lines.append("[OK] Found DRM cards: " + ", ".join(c.name for c in cards))

    # 2) Driver bound
    any_driver = False
    for c in cards:
        drv = get_driver_for_card(c)
        ident = device_identity(c)
        if drv:
            any_driver = True
            lines.append(f"[OK] {c.name}: driver bound = {drv}")
        else:
            lines.append(f"[WARN] {c.name}: no driver bound symlink")
        if ident:
            brief = ", ".join(f"{k}={v}" for k, v in ident.items() if k in ("DRIVER", "PCI_ID", "vendor", "device", "class"))
            lines.append(f"[INFO] {c.name}: identity: {brief or '<partial>'}")

        pm = runtime_pm_info(c)
        if pm:
            lines.append("[INFO] " + f"{c.name} runtime PM: " + ", ".join(f"{k}={v}" for k, v in pm.items()))

    if not any_driver:
        lines.append("[FAIL] DRM cards exist but none show a bound driver: probe/bind issue")
        return 2, lines

    # 3) /dev/dri nodes
    dri_nodes = list_dev_dri_nodes()
    if not dri_nodes:
        lines.append("[FAIL] /dev/dri missing/empty: udev/devtmpfs nodes not created")
        return 2, lines
    lines.append("[INFO] /dev/dri nodes: " + ", ".join(dri_nodes))

    has_card = any(n.startswith("card") for n in dri_nodes)
    has_render = any(n.startswith("renderD") for n in dri_nodes)
    if not has_card:
        lines.append("[FAIL] No /dev/dri/card*: compositor cannot open KMS")
        return 2, lines
    lines.append("[OK] /dev/dri/card* present (KMS node)")

    if has_render:
        lines.append("[OK] /dev/dri/renderD* present (render node)")
    else:
        lines.append("[WARN] No /dev/dri/renderD*: Mesa may fall back to llvmpipe or rendering may fail")

    # 4) KMS gating module params
    params = []
    for mod, param in [("nvidia_drm", "modeset"), ("i915", "modeset"), ("amdgpu", "dc"), ("radeon", "modeset")]:
        v = module_param(mod, param)
        if v is not None:
            params.append(f"{mod}.{param}={v}")
    lines.append("[INFO] modeset params: " + (", ".join(params) if params else "<none readable>"))
    if any(p.startswith("nvidia_drm.modeset=0") for p in params):
        lines.append("[FAIL] nvidia_drm.modeset=0: KMS disabled for NVIDIA DRM (often black screen on Wayland)")
        return 2, lines

    # 5) Connection / EDID / modes
    any_connected = False
    for c in cards:
        conns = drm_connectors_for(c)
        if not conns:
            lines.append(f"[WARN] {c.name}: no connectors found (headless/render-only?)")
            continue
        for conn in conns:
            ci = connector_info(conn)
            status = (ci.get("status") or "").strip()
            modes = (ci.get("modes") or "").splitlines()
            edid_bytes = ci.get("edid_bytes", "0")
            link_status = (ci.get("link_status") or "").strip()
            lines.append(f"[INFO] {ci['name']}: status={status or '<unknown>'}, edid_bytes={edid_bytes}, modes={len(modes)}" +
                         (f", link_status={link_status}" if link_status else ""))
            if status == "connected":
                any_connected = True
                if len(modes) == 0:
                    lines.append(f"[WARN] {ci['name']}: connected but no modes (EDID/AUX/DDC/link issue)")
                if edid_bytes in ("0", "", "?"):
                    lines.append(f"[WARN] {ci['name']}: EDID size suspicious (edid_bytes={edid_bytes})")
                if link_status and link_status.lower() != "good":
                    lines.append(f"[WARN] {ci['name']}: link_status={link_status}")

    if any_connected:
        lines.append("[OK] At least one connector is connected")
    else:
        lines.append("[WARN] No connectors report connected (if you expect display: cable/hotplug/link training)")

    # 6) runtime checkiong
    card = pick_primary_card()
    if card is None:
        print("[WARN] no /sys/kernel/debug/dri/<N> found")
        return 2
    else:
        flips = check_framebuffer_flips(card, samples=10, interval_s=0.2)
        print(flips)

        vb = check_vblank_events(card, interval_s=0.5)
        print(vb)

        psr_alpm = check_psr_alpm_state(card)
        print(psr_alpm)

    # Exit: fail only if major KMS prerequisites are missing
    return 0, lines


# ------------------------- main -------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-kms", action="store_true",
                    help="Treat missing KMS pieces as FAIL (desktop expectation).")
    args = ap.parse_args()

    cmd = parse_cmdline()
    print("[INFO] " + bullet("Kernel cmdline", cmd.get("_raw", "")))

    nomodeset = ("nomodeset" in cmd) or (cmd.get("nomodeset") == "1")
    if nomodeset and args.expect_kms:
        print("The system run with nomodeset but we expected KMS.")
        return
        # logging.error("The system run with nomodeset but we expected KMS.")
        # raise SystemExit("FAIL: RPMSG channel is not created") 
    elif nomodeset:
        rc, lines = run_flow_nomodeset(deep=args.deep)
    else:
        rc, lines = run_flow_kms(deep=args.deep)

    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())

