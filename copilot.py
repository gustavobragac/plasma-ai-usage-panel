from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

from common import format_eta, format_output, get_cached_or_fetch, load_cookies


# ==================== Configuration ====================

CONFIG_PATH = Path("~/.config/plasma-ai-usage-panel/copilot.conf").expanduser()
COPILOT_ICON = "\uf4b8"   # nf-seti-copilot — same as LazyVim/Neovim Copilot ()
COPILOT_COLOR = "#8b5cf6"
DEFAULT_QUOTA = 300
GITHUB_API_BASE = "https://api.github.com"
COPILOT_FEATURES_URL = "https://github.com/settings/copilot/features"

# Local Copilot editor/CLI credentials (written by the official Copilot plugins).
COPILOT_CONFIG_DIR = Path("~/.config/github-copilot").expanduser()
COPILOT_APPS_JSON = COPILOT_CONFIG_DIR / "apps.json"
COPILOT_OAUTH_JSON = COPILOT_CONFIG_DIR / "oauth.json"
COPILOT_INTERNAL_URL = f"{GITHUB_API_BASE}/copilot_internal/user"

# Order in which quota snapshots / usage rows are preferred when the account
# exposes more than one metric.
QUOTA_SNAPSHOT_PRIORITY = ("premium_interactions", "premium_requests", "chat", "completions")
USAGE_LABEL_PRIORITY = (
    "premium requests",
    "premium interactions",
    "included credits",
    "monthly limit",
    "inline suggestions",
)

VALID_SOURCES = ("auto", "api", "internal", "browser")


def load_copilot_config(config_path: Path | None = None) -> dict:
    """Load Copilot config from file.

    Recognized keys:
    - GITHUB_TOKEN:   fine-grained PAT with 'Plan (read)' (personal paid accounts)
    - COPILOT_QUOTA:  fallback quota used when the source doesn't report one
    - COPILOT_SOURCE: auto | api | internal | browser
    - COPILOT_METRIC: label/quota id to prefer (e.g. 'premium_interactions')
    """
    path = config_path or CONFIG_PATH
    config: dict = {
        "GITHUB_TOKEN": None,
        "COPILOT_QUOTA": DEFAULT_QUOTA,
        "COPILOT_SOURCE": "auto",
        "COPILOT_METRIC": None,
    }

    if not path.exists():
        return config

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "GITHUB_TOKEN":
                config["GITHUB_TOKEN"] = value
            elif key == "COPILOT_QUOTA":
                try:
                    config["COPILOT_QUOTA"] = int(value)
                except ValueError:
                    pass
            elif key == "COPILOT_SOURCE":
                value = value.lower()
                if value in VALID_SOURCES:
                    config["COPILOT_SOURCE"] = value
            elif key == "COPILOT_METRIC":
                config["COPILOT_METRIC"] = value or None

    return config


# ==================== Core Logic: Get Usage ====================

class CopilotHTTPError(RuntimeError):
    """Raised for HTTP errors from the GitHub API, carrying the numeric status code."""
    def __init__(self, code: int, body: str) -> None:
        super().__init__(f"HTTP {code}: {body}")
        self.code = code


def _github_get(url: str, token: str) -> dict | list:
    """Make authenticated GET request to GitHub API."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "plasma-ai-usage-panel/copilot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise CopilotHTTPError(e.code, body) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def _get_github_username(token: str) -> str:
    """Fetch and cache the authenticated GitHub username (TTL: 1 hour)."""
    data = get_cached_or_fetch(
        "copilot_user",
        lambda: _github_get(f"{GITHUB_API_BASE}/user", token),
        ttl=3600,
    )
    username = data.get("login") if isinstance(data, dict) else None
    if not username:
        raise RuntimeError("Could not determine GitHub username from /user endpoint")
    return username


def _fetch_copilot_usage_uncached(token: str) -> dict:
    """Fetch Copilot premium request usage from GitHub API (not cached)."""
    username = _get_github_username(token)
    url = f"{GITHUB_API_BASE}/users/{username}/settings/billing/premium_request/usage"
    usage_data = _github_get(url, token)

    # Response may be a list directly or a dict with usageItems
    if isinstance(usage_data, list):
        usage_items = usage_data
    else:
        usage_items = usage_data.get("usageItems", [])

    used = sum(item.get("grossQuantity", 0) for item in usage_items)
    return {"used": round(used, 1), "raw": usage_data, "source": "billing-api"}


# ==================== Source: local Copilot OAuth token ====================

class CopilotNoLocalToken(RuntimeError):
    """Raised when no local Copilot editor/CLI OAuth token could be found."""


def _load_copilot_oauth_token() -> str:
    """Read the OAuth token stored by the official Copilot editor/CLI plugins.

    Both ~/.config/github-copilot/apps.json and the older oauth.json map a host key
    (e.g. "github.com:Ov23li..." or "github.com") to an object containing
    ``oauth_token``. Any github.com entry works for the copilot_internal endpoint.
    """
    for path in (COPILOT_APPS_JSON, COPILOT_OAUTH_JSON):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key, entry in data.items():
            if not str(key).startswith("github.com"):
                continue
            token = entry.get("oauth_token") if isinstance(entry, dict) else None
            if token:
                return token

    raise CopilotNoLocalToken(
        f"No Copilot OAuth token found in {COPILOT_APPS_JSON} or {COPILOT_OAUTH_JSON}. "
        "Sign in to Copilot from an editor or the Copilot CLI."
    )


def _pick_quota_snapshot(snapshots: dict, preferred: str | None) -> tuple[str, dict] | None:
    """Choose the most relevant quota snapshot from copilot_internal/user."""
    if not isinstance(snapshots, dict):
        return None

    def usable(snap: object) -> bool:
        return isinstance(snap, dict) and snap.get("has_quota") and not snap.get("unlimited")

    if preferred and usable(snapshots.get(preferred)):
        return preferred, snapshots[preferred]

    for name in QUOTA_SNAPSHOT_PRIORITY:
        if usable(snapshots.get(name)):
            return name, snapshots[name]

    for name, snap in snapshots.items():
        if usable(snap):
            return name, snap

    return None


def _fetch_copilot_usage_from_internal(metric: str | None = None) -> dict:
    """Fetch usage from api.github.com/copilot_internal/user using the local token.

    This is the endpoint the official editor plugins use, so it works for
    enterprise/EMU (Copilot Business) seats where the public billing API returns 400.
    """
    token = _load_copilot_oauth_token()
    req = urllib.request.Request(
        COPILOT_INTERNAL_URL,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "Editor-Version": "vscode/1.99.0",
            "Editor-Plugin-Version": "copilot-chat/0.26.7",
            "User-Agent": "GitHubCopilotChat/0.26.7",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise CopilotHTTPError(e.code, e.read().decode()) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e

    picked = _pick_quota_snapshot(data.get("quota_snapshots", {}), metric)
    if picked is None:
        raise RuntimeError("copilot_internal returned no metered quota snapshot")

    quota_id, snap = picked
    entitlement = float(snap.get("entitlement") or 0)
    remaining = snap.get("remaining")
    if remaining is None:
        remaining = snap.get("quota_remaining")

    if entitlement > 0 and remaining is not None:
        used = max(entitlement - float(remaining), 0.0)
        pct = used / entitlement * 100
    else:
        pct = 100.0 - float(snap.get("percent_remaining") or 0)
        used = round(entitlement * pct / 100, 1) if entitlement > 0 else float(
            snap.get("credits_used") or 0
        )

    overage = float(snap.get("overage_count") or 0)

    return {
        "pct": round(pct, 2),
        "used": round(used + overage, 1),
        "quota": int(entitlement) if entitlement > 0 else None,
        "reset": data.get("quota_reset_date_utc") or data.get("quota_reset_date"),
        "plan": data.get("copilot_plan"),
        "login": data.get("login"),
        "quota_id": quota_id,
        "overage": overage,
        "source": "copilot-internal",
    }


def _iter_chromium_profile_cookies(domain: str):
    """Yield (cookies_dict, label) for every Chrome/Chromium profile that has cookies for domain.

    Browsers store profiles like ~/.config/google-chrome/{Default, Profile 1, Profile 2, ...}.
    The standard load_cookies() only reads the Default profile; this helper covers
    accounts that live in secondary profiles (e.g. an Enterprise managed account).
    """
    import os
    import browser_cookie3 as _bc3

    roots = [
        ("chrome", os.path.expanduser("~/.config/google-chrome")),
        ("chromium", os.path.expanduser("~/.config/chromium")),
        ("brave", os.path.expanduser("~/.config/BraveSoftware/Brave-Browser")),
    ]
    for browser_name, root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            cookies_path = os.path.join(root, entry, "Cookies")
            if not os.path.exists(cookies_path):
                continue
            try:
                cj = _bc3.chrome(cookie_file=cookies_path, domain_name=domain)
                cookies = {c.name: c.value for c in cj}
            except Exception:
                continue
            if cookies:
                yield cookies, f"{browser_name}:{entry}"


def _parse_usage_rows(html: str) -> dict[str, float]:
    """Extract {row label: percent used} from the Copilot settings 'Usage' box.

    Current (2026) markup, one <li class="Box-row"> per metric:
        <span ... class="text-bold">Included credits</span>
        ... <span class="mr-2 ...">12% used</span>
        <span style="width: 12.5%;" ... class="Progress-item ..."></span>
    """
    rows: dict[str, float] = {}
    for row in re.findall(r'<li[^>]*class="[^"]*Box-row[^"]*"[^>]*>.*?</li>', html, re.S):
        label_match = re.search(r'class="[^"]*text-bold[^"]*"[^>]*>\s*([^<]+?)\s*<', row)
        if not label_match:
            continue
        # Prefer the precise progress-bar width; fall back to the rounded "N% used" text.
        width_match = re.search(r'width:\s*([\d.]+)%[^>]*Progress-item', row)
        used_match = re.search(r'(\d+(?:\.\d+)?)%\s*used', row)
        if width_match:
            pct = float(width_match.group(1))
        elif used_match:
            pct = float(used_match.group(1))
        else:
            continue
        rows[label_match.group(1).strip().lower()] = pct
    return rows


def _pick_usage_row(rows: dict[str, float], preferred: str | None) -> float | None:
    """Choose which usage row represents the quota we want to display."""
    if not rows:
        return None
    if preferred:
        wanted = preferred.strip().lower().replace("_", " ")
        for label, pct in rows.items():
            if wanted in label:
                return pct
    for wanted in USAGE_LABEL_PRIORITY:
        for label, pct in rows.items():
            if wanted in label:
                return pct
    return next(iter(rows.values()))


def _parse_copilot_features_page(html: str, metric: str | None = None):
    """Return (pct, managed_by_name, managed_by_href) if the page renders usage, else None.

    Supports three layouts of https://github.com/settings/copilot/features:
    - 2026 'Usage' box with one Box-row per metric ("Included credits",
      "Premium requests", "Inline suggestions").
    - 2025 usage-based billing: a "Monthly Limit" section with a progress bar.
    - Legacy: the "copilot-overages-usage" premium-request section.
    """
    managed_by = re.search(
        r'Managed by\s*<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>',
        html,
    )
    managed_name = managed_by.group(2) if managed_by else None
    managed_href = managed_by.group(1) if managed_by else None

    # Current layout: the "Usage" box with labelled rows.
    usage_idx = html.find(">Usage<")
    if usage_idx != -1:
        pct = _pick_usage_row(_parse_usage_rows(html[usage_idx:usage_idx + 20000]), metric)
        if pct is not None:
            return (pct, managed_name, managed_href)

    # 2025 layout (usage-based billing): "Monthly Limit" usage section.
    idx = html.find("Monthly Limit")
    if idx != -1:
        section = html[idx:idx + 3000]
        width_match = re.search(r'width:\s*([\d.]+)%[^>]*Progress-item', section)
        used_match = re.search(r'(\d+(?:\.\d+)?)%\s*used', section)
        if width_match:
            return (float(width_match.group(1)), managed_name, managed_href)
        if used_match:
            return (float(used_match.group(1)), managed_name, managed_href)

    # Legacy layout: premium-request overages section.
    if 'id="copilot-overages-usage"' in html:
        section_match = re.search(r'<div id="copilot-overages-usage".*?</li>', html, re.S)
        if section_match:
            pct_match = re.search(r'>\s*(\d+(?:\.\d+)?)%\s*<', section_match.group(0))
            if pct_match:
                return (float(pct_match.group(1)), managed_name, managed_href)

    return None


def _fetch_copilot_usage_from_browser(metric: str | None = None) -> dict:
    """Fetch Copilot usage percentage from the authenticated Copilot settings page.

    Iterates through every Chrome/Chromium/Brave profile until one returns a page
    with the usage section. This handles the common case where the user has a
    personal account in 'Default' and an Enterprise/Business account in 'Profile 1'.
    """
    last_error = "no chromium-based browser profile found"
    tried: list[str] = []

    for cookies, label in _iter_chromium_profile_cookies("github.com"):
        tried.append(label)
        try:
            response = requests.get(
                COPILOT_FEATURES_URL,
                cookies=cookies,
                impersonate="chrome",
                timeout=20,
                allow_redirects=True,
            )
        except Exception as exc:
            last_error = f"{label}: {exc}"
            continue

        if response.status_code != 200:
            last_error = f"{label}: HTTP {response.status_code}"
            continue

        parsed = _parse_copilot_features_page(response.text, metric)
        if parsed is None:
            last_error = f"{label}: no copilot usage section"
            continue

        pct, managed_name, managed_href = parsed
        return {
            "pct": pct,
            "raw": {"managed_by_name": managed_name, "managed_by_href": managed_href},
            "source": f"{label}:copilot-features",
        }

    # Last-resort fallback: use the original load_cookies() flow (covers firefox/helium).
    try:
        cookies, browser_name = load_cookies("github.com")
    except Exception as exc:
        raise RuntimeError(
            f"No browser profile with Copilot usage found. Tried: {tried or 'none'}. "
            f"Last error: {last_error}. Cookie loader: {exc}"
        )

    response = requests.get(
        COPILOT_FEATURES_URL, cookies=cookies, impersonate="chrome", timeout=20, allow_redirects=True
    )
    if response.status_code != 200:
        raise RuntimeError(f"{browser_name}: HTTP {response.status_code}")
    parsed = _parse_copilot_features_page(response.text, metric)
    if parsed is None:
        raise RuntimeError(
            f"No copilot usage section in any profile. Tried: {tried + [browser_name]}"
        )
    pct, managed_name, managed_href = parsed
    return {
        "pct": pct,
        "raw": {"managed_by_name": managed_name, "managed_by_href": managed_href},
        "source": f"{browser_name}:copilot-features",
    }


def _should_fallback(error: Exception) -> bool:
    """Only fall back for user billing API responses that are expected for org-managed Copilot."""
    return isinstance(error, CopilotHTTPError) and error.code in (400, 401, 403, 404)


def get_copilot_usage(token: str | None, source: str = "auto", metric: str | None = None) -> dict:
    """Fetch Copilot usage with file-based caching (TTL: 60 seconds).

    Sources are tried in order (``auto``):
    1. ``api``      - public billing API with a PAT (personal paid accounts)
    2. ``internal`` - api.github.com/copilot_internal/user with the local Copilot
                      editor/CLI OAuth token (works for enterprise/EMU seats)
    3. ``browser``  - scrape /settings/copilot/features using browser cookies
    """
    def from_api() -> dict:
        if not token:
            raise RuntimeError(f"No GITHUB_TOKEN configured in {CONFIG_PATH}")
        return get_cached_or_fetch("copilot", lambda: _fetch_copilot_usage_uncached(token))

    def from_internal() -> dict:
        return get_cached_or_fetch(
            "copilot_internal", lambda: _fetch_copilot_usage_from_internal(metric)
        )

    def from_browser() -> dict:
        return get_cached_or_fetch(
            "copilot_browser_v2", lambda: _fetch_copilot_usage_from_browser(metric)
        )

    if source == "api":
        return from_api()
    if source == "internal":
        return from_internal()
    if source == "browser":
        return from_browser()

    errors: list[str] = []

    if token:
        try:
            return from_api()
        except Exception as exc:
            if not _should_fallback(exc):
                raise
            errors.append(f"billing api: {exc}")

    try:
        return from_internal()
    except Exception as exc:
        errors.append(f"copilot_internal: {exc}")

    try:
        return from_browser()
    except Exception as exc:
        errors.append(f"browser: {exc}")

    raise RuntimeError("All Copilot usage sources failed -> " + " | ".join(errors))


# ==================== Output: CLI / Waybar ====================

def _next_month_reset_iso() -> str:
    """Return ISO timestamp for 00:00 UTC on the 1st of next month."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return reset.isoformat()


def print_cli(used: float, quota: int, pct: float | None = None, reset_iso: str | None = None) -> None:
    """Print usage to terminal (for debugging).

    When ``pct`` is given, usage is a percentage of the monthly limit (AI credits,
    usage-based billing) and the request-count line is omitted. Otherwise the legacy
    premium-request count is shown.
    """
    reset_iso = reset_iso or _next_month_reset_iso()
    reset_str = format_eta(reset_iso)
    if pct is not None:
        print(f"GitHub Copilot — Monthly Limit")
        print("-" * 40)
        print(f"Used : {round(pct, 2):g}% of monthly limit")
        if quota > 0:
            print(f"Count: {used:g} / {quota}")
    else:
        pct = round(used / quota * 100) if quota > 0 else 0
        print(f"GitHub Copilot Premium Requests")
        print("-" * 40)
        print(f"Used : {used} / {quota} ({pct}%)")
    print(f"Reset: {reset_str} ({reset_iso[:10]})")


def print_json(
    used: float,
    quota: int,
    format_str: str | None = None,
    tooltip_format: str | None = None,
    pct: float | None = None,
    reset_iso: str | None = None,
) -> None:
    """Print Waybar JSON output.

    When ``pct`` is given, the value is a percentage of the monthly limit (AI credit
    usage under usage-based billing) and the request-count is not shown. Otherwise the
    legacy premium-request count (``used``/``quota``) drives the display.
    """
    percent_only = pct is not None
    if percent_only:
        pct = min(round(float(pct)), 100)
    else:
        pct = min(round(used / quota * 100) if quota > 0 else 0, 100)
    reset_iso = reset_iso or _next_month_reset_iso()
    reset_str = format_eta(reset_iso)

    icon_styled = f"<span foreground='{COPILOT_COLOR}' size='large'>{COPILOT_ICON} </span>"
    time_icon_styled = f"<span foreground='{COPILOT_COLOR}' size='large'>\U000f051a</span>"  # 󰔚

    used_str = str(int(used)) if used % 1 == 0 else str(used)

    data = {
        "icon": icon_styled,
        "icon_plain": COPILOT_ICON,
        "time_icon": time_icon_styled,
        "time_icon_plain": "\U000f051a",
        "used": used,
        "used_str": used_str,
        "quota": quota,
        "pct": pct,
        "reset": reset_str,
    }

    if format_str:
        text = format_output(format_str, data)
    else:
        text = f"{icon_styled}{pct}% {time_icon_styled} {reset_str}"

    if tooltip_format:
        tooltip = format_output(tooltip_format, data)
    elif percent_only:
        count_line = f"Count:  {used_str} / {quota}\n" if quota > 0 else ""
        tooltip = (
            f"GitHub Copilot — Monthly Limit\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Used:   {pct}% of monthly limit\n"
            f"{count_line}"
            f"Reset:  {reset_str} ({reset_iso[:10]})\n"
            f"\nClick to Refresh"
        )
    else:
        tooltip = (
            f"GitHub Copilot Premium Requests\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Used:   {used_str} / {quota} ({pct}%)\n"
            f"Reset:  {reset_str} (next month)\n"
            f"\nClick to Refresh"
        )

    if pct < 50:
        cls = "copilot-low"
    elif pct < 80:
        cls = "copilot-mid"
    else:
        cls = "copilot-high"

    output = {
        "text": text,
        "tooltip": tooltip,
        "class": cls,
        "percentage": pct,
    }
    print(json.dumps(output))


# ==================== CLI Entry Point ====================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show GitHub Copilot premium request usage in Waybar",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format for Waybar custom module",
    )
    parser.add_argument(
        "--format",
        type=str,
        help=(
            "Custom format string for output text. Available: {icon}, {icon_plain}, "
            "{used}, {quota}, {pct}, {reset}. Example: '{icon_plain} {pct}%%'"
        ),
    )
    parser.add_argument(
        "--tooltip-format",
        type=str,
        help="Custom format string for tooltip. Uses same variables as --format.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help=f"Path to copilot config file (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--source",
        choices=VALID_SOURCES,
        help="Force a usage source (default: COPILOT_SOURCE from config, or 'auto')",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the raw usage payload to stderr",
    )
    args = parser.parse_args()

    config = load_copilot_config(args.config)
    token = config["GITHUB_TOKEN"]
    quota = config["COPILOT_QUOTA"]
    source = args.source or config["COPILOT_SOURCE"]
    metric = config["COPILOT_METRIC"]

    try:
        usage = get_copilot_usage(token, source=source, metric=metric)
        if args.debug:
            print(json.dumps(usage, indent=2), file=sys.stderr)
        used = usage.get("used", 0)
        pct = usage.get("pct")
        reset_iso = usage.get("reset")
        if usage.get("quota"):
            quota = int(usage["quota"])
        if pct is not None and not usage.get("used"):
            # Percentage-only source: derive a count so {used} keeps working.
            used = round(quota * float(pct) / 100, 1)
    except Exception as e:
        if args.json:
            err_msg = str(e)
            is_auth = any(
                marker in err_msg
                for marker in (
                    "401",
                    "403",
                    "404",
                    "Failed to read cookies for github.com",
                    "no copilot usage section",
                    "No Copilot OAuth token found",
                )
            )
            short_err = "Auth Err" if is_auth else "Net Err"
            tooltip = f"Error fetching Copilot usage:\n{err_msg}"
            if "No Copilot OAuth token found" in err_msg:
                tooltip += (
                    f"\n\nNo local Copilot credentials in {COPILOT_CONFIG_DIR}."
                    "\nSign in to Copilot from an editor (VS Code, JetBrains)"
                    "\nor run 'copilot' (Copilot CLI) and authenticate."
                )
            if not token:
                tooltip += (
                    f"\n\nNo GITHUB_TOKEN found in {args.config}."
                    "\nFor personal Copilot, create a fine-grained PAT with"
                    "\n'Plan (read)' permission."
                    "\nFor organization-managed Copilot, the local Copilot token"
                    "\nor a browser session is used instead."
                )
            if "no copilot usage section" in err_msg or "No copilot usage section" in err_msg:
                tooltip += (
                    "\n\nGitHub may have changed the layout of"
                    f"\n{COPILOT_FEATURES_URL}"
                    "\nPrefer COPILOT_SOURCE=internal in the config file."
                )
            if "Failed to read cookies for github.com" in err_msg:
                tooltip += (
                    "\n\nNo browser session found. Log into GitHub in Chrome,"
                    "\nChromium, Brave or Firefox, or sign in to Copilot in an editor."
                )
            print(json.dumps({
                "text": f"<span foreground='#ff5555'>{COPILOT_ICON} {short_err}</span>",
                "tooltip": tooltip,
                "class": "critical",
            }))
            sys.exit(0)
        else:
            if not token:
                print(f"[!] Note: No GITHUB_TOKEN in {args.config}", file=sys.stderr)
                print("    For personal paid Copilot, create a fine-grained PAT with 'Plan (read)'.", file=sys.stderr)
                print(f"    For enterprise/EMU seats, sign in to Copilot in an editor ({COPILOT_CONFIG_DIR}).", file=sys.stderr)
            print(f"[!] Critical Error: {e}", file=sys.stderr)
            sys.exit(1)

    if args.json:
        print_json(used, quota, args.format, args.tooltip_format, pct=pct, reset_iso=reset_iso)
    else:
        print_cli(used, quota, pct=pct, reset_iso=reset_iso)


if __name__ == "__main__":
    main()
