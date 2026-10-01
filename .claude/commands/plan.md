---
description: Request a project plan and independent review through the shared supervisor
---
Check the bound project with the Orchestrator MCP status tool.
Treat "$ARGUMENTS" as the planning request; ask for the intended goal if it is empty.
Read the current brief, constraints, decisions, and open questions before calling start_plan with the request.
Do not silently bind or take over a project.
Use the supervisor's plan and task IDs, not a second local scheduler or direct worker invocation.
Report that planning was queued, not that planning or review succeeded.
Handle later feedback through updates and explicit acknowledgments.
Specialist planning and review do not authorize general-purpose implementation workers.
