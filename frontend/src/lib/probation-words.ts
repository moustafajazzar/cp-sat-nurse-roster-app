/** The words a roster shows of a nurse on probation, on the grid, My roster and the print page alike. */

/** The heading her rows sit under: she works beside her preceptor and counts toward no shift. */
export const ON_PROBATION = "On probation"

/** What the heading means, in each legend that shows it. */
export const ON_PROBATION_MEANS = `${ON_PROBATION}: works beside their preceptor and is not counted toward any shift`

/** The line under her name. */
export const preceptorLine = (name: string | null) => name ? `Preceptor: ${name}` : "No preceptor"
