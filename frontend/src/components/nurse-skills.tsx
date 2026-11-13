import { type ReactNode, useId } from "react"

import type { NurseSkillRow } from "@/api/generated/model"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { byKind } from "@/lib/skill-kinds"
import { scrollableRegion, shortDay } from "@/lib/utils"

/** The last day a skill counts, in the page's words. Whether that day has passed is the expiries page's to say,
 *  since it needs the hospital's today. */
export const untilWords = (skill: NurseSkillRow) => (skill.expiry ? `until ${shortDay(skill.expiry)}` : "")

/** A nurse's skills in a table cell, under Skills, Certificates and Licences: each name, its expiry, and whether the CNO has
 *  retired it. Never the identifier or the issue date, which the records' own table shows to those who may read them. */
export function SkillList({ skills }: { skills: NurseSkillRow[] }) {
  const id = useId()
  if (skills.length === 0) return <span className="text-muted-foreground">None</span>
  return (
    <div className="flex flex-col gap-2">
      {byKind(skills, (skill) => skill.kind).map((group) => (
        <div key={group.kind} className="flex flex-col gap-1">
          <p id={`${id}-${group.kind}`} className="text-xs font-medium text-muted-foreground">{group.heading}</p>
          <ul aria-labelledby={`${id}-${group.kind}`} className="flex flex-col items-start gap-1">
            {group.rows.map((skill) => (
              <li key={skill.skill_id} className="flex flex-wrap items-center gap-1">
                <span>{skill.name}</span>
                {skill.expiry && <span className="tabular-nums text-muted-foreground">{untilWords(skill)}</span>}
                {!skill.active && <Badge variant="outline">Retired</Badge>}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

type RecordsTableProps = {
  skills: NurseSkillRow[]
  /** The table's name, for its scrolling region and its own name. */
  label: string
  /** Whether the reader reads identifiers and issue dates: the CNO, the Deputy CNO and the nurse herself. */
  details: boolean
  /** A cell of buttons for each record, under an Actions column. */
  actions?: (skill: NurseSkillRow) => ReactNode
  /** False inside another table that scrolls already, so that each nurse's row adds no stop for the keyboard. */
  scrolls?: boolean
}

/** A nurse's records, grouped under Skills, Certificates and Licences, each heading a row group with its own name.
 *  Within a group, in the order of their names, as the API gives them. Scrolls inside itself on a phone. */
export function RecordsTable({ skills, label, details, actions, scrolls = true }: RecordsTableProps) {
  const id = useId()
  const columns = ["Name", "Expiry", ...(details ? ["Identifier", "Issue date"] : []), ...(actions ? ["Actions"] : [])]
  return (
    // A region a keyboard scrolls only when nothing in it takes focus: its buttons scroll it otherwise.
    <Table container={scrolls && !actions ? scrollableRegion(label) : undefined} aria-label={label}>
      <TableHeader>
        <TableRow>
          {columns.map((column) => (
            <TableHead key={column}>{column === "Actions" ? <span className="sr-only">Actions</span> : column}</TableHead>
          ))}
        </TableRow>
      </TableHeader>
      {byKind(skills, (skill) => skill.kind).map((group) => (
        <TableBody key={group.kind} aria-labelledby={`${id}-${group.kind}`}>
          <TableRow className="hover:bg-transparent">
            <TableHead id={`${id}-${group.kind}`} scope="rowgroup" colSpan={columns.length}
              className="bg-muted/50 font-medium text-foreground">{group.heading}</TableHead>
          </TableRow>
          {group.rows.map((skill) => (
            <TableRow key={skill.skill_id}>
              <TableCell>
                <span className="flex flex-wrap items-center gap-1">
                  {skill.name}
                  {!skill.active && <Badge variant="outline">Retired</Badge>}
                </span>
              </TableCell>
              <TableCell className="tabular-nums">
                {skill.expiry ? shortDay(skill.expiry) : <span className="text-muted-foreground">Does not expire</span>}
              </TableCell>
              {details && (
                <>
                  <TableCell translate="no">{skill.details?.identifier ?? <span className="text-muted-foreground">None</span>}</TableCell>
                  <TableCell className="tabular-nums">
                    {skill.details?.issue_date ? shortDay(skill.details.issue_date) : <span className="text-muted-foreground">Not recorded</span>}
                  </TableCell>
                </>
              )}
              {actions && <TableCell><div className="flex flex-wrap gap-2">{actions(skill)}</div></TableCell>}
            </TableRow>
          ))}
        </TableBody>
      ))}
    </Table>
  )
}
