"""S28.3a §6.6 — meinchat must not import any downstream plugin.

Core-agnosticism for the plugin layer: meinchat is the base; meinchat-plus /
meinchat-enterprise depend on IT, never the reverse. A hard import would couple
them and break the "plugin-free still works" guarantee.
"""
import os
import re

_MEINCHAT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "meinchat")
)
_BANNED = re.compile(
    r"(?:from|import)\s+plugins\.(meinchat_plus|meinchat_enterprise)\b"
)


def test_meinchat_has_no_downstream_plugin_imports():
    offenders = []
    for root, _dirs, files in os.walk(_MEINCHAT_DIR):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, 1):
                    if _BANNED.search(line):
                        offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "meinchat imports a downstream plugin:\n" + "\n".join(
        offenders
    )
