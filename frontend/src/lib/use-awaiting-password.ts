import { type Mutation, useIsMutating, useQueryClient } from "@tanstack/react-query"

import {
  getConfirmNurseImportMutationKey,
  getCreateNurseMutationKey,
  getCreateUserMutationKey,
  getResetNursePasswordMutationKey,
  getResetUserPasswordMutationKey,
} from "@/api/generated/endpoints"

/** Every request whose answer holds a generated password. A new one belongs here. */
const ANSWERED_WITH_A_PASSWORD = new Set<unknown>([
  getConfirmNurseImportMutationKey(),
  getCreateNurseMutationKey(),
  getCreateUserMutationKey(),
  getResetNursePasswordMutationKey(),
  getResetUserPasswordMutationKey(),
].flat())

const answeredWithAPassword = ({ options }: Mutation<any, any, any, any>) => ANSWERED_WITH_A_PASSWORD.has(options.mutationKey?.[0]) // eslint-disable-line @typescript-eslint/no-explicit-any

/** Whether such a request is running. The answer reaches only a component still mounted when it comes, and nobody can
 *  ask for that password again, so a page holds still until then. From the cache, because the form that sent the
 *  request is not the page or the header that has to know. */
export function useAwaitingPassword() {
  return useIsMutating({ predicate: answeredWithAPassword }) > 0
}

/** For someone who chose to leave without it. The request runs on, but nothing waits for it any more, or the next page
 *  would hold still for an answer that can never reach it. */
export function useAbandonAwaitedPassword() {
  const cache = useQueryClient().getMutationCache()
  return () => cache.findAll({ status: "pending", predicate: answeredWithAPassword }).forEach((mutation) => cache.remove(mutation))
}
