"""
Installer: auto-configure nodriver-mcp into various MCP clients.
"""

import json
import os
import sys
import tempfile

try:
    import tomllib
except ImportError:
    tomllib = None

import tomli_w

from .installer_data import (
    MCP_SERVER_NAME,
    GLOBAL_SPECIAL_JSON_STRUCTURES,
    PROJECT_SPECIAL_JSON_STRUCTURES,
    get_global_configs,
    get_project_configs,
    resolve_client_name,
)
from .installer_tui import interactive_select


def _find_project_dir() -> str | None:
    """Find the nodriver-mcp project root (contains pyproject.toml + src/)."""
    pkg_dir = os.path.dirname(os.path.realpath(__file__))
    # src/nodriver_mcp/ -> src/ -> project root
    candidate = os.path.dirname(os.path.dirname(pkg_dir))
    if os.path.exists(os.path.join(candidate, "pyproject.toml")):
        return candidate
    return None


def _is_uv_project() -> bool:
    """Check if we're running inside a uv-managed project."""
    proj = _find_project_dir()
    if proj and os.path.exists(os.path.join(proj, "uv.lock")):
        return True
    return False


def _find_uv() -> str | None:
    """Find the uv executable."""
    import shutil
    return shutil.which("uv")


def generate_mcp_config(client_name: str = "Generic") -> dict:
    """Generate the MCP server config dict for a given client.

    Strategy:
    1. If installed in a uv project (uv.lock exists), use `uv run --directory <project> nodriver-mcp`
    2. Otherwise, use the Python executable + `-m nodriver_mcp` (works for uv tool install / pip install)
    """
    proj_dir = _find_project_dir()
    uv = _find_uv()

    # Prefer uv run --directory if this is a uv-managed project
    if _is_uv_project() and uv and proj_dir:
        if client_name == "Opencode":
            return {
                "type": "local",
                "command": [uv, "run", "--directory", proj_dir, "nodriver-mcp"],
            }
        if client_name == "Codex":
            return {
                "command": uv,
                "args": ["run", "--directory", proj_dir, "nodriver-mcp"],
            }
        return {
            "command": uv,
            "args": ["run", "--directory", proj_dir, "nodriver-mcp"],
        }

    # Fallback: use python -m nodriver_mcp (works for uv tool install / pip install)
    python = sys.executable
    if client_name == "Opencode":
        return {
            "type": "local",
            "command": [python, "-m", "nodriver_mcp"],
        }
    if client_name == "Codex":
        return {
            "command": python,
            "args": ["-m", "nodriver_mcp"],
        }
    return {
        "command": python,
        "args": ["-m", "nodriver_mcp"],
    }


def print_mcp_config():
    print(json.dumps(
        {"mcpServers": {MCP_SERVER_NAME: generate_mcp_config()}},
        indent=2,
    ))


def _read_config(path: str, *, is_toml: bool) -> dict | None:
    try:
        if is_toml:
            if tomllib is None:
                return None
            with open(path, "rb") as f:
                data = f.read()
                return tomllib.loads(data.decode()) if data else {}
        with open(path, "r", encoding="utf-8") as f:
            data = f.read().strip()
            return json.loads(data) if data else {}
    except (json.JSONDecodeError, OSError, Exception):
        return None


def _write_config(path: str, config: dict, *, is_toml: bool):
    config_dir = os.path.dirname(path)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)
    suffix = ".toml" if is_toml else ".json"
    fd, tmp = tempfile.mkstemp(dir=config_dir, prefix=".tmp_", suffix=suffix)
    try:
        if is_toml:
            with os.fdopen(fd, "wb") as f:
                f.write(tomli_w.dumps(config).encode("utf-8"))
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def _get_servers_view(
    config: dict,
    client_name: str,
    is_toml: bool,
    special: dict[str, tuple[str | None, str]],
) -> dict:
    if is_toml:
        return config.setdefault("mcp_servers", {})
    if client_name in special:
        top, nested = special[client_name]
        if top is None:
            return config.setdefault(nested, {})
        return config.setdefault(top, {}).setdefault(nested, {})
    return config.setdefault("mcpServers", {})


def install_mcp_servers(
    *,
    uninstall: bool = False,
    only: list[str] | None = None,
    project: bool = False,
    project_dir: str | None = None,
):
    if project:
        configs = get_project_configs(project_dir or os.getcwd())
        special = PROJECT_SPECIAL_JSON_STRUCTURES
    else:
        configs = get_global_configs()
        special = GLOBAL_SPECIAL_JSON_STRUCTURES

    if not configs:
        print(f"Unsupported platform: {sys.platform}")
        return

    # Filter targets
    if only is not None:
        available = list(configs.keys())
        filtered = {}
        for t in only:
            resolved = resolve_client_name(t, available)
            if resolved is None:
                print(f"Unknown client: '{t}', use --list-clients to see available targets")
            elif resolved not in filtered:
                filtered[resolved] = configs[resolved]
        configs = filtered

    if not configs:
        return

    changed = 0
    for name, (config_dir, config_file) in configs.items():
        config_path = os.path.join(config_dir, config_file)
        is_toml = config_file.endswith(".toml")

        if not os.path.exists(config_dir):
            if project and not uninstall:
                os.makedirs(config_dir, exist_ok=True)
            else:
                action = "uninstall" if uninstall else "install"
                print(f"Skipping {name} {action}\n  Config: {config_path} (directory not found)")
                continue

        config = {}
        if os.path.exists(config_path):
            config = _read_config(config_path, is_toml=is_toml)
            if config is None:
                kind = "TOML" if is_toml else "JSON"
                action = "uninstall" if uninstall else "install"
                print(f"Skipping {name} {action}\n  Config: {config_path} (invalid {kind})")
                continue

        servers = _get_servers_view(config, name, is_toml, special)

        if uninstall:
            if MCP_SERVER_NAME not in servers:
                print(f"Skipping {name} uninstall\n  Config: {config_path} (not installed)")
                continue
            del servers[MCP_SERVER_NAME]
        else:
            servers[MCP_SERVER_NAME] = generate_mcp_config(client_name=name)

        _write_config(config_path, config, is_toml=is_toml)
        action = "Uninstalled" if uninstall else "Installed"
        print(f"{action} {name} MCP server (restart client to apply)\n  Config: {config_path}")
        changed += 1

    if not uninstall and changed == 0:
        print("No MCP servers were installed. For manual configuration use:\n")
        print_mcp_config()


def list_available_clients():
    configs = get_global_configs()
    if not configs:
        print(f"Unsupported platform: {sys.platform}")
        return

    print("Available install targets:\n")
    for name, (config_dir, _) in configs.items():
        status = "found" if os.path.exists(config_dir) else "not found"
        print(f"  {name:<20} ({status})")

    print("\nUsage examples:")
    print("  nodriver-mcp install                    # Interactive selection")
    print("  nodriver-mcp install claude,cursor       # Specific clients")
    print("  nodriver-mcp install --scope project     # Project-level config")
    print("  nodriver-mcp uninstall cursor            # Uninstall from client")


def _is_installed(name: str, config_dir: str, config_file: str, special: dict, is_project: bool) -> bool:
    """Check if nodriver MCP server is already installed for a given client."""
    config_path = os.path.join(config_dir, config_file)
    is_toml = config_file.endswith(".toml")
    if not os.path.exists(config_path):
        return False
    config = _read_config(config_path, is_toml=is_toml)
    if config is None:
        return False
    servers = _get_servers_view(config, name, is_toml, special)
    return MCP_SERVER_NAME in servers


def run_install_command(*, uninstall: bool, targets_str: str, project: bool):
    if targets_str:
        targets = [t.strip() for t in targets_str.split(",") if t.strip()]
        install_mcp_servers(uninstall=uninstall, only=targets, project=project)
        return

    # Interactive mode
    if not sys.stdin.isatty():
        print("Non-interactive terminal. Please specify client targets, e.g.: nodriver-mcp install claude,cursor")
        return

    configs = get_global_configs() if not project else get_project_configs(os.getcwd())
    special = PROJECT_SPECIAL_JSON_STRUCTURES if project else GLOBAL_SPECIAL_JSON_STRUCTURES
    items = []
    for name, (config_dir, config_file) in configs.items():
        installed = _is_installed(name, config_dir, config_file, special, project)
        if uninstall:
            if not installed:
                continue  # Only show installed clients in uninstall mode
        items.append((name, installed))
    action = "uninstall from" if uninstall else "install to"
    selected = interactive_select(items, f"Select MCP clients to {action}:", show_status=True)
    if selected is None:
        print("Cancelled.")
        return
    install_mcp_servers(uninstall=uninstall, only=selected, project=project)
