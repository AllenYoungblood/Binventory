"""Business logic, callable without HTTP.

Route handlers should read request data, call a service, and shape a response —
nothing more. Keeping logic here is what lets the planned MCP integration, a
CLI, or an automation script reuse it directly instead of making HTTP calls to
our own process.
"""
