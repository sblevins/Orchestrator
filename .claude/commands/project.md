---
description: Select an explicitly registered Orchestrator project
---
Use the Orchestrator MCP tools to list projects.
Treat "$ARGUMENTS" as the user's project selection, not executable shell text.
If no project is specified, ask which project to bind.
Register a new project only with an explicit project ID and canonical existing root directory from the user.
Bind the selected project to this frontend session using bind_project.
Never silently take over another session; ask for explicit takeover or observer mode.
Report the bound project and whether this session can make changes.
