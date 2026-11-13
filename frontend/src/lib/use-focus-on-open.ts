import { type DependencyList, useLayoutEffect } from "react"

/** Puts focus where a form starts, in the same commit that puts the form on the page. A plain effect may run late on a busy
 *  page, after someone has already moved to another field, and pull focus back from under their typing: the browser suite
 *  once saw a nurse's name typed into Hospital ID that way. */
export function useFocusOnOpen(focus: () => void, deps: DependencyList) {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useLayoutEffect(focus, deps)
}
