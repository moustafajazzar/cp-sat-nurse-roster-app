import type { ThrottleRefusal } from "@/api/generated/model"
import { hospitalClock } from "@/lib/utils"

/** The 429 of the login and Change password: too many failed logins from the client's address (the throttle). The time
 *  carries the hospital's offset, so it is the hospital's clock whatever the browser's time zone. A 429 without one (not
 *  the application's) still gets the words. */
export function throttledWords(refusal: Partial<ThrottleRefusal> | undefined): string {
  const until = refusal?.throttled_until
  return until
    ? `Too many failed logins from this network. Try again at ${hospitalClock(until)}.`
    : "Too many failed logins from this network. Try again later."
}
