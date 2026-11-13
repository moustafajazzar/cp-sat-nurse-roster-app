import { useState } from "react"
import { createStore, useStore } from "zustand"

import type { EditBody } from "@/api/generated/model"

/** A cell, as `hospital_id|date`. */
export const cellKey = (hospitalId: string, date: string) => `${hospitalId}|${date}`
export const keyOf = (edit: EditBody) => cellKey(edit.hospital_id, edit.date)

type PendingEdits = {
  edits: Record<string, EditBody>
  put: (edit: EditBody) => void
  drop: (key: string) => void
  clear: () => void
}

/** The draft's edits not yet saved, held in the page alone, in Zustand. One store per page, so another month, another
 *  visit or another test starts from nothing; the page asks before anything would throw them away. */
export function usePendingEdits() {
  const [store] = useState(() => createStore<PendingEdits>((set) => ({
    edits: {},
    put: (edit) => set((state) => ({ edits: { ...state.edits, [keyOf(edit)]: edit } })),
    drop: (key) => set((state) => {
      const { [key]: _, ...rest } = state.edits
      return { edits: rest }
    }),
    clear: () => set({ edits: {} }),
  })))
  return useStore(store)
}
