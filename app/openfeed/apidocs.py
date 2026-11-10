"""The /docs API reference, drawn from the OpenAPI schema on this server.

FastAPI's built-in /docs (Swagger UI) and /redoc pages load their JavaScript, CSS and a
font from cdn.jsdelivr.net and Google Fonts, so opening them sent the reader's browser to
third parties. This page lists the same routes without loading anything from elsewhere.
The machine-readable schema stays at /openapi.json.
"""

from __future__ import annotations

from html import escape
from typing import Any

_METHODS = ("get", "post", "put", "patch", "delete")

_STYLE = """
  :root { color-scheme: light dark; --bg: #fff; --fg: #18181b; --muted: #63636b; --line: #e4e4e7; --chip: #f4f4f5; }
  @media (prefers-color-scheme: dark) { :root { --bg: #0a0a0b; --fg: #ededef; --muted: #9a9aa3; --line: #232327; --chip: #1a1a1d; } }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 960px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; }
  p { color: var(--muted); margin: 0 0 12px; } a { color: inherit; }
  ul { list-style: none; margin: 0; padding: 0; border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
  li { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 4px 12px; padding: 8px 12px; border-top: 1px solid var(--line); }
  li:first-child { border-top: 0; }
  code { font: 12.5px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
  .method { font-weight: 650; font-size: 11.5px; text-transform: uppercase; background: var(--chip); border-radius: 6px; padding: 2px 6px; justify-self: start; }
  small { grid-column: 2; color: var(--muted); }
"""


def reference_html(schema: dict[str, Any]) -> str:
    info = schema.get("info") or {}
    groups: dict[str, list[str]] = {}
    for path, operations in sorted((schema.get("paths") or {}).items()):
        if not isinstance(operations, dict):
            continue
        for method in _METHODS:
            operation = operations.get(method)
            if not isinstance(operation, dict):
                continue
            tag = str((operation.get("tags") or ["other"])[0])
            summary = str(operation.get("summary") or "")
            groups.setdefault(tag, []).append(
                f'<li><span class="method">{escape(method)}</span><code>{escape(path)}</code>'
                f'{f"<small>{escape(summary)}</small>" if summary else ""}</li>'
            )
    sections = "".join(
        f'<section><h2 id="tag-{escape(tag, quote=True)}">{escape(tag)}</h2><ul>{"".join(rows)}</ul></section>'
        for tag, rows in sorted(groups.items())
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(str(info.get("title") or "API"))} API reference</title>'
        '<link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon-32.png">'
        f"<style>{_STYLE}</style></head><body><main>"
        f'<h1>{escape(str(info.get("title") or "API"))} API <small>{escape(str(info.get("version") or ""))}</small></h1>'
        '<p>Every route on this server. The full machine-readable schema, with request and response '
        'bodies, is at <a href="/openapi.json">/openapi.json</a>; any OpenAPI viewer can open it.</p>'
        f"{sections}</main></body></html>"
    )
