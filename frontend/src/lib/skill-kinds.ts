import { SkillBodyKind } from "@/api/generated/model"

export const Kind = SkillBodyKind
export type Kind = SkillBodyKind

/** The three kinds in the order every table lists them, each with its group's heading. */
export const KINDS: { kind: Kind; one: string; heading: string }[] = [
  { kind: Kind.skill, one: "Skill", heading: "Skills" },
  { kind: Kind.certificate, one: "Certificate", heading: "Certificates" },
  { kind: Kind.licence, one: "Licence", heading: "Licences" },
]

/** Rows grouped by their kind, in the kinds' order and each group in the order given; a kind with no rows is left out.
 *  Every table that lists catalogue entries or a nurse's records groups them so. */
export function byKind<Row>(rows: Row[], kindOf: (row: Row) => Kind): { kind: Kind; heading: string; rows: Row[] }[] {
  return KINDS.map(({ kind, heading }) => ({ kind, heading, rows: rows.filter((row) => kindOf(row) === kind) }))
    .filter((group) => group.rows.length > 0)
}
