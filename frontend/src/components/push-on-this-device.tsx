import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import type { PushState } from "@/api/generated/model"
import { Button } from "@/components/ui/button"
import { browserSubscription, deviceState, PUSH_STATE_KEY, pushSupport, turnOff, turnOffEverywhere, turnOn, TurnOnFailed } from "@/lib/push"

/** What the status line says when Turn on stops; a refusal shows the blocked state instead. */
const SAID: Record<Exclude<TurnOnFailed["step"], "blocked">, string> = {
  dismissed: "Pushes were not turned on: the browser was not given permission.",
  subscribe: "This browser could not subscribe to pushes. Try again later.",
  server: "Pushes could not be turned on. Try again.",
}

const devicesOn = (count: number) => `Pushes are on on ${count} ${count === 1 ? "device" : "devices"}.`

/** The Notifications page's "On this device": whether pushes reach this device, and on how many of the user's they
 *  are on. Nothing here names a device. */
export function PushOnThisDevice() {
  const queryClient = useQueryClient()
  const [support, setSupport] = useState(pushSupport)
  const [working, setWorking] = useState(false)
  const [failure, setFailure] = useState("")
  const state = useQuery({ queryKey: PUSH_STATE_KEY, queryFn: deviceState })
  const answer = state.data

  const settle = (next: PushState) => queryClient.setQueryData(PUSH_STATE_KEY, next)
  const run = async (work: () => Promise<void>) => {
    setWorking(true)
    setFailure("")
    try {
      await work()
    } finally {
      setWorking(false)
    }
  }

  const onTurnOn = (publicKey: string) => run(async () => {
    try {
      settle(await turnOn(publicKey))
    } catch (error) {
      const step = error instanceof TurnOnFailed ? error.step : "subscribe"
      if (step === "blocked") setSupport("blocked")
      else setFailure(SAID[step])
    }
  })
  const onTurnOff = () => run(async () => {
    try {
      const subscription = await browserSubscription()
      settle(subscription ? await turnOff(subscription) : await deviceState())
    } catch {
      setFailure("Pushes could not be turned off. Try again.")
    }
  })
  const onTurnOffEverywhere = () => run(async () => {
    try {
      settle(await turnOffEverywhere())
    } catch {
      setFailure("Pushes could not be turned off on every device. Try again.")
    }
  })

  let said: string
  let action = null
  if (support === "unsupported") said = "This browser cannot receive pushes."
  else if (support === "add_to_home_screen") said = "Add the app to your Home Screen to turn these on."
  else if (support === "blocked") said = "Notifications are blocked for this site in your browser's settings."
  else if (!answer) said = state.isError ? "Whether pushes are on could not be read. Reload the page to try again." : "Loading…"
  else if (answer.this_device) {
    said = "Pushes are on for this device."
    action = <Button variant="outline" size="sm" onClick={onTurnOff} disabled={working}>Turn off</Button>
  } else {
    said = "You are told when a swap, a request window or a roster waits for you, and nothing more."
    action = <Button size="sm" onClick={() => onTurnOn(answer.public_key)} disabled={working}>Turn on</Button>
  }

  return (
    <section aria-labelledby="push-heading" className="flex flex-col gap-2 rounded-lg border p-4">
      <h2 id="push-heading" className="font-medium">On this device</h2>
      <div role="status" aria-labelledby="push-heading" className="flex flex-col gap-1">
        <p>{said}</p>
        {support === "add_to_home_screen" && <p className="text-sm text-muted-foreground">In Safari, tap Share, then Add to Home Screen.</p>}
        {failure && <p className="text-destructive">{failure}</p>}
      </div>
      {action && <div>{action}</div>}
      {answer && answer.devices > 0 && (
        <div className="flex flex-wrap items-center gap-3 border-t pt-2">
          <p>{devicesOn(answer.devices)}</p>
          <Button variant="outline" size="sm" onClick={onTurnOffEverywhere} disabled={working}>Turn off on every device</Button>
        </div>
      )}
    </section>
  )
}
