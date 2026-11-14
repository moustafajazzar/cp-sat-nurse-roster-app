/* PROTOTYPE, throw away. Question: what one pattern do long rules and settings forms, and the catalogue pages, share?
   Three layouts on the real Unit rules form and the real Shifts page, behind ?variant=a|b|c and a floating bar:
   A "Long page"   every section open on one page, a sticky section index (a column on desktop, a chip row on a phone), and a
                   sticky Save bar that says when something is not saved. Shifts: the table, the form below it.
   B "Collapsed"   each section closed under its heading, opened one or more at a time, with the same sticky Save bar.
                   Shifts: a row's Edit opens the form in a side drawer.
   C "One section" settings-style: a list of sections, one shown at a time (on a phone the list, then the section with Back),
                   still one form and one Save. Shifts: the list beside its detail.
   The form, its validation and its save are the real ones; only the layout changes. "Ward" tokens (index.css .ward). */
import { Link, useNavigate, useRouterState } from "@tanstack/react-router"
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { createContext, type ReactNode, useContext, useEffect, useState } from "react"

import { cn } from "@/lib/utils"

export type FormVariant = "a" | "b" | "c"
const NAMES: Record<FormVariant, string> = { a: "Long page", b: "Collapsed", c: "One section" }
const KEYS: FormVariant[] = ["a", "b", "c"]

type Layout = { variant: FormVariant; active: string; setActive: (title: string) => void }
const LayoutContext = createContext<Layout | null>(null)
export const useFormLayout = () => useContext(LayoutContext)
export const sectionId = (title: string) => `section-${title.toLowerCase().replaceAll(" ", "-")}`

/** The page frame: Ward tokens, the section index or list, and the prototype's switcher. */
export function FormLayoutFrame({ variant, sections, children }: { variant: FormVariant; sections: string[]; children: ReactNode }) {
  const [active, setActive] = useState(sections[0] ?? "")
  const [phoneList, setPhoneList] = useState(true)
  const [dark, setDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches)
  const choose = (title: string) => { setActive(title); setPhoneList(false); if (variant === "a") document.getElementById(sectionId(title))?.scrollIntoView({ behavior: "smooth", block: "start" }) }
  const index = sections.length > 0 && (
    <nav aria-label="Sections" className={cn(variant === "a" && "max-md:sticky max-md:top-0 max-md:z-20 max-md:-mx-4 max-md:overflow-x-auto max-md:border-b max-md:bg-background max-md:px-4 max-md:py-2 md:sticky md:top-4 md:self-start",
      variant === "c" && cn("md:sticky md:top-4 md:self-start", !phoneList && "max-md:hidden"))}>
      <ul className={cn("flex gap-1", variant === "a" ? "max-md:flex-row md:flex-col" : "flex-col", variant === "c" && "max-md:divide-y max-md:rounded-lg max-md:border max-md:bg-[var(--ward-surface)]")}>
        {sections.map((title) => (
          <li key={title}>
            <button type="button" onClick={() => choose(title)} aria-current={active === title ? "true" : undefined}
              className={cn("flex w-full items-center gap-2 rounded-[5px] px-2.5 py-1.5 text-start text-sm whitespace-nowrap text-muted-foreground hover:bg-muted hover:text-foreground",
                active === title && "bg-accent font-medium text-foreground", variant === "c" && "max-md:min-h-11 max-md:rounded-none max-md:bg-transparent max-md:text-base max-md:text-foreground")}>
              {title}{variant === "c" && <ChevronRightIcon className="ms-auto size-4 md:hidden" />}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
  return (
    <LayoutContext.Provider value={{ variant, active, setActive }}>
      <div className={cn("ward -m-4 min-h-svh bg-background p-4 pb-24 text-foreground", dark && "dark")}>
        <div className={cn("mx-auto grid max-w-5xl gap-6", (variant === "a" || variant === "c") && sections.length > 0 && "md:grid-cols-[13rem_minmax(0,1fr)]")}>
          {(variant === "a" || variant === "c") && index}
          <div className={cn("min-w-0", variant === "c" && sections.length > 0 && phoneList && "max-md:hidden")}>
            {variant === "c" && sections.length > 0 && (
              <button type="button" onClick={() => setPhoneList(true)} className="mb-3 flex items-center gap-1 text-sm font-medium text-primary md:hidden"><ChevronLeftIcon className="size-4" />All sections</button>
            )}
            {children}
          </div>
        </div>
        <Switcher variant={variant} dark={dark} onDark={() => setDark(!dark)} />
      </div>
    </LayoutContext.Provider>
  )
}

/** One section of a form, laid out by the variant. Outside a prototype frame it returns null, and the caller draws its own. */
export function LayoutSection({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  const layout = useFormLayout()
  if (!layout) return null
  const id = sectionId(title)
  const head = <><h2 id={id} className="text-base font-semibold">{title}</h2>{description && <p className="text-sm text-muted-foreground">{description}</p>}</>
  if (layout.variant === "b") return (
    <details className="group rounded-lg border bg-[var(--ward-surface)] open:pb-4" open={layout.active === title} onToggle={(event) => { if ((event.target as HTMLDetailsElement).open) layout.setActive(title) }}>
      <summary className="flex min-h-12 cursor-pointer list-none items-center gap-2 px-4 py-3 [&::-webkit-details-marker]:hidden">
        <span className="min-w-0 flex-1">{head}</span><ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
      </summary>
      <div className="px-4">{children}</div>
    </details>
  )
  // C keeps every section mounted, so the one form keeps every value; only the chosen one shows.
  return (
    <section role="region" aria-labelledby={id} hidden={layout.variant === "c" && layout.active !== title}
      className={cn("scroll-mt-16 rounded-lg border bg-[var(--ward-surface)] p-4", layout.variant === "c" && "border-0 bg-transparent p-0")}>
      <div className="mb-3 flex flex-col gap-1">{head}</div>{children}
    </section>
  )
}

/** The Save bar: sticky at the bottom, and it says when something is not saved. */
export function SaveBar({ dirty, children }: { dirty: boolean; children: ReactNode }) {
  return (
    <div className="sticky bottom-3 z-20 flex items-center gap-3 rounded-lg border bg-[var(--ward-surface)] px-4 py-2.5 shadow-lg">
      <span className={cn("text-sm", dirty ? "font-medium text-[var(--ward-warn)]" : "text-muted-foreground")}>{dirty ? "Changes not saved" : "Everything is saved"}</span>
      <span className="flex-1" />{children}
    </div>
  )
}

function Switcher({ variant, dark, onDark }: { variant: FormVariant; dark: boolean; onDark: () => void }) {
  const navigate = useNavigate()
  const location = useRouterState({ select: (state) => state.location })
  const go = (by: number) => ({ ...(location.search as Record<string, unknown>), variant: KEYS[(KEYS.indexOf(variant) + by + 3) % 3] })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest("input, textarea, select, [contenteditable]")) return
      const by = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0
      if (by) void navigate({ to: location.pathname, search: go(by) as never, replace: true })
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  })
  return (
    <div role="toolbar" aria-label="Prototype controls" className="fixed top-2 left-1/2 z-50 flex -translate-x-1/2 items-center gap-0.5 rounded-full bg-neutral-900 p-1 text-[12px] text-white shadow-xl">
      <Link to={location.pathname} replace search={go(-1) as never} className="rounded-full px-2.5 py-1 hover:bg-neutral-700" aria-label="Previous variant">←</Link>
      <span className="px-1.5 whitespace-nowrap">{variant.toUpperCase()} · {NAMES[variant]}</span>
      <Link to={location.pathname} replace search={go(1) as never} className="rounded-full px-2.5 py-1 hover:bg-neutral-700" aria-label="Next variant">→</Link>
      <button type="button" className="rounded-full px-2.5 py-1 hover:bg-neutral-700" onClick={onDark}>{dark ? "Dark" : "Light"}</button>
    </div>
  )
}
