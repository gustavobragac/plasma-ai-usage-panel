from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from curl_cffi import requests

from common import format_eta, load_cookies, parse_window_direct, format_output, get_cached_or_fetch, open_login_url, LOGIN_URLS


# ================= Configuration =================

BASE_HEADERS = {
    "Referer": "https://chatgpt.com/",
    "Origin": "https://chatgpt.com",
    "Accept": "*/*"
}

SESSION_URL = "https://chatgpt.com/api/auth/session"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# SVG icon path (unused in current version)
SCRIPT_DIR = Path(__file__).parent
ICON_PATH = SCRIPT_DIR / "assets" / "codex.svg"

# ================= Network Logic =================

def _fetch_codex_usage_uncached(browsers: list[str] | None = None) -> dict:
    """Internal function to fetch Codex usage data without caching"""
    try:
        cookies_dict, _browser = load_cookies("chatgpt.com", browsers)
    except Exception as e:
        raise RuntimeError(f"Failed to read browser cookies: {e}")

    # Retry once (2 attempts total)
    last_error = None
    for attempt in range(2):
        try:
            # Get Access Token
            session_resp = requests.get(
                SESSION_URL,
                cookies=cookies_dict,
                headers=BASE_HEADERS,
                impersonate="chrome",
                timeout=10
            )

            if session_resp.status_code == 403:
                raise RuntimeError("403 Forbidden: Cloudflare blocked, check IP or update browser_cookie3")

            session_resp.raise_for_status()
            session_data = session_resp.json()

            access_token = session_data.get("accessToken")
            if not access_token:
                raise RuntimeError("accessToken not found in session response.")

            # Get Usage Data
            usage_headers = BASE_HEADERS.copy()
            usage_headers["Authorization"] = f"Bearer {access_token}"

            usage_resp = requests.get(
                CODEX_USAGE_URL,
                cookies=cookies_dict,
                headers=usage_headers,
                impersonate="chrome",
                timeout=10
            )

            usage_resp.raise_for_status()
            return usage_resp.json()

        except Exception as e:
            last_error = e
            if attempt == 0:  # First failure, retry
                continue

    # Both attempts failed
    raise RuntimeError(f"Request failed: {last_error}")


def get_codex_usage(browsers: list[str] | None = None) -> dict:
    """
    Fetch ChatGPT Codex usage data.

    Uses file-based caching to prevent multiple Waybar instances (one per monitor)
    from making concurrent API requests that might be rate-limited.
    """
    return get_cached_or_fetch("codex", lambda: _fetch_codex_usage_uncached(browsers))


# ================= Output Logic =================


def print_json(
    usage: dict,
    format_str: str | None = None,
    tooltip_format: str | None = None,
    show_5h: bool = False,
) -> None:
    rate = usage.get("rate_limit") or {}
    p_raw = rate.get("primary_window") or {}
    s_raw = rate.get("secondary_window") or {}
    windows = [
        (raw, parse_window_direct(raw))
        for raw in (p_raw, s_raw)
        if raw
    ]

    def window_with_duration(seconds: int):
        return next(
            ((raw, win) for raw, win in windows if raw.get("limit_window_seconds") == seconds),
            ({}, parse_window_direct(None)),
        )

    fh_raw, fh_win = window_with_duration(5 * 60 * 60)
    weekly_raw, weekly_win = window_with_duration(7 * 24 * 60 * 60)

    # Prepare all data points without icons
    fh_reset_str = format_eta(fh_win.resets_at) if fh_win.resets_at else "Not started"
    weekly_reset_str = format_eta(weekly_win.resets_at) if weekly_win.resets_at else "Not started"

    # Icons with colors (users can customize)
    icon_styled = "<span foreground='#74AA9C' size='large'>󰬫</span>"
    time_icon_styled = "<span foreground='#74AA9C' size='large'>󰔚</span>"

    # The API's primary/secondary positions do not identify the window length.
    # ChatGPT plans may expose only a weekly primary window.
    if show_5h and fh_raw:
        target_raw, target_win, win_type = fh_raw, fh_win, "5h"
    elif weekly_raw and weekly_win.utilization > 80:
        target_raw, target_win, win_type = weekly_raw, weekly_win, "7d"
    elif fh_raw:
        target_raw, target_win, win_type = fh_raw, fh_win, "5h"
    elif weekly_raw:
        target_raw, target_win, win_type = weekly_raw, weekly_win, "7d"
    elif windows:
        target_raw, target_win = windows[0]
        win_type = "Custom"
    else:
        target_raw, target_win = {}, parse_window_direct(None)
        win_type = "None"

    pct = int(round(target_win.utilization))

    # Check if window is unused (used_percent == 0 and reset_after near window length)
    used_pct = target_raw.get("used_percent", 0)
    reset_after = target_raw.get("reset_after_seconds", 0)
    window_length = target_raw.get("limit_window_seconds", 0)

    is_unused = (used_pct == 0 and reset_after >= window_length - 1)

    window_not_started = (target_win.utilization == 0 and target_win.resets_at is None)

    # Determine status
    if weekly_win.utilization >= 100:
        status = "Pause"
    elif is_unused or window_not_started:
        status = "Ready"
    else:
        status = ""

    # Prepare data dictionary for formatting
    data = {
        "5h_pct": int(round(fh_win.utilization)),
        "7d_pct": int(round(weekly_win.utilization)),
        "5h_reset": fh_reset_str,
        "7d_reset": weekly_reset_str,
        "icon": icon_styled,
        "icon_plain": "󰬫",
        "time_icon": time_icon_styled,
        "time_icon_plain": "󰔚",
        "status": status,
        "pct": pct,
        "reset": format_eta(target_win.resets_at) if target_win.resets_at else "Not started",
        "win": win_type,
    }

    # Use custom format or default
    if format_str:
        text = format_output(format_str, data)
    else:
        # Default format (backward compatible)
        if status == "Pause":
            text = f"{icon_styled} Pause"
        elif status == "Ready":
            text = f"{icon_styled} Ready"
        else:
            text = f"{icon_styled} {pct}% {time_icon_styled} {data['reset']}"

    # Use custom tooltip format or default
    if tooltip_format:
        tooltip = format_output(tooltip_format, data)
    else:
        # Default tooltip
        lines = ["Window     Used    Reset", "━━━━━━━━━━━━━━━━━━━━━━━━"]
        if fh_raw:
            lines.append(f"5-Hour     {fh_win.utilization:>3.0f}%    {fh_reset_str}")
        if weekly_raw:
            lines.append(f"Weekly     {weekly_win.utilization:>3.0f}%    {weekly_reset_str}")
        lines.extend(["", "Click to Refresh"])
        tooltip = "\n".join(lines)

    if pct < 50:
        cls = "codex-low"
    elif pct < 80:
        cls = "codex-mid"
    else:
        cls = "codex-high"

    output = {
        "text": text,
        "tooltip": tooltip,
        "class": cls,
        "alt": win_type,
        "percentage": data["pct"],
    }

    print(json.dumps(output))


def print_cli(usage: dict) -> None:
    print(json.dumps(usage, indent=2))
    rate = usage.get("rate_limit") or {}

    print("-" * 40)
    for raw in (rate.get("primary_window"), rate.get("secondary_window")):
        if not raw:
            continue
        win = parse_window_direct(raw)
        seconds = raw.get("limit_window_seconds")
        if seconds == 5 * 60 * 60:
            label = "5-Hour"
        elif seconds == 7 * 24 * 60 * 60:
            label = "Weekly"
        else:
            label = "Custom"
        print(f"{label:<8}: {win.utilization:>5.1f}% | Reset in {format_eta(win.resets_at)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--browser",
        action="append",
        help="Browser cookie source to try (repeatable). Example: --browser chromium",
    )
    parser.add_argument(
        "--format",
        type=str,
        help=(
            "Custom format string for output text. Available: {icon}, {icon_plain}, "
            "{time_icon}, {time_icon_plain}, {5h_pct}, {7d_pct}, {5h_reset}, {7d_reset}, "
            "{status}, {pct}, {reset}, {win}. Example: '{icon_plain} {5h_pct}%%'"
        ),
    )
    parser.add_argument(
        "--tooltip-format",
        type=str,
        help="Custom format string for tooltip. Uses same variables as --format.",
    )
    parser.add_argument(
        "--show-5h",
        action="store_true",
        help="Always show 5-hour window data (instead of auto-switching to 7-day at 80%%)",
    )
    args = parser.parse_args()

    try:
        usage = get_codex_usage(args.browser)
    except Exception as e:
        if args.json:
            err_msg = str(e)
            err_lower = err_msg.lower()
            is_http_auth = "403" in err_msg or "401" in err_msg
            is_cookie = "cookie" in err_lower
            short_err = "Auth Err" if (is_http_auth or is_cookie) else "Net Err"
            tooltip = f"Error:\n{err_msg}"
            if is_http_auth:
                if open_login_url(LOGIN_URLS["chatgpt.com"]):
                    tooltip += "\n\nOpened login page — log in then click to refresh"
            print(json.dumps({
                "text": f"<span foreground='#ff5555'>󰬫 {short_err}</span>",
                "tooltip": tooltip,
                "class": "critical"
            }))
            sys.exit(0)
        else:
            print(f"[!] Critical Error: {e}", file=sys.stderr)
            sys.exit(1)

    if args.json:
        print_json(usage, args.format, args.tooltip_format, args.show_5h)
    else:
        print_cli(usage)

if __name__ == "__main__":
    main()
