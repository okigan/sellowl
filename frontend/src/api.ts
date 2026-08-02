export type Condition = "rough" | "usable" | "clean" | "unknown";
export type VerdictKind = "underpriced" | "overpriced" | "fair" | "insufficient_data";
export type Venue = "ebay_sold" | "fb_local";
export type JobState = "queued" | "running" | "done" | "failed";

export interface PriceBand {
  p25: number;
  p50: number;
  p75: number;
  p90: number;
  n: number;
}

export interface Verdict {
  kind: VerdictKind;
  reason: string;
  sold_band: PriceBand | null;
  local_band: PriceBand | null;
  target_low: number | null;
  target_high: number | null;
  target: number | null;
  recommended_venue: Venue | null;
  ebay_net: number | null;
  local_net: number | null;
  current_net: number | null;
  opportunity_usd: number | null;
  shipping_estimate: number | null;
  days_listed: number | null;
  // Which bucket the band actually came from: "model+condition" (narrowest),
  // "condition" (blended across models), or "all" (blended across
  // conditions too). The reason text already discloses "all" in prose; this
  // is here for anything that wants to key off it directly.
  sold_band_tier: string | null;
  local_band_tier: string | null;
  local_ask_discount: number | null;
}

export interface SpecAdjustment {
  feature: string;
  comp_amount: string;
  item_amount: string;
  factor: number;
  /** False for dimensions shown for context but deliberately not priced
   * (form factor: a 140mm fan is a different fan, not more fan). */
  scaled: boolean;
}

export interface Comp {
  external_id: string;
  venue: Venue;
  title: string;
  price: number | null;
  url: string;
  photo_url: string;
  city: string;
  state: string;
  delivery: string[];
  sold_at: string | null;
  is_sold: boolean;
  condition: Condition;
  condition_evidence: string;
  description: string;
  score: number;
  price_note: string;
  spec_adjustments: SpecAdjustment[];
}

export interface Vision {
  canonical_description: string;
  attributes: Record<string, string>;
  condition: Condition;
  condition_evidence: string;
  search_query_broad: string;
  search_query_narrow: string;
}

export interface Item {
  external_id: string;
  title: string;
  ask_price: number | null;
  url: string;
  photo_url: string;
  days_listed: number | null;
  vision: Vision | null;
  comps: Comp[];
  verdict: Verdict | null;
}

export interface JobStatus {
  job_id: string;
  status: JobState;
  stage: string;
  detail: string;
  done: number;
  total: number;
  error: string;
  item_count: number;
}

export interface Health {
  ok: boolean;
  elastic_configured: boolean;
  search_backend: string;
  comp_source: string;
  embedding_model: string;
  apify_configured: boolean;
  vision_configured: boolean;
  default_store_url: string;
  metro: string;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text();
    let detail = body;
    try {
      detail = (JSON.parse(body) as { detail?: string }).detail ?? body;
    } catch {
      /* plain text error */
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => fetch("/health").then(json<Health>),

  analyze: (store_url: string, metro: string) =>
    fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ store_url, metro }),
    }).then(json<{ job_id: string }>),

  job: (id: string) => fetch(`/api/jobs/${id}`).then(json<JobStatus>),

  items: (id: string) => fetch(`/api/jobs/${id}/items`).then(json<Item[]>),

  revisePayload: (jobId: string, externalId: string, apiKey: string, price: number | null) =>
    fetch(`/api/items/${jobId}/${encodeURIComponent(externalId)}/revise-payload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey, price }),
    }).then(json<Record<string, unknown>>),
};

export const money = (value: number | null | undefined, dp = 0): string =>
  value === null || value === undefined
    ? "—"
    : value.toLocaleString("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: dp,
        maximumFractionDigits: dp,
      });

/** Stage machine names -> something a human can read while waiting. */
export const STAGE_COPY: Record<string, string> = {
  queued: "Queued",
  scraping_store: "Reading your store",
  reading_photos: "Looking at photos",
  finding_comps: "Hunting comps",
  indexing: "Indexing comps",
  matching: "Matching and pricing",
  done: "Done",
};
