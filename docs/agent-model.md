# Agent model

AgentRole defines purpose and default capabilities. Agent defines model, tools, allowed workspaces, memory access, and write permissions.

AgentTask and AgentQueueItem represent queued work. AgentRun records workspace, timing, tokens, cost, status, output, and error. ToolCall and RunTrace make execution inspectable. RunOutput retains results and source references; RunError retains failure detail and retryability.

Seeded roles:

- Sol: advisor, reviewer, and risk detector
- Terra: deep builder and implementation agent
- Luna: execution and consistency operations

Agents cannot use an undeclared tool or enter an undeclared workspace. Knowledge writes use proposals unless an explicit policy permits canonical writes. External and one-way actions use ApprovalRequest.
