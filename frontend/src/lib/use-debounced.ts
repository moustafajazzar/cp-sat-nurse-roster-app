import { useDebouncedValue } from "@tanstack/react-pacer/debouncer"

/** How long editing has to pause before its result is asked for. Short in a test, so a test sees the answer without waiting. */
export const DEBOUNCE_MS = import.meta.env.MODE === "test" ? 1 : 400

/** `value` once it has stopped changing for a moment. The one hook in front of TanStack Pacer, which is beta by its
 *  own account, so an API change is absorbed here. A string, so that what is equal is the same value: an object rebuilt on each
 *  render would never settle. */
export function useDebounced(value: string): string {
  const [settled] = useDebouncedValue(value, { wait: DEBOUNCE_MS })
  return settled
}
