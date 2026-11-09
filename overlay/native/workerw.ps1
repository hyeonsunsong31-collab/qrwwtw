<#
  workerw.ps1 -- place the JARVIS wallpaper window behind the desktop icons (requirement 18).

  Same technique live-wallpaper apps use: ask Explorer (Progman, message 0x052C) to create
  the WorkerW layer that sits between the wallpaper and the icons, then SetParent our
  window into it. Handles both the classic layout and Windows 11 24H2+, where that
  WorkerW is a child of Progman. If no WorkerW can be found, the window is kept directly
  above the desktop and beneath every app window instead.

  Usage (called by overlay/main.js):
    workerw.ps1 -Hwnd <window handle>     attach
    workerw.ps1 -Detach                   repaint the user's normal wallpaper
  Exit codes: 0 attached / repainted, 2 fallback layering used.
#>
param([string]$Hwnd = "0", [switch]$Detach)
$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class JarvisDesk {
    public delegate bool EnumProc(IntPtr hwnd, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string cls, string name);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string name);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumProc fn, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr w, IntPtr l,
                                                   uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll")]
    public static extern IntPtr SetParent(IntPtr child, IntPtr parent);
    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern bool SystemParametersInfo(uint action, uint param, StringBuilder value, uint flags);

    public static IntPtr FindWorkerW() {
        IntPtr progman = FindWindow("Progman", null);
        IntPtr result;
        SendMessageTimeout(progman, 0x052C, new IntPtr(0xD), new IntPtr(0x1), 0, 1000, out result);
        SendMessageTimeout(progman, 0x052C, IntPtr.Zero, IntPtr.Zero, 0, 1000, out result);
        IntPtr worker = IntPtr.Zero;
        EnumWindows(delegate (IntPtr top, IntPtr lp) {
            if (FindWindowEx(top, IntPtr.Zero, "SHELLDLL_DefView", null) != IntPtr.Zero) {
                IntPtr w = FindWindowEx(IntPtr.Zero, top, "WorkerW", null);
                if (w != IntPtr.Zero) { worker = w; }
            }
            return true;
        }, IntPtr.Zero);
        if (worker == IntPtr.Zero) {
            // Windows 11 24H2 and later: the WorkerW layer lives inside Progman
            worker = FindWindowEx(progman, IntPtr.Zero, "WorkerW", null);
        }
        return worker;
    }
}
"@

if ($Detach) {
    # Re-apply the current wallpaper so Explorer repaints it after our window is gone.
    $sb = New-Object System.Text.StringBuilder 520
    [JarvisDesk]::SystemParametersInfo(0x0073, 520, $sb, 0) | Out-Null   # SPI_GETDESKWALLPAPER
    [JarvisDesk]::SystemParametersInfo(0x0014, 0, $sb, 0x3) | Out-Null   # SPI_SETDESKWALLPAPER
    exit 0
}

$handle = [IntPtr]([Int64]$Hwnd)
if ($handle -eq [IntPtr]::Zero) { exit 1 }

$worker = [JarvisDesk]::FindWorkerW()
if ($worker -ne [IntPtr]::Zero) {
    [JarvisDesk]::SetParent($handle, $worker) | Out-Null
    exit 0
}

# Fallback: directly above the desktop window, beneath all application windows.
$flags = 0x0001 -bor 0x0002 -bor 0x0010          # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
$bottom = [IntPtr]1                               # HWND_BOTTOM
[JarvisDesk]::SetWindowPos($handle, $bottom, 0, 0, 0, 0, $flags) | Out-Null
$progman = [JarvisDesk]::FindWindow("Progman", $null)
[JarvisDesk]::SetWindowPos($progman, $bottom, 0, 0, 0, 0, $flags) | Out-Null
exit 2
