/** Pushes on this device: what the browser can do, its own subscription, and the calls that turn this
 *  device on and off. Decided by feature detection and the permission's state alone, never by a browser's name. Nothing is
 *  sent from here; the service worker shows what arrives. */
import { readPushState, turnPushOff, turnPushOffEverywhere, turnPushOn } from "@/api/generated/endpoints"
import type { PushState } from "@/api/generated/model"

/** The query that holds this device's state, for the page and for the start's renewal. */
export const PUSH_STATE_KEY = ["push", "this-device"] as const

/** Why this device has no Turn on button, or `ready` when it may have one. */
export type PushSupport = "unsupported" | "add_to_home_screen" | "blocked" | "ready"

/** An iPhone or iPad: Safari's own `navigator.standalone`, on a touch screen (an iPad says it is a Mac, and a Mac has none). */
function isAppleTouchDevice(): boolean {
  return "standalone" in navigator && navigator.maxTouchPoints > 0
}

function runsFromTheHomeScreen(): boolean {
  return (navigator as Navigator & { standalone?: boolean }).standalone === true
    || window.matchMedia?.("(display-mode: standalone)").matches === true
}

export function pushSupport(): PushSupport {
  // Before the next test: Safari outside a Home Screen web app has no PushManager at all.
  if (isAppleTouchDevice() && !runsFromTheHomeScreen()) return "add_to_home_screen"
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return "unsupported"
  if (Notification.permission === "denied") return "blocked"
  return "ready"
}

/** As `bytesOf` in public/sw.js, which cannot import this module. */
function bytesOf(base64url: string): Uint8Array<ArrayBuffer> {
  const base64 = base64url.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(base64url.length / 4) * 4, "=")
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0))
}

function base64urlOf(buffer: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

/** Whether a browser subscription was made with this public key. */
function boundTo(subscription: PushSubscription, publicKey: string): boolean {
  const key = subscription.options.applicationServerKey
  return key !== null && base64urlOf(key) === publicKey
}

async function registration(): Promise<ServiceWorkerRegistration | undefined> {
  return (await navigator.serviceWorker.getRegistration("/")) ?? undefined
}

/** The browser's own subscription, when it can have one. */
export async function browserSubscription(): Promise<PushSubscription | null> {
  if (pushSupport() !== "ready") return null
  return (await (await registration())?.pushManager.getSubscription()) ?? null
}

/** This device as the server holds it, for whatever the browser can do. */
export async function deviceState(): Promise<PushState> {
  const subscription = await browserSubscription()
  return readPushState({ endpoint: subscription?.endpoint ?? null })
}

function subscriptionBody(subscription: PushSubscription) {
  const { endpoint, keys } = subscription.toJSON()
  return { endpoint: endpoint!, keys: { p256dh: keys!.p256dh, auth: keys!.auth } }
}

/** Why Turn on stopped, in the words the page says. */
export class TurnOnFailed extends Error {
  constructor(readonly step: "dismissed" | "blocked" | "subscribe" | "server") {
    super(step)
  }
}

/** Turn on, from inside the tap: iOS asks for permission only there, so the question comes before anything is awaited. */
export async function turnOn(publicKey: string): Promise<PushState> {
  const permission = await Notification.requestPermission()
  if (permission === "denied") throw new TurnOnFailed("blocked")
  if (permission !== "granted") throw new TurnOnFailed("dismissed")
  let subscription: PushSubscription
  try {
    const pushManager = (await registration())?.pushManager
    if (!pushManager) throw new Error("no service worker")
    const existing = await pushManager.getSubscription()
    // A browser holds one subscription; one made with another key would make subscribing fail.
    if (existing && !boundTo(existing, publicKey)) await existing.unsubscribe()
    subscription = await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytesOf(publicKey) })
  } catch {
    throw new TurnOnFailed("subscribe")
  }
  try {
    return await turnPushOn(subscriptionBody(subscription))
  } catch {
    throw new TurnOnFailed("server")
  }
}

/** Turn off: the server first, so a failure leaves this device as it was, then the browser's own. */
export async function turnOff(subscription: PushSubscription): Promise<PushState> {
  const answer = await turnPushOff({ endpoint: subscription.endpoint })
  await subscription.unsubscribe().catch(() => undefined)
  return answer
}

/** Every device of the user, from this one, for a phone that was lost; this browser's own subscription goes too. */
export async function turnOffEverywhere(): Promise<PushState> {
  const answer = await turnPushOffEverywhere()
  await (await browserSubscription())?.unsubscribe().catch(() => undefined)
  return answer
}

/** Log out's first step: this device stops being reached, whatever the server answers, since the next person at a shared
 *  computer must get none of these pushes. Never throws, so Log out goes on. */
export async function turnThisDeviceOffForLogOut(): Promise<void> {
  try {
    // Not `browserSubscription`: notifications blocked since turning on leave a subscription the server still holds.
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return
    const subscription = (await (await registration())?.pushManager.getSubscription()) ?? null
    if (!subscription) return
    const endpoint = subscription.endpoint
    await subscription.unsubscribe().catch(() => undefined)
    await turnPushOff({ endpoint })
  } catch {
    // Signing out goes on regardless.
  }
}

/** At each start of the app for a signed-in user (public/sw.js's `renewSubscription` does the same for a renewal by the browser): a subscription the server holds but bound to a public key since replaced
 *  (`rotate_push_keys`) is replaced, with no question, since the permission is already given. One the server does not hold is
 *  left alone, so Turn off on every device from another one is not undone by a visit. Never throws. */
export async function renewAfterARotation(): Promise<boolean> {
  try {
    if (pushSupport() !== "ready" || Notification.permission !== "granted") return false
    const pushManager = (await registration())?.pushManager
    const old = await pushManager?.getSubscription()
    if (!pushManager || !old) return false
    const state = await readPushState({ endpoint: old.endpoint })
    if (!state.this_device || boundTo(old, state.public_key)) return false
    await old.unsubscribe()
    const renewed = await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytesOf(state.public_key) })
    await turnPushOn({ ...subscriptionBody(renewed), replaces: old.endpoint })
    return true
  } catch {
    return false
  }
}
