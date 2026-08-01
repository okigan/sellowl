import { useState } from "react";
import type { Condition, Verdict, VerdictKind } from "../api";
import { money } from "../api";

const CONDITION_STYLE: Record<Condition, string> = {
  clean: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/25",
  usable: "bg-sky-500/10 text-sky-300 ring-sky-500/25",
  rough: "bg-amber-500/10 text-amber-300 ring-amber-500/25",
  unknown: "bg-white/5 text-muted ring-white/10",
};

export function ConditionChip({ condition, evidence }: { condition: Condition; evidence?: string }) {
  return (
    <span
      title={evidence || "No photo evidence — graded from the title only."}
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${CONDITION_STYLE[condition]}`}
    >
      {condition}
    </span>
  );
}

const VERDICT_COPY: Record<VerdictKind, string> = {
  underpriced: "underpriced",
  overpriced: "overpriced",
  fair: "fair",
  insufficient_data: "no data",
};

export function VerdictChip({ kind }: { kind: VerdictKind }) {
  const style =
    kind === "underpriced"
      ? "text-money"
      : kind === "overpriced"
        ? "text-bad"
        : kind === "fair"
          ? "text-muted"
          : "text-muted/60";
  return <span className={`text-[11px] font-medium ${style}`}>{VERDICT_COPY[kind]}</span>;
}

/** The number the whole app exists to produce. Only this gets the accent. */
export function Opportunity({ verdict }: { verdict: Verdict | null }) {
  if (!verdict || verdict.opportunity_usd === null) {
    return <span className="text-muted/50">—</span>;
  }
  const value = verdict.opportunity_usd;
  if (verdict.kind === "insufficient_data") return <span className="text-muted/50">—</span>;
  const positive = value > 0;
  return (
    <span className={positive ? "text-money font-semibold" : "text-bad"}>
      {positive ? "+" : ""}
      {money(value)}
    </span>
  );
}

export function Band({ band }: { band: { p25: number; p50: number; p75: number; n: number } | null }) {
  if (!band) return <span className="text-muted/50">—</span>;
  return (
    <span className="whitespace-nowrap">
      <span className="text-text">{money(band.p50)}</span>
      <span className="text-muted/70 text-[11px]">
        {" "}
        ({money(band.p25)}–{money(band.p75)}, n={band.n})
      </span>
    </span>
  );
}

export function VenueTag({ venue }: { venue: string | null }) {
  if (!venue) return <span className="text-muted/50">—</span>;
  const local = venue === "fb_local";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] ring-1 ring-inset ${
        local
          ? "bg-violet-500/10 text-violet-300 ring-violet-500/25"
          : "bg-blue-500/10 text-blue-300 ring-blue-500/25"
      }`}
    >
      {local ? "sell local" : "sell on eBay"}
    </span>
  );
}

const THUMB_SIZE = {
  sm: "h-10 w-10",
  lg: "h-16 w-16",
  xl: "h-20 w-20",
} as const;

export function Thumb({
  src,
  alt,
  size = "sm",
}: {
  src: string;
  alt: string;
  size?: keyof typeof THUMB_SIZE;
}) {
  const dims = THUMB_SIZE[size];
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return <div className={`${dims} shrink-0 rounded bg-line`} aria-hidden />;
  }
  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      className={`${dims} shrink-0 rounded object-cover ring-1 ring-line`}
      onError={() => setFailed(true)}
    />
  );
}

/** Right dimensions so nothing reflows when the real row lands. */
export function SkeletonRow() {
  return (
    <tr className="border-b border-line/60">
      <td className="px-3 py-3">
        <div className="flex items-center gap-3">
          <div className="skeleton h-20 w-20 rounded" />
          <div className="skeleton h-3 w-48 rounded" />
        </div>
      </td>
      {Array.from({ length: 5 }).map((_, i) => (
        <td key={i} className="px-3 py-3">
          <div className="skeleton h-3 w-16 rounded" />
        </td>
      ))}
    </tr>
  );
}
