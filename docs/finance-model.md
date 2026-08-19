# Finance model

FinanceConnection is the truth-state switch. Without a connected provider, finance APIs return not_connected with null metrics.

The ledger supports Revenue, Invoice, Payment, Cost, Budget, Expense, SoftwareCost, AIUsageCost, and ClientEconomics. Every imported financial record carries a source. ClientEconomics derives revenue, labor, software, AI, other cost, gross contribution, and margin for a period.

No dashboard, report, fixture, or API may invent an MRR, margin, invoice, or cost value. Mutating finance data requires a writable workspace member; connection changes require an organization owner/admin.
