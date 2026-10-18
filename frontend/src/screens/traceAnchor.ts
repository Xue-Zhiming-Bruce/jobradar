/**
 * The one spelling of a trace's anchor: the id `TelemetryScreen` gives one trace's
 * table, and the fragment `RunDigestNow` links its run to -- the app has no router,
 * so a run reaches its trace as a same-page fragment (#95 AC2).
 *
 * The trace id is the contract's bounded `[A-Za-z0-9._-]`, at most 64 characters
 * (#90, #102), so a prefixed id is a safe fragment as-is.
 */
export const traceAnchorId = (traceId: string) => `trace-${traceId}`
