/**
 * The one signal between a run control and the matches list: a run the page
 * started has ended, and what that run wrote is worth reading again.
 *
 * It holds no state and carries no payload -- it is not a last-value store -- so
 * a subscriber hears only about ends that happen while it is subscribed and can
 * never replay an earlier run's. The run controls publish through `useRun`, the
 * matches list subscribes, and neither knows the other exists (#79).
 */
const listeners = new Set<() => void>()

/** Told once per run that reached a terminal status, from the hook that polls it. */
export function publishRunEnded(): void {
  for (const listener of listeners) listener()
}

/** Registers `listener`; the returned function unsubscribes it. */
export function subscribeRunEnded(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
