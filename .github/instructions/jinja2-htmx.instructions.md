---
applyTo: '**/*.html'
description: 'Jinja2 and HTMX template conventions for the Fabric Unified Permission Hub'
---

# Jinja2 & HTMX Template Standards

## Template Structure
- Base template: `templates/base.html` — all pages extend this
- Full pages: load skeleton HTML, then use HTMX to fetch data partials
- Data partials: prefixed with `_` (e.g., `_home_data.html`) — return HTML fragments
- Loading indicator: use `_loading.html` partial for HTMX swap targets

## HTMX Patterns
- Use `hx-get` to fetch data partials after page load
- Use `hx-target` to specify where the response HTML goes
- Use `hx-swap="innerHTML"` for replacing content within a container
- Use `hx-trigger="load"` for auto-fetching on page load
- Support `?refresh=1` query param to bypass server-side caches

## Security
- Jinja2 autoescaping is ON — never use `|safe` on user-controlled data
- Never use `{% raw %}` blocks with user input
- Template file paths must be hardcoded — never derived from user input
- Don't expose internal IDs, token values, or system paths in HTML output

## Conventions
- Use `{{ url_for() }}` for generating internal links
- Keep logic minimal in templates — compute in Python route handlers
- Use Jinja2 macros for repeated UI patterns
- Organize templates by feature in subdirectories (databricks/, fabric/, pairings/, etc.)
