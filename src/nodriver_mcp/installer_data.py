"""
MCP client configuration paths for all supported platforms.
"""

import os
import sys

MCP_SERVER_NAME = "nodriver"

CLIENT_ALIASES: dict[str, str] = {
    "vscode": "VS Code",
    "vs-code": "VS Code",
    "claude": "Claude Desktop",
    "claude-desktop": "Claude Desktop",
    "claude-app": "Claude Desktop",
    "claude-code": "Claude Code",
    "roo": "Roo Code",
    "roocode": "Roo Code",
    "gemini": "Gemini CLI",
    "copilot": "Copilot CLI",
    "amazonq": "Amazon Q",
    "amazon-q": "Amazon Q",
    "lmstudio": "LM Studio",
    "lm-studio": "LM Studio",
    "augment": "Augment Code",
}

# Special JSON structures for global configs
GLOBAL_SPECIAL_JSON_STRUCTURES: dict[str, tuple[str | None, str]] = {
    "VS Code": ("mcp", "servers"),
    "Opencode": (None, "mcp"),
}

# Project-level config definitions
PROJECT_LEVEL_CONFIGS: dict[str, tuple[str, str]] = {
    "Claude Code": ("", ".mcp.json"),
    "Cursor": (".cursor", "mcp.json"),
    "VS Code": (".vscode", "mcp.json"),
    "Windsurf": (".windsurf", "mcp.json"),
}

PROJECT_SPECIAL_JSON_STRUCTURES: dict[str, tuple[str | None, str]] = {
    "VS Code": (None, "servers"),
}


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def get_global_configs() -> dict[str, tuple[str, str]]:
    if sys.platform == "darwin":
        return {
            "Claude Desktop": (
                _home("Library", "Application Support", "Claude"),
                "claude_desktop_config.json",
            ),
            "Claude Code": (_home(), ".claude.json"),
            "Cursor": (_home(".cursor"), "mcp.json"),
            "Windsurf": (_home(".codeium", "windsurf"), "mcp_config.json"),
            "Codex": (_home(".codex"), "config.toml"),
            "Gemini CLI": (_home(".gemini"), "settings.json"),
            "Copilot CLI": (_home(".copilot"), "mcp-config.json"),
            "Kiro": (_home(".kiro", "settings"), "mcp.json"),
            "VS Code": (
                _home("Library", "Application Support", "Code", "User"),
                "settings.json",
            ),
            "Cline": (
                _home("Library", "Application Support", "Code", "User",
                      "globalStorage", "saoudrizwan.claude-dev", "settings"),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                _home("Library", "Application Support", "Code", "User",
                      "globalStorage", "rooveterinaryinc.roo-cline", "settings"),
                "mcp_settings.json",
            ),
            "Amazon Q": (_home(".aws", "amazonq"), "mcp_config.json"),
            "Warp": (_home(".warp"), "mcp_config.json"),
            "Opencode": (_home(".config", "opencode"), "opencode.json"),
            "Trae": (_home(".trae"), "mcp_config.json"),
        }
    elif sys.platform == "win32":
        appdata = os.getenv("APPDATA", "")
        return {
            "Claude Desktop": (
                os.path.join(appdata, "Claude"),
                "claude_desktop_config.json",
            ),
            "Claude Code": (_home(), ".claude.json"),
            "Cursor": (_home(".cursor"), "mcp.json"),
            "Windsurf": (_home(".codeium", "windsurf"), "mcp_config.json"),
            "Codex": (_home(".codex"), "config.toml"),
            "Gemini CLI": (_home(".gemini"), "settings.json"),
            "Copilot CLI": (_home(".copilot"), "mcp-config.json"),
            "Kiro": (_home(".kiro", "settings"), "mcp.json"),
            "VS Code": (
                os.path.join(appdata, "Code", "User"),
                "settings.json",
            ),
            "Cline": (
                os.path.join(appdata, "Code", "User", "globalStorage",
                             "saoudrizwan.claude-dev", "settings"),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                os.path.join(appdata, "Code", "User", "globalStorage",
                             "rooveterinaryinc.roo-cline", "settings"),
                "mcp_settings.json",
            ),
            "Amazon Q": (_home(".aws", "amazonq"), "mcp_config.json"),
            "Warp": (_home(".warp"), "mcp_config.json"),
            "Opencode": (_home(".config", "opencode"), "opencode.json"),
            "Trae": (_home(".trae"), "mcp_config.json"),
        }
    elif sys.platform == "linux":
        return {
            "Claude Code": (_home(), ".claude.json"),
            "Cursor": (_home(".cursor"), "mcp.json"),
            "Windsurf": (_home(".codeium", "windsurf"), "mcp_config.json"),
            "Codex": (_home(".codex"), "config.toml"),
            "Gemini CLI": (_home(".gemini"), "settings.json"),
            "Copilot CLI": (_home(".copilot"), "mcp-config.json"),
            "Kiro": (_home(".kiro", "settings"), "mcp.json"),
            "VS Code": (
                _home(".config", "Code", "User"),
                "settings.json",
            ),
            "Cline": (
                _home(".config", "Code", "User", "globalStorage",
                      "saoudrizwan.claude-dev", "settings"),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                _home(".config", "Code", "User", "globalStorage",
                      "rooveterinaryinc.roo-cline", "settings"),
                "mcp_settings.json",
            ),
            "Amazon Q": (_home(".aws", "amazonq"), "mcp_config.json"),
            "Warp": (_home(".warp"), "mcp_config.json"),
            "Opencode": (_home(".config", "opencode"), "opencode.json"),
            "Trae": (_home(".trae"), "mcp_config.json"),
        }
    return {}


def get_project_configs(project_dir: str) -> dict[str, tuple[str, str]]:
    result = {}
    for name, (subdir, config_file) in PROJECT_LEVEL_CONFIGS.items():
        config_dir = os.path.join(project_dir, subdir) if subdir else project_dir
        result[name] = (config_dir, config_file)
    return result


def resolve_client_name(input_name: str, available: list[str]) -> str | None:
    lower = input_name.strip().lower()
    for c in available:
        if c.lower() == lower:
            return c
    if lower in CLIENT_ALIASES:
        target = CLIENT_ALIASES[lower]
        if target in available:
            return target
    matches = [c for c in available if lower in c.lower()]
    return matches[0] if len(matches) == 1 else None
