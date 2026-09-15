"""Home-automation integrations namespace.

Each provider (eero, and future home-automation providers) owns its own
module here and exposes pause/resume/status-style operations that
`api/routes/home.py`, the agent tool registry, and the MCP bridge all call
into. Nothing in this package talks to a vendor directly except the
provider modules themselves.
"""
