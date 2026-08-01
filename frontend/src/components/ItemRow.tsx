import { useState } from "react";
import type { Comp, Item } from "../api";
import { money } from "../api";
import { Band, ConditionChip, Opportunity, Thumb, VenueTag, VerdictChip } from "./Bits";

function CompRow({ comp }: { comp: Comp }) {
  return (
    <tr className="text-[12px]">
      <td className="py-2.5 pl-3 pr-2">
        <div className="flex items-center gap-2">
          <Thumb src={comp.photo_url} alt="" size="lg" />
          <a
            href={comp.url || undefined}
            target="_blank"
            rel="noreferrer"
            className="line-clamp-2 max-w-[28rem] text-text/80 hover:text-text hover:underline"
          >
            {comp.title}
          </a>
        </div>
      </td>
      <td className="px-2 py-2.5">
        <span className="rounded bg-overlay-1 px-1.5 py-0.5 text-[10px] text-muted">
          {comp.venue === "ebay_sold" ? "sold" : "asking"}
        </span>
      </td>
      <td className="px-2 py-2.5">
        {money(comp.price)}
        {comp.price_note && (
          <span className="ml-1 cursor-help text-warn" title={comp.price_note}>
            *
          </span>
        )}
      </td>
      <td className="px-2 py-2.5">
        <ConditionChip condition={comp.condition} evidence={comp.condition_evidence} />
      </td>
      <td className="px-2 py-2.5 text-muted">
        {comp.city ? `${comp.city}${comp.state ? `, ${comp.state}` : ""}` : "—"}
      </td>
      <td className="px-2 py-2.5 text-right text-muted/70">{comp.score.toFixed(3)}</td>
    </tr>
  );
}

export function ItemRow({ item, index }: { item: Item; index: number }) {
  const [open, setOpen] = useState(false);
  const verdict = item.verdict;
  const condition = item.vision?.condition ?? "unknown";

  return (
    <>
      <tr
        className="row-in cursor-pointer border-b border-line/60 hover:bg-overlay-hover"
        style={{ animationDelay: `${Math.min(index, 12) * 25}ms` }}
        onClick={() => setOpen((v) => !v)}
      >
        <td className="px-3 py-3">
          <div className="flex items-center gap-3">
            <span
              className={`text-muted/50 transition-transform ${open ? "rotate-90" : ""}`}
              aria-hidden
            >
              ›
            </span>
            <Thumb src={item.photo_url} alt="" size="xl" />
            <div className="min-w-0">
              <div className="truncate max-w-[22rem] text-[13px]">{item.title}</div>
              <div className="flex items-center gap-2 pt-0.5">
                <ConditionChip
                  condition={condition}
                  evidence={item.vision?.condition_evidence}
                />
                {verdict && <VerdictChip kind={verdict.kind} />}
              </div>
            </div>
          </div>
        </td>
        <td className="px-3 py-3 text-[13px]">{money(item.ask_price)}</td>
        <td className="px-3 py-3 text-[13px]">
          <Band band={verdict?.sold_band ?? null} />
        </td>
        <td className="px-3 py-3 text-[13px]">
          {verdict?.target ? (
            <span>
              {money(verdict.target)}
              <span className="text-muted/70 text-[11px]">
                {" "}
                ({money(verdict.target_low)}–{money(verdict.target_high)})
              </span>
              {verdict.recommended_venue === "fb_local" && (
                <span
                  className="ml-1 text-[10px] text-muted/60"
                  title="This target is from local asks, not the eBay sold band shown to the left."
                >
                  local
                </span>
              )}
            </span>
          ) : (
            <span className="text-muted/50">—</span>
          )}
        </td>
        <td className="px-3 py-3">
          <VenueTag venue={verdict?.recommended_venue ?? null} />
        </td>
        <td className="px-3 py-3 text-right text-[13px]">
          <Opportunity verdict={verdict} />
        </td>
      </tr>

      {open && (
        <tr className="border-b border-line/60 bg-black/20">
          <td colSpan={6} className="px-3 py-3">
            {verdict && (
              <p className="pb-3 text-[12px] text-muted">
                {verdict.reason}
                {verdict.shipping_estimate !== null && (
                  <span className="text-muted/60">
                    {" "}
                    · shipping est. {money(verdict.shipping_estimate)} (rough, by size)
                  </span>
                )}
              </p>
            )}

            {verdict?.current_net !== null && verdict?.current_net !== undefined && (
              <p className="pb-3 text-[12px] text-muted/60">
                You'd net {money(verdict.current_net)} today at {money(item.ask_price)} on eBay
                {" vs. "}
                {money(
                  verdict.recommended_venue === "fb_local" ? verdict.local_net : verdict.ebay_net,
                )}{" "}
                selling {verdict.recommended_venue === "fb_local" ? "locally" : "on eBay"} at the
                target — the gap is the opportunity number.
              </p>
            )}

            {item.vision?.canonical_description && (
              <p className="pb-3 text-[12px] text-muted">
                <span className="text-muted/60">Read from the photo: </span>
                {item.vision.canonical_description}
              </p>
            )}

            {verdict?.local_band && (
              <p className="pb-3 text-[12px] text-muted">
                <span className="text-muted/60">Local asks: </span>
                <Band band={verdict.local_band} />
                {verdict.local_net === null && (
                  <span className="text-muted/60">
                    {" "}
                    — spread too wide to trust, ignored for pricing
                  </span>
                )}
              </p>
            )}

            {item.comps.length === 0 ? (
              <p className="text-[12px] text-muted/60">
                No comps survived matching. Nothing to price against.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[46rem]">
                  <thead>
                    <tr className="text-left text-[10px] uppercase tracking-wide text-muted/60">
                      <th className="pb-1 pl-3 font-medium">
                        Comps behind this verdict ({item.comps.length})
                      </th>
                      <th className="px-2 pb-1 font-medium">Kind</th>
                      <th className="px-2 pb-1 font-medium">Price</th>
                      <th className="px-2 pb-1 font-medium">Condition</th>
                      <th className="px-2 pb-1 font-medium">Where</th>
                      <th className="px-2 pb-1 text-right font-medium">RRF</th>
                    </tr>
                  </thead>
                  <tbody>
                    {item.comps.map((comp) => (
                      <CompRow key={`${comp.venue}:${comp.external_id}`} comp={comp} />
                    ))}
                  </tbody>
                </table>
                {item.comps.some((c) => c.price_note) && (
                  <p className="pt-2 text-[11px] text-muted/60">
                    <span className="text-warn">*</span> price adjusted for a different
                    capacity/spec than this item's — hover for detail.
                  </p>
                )}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
