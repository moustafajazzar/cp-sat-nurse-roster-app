import { useLayoutEffect, useRef, useState } from "react"

/** The row a page's form is editing. When the editing ends, focus goes back to the button that started it,
 *  so a keyboard stays in its place in the table. */
export function useEditing<Row>() {
  const [editing, setEditing] = useState<Row | null>(null)
  const returnTo = useRef<HTMLElement | null>(null)

  // In the commit that takes the form away, as `useFocusOnOpen` does: a late effect could take focus from wherever someone
  // has gone since.
  useLayoutEffect(() => {
    if (editing !== null || !returnTo.current) return
    returnTo.current.focus()
    returnTo.current = null
  }, [editing])

  return {
    editing,
    start: (row: Row, button: HTMLElement) => {
      returnTo.current = button
      setEditing(row)
    },
    finish: () => setEditing(null),
    /** When the row itself has gone, and its button with it. */
    drop: () => {
      returnTo.current = null
      setEditing(null)
    },
  }
}
