# ADR 0008: Fail-closed GitHub write reconciliation

Status: accepted for Phase 4.

Check Run and pull-request review creates reserve durable operations before POST. A complete bounded
search with zero exact matches authorizes the first POST only while unattempted. After a POST may have
reached GitHub, zero matches never authorizes another POST. External IDs and HMAC markers are
reconciliation identities, not provider uniqueness constraints.

Only an explicit non-creating anchor-validation rejection enters `known_rejected` and permits one
same-head, complete-zero-match, summary-only fallback. Ambiguous outcomes reconcile only. Exhaustion
retains an unresolved audit state and never claims provider success or absence.
